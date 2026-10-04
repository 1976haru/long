"""24H Playlist LIVE Studio (Toplevel 창).

로직은 live_controller.py에 있고 이 파일은 Tk 위젯만 다룬다.
backend 스레드는 위젯을 직접 만지지 않는다: controller.events 큐 → after() 폴링.
Stream Key는 어떤 messagebox/title/로그에도 표시하지 않는다.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .core import format_duration
from .live_controller import LOCKED_STATES, LiveController, describe_input, format_bitrate, probe_live_input, run_preflight
from .live_profile import (
    DEFAULT_PRESET_KEY, LIVE_PRESETS, YOUTUBE_RTMPS_INGEST, LiveConfigError, mask_secret, preset_by_key,
    recommend_preset, redact,
)
from .live_secrets import default_key_store
from .live_supervisor import LiveBusyError, LiveState

KEY_REVEAL_MS = 8000
TICK_MS = 500


class LiveWindow(tk.Toplevel):
    def __init__(self, master, *, tools: Callable[[], tuple], controller: LiveController | None = None, key_store=None):
        super().__init__(master)
        self.title("24H Playlist LIVE Studio")
        self.geometry("850x720")
        self.minsize(640, 480)
        self._tools = tools
        self.controller = controller or LiveController()
        self.store = key_store or default_key_store()
        self._closing = False
        self._reveal_job = None
        self._tick_job = None
        self._failed_shown = False

        self.input_path = tk.StringVar()
        self.input_info = tk.StringVar(value="LIVE로 송출할 완성 MP4를 선택하세요.")
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
            "state", "session", "fps", "bitrate", "speed", "out_time", "reconnects", "retry", "exit", "error")}
        self._height = 0

        self._ui()
        self._load_saved_key()
        self._update_preset_detail()
        self.protocol("WM_DELETE_WINDOW", self.request_close)
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
        return inner

    def _ui(self):
        root = self._scroll_area()
        head = ttk.Frame(root); head.pack(anchor="w")
        ttk.Label(head, text="●", foreground="red", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(head, text=" 24H Playlist LIVE Studio", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(root, text="완성 MP4 1개를 YouTube LIVE로 무한 반복 송출합니다.").pack(anchor="w", pady=(0, 6))

        f1 = ttk.LabelFrame(root, text="① LIVE 영상", padding=7)
        f1.pack(fill="x")
        r = ttk.Frame(f1); r.pack(fill="x")
        ttk.Entry(r, textvariable=self.input_path, state="readonly").pack(side="left", fill="x", expand=True)
        self.btn_video = ttk.Button(r, text="영상 선택", command=self._pick_video)
        self.btn_video.pack(side="left", padx=(5, 0))
        ttk.Label(f1, textvariable=self.input_info).pack(anchor="w", pady=(5, 0))

        f2 = ttk.LabelFrame(root, text="② YouTube 송출", padding=7)
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
        rr = ttk.Frame(f2); rr.pack(fill="x", pady=(4, 0))
        self.chk_remember = ttk.Checkbutton(rr, text="이 PC에 안전하게 기억 (Windows 암호화)", variable=self.remember, command=self._apply_remember)
        self.chk_remember.pack(side="left")
        ttk.Label(rr, textvariable=self.key_note).pack(side="left", padx=(10, 0))

        f3 = ttk.LabelFrame(root, text="③ 송출 품질", padding=7)
        f3.pack(fill="x", pady=(8, 0))
        pr = ttk.Frame(f3); pr.pack(fill="x")
        ttk.Label(pr, text="프로필", width=11).pack(side="left")
        self.cmb_preset = ttk.Combobox(pr, textvariable=self.preset_label, state="readonly", width=34, values=[p.label for p in LIVE_PRESETS])
        self.cmb_preset.pack(side="left")
        self.cmb_preset.bind("<<ComboboxSelected>>", lambda e: self._update_preset_detail())
        ttk.Label(f3, textvariable=self.preset_detail, justify="left").pack(anchor="w", pady=(5, 0))

        f4 = ttk.LabelFrame(root, text="④ 안전 설정", padding=7)
        f4.pack(fill="x", pady=(8, 0))
        self.chk_reconnect = ttk.Checkbutton(f4, text="끊김 시 자동 재접속 (5→10→30→60초)", variable=self.reconnect)
        self.chk_reconnect.pack(anchor="w")
        self.chk_awake = ttk.Checkbutton(f4, text="LIVE 중 Windows 절전 방지", variable=self.keep_awake)
        self.chk_awake.pack(anchor="w")
        ttk.Checkbutton(f4, text="LIVE 종료 전 확인", variable=self.confirm_stop).pack(anchor="w")

        ar = ttk.Frame(root); ar.pack(fill="x", pady=(10, 0))
        self.btn_check = ttk.Button(ar, text="송출 설정 검사", command=self._check)
        self.btn_check.pack(fill="x")
        br = ttk.Frame(root); br.pack(fill="x", pady=(6, 0))
        self.btn_start = ttk.Button(br, text="▶ LIVE 시작", command=self._start)
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(br, text="■ LIVE 종료", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", fill="x", expand=True, padx=(5, 0))

        f5 = ttk.LabelFrame(root, text="상태", padding=7)
        f5.pack(fill="x", pady=(8, 0))
        rows = (("state", "상태"), ("session", "방송 시간"), ("fps", "FPS"), ("bitrate", "Bitrate"),
                ("speed", "Speed"), ("out_time", "송출 위치"), ("reconnects", "재접속"),
                ("retry", "재접속까지"), ("exit", "마지막 종료 코드"), ("error", "마지막 오류"))
        for i, (k, label) in enumerate(rows):
            ttk.Label(f5, text=label, width=16).grid(row=i, column=0, sticky="w")
            lbl = ttk.Label(f5, textvariable=self.st[k], wraplength=560, justify="left")
            lbl.grid(row=i, column=1, sticky="w")
            if k == "state":
                lbl.configure(font=("Segoe UI", 11, "bold"))
                self.lbl_state = lbl
        ttk.Label(root, text="처음 테스트는 YouTube Live Control Room에서 비공개/일부공개 스트림으로 확인하세요.",
                  foreground="gray30").pack(anchor="w", pady=(8, 0))
        self._sync_widgets()

    # ---------- inputs ----------
    def _pick_video(self):
        ffmpeg, ffprobe = self._tools()
        if not ffprobe:
            messagebox.showerror("FFmpeg", "FFmpeg/ffprobe를 찾을 수 없습니다. 메인 창에서 FFmpeg 설정을 확인하세요.", parent=self)
            return
        p = filedialog.askopenfilename(parent=self, title="LIVE 영상 선택", filetypes=[("MP4", "*.mp4"), ("영상", "*.mov *.mkv *.m4v"), ("모든 파일", "*.*")])
        if not p:
            return
        try:
            info = probe_live_input(Path(p), ffprobe)
        except LiveConfigError as e:
            messagebox.showerror("영상 확인 실패", str(e), parent=self)
            return
        self.input_path.set(str(Path(p).resolve()))
        text = f"{Path(p).name}\n{describe_input(info)}"
        if not info.audio_codec:
            text += "\n⚠ 오디오가 없는 영상은 LIVE를 시작할 수 없습니다."
        self.input_info.set(text)
        self._height = info.height
        self.preset_label.set(recommend_preset(info.height).label)
        self._update_preset_detail()

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
            f"Video     {p.video_bitrate_kbps} kbps (CBR)\nAudio     {p.audio_bitrate_kbps} kbps AAC 44.1kHz stereo\n"
            f"FPS       {p.fps}\nKeyframe  {p.keyframe_seconds} sec\nEncoder   CPU / libx264\n해상도    {res}{warn}")

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
    def _preflight(self):
        ffmpeg, ffprobe = self._tools()
        return run_preflight(
            ffmpeg=ffmpeg, ffprobe=ffprobe, input_path=self.input_path.get() or None,
            ingest_url=self._ingest(), stream_key=self.key_var.get(), preset=self._preset(),
            guard=self.controller.guard, supervisor_state=self.controller.state,
        )

    def _check(self):
        r = self._preflight()
        (messagebox.showinfo if r.ok else messagebox.showwarning)("송출 설정 검사", r.report(), parent=self)

    def _start(self):
        if self.controller.active:
            return
        r = self._preflight()
        if not r.ok:
            messagebox.showerror("LIVE 시작 불가", "\n".join(r.errors()) or "송출 설정을 확인하세요.", parent=self)
            return
        self._hide_key()
        if self.remember.get():
            self._apply_remember()
        ffmpeg, _ = self._tools()
        self._failed_shown = False
        try:
            state = self.controller.start(ffmpeg=ffmpeg, config=r.config,
                                          reconnect=bool(self.reconnect.get()), keep_awake=bool(self.keep_awake.get()))
        except LiveBusyError as e:
            messagebox.showwarning("LIVE 시작 불가", str(e), parent=self)
            return
        if state is LiveState.FAILED:
            self._show_failed()
        self._refresh()

    def _stop(self):
        if not self.controller.active or self.controller.stopping:
            return
        if self.confirm_stop.get() and not messagebox.askyesno("LIVE 종료", "LIVE 송출을 종료할까요?", parent=self):
            return
        self.controller.stop_async()
        self._refresh()

    def _show_failed(self):
        if self._failed_shown:
            return
        self._failed_shown = True
        snap = self.controller.snapshot()
        msg = redact(snap.last_error or "FFmpeg 송출이 중단되었습니다.", [self.key_var.get().strip()])
        messagebox.showerror("LIVE 오류", msg, parent=self)

    # ---------- periodic UI update (Tk main thread only) ----------
    def _tick(self):
        self._tick_job = None
        try:
            for ev in self.controller.drain_events():
                if ev[0] == "state" and ev[1] is LiveState.FAILED and not self._closing:
                    self.after_idle(self._show_failed)
            self._refresh()
        finally:
            if self.winfo_exists():
                self._tick_job = self.after(TICK_MS, self._tick)

    def _refresh(self):
        s = self.controller.snapshot()
        st = self.st
        label = "종료 중" if self.controller.stopping else s.label
        st["state"].set(label)
        self.lbl_state.configure(foreground="red" if s.state is LiveState.RUNNING else ("darkorange" if s.state in LOCKED_STATES else ("firebrick" if s.state is LiveState.FAILED else "")))
        st["session"].set(format_duration(s.session_seconds) if s.session_seconds else "-")
        st["fps"].set(f"{s.fps:.1f}" if s.fps is not None else "-")
        st["bitrate"].set(format_bitrate(s.bitrate))
        st["speed"].set(f"{s.speed:.2f}x" if s.speed is not None else "-")
        st["out_time"].set(format_duration(s.out_time_seconds) if s.out_time_seconds is not None else "-")
        st["reconnects"].set(f"{s.reconnects}회")
        st["retry"].set(f"{s.retry_in:.0f}초" if s.retry_in is not None else "-")
        st["exit"].set("-" if s.last_exit_code is None else str(s.last_exit_code))
        st["error"].set(redact(s.last_error, [self.key_var.get().strip()]) or "없음")
        self._sync_widgets()

    def _sync_widgets(self):
        locked = self.controller.active
        normal = "disabled" if locked else "normal"
        for w in (self.btn_video, self.btn_reveal, self.rb_youtube, self.rb_custom, self.btn_check,
                  self.btn_start, self.chk_reconnect, self.chk_awake):
            w.configure(state=normal)
        self.ent_key.configure(state=normal)
        self.cmb_preset.configure(state="disabled" if locked else "readonly")
        self.ent_custom.configure(state="normal" if not locked and self.server_mode.get() == "custom" else "disabled")
        self.chk_remember.configure(state="normal" if not locked and getattr(self.store, "persistent", False) else "disabled")
        self.btn_stop.configure(state="normal" if locked and not self.controller.stopping else "disabled")

    # ---------- close lifecycle ----------
    def request_close(self):
        """LIVE 창 X 버튼."""
        if self.controller.active:
            if not messagebox.askyesno("LIVE 송출 중", "LIVE 송출 중입니다.\n송출을 종료하고 창을 닫을까요?", parent=self):
                return
            self.shutdown(self.destroy)
            return
        self.destroy()

    def shutdown(self, on_done: Callable[[], None] | None = None):
        """GUI를 멈추지 않고 graceful stop → watchdog 종료 → guard/keep-awake 해제 후 on_done 호출."""
        self._closing = True
        self.controller.stop_async()
        self._refresh()

        def wait_done():
            if self.controller.active or self.controller.stopping:
                self.after(100, wait_done)
                return
            self.controller.stop_blocking()  # 남은 watchdog/guard/keep-awake 정리 (이미 종료 상태면 즉시 반환)
            if on_done:
                on_done()
        wait_done()

    def destroy(self):
        if self.controller.active:
            # 예외 경로 안전장치: 창이 사라지기 전 반드시 FFmpeg 종료.
            self.controller.stop_blocking()
        self.controller.keep_awake.disable()
        for job in (self._tick_job, self._reveal_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self.key_var.set("")
        super().destroy()
