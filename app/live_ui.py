"""24H Playlist LIVE Studio (Toplevel 창).

로직은 live_controller.py / live_ready.py / cloud_client.py에 있고 이 파일은 Tk 위젯만 다룬다.
backend 스레드는 위젯을 직접 만지지 않는다: 이벤트 큐 → after() 폴링.
Stream Key는 어떤 messagebox/title/로그에도 표시하지 않는다.

평소 화면에는 전문용어(ssh/systemd/ffmpeg 명령 등)를 숨기고 [상세 보기]에서만 보여준다.
"""
from __future__ import annotations

import queue
import secrets as pysecrets
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .cloud_client import CloudClient, CloudLiveController, CloudStatus, fix_key_permissions
from .cloud_model import CLOUD_UNAVAILABLE, FREE_UNSURE, CloudConfigError, load_cloud_profile
from .core import format_duration
from .live_controller import LOCKED_STATES, LiveController, describe_input, format_bitrate, probe_live_input, run_preflight
from .live_profile import (
    DEFAULT_PRESET_KEY, LIVE_PRESETS, MODE_COPY, MODE_TRANSCODE, YOUTUBE_RTMPS_INGEST, LiveConfigError,
    mask_secret, preset_by_key, recommend_preset, redact,
)
from .live_playlist import LivePlaylist, PlaylistError, entry_durations, validate_playlist, write_ffconcat
from .live_ready import LiveReadyCancelled, analyze_live_ready, make_live_ready_file
from .live_session import (
    ARCHIVE_SAFE_SECONDS, SESSION_ARCHIVE_SAFE, SESSION_CONTINUOUS, ManualSessionProvider, archive_notice,
    session_limit_seconds,
)
from .live_secrets import default_key_store
from .live_supervisor import LiveBusyError, LiveState, busy_message
from .settings import settings_dir
from .tooling import FFMPEG_GUARD, release_tk_variables

KEY_REVEAL_MS = 8000
TICK_MS = 500
LOC_CLOUD, LOC_LOCAL = "cloud", "local"
SEND_AUTO, SEND_TRANSCODE = "auto", "transcode"
CONVERT_OWNER = "convert"


def ask_cloud_close(parent, message: str) -> str:
    """[PC만 종료] / [LIVE도 종료] / [취소] → "pc" | "stop" | "cancel". 기본(권장)은 PC만 종료."""
    result = {"v": "cancel"}
    d = tk.Toplevel(parent)
    d.title("Cloud LIVE 방송 중")
    d.transient(parent)
    d.resizable(False, False)
    ttk.Label(d, text=message, padding=16, justify="left").pack()
    row = ttk.Frame(d, padding=(16, 0, 16, 16)); row.pack()

    def pick(v):
        result["v"] = v
        d.destroy()
    b = ttk.Button(row, text="PC만 종료 (권장)", command=lambda: pick("pc"))
    b.pack(side="left")
    ttk.Button(row, text="LIVE도 종료", command=lambda: pick("stop")).pack(side="left", padx=6)
    ttk.Button(row, text="취소", command=lambda: pick("cancel")).pack(side="left")
    d.bind("<Escape>", lambda e: pick("cancel"))
    d.bind("<Return>", lambda e: pick("pc"))
    b.focus_set()
    d.grab_set()
    parent.wait_window(d)
    return result["v"]


CLOUD_CLOSE_MESSAGE = "Cloud에서 LIVE가 계속 방송 중입니다.\n\nPC 프로그램만 종료할까요?"


def default_cloud_client() -> CloudClient:
    """CloudLiveController 스레드가 호출한다 — Tk 창을 참조하지 않는 모듈 함수여야 한다."""
    profile = load_cloud_profile()
    if profile is None:
        raise CloudConfigError("무료 Cloud가 아직 설정되지 않았습니다. [처음 설정 도우미]를 진행하세요.")
    return CloudClient(profile.validated())

# 주의: 백그라운드 스레드의 함수는 Tk 객체(self, 위젯, StringVar)를 참조하면 안 된다.
# 스레드가 Tk 창의 마지막 참조를 놓으면 tkinter Variable이 다른 스레드에서 해제되어
# "main thread is not in main loop" 오류로 Tk 상태가 깨진다. 큐/일반 값만 넘긴다.


class LiveWindow(tk.Toplevel):
    def __init__(self, master, *, tools: Callable[[], tuple], controller: LiveController | None = None, key_store=None,
                 cloud: CloudLiveController | None = None):
        super().__init__(master)
        self.title("24H Playlist LIVE Studio")
        self.geometry("880x760")
        self.minsize(640, 480)
        self._tools = tools
        self.controller = controller or LiveController()
        self.store = key_store or default_key_store()
        self.cloud = cloud or CloudLiveController(default_cloud_client)
        self._closing = False
        self._reveal_job = None
        self._tick_job = None
        self._failed_shown = False
        self._ui_q: queue.Queue = queue.Queue()
        self.ready_report = None
        self._analyze_token = 0
        self._convert_thread: threading.Thread | None = None
        self._convert_cancel = threading.Event()
        self._cloud_msg = ""
        self._cloud_progress = ""
        self._cloud_reachable: bool | None = None

        self.input_path = tk.StringVar()
        self.source_mode = tk.StringVar(value="single")  # single | playlist
        self.session_mode = tk.StringVar(value=SESSION_CONTINUOUS)
        self.playlist = LivePlaylist()
        self.playlist_summary = tk.StringVar(value="[영상 추가]로 LIVE READY MP4를 2개 이상 넣으세요.")
        self.next_session_msg = tk.StringVar()
        self.session_provider = ManualSessionProvider()
        self.input_info = tk.StringVar(value="LIVE로 송출할 완성 MP4를 선택하세요.")
        self.ready_text = tk.StringVar()
        self.location = tk.StringVar(value=LOC_CLOUD)
        self.cloud_line = tk.StringVar()
        self.send_mode = tk.StringVar(value=SEND_AUTO)
        self.send_note = tk.StringVar()
        self.server_mode = tk.StringVar(value="youtube")
        self.custom_url = tk.StringVar()
        self.key_var = tk.StringVar()
        self.key_note = tk.StringVar()
        self.remember = tk.BooleanVar(value=False)
        self.preset_label = tk.StringVar(value=preset_by_key(DEFAULT_PRESET_KEY).label)
        self.preset_detail = tk.StringVar()
        self.reconnect = tk.BooleanVar(value=True)
        self.keep_awake = tk.BooleanVar(value=True)
        self.confirm_stop = tk.BooleanVar(value=True)
        self.st = {k: tk.StringVar(value="-") for k in (
            "state", "where", "session", "fps", "bitrate", "speed", "out_time", "reconnects", "retry", "exit",
            "error", "media", "mode", "server", "disk", "playlist", "session_time", "session_left")}
        self._height = 0

        self._ui()
        self._load_saved_key()
        self._update_preset_detail()
        self._update_cloud_line()
        self.protocol("WM_DELETE_WINDOW", self.request_close)
        if load_cloud_profile() is not None:
            self.cloud.check_async()
            self.cloud.start_polling()
        self._tick()


    # ---------- layout ----------
    def _scroll_area(self):
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=12)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        wheel = lambda e: canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        self._canvas = canvas
        return inner

    def _ui(self):
        root = self._scroll_area()
        head = ttk.Frame(root); head.pack(anchor="w")
        ttk.Label(head, text="●", foreground="red", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(head, text=" 24H Playlist LIVE Studio", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(root, text="완성 MP4 1개 또는 여러 개(Playlist)를 YouTube LIVE로 무한 반복 송출합니다.").pack(anchor="w", pady=(0, 6))

        # ① 영상 + LIVE READY
        f1 = ttk.LabelFrame(root, text="① LIVE 영상", padding=7)
        f1.pack(fill="x")
        mr = ttk.Frame(f1); mr.pack(fill="x", pady=(0, 4))
        self.rb_single = ttk.Radiobutton(mr, text="단일 영상", variable=self.source_mode, value="single",
                                         command=self._on_source_mode)
        self.rb_single.pack(side="left")
        self.rb_playlist = ttk.Radiobutton(mr, text="여러 영상 Playlist (순서대로 반복)", variable=self.source_mode,
                                           value="playlist", command=self._on_source_mode)
        self.rb_playlist.pack(side="left", padx=(12, 0))
        self.single_frame = ttk.Frame(f1); self.single_frame.pack(fill="x")
        r = ttk.Frame(self.single_frame); r.pack(fill="x")
        ttk.Entry(r, textvariable=self.input_path, state="readonly").pack(side="left", fill="x", expand=True)
        self.btn_video = ttk.Button(r, text="영상 선택", command=self._pick_video)
        self.btn_video.pack(side="left", padx=(5, 0))
        ttk.Label(self.single_frame, textvariable=self.input_info).pack(anchor="w", pady=(5, 0))
        self.lbl_ready = ttk.Label(self.single_frame, textvariable=self.ready_text, justify="left")
        self.lbl_ready.pack(anchor="w", pady=(4, 0))
        # Playlist (DIRECT COPY 전용, 순차 반복)
        self.playlist_frame = ttk.Frame(f1)
        pb = ttk.Frame(self.playlist_frame); pb.pack(fill="x")
        self.btn_pl_add = ttk.Button(pb, text="영상 추가", command=self._pl_add)
        self.btn_pl_remove = ttk.Button(pb, text="제거", command=self._pl_remove)
        self.btn_pl_up = ttk.Button(pb, text="▲ 위로", command=lambda: self._pl_move(-1))
        self.btn_pl_down = ttk.Button(pb, text="▼ 아래로", command=lambda: self._pl_move(1))
        self.btn_pl_clear = ttk.Button(pb, text="전체 지우기", command=self._pl_clear)
        for b in (self.btn_pl_add, self.btn_pl_remove, self.btn_pl_up, self.btn_pl_down):
            b.pack(side="left", padx=(0, 4))
        self.btn_pl_clear.pack(side="right")
        cols = ("n", "name", "dur", "res", "fps", "state")
        self.ptree = ttk.Treeview(self.playlist_frame, columns=cols, show="headings", height=6)
        for c, h, w in zip(cols, ("순서", "파일명", "길이", "해상도", "FPS", "상태"), (44, 300, 80, 100, 60, 200)):
            self.ptree.heading(c, text=h)
            self.ptree.column(c, width=w, anchor="w" if c in ("name", "state") else "center")
        self.ptree.pack(fill="x", pady=(4, 0))
        self.lbl_playlist = ttk.Label(self.playlist_frame, textvariable=self.playlist_summary, justify="left")
        self.lbl_playlist.pack(anchor="w", pady=(4, 0))
        rr = ttk.Frame(self.single_frame); rr.pack(fill="x", pady=(4, 0))
        self.btn_make_ready = ttk.Button(rr, text="LIVE READY 파일 만들기", command=self._make_ready)
        self.btn_cancel_ready = ttk.Button(rr, text="변환 중지", command=self._convert_cancel.set)
        self.ready_bar = ttk.Progressbar(rr, maximum=100, length=260)
        self.ready_progress = tk.StringVar()
        self.lbl_ready_progress = ttk.Label(rr, textvariable=self.ready_progress)

        # ② 실행 위치
        f0 = ttk.LabelFrame(root, text="② 실행 위치", padding=7)
        f0.pack(fill="x", pady=(8, 0))
        self.rb_cloud = ttk.Radiobutton(f0, text="무료 Cloud (권장) — PC를 꺼도 방송이 계속됩니다.",
                                        variable=self.location, value=LOC_CLOUD, command=self._on_location)
        self.rb_cloud.pack(anchor="w")
        self.rb_local = ttk.Radiobutton(f0, text="내 PC — 무료 Cloud를 사용할 수 없을 때 사용합니다.",
                                        variable=self.location, value=LOC_LOCAL, command=self._on_location)
        self.rb_local.pack(anchor="w")
        cl = ttk.Frame(f0); cl.pack(fill="x", pady=(6, 0))
        ttk.Label(cl, text="무료 Cloud 상태", width=14).pack(side="left")
        self.lbl_cloud = ttk.Label(cl, textvariable=self.cloud_line, justify="left")
        self.lbl_cloud.pack(side="left")
        cb = ttk.Frame(f0); cb.pack(fill="x", pady=(4, 0))
        self.btn_wizard = ttk.Button(cb, text="처음 설정 도우미", command=self._open_wizard)
        self.btn_wizard.pack(side="left")
        self.btn_recheck = ttk.Button(cb, text="다시 확인", command=self._cloud_recheck)
        self.btn_recheck.pack(side="left", padx=(5, 0))
        self.btn_upload = ttk.Button(cb, text="Cloud에 영상 보내기", command=self._cloud_upload)
        self.btn_upload.pack(side="left", padx=(5, 0))
        self.btn_use_local = ttk.Button(cb, text="내 PC에서 LIVE", command=self._use_local)
        self.btn_use_local.pack(side="left", padx=(5, 0))
        self.btn_keyfix = ttk.Button(cb, text="키 파일 권한 고치기", command=self._fix_key)
        self.cloud_bar = ttk.Progressbar(f0, maximum=100)

        # ③ YouTube 송출
        f2 = ttk.LabelFrame(root, text="③ YouTube 송출", padding=7)
        f2.pack(fill="x", pady=(8, 0))
        sr = ttk.Frame(f2); sr.pack(fill="x")
        ttk.Label(sr, text="서버", width=11).pack(side="left")
        self.rb_youtube = ttk.Radiobutton(sr, text="YouTube RTMPS 기본", variable=self.server_mode, value="youtube", command=self._sync_widgets)
        self.rb_youtube.pack(side="left")
        self.rb_custom = ttk.Radiobutton(sr, text="직접 입력 (고급)", variable=self.server_mode, value="custom", command=self._sync_widgets)
        self.rb_custom.pack(side="left", padx=(10, 0))
        cr = ttk.Frame(f2); cr.pack(fill="x", pady=(4, 0))
        ttk.Label(cr, text="", width=11).pack(side="left")
        self.ent_custom = ttk.Entry(cr, textvariable=self.custom_url)
        self.ent_custom.pack(side="left", fill="x", expand=True)
        ttk.Label(f2, text=f"기본 주소: {YOUTUBE_RTMPS_INGEST}  (Live Control Room의 스트림 URL과 다르면 직접 입력)").pack(anchor="w", pady=(2, 0))
        kr = ttk.Frame(f2); kr.pack(fill="x", pady=(6, 0))
        ttk.Label(kr, text="Stream Key", width=11).pack(side="left")
        self.ent_key = ttk.Entry(kr, textvariable=self.key_var, show="●")
        self.ent_key.pack(side="left", fill="x", expand=True)
        self.btn_reveal = ttk.Button(kr, text="보기", width=6, command=self._toggle_reveal)
        self.btn_reveal.pack(side="left", padx=(5, 0))
        rr2 = ttk.Frame(f2); rr2.pack(fill="x", pady=(4, 0))
        self.chk_remember = ttk.Checkbutton(rr2, text="이 PC에 안전하게 기억 (Windows 암호화)", variable=self.remember, command=self._apply_remember)
        self.chk_remember.pack(side="left")
        ttk.Label(rr2, textvariable=self.key_note).pack(side="left", padx=(10, 0))

        # ④ 송출 방식
        f3 = ttk.LabelFrame(root, text="④ 송출 방식", padding=7)
        f3.pack(fill="x", pady=(8, 0))
        self.rb_auto = ttk.Radiobutton(f3, text="자동 / 저부하 권장", variable=self.send_mode, value=SEND_AUTO, command=self._sync_widgets)
        self.rb_auto.pack(anchor="w")
        self.rb_transcode = ttk.Radiobutton(f3, text="재인코딩 (고급 · 내 PC 전용 · CPU 사용 높음)", variable=self.send_mode, value=SEND_TRANSCODE, command=self._sync_widgets)
        self.rb_transcode.pack(anchor="w")
        ttk.Label(f3, textvariable=self.send_note, foreground="gray30").pack(anchor="w", pady=(2, 0))
        pr = ttk.Frame(f3); pr.pack(fill="x", pady=(4, 0))
        ttk.Label(pr, text="재인코딩 프로필", width=14).pack(side="left")
        self.cmb_preset = ttk.Combobox(pr, textvariable=self.preset_label, state="readonly", width=34, values=[p.label for p in LIVE_PRESETS])
        self.cmb_preset.pack(side="left")
        self.cmb_preset.bind("<<ComboboxSelected>>", lambda e: self._update_preset_detail())
        self.lbl_preset = ttk.Label(f3, textvariable=self.preset_detail, justify="left")
        self.lbl_preset.pack(anchor="w", pady=(5, 0))

        f4 = ttk.LabelFrame(root, text="⑤ 안전 설정", padding=7)
        f4.pack(fill="x", pady=(8, 0))
        self.chk_reconnect = ttk.Checkbutton(f4, text="끊김 시 자동 재접속 (5→10→30→60초)", variable=self.reconnect)
        self.chk_reconnect.pack(anchor="w")
        self.chk_awake = ttk.Checkbutton(f4, text="LIVE 중 Windows 절전 방지 (내 PC 모드)", variable=self.keep_awake)
        self.chk_awake.pack(anchor="w")
        ttk.Checkbutton(f4, text="LIVE 종료 전 확인", variable=self.confirm_stop).pack(anchor="w")

        f6 = ttk.LabelFrame(root, text="⑥ 세션 관리", padding=7)
        f6.pack(fill="x", pady=(8, 0))
        self.rb_continuous = ttk.Radiobutton(f6, text="계속 방송", variable=self.session_mode, value=SESSION_CONTINUOUS)
        self.rb_continuous.pack(anchor="w")
        self.rb_archive = ttk.Radiobutton(f6, text="보관 안전 모드 — 11시간 50분마다 세션 종료",
                                          variable=self.session_mode, value=SESSION_ARCHIVE_SAFE)
        self.rb_archive.pack(anchor="w")
        ttk.Label(f6, foreground="gray30", justify="left", wraplength=780, text=(
            "YouTube는 12시간을 넘는 LIVE를 보관하지 못할 수 있습니다. 보관 안전 모드는 11시간 50분에서 송출을 안전 종료합니다.\n"
            "(11시간 50분은 YouTube 공식 숫자가 아니라 이 프로그램이 정한 안전 여유값입니다. "
            "새 YouTube LIVE 자동 생성은 다음 단계 기능입니다.)")).pack(anchor="w", pady=(2, 0))
        self.next_frame = ttk.Frame(f6)
        ttk.Label(self.next_frame, textvariable=self.next_session_msg, foreground="darkorange", justify="left").pack(anchor="w")
        self.btn_next_session = ttk.Button(self.next_frame, text="▶ 다음 세션 시작", command=self._next_session)
        self.btn_next_session.pack(anchor="w", pady=(4, 0))

        ar = ttk.Frame(root); ar.pack(fill="x", pady=(10, 0))
        self.btn_check = ttk.Button(ar, text="송출 설정 검사", command=self._check)
        self.btn_check.pack(fill="x")
        br = ttk.Frame(root); br.pack(fill="x", pady=(6, 0))
        self.btn_start = ttk.Button(br, text="▶ 24H LIVE 시작", command=self._start)
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(br, text="■ LIVE 종료", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", fill="x", expand=True, padx=(5, 0))

        f5 = ttk.LabelFrame(root, text="상태", padding=7)
        f5.pack(fill="x", pady=(8, 0))
        rows = (("state", "상태"), ("where", "실행 위치"), ("session", "방송 시간"), ("media", "현재 영상"),
                ("playlist", "Playlist 회차"), ("session_time", "세션 시간"), ("session_left", "세션 종료까지"),
                ("mode", "송출 방식"), ("fps", "FPS"), ("bitrate", "Bitrate"), ("speed", "Speed"),
                ("out_time", "송출 위치"), ("reconnects", "재접속"), ("retry", "재접속까지"),
                ("exit", "마지막 종료 코드"), ("error", "마지막 오류"), ("server", "Cloud 서버"), ("disk", "서버 저장 공간"))
        for i, (k, label) in enumerate(rows):
            ttk.Label(f5, text=label, width=16).grid(row=i, column=0, sticky="w")
            lbl = ttk.Label(f5, textvariable=self.st[k], wraplength=560, justify="left")
            lbl.grid(row=i, column=1, sticky="w")
            if k == "state":
                lbl.configure(font=("Segoe UI", 11, "bold"))
                self.lbl_state = lbl
        ttk.Button(root, text="상세 보기", command=self._show_details).pack(anchor="e", pady=(6, 0))
        ttk.Label(root, text="처음 테스트는 YouTube Live Control Room에서 비공개/일부공개 스트림으로 확인하세요.",
                  foreground="gray30").pack(anchor="w", pady=(4, 0))
        self._sync_widgets()

    # ---------- inputs ----------
    def _pick_video(self):
        ffmpeg, ffprobe = self._tools()
        if not ffprobe:
            messagebox.showerror("FFmpeg", "FFmpeg/ffprobe를 찾을 수 없습니다. 메인 창에서 FFmpeg 설정을 확인하세요.", parent=self)
            return
        p = filedialog.askopenfilename(parent=self, title="LIVE 영상 선택", filetypes=[("MP4", "*.mp4"), ("영상", "*.mov *.mkv *.m4v"), ("모든 파일", "*.*")])
        if p:
            self.set_input(Path(p), ffprobe)

    def set_input(self, path: Path, ffprobe: Path):
        try:
            info = probe_live_input(path, ffprobe)
        except LiveConfigError as e:
            messagebox.showerror("영상 확인 실패", str(e), parent=self)
            return
        self.input_path.set(str(Path(path).resolve()))
        text = f"{Path(path).name}\n{describe_input(info)}"
        if not info.audio_codec:
            text += "\n⚠ 오디오가 없는 영상은 LIVE를 시작할 수 없습니다."
        self.input_info.set(text)
        self._height = info.height
        self.preset_label.set(recommend_preset(info.height).label)
        self._update_preset_detail()
        self._analyze(Path(path), ffprobe)

    def _analyze(self, path: Path, ffprobe: Path):
        """LIVE READY 분석은 ffprobe packet 헤더만 읽지만 GUI를 막지 않도록 스레드에서 실행."""
        self.ready_report = None
        self._analyze_token += 1
        token = self._analyze_token
        self.ready_text.set("LIVE READY 확인 중...")

        q = self._ui_q

        def work():
            q.put(("ready", token, analyze_live_ready(path, ffprobe)))
        threading.Thread(target=work, name="live-ready", daemon=True).start()

    def _apply_ready(self, rep):
        self.ready_report = rep
        lines = rep.summary_lines()
        if rep.ready:
            lines += ["", "송출 방식: DIRECT COPY", "예상: CPU 매우 낮음 / RAM 매우 낮음"]
        else:
            lines += ["", "→ [LIVE READY 파일 만들기]로 PC에서 한 번만 변환하면 Cloud/PC 모두 저부하로 송출할 수 있습니다."]
        self.ready_text.set("\n".join(lines))
        self.lbl_ready.configure(foreground="darkgreen" if rep.ready else "darkorange")
        self._sync_widgets()

    # ---------- LIVE READY 파일 만들기 ----------
    def _make_ready(self):
        ffmpeg, ffprobe = self._tools()
        src = self.input_path.get()
        if not (ffmpeg and ffprobe and src):
            return
        if not FFMPEG_GUARD.try_acquire(CONVERT_OWNER):
            messagebox.showwarning("FFmpeg 사용 중", busy_message(FFMPEG_GUARD.owner), parent=self)
            return
        duration = self.ready_report.duration if self.ready_report else 0.0
        self._convert_cancel.clear()
        self.ready_bar["value"] = 0
        self.ready_progress.set("LIVE READY 변환 준비 중")

        q, cancel, height = self._ui_q, self._convert_cancel, self._height

        def work():
            try:
                out = make_live_ready_file(ffmpeg=ffmpeg, ffprobe=ffprobe, src=Path(src), height=height,
                                           duration=duration, cancel=cancel,
                                           progress_cb=lambda f, t: q.put(("convert_progress", f, t)))
                q.put(("convert_done", True, out))
            except LiveReadyCancelled as e:
                q.put(("convert_done", False, str(e)))
            except Exception as e:
                q.put(("convert_done", False, str(e)[:600]))
            finally:
                FFMPEG_GUARD.release(CONVERT_OWNER)
        self._convert_thread = threading.Thread(target=work, name="live-ready-convert", daemon=True)
        self._convert_thread.start()
        self._sync_widgets()

    @property
    def converting(self) -> bool:
        return bool(self._convert_thread and self._convert_thread.is_alive())

    # ---------- 실행 위치 / Cloud ----------
    def _on_location(self):
        if self.location.get() == LOC_CLOUD and load_cloud_profile() is not None:
            self.cloud.start_polling()
        self._update_cloud_line()
        self._sync_widgets()

    def _use_local(self):
        self.location.set(LOC_LOCAL)
        self._on_location()

    def _cloud_recheck(self):
        if load_cloud_profile() is None:
            self._open_wizard()
            return
        self.cloud.client = None
        self._cloud_msg = "확인 중..."
        self.cloud.check_async()
        self._update_cloud_line()

    def _cloud_upload(self):
        if self.playlist_mode:
            v = self.playlist_validation() if self.playlist.items else None
            if v is None or not v.ok:
                messagebox.showwarning("Cloud에 영상 보내기", (v.first_error if v else "Playlist에 영상을 추가하세요."), parent=self)
                return
            self.cloud.upload_async(self.playlist.paths)
            self._sync_widgets()
            return
        src = self.input_path.get()
        if not src:
            messagebox.showwarning("Cloud에 영상 보내기", "먼저 LIVE 영상을 선택하세요.", parent=self)
            return
        if self.ready_report is not None and not self.ready_report.ready:
            messagebox.showwarning("Cloud에 영상 보내기", "Cloud는 LIVE READY 파일만 송출합니다.\n먼저 [LIVE READY 파일 만들기]를 진행하세요.", parent=self)
            return
        self.cloud.upload_async(Path(src))
        self._sync_widgets()

    def _open_wizard(self):
        from .cloud_setup_ui import CloudSetupWizard

        def done(profile):
            self.cloud.client = None
            self.location.set(LOC_CLOUD)
            self.cloud.check_async()
            self.cloud.start_polling()
            self._update_cloud_line()
        CloudSetupWizard(self, on_done=done)

    def _fix_key(self):
        profile = load_cloud_profile()
        if profile is None:
            return
        if messagebox.askyesno("키 파일 권한", "SSH Key 파일을 '나만 읽기' 권한으로 바꿀까요?\n(Windows OpenSSH 요구사항)", parent=self):
            ok = fix_key_permissions(Path(profile.key_path))
            messagebox.showinfo("키 파일 권한", "변경했습니다. [다시 확인]을 눌러 주세요." if ok else "변경하지 못했습니다.", parent=self)
            self.btn_keyfix.pack_forget()

    def _update_cloud_line(self):
        if load_cloud_profile() is None:
            self.cloud_line.set("○ 연결 안 됨 — [처음 설정 도우미]로 무료 Cloud를 준비하세요.")
            self.lbl_cloud.configure(foreground="gray30")
            return
        st = self.cloud.status
        if self.cloud.busy and self._cloud_progress:
            text, color = f"… {self._cloud_progress}", "darkorange"
        elif self._cloud_reachable is False:
            text, color = f"✗ {CLOUD_UNAVAILABLE}\n{self._cloud_msg}", "firebrick"
        elif st is not None and st.live:
            text, color = "● CLOUD LIVE 방송 중", "red"
        elif st is not None and st.reachable and st.installed:
            text, color = f"✓ 준비됨 (대기) · {FREE_UNSURE}", "darkgreen"
        elif st is not None and st.reachable:
            text, color = "⚠ 연결됨 · LIVE Worker 미설치 — [처음 설정 도우미]를 진행하세요.", "darkorange"
        else:
            text, color = self._cloud_msg or "확인 중...", "gray30"
        self.cloud_line.set(text)
        self.lbl_cloud.configure(foreground=color)

    # ---------- 송출 방식 ----------
    def _preset(self):
        for p in LIVE_PRESETS:
            if p.label == self.preset_label.get():
                return p
        return preset_by_key(DEFAULT_PRESET_KEY)

    def _update_preset_detail(self):
        p = self._preset()
        res = f"입력 해상도 그대로 ({self._height}p)" if self._height else "입력 해상도 그대로"
        warn = ""
        rec = recommend_preset(self._height) if self._height else None
        if rec and rec.input_height != p.input_height:
            warn = f"\n⚠ 이 프로필은 {p.input_height}p 입력용입니다. 추천: {rec.label}"
        self.preset_detail.set(
            f"Video {p.video_bitrate_kbps} kbps (CBR) · Audio {p.audio_bitrate_kbps} kbps AAC 44.1kHz · "
            f"{p.fps}fps · Keyframe {p.keyframe_seconds}s · CPU/libx264 · {res}{warn}")

    def effective_mode(self) -> str:
        if self.location.get() == LOC_CLOUD or self.send_mode.get() == SEND_AUTO or self.playlist_mode:
            return MODE_COPY
        return MODE_TRANSCODE

    def _ingest(self) -> str:
        return YOUTUBE_RTMPS_INGEST if self.server_mode.get() == "youtube" else self.custom_url.get().strip()

    # ---------- stream key ----------
    def _load_saved_key(self):
        persistent = bool(getattr(self.store, "persistent", False))
        if not persistent:
            self.chk_remember.configure(state="disabled")
            self.key_note.set("이 환경에서는 저장 불가 (메모리에서만 사용)")
            return
        key = self.store.get()
        if key:
            self.key_var.set(key)  # entry는 show="●" 상태 유지
            self.remember.set(True)
            self.key_note.set(f"저장된 키 사용 중 ({mask_secret(key)})")

    def _apply_remember(self):
        if not getattr(self.store, "persistent", False):
            return
        key = self.key_var.get().strip()
        try:
            if self.remember.get():
                if key:
                    self.store.set(key)
                    self.key_note.set("이 PC에 암호화 저장됨")
                else:
                    self.key_note.set("Stream Key 입력 후 LIVE 시작 시 저장됩니다")
            else:
                self.store.clear()
                self.key_note.set("저장된 키 삭제됨 · 메모리에서만 사용")
        except Exception:
            self.key_note.set("⚠ 키 저장/삭제 실패 (메모리에서만 사용)")

    def _toggle_reveal(self):
        if self.ent_key.cget("show"):
            self.ent_key.configure(show="")
            self.btn_reveal.configure(text="숨기기")
            self._reveal_job = self.after(KEY_REVEAL_MS, self._hide_key)
        else:
            self._hide_key()

    def _hide_key(self):
        if self._reveal_job:
            try:
                self.after_cancel(self._reveal_job)
            except tk.TclError:
                pass
            self._reveal_job = None
        self.ent_key.configure(show="●")
        self.btn_reveal.configure(text="보기")

    # ---------- actions ----------
    @property
    def playlist_mode(self) -> bool:
        return self.source_mode.get() == "playlist"

    def _manifest_path(self) -> Path:
        return settings_dir() / "live_playlist.ffconcat"

    def _preflight(self):
        ffmpeg, ffprobe = self._tools()
        input_path, ready, pl_reports = self.input_path.get() or None, self.ready_report, None
        if self.playlist_mode:
            items = self.playlist.items
            input_path = str(items[0].path) if items else None
            ready = items[0].report if len(items) == 1 else None
            pl_reports = [i.report for i in items] if len(items) > 1 else None
        return run_preflight(
            ffmpeg=ffmpeg, ffprobe=ffprobe, input_path=input_path,
            ingest_url=self._ingest(), stream_key=self.key_var.get(), preset=self._preset(),
            guard=self.controller.guard, supervisor_state=self.controller.state,
            mode=self.effective_mode(), ready_report=ready, location=self.location.get(),
            playlist_reports=pl_reports, manifest_path=self._manifest_path() if pl_reports else None,
        )

    # ---------- Playlist ----------
    def _on_source_mode(self):
        if self.playlist_mode:
            self.single_frame.pack_forget()
            self.playlist_frame.pack(fill="x")
        else:
            self.playlist_frame.pack_forget()
            self.single_frame.pack(fill="x")
        self._refresh_playlist()
        self._sync_widgets()

    def _pl_add(self):
        _, ffprobe = self._tools()
        if not ffprobe:
            messagebox.showerror("FFmpeg", "FFmpeg/ffprobe를 찾을 수 없습니다. 메인 창에서 FFmpeg 설정을 확인하세요.", parent=self)
            return
        picked = filedialog.askopenfilenames(parent=self, title="Playlist에 넣을 LIVE READY MP4 선택",
                                             filetypes=[("MP4", "*.mp4"), ("모든 파일", "*.*")])
        added, problems = [], []
        for raw in picked or ():
            try:
                added.append(self.playlist.add(Path(raw).resolve()).path)
            except PlaylistError as e:
                problems.append(str(e))
        if problems:
            messagebox.showwarning("Playlist", "\n".join(problems), parent=self)
        if added:
            q = self._ui_q  # 스레드에는 Tk 객체를 넘기지 않는다

            def work():
                for path in added:  # 하나씩 (ffprobe 메타데이터만, 동시 실행 1개)
                    q.put(("pl_ready", path, analyze_live_ready(path, ffprobe)))
            threading.Thread(target=work, name="playlist-analyze", daemon=True).start()
        self._refresh_playlist()

    def _pl_selected(self) -> int | None:
        sel = self.ptree.selection()
        return self.ptree.index(sel[0]) if sel else None

    def _pl_remove(self):
        i = self._pl_selected()
        if i is not None:
            self.playlist.remove(i)
            self._refresh_playlist()

    def _pl_move(self, d: int):
        i = self._pl_selected()
        if i is not None:
            j = self.playlist.move(i, d)
            self._refresh_playlist(select=j)

    def _pl_clear(self):
        self.playlist.clear()
        self._refresh_playlist()

    def playlist_validation(self):
        return validate_playlist([i.report for i in self.playlist.items])

    def _refresh_playlist(self, select: int | None = None):
        if not hasattr(self, "ptree"):
            return
        for x in self.ptree.get_children():
            self.ptree.delete(x)
        v = self.playlist_validation() if self.playlist.items else None
        for n, item in enumerate(self.playlist.items, 1):
            rep_ = item.report
            status = (v.item_status[n - 1] if v and n - 1 < len(v.item_status) and v.item_status[n - 1] else "확인 중")
            self.ptree.insert("", "end", values=(
                n, item.name,
                format_duration(rep_.duration) if rep_ else "-",
                f"{rep_.width}×{rep_.height}" if rep_ else "-",
                f"{rep_.fps:.2f}" if rep_ else "-",
                status))
        if select is not None and self.ptree.get_children():
            self.ptree.selection_set(self.ptree.get_children()[select])
        n = len(self.playlist)
        if n == 0:
            text, color = "[영상 추가]로 LIVE READY MP4를 2개 이상 넣으세요.", "gray30"
        else:
            lines = [f"총 {n}개", f"총 재생시간 {format_duration(self.playlist.total_duration)}"]
            if not self.playlist.analyzed:
                lines.append("영상 확인 중...")
                color = "gray30"
            elif v.ok:
                lines += ["✓ 모두 LIVE READY", "✓ DIRECT COPY Playlist 가능" if n > 1 else "✓ 1개는 단일 영상과 같은 방식으로 송출"]
                color = "darkgreen"
            else:
                lines += ["⚠ " + m for m in v.messages]
                color = "darkorange"
            text = "\n".join(lines)
        self.playlist_summary.set(text)
        self.lbl_playlist.configure(foreground=color)

    # ---------- 세션 ----------
    def _session_complete(self) -> bool:
        if self.controller.state is LiveState.SESSION_LIMIT_REACHED:
            return True
        cs = self.cloud.status
        return bool(self.location.get() == LOC_CLOUD and cs is not None and cs.session_complete)

    def _next_session(self):
        """Phase 3A: 수동 — 사용자가 YouTube에서 다음 LIVE를 준비한 뒤 누른다 (Phase 3B에서 API provider로 교체)."""
        self.session_provider.complete_current_broadcast()
        self._start()

    def _check(self):
        r = self._preflight()
        extra = ""
        if self.location.get() == LOC_CLOUD and load_cloud_profile() is None:
            extra = "\n\n무료 Cloud가 아직 설정되지 않았습니다. [처음 설정 도우미]를 진행하거나 [내 PC에서 LIVE]를 선택하세요."
        (messagebox.showinfo if r.ok and not extra else messagebox.showwarning)("송출 설정 검사", r.report() + extra, parent=self)

    @property
    def busy_any(self) -> bool:
        return self.controller.active or self.cloud.busy or self.converting

    def _start(self):
        if self.busy_any or self.cloud.cloud_live_active:
            return
        analyzing = (not self.playlist.analyzed) if self.playlist_mode else (
            self.ready_report is None and self.effective_mode() == MODE_COPY and self.input_path.get())
        if analyzing:
            messagebox.showinfo("LIVE READY", "영상 분석 중입니다. 잠시 후 다시 눌러 주세요.", parent=self)
            return
        r = self._preflight()
        if not r.ok:
            messagebox.showerror("LIVE 시작 불가", "\n".join(r.errors()) or "송출 설정을 확인하세요.", parent=self)
            return
        self._hide_key()
        if self.remember.get():
            self._apply_remember()
        if self.location.get() == LOC_CLOUD:
            if load_cloud_profile() is None:
                messagebox.showwarning("무료 Cloud", "먼저 [처음 설정 도우미]로 무료 Cloud를 준비하세요.\n"
                                       "Cloud를 사용할 수 없으면 [내 PC에서 LIVE]를 선택하세요.", parent=self)
                return
            local = self.playlist.paths if self.playlist_mode and len(self.playlist) > 1 else r.config.input_path
            self.cloud.start_async(local=local, ingest_url=r.config.ingest_url, stream_key=r.config.stream_key,
                                   session_mode=self.session_mode.get(), session_id=pysecrets.token_hex(8))
            self._cloud_progress = "Cloud LIVE 준비 중"
            self._refresh()
            return
        ffmpeg, _ = self._tools()
        self._failed_shown = False
        playlist = None
        if r.config.input_format == "concat":
            reports = [i.report for i in self.playlist.items]
            durations = entry_durations(reports)
            write_ffconcat(self._manifest_path(), list(zip([i.path for i in self.playlist.items], durations)))
            playlist = [(i.name, d) for i, d in zip(self.playlist.items, durations)]
        try:
            state = self.controller.start(ffmpeg=ffmpeg, config=r.config,
                                          reconnect=bool(self.reconnect.get()), keep_awake=bool(self.keep_awake.get()),
                                          session_limit=session_limit_seconds(self.session_mode.get()), playlist=playlist)
        except LiveBusyError as e:
            messagebox.showwarning("LIVE 시작 불가", str(e), parent=self)
            return
        if state is LiveState.FAILED:
            self._show_failed()
        self._refresh()

    def _stop(self):
        if self.cloud.cloud_live_active and not self.controller.active:
            if self.cloud.busy:
                return
            if self.confirm_stop.get() and not messagebox.askyesno("Cloud LIVE 종료", "Cloud LIVE 송출을 종료할까요?", parent=self):
                return
            self.cloud.stop_async()
            self._cloud_progress = "Cloud LIVE 종료 중"
            self._refresh()
            return
        if not self.controller.active or self.controller.stopping:
            return
        if self.confirm_stop.get() and not messagebox.askyesno("LIVE 종료", "LIVE 송출을 종료할까요?", parent=self):
            return
        self.controller.stop_async()
        self._refresh()

    def _show_failed(self):
        if self._failed_shown or getattr(self, "_destroyed", False):
            return
        self._failed_shown = True
        snap = self.controller.snapshot()
        msg = redact(snap.last_error or "FFmpeg 송출이 중단되었습니다.", [self.key_var.get().strip()])
        messagebox.showerror("LIVE 오류", msg, parent=self)

    def _show_details(self):
        """고급 정보: 실제 명령/서버 로그 (Stream Key는 가려짐)."""
        d = tk.Toplevel(self)
        d.title("상세 보기 (고급)")
        d.geometry("760x460")
        txt = tk.Text(d, wrap="none", font=("Consolas", 9))
        txt.pack(fill="both", expand=True)
        key = self.key_var.get().strip()
        lines = ["[내 PC LIVE 최근 오류]"]
        sup = self.controller.supervisor
        proc = getattr(sup, "_proc", None) if sup else None
        lines += (proc.recent_errors() if proc is not None and hasattr(proc, "recent_errors") else []) or ["(없음)"]
        lines += ["", "[Cloud 작업 기록 (최근 200줄)]"]
        client = self.cloud.client
        lines += list(client.detail) if client is not None else ["(없음)"]
        txt.insert("1.0", redact("\n".join(lines), [key]))
        txt.configure(state="disabled")

        self._details_txt = txt

        def load_logs():
            client, q = self.cloud.client, self._ui_q
            if client is None:
                return

            def work():
                try:
                    logs = client.logs()
                except Exception as e:
                    logs = [str(e)]
                q.put(("logs", logs))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(d, text="Cloud 로그 불러오기 (최근 50줄)", command=load_logs).pack(anchor="e")

    # ---------- periodic UI update (Tk main thread only) ----------
    def _tick(self):
        self._tick_job = None
        try:
            for ev in self.controller.drain_events():
                if ev[0] == "state" and ev[1] is LiveState.FAILED and not self._closing:
                    self.after_idle(self._show_failed)
            self._drain_ui()
            self._drain_cloud()
            self._refresh()
        finally:
            if self.winfo_exists():
                self._tick_job = self.after(TICK_MS, self._tick)

    def _drain_ui(self):
        while True:
            try:
                ev = self._ui_q.get_nowait()
            except queue.Empty:
                return
            kind = ev[0]
            if kind == "ready" and ev[1] == self._analyze_token:
                self._apply_ready(ev[2])
            elif kind == "pl_ready":
                self.playlist.set_report(ev[1], ev[2])
                self._refresh_playlist()
            elif kind == "convert_progress":
                self.ready_bar["value"] = ev[1] * 100
                self.ready_progress.set(ev[2])
            elif kind == "convert_done":
                ok, payload = ev[1], ev[2]
                self.ready_progress.set("")
                if ok:
                    _, ffprobe = self._tools()
                    self.set_input(Path(payload), ffprobe)
                    if not self._closing:
                        messagebox.showinfo("LIVE READY", f"LIVE READY 파일을 만들었습니다:\n{Path(payload).name}\n\n이제 이 파일을 DIRECT COPY로 송출합니다.", parent=self)
                elif not self._closing:
                    messagebox.showwarning("LIVE READY", payload, parent=self)
                self._sync_widgets()
            elif kind == "logs":
                _, logs = ev
                txt = getattr(self, "_details_txt", None)
                if txt is None:
                    continue
                try:
                    txt.configure(state="normal")
                    txt.insert("end", "\n\n[Cloud 서버 로그 (최근 50줄)]\n" + redact("\n".join(logs), [self.key_var.get().strip()]))
                    txt.configure(state="disabled")
                except tk.TclError:
                    pass

    def _drain_cloud(self):
        for ev in self.cloud.drain_events():
            kind = ev[0]
            if kind == "status":
                st = ev[1]
                self._cloud_reachable = st.reachable
                self._cloud_msg = st.message
                if st.message and "권한" in st.message and "Key" in st.message:
                    self.btn_keyfix.pack(side="left", padx=(5, 0))
            elif kind == "progress":
                self._cloud_progress = ev[3]
                self.cloud_bar.pack(fill="x", pady=(4, 0))
                self.cloud_bar["value"] = ev[2] * 100
            elif kind == "op":
                _, name, ok, payload = ev
                self._cloud_progress = ""
                self.cloud_bar.pack_forget()
                if ok:
                    if isinstance(payload, CloudStatus):
                        self._cloud_reachable = payload.reachable
                        self._cloud_msg = payload.message
                    else:
                        self._cloud_reachable = True
                    if name == "upload" and not self._closing:
                        ups = payload if isinstance(payload, list) else [payload]
                        skipped = sum(1 for u in ups if u.skipped)
                        if len(ups) == 1:
                            msg = "이미 Cloud에 같은 영상이 있습니다 (업로드 생략)." if skipped else "Cloud에 영상을 보냈습니다 (SHA256 검증 완료)."
                        else:
                            msg = f"{len(ups)}개 완료 — 새로 보냄 {len(ups) - skipped}개 / 이미 있음 {skipped}개 (SHA256 검증)"
                        messagebox.showinfo("Cloud에 영상 보내기", msg, parent=self)
                    elif name == "start" and not self._closing:
                        messagebox.showinfo("Cloud LIVE", "● CLOUD LIVE 시작\n\n이제 PC 프로그램을 종료해도 Cloud에서 방송이 계속됩니다.", parent=self)
                else:
                    msg = redact(str(payload), [self.key_var.get().strip()])
                    if name == "check":
                        self._cloud_reachable = False
                        self._cloud_msg = msg
                    if "권한" in msg and "Key" in msg:
                        self.btn_keyfix.pack(side="left", padx=(5, 0))
                    if name != "check" and not self._closing:
                        messagebox.showerror("무료 Cloud", msg + "\n\n무료 Cloud를 사용할 수 없으면 [내 PC에서 LIVE]를 선택하세요.", parent=self)
        self._update_cloud_line()

    def _refresh(self):
        st = self.st
        cloud_mode = self.location.get() == LOC_CLOUD and not self.controller.active
        if cloud_mode:
            cs = self.cloud.status
            live = self.cloud.cloud_live_active
            label = ("Cloud 작업 중" if self.cloud.busy else "● CLOUD LIVE" if live else
                     {"RECONNECT_WAIT": "재연결 대기", "FAILED": "오류",
                      "SESSION_LIMIT_REACHED": "보관 안전 종료 · 다음 세션 대기"}.get(cs.state if cs else "", "대기"))
            st["state"].set(label)
            self.lbl_state.configure(foreground="red" if live else ("darkorange" if self.cloud.busy else ""))
            st["where"].set("무료 Cloud")
            st["session"].set(format_duration(cs.runtime_seconds) if cs and cs.runtime_seconds else "-")
            if cs and cs.playlist_count > 1 and cs.current_playlist_index is not None:
                st["media"].set(f"{cs.current_playlist_index + 1}/{cs.playlist_count} {cs.media}")
            else:
                st["media"].set(cs.media if cs and cs.media else "-")
            st["playlist"].set(f"{cs.playlist_round}회" if cs and cs.playlist_round else "-")
            self._set_session_rows(cs.runtime_seconds if cs else 0.0, cs.session_limit if cs else None,
                                   cs.session_remaining if cs and live else None)
            st["mode"].set(cs.mode if cs and cs.mode else "DIRECT COPY")
            st["fps"].set(f"{cs.fps:.1f}" if cs and cs.fps is not None else "-")
            st["bitrate"].set(format_bitrate(cs.bitrate) if cs else "-")
            st["speed"].set(f"{cs.speed:.2f}x" if cs and cs.speed is not None else "-")
            st["out_time"].set("-")
            st["reconnects"].set(f"{cs.reconnects}회" if cs else "-")
            st["retry"].set(f"{cs.retry_in:.0f}초" if cs and cs.retry_in is not None else "-")
            st["exit"].set("-")
            st["error"].set(redact(cs.last_error, [self.key_var.get().strip()]) if cs and cs.last_error else "없음")
            st["server"].set(("연결됨" if cs.reachable else "연결 안 됨") + (" · Worker 설치됨" if cs.installed else "") if cs else "-")
            st["disk"].set(f"{cs.disk_free_bytes / 1024**3:.1f} GB 남음" if cs and cs.disk_free_bytes else "-")
        else:
            s = self.controller.snapshot()
            label = "종료 중" if self.controller.stopping else s.label
            st["state"].set(label)
            self.lbl_state.configure(foreground="red" if s.state is LiveState.RUNNING else ("darkorange" if s.state in LOCKED_STATES else ("firebrick" if s.state is LiveState.FAILED else "")))
            st["where"].set("내 PC")
            st["session"].set(format_duration(s.session_seconds) if s.session_seconds else "-")
            if s.playlist_count > 1 and s.playlist_index is not None:
                st["media"].set(f"{s.playlist_index + 1}/{s.playlist_count} {s.current_media}")
            elif self.playlist_mode and self.playlist.items:
                st["media"].set(self.playlist.items[0].name if len(self.playlist) == 1 else f"Playlist {len(self.playlist)}개")
            else:
                st["media"].set(Path(self.input_path.get()).name if self.input_path.get() else "-")
            st["playlist"].set(f"{s.playlist_round}회" if s.playlist_round else "-")
            self._set_session_rows(s.session_seconds, s.session_limit, s.session_remaining)
            st["mode"].set("DIRECT COPY" if self.effective_mode() == MODE_COPY else "재인코딩 (libx264)")
            st["fps"].set(f"{s.fps:.1f}" if s.fps is not None else "-")
            st["bitrate"].set(format_bitrate(s.bitrate))
            st["speed"].set(f"{s.speed:.2f}x" if s.speed is not None else "-")
            st["out_time"].set(format_duration(s.out_time_seconds) if s.out_time_seconds is not None else "-")
            st["reconnects"].set(f"{s.reconnects}회")
            st["retry"].set(f"{s.retry_in:.0f}초" if s.retry_in is not None else "-")
            st["exit"].set("-" if s.last_exit_code is None else str(s.last_exit_code))
            st["error"].set(redact(s.last_error, [self.key_var.get().strip()]) or "없음")
            st["server"].set("-")
            st["disk"].set("-")
        self._sync_widgets()

    def _set_session_rows(self, elapsed: float, limit: float | None, remaining: float | None):
        st = self.st
        if limit:
            st["session_time"].set(f"{format_duration(elapsed)} / {format_duration(limit)}" if elapsed else
                                   f"- / {format_duration(limit)}")
            notice = archive_notice(remaining)
            st["session_left"].set(f"{format_duration(remaining)}" + (f"  ({notice})" if notice else "")
                                   if remaining is not None else "-")
        else:
            st["session_time"].set(format_duration(elapsed) + " (계속 방송)" if elapsed else "-")
            st["session_left"].set("-")
        if self._session_complete():
            self.next_session_msg.set("보관 안전 종료되었습니다.\n" + self.session_provider.prepare_next_broadcast())
            self.next_frame.pack(anchor="w", fill="x", pady=(6, 0))
        else:
            self.next_frame.pack_forget()

    def _sync_widgets(self):
        local_live = self.controller.active
        cloud_live = self.cloud.cloud_live_active
        locked = local_live or cloud_live or self.cloud.busy or self.converting
        normal = "disabled" if locked else "normal"
        for w in (self.btn_video, self.btn_reveal, self.rb_youtube, self.rb_custom, self.btn_check,
                  self.btn_start, self.chk_reconnect, self.chk_awake, self.rb_cloud, self.rb_local,
                  self.rb_auto, self.btn_wizard, self.btn_upload, self.rb_single, self.rb_playlist,
                  self.btn_pl_add, self.btn_pl_remove, self.btn_pl_up, self.btn_pl_down, self.btn_pl_clear,
                  self.rb_continuous, self.rb_archive, self.btn_next_session):
            w.configure(state=normal)
        is_cloud = self.location.get() == LOC_CLOUD
        self.rb_transcode.configure(state="disabled" if locked or is_cloud else "normal")
        if is_cloud and self.send_mode.get() == SEND_TRANSCODE:
            self.send_mode.set(SEND_AUTO)
        self.ent_key.configure(state=normal)
        transcode = self.effective_mode() == MODE_TRANSCODE
        self.cmb_preset.configure(state="readonly" if transcode and not locked else "disabled")
        self.ent_custom.configure(state="normal" if not locked and self.server_mode.get() == "custom" else "disabled")
        self.chk_remember.configure(state="normal" if not locked and getattr(self.store, "persistent", False) else "disabled")
        self.btn_recheck.configure(state="disabled" if self.cloud.busy else "normal")
        self.btn_use_local.configure(state="disabled" if locked else "normal")
        stoppable = (local_live and not self.controller.stopping) or (cloud_live and not self.cloud.busy)
        self.btn_stop.configure(state="normal" if stoppable else "disabled")
        for w in (self.btn_upload, self.btn_recheck):
            if not is_cloud:
                w.configure(state="disabled")
        # LIVE READY 버튼: 분석 결과가 '아님'일 때만
        show_make = self.ready_report is not None and not self.ready_report.ready and not self.converting
        if show_make:
            self.btn_make_ready.pack(side="left")
            self.btn_make_ready.configure(state="disabled" if locked else "normal")
        else:
            self.btn_make_ready.pack_forget()
        if self.converting:
            self.btn_cancel_ready.pack(side="left")
            self.ready_bar.pack(side="left", padx=(6, 0))
            self.lbl_ready_progress.pack(side="left", padx=(6, 0))
        else:
            for w in (self.btn_cancel_ready, self.ready_bar, self.lbl_ready_progress):
                w.pack_forget()
        if is_cloud:
            note = "무료 Cloud는 LIVE READY 파일을 재인코딩 없이 그대로 송출합니다 (DIRECT COPY)."
        elif transcode:
            note = "고급: PC에서 실시간 재인코딩합니다. CPU 사용이 높습니다."
        else:
            note = "LIVE READY 파일은 재인코딩 없이 그대로 송출합니다 (CPU/RAM 매우 낮음). 아니면 [LIVE READY 파일 만들기]를 권장합니다."
        self.send_note.set(note)

    # ---------- close lifecycle ----------
    def request_close(self):
        """LIVE 창 X 버튼."""
        self.confirm_close(self.destroy, for_app=False)

    def confirm_close(self, on_done: Callable[[], None], *, for_app: bool) -> None:
        """Local LIVE → 확인 후 FFmpeg 정상 종료. Cloud LIVE → [PC만 종료]/[LIVE도 종료]/[취소] (기본: PC만 종료).

        Cloud와 Local lifecycle을 섞지 않는다: PC만 종료는 Cloud에 아무 명령도 보내지 않는다.
        """
        if self.controller.active:
            msg = ("현재 LIVE 송출 중입니다.\nLIVE를 종료하고 프로그램을 닫을까요?" if for_app
                   else "LIVE 송출 중입니다.\n송출을 종료하고 창을 닫을까요?")
            if not messagebox.askyesno("LIVE 송출 중", msg, parent=self):
                return
            self.shutdown(on_done)
            return
        if self.converting:
            if not messagebox.askyesno("LIVE READY 변환 중", "LIVE READY 파일 변환 중입니다.\n변환을 중지하고 닫을까요?", parent=self):
                return
            self._convert_cancel.set()
            self._wait_then(lambda: not self.converting, on_done)
            return
        if self.cloud.cloud_live_active:
            choice = ask_cloud_close(self, CLOUD_CLOSE_MESSAGE)
            if choice == "cancel":
                return
            if choice == "stop":
                self._closing = True
                self.cloud.stop_async()
                self._wait_then(lambda: not self.cloud.busy, on_done)
                return
            on_done()  # PC만 종료: Cloud LIVE는 그대로
            return
        on_done()

    def _wait_then(self, cond: Callable[[], bool], on_done: Callable[[], None]):
        def check():
            if not cond():
                self.after(100, check)
                return
            on_done()
        check()

    def shutdown(self, on_done: Callable[[], None] | None = None):
        """Local LIVE: GUI를 멈추지 않고 graceful stop → watchdog 종료 → guard/keep-awake 해제 후 on_done."""
        self._closing = True
        self.controller.stop_async()
        self._refresh()

        def wait_done():
            if self.controller.active or self.controller.stopping:
                self.after(100, wait_done)
                return
            self.controller.stop_blocking()  # 남은 watchdog/guard/keep-awake 정리
            if on_done:
                on_done()
        wait_done()

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        if self.controller.active:
            # 예외 경로 안전장치: 창이 사라지기 전 반드시 로컬 FFmpeg 종료. (Cloud LIVE는 건드리지 않음)
            self.controller.stop_blocking()
        if self.converting:
            self._convert_cancel.set()
            self._convert_thread.join(10)
        self.cloud.stop_polling()
        self.controller.keep_awake.disable()
        for job in (self._tick_job, self._reveal_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self.key_var.set("")
        super().destroy()
        release_tk_variables(self)  # 이후 어느 스레드에서 GC가 돌아도 Tcl 호출이 없도록 (main thread에서 정리)
