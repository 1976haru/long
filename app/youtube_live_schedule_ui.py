"""② 실시간 스트리밍 · 예약 LIVE 창 — YouTube에 앞으로의 LIVE 방송을 미리 예약 (반복 규칙, 7일/최대 7개 rolling).

실행 위치
- 무료 Cloud (TRUE 예약 LIVE): 영상을 Cloud에 보내고 Cloud 자동 시작(job)을 등록 → 예약 시각에 PC가 꺼져 있어도
  Cloud가 Playlist를 송출하고 YouTube가 자동으로 LIVE/종료 (enableAutoStart/Stop). 준비 Pipeline은 scheduled_live.py.
- YouTube 예약만: 기존 동작 (방송 페이지만 예약, 송출은 직접 시작).

- LIVE 창에서 연결한 YouTube 계정(자동 세션 계정)을 그대로 쓴다. 연결은 ② LIVE 창의 [YouTube 연결]에서.
- 규칙(비밀 아님)은 settings.json "live_schedule_rules"에 저장 → [부족분 보충]으로 다음 회차를 이어서 만든다
  (Cloud 규칙은 회차마다 Cloud job도 함께, 같은 영상은 다시 업로드하지 않음).
- 예약 생성/보충은 백그라운드 스레드에서 하고, 결과만 큐로 받는다 (Tk 객체를 스레드에 넘기지 않음).
"""
from __future__ import annotations

import queue
import secrets
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .ui_theme import ensure as ensure_theme
from .cloud_setup_ui import _background
from .core import format_duration
from .settings import load_settings, update_settings
from .tooling import release_tk_variables
from .ui_scroll import ScrollFrame
from .youtube_api import YouTubeApiError
from .youtube_config import build_api_client, is_connected, load_youtube_settings, save_youtube_settings
from .youtube_metadata import (
    DEFAULT_CATEGORIES, PRIVACY_LABELS, THUMB_FIXED, TIMEZONES, MetadataError, MetadataTemplate,
)
from .youtube_oauth import OAuthError
from .youtube_schedule import (
    CUSTOM_WEEKDAYS, DAY_CODES, MODE_LABELS, ONCE, ReservationStore, ScheduleError, ScheduleRule, get_zone, top_up,
)

RULES_KEY = "live_schedule_rules"
DAY_LABELS = ("월", "화", "수", "목", "금", "토", "일")
CUSTOM_DURATION = "직접 입력"
EXEC_CLOUD_LABEL = "무료 Cloud — PC를 꺼도 예약 시각에 자동 송출 (권장)"
EXEC_YT_LABEL = "YouTube 예약만 — 방송 페이지만 만들고 송출은 직접 시작"


def reservation_store() -> ReservationStore:
    return ReservationStore(load_settings, update_settings)


def load_rules() -> list[dict]:
    return [r for r in (load_settings().get(RULES_KEY) or []) if isinstance(r, dict) and r.get("rule_id")]


def save_rule(rule_id: str, rule: ScheduleRule, template: MetadataTemplate, **extra) -> None:
    """extra (Cloud 규칙): execution, media(로컬 경로 목록), cloud_playlist(SHA256 재사용용) — 비밀 없음."""
    rows = [r for r in load_rules() if r["rule_id"] != rule_id]
    old = next((r for r in load_rules() if r["rule_id"] == rule_id), {})
    row = {k: v for k, v in old.items() if k not in ("rule", "template")}
    row.update({"rule_id": rule_id, "rule": rule.to_dict(), "template": template.to_dict(), **extra})
    rows.append(row)
    update_settings(**{RULES_KEY: rows})


def update_rule_extra(rule_id: str, **extra) -> None:
    rows = load_rules()
    for r in rows:
        if r["rule_id"] == rule_id:
            r.update(extra)
    update_settings(**{RULES_KEY: rows})


def _err_text(e: Exception) -> str:
    from .cloud_client import CloudError
    from .cloud_model import CloudConfigError
    from .scheduled_live import ScheduledLiveError
    if isinstance(e, (YouTubeApiError, OAuthError, ScheduleError, MetadataError, CloudError, CloudConfigError,
                      ScheduledLiveError)):
        return str(e)
    return f"예약 중 오류 ({type(e).__name__})"


def _default_cloud_factory():
    from .live_ui import default_cloud_client
    return default_cloud_client()


def _cloud_configured() -> bool:
    from .cloud_model import load_cloud_profile
    return load_cloud_profile() is not None


def _analyze(paths, ffprobe):
    from .live_ready import analyze_live_ready
    return [analyze_live_ready(p, ffprobe) for p in paths]


class LiveScheduleWindow(tk.Toplevel):
    def __init__(self, master, *, api_factory: Callable = build_api_client, connected: Callable[[], bool] = is_connected,
                 clock: Callable[[], float] = time.time, pick_file: Callable = filedialog.askopenfilename,
                 open_url: Callable[[str], object] = webbrowser.open,
                 playlist_source: Callable[[], list] | None = None, tools: Callable[[], tuple] | None = None,
                 cloud_factory: Callable | None = None, cloud_configured: Callable[[], bool] = _cloud_configured,
                 analyze: Callable = _analyze, pick_files: Callable = filedialog.askopenfilenames):
        super().__init__(master)
        ensure_theme(self)  # 글자 크기/버튼 테마 (ui_theme)
        self.title("② 예약 LIVE")
        self.geometry("980x820")
        self.minsize(820, 600)
        self._api_factory = api_factory
        self._connected = connected
        self._clock = clock
        self._pick_file = pick_file
        self._pick_files = pick_files
        self._open_url = open_url
        self._playlist_source = playlist_source or (lambda: [])
        self._tools = tools or (lambda: (None, None))
        self._cloud_factory = cloud_factory or _default_cloud_factory
        self._cloud_configured = cloud_configured
        self._analyze_fn = analyze
        self._q: queue.Queue = queue.Queue()
        self._worker = None
        self._cancel = threading.Event()
        self.store = reservation_store()
        # 여러 채널: 예약할 채널 (LIVE 창에서 고른 채널로 시작). 기본 채널은 기존 연결/설정 그대로.
        from .live_channels import LiveChannelStore
        self.channels = LiveChannelStore()
        self.channel_id = self.channels.selected_id()
        self._channel_ids: list[str] = []
        self.channel_var = tk.StringVar()
        self.media: list[tuple[Path, object]] = []  # (경로, LiveReadyReport|None) — 방송 영상 순서
        self.media_from_live = False
        self.last_outcome = None

        now_local = datetime.fromtimestamp(clock(), timezone.utc).astimezone(get_zone("Asia/Seoul"))
        self.title_template = tk.StringVar(value="{date} ({weekday}) 24H LIVE #{session}")
        self.tags = tk.StringVar()
        self.thumbnail = tk.StringVar()
        self.privacy = tk.StringVar(value=PRIVACY_LABELS["unlisted"])
        self.category = tk.StringVar(value=DEFAULT_CATEGORIES["10"])
        self.repeat = tk.StringVar(value=MODE_LABELS["DAILY"])
        self.start_date = tk.StringVar(value=(now_local.date() + timedelta(days=1)).isoformat())
        self.start_time = tk.StringVar(value="07:00")
        self.tz = tk.StringVar(value="Asia/Seoul")
        self.duration = tk.IntVar(value=710)
        self.duration_preset = tk.StringVar(value="11시간 50분")
        self.days = {code: tk.BooleanVar(value=False) for code in DAY_CODES}
        self.account = tk.StringVar()
        self.message = tk.StringVar()
        self.media_text = tk.StringVar()
        cloud_ok = False
        try:
            cloud_ok = bool(self._cloud_configured())
        except Exception:
            pass
        self.execution = tk.StringVar(value="cloud" if cloud_ok else "youtube")
        self.advanced = tk.BooleanVar(value=False)
        self.card_text = tk.StringVar()
        self.progress_text = tk.StringVar()

        self._ui()
        self.use_live_playlist(quiet=True)
        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(200, self._pump)

    # ---------------- layout ----------------
    def _ui(self):
        from .scheduled_live import DELAY_NOTICE, DURATION_PRESETS, STEPS
        sf = ScrollFrame(self)
        sf.pack(fill="both", expand=True)
        self._scroll = sf
        root = ttk.Frame(sf.body, padding=12)
        root.pack(fill="both", expand=True)
        head = ttk.Frame(root); head.pack(fill="x")
        ttk.Label(head, text="② 예약 LIVE", font="PLS.Title").pack(side="left")
        ttk.Button(head, text="초보자 빠른 예약", style="Primary.TButton", command=self.beginner_quick).pack(side="right")
        self.channel_row = ttk.Frame(root)
        ttk.Label(self.channel_row, text="예약할 채널", font="PLS.Strong").pack(side="left")
        self.cmb_channel = ttk.Combobox(self.channel_row, textvariable=self.channel_var, state="readonly", width=28)
        self.cmb_channel.pack(side="left", padx=(6, 0))
        self.cmb_channel.bind("<<ComboboxSelected>>", lambda e: self._on_channel())
        self.channel_row.pack(fill="x", pady=(2, 0))
        self._load_channels()
        ttk.Label(root, textvariable=self.account, foreground="gray30").pack(anchor="w", pady=(0, 8))

        form = ttk.LabelFrame(root, text="① 방송 영상 · 시간 · 정보", padding=8)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        def row(parent, r, text, widget):
            ttk.Label(parent, text=text, width=12).grid(row=r, column=0, sticky="nw", pady=2)
            widget.grid(row=r, column=1, sticky="ew", pady=2)
        mf = ttk.Frame(form)
        ttk.Label(mf, textvariable=self.media_text, justify="left", wraplength=700).pack(anchor="w")
        mb = ttk.Frame(mf); mb.pack(anchor="w", pady=(2, 0))
        ttk.Button(mb, text="현재 LIVE Playlist 사용", command=self.use_live_playlist).pack(side="left")
        ttk.Button(mb, text="영상 직접 선택", command=self._pick_media).pack(side="left", padx=(5, 0))
        row(form, 0, "방송 영상", mf)
        sf2 = ttk.Frame(form)
        ttk.Label(sf2, text="날짜").pack(side="left")
        ttk.Entry(sf2, textvariable=self.start_date, width=12).pack(side="left", padx=(2, 8))
        ttk.Label(sf2, text="시각").pack(side="left")
        ttk.Entry(sf2, textvariable=self.start_time, width=7).pack(side="left", padx=(2, 8))
        ttk.Label(sf2, text="방송 길이").pack(side="left", padx=(4, 2))
        cb_d = ttk.Combobox(sf2, textvariable=self.duration_preset, state="readonly", width=11,
                            values=[label for label, _ in DURATION_PRESETS] + [CUSTOM_DURATION])
        cb_d.pack(side="left")
        cb_d.bind("<<ComboboxSelected>>", lambda e: self._on_duration_preset())
        self.spin_duration = ttk.Spinbox(sf2, from_=1, to=720, width=6, textvariable=self.duration)
        self.spin_duration.pack(side="left", padx=(4, 0))
        ttk.Label(sf2, text="분").pack(side="left", padx=(2, 0))
        row(form, 1, "시작", sf2)
        row(form, 2, "제목", ttk.Entry(form, textvariable=self.title_template))
        ttk.Label(form, foreground="gray30", text="변수: {date} {month} {day} {weekday} {session} {channel}").grid(
            row=3, column=1, sticky="w")
        tf = ttk.Frame(form)
        ttk.Entry(tf, textvariable=self.thumbnail).pack(side="left", fill="x", expand=True)
        ttk.Button(tf, text="찾기", command=self._pick_thumb).pack(side="left", padx=(4, 0))
        row(form, 4, "썸네일 (선택)", tf)
        row(form, 5, "공개 상태", ttk.Combobox(form, textvariable=self.privacy, state="readonly", width=10,
                                            values=list(PRIVACY_LABELS.values())))
        ef = ttk.Frame(form)
        self.rb_exec_cloud = ttk.Radiobutton(ef, text=EXEC_CLOUD_LABEL, variable=self.execution, value="cloud",
                                             command=self._on_execution)
        self.rb_exec_cloud.pack(anchor="w")
        ttk.Radiobutton(ef, text=EXEC_YT_LABEL, variable=self.execution, value="youtube",
                        command=self._on_execution).pack(anchor="w")
        ttk.Label(ef, text=DELAY_NOTICE, foreground="gray30").pack(anchor="w")
        row(form, 6, "실행 위치", ef)

        self.btn_adv = ttk.Button(form, text="고급 설정 ▸ (반복 · 설명 · 태그 · 카테고리 · 시간대)",
                                  command=self.toggle_advanced)
        self.btn_adv.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
        adv = self.adv_frame = ttk.Frame(form)
        adv.columnconfigure(1, weight=1)
        self.txt_desc = tk.Text(adv, height=3, wrap="word")
        row(adv, 0, "설명", self.txt_desc)
        row(adv, 1, "태그", ttk.Entry(adv, textvariable=self.tags))
        row(adv, 2, "카테고리", ttk.Combobox(adv, textvariable=self.category, state="readonly", width=14,
                                          values=list(DEFAULT_CATEGORIES.values())))
        rf = ttk.Frame(adv)
        cb = ttk.Combobox(rf, textvariable=self.repeat, state="readonly", width=10, values=list(MODE_LABELS.values()))
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_repeat())
        self.day_checks = []
        for code, label in zip(DAY_CODES, DAY_LABELS):
            c = ttk.Checkbutton(rf, text=label, variable=self.days[code])
            c.pack(side="left", padx=(6 if code == "MON" else 0, 0))
            self.day_checks.append(c)
        row(adv, 3, "반복", rf)
        row(adv, 4, "시간대", ttk.Combobox(adv, textvariable=self.tz, width=16, values=list(TIMEZONES)))

        act = ttk.Frame(form); act.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.btn_create = ttk.Button(act, text="", style="Primary.TButton", command=self.create)
        self.btn_create.pack(side="left", fill="x", expand=True)
        self.btn_topup = ttk.Button(act, text="저장된 규칙 부족분 보충", command=self.top_up_saved)
        self.btn_topup.pack(side="left", padx=(5, 0))
        self.btn_stop = ttk.Button(act, text="준비 중지", command=self._cancel.set)
        self.lbl_msg = ttk.Label(root, textvariable=self.message, justify="left", wraplength=900)
        self.lbl_msg.pack(anchor="w", pady=(6, 0))

        # 예약 준비 진행 (Cloud)
        self.steps_frame = ttk.LabelFrame(root, text="예약 준비 중", padding=8)
        self.step_vars = {}
        for key, label in STEPS:
            v = tk.StringVar(value=f"○ {label}")
            self.step_vars[key] = v
            ttk.Label(self.steps_frame, textvariable=v).pack(anchor="w")
        self.prog_bar = ttk.Progressbar(self.steps_frame, maximum=100)
        self.prog_bar.pack(fill="x", pady=(4, 0))
        ttk.Label(self.steps_frame, textvariable=self.progress_text, foreground="gray30").pack(anchor="w")

        # 예약 완료 카드
        self.card = ttk.LabelFrame(root, text="예약 결과", padding=10)
        self.lbl_card = ttk.Label(self.card, textvariable=self.card_text, justify="left", font="PLS.Strong")
        self.lbl_card.pack(anchor="w")
        cr = ttk.Frame(self.card); cr.pack(anchor="w", pady=(6, 0))
        self.btn_card_open = ttk.Button(cr, text="YouTube 예약 보기", command=self._open_card)
        self.btn_card_open.pack(side="left")
        self.btn_card_cancel = ttk.Button(cr, text="예약 취소", command=self._cancel_card)
        self.btn_card_cancel.pack(side="left", padx=(5, 0))
        self.btn_card_retry = ttk.Button(cr, text="Cloud 준비 다시 시도", command=self._retry_card)

        self.list_frame = lf = ttk.LabelFrame(root, text="② 예약된 LIVE", padding=8)
        lf.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("start", "title", "privacy", "state", "cloud")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", height=8, selectmode="browse")
        for c, h, w in zip(cols, ("시작 (현지)", "제목", "공개", "상태", "Cloud 자동 송출"), (150, 380, 70, 110, 150)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c == "title" else "center")
        self.tree.pack(fill="both", expand=True)
        tb = ttk.Frame(lf); tb.pack(fill="x", pady=(4, 0))
        ttk.Button(tb, text="YouTube에서 보기", command=self.open_selected).pack(side="left")
        self.btn_cancel_sel = ttk.Button(tb, text="예약 취소", command=self.cancel_selected)
        self.btn_cancel_sel.pack(side="left", padx=(5, 0))
        self.btn_retry_sel = ttk.Button(tb, text="Cloud 준비 다시 시도", command=self.retry_selected)
        self.btn_retry_sel.pack(side="left", padx=(5, 0))
        self.btn_sync = ttk.Button(tb, text="Cloud 상태 확인", command=self.sync_cloud)
        self.btn_sync.pack(side="left", padx=(5, 0))
        ttk.Button(tb, text="목록에서 지우기", command=self.remove_selected).pack(side="right")
        ttk.Label(lf, foreground="gray30", text="목록에서 지워도 YouTube 예약은 지워지지 않습니다 (YouTube Studio에서 관리).").pack(anchor="w")
        self._on_repeat()
        self._on_execution()

    # ---------------- helpers ----------------
    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def _say(self, text: str, color: str = "") -> None:
        self.message.set(text)
        self.lbl_msg.configure(foreground=color or "black")

    def _on_repeat(self):
        st = "normal" if self._repeat_mode() == CUSTOM_WEEKDAYS else "disabled"
        for c in self.day_checks:
            c.configure(state=st)

    def _repeat_mode(self) -> str:
        return next((k for k, v in MODE_LABELS.items() if v == self.repeat.get()), "ONCE")

    def _on_duration_preset(self):
        from .scheduled_live import DURATION_PRESETS
        minutes = dict(DURATION_PRESETS).get(self.duration_preset.get())
        if minutes:
            self.duration.set(minutes)
            self.spin_duration.configure(state="disabled")
        else:
            self.spin_duration.configure(state="normal")

    def _on_execution(self):
        cloud = self.execution.get() == "cloud"
        self.btn_create.configure(text="예약 준비 시작 (Cloud 자동 송출 · PC 꺼도 됨)" if cloud
                                  else "＋ 예약 만들기 (앞으로 7일 · 최대 7개)")

    def toggle_advanced(self, show: bool | None = None):
        show = (not self.advanced.get()) if show is None else show
        self.advanced.set(show)
        if show:
            self.adv_frame.grid(row=8, column=0, columnspan=2, sticky="ew")
            self.btn_adv.configure(text="고급 설정 ▾ 접기")
        else:
            self.adv_frame.grid_remove()
            self.btn_adv.configure(text="고급 설정 ▸ (반복 · 설명 · 태그 · 카테고리 · 시간대)")

    def beginner_quick(self):
        """[초보자 빠른 예약]: 핵심만 (영상/날짜/시간/길이/제목/썸네일/공개). 한 번만 · 일부공개 · 2시간 · 무료 Cloud."""
        from .scheduled_live import FIRST_TEST_DURATION
        self.toggle_advanced(False)
        self.repeat.set(MODE_LABELS[ONCE])
        self._on_repeat()
        self.privacy.set(PRIVACY_LABELS["unlisted"])
        self.duration.set(FIRST_TEST_DURATION)
        self.duration_preset.set("2시간")
        self._on_duration_preset()
        if self._cloud_configured():
            self.execution.set("cloud")
        self._on_execution()
        if not self.media:
            self.use_live_playlist(quiet=True)
        self._say("초보자 빠른 예약: 한 번만 · 일부공개 · 방송 2시간으로 맞췄습니다.\n"
                  "날짜/시각과 제목을 확인하고 [예약 준비 시작]을 누르세요.", "darkgreen")

    def _pick_thumb(self):
        p = self._pick_file(parent=self, title="썸네일 선택", filetypes=[("이미지", "*.jpg *.jpeg *.png"), ("모든 파일", "*.*")])
        if p:
            self.thumbnail.set(p)

    # ---------------- 방송 영상 ----------------
    def set_media(self, items, *, from_live: bool) -> None:
        self.media = [(Path(p), r) for p, r in items]
        self.media_from_live = from_live
        self._update_media_text()
        missing = [p for p, r in self.media if r is None]
        if missing:
            self._analyze_missing(missing)

    def use_live_playlist(self, quiet: bool = False):
        try:
            items = list(self._playlist_source() or [])
        except Exception:
            items = []
        if not items:
            if not quiet:
                messagebox.showinfo("방송 영상", "LIVE 창에 영상이 없습니다.\n24H LIVE 창에서 Playlist를 만들거나 [영상 직접 선택]을 누르세요.",
                                    parent=self)
            self._update_media_text()
            return
        self.set_media(items, from_live=True)

    def _pick_media(self):
        picked = self._pick_files(parent=self, title="예약 LIVE 영상 선택 (순서대로 반복)",
                                  filetypes=[("MP4", "*.mp4"), ("모든 파일", "*.*")])
        if picked:
            self.set_media([(Path(p), None) for p in picked], from_live=False)

    def _analyze_missing(self, paths):
        _, ffprobe = self._tools()
        if not ffprobe:
            return
        q, fn = self._q, self._analyze_fn

        def work():
            try:
                q.put(("media_reports", [str(p) for p in paths], fn(paths, ffprobe)))
            except Exception:
                q.put(("media_reports", [], []))
        threading.Thread(target=work, name="schedule-analyze", daemon=True).start()

    def _update_media_text(self):
        from .live_playlist import validate_playlist
        if not self.media:
            self.media_text.set("영상 없음 — [현재 LIVE Playlist 사용] 또는 [영상 직접 선택]")
            return
        reports = [r for _, r in self.media]
        total = sum(getattr(r, "duration", 0.0) or 0.0 for r in reports)
        src = "● 현재 LIVE Playlist 사용" if self.media_from_live else "● 직접 선택한 영상"
        lines = [f"{src}  ·  총 {len(self.media)}개 / 총 길이 {format_duration(total) if all(reports) else '확인 중'}"]
        lines += [f"  {n}. {p.name}" for n, (p, _) in enumerate(self.media, 1)]
        if all(r is not None for r in reports):
            v = validate_playlist(reports)
            lines.append("✓ Playlist LIVE READY · DIRECT COPY 가능" if v.ok else f"⚠ {v.first_error}\n"
                         "→ 24H LIVE 창의 [문제 영상 모두 LIVE READY로 만들기]로 먼저 맞추세요.")
        self.media_text.set("\n".join(lines))

    # ---------------- list ----------------
    # ---------------- 채널 (여러 채널 LIVE) ----------------
    def _load_channels(self) -> None:
        profiles = self.channels.all()
        self._channel_ids = [p.channel_profile_id for p in profiles]
        if self.channel_id not in self._channel_ids:
            self.channel_id = self._channel_ids[0]
        self.cmb_channel.configure(values=[p.display_name for p in profiles])
        self.cmb_channel.current(self._channel_ids.index(self.channel_id))
        if len(profiles) > 1:
            self.channel_row.pack(fill="x", pady=(2, 0))
        else:
            self.channel_row.pack_forget()  # 채널 1개: 기존 화면 그대로

    def _on_channel(self) -> None:
        i = self.cmb_channel.current()
        if 0 <= i < len(self._channel_ids) and not self.busy:
            self.channel_id = self._channel_ids[i]
            self.refresh()

    def _channel(self, pid: str | None = None):
        from .live_channels import default_channel
        return self.channels.get(pid or self.channel_id) or default_channel()

    def _legacy(self, pid: str | None = None) -> bool:
        """기본 채널 + 채널 전용 Google 연결 없음 → 기존 연결(youtube_token.dat / settings["youtube"])."""
        ch = self._channel(pid)
        return ch.is_default and not ch.oauth_profile_id

    def _api_factory_for(self, pid: str | None = None) -> Callable:
        if self._legacy(pid):
            return self._api_factory
        from .live_channels import channel_api
        ch = self._channel(pid)
        return lambda: channel_api(ch)

    def _channel_youtube(self, pid: str | None = None) -> tuple[bool, str, str | None]:
        """(연결 여부, 실제 YouTube 채널 이름, 저장된 송출 스트림 ID)."""
        if self._legacy(pid):
            s = load_youtube_settings()
            return bool(self._connected()), s.get("channel_title") or "", s.get("stream_id") or None
        from .live_channels import oauth_connected, oauth_profile_for
        ch = self._channel(pid)
        op = oauth_profile_for(ch)
        return (bool(oauth_connected(ch)), op.channel_title if op else "", (op.stream_id or None) if op else None)

    def _save_stream_id(self, pid: str, stream_id: str) -> None:
        if self._legacy(pid):
            save_youtube_settings(stream_id=stream_id)
            return
        from .live_channels import oauth_profile_for
        from .youtube_accounts import ProfileStore
        profiles = ProfileStore()
        op = oauth_profile_for(self._channel(pid), profiles)
        if op is not None and op.stream_id != stream_id:
            op.stream_id = stream_id
            profiles.save(op)

    def _refresh_account(self) -> bool:
        """YouTube 연결 상태 (LIVE 창에서 연결/해제하면 창을 다시 열지 않아도 반영)."""
        try:
            ok, title, _ = self._channel_youtube()
        except Exception:
            ok, title = False, ""
        self.account.set(f"✓ YouTube 연결됨 · 채널: {title or '-'}" if ok else
                         "○ YouTube 계정이 연결되지 않았습니다 → ② LIVE 창 ③ YouTube 송출의 [YouTube 연결]을 먼저 하세요.")
        self._account_ok = ok
        return ok

    def refresh(self) -> None:
        ok = self._refresh_account()
        sel = self._selected()
        for x in self.tree.get_children():
            self.tree.delete(x)
        for r in self.store.all():
            try:
                start = r.start_local(self.tz.get().strip() or "Asia/Seoul").strftime("%Y-%m-%d %H:%M")
            except (ScheduleError, ValueError):
                start = r.start_utc
            self.tree.insert("", "end", iid=r.broadcast_id, values=(
                start, r.title, PRIVACY_LABELS.get(r.privacy, r.privacy), r.status_label, r.cloud_label))
        if sel and self.tree.exists(sel):
            self.tree.selection_set(sel)
        st = "disabled" if self.busy or not ok else "normal"
        self.btn_create.configure(state=st)
        self.btn_topup.configure(state="disabled" if self.busy or not ok or not load_rules() else "normal")
        for b in (self.btn_cancel_sel, self.btn_retry_sel, self.btn_sync, self.btn_card_cancel, self.btn_card_retry):
            b.configure(state="disabled" if self.busy else "normal")
        if self.busy:
            self.btn_stop.pack(side="left", padx=(5, 0))
        else:
            self.btn_stop.pack_forget()

    def build(self) -> tuple[ScheduleRule, MetadataTemplate]:
        mode = self._repeat_mode()
        try:
            duration = int(self.duration.get())
        except (tk.TclError, ValueError):
            raise ScheduleError("방송 길이(분)를 숫자로 입력하세요.") from None
        rule = ScheduleRule(mode=mode, start_date=self.start_date.get().strip(), start_time_local=self.start_time.get().strip(),
                            timezone=self.tz.get().strip(), duration_minutes=duration,
                            custom_weekdays=[c for c in DAY_CODES if self.days[c].get()] if mode == CUSTOM_WEEKDAYS else [])
        privacy = next((k for k, v in PRIVACY_LABELS.items() if v == self.privacy.get()), "unlisted")
        category = next((k for k, v in DEFAULT_CATEGORIES.items() if v == self.category.get()), "10")
        thumb = self.thumbnail.get().strip().strip('"')
        template = MetadataTemplate(name="예약 LIVE", title_template=self.title_template.get(),
                                    description_template=self.txt_desc.get("1.0", "end").strip(), tags=self.tags.get(),
                                    thumbnail_mode=THUMB_FIXED, thumbnail_paths=[thumb] if thumb else [],
                                    category_id=category, privacy_status=privacy)
        return rule.validate(), template.validate()

    # ---------------- 예약 만들기 ----------------
    def _run(self, jobs: list[tuple[str, dict, dict]], save_new: bool) -> None:
        """YouTube 예약만 (기존). jobs: (rule_id, rule dict, template dict) — 스레드에는 dict만 넘긴다."""
        api_factory, clock, store = self._api_factory_for(), self._clock, self.store
        _, channel, stream_id = self._channel_youtube()

        def work():
            api = api_factory()
            now = datetime.fromtimestamp(clock(), timezone.utc)
            made, lines = 0, []
            for rule_id, rd, td in jobs:
                rule, tpl = ScheduleRule.from_dict(rd), MetadataTemplate.from_dict(td)
                if save_new:
                    save_rule(rule_id, rule, tpl, execution="youtube")
                for res in top_up(api, store, rule_id=rule_id, rule=rule, template=tpl, now=now, stream_id=stream_id,
                                  channel=channel):
                    made += res.broadcast_ok
                    if not res.complete:
                        lines.extend(res.summary_lines())
            return made, lines

        def result(ok, v):
            return ("done", ok, v if ok else _err_text(v))
        self._worker = _background("youtube-live-schedule", self._q, work, result)
        self._say("YouTube에 예약 중…")
        self.refresh()

    def _run_cloud(self, jobs: list[dict]) -> None:
        """TRUE 예약 LIVE 준비 (Cloud). jobs: {rule_id, rule, template, media, reports?, cloud_playlist, save_new}."""
        from .scheduled_live import STEP_LABELS, prepare_cloud_schedule
        cloud_factory, clock, store = self._cloud_factory, self._clock, self.store
        # 채널마다: YouTube API(채널 token) · 송출 스트림 · 채널 이름 (스레드에는 Tk 객체 없이 값/함수만)
        per = {}
        for job in jobs:
            pid = job.get("profile_id") or self.channel_id
            job["profile_id"] = pid
            if pid not in per:
                _, title, stream = self._channel_youtube(pid)
                per[pid] = (self._api_factory_for(pid), stream, title)
        q, cancel, analyze_fn = self._q, self._cancel, self._analyze_fn
        _, ffprobe = self._tools()
        cancel.clear()
        for key, label in STEP_LABELS.items():
            self.step_vars[key].set(f"○ {label}")
        self.prog_bar["value"] = 0
        self.progress_text.set("")
        self.card.pack_forget()
        self.steps_frame.pack(fill="x", pady=(8, 0), before=self.list_frame)

        def emit(kind, *payload):
            q.put(("cloud_" + kind, *payload))

        def work():
            client = cloud_factory()
            now = datetime.fromtimestamp(clock(), timezone.utc)
            outcomes = []
            apis = {}
            for job in jobs:
                pid = job["profile_id"]
                api_factory, saved_stream, channel = per[pid]
                if pid not in apis:
                    apis[pid] = api_factory()
                api = apis[pid]
                rule, tpl = ScheduleRule.from_dict(job["rule"]), MetadataTemplate.from_dict(job["template"])
                paths = [Path(p) for p in job["media"]]
                reports = job.get("reports")
                if reports is None or any(r is None for r in reports):
                    if not ffprobe:
                        raise RuntimeError("FFmpeg/ffprobe를 찾을 수 없습니다 (영상 검사 불가).")
                    reports = analyze_fn(paths, ffprobe)
                if job.get("save_new"):
                    save_rule(job["rule_id"], rule, tpl, execution="cloud", media=[str(p) for p in paths],
                              profile_id=pid)
                out = prepare_cloud_schedule(
                    api=api, client=client, media_paths=paths, reports=reports, rule=rule, template=tpl,
                    rule_id=job["rule_id"], store=store, now=now, saved_stream_id=saved_stream, channel=channel,
                    emit=emit, cancel=cancel, saved_playlist=job.get("cloud_playlist"),
                    on_stream=lambda sid, pid=pid: q.put(("stream_id", sid, pid)),
                    on_playlist=lambda pl, rid=job["rule_id"]: update_rule_extra(rid, cloud_playlist=pl),
                    profile_id=pid)
                outcomes.append(out)
                if out.failed_step:
                    break
            return outcomes

        def result(ok, v):
            return ("cloud_done", ok, v if ok else _err_text(v))
        self._worker = _background("cloud-live-schedule", q, work, result)
        self._say("예약 준비 중… (영상이 크면 Cloud 전송에 시간이 걸립니다. 창을 닫지 마세요)")
        self.refresh()

    def create(self):
        if self.busy:
            return
        try:
            rule, template = self.build()
        except (ScheduleError, MetadataError) as e:
            messagebox.showerror("예약 LIVE", str(e), parent=self)
            return
        if self.execution.get() != "cloud":
            self._run([(secrets.token_hex(6), rule.to_dict(), template.to_dict())], save_new=True)
            return
        if not self._cloud_configured():
            messagebox.showwarning("예약 LIVE", "무료 Cloud가 아직 설정되지 않았습니다.\n24H LIVE 창의 [처음 설정 도우미]를 먼저 진행하거나\n"
                                   "'YouTube 예약만'을 선택하세요.", parent=self)
            return
        if not self.media:
            messagebox.showwarning("예약 LIVE", "방송 영상이 없습니다. [현재 LIVE Playlist 사용] 또는 [영상 직접 선택]을 누르세요.",
                                   parent=self)
            return
        self._run_cloud([{"rule_id": secrets.token_hex(6), "rule": rule.to_dict(), "template": template.to_dict(),
                          "media": [str(p) for p, _ in self.media], "reports": [r for _, r in self.media],
                          "save_new": True, "profile_id": self.channel_id}])

    def top_up_saved(self):
        if self.busy:
            return
        rules = load_rules()
        if not rules:
            self._say("저장된 반복 규칙이 없습니다.")
            return
        yt = [r for r in rules if r.get("execution") != "cloud"]
        cloud = [r for r in rules if r.get("execution") == "cloud"]
        if cloud:
            if not self._cloud_configured():
                self._say("✗ Cloud 예약 규칙이 있지만 무료 Cloud가 설정되지 않았습니다.", "firebrick")
                return
            # Cloud 규칙: YouTube 예약과 Cloud job을 회차마다 함께 (같은 영상은 SHA256로 확인 후 다시 보내지 않음)
            self._pending_yt = [(r["rule_id"], r.get("rule") or {}, r.get("template") or {}) for r in yt]
            self._run_cloud([{"rule_id": r["rule_id"], "rule": r.get("rule") or {}, "template": r.get("template") or {},
                              "media": list(r.get("media") or []), "cloud_playlist": r.get("cloud_playlist") or None,
                              "profile_id": r.get("profile_id") or "default"}  # 규칙마다 저장된 채널 (기존 규칙 = 기본 채널)
                             for r in cloud])
            return
        self._run([(r["rule_id"], r.get("rule") or {}, r.get("template") or {}) for r in rules], save_new=False)

    # ---------------- 선택 항목 ----------------
    def _selected(self) -> str:
        sel = self.tree.selection()
        return sel[0] if sel else ""

    def _record(self, bid: str):
        return next((r for r in self.store.all() if r.broadcast_id == bid), None)

    def open_selected(self):
        bid = self._selected()
        if bid:
            self._open_url(f"https://www.youtube.com/watch?v={bid}")

    def remove_selected(self):
        bid = self._selected()
        if bid and messagebox.askyesno("목록에서 지우기", "이 예약을 목록에서만 지울까요? (YouTube 예약은 그대로 남습니다)",
                                       parent=self):
            self.store.remove(bid)
            self.refresh()

    def ask_cancel_mode(self, rec) -> str:
        """'youtube' (YouTube 예약도 삭제) | 'cloud' (Cloud 자동 시작만 취소) | '' (닫기)."""
        from .live_ui import ask_choice
        running = rec.cloud_state in ("STARTING", "LIVE")
        msg = (("Cloud에서 지금 송출 중입니다. 송출을 정상 종료합니다.\n\n" if running else "") +
               "예약된 YouTube LIVE도 삭제할까요?")
        choices = [("Cloud 자동 시작만 취소", "cloud"), ("YouTube 예약도 삭제", "youtube"), ("닫기", "")]
        if rec.execution != "cloud":
            msg, choices = "YouTube 예약을 삭제할까요?", [("YouTube 예약 삭제", "youtube"), ("닫기", "")]
        return ask_choice(self, "예약 취소", msg, choices, default=choices[0][1], cancel="")

    def cancel_selected(self, mode: str | None = None):
        rec = self._record(self._selected())
        if rec is None or self.busy:
            return
        mode = self.ask_cancel_mode(rec) if mode is None else mode
        if not mode:
            return
        from .scheduled_live import cancel_reservation
        api_factory = self._api_factory_for(getattr(rec, "profile_id", "") or "default")  # 예약한 채널의 YouTube 연결
        cloud_factory, store = self._cloud_factory, self.store
        need_cloud = rec.execution == "cloud" and bool(rec.cloud_job_id)

        def work():
            client = cloud_factory() if need_cloud else None
            api = api_factory() if mode == "youtube" else None
            return cancel_reservation(rec, store=store, client=client, api=api, delete_youtube=(mode == "youtube"))
        self._worker = _background("schedule-cancel", self._q, work,
                                   lambda ok, v: ("action_done", ok, v if ok else _err_text(v)))
        self._say("예약 취소 중…")
        self.refresh()

    def retry_selected(self):
        rec = self._record(self._selected())
        if rec is None or self.busy:
            return
        if rec.execution != "cloud" or rec.cloud_state != "PARTIAL":
            self._say("Cloud 준비가 실패한 예약(⚠)만 다시 시도할 수 있습니다.")
            return
        from .scheduled_live import retry_cloud_job
        api_factory = self._api_factory_for(getattr(rec, "profile_id", "") or "default")
        cloud_factory, clock, store = self._cloud_factory, self._clock, self.store

        def work():
            r = retry_cloud_job(rec, api=api_factory(), client=cloud_factory(), store=store,
                                now=datetime.fromtimestamp(clock(), timezone.utc))
            return ["✓ Cloud 자동 시작 준비 완료", f"{r.start_local().strftime('%Y-%m-%d %H:%M')} — PC를 꺼도 됩니다."]
        self._worker = _background("schedule-retry", self._q, work,
                                   lambda ok, v: ("action_done", ok, v if ok else _err_text(v)))
        self._say("Cloud 준비 다시 시도 중…")
        self.refresh()

    def sync_cloud(self):
        if self.busy:
            return
        if not self._cloud_configured():
            self._say("무료 Cloud가 설정되지 않았습니다.")
            return
        from .scheduled_live import sync_cloud_states
        cloud_factory, store = self._cloud_factory, self.store

        def work():
            n = sync_cloud_states(store, cloud_factory())
            return [f"✓ Cloud 상태를 확인했습니다 (바뀐 예약 {n}개)."]
        self._worker = _background("schedule-sync", self._q, work,
                                   lambda ok, v: ("action_done", ok, v if ok else _err_text(v)))
        self._say("Cloud 상태 확인 중…")
        self.refresh()

    # ---------------- 완료 카드 ----------------
    def _card_record(self):
        out = self.last_outcome
        return out.records[0] if out is not None and out.records else None

    def _open_card(self):
        rec = self._card_record()
        if rec is not None:
            self._open_url(rec.youtube_url)

    def _cancel_card(self):
        rec = self._card_record()
        if rec is not None and self.tree.exists(rec.broadcast_id):
            self.tree.selection_set(rec.broadcast_id)
            self.cancel_selected()

    def _retry_card(self):
        out = self.last_outcome
        rec = out.partial[0] if out is not None and out.partial else None
        if rec is not None and self.tree.exists(rec.broadcast_id):
            self.tree.selection_set(rec.broadcast_id)
            self.retry_selected()

    def show_outcome(self, outcomes) -> None:
        from .scheduled_live import STEP_LABELS, duration_label
        out = outcomes[-1] if outcomes else None
        self.last_outcome = out
        self.steps_frame.pack_forget()
        if out is None:
            return
        tz = self.tz.get().strip() or "Asia/Seoul"
        made = sum(o.made for o in outcomes)
        ready = sum(o.ready for o in outcomes)
        all_ok = all(o.all_ready or (o.made == 0 and not o.failed_step) for o in outcomes)
        first = next((o.first_start_utc for o in outcomes if o.first_start_utc), "")
        when = datetime.fromisoformat(first).astimezone(get_zone(tz)).strftime("%Y-%m-%d %H:%M") if first else "-"
        privacy = PRIVACY_LABELS.get(out.privacy, out.privacy)
        if made == 0 and all_ok:
            self.card_text.set("새로 만들 회차가 없습니다 (이미 7일/7개 준비됨).")
            color = "darkgreen"
        elif all_ok:
            lines = [f"✓ LIVE 예약 준비 완료 ({made}개)" if made > 1 else "✓ LIVE 예약 준비 완료", "",
                     f"{when}" + (f" 외 {made - 1}회" if made > 1 else ""), privacy,
                     f"방송 {duration_label(out.duration_minutes)}", f"Playlist {out.media_count}개", "무료 Cloud", "",
                     "✓ 영상 Cloud 저장 완료", "✓ YouTube 예약 완료", "✓ 자동 시작 등록 완료", "",
                     "PC를 종료해도 됩니다."]
            self.card_text.set("\n".join(lines))
            color = "darkgreen"
        else:
            failed = next((o for o in outcomes if o.failed_step or not o.all_ready), out)
            step = STEP_LABELS.get(failed.failed_step, failed.failed_step or "Cloud 준비")
            lines = []
            if made:
                lines += ["⚠ YouTube 예약은 만들어졌지만", "Cloud 자동 시작 준비에 실패했습니다." if ready < made
                          else "Cloud 자동 시작 확인을 끝내지 못했습니다.", f"(준비 완료 {ready} / {made})"]
            else:
                lines += [f"✗ 예약 준비 실패 — {step}", "YouTube 예약은 만들지 않았습니다."]
            if failed.errors:
                lines += ["", failed.errors[0]]
            lines += ["", "PC를 끄지 마세요 — 아직 자동 송출이 준비되지 않았습니다."]
            self.card_text.set("\n".join(lines))
            color = "darkorange" if made else "firebrick"
        self.lbl_card.configure(foreground=color)
        has_rec = out.records and self.tree.exists(out.records[0].broadcast_id)
        self.btn_card_open.configure(state="normal" if has_rec else "disabled")
        self.btn_card_cancel.configure(state="normal" if has_rec else "disabled")
        if out.partial:
            self.btn_card_retry.pack(side="left", padx=(5, 0))
        else:
            self.btn_card_retry.pack_forget()
        self.card.pack(fill="x", pady=(8, 0), before=self.list_frame)

    # ---------------- 이벤트 ----------------
    def _pump(self):
        if getattr(self, "_destroyed", False):
            return
        try:
            while True:
                ev = self._q.get_nowait()
                tag = ev[0]
                if tag == "cloud_step":
                    self._on_step(*ev[1:])
                elif tag == "cloud_progress":
                    self.prog_bar["value"] = ev[1] * 100
                    self.progress_text.set(ev[2])
                elif tag == "stream_id":
                    try:
                        self._save_stream_id(ev[2] if len(ev) > 2 else self.channel_id, ev[1])
                    except Exception:
                        pass  # 스트림 ID 기억은 편의 기능 (다음 예약에서 다시 찾음)
                elif tag == "media_reports":
                    by = dict(zip(ev[1], ev[2]))
                    self.media = [(p, by.get(str(p), r)) for p, r in self.media]
                    self._update_media_text()
                elif tag == "cloud_done":
                    self._worker = None
                    ok, payload = ev[1], ev[2]
                    if not ok:
                        self._pending_yt = None
                    if ok:
                        self.show_outcome(payload)
                        made = sum(o.made for o in payload)
                        all_ok = payload and all(o.all_ready or (o.made == 0 and not o.failed_step) for o in payload)
                        self._say(f"✓ 예약 {made}개 준비 완료 — 자동 시작 등록 확인됨" if all_ok and made else
                                  ("새로 만들 회차가 없습니다 (이미 7일/7개 준비됨)." if all_ok else
                                   "⚠ 일부 준비에 실패했습니다. 아래 결과를 확인하세요."),
                                  "darkgreen" if all_ok else "darkorange")
                        pending = getattr(self, "_pending_yt", None)
                        self._pending_yt = None
                        if pending:
                            self._run(pending, save_new=False)
                    else:
                        self.steps_frame.pack_forget()
                        self._say(f"✗ {payload}", "firebrick")
                    self.refresh()
                elif tag == "action_done":
                    self._worker = None
                    ok, payload = ev[1], ev[2]
                    self._say("\n".join(payload) if ok else f"✗ {payload}", "darkgreen" if ok else "firebrick")
                    self.refresh()
                elif tag == "done":
                    self._worker = None
                    ok, payload = ev[1], ev[2]
                    if ok:
                        made, lines = payload
                        text = f"✓ 예약 {made}개를 만들었습니다." if made else "새로 만들 회차가 없습니다 (이미 7일/7개 준비됨)."
                        self._say("\n".join([text, *lines]), "darkgreen" if not lines else "darkorange")
                    else:
                        self._say(f"✗ {payload}", "firebrick")
                    self.refresh()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self._pump_ticks = getattr(self, "_pump_ticks", 0) + 1
        if self._pump_ticks % 10 == 0:  # 2초마다 연결 상태 확인 → 바뀌었으면 버튼 상태까지 다시
            try:
                before = getattr(self, "_account_ok", None)
                if self._refresh_account() != before:
                    self.refresh()
            except tk.TclError:
                return
            except Exception:
                pass
        if self.winfo_exists():
            self.after(200, self._pump)

    def _on_step(self, key: str, status: str, text: str):
        from .scheduled_live import STEP_LABELS
        v = self.step_vars.get(key)
        if v is None:
            return
        mark = {"ok": "✓", "fail": "✗", "run": "…", "skip": "-"}.get(status, "○")
        v.set(f"{mark} {STEP_LABELS[key]}" + (f" — {text}" if text else ""))

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        self._cancel.set()  # 진행 중 Cloud 전송 중지 (이미 만든 예약은 그대로)
        super().destroy()
        release_tk_variables(self)
