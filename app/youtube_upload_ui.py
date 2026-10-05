"""③ 예약 업로드 창 — 한국·일본 등 여러 YouTube 채널에 완성 영상을 예약 업로드 (대기열, 1개씩 순차).

간편 흐름: ① 채널 → ② 영상 (1개 / 폴더 한꺼번에) → ③ 예약 (첫 날짜·시각·간격) → [미리보기] → [N개 대기열에 추가]
상세 설정(▼)에서만 제목·설명·태그·썸네일 방식·카테고리·언어·아동용·공개 상태를 바꾼다 (채널별 템플릿으로 저장).

- 채널을 바꾸면 그 채널의 마지막/기본 템플릿을 다시 적용한다 → 이전 채널의 설명/태그가 남아 잘못 올라가지 않는다.
- 업로드 대기열(UploadQueue)은 MainWindow가 가진다 → 이 창을 닫아도 업로드는 계속되고, 다시 열면 상태가 보인다.
- 화면에는 업로드 세션 URL/token을 표시하지 않는다. 영상/썸네일 파일은 읽기만 한다 (이동/삭제/이름 변경 없음).
"""
from __future__ import annotations

import tkinter as tk
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from . import help_ui
from . import youtube_upload_preview as preview_ui
from .help_content import TOOLTIPS
from .help_ui import InfoTip, show_usage
from .settings import load_settings, update_settings
from .tooling import release_tk_variables
from .ui_text import friendly_error, friendly_status, is_beginner
from .ui_scroll import ScrollFrame
from .youtube_accounts import ProfileStore, verify_channel
from .youtube_batch import (
    DAILY, EVERY_2_DAYS, EVERY_N_DAYS, INTERVAL_LABELS, SPEEDS_MBPS, WEEKDAYS, WEEKLY, BatchItem, BatchPlan,
    ThumbMatch, UploadTemplateStore, build_plan, episode_from_filename, estimate_seconds, human_duration, human_size,
    last_folder, match_all, profile_defaults_template, remember_folder, scan_folder, schedule_times,
)
from .youtube_metadata import (
    DEFAULT_CATEGORIES, LANGUAGES, PRIVACY_LABELS, TEMPLATE_VARIABLES, THUMB_FIXED, THUMB_FOLDER, MetadataError,
    MetadataTemplate,
)
from .youtube_schedule import ScheduleError, get_zone
from .youtube_upload_queue import (
    ACTIVE_STATES, API_REVIEW_REQUIRED, BLOCKED, CANCELLED, COMPLETE, FAILED, MAX_JOBS, PARTIAL, PAUSED, PENDING,
    QueueError, UploadQueue,
)
from .youtube_comments import DEFAULT_FIRST_COMMENTS, TASK_LABELS, WAITING_PUBLIC, WAITING_PRIVACY_CHANGE, CommentStore
from .youtube_usage import usage_text

SCHEDULE, NOW = "schedule", "now"
UPLOAD_STEPS = ("채널 선택", "영상 선택", "날짜 선택", "미리보기", "예약 시작")
LAST_PROFILE_KEY, LAST_TIME_KEY = "upload_last_profile", "upload_last_time"


def default_upload_time() -> str:
    """마지막으로 쓴 시간 → 처음 설정의 기본 시간 → 19:00."""
    from .settings import load_settings
    d = load_settings()
    t = d.get(LAST_TIME_KEY) or (d.get("upload_defaults") or {}).get("time") or "19:00"
    return t if isinstance(t, str) and len(t) == 5 and t[2] == ":" else "19:00"
TIME_PRESETS = ("07:00", "09:00", "18:00", "19:00", "21:00")
THUMB_NONE = "none"
THUMB_STRATEGY = {THUMB_NONE: "없음", THUMB_FIXED: "고정 1개", THUMB_FOLDER: "폴더에서 순서대로"}
# 상태 색 (항상 글자와 함께 — 색만으로 구분하지 않음)
STATE_COLORS = {PENDING: "black", COMPLETE: "darkgreen", PARTIAL: "darkorange", BLOCKED: "firebrick",
                FAILED: "firebrick", API_REVIEW_REQUIRED: "purple", PAUSED: "darkorange", CANCELLED: "gray45"}
ACTIVE_COLOR = "#1d4fa8"


def _label(mapping: dict, key: str) -> str:
    return f"{mapping.get(key, key)} ({key})" if key else mapping.get(key, "")


def _key(mapping: dict, label: str) -> str:
    return next((k for k in mapping if _label(mapping, k) == label), label.strip())


class MultiChannelUploadWindow(tk.Toplevel):
    def __init__(self, master, *, upload_queue: UploadQueue, profiles: ProfileStore | None = None,
                 templates: UploadTemplateStore | None = None, channel_window: Callable | None = None,
                 pick_file: Callable = filedialog.askopenfilename, pick_files: Callable = filedialog.askopenfilenames,
                 pick_dir: Callable = filedialog.askdirectory, video_path: str = "", title: str = "",
                 clock: Callable[[], float] | None = None, live_guard: Callable[[], str] | None = None,
                 comments=None):
        super().__init__(master)
        self.title("③ 예약 업로드 · 여러 YouTube 채널")
        sh = self.winfo_screenheight()
        self.geometry(f"1040x{max(520, min(900, sh - 90))}")
        self.minsize(820, 480)
        self.q = upload_queue
        self.profiles = profiles or upload_queue.profiles
        self.templates = templates or UploadTemplateStore()
        self._channel_window_factory = channel_window
        self._pick_file, self._pick_files, self._pick_dir = pick_file, pick_files, pick_dir
        self._clock = clock or upload_queue.clock
        self._live_guard = live_guard
        self.comments = comments  # CommentService (MainWindow가 가짐). 없으면 댓글 창을 열 때 만든다
        self.comment_win = None
        self.channel_win = None
        self.preview_win = None
        self.dnd_enabled = False  # v1.1: 끌어놓기는 꺼 둠 (선택 버튼/폴더 추가는 항상 사용 가능)
        self._profile_ids: list[str] = []
        self._template_ids: list[str] = []
        self.template_id = ""
        self.items: list[BatchItem] = []

        self.profile = tk.StringVar()
        self.tz_text = tk.StringVar()
        self.template_choice = tk.StringVar()
        self.recursive = tk.BooleanVar(value=False)
        self.items_summary = tk.StringVar()
        self.speed = tk.StringVar(value=f"{SPEEDS_MBPS[1]} Mbps")
        self.episode_var = tk.StringVar()
        self.publish_kind = tk.StringVar(value=SCHEDULE)
        self.pub_date = tk.StringVar()
        self.pub_time = tk.StringVar(value=default_upload_time())
        self.interval = tk.StringVar(value=DAILY)
        self.every_days = tk.IntVar(value=3)
        self.detail_open = tk.BooleanVar(value=False)
        self.template_name = tk.StringVar()
        self.title_template = tk.StringVar(value="{filename}")
        self.tags = tk.StringVar()
        self.series = tk.StringVar()
        self.thumb_strategy = tk.StringVar(value=THUMB_STRATEGY[THUMB_NONE])
        self.thumb_source = tk.StringVar()
        self.category = tk.StringVar(value=_label(DEFAULT_CATEGORIES, "10"))
        self.language = tk.StringVar(value=_label(LANGUAGES, "ko"))
        self.made_for_kids = tk.BooleanVar(value=False)
        self.privacy_now = tk.StringVar(value=_label(PRIVACY_LABELS, "private"))
        self.first_comment_on = tk.BooleanVar(value=False)
        self.first_comment_preset = tk.StringVar()
        self.progress_head = tk.StringVar()
        self.progress_text = tk.StringVar()
        self.run_ids: list[str] = []  # 이번 [▶ 예약 업로드 시작]으로 올리는 작업
        self.done_win = None
        self.beginner = is_beginner()
        self.summary = tk.StringVar()
        self.detail = tk.StringVar()
        self.usage = tk.StringVar()

        self._ui()
        self.refresh_profiles()
        if video_path:
            self.set_video(video_path, title)
        self.refresh_jobs()
        self.pub_date.trace_add("write", lambda *a: self._refresh_steps())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(300, self._pump)

    # ================= 화면 =================
    def _ui(self):
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        root = ttk.Frame(self.scroll.body, padding=12)
        root.pack(fill="both", expand=True)
        top = ttk.Frame(root); top.pack(fill="x")
        ttk.Label(top, text="③ 예약 업로드", font=("Segoe UI", 15, "bold")).pack(side="left")
        ttk.Button(top, text="YouTube 채널 관리", command=self.open_channels).pack(side="right")
        ttk.Button(top, text="💬 댓글 관리", command=self.open_comments).pack(side="right", padx=6)
        ttk.Button(top, text="? 사용법", command=lambda: show_usage(self, "upload")).pack(side="right")
        ttk.Label(root, foreground="gray30", text=(
            "채널 → 영상 → 예약 순서로 고르고 [미리보기]에서 확인한 뒤 시작하세요. 업로드 직전마다 채널을 다시 "
            "확인하고, 다르면 업로드하지 않습니다. 이 창을 닫아도 프로그램이 켜져 있으면 업로드는 계속됩니다."),
            wraplength=980).pack(anchor="w", pady=(0, 6))
        # 단계 표시 (현재 단계는 색 + '▶' 글자로 — 색만으로 구분하지 않음)
        sf = ttk.Frame(root); sf.pack(fill="x", pady=(0, 8))
        self.step_labels = []
        for i, name in enumerate(UPLOAD_STEPS, 1):
            if i > 1:
                ttk.Label(sf, text="→", foreground="gray50").pack(side="left", padx=2)
            lb = tk.Label(sf, text=f"STEP {i}\n{name}", justify="center", padx=10, pady=3, relief="groove", borderwidth=1)
            lb.pack(side="left")
            self.step_labels.append(lb)

        # ① 채널
        f1 = ttk.LabelFrame(root, text="① 채널", padding=8); f1.pack(fill="x")
        self.cb_profile = ttk.Combobox(f1, textvariable=self.profile, state="readonly", width=40)
        self.cb_profile.pack(side="left")
        self.cb_profile.bind("<<ComboboxSelected>>", lambda e: self._on_profile())
        ttk.Label(f1, textvariable=self.tz_text, foreground="gray30").pack(side="left", padx=8)
        self.cb_template = ttk.Combobox(f1, textvariable=self.template_choice, state="readonly", width=26)
        self.cb_template.pack(side="right")
        self.cb_template.bind("<<ComboboxSelected>>", lambda e: self._on_template())
        ttk.Label(f1, text="템플릿").pack(side="right", padx=(0, 4))

        # ② 영상
        f2 = ttk.LabelFrame(root, text="② 영상", padding=8); f2.pack(fill="x", pady=(8, 0))
        br = ttk.Frame(f2); br.pack(fill="x")
        ttk.Button(br, text="영상 선택", command=self.pick_videos).pack(side="left")
        ttk.Button(br, text="폴더 한꺼번에 추가", command=self.pick_folder).pack(side="left", padx=4)
        self.btn_last = ttk.Button(br, text="최근 폴더 다시 불러오기", command=self.reload_last_folder)
        self.btn_last.pack(side="left")
        ttk.Checkbutton(br, text="하위 폴더 포함 (고급)", variable=self.recursive).pack(side="left", padx=8)
        ttk.Button(br, text="전체 비우기", command=self.clear_items).pack(side="right")
        ttk.Button(br, text="선택 빼기", command=self.remove_items).pack(side="right", padx=4)
        cols = ("n", "video", "thumb", "episode", "size")
        self.items_tree = ttk.Treeview(f2, columns=cols, show="headings", height=5, selectmode="extended")
        for c, h, w in zip(cols, ("#", "영상", "썸네일", "회차", "크기"), (40, 360, 280, 70, 90)):
            self.items_tree.heading(c, text=h)
            self.items_tree.column(c, width=w, anchor="w" if c in ("video", "thumb") else "center")
        self.items_tree.pack(fill="x", pady=(4, 0))
        ir = ttk.Frame(f2); ir.pack(fill="x", pady=(4, 0))
        ttk.Button(ir, text="썸네일 직접 지정", command=self.pick_item_thumbnail).pack(side="left")
        ttk.Label(ir, text="회차").pack(side="left", padx=(12, 2))
        ttk.Entry(ir, textvariable=self.episode_var, width=6).pack(side="left")
        ttk.Button(ir, text="선택 영상에 회차 지정 (여러 개면 1씩 증가)", command=self.assign_episode).pack(side="left", padx=4)
        sr = ttk.Frame(f2); sr.pack(fill="x", pady=(4, 0))
        ttk.Label(sr, textvariable=self.items_summary).pack(side="left")
        cb = ttk.Combobox(sr, textvariable=self.speed, state="readonly", width=9, values=[f"{s} Mbps" for s in SPEEDS_MBPS])
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_items())
        ttk.Label(sr, text="참고 업로드 속도").pack(side="right", padx=(0, 4))

        # ③ 예약
        f3 = ttk.LabelFrame(root, text="③ 예약", padding=8); f3.pack(fill="x", pady=(8, 0))
        kr = ttk.Frame(f3); kr.pack(fill="x")
        ttk.Radiobutton(kr, text="예약 공개 (지정 시각에 공개)", variable=self.publish_kind, value=SCHEDULE,
                        command=self._on_kind).pack(side="left")
        InfoTip(kr, TOOLTIPS["schedule"]).pack(side="left", padx=(2, 0))
        ttk.Radiobutton(kr, text="지금 올리기 (상세 설정의 공개 상태)", variable=self.publish_kind, value=NOW,
                        command=self._on_kind).pack(side="left", padx=12)
        dr = ttk.Frame(f3); dr.pack(fill="x", pady=(6, 0))
        ttk.Label(dr, text="첫 날짜").pack(side="left")
        self.ent_date = ttk.Entry(dr, textvariable=self.pub_date, width=12)
        self.ent_date.pack(side="left", padx=(2, 2))
        self.sched_widgets = [self.ent_date]
        for text, days in (("오늘", 0), ("내일", 1)):
            b = ttk.Button(dr, text=text, width=5, command=lambda d=days: self.set_day(d))
            b.pack(side="left")
            self.sched_widgets.append(b)
        ttk.Label(dr, text="시각").pack(side="left", padx=(12, 2))
        self.ent_time = ttk.Entry(dr, textvariable=self.pub_time, width=7)
        self.ent_time.pack(side="left", padx=(0, 2))
        self.sched_widgets.append(self.ent_time)
        for t in TIME_PRESETS:
            b = ttk.Button(dr, text=t, width=6, command=lambda v=t: self.pub_time.set(v))
            b.pack(side="left")
            self.sched_widgets.append(b)
        vr = ttk.Frame(f3); vr.pack(fill="x", pady=(6, 0))
        ttk.Label(vr, text="여러 영상 간격").pack(side="left")
        for key in (DAILY, WEEKDAYS, EVERY_2_DAYS, WEEKLY, EVERY_N_DAYS):
            rb = ttk.Radiobutton(vr, text=INTERVAL_LABELS[key], variable=self.interval, value=key)
            rb.pack(side="left", padx=(8, 0))
            self.sched_widgets.append(rb)
        sp = ttk.Spinbox(vr, from_=1, to=365, width=5, textvariable=self.every_days)
        sp.pack(side="left", padx=(2, 0))
        self.sched_widgets.append(sp)
        ttk.Label(vr, text="일").pack(side="left")
        ttk.Label(f3, foreground="gray30", text="날짜·시각은 선택한 채널의 시간대 기준입니다 (한국 Asia/Seoul, 일본 Asia/Tokyo).").pack(anchor="w", pady=(4, 0))

        # 상세 설정 (기본 접힘)
        self.btn_detail = ttk.Button(root, text="▼ 제목·설명·태그·썸네일 상세 설정", command=self.toggle_detail)
        self.btn_detail.pack(fill="x", pady=(8, 0))
        self.detail_frame = df = ttk.LabelFrame(root, text="상세 설정 (채널별 템플릿)", padding=8)
        df.columnconfigure(1, weight=1)

        def row(r, text, widget):
            ttk.Label(df, text=text, width=14).grid(row=r, column=0, sticky="nw", pady=2)
            widget.grid(row=r, column=1, sticky="ew", pady=2)
        tr = ttk.Frame(df)
        ttk.Entry(tr, textvariable=self.template_name, width=30).pack(side="left")
        ttk.Button(tr, text="템플릿으로 저장", command=self.save_template).pack(side="left", padx=4)
        ttk.Button(tr, text="템플릿 삭제", command=self.delete_template).pack(side="left")
        row(0, "템플릿 이름", tr)
        row(1, "제목", ttk.Entry(df, textvariable=self.title_template))
        ttk.Label(df, foreground="gray30", text="변수: " + " ".join("{" + v + "}" for v in TEMPLATE_VARIABLES
                                                                  if v not in ("month", "day", "session"))).grid(
            row=2, column=1, sticky="w")
        self.txt_desc = tk.Text(df, height=4, wrap="word")
        row(3, "설명", self.txt_desc)
        row(4, "태그", ttk.Entry(df, textvariable=self.tags))
        row(5, "시리즈 {series}", ttk.Entry(df, textvariable=self.series))
        th = ttk.Frame(df)
        tcb = ttk.Combobox(th, textvariable=self.thumb_strategy, state="readonly", width=16,
                           values=list(THUMB_STRATEGY.values()))
        tcb.pack(side="left")
        tcb.bind("<<ComboboxSelected>>", lambda e: self._rematch())
        ttk.Entry(th, textvariable=self.thumb_source).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(th, text="찾기", command=self._pick_thumb_source).pack(side="left")
        InfoTip(th, TOOLTIPS["thumb"]).pack(side="left", padx=(4, 0))
        row(6, "썸네일 없을 때", th)  # 같은 이름 썸네일(001.mp4 ↔ 001.jpg)이 없을 때만 사용
        mr = ttk.Frame(df)
        ttk.Combobox(mr, textvariable=self.category, state="readonly", width=18,
                     values=[_label(DEFAULT_CATEGORIES, k) for k in DEFAULT_CATEGORIES]).pack(side="left")
        ttk.Label(mr, text="언어").pack(side="left", padx=(12, 2))
        ttk.Combobox(mr, textvariable=self.language, state="readonly", width=14,
                     values=[_label(LANGUAGES, k) for k in LANGUAGES]).pack(side="left")
        ttk.Checkbutton(mr, text="아동용", variable=self.made_for_kids).pack(side="left", padx=(12, 0))
        InfoTip(mr, TOOLTIPS["kids"]).pack(side="left", padx=(2, 12))
        ttk.Label(mr, text="공개 상태 (지금 올리기)").pack(side="left", padx=(12, 2))
        ttk.Combobox(mr, textvariable=self.privacy_now, state="readonly", width=14,
                     values=[_label(PRIVACY_LABELS, k) for k in PRIVACY_LABELS]).pack(side="left")
        row(7, "카테고리", mr)
        self.adv_row = (df.grid_slaves(row=7, column=0) + df.grid_slaves(row=7, column=1))
        self.btn_adv = ttk.Button(df, text="▶ 고급 설정 (카테고리·언어·아동용·공개 상태)", command=self.toggle_advanced)
        self.btn_adv.grid(row=11, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.advanced_open = False
        fc = ttk.Frame(df)
        ttk.Checkbutton(fc, text="공개 후 첫 댓글 자동등록", variable=self.first_comment_on).pack(side="left")
        ttk.Label(fc, text="댓글 템플릿").pack(side="left", padx=(16, 2))
        self.cb_first_comment = ttk.Combobox(fc, textvariable=self.first_comment_preset, state="readonly", width=46)
        self.cb_first_comment.pack(side="left")
        self.cb_first_comment.bind("<<ComboboxSelected>>", lambda e: self._use_first_comment_preset())
        row(8, "첫 댓글", fc)
        self.txt_first_comment = tk.Text(df, height=3, wrap="word")
        row(9, "", self.txt_first_comment)
        ttk.Label(df, foreground="gray30", text=(
            "예약 영상은 공개되기 전(비공개)에는 댓글을 달 수 없어, 공개된 뒤 자동으로 답니다. 프로그램이 꺼져 있었다면 "
            "다시 켰을 때 등록합니다. 변수: {title} {channel} {date} {series} {episode} {filename}"),
            wraplength=820, justify="left").grid(row=10, column=1, sticky="w")
        self._detail_anchor = ttk.Frame(root)
        self._detail_anchor.pack(fill="x")

        ttk.Button(root, text="▶ 미리보기 후 대기열에 추가", command=self.preview).pack(fill="x", pady=(8, 0))

        # ④ 대기열
        qf = ttk.LabelFrame(root, text=f"④ 예약 업로드 대기열 (최대 {MAX_JOBS}개 · 1개씩 순차 업로드)", padding=8)
        qf.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("n", "channel", "title", "publish", "state", "progress", "comment")
        self.tree = ttk.Treeview(qf, columns=cols, show="headings", height=8, selectmode="extended")
        for c, h, w in zip(cols, ("#", "채널", "제목", "공개 시각", "상태", "진행", "첫 댓글"),
                           (36, 150, 280, 160, 120, 50, 170)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("channel", "title") else "center")
        for st, color in STATE_COLORS.items():
            self.tree.tag_configure(st, foreground=color)
        self.tree.tag_configure("active", foreground=ACTIVE_COLOR)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        ttk.Label(qf, textvariable=self.detail, foreground="gray30", wraplength=960, justify="left").pack(anchor="w")
        tb = ttk.Frame(qf); tb.pack(fill="x", pady=(4, 0))
        ttk.Button(tb, text="▲", width=3, command=lambda: self._move(-1)).pack(side="left")
        ttk.Button(tb, text="▼", width=3, command=lambda: self._move(1)).pack(side="left", padx=(2, 8))
        ttk.Button(tb, text="다시 시도", command=self.retry_selected).pack(side="left")
        ttk.Button(tb, text="취소", command=self.cancel_selected).pack(side="left", padx=4)
        ttk.Button(tb, text="삭제", command=self.remove_selected).pack(side="left")
        ttk.Button(tb, text="완료만 정리", command=self.clear_done).pack(side="right")
        ttk.Button(tb, text="실패만 다시 시도", command=self.retry_failed).pack(side="right", padx=4)
        ttk.Button(tb, text="선택 삭제", command=self.remove_selected).pack(side="right")
        ttk.Button(tb, text="선택 전체", command=self.select_all).pack(side="right", padx=4)

        # 진행 상태 (크게) — 업로드 중에만 보인다
        self.progress_frame = pf = ttk.LabelFrame(root, text="업로드 진행", padding=8)
        ttk.Label(pf, textvariable=self.progress_head, font=("Segoe UI", 13, "bold")).pack(anchor="w")
        self.progress_bar = ttk.Progressbar(pf, maximum=100)
        self.progress_bar.pack(fill="x", pady=4)
        ttk.Label(pf, textvariable=self.progress_text, justify="left").pack(anchor="w")
        self._progress_anchor = ttk.Frame(root)
        self._progress_anchor.pack(fill="x")
        ar = ttk.Frame(root); ar.pack(fill="x", pady=(8, 0))
        self.btn_start = ttk.Button(ar, text="▶ 예약 업로드 시작", command=self.start)
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(ar, text="■ 중지 (나중에 이어 올리기)", command=self.stop)
        self.btn_stop.pack(side="left", padx=(5, 0))
        ttk.Label(root, textvariable=self.summary).pack(anchor="w", pady=(4, 0))
        self.lbl_usage = ttk.Label(root, textvariable=self.usage, foreground="gray40", wraplength=980)
        self.lbl_usage.pack(anchor="w")
        self._on_kind()
        self.apply_mode()

    def apply_mode(self) -> None:
        """초보자 모드: 고급 항목(카테고리·언어·아동용·공개 상태, API 사용량)을 접어 둔다."""
        self.beginner = is_beginner()
        if self.beginner:
            self.lbl_usage.pack_forget()
            show_adv = self.advanced_open
            self.btn_adv.grid()
        else:
            if not self.lbl_usage.winfo_manager():
                self.lbl_usage.pack(anchor="w")
            show_adv = True
            self.btn_adv.grid_remove()
        for w in self.adv_row:
            w.grid() if show_adv else w.grid_remove()

    def toggle_advanced(self) -> None:
        self.advanced_open = not self.advanced_open
        self.btn_adv.configure(text="▼ 고급 설정 접기" if self.advanced_open else "▶ 고급 설정 (카테고리·언어·아동용·공개 상태)")
        self.apply_mode()

    def current_step(self) -> int:
        """1 채널 → 2 영상 → 3 날짜 → 4 미리보기 → 5 예약 시작."""
        if self.selected_profile() is None:
            return 1
        if not self.items:
            return 5 if any(j.status == PENDING for j in self.q.snapshot()) else 2
        if self.publish_kind.get() == SCHEDULE and not self.pub_date.get().strip():
            return 3
        return 4

    def _refresh_steps(self) -> None:
        cur = self.current_step()
        for i, lb in enumerate(self.step_labels, 1):
            name = UPLOAD_STEPS[i - 1]
            if i == cur:
                lb.configure(text=f"▶ STEP {i}\n{name}", bg="#2f6fdf", fg="white", font=("Segoe UI", 9, "bold"))
            else:
                lb.configure(text=f"{'✓' if i < cur else ''} STEP {i}\n{name}".strip(), bg="#eef3fd" if i < cur else "#f4f4f4",
                             fg="#1d4fa8" if i < cur else "gray35", font=("Segoe UI", 9))

    def toggle_detail(self):
        if self.detail_open.get():
            self.detail_frame.pack_forget()
            self.detail_open.set(False)
            self.btn_detail.configure(text="▼ 제목·설명·태그·썸네일 상세 설정")
        else:
            self.detail_frame.pack(fill="x", pady=(4, 0), before=self._detail_anchor)
            self.detail_open.set(True)
            self.btn_detail.configure(text="▲ 상세 설정 접기")

    # ================= 채널 / 템플릿 =================
    def open_channels(self):
        if self.channel_win is not None:
            try:
                if self.channel_win.winfo_exists():
                    self.channel_win.lift()
                    return self.channel_win
            except tk.TclError:
                pass
        if self._channel_window_factory:
            self.channel_win = self._channel_window_factory(self, on_change=self.refresh_profiles)
        else:
            from .youtube_channels_ui import ChannelManagerWindow
            self.channel_win = ChannelManagerWindow(self, profiles=self.profiles, templates=self.templates,
                                                    on_change=self.refresh_profiles,
                                                    connect_guide=help_ui.ask_connect_guide)
        return self.channel_win

    def refresh_profiles(self) -> None:
        profiles = self.profiles.all()
        cur = self.selected_profile()
        self._profile_ids = [p.profile_id for p in profiles]
        self.cb_profile.configure(values=[p.label for p in profiles])
        last = load_settings().get(LAST_PROFILE_KEY, "")  # 마지막으로 쓴 채널
        keep = cur.profile_id if cur and cur.profile_id in self._profile_ids else (
            last if last in self._profile_ids else (self._profile_ids[0] if self._profile_ids else ""))
        self.profile.set(next((p.label for p in profiles if p.profile_id == keep), ""))
        if not profiles:
            self.tz_text.set("등록된 채널이 없습니다 → [YouTube 채널 관리]")
        self._on_profile(reapply=cur is None or cur.profile_id != keep)
        if not self.pub_date.get():
            self.set_day(1)

    def selected_profile(self):
        values = list(self.cb_profile.cget("values") or ())
        label = self.profile.get()
        if label in values and values.index(label) < len(self._profile_ids):
            return self.profiles.get(self._profile_ids[values.index(label)])
        return None

    def select_profile(self, profile_id: str) -> None:
        p = self.profiles.get(profile_id)
        if p:
            self.profile.set(p.label)
            self._on_profile()

    def _on_profile(self, reapply: bool = True) -> None:
        p = self.selected_profile()
        if p is None:
            self._set_template_choices(None)
            return
        self.tz_text.set(f"시간대 {p.timezone}" + ("" if p.channel_id else " · ⚠ 연결 안 됨"))
        if reapply or not self._template_ids:
            self._set_template_choices(p)
            picked = self.templates.pick_for(p)
            self.apply_template(*(picked or ("", profile_defaults_template(p))))

    def _set_template_choices(self, profile) -> None:
        own = self.templates.for_profile(profile.profile_id) if profile else []
        self._template_ids = [""] + [tid for tid, _ in own]
        self.cb_template.configure(values=["(채널 기본값)"] + [t.name for _, t in own])

    def _on_template(self) -> None:
        p = self.selected_profile()
        values = list(self.cb_template.cget("values") or ())
        i = values.index(self.template_choice.get()) if self.template_choice.get() in values else 0
        tid = self._template_ids[i] if i < len(self._template_ids) else ""
        if p is None:
            return
        got = self.templates.get(tid) if tid else None
        if got and got[0] == p.profile_id:
            self.apply_template(tid, got[1])
            self.templates.remember(p.profile_id, tid)
        else:
            self.apply_template("", profile_defaults_template(p))
            self.templates.remember(p.profile_id, "")

    def apply_template(self, template_id: str, t: MetadataTemplate) -> None:
        """폼 전체를 이 템플릿 값으로 바꾼다 (이전 채널/템플릿 값을 남기지 않음)."""
        self.template_id = template_id
        self.template_name.set(t.name)
        self.title_template.set(t.title_template or "{filename}")
        self.txt_desc.delete("1.0", "end")
        self.txt_desc.insert("1.0", t.description_template or "")
        self.tags.set(", ".join(t.tags) if isinstance(t.tags, list) else str(t.tags or ""))
        self.series.set(t.series)
        p = self.selected_profile()
        self.category.set(_label(DEFAULT_CATEGORIES, str(t.category_id)))
        self.language.set(_label(LANGUAGES, t.default_language if t.default_language else (p.language if p else "")))
        self.made_for_kids.set(bool(t.made_for_kids))
        self.privacy_now.set(_label(PRIVACY_LABELS, t.privacy_status))
        if t.thumbnail_mode == THUMB_FOLDER and t.thumbnail_folder:
            self.thumb_strategy.set(THUMB_STRATEGY[THUMB_FOLDER]); self.thumb_source.set(t.thumbnail_folder)
        elif t.thumbnail_paths:
            self.thumb_strategy.set(THUMB_STRATEGY[THUMB_FIXED]); self.thumb_source.set(t.thumbnail_paths[0])
        else:
            self.thumb_strategy.set(THUMB_STRATEGY[THUMB_NONE]); self.thumb_source.set("")
        self.first_comment_on.set(bool(t.first_comment_enabled))
        self.txt_first_comment.delete("1.0", "end")
        self.txt_first_comment.insert("1.0", t.first_comment_template or "")
        lang = t.default_language or (p.language if p else "ko")
        self.cb_first_comment.configure(values=[s.replace("\n", " / ") for s in DEFAULT_FIRST_COMMENTS.get(lang, [])])
        self.first_comment_preset.set("")
        names = list(self.cb_template.cget("values") or ())
        idx = self._template_ids.index(template_id) if template_id in self._template_ids else 0
        self.template_choice.set(names[idx] if idx < len(names) else "")
        self._rematch()

    def _use_first_comment_preset(self) -> None:
        values = list(self.cb_first_comment.cget("values") or ())
        p = self.selected_profile()
        lang = _key(LANGUAGES, self.language.get()) or (p.language if p else "ko")
        presets = DEFAULT_FIRST_COMMENTS.get(lang, [])
        v = self.first_comment_preset.get()
        if v in values and values.index(v) < len(presets):
            self.txt_first_comment.delete("1.0", "end")
            self.txt_first_comment.insert("1.0", presets[values.index(v)])
            self.first_comment_on.set(True)

    def form_template(self) -> MetadataTemplate:
        src = self.thumb_source.get().strip().strip('"')
        strategy = next((k for k, v in THUMB_STRATEGY.items() if v == self.thumb_strategy.get()), THUMB_NONE)
        return MetadataTemplate(
            name=self.template_name.get().strip() or "기본", title_template=self.title_template.get(),
            description_template=self.txt_desc.get("1.0", "end").rstrip("\n"), tags=self.tags.get(),
            thumbnail_mode=THUMB_FOLDER if strategy == THUMB_FOLDER else THUMB_FIXED,
            thumbnail_paths=[src] if strategy == THUMB_FIXED and src else [],
            thumbnail_folder=src if strategy == THUMB_FOLDER else "",
            category_id=_key(DEFAULT_CATEGORIES, self.category.get()), privacy_status=_key(PRIVACY_LABELS, self.privacy_now.get()),
            made_for_kids=bool(self.made_for_kids.get()), default_language=_key(LANGUAGES, self.language.get()),
            series=self.series.get().strip(), first_comment_enabled=bool(self.first_comment_on.get()),
            first_comment_template=self.txt_first_comment.get("1.0", "end").strip())

    def save_template(self):
        p = self.selected_profile()
        if p is None:
            messagebox.showwarning("템플릿", "채널을 먼저 선택하세요.", parent=self)
            return None
        if not self.template_name.get().strip():
            messagebox.showwarning("템플릿", "템플릿 이름을 입력하세요 (예: 한국 시니어 / 가을 샹송).", parent=self)
            return None
        try:
            tid = self.templates.save(p.profile_id, self.form_template(), self.template_id)
        except (MetadataError, ValueError) as e:
            messagebox.showerror("템플릿", str(e), parent=self)
            return None
        self.templates.remember(p.profile_id, tid)
        self._set_template_choices(p)
        got = self.templates.get(tid)
        self.apply_template(tid, got[1])
        self.summary.set(f"✓ 템플릿 '{got[1].name}' 저장 ({p.alias})")
        return tid

    def delete_template(self):
        p = self.selected_profile()
        if p is None or not self.template_id:
            return
        if messagebox.askyesno("템플릿 삭제", f"'{self.template_name.get()}' 템플릿을 삭제할까요?", parent=self):
            self.templates.delete(self.template_id)
            if p.default_template_id == self.template_id:
                p.default_template_id = ""
                self.profiles.save(p)
            self.templates.remember(p.profile_id, "")
            self._set_template_choices(p)
            self.apply_template("", profile_defaults_template(p))

    def _pick_thumb_source(self):
        if self.thumb_strategy.get() == THUMB_STRATEGY[THUMB_FOLDER]:
            p = self._pick_dir(parent=self, title="썸네일 폴더 선택")
        else:
            p = self._pick_file(parent=self, title="썸네일 선택", filetypes=[("이미지", "*.jpg *.jpeg *.png"), ("모든 파일", "*.*")])
        if p:
            self.thumb_source.set(p)
            self._rematch()

    # ================= 영상 목록 =================
    def add_videos(self, paths) -> int:
        have = {str(Path(i.video_path)).lower() for i in self.items}
        added = 0
        for raw in paths:
            p = Path(str(raw))
            if str(p).lower() in have:
                continue
            have.add(str(p).lower())
            self.items.append(BatchItem(str(p), episode=episode_from_filename(p.name)))
            added += 1
        self._rematch()
        return added

    def set_video(self, path: str, title: str = "") -> None:
        """①에서 [예약 업로드로 보내기]: 그 영상 1개로 시작 (제목은 템플릿 {filename} 등으로 만든다)."""
        self.items = []
        self.add_videos([path])

    def pick_videos(self):
        paths = self._pick_files(parent=self, title="업로드할 영상 선택", filetypes=[("MP4", "*.mp4"), ("영상", "*.mov *.m4v *.mkv"), ("모든 파일", "*.*")])
        if paths:
            self.add_videos(list(paths))

    def add_folder(self, folder) -> int:
        try:
            found = scan_folder(folder, recursive=bool(self.recursive.get()))
        except ValueError as e:
            messagebox.showerror("폴더", str(e), parent=self)
            return 0
        remember_folder(folder)
        n = self.add_videos(found)
        self.summary.set(f"폴더에서 영상 {len(found)}개 찾음 · 새로 추가 {n}개 (파일은 옮기거나 바꾸지 않습니다)")
        self._refresh_last_button()
        return n

    def pick_folder(self):
        d = self._pick_dir(parent=self, title="영상 폴더 선택", initialdir=last_folder() or None)
        if d:
            self.add_folder(d)

    def reload_last_folder(self):
        d = last_folder()
        if not d:
            messagebox.showinfo("최근 폴더", "최근에 쓴 영상 폴더가 없습니다.", parent=self)
            return 0
        return self.add_folder(d)

    def _refresh_last_button(self):
        self.btn_last.configure(state="normal" if last_folder() else "disabled")

    def _selected_items(self) -> list[int]:
        return sorted(self.items_tree.index(i) for i in self.items_tree.selection())

    def remove_items(self):
        for idx in reversed(self._selected_items()):
            self.items.pop(idx)
        self._rematch()

    def clear_items(self):
        self.items = []
        self._rematch()

    def pick_item_thumbnail(self):
        sel = self._selected_items()
        if not sel:
            messagebox.showinfo("썸네일", "영상 목록에서 영상을 선택하세요.", parent=self)
            return
        p = self._pick_file(parent=self, title="썸네일 선택", filetypes=[("이미지", "*.jpg *.jpeg *.png"), ("모든 파일", "*.*")])
        if p:
            for idx in sel:
                self.items[idx].thumb = ThumbMatch(p, "manual")
            self._refresh_items()

    def assign_episode(self):
        sel = self._selected_items()
        v = self.episode_var.get().strip()
        if not sel or not v:
            return
        for k, idx in enumerate(sel):
            self.items[idx].episode = str(int(v) + k) if v.isdigit() else v
        self._refresh_items()

    def _rematch(self):
        """같은 이름 썸네일 → 템플릿 방식 순서로 다시 맞춘다. 직접 지정한 썸네일은 그대로 둔다."""
        try:
            tpl = self.form_template()
        except tk.TclError:
            return
        auto = match_all([i.video_path for i in self.items], tpl)
        for it, m in zip(self.items, auto):
            if it.thumb.status != "manual":
                it.thumb = m
        self._refresh_items()

    def _refresh_items(self):
        for x in self.items_tree.get_children():
            self.items_tree.delete(x)
        total = 0
        for i, it in enumerate(self.items, 1):
            size = it.size
            total += size
            self.items_tree.insert("", "end", values=(i, it.name, it.thumb.label, it.episode or "-", human_size(size)))
        if not self.items:
            self.items_summary.set("영상을 선택하거나 폴더를 한꺼번에 추가하세요.")
        else:
            mbps = int(self.speed.get().split()[0]) if self.speed.get() else SPEEDS_MBPS[1]
            thumbs = sum(bool(i.thumb.path) for i in self.items)
            self.items_summary.set(f"{len(self.items)}개 · 총 {human_size(total)} · 썸네일 {thumbs}/{len(self.items)} · "
                                   f"{mbps} Mbps 기준 예상 약 {human_duration(estimate_seconds(total, mbps))} (참고용, 실제 속도에 따라 다름)")
        self._refresh_last_button()
        if hasattr(self, "step_labels") and hasattr(self, "tree"):
            self._refresh_steps()

    # ================= 예약 =================
    def _on_kind(self):
        st = "normal" if self.publish_kind.get() == SCHEDULE else "disabled"
        for w in self.sched_widgets:
            w.configure(state=st)

    def _zone_now(self):
        p = self.selected_profile()
        return datetime.fromtimestamp(self._clock(), timezone.utc).astimezone(get_zone(p.timezone if p else "Asia/Seoul"))

    def set_day(self, days_from_today: int):
        self.pub_date.set((self._zone_now().date() + timedelta(days=days_from_today)).isoformat())

    def schedule(self, count: int) -> list[datetime] | None:
        if self.publish_kind.get() != SCHEDULE:
            return None
        p = self.selected_profile()
        try:
            d = date.fromisoformat(self.pub_date.get().strip())
            h, m = self.pub_time.get().strip().split(":")
            t = dtime(int(h), int(m))
            every = int(self.every_days.get())
        except (ValueError, tk.TclError):
            raise ScheduleError("예약 날짜/시각 형식이 올바르지 않습니다 (예: 2026-10-06, 19:00).") from None
        return schedule_times(d, t, p.timezone, count, self.interval.get(), every)

    def build_plan(self) -> BatchPlan:
        p = self.selected_profile()
        if p is None:
            raise QueueError("채널을 선택하세요. 없으면 [YouTube 채널 관리]에서 등록하세요.")
        tpl = self.form_template()
        times = self.schedule(len(self.items))
        snap = self.q.snapshot()
        queued = {str(Path(j.video_path)).lower() for j in snap if j.status != CANCELLED}
        now = datetime.fromtimestamp(self._clock(), timezone.utc)
        return build_plan(p, self.items, tpl, times=times, privacy_now=_key(PRIVACY_LABELS, self.privacy_now.get()),
                          now=now, queued_paths=queued, capacity=MAX_JOBS - len(snap))

    def preview(self):
        try:
            plan = self.build_plan()
        except (QueueError, ScheduleError, MetadataError, ValueError) as e:
            messagebox.showerror("예약 업로드", str(e), parent=self)
            return None
        p = self.selected_profile()
        verify = None
        if p is not None and p.channel_id:
            q, profiles, expected = self.q, self.profiles, p.channel_id
            verify = lambda: verify_channel(q.api_factory(p, profiles), expected)  # noqa: E731
        if self.preview_win is not None:
            try:
                self.preview_win.destroy()
            except tk.TclError:
                pass
        self.preview_win = preview_ui.PreviewDialog(self, plan, on_confirm=self.enqueue, verify=verify,
                                                    on_start=self.enqueue_and_start, on_reselect=self.reselect_channel)
        return self.preview_win

    def enqueue_and_start(self, plan: BatchPlan):
        """미리보기의 [맞습니다. 예약 업로드 시작]: 대기열에 넣고 바로 시작."""
        jobs = self.enqueue(plan)
        if jobs:
            self.start()
        return jobs

    def reselect_channel(self) -> None:
        """미리보기의 [채널 다시 선택]."""
        self.scroll.canvas.yview_moveto(0.0)
        self.cb_profile.focus_set()
        try:
            self.cb_profile.event_generate("<Down>")  # 목록 열기
        except tk.TclError:
            pass

    def enqueue(self, plan: BatchPlan):
        """미리보기에서 [N개 대기열에 추가]: 모두 검증한 뒤 한꺼번에 넣는다 (하나라도 실패하면 하나도 넣지 않음)."""
        if not plan.ok:
            return None
        try:
            jobs = [self.q.make_job(profile_id=plan.profile_id, video_path=it.video_path, title=it.title,
                                    description=it.description, tags=it.tags, thumbnail_path=it.thumbnail_path,
                                    category_id=it.category_id, language=it.language, made_for_kids=it.made_for_kids,
                                    privacy=it.privacy, publish_at=it.publish_at, first_comment=it.first_comment)
                    for it in plan.items]
            if any(j.channel_id != plan.channel_id for j in jobs):
                raise QueueError("미리보기 뒤 채널 연결이 바뀌었습니다. 다시 미리보기 하세요.")
            if len(self.q.jobs) + len(jobs) > MAX_JOBS:
                raise QueueError(f"예약 업로드 대기열은 최대 {MAX_JOBS}개입니다.")
            for j in jobs:
                self.q.add(j)
        except QueueError as e:
            messagebox.showerror("예약 업로드", str(e), parent=self)
            return None
        if self.template_id:
            self.templates.remember(plan.profile_id, self.template_id)
        last = {LAST_PROFILE_KEY: plan.profile_id}
        if self.publish_kind.get() == SCHEDULE:
            last[LAST_TIME_KEY] = self.pub_time.get().strip()
        update_settings(**last)  # 다음에 같은 채널/시간으로 시작
        self.items = []
        self._refresh_items()
        self.refresh_jobs(select=jobs[0].job_id if jobs else None)
        self.summary.set(f"✓ {len(jobs)}개를 대기열에 추가했습니다. [▶ 예약 업로드 시작]을 누르세요.")
        return jobs

    # ================= 대기열 =================
    def _selected_jobs(self) -> list[str]:
        return list(self.tree.selection())

    def _act(self, fn, *args) -> None:
        sel = self._selected_jobs()
        errors = []
        for jid in sel:
            try:
                fn(jid, *args)
            except QueueError as e:
                errors.append(str(e))
        if errors:
            messagebox.showwarning("예약 업로드", "\n".join(dict.fromkeys(errors)), parent=self)
        self.refresh_jobs(select=sel[0] if sel else None)

    def retry_selected(self):
        self._act(self.q.retry)

    def cancel_selected(self):
        self._act(self.q.cancel_job)

    def remove_selected(self):
        sel = self._selected_jobs()
        if sel and messagebox.askyesno("삭제", f"선택한 {len(sel)}개를 대기열에서 지울까요? (YouTube에 올라간 영상은 지우지 않습니다)",
                                       parent=self):
            self._act(self.q.remove)

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def retry_failed(self):
        n = 0
        for j in self.q.snapshot():
            if j.status == FAILED:
                self.q.retry(j.job_id)
                n += 1
        self.refresh_jobs()
        self.summary.set(f"실패 {n}개를 다시 대기로 바꿨습니다." if n else "실패한 작업이 없습니다.")

    def _move(self, delta: int):
        sel = self._selected_jobs()
        if len(sel) == 1:
            self._act(self.q.move, delta)

    def clear_done(self):
        for j in self.q.snapshot():
            if j.status == COMPLETE:
                self.q.remove(j.job_id)
        self.refresh_jobs()

    def start(self):
        if not any(j.status == PENDING for j in self.q.snapshot()):
            messagebox.showinfo("예약 업로드", "대기 중인 작업이 없습니다. 실패/중지 작업은 [다시 시도]를 누르세요.", parent=self)
            return False
        kind = self._live_guard() if self._live_guard else ""
        if kind == "local" and not preview_ui.ask_bandwidth(self):  # Cloud LIVE는 PC 대역폭을 쓰지 않음 → 묻지 않음
            self.summary.set("이 PC에서 LIVE 송출 중이라 업로드를 시작하지 않았습니다. LIVE가 끝난 뒤 [▶ 예약 업로드 시작]을 누르세요.")
            return False
        self.run_ids = [j.job_id for j in self.q.snapshot() if j.status == PENDING]
        self._was_running = True
        self.q.start()
        self.refresh_jobs()
        return True

    def _refresh_progress(self, jobs) -> None:
        """'영상 3 / 10 · 현재: 03_가을카페.mp4 · 68% · 남은 영상 7개' (업로드 중에만 표시)."""
        run = [j for j in jobs if j.job_id in self.run_ids]
        active = next((j for j in run if j.status in ACTIVE_STATES), None)
        if not self.q.running or not run:
            if self.progress_frame.winfo_manager():
                self.progress_frame.pack_forget()
            return
        if not self.progress_frame.winfo_manager():
            self.progress_frame.pack(fill="x", pady=(8, 0), before=self._progress_anchor)
        done = sum(j.status not in (PENDING, *ACTIVE_STATES) for j in run)
        idx = done + (1 if active else 0)
        self.progress_head.set(f"영상 {max(1, idx)} / {len(run)}")
        overall = (done + (active.progress if active else 0.0)) / len(run)
        self.progress_bar["value"] = overall * 100
        if active:
            self.progress_text.set(f"현재: {Path(active.video_path).name}   {active.progress * 100:.0f}%\n"
                                   f"{friendly_status(active.status)}\n남은 영상: {len(run) - idx}개")
        else:
            self.progress_text.set(f"다음 영상을 준비하고 있습니다.\n남은 영상: {len(run) - done}개")

    def _maybe_done(self, jobs) -> None:
        """이번 실행이 끝나면 '✓ 예약 업로드가 완료되었습니다' + 다음 행동 버튼."""
        if not self.run_ids or self.q.running or not getattr(self, "_was_running", False):
            return
        self._was_running = False
        run = [j for j in jobs if j.job_id in self.run_ids]
        if not run or self.q.cancel.is_set():  # 사용자가 [■ 중지] → 완료 창 대신 요약만
            self.run_ids = []
            return
        ok = sum(j.status == COMPLETE for j in run)
        aliases = ", ".join(dict.fromkeys(j.profile_alias for j in run))
        self.done_win = help_ui.show_done(self, count=ok, failed=len(run) - ok, alias=aliases,
                                          on_list=lambda: self.scroll.scroll_to(self.tree))
        self.run_ids = []

    def stop(self):
        self.q.cancel.set()  # 현재 조각 전송 뒤 멈춘다 (UI를 막지 않도록 join하지 않음)
        self.summary.set("중지 중… 다시 시작하면 받은 위치부터 이어서 올립니다.")

    def refresh_jobs(self, select: str | None = None) -> None:
        keep = [select] if select else self._selected_jobs()
        jobs = self.q.snapshot()
        for x in self.tree.get_children():
            self.tree.delete(x)
        tasks = {t.task_id: t for t in self._comment_store().tasks()}
        for i, j in enumerate(jobs, 1):
            tag = "active" if j.status in ACTIVE_STATES else j.status
            self.tree.insert("", "end", iid=j.job_id, tags=(tag,), values=(
                i, j.profile_alias, j.title, j.publish_local_text(), j.status_label, f"{j.progress * 100:.0f}%",
                self.first_comment_label(j, tasks.get(j.job_id))))
        keep = [k for k in keep if k and self.tree.exists(k)]
        if keep:
            self.tree.selection_set(keep)
        c = self.q.counts()
        running = self.q.running
        self.summary.set(f"대기 {c['waiting']} · 예약 완료 {c['done']} · 확인 필요 {c['attention']} · 전체 {c['total']}/{MAX_JOBS}"
                         + (" · 업로드 중" if running else ""))
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.usage.set(usage_text(self._clock))
        self._refresh_progress(jobs)
        self._maybe_done(jobs)
        self._refresh_steps()
        self._on_select()

    def _on_select(self) -> None:
        sel = self._selected_jobs()
        j = next((x for x in self.q.snapshot() if sel and x.job_id == sel[0]), None)
        if j is None:
            self.detail.set("")
            return
        parts = [f"{j.profile_alias} · {Path(j.video_path).name}"]
        if j.status == COMPLETE and j.publish_at_utc:
            parts.append(f"영상 예약 완료 · {j.publish_local_text()} 공개 예정")
        if j.video_id and not self.beginner:
            parts.append(f"YouTube video ID {j.video_id}")
        if j.first_comment:
            parts.append("첫 댓글: " + self.first_comment_label(j, self._comment_store().task(j.job_id)))
        if j.error:
            if self.beginner:  # 기술 내용 대신 '문제 → 해결'
                fe = friendly_error(message=j.error)
                parts.append(f"문제: {fe.problem} 해결: {fe.action}")
            else:
                parts.append(j.error)
        self.detail.set(" · ".join(parts))

    def _comment_store(self) -> CommentStore:
        return self.comments.store if self.comments is not None else CommentStore()

    @staticmethod
    def first_comment_label(job, task) -> str:
        """'댓글 완료'를 미리 표시하지 않는다 — 실제로 등록된 뒤에만 '등록 완료'."""
        if not job.first_comment:
            return "-"
        if task is not None:
            return task.label
        if job.status in (COMPLETE, PARTIAL):
            return TASK_LABELS[WAITING_PUBLIC] if job.publish_at_utc else (
                TASK_LABELS[WAITING_PRIVACY_CHANGE] if job.privacy == "private" else "업로드 후 등록 준비")
        return "업로드 후 " + ("공개되면 자동등록" if job.publish_at_utc else "자동등록")

    def open_comments(self):
        if self.comment_win is not None:
            try:
                if self.comment_win.winfo_exists():
                    self.comment_win.lift()
                    return self.comment_win
            except tk.TclError:
                pass
        from .youtube_comments import CommentService
        from .youtube_comments_ui import CommentManagerWindow
        if self.comments is None:
            self.comments = CommentService(self.profiles, api_factory=self.q.api_factory, jobs=self.q.snapshot,
                                           clock=self._clock)
        p = self.selected_profile()
        self.comment_win = CommentManagerWindow(self, service=self.comments, profile_id=p.profile_id if p else "",
                                                open_channels=self.open_channels)
        return self.comment_win

    def _pump(self) -> None:
        if getattr(self, "_destroyed", False):
            return
        changed = False
        try:
            while True:
                self.q.events.get_nowait()
                changed = True
        except Exception:
            pass
        try:
            if changed or self.q.running != (str(self.btn_stop.cget("state")) == "normal"):
                self.refresh_jobs()
        except tk.TclError:
            return
        if self.winfo_exists():
            self.after(300, self._pump)

    @property
    def uploading(self) -> bool:
        return self.q.running or any(j.status in ACTIVE_STATES for j in self.q.snapshot())

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        for w in (self.channel_win, self.preview_win, self.comment_win):
            if w is not None:
                try:
                    w.destroy()
                except tk.TclError:
                    pass
        super().destroy()
        release_tk_variables(self)
