"""④ 내 영상으로 배경 만들기 — 독립 창 (Tkinter).

흐름: 영상·사진 불러오기 → (자동) 썸네일·길이·해상도·비율·FPS + 반복/중복/품질 분석 → 화면 맞춤(16:9) ·
디졸브·길이·해상도 선택 → 제작 시작. 사진은 표시 시간만큼 정지 화면(선택 시 아주 느린 움직임)으로 영상화한다.
모든 FFmpeg 작업은 백그라운드 스레드에서 실행하고, 화면 갱신은 이벤트 큐로 Tk main thread에서만 한다.
제작 FFmpeg는 기존 ① 영상 늘리기 / LIVE와 같은 FFMPEG_GUARD를 써서 동시에 돌지 않는다.
"""
from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from dataclasses import replace

from . import bg_analysis as A
from . import bg_fit as F
from . import media_audio as MA
from .bg_core import (
    CODEC_H264, CODEC_HEVC, DEFAULT_DISSOLVE, DEFAULT_PROJECT_FPS, DISSOLVE_CHOICES, ENCODE_PROFILES, GUARD_OWNER,
    PREVIEW_CONTEXT, PROJECT_FPS_CHOICES, QUALITY_4K, QUALITY_FHD, QUALITY_HD, TARGET_MINUTES, BgPlan, Clip,
    build_bg_plan, choose_output_spec, cleanup_stale_temp, crop_preview_png, default_bg_output_name,
    estimate_build_seconds, fit_preview_png, format_precise, get_profile, hevc_available, make_seam_preview,
    measure_bitrate, measurement_pieces, parse_length, run_background_build, with_measured_rate,
)
from .core import BuildCancelled, enough_disk_space, ensure_unique_output, format_duration, free_bytes, probe_video
from .settings import load_settings, update_settings
from .tooling import FFMPEG_GUARD, prevent_windows_sleep, release_tk_variables
from .ui_scroll import ScrollFrame

TITLE = "④ 내 영상으로 배경 만들기"
SETTINGS_KEY = "bg_maker"
THUMB_W, THUMB_H = 112, 63
STEPS = ("영상·사진 불러오기", "자동 분석", "순서·구간 확인", "경계·전환 미리보기", "길이·해상도·화질",
         "용량·공간 확인", "영상 제작", "완성 파일 재생")
CIRCLED = "①②③④⑤⑥⑦⑧"
OPTIONAL_STEPS = {2, 3, 4}  # 0부터: ③④⑤는 기본값 그대로 넘어가도 된다


def _busy_text(owner):
    from .live_supervisor import busy_message
    return busy_message(owner)


def _dissolve_label(v: float) -> str:
    return f"{v:g}초"


CUSTOM = 0  # self.minutes 값: 음악 길이 직접 입력


def _fps_label(v: float) -> str:
    return f"{v:g}"


def _still_label(v: float) -> str:
    return f"{v:g}초"


MEDIA_TYPES = [("영상·사진", "*.mp4 *.mov *.mkv *.m4v *.jpg *.jpeg *.png *.webp"), ("영상", "*.mp4 *.mov *.mkv *.m4v"),
               ("사진", "*.jpg *.jpeg *.png *.webp"), ("모든 파일", "*.*")]
FIT_PREVIEW_W, FIT_PREVIEW_H = 384, 216


def _time_text(seconds: float) -> str:
    m = int(round(seconds / 60))
    return f"약 {m // 60}시간 {m % 60}분" if m >= 60 else f"약 {max(1, m)}분"


class BackgroundMakerWindow(tk.Toplevel):
    def __init__(self, master, *, tools, send_to_long=None):
        super().__init__(master)
        self.title(TITLE)
        sh = self.winfo_screenheight()
        self.geometry(f"1180x{max(560, min(940, sh - 90))}")
        self.minsize(900, 520)
        self.tools = tools                    # () -> (ffmpeg, ffprobe) | (None, None)
        self.send_to_long_cb = send_to_long   # (path) -> None
        self.clips: list[Clip] = []
        self.analyses: dict[Path, A.ClipAnalysis] = {}
        self.analysis_errors: dict[Path, str] = {}
        self.advice: dict[Path, A.LoopAdvice] = {}
        self.thumbs: dict[Path, tk.PhotoImage] = {}
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.stop_loading = threading.Event()
        self.loading = 0
        self.running = False
        self.last_output: Path | None = None
        self.plan: BgPlan | None = None
        self._hevc: bool | None = None
        self._threads: list[threading.Thread] = []
        self.range_eval: dict[Path, A.LoopAdvice] = {}   # 사용자가 구간을 바꾼 클립: 그 구간 그대로의 평가
        self._advice_cache: dict[tuple, A.LoopAdvice] = {}
        self.previewing = False
        self.preview_cancel = threading.Event()
        self.preview_files: list[Path] = []
        self.audio_checks: dict[Path, MA.AudioCheck] = {}   # 원본 오디오 검사 (④ 결과에는 항상 제외)
        self.silent_running = False
        self.silent_cancel = threading.Event()
        self.last_silent = None
        self.always_exclude = tk.BooleanVar(value=True)
        self.last_preview: Path | None = None

        saved = load_settings().get(SETTINGS_KEY)
        saved = saved if isinstance(saved, dict) else {}
        try:
            d0 = float(saved.get("dissolve", DEFAULT_DISSOLVE))
            m0 = int(saved.get("minutes", 10))
        except (TypeError, ValueError):
            d0, m0 = DEFAULT_DISSOLVE, 10
        self.dissolve = tk.StringVar(value=_dissolve_label(d0 if d0 in DISSOLVE_CHOICES else DEFAULT_DISSOLVE))
        self.minutes = tk.IntVar(value=m0 if m0 in TARGET_MINUTES or m0 == CUSTOM else 10)  # 0 = 음악 길이 직접 입력
        self.custom_length = tk.StringVar(value=str(saved.get("custom_length") or "00:50:07"))
        try:
            pf = float(saved.get("project_fps", DEFAULT_PROJECT_FPS))
        except (TypeError, ValueError):
            pf = DEFAULT_PROJECT_FPS
        self.project_fps = tk.StringVar(value=_fps_label(pf if pf in PROJECT_FPS_CHOICES else DEFAULT_PROJECT_FPS))
        self.length_check = tk.StringVar()
        self.measured_speed: dict[tuple, float] = {}   # 계획 서명 → 실측 인코딩 속도 (영상 초 / 실제 초)
        self._length_job = None
        self.quality = tk.StringVar(value=saved.get("quality") if saved.get("quality") in (QUALITY_HD, QUALITY_FHD, QUALITY_4K)
                                    else QUALITY_FHD)
        # 화면 맞춤 (통합 07) — 새 설정 키는 없으면 기본값 (기존 설정과 호환)
        self.fit = tk.StringVar(value=saved.get("fit") if saved.get("fit") in F.FIT_CHOICES else F.DEFAULT_FIT)
        try:
            b0 = int(saved.get("blur", F.DEFAULT_BLUR))
        except (TypeError, ValueError):
            b0 = F.DEFAULT_BLUR
        self.blur = tk.StringVar(value=F.BLUR_LABELS[b0 if b0 in F.BLUR_LEVELS else F.DEFAULT_BLUR])
        pad0 = saved.get("pad") if saved.get("pad") in F.PAD_COLORS.values() else F.DEFAULT_PAD
        self.pad = tk.StringVar(value=next(k for k, v in F.PAD_COLORS.items() if v == pad0))
        self.still_default = tk.StringVar(value=_still_label(F.clamp_still(saved.get("still_seconds", F.DEFAULT_STILL))))
        m0_ = saved.get("motion") if saved.get("motion") in F.MOTION_CHOICES else F.DEFAULT_MOTION
        self.motion_default = tk.StringVar(value=F.MOTION_LABELS[m0_])
        self.still_sel = tk.StringVar()
        self.motion_sel = tk.StringVar()
        self.show_advanced = tk.BooleanVar(value=False)
        self.fit_note = tk.StringVar()
        self.fit_previewing = False
        self.fit_preview_imgs: list[tk.PhotoImage] = []
        self.profile = tk.StringVar(value=get_profile(saved.get("profile")).label)
        self.codec = tk.StringVar(value=CODEC_HEVC if saved.get("codec") == CODEC_HEVC else CODEC_H264)
        self.profile_note = tk.StringVar()
        self.size_text = tk.StringVar()
        self.measured: dict[tuple, float] = {}   # 계획 서명 → 실측 바이트/초
        self.measuring = False
        self.measure_cancel = threading.Event()
        self.previewed = False
        self.played = False
        self.transitions: dict[Path, A.TransitionAdvice] = {}
        self.outdir = tk.StringVar(value=str(saved.get("outdir", "")))
        self.outname = tk.StringVar()
        self.summary = tk.StringVar(value="영상·사진을 불러오세요 (영상 30초~2분 길이 권장, 세로 숏츠·사진도 가능).")
        self.plan_text = tk.StringVar()
        self.warn_text = tk.StringVar()
        self.detail = tk.StringVar(value="목록에서 영상을 선택하면 분석 결과가 보입니다.")
        self.status = tk.StringVar(value="대기 중")
        self.range_start = tk.StringVar()
        self.range_end = tk.StringVar()
        self._name_auto = True

        self._ui()
        self._refresh_buttons()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._pump)

    # ---------------- 화면 ----------------
    def _ui(self):
        st = ttk.Style(self)
        st.configure("BG.Treeview", rowheight=THUMB_H + 8)
        st.configure("StepNow.TLabel", foreground="#ffffff", background="#2f6fdf", padding=(6, 2))
        st.configure("StepDone.TLabel", foreground="#1d7a3a", padding=(6, 2))
        st.configure("StepTodo.TLabel", foreground="#777777", padding=(6, 2))
        # 제작 버튼·진행률은 항상 창 아래에 보이게 고정하고, 나머지는 작은 화면에서 세로 스크롤
        bottom = ttk.Frame(self, padding=(10, 4, 10, 8))
        bottom.pack(side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        root = ttk.Frame(self.scroll.body, padding=10)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text=TITLE, font="PLS.Title").pack(anchor="w")
        ttk.Label(root, text="직접 준비한 영상·사진 여러 개(세로 숏츠·가로·정사각형·4:3·21:9·JPG·PNG 모두 가능)를 16:9 화면에 "
                             "맞추고 자연스러운 디졸브로 이어 3~60분 또는 음악 전체 길이(직접 입력, 최대 12시간)의 배경 영상을 "
                             "만듭니다. 원본 오디오는 넣지 않습니다 (음악은 나중에 따로). 기본값 그대로 [⑦ 제작 시작]만 눌러도 됩니다.",
                  style="Hint.TLabel", wraplength=1100, justify="left").pack(anchor="w", pady=(0, 4))
        sb = ttk.Frame(root); sb.pack(fill="x", pady=(0, 6))
        self.step_labels: list[ttk.Label] = []
        half = (len(STEPS) + 1) // 2  # 한 줄에 다 넣으면 기본 창 폭에서 ⑦⑧이 잘려 두 줄로 나눈다
        for k, name in enumerate(STEPS):
            row, col = divmod(k, half)
            if col:
                ttk.Label(sb, text="→", style="Hint.TLabel").grid(row=row, column=col * 2 - 1)
            lb = ttk.Label(sb, text=f"{CIRCLED[k]} {name}", style="StepTodo.TLabel")
            lb.grid(row=row, column=col * 2, sticky="w", pady=1)
            self.step_labels.append(lb)
        self.step_hint = tk.StringVar()
        ttk.Label(root, textvariable=self.step_hint, font="PLS.Strong").pack(anchor="w", pady=(0, 4))

        f1 = ttk.LabelFrame(root, text="①② 영상 불러오기 · 자동 분석   ③ 순서 · 사용/제외", padding=6)
        f1.pack(fill="both", expand=True)
        br = ttk.Frame(f1); br.pack(fill="x", pady=(0, 4))
        ttk.Button(br, text="① ＋ 영상·사진 불러오기", command=self._pick_files, style="Primary.TButton").pack(side="left")
        ttk.Button(br, text="▲ 위로", command=lambda: self._move(-1)).pack(side="left", padx=(8, 2))
        ttk.Button(br, text="▼ 아래로", command=lambda: self._move(1)).pack(side="left")
        self.btn_order = ttk.Button(br, text="자동 순서 추천", command=self._auto_order)
        self.btn_order.pack(side="left", padx=(8, 2))
        self.btn_toggle = ttk.Button(br, text="사용/제외 전환", command=self._toggle_enabled)
        self.btn_toggle.pack(side="left", padx=(8, 2))
        ttk.Button(br, text="목록에서 빼기", command=self._remove).pack(side="left", padx=2)
        ttk.Button(br, text="↻ 다시 분석", command=self._reanalyze).pack(side="right")
        ttk.Button(br, text="추천 구간 모두 적용", command=self._apply_all_advice).pack(side="right", padx=4)

        tf = ttk.Frame(f1); tf.pack(fill="both", expand=True)
        cols = ("use", "name", "dur", "res", "fps", "range", "loop", "next", "quality", "audio")
        self.tree = ttk.Treeview(tf, columns=cols, show="tree headings", height=4, style="BG.Treeview",
                                 selectmode="browse")
        self.tree.heading("#0", text="미리보기")
        self.tree.column("#0", width=THUMB_W + 26, stretch=False)
        for c, h, w in zip(cols, ("사용", "파일명", "길이", "해상도·비율", "FPS", "사용 구간", "반복 평가", "→ 다음 전환",
                                  "품질·중복 (참고)", "원본 오디오"),
                           (44, 160, 64, 130, 50, 100, 104, 96, 140, 100)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("name", "quality", "loop", "next") else "center")
        vs = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: (self._show_detail(), self._refresh_buttons()))
        ttk.Label(f1, textvariable=self.summary).pack(anchor="w", pady=(4, 0))
        ar0 = ttk.Frame(f1); ar0.pack(fill="x", pady=(2, 0))
        ttk.Checkbutton(ar0, text=MA.EXCLUDE_OPTION, variable=self.always_exclude, state="disabled").pack(side="left")
        ttk.Label(ar0, text="④ 배경 영상은 항상 영상만 사용합니다 (원본 자연음·말소리·음악은 결과에 들어가지 않음). "
                            "원본 파일은 그대로입니다.", style="Hint.TLabel").pack(side="left", padx=(6, 0))

        f2 = ttk.LabelFrame(root, text="③ 사용 구간   ④ 반복 경계·전환 미리보기 (선택한 영상) — 분석은 휴리스틱 참고 지표이며 "
                                       "AI 인물·로고 인식은 하지 않습니다", padding=6)
        f2.pack(fill="x", pady=(6, 0))
        ttk.Label(f2, textvariable=self.detail, wraplength=1100, justify="left").pack(anchor="w")
        rr = ttk.Frame(f2); rr.pack(fill="x", pady=(4, 0))
        ttk.Label(rr, text="사용 구간  시작(초)").pack(side="left")
        ttk.Entry(rr, textvariable=self.range_start, width=8).pack(side="left", padx=(4, 8))
        ttk.Label(rr, text="끝(초)").pack(side="left")
        ttk.Entry(rr, textvariable=self.range_end, width=8).pack(side="left", padx=(4, 8))
        ttk.Button(rr, text="구간 적용", command=self._apply_range).pack(side="left")
        ttk.Button(rr, text="추천 구간 적용", command=self._apply_advice).pack(side="left", padx=4)
        ttk.Button(rr, text="전체 사용", command=self._full_range).pack(side="left")
        # 사진 전용 (선택한 항목이 사진일 때만 사용 가능)
        ir = ttk.Frame(f2); ir.pack(fill="x", pady=(4, 0))
        ttk.Label(ir, text="🖼 선택한 사진  표시 시간").pack(side="left")
        self.cb_still_sel = ttk.Combobox(ir, textvariable=self.still_sel, width=7,
                                         values=[_still_label(v) for v in F.STILL_CHOICES])
        self.cb_still_sel.pack(side="left", padx=(4, 2))
        self.cb_still_sel.bind("<<ComboboxSelected>>", lambda e: self._apply_still())
        self.cb_still_sel.bind("<Return>", lambda e: self._apply_still())
        ttk.Label(ir, text="움직임").pack(side="left", padx=(8, 2))
        self.cb_motion_sel = ttk.Combobox(ir, textvariable=self.motion_sel, state="readonly", width=18,
                                          values=[F.MOTION_LABELS[m] for m in F.MOTION_CHOICES])
        self.cb_motion_sel.pack(side="left")
        self.cb_motion_sel.bind("<<ComboboxSelected>>", lambda e: self._apply_still())
        self.btn_still_apply = ttk.Button(ir, text="적용", command=self._apply_still)
        self.btn_still_apply.pack(side="left", padx=(6, 2))
        self.btn_still_all = ttk.Button(ir, text="모든 사진에 적용", command=lambda: self._apply_still(all_images=True))
        self.btn_still_all.pack(side="left")
        ttk.Label(ir, text="직접 입력 3~600초 · 사진은 정지 이미지 (촬영 영상 아님)",
                  style="Hint.TLabel").pack(side="left", padx=(8, 0))
        pr = ttk.Frame(f2); pr.pack(fill="x", pady=(4, 0))
        self.btn_preview_loop = ttk.Button(pr, text="▶ 반복 경계 미리보기 (끝 → 처음)",
                                           command=lambda: self.preview_seam("self"))
        self.btn_preview_loop.pack(side="left")
        self.btn_preview_next = ttk.Button(pr, text="▶ 다음 장면 전환 미리보기",
                                           command=lambda: self.preview_seam("next"))
        self.btn_preview_next.pack(side="left", padx=4)
        self.btn_silent = ttk.Button(pr, text="무음 MP4 따로 저장", command=self.save_silent_copy)
        self.btn_silent.pack(side="right")
        self.btn_recheck_audio = ttk.Button(pr, text="오디오 다시 검사", command=self.recheck_audio)
        self.btn_recheck_audio.pack(side="right", padx=4)
        ttk.Label(pr, text=f"경계 앞뒤 약 {PREVIEW_CONTEXT:g}초를 저화질로 빠르게 만들어 재생합니다 (현재 사용 구간·디졸브 기준).",
                  style="Hint.TLabel").pack(side="left", padx=(8, 0))

        ff = ttk.LabelFrame(root, text="화면 맞춤 — 출력 비율 16:9 (유튜브 플레이리스트 기본) · 원본을 찌그러뜨리지 않습니다",
                            padding=6)
        ff.pack(fill="x", pady=(6, 0))
        fr = self.fit_row = ttk.Frame(ff); fr.pack(fill="x")
        ttk.Label(fr, text="16:9가 아닌 영상·사진").pack(side="left")
        for k in F.FIT_CHOICES:
            ttk.Radiobutton(fr, text=F.FIT_LABELS[k], value=k, variable=self.fit,
                            command=self._options_changed).pack(side="left", padx=2)
        self.btn_fit_preview = ttk.Button(fr, text="▶ 화면 맞춤 미리보기", command=self.preview_fit)
        self.btn_fit_preview.pack(side="right")
        ttk.Checkbutton(fr, text="고급 옵션", variable=self.show_advanced,
                        command=self._toggle_advanced).pack(side="right", padx=8)
        self.adv = ttk.Frame(ff)  # 고급 옵션: 기본은 숨김 (초보자는 기본값 그대로)
        ttk.Label(self.adv, text="블러 강도").pack(side="left")
        cbb = ttk.Combobox(self.adv, textvariable=self.blur, state="readonly", width=10,
                           values=[F.BLUR_LABELS[b] for b in F.BLUR_LEVELS])
        cbb.pack(side="left", padx=(4, 10))
        cbb.bind("<<ComboboxSelected>>", lambda e: self._options_changed())
        ttk.Label(self.adv, text="여백 색 (원본 전체)").pack(side="left")
        cbp = ttk.Combobox(self.adv, textvariable=self.pad, state="readonly", width=10, values=list(F.PAD_COLORS))
        cbp.pack(side="left", padx=(4, 10))
        cbp.bind("<<ComboboxSelected>>", lambda e: self._options_changed())
        ttk.Label(self.adv, text="새로 불러오는 사진: 표시 시간").pack(side="left")
        cbs = ttk.Combobox(self.adv, textvariable=self.still_default, width=7,
                           values=[_still_label(v) for v in F.STILL_CHOICES])
        cbs.pack(side="left", padx=(4, 6))
        cbs.bind("<<ComboboxSelected>>", lambda e: self._save_settings())
        cbs.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Label(self.adv, text="움직임").pack(side="left")
        cbm = ttk.Combobox(self.adv, textvariable=self.motion_default, state="readonly", width=18,
                           values=[F.MOTION_LABELS[m] for m in F.MOTION_CHOICES])
        cbm.pack(side="left", padx=(4, 0))
        cbm.bind("<<ComboboxSelected>>", lambda e: self._save_settings())
        self.fit_note_label = ttk.Label(ff, textvariable=self.fit_note, style="Hint.TLabel", wraplength=1100,
                                        justify="left")
        self.fit_note_label.pack(anchor="w", pady=(2, 0))
        pv = ttk.Frame(ff); pv.pack(fill="x", pady=(2, 0))
        self.fit_img_result = ttk.Label(pv, compound="top")
        self.fit_img_result.pack(side="left")
        self.fit_img_source = ttk.Label(pv, compound="top")
        self.fit_img_source.pack(side="left", padx=(8, 0))

        f3 = ttk.LabelFrame(root, text="⑤ 출력 길이 · 해상도 · 화질   ⑥ 예상 용량 · 디스크 공간", padding=6)
        f3.pack(fill="x", pady=(6, 0))
        o1 = ttk.Frame(f3); o1.pack(fill="x")
        ttk.Label(o1, text="출력 길이").pack(side="left")
        for m in TARGET_MINUTES:
            ttk.Radiobutton(o1, text=f"{m}분", value=m, variable=self.minutes,
                            command=self._options_changed).pack(side="left", padx=2)
        ttk.Radiobutton(o1, text="음악 길이 직접 입력", value=CUSTOM, variable=self.minutes,
                        command=self._options_changed).pack(side="left", padx=2)
        ttk.Label(o1, text="   해상도").pack(side="left", padx=(12, 0))
        ttk.Radiobutton(o1, text="720p", value=QUALITY_HD, variable=self.quality,
                        command=self._options_changed).pack(side="left", padx=2)
        ttk.Radiobutton(o1, text="FHD (기본)", value=QUALITY_FHD, variable=self.quality,
                        command=self._options_changed).pack(side="left", padx=2)
        ttk.Radiobutton(o1, text="4K (원본 4K만)", value=QUALITY_4K, variable=self.quality,
                        command=self._options_changed).pack(side="left", padx=2)
        ttk.Label(o1, text="   장면 전환(디졸브)").pack(side="left", padx=(12, 0))
        cb = ttk.Combobox(o1, textvariable=self.dissolve, state="readonly", width=6,
                          values=[_dissolve_label(v) for v in DISSOLVE_CHOICES])
        cb.pack(side="left", padx=(4, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self._options_changed())
        oc = ttk.Frame(f3); oc.pack(fill="x", pady=(4, 0))
        ttk.Label(oc, text="음악 전체 길이").pack(side="left")
        self.ent_length = ttk.Entry(oc, textvariable=self.custom_length, width=14)
        self.ent_length.pack(side="left", padx=(4, 4))
        self.ent_length.bind("<FocusIn>", lambda e: self._use_custom())
        ttk.Label(oc, text="CapCut 프로젝트 FPS (:FF 표기용)").pack(side="left", padx=(8, 2))
        fcb = ttk.Combobox(oc, textvariable=self.project_fps, state="readonly", width=7,
                           values=[_fps_label(v) for v in PROJECT_FPS_CHOICES])
        fcb.pack(side="left")
        fcb.bind("<<ComboboxSelected>>", lambda e: self._length_changed())
        ttk.Label(oc, textvariable=self.length_check, font="PLS.Strong").pack(side="left", padx=(8, 0))
        ttk.Label(f3, text="예: 00:50:07 · 00:50:07.250 (소수 초) · 00:50:07:15 (CapCut 프레임 표기). 최대 12:00:00. "
                           "결과는 원본 오디오 없는 자연풍경 MP4이며, 목표보다 짧지 않게 1프레임 안으로 맞춥니다 "
                           "(음원은 CapCut에서 따로 넣으세요).", style="Hint.TLabel", wraplength=1100,
                  justify="left").pack(anchor="w")
        self.custom_length.trace_add("write", lambda *a: self._length_changed(typed=True))
        o3 = ttk.Frame(f3); o3.pack(fill="x", pady=(4, 0))
        ttk.Label(o3, text="화질·용량").pack(side="left")
        self.profile_buttons = []
        for p in ENCODE_PROFILES:
            b = ttk.Radiobutton(o3, text=p.short or p.label, value=p.label, variable=self.profile,
                                command=self._options_changed)
            b.pack(side="left", padx=2)
            self.profile_buttons.append(b)
        ttk.Label(o3, text="   코덱").pack(side="left", padx=(12, 0))
        ttk.Radiobutton(o3, text="H.264 (기본 · LIVE·업로드 호환)", value=CODEC_H264, variable=self.codec,
                        command=self._options_changed).pack(side="left", padx=2)
        self.rb_hevc = ttk.Radiobutton(o3, text="H.265 (저장·업로드용 · 더 작음 · LIVE 송출 불가)", value=CODEC_HEVC,
                                       variable=self.codec, command=self._options_changed)
        self.rb_hevc.pack(side="left", padx=2)
        ttk.Label(f3, textvariable=self.profile_note, style="Hint.TLabel", wraplength=1100,
                  justify="left").pack(anchor="w", pady=(2, 0))
        o2 = ttk.Frame(f3); o2.pack(fill="x", pady=(4, 0))
        ttk.Label(o2, text="저장 폴더", width=9).pack(side="left")
        ttk.Entry(o2, textvariable=self.outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(o2, text="변경", command=self._pick_outdir).pack(side="left", padx=4)
        ttk.Label(o2, text="파일명").pack(side="left", padx=(8, 2))
        e = ttk.Entry(o2, textvariable=self.outname, width=34)
        e.pack(side="left")
        e.bind("<Key>", lambda ev: setattr(self, "_name_auto", False))
        ttk.Label(f3, textvariable=self.plan_text, wraplength=1100, justify="left").pack(anchor="w", pady=(4, 0))
        o4 = ttk.Frame(f3); o4.pack(fill="x", pady=(2, 0))
        self.btn_measure = ttk.Button(o4, text="⑥ 실제 용량 측정 (약 10~40초)", command=self.measure_size)
        self.btn_measure.pack(side="left")
        ttk.Label(o4, textvariable=self.size_text, font="PLS.Strong", wraplength=900,
                  justify="left").pack(side="left", padx=(8, 0), fill="x", expand=True)
        self.warn_label = ttk.Label(f3, textvariable=self.warn_text, foreground="#9a5b00", wraplength=1100, justify="left")
        self.warn_label.pack(anchor="w", pady=(4, 0))

        ar = ttk.Frame(bottom); ar.pack(fill="x")
        self.btn_start = ttk.Button(ar, text="⑦ ▶ 제작 시작", command=self.start, style="Primary.TButton")
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(ar, text="■ 중단", command=self.stop, state="disabled", style="Danger.TButton")
        self.btn_stop.pack(side="left", padx=(6, 0))
        self.bar = ttk.Progressbar(bottom, maximum=100)
        self.bar.pack(fill="x", pady=(6, 0))
        ttk.Label(bottom, textvariable=self.status).pack(anchor="w")
        rr2 = ttk.Frame(bottom); rr2.pack(fill="x", pady=(4, 0))
        self.btn_open = ttk.Button(rr2, text="⑧ ▶ 완성 영상 재생", command=self._open_output, state="disabled")
        self.btn_open.pack(side="left")
        self.btn_folder = ttk.Button(rr2, text="폴더 열기", command=self._open_folder, state="disabled")
        self.btn_folder.pack(side="left", padx=4)
        self.btn_send = ttk.Button(rr2, text="① 영상 늘리기로 보내기", command=self._send_to_long, state="disabled")
        self.btn_send.pack(side="left", padx=4)

    def current_step(self) -> int:
        """지금 할 일 (0부터). ③④⑤는 기본값으로 넘어가도 되므로 '현재'로 멈추지 않는다."""
        if not self.clips:
            return 0
        if self.loading or any(c.path not in self.analyses and c.path not in self.analysis_errors for c in self.clips):
            return 1
        if self.running:
            return 6
        if self.last_output is not None and self.last_output.exists():
            return 7
        return 5

    def _update_steps(self):
        cur = self.current_step()
        done = set(range(cur))
        done.discard(3)  # ④ 미리보기는 실제로 해 본 경우에만 ✓ (기본값으로 넘어갈 수 있는 선택 단계)
        if self.previewed:
            done.add(3)
        if self.played:
            done.add(7)
        hints = {
            0: "① [＋ 영상·사진 불러오기]로 배경에 쓸 영상·사진을 고르세요 (세로 숏츠·정사각형·JPG·PNG도 16:9로 자동 맞춤).",
            1: "② 자동 분석 중입니다. 끝나면 반복 평가·다음 전환·품질이 목록에 표시됩니다.",
            5: "③④⑤는 필요할 때만 바꾸세요. ⑥ 예상 용량과 여유 공간을 확인한 뒤 [⑦ 제작 시작]을 누르세요.",
            6: "⑦ 제작 중입니다. 언제든 [■ 중단]할 수 있습니다 (원본은 그대로, 임시 파일만 정리).",
            7: "⑧ 완성! [⑧ 완성 영상 재생]으로 확인하세요.",
        }
        try:
            for k, lb in enumerate(self.step_labels):
                style = "StepNow.TLabel" if k == cur else ("StepDone.TLabel" if k in done else "StepTodo.TLabel")
                mark = "✓ " if k in done and k != cur else ""
                opt = " (선택)" if k in OPTIONAL_STEPS and k not in done and k != cur else ""
                lb.configure(style=style, text=f"{mark}{CIRCLED[k]} {STEPS[k]}{opt}")
            self.step_hint.set(hints.get(cur, ""))
        except tk.TclError:
            pass

    # ---------------- 도구 ----------------
    def _tools(self):
        try:
            ff, fp = self.tools()
        except Exception:
            ff = fp = None
        if not ff or not fp:
            messagebox.showwarning("FFmpeg", "FFmpeg/ffprobe를 찾지 못했습니다. 메인 화면의 [FFmpeg 설정]에서 지정하세요.", parent=self)
            return None
        return Path(ff), Path(fp)

    # ---------------- 불러오기 / 분석 (백그라운드) ----------------
    def _pick_files(self):
        if self.running:
            return
        paths = filedialog.askopenfilenames(parent=self, title="배경으로 쓸 영상·사진 선택 (여러 개 가능)",
                                            filetypes=MEDIA_TYPES)
        if paths:
            self.add_paths([Path(p) for p in paths])

    def add_paths(self, paths):
        tools = self._tools()
        if not tools:
            return False
        have = {c.path for c in self.clips}
        fresh = []
        for p in paths:
            try:
                rp = Path(p).resolve()
            except OSError:
                continue
            if rp not in have and rp not in fresh:
                fresh.append(rp)
        if not fresh:
            return False
        if not self.outdir.get().strip():
            self.outdir.set(str(fresh[0].parent / "BG_OUTPUT"))
        # Tk 변수는 메인 스레드에서만 읽는다 → 사진 기본 표시 시간·움직임을 여기서 읽어 작업 스레드에 넘김
        self._spawn(self._load_worker, tools, fresh, self.still_default_seconds(),
                    self.motion_key(self.motion_default.get()))
        return True

    def _spawn(self, fn, *args):
        self.loading += 1
        self.stop_loading.clear()
        t = threading.Thread(target=self._guard_worker, args=(fn, *args), daemon=True)
        self._threads.append(t)
        t.start()
        self._refresh_buttons()

    def _guard_worker(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:  # pragma: no cover - 예기치 못한 오류도 창을 멈추지 않게
            self.events.put(("load_error", None, str(e)))
        finally:
            self.events.put(("loaded",))

    def still_default_seconds(self) -> float:
        try:
            return F.parse_still(self.still_default.get())
        except ValueError:
            return F.DEFAULT_STILL

    def motion_key(self, label: str) -> str:
        return next((k for k, v in F.MOTION_LABELS.items() if v == label), F.DEFAULT_MOTION)

    def _load_worker(self, tools, paths, still=F.DEFAULT_STILL, motion=F.DEFAULT_MOTION):
        ff, fp = tools
        probed = []
        for p in paths:
            if self.stop_loading.is_set():
                return
            self.events.put(("status", f"영상·사진 정보 읽는 중 · {p.name}"))
            try:
                info, meta = F.probe_media(p, fp, still)
            except Exception as e:
                self.events.put(("load_error", p, str(e)))
                continue
            self.events.put(("clip", info, meta, motion if meta.is_image else F.MOTION_NONE))
            pre = meta.pre_filter if meta.is_image else None
            self.events.put(("audio", info.path, MA.AudioCheck(MA.NONE) if meta.is_image else MA.check_audio(fp, info.path)))
            try:
                self.events.put(("thumb", info.path, A.thumbnail_b64(ff, info.path, info.duration, still_pre=pre)))
            except Exception:
                pass
            probed.append((info.path, info.duration, pre))
        self._analyze(ff, probed)

    def _analyze(self, ff, items):
        for k, (path, dur, pre) in enumerate(items, 1):
            if self.stop_loading.is_set():
                return
            self.events.put(("status", f"자동 분석 중 ({k}/{len(items)}) · {path.name}"))
            try:
                an = A.analyze_clip(ff, path, dur, self.stop_loading, still_pre=pre)
                self.events.put(("analysis", path, an))
            except Exception as e:
                self.events.put(("analysis_error", path, str(e)))

    def _analysis_items(self, clips):
        return [(c.path, c.info.duration, c.meta.pre_filter if c.is_image else None) for c in clips]

    def _reanalyze(self):
        if self.running or not self.clips:
            return
        tools = self._tools()
        if tools:
            self._spawn(self._analyze, tools[0], self._analysis_items(self.clips))

    # ---------------- 이벤트 ----------------
    def _pump(self):
        try:
            changed = False
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "status":
                    self.status.set(ev[1])
                elif kind == "clip":
                    info, meta, motion = ev[1], ev[2], ev[3]
                    if all(c.path != info.path for c in self.clips):
                        self.clips.append(Clip(info, meta=meta, motion=motion))
                        changed = True
                elif kind == "fit_preview":
                    self._fit_preview_ready(ev[1], ev[2], ev[3])
                elif kind == "fit_preview_error":
                    self.fit_previewing = False
                    self.status.set("화면 맞춤 미리보기를 만들지 못했습니다" if ev[1] else "미리보기 취소됨")
                    self._refresh_buttons()
                    if ev[1] and not getattr(self, "quiet_errors", False):
                        messagebox.showwarning("화면 맞춤 미리보기", ev[1], parent=self)
                elif kind == "thumb":
                    try:
                        self.thumbs[ev[1]] = tk.PhotoImage(master=self, data=ev[2])
                        changed = True
                    except tk.TclError:
                        pass
                elif kind == "analysis":
                    self.analyses[ev[1]] = ev[2]
                    self.analysis_errors.pop(ev[1], None)
                    changed = True
                elif kind == "audio":
                    self.audio_checks[ev[1]] = ev[2]
                    changed = True
                elif kind == "silent_done":
                    self._silent_finished(ev[1])
                elif kind == "silent_error":
                    self.silent_running = False
                    self.status.set("무음 사본 만들기 중단 (임시 파일 정리, 원본은 그대로)" if ev[1]
                                    else "무음 사본을 만들지 못했습니다 (원본은 그대로)")
                    self.silent_error = ev[2]
                    self._refresh_buttons()
                    if ev[2] and not getattr(self, "quiet_errors", False):
                        messagebox.showwarning("무음 MP4", ev[2], parent=self)
                elif kind == "analysis_error":
                    self.analysis_errors[ev[1]] = ev[2]
                    changed = True
                elif kind == "load_error":
                    name = ev[1].name if ev[1] else "영상"
                    messagebox.showerror("영상을 불러올 수 없습니다", f"{name}\n\n{ev[2]}\n\n이 파일은 건너뜁니다.", parent=self)
                elif kind == "loaded":
                    self.loading = max(0, self.loading - 1)
                    if not self.loading and not self.running:
                        self.status.set("분석 완료 · 설정을 확인하고 [⑦ 제작 시작]을 누르세요." if self.clips else "대기 중")
                    changed = True
                elif kind == "progress":
                    self.bar["value"] = ev[1] * 100
                    self.status.set(ev[2])
                elif kind == "done":
                    self._finished(ev[1])
                elif kind == "failed":
                    self._failed(ev[1], ev[2])
                elif kind == "preview":
                    self._preview_ready(ev[1], ev[2])
                elif kind == "preview_error":
                    self.previewing = False
                    self.status.set("미리보기를 만들지 못했습니다" if ev[1] else "미리보기 취소됨")
                    self._refresh_buttons()
                    if ev[1] and not getattr(self, "quiet_errors", False):
                        messagebox.showwarning("미리보기", ev[1], parent=self)
                elif kind == "measured":
                    self.measuring = False
                    self.measured[ev[1]] = ev[2]
                    self.status.set(f"실측 완료 · 평균 {ev[2] * 8 / 1e6:.1f} Mbps (시험 구간 기준)")
                    changed = True
                elif kind == "measure_error":
                    self.measuring = False
                    self.status.set("용량 측정 실패" if ev[1] else "용량 측정 취소됨")
                    changed = True
                    if ev[1] and not getattr(self, "quiet_errors", False):
                        messagebox.showwarning("용량 측정", ev[1], parent=self)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if changed:
            self._refresh_all()
        try:
            self.after(100, self._pump)
        except tk.TclError:
            pass

    # ---------------- 목록 조작 ----------------
    def _selected_index(self):
        sel = self.tree.selection()
        return self.tree.index(sel[0]) if sel else None

    def _move(self, d):
        i = self._selected_index()
        if i is None or self.running:
            return
        j = i + d
        if 0 <= j < len(self.clips):
            self.clips[i], self.clips[j] = self.clips[j], self.clips[i]
            self._refresh_all(select=j)

    def _auto_order(self):
        """비슷한 장면이 연달아 나오지 않고 밝기·움직임이 부드럽게 이어지게 사용 중인 영상 순서를 바꾼다 (첫 영상 고정)."""
        if self.running or self.loading:
            return False
        active = [c for c in self.clips if c.enabled]
        if len(active) < 3 or any(c.path not in self.analyses for c in active):
            messagebox.showinfo("자동 순서 추천", "분석이 끝난 사용 영상이 3개 이상일 때 쓸 수 있습니다.", parent=self)
            return False
        order = A.suggest_order([self.analyses[c.path] for c in active], self.effective_dissolve()[0],
                                [(c.used_start, c.end) for c in active])
        if order == list(range(len(active))):
            self.status.set("자동 순서 추천: 지금 순서가 이미 가장 자연스럽습니다.")
            return True
        sel = self._selected_index()
        picked = self.clips[sel] if sel is not None else None
        new_active = iter([active[k] for k in order])
        self.clips = [next(new_active) if c.enabled else c for c in self.clips]  # 제외한 영상 자리는 그대로
        self.status.set("자동 순서 추천을 적용했습니다 (비슷한 장면은 떨어뜨리고 밝기·움직임이 부드럽게). ▲▼로 다시 바꿀 수 있습니다.")
        self._refresh_all(select=self.clips.index(picked) if picked in self.clips else None)
        return True

    def _toggle_enabled(self):
        i = self._selected_index()
        if i is None or self.running:
            return
        self.clips[i].enabled = not self.clips[i].enabled
        self._refresh_all(select=i)

    def _remove(self):
        i = self._selected_index()
        if i is None or self.running:
            return
        c = self.clips.pop(i)  # 목록에서만 뺀다 — 원본 파일은 절대 지우지 않음
        self.thumbs.pop(c.path, None)
        self._refresh_all(select=min(i, len(self.clips) - 1) if self.clips else None)

    def _parse_range(self, clip):
        try:
            s = float(self.range_start.get().strip() or 0)
            e_txt = self.range_end.get().strip()
            e = float(e_txt) if e_txt else clip.info.duration
        except ValueError:
            raise ValueError("시작/끝은 초 단위 숫자로 입력하세요 (예: 2.5).")
        if s < 0 or e > clip.info.duration + 0.01 or e - s < 1:
            raise ValueError(f"구간이 올바르지 않습니다. 0 ~ {clip.info.duration:.1f}초 안에서, 1초 이상으로 정하세요.")
        return s, (None if e >= clip.info.duration - 0.01 else e)

    def _apply_range(self):
        i = self._selected_index()
        if i is None or self.running:
            return
        c = self.clips[i]
        if c.is_image:
            messagebox.showinfo("사용 구간", "사진은 사용 구간 대신 아래 [표시 시간]을 정하세요.", parent=self)
            return
        try:
            c.start, c.end = self._parse_range(c)
        except ValueError as e:
            messagebox.showwarning("사용 구간", str(e), parent=self)
            return
        self._refresh_all(select=i)

    def _apply_advice_to(self, c):
        adv = self.advice.get(c.path)
        if adv and not c.is_image:  # 사진은 같은 화면이 이어지므로 구간을 바꾸지 않는다 (표시 시간 전체)
            c.start = adv.start
            c.end = None if adv.end >= c.info.duration - 0.01 else adv.end

    def _apply_advice(self):
        i = self._selected_index()
        if i is None or self.running:
            return
        if self.clips[i].path not in self.advice:
            messagebox.showinfo("추천 구간", "아직 분석 결과가 없습니다.", parent=self)
            return
        self._apply_advice_to(self.clips[i])
        self._refresh_all(select=i)

    def _apply_all_advice(self):
        if self.running:
            return
        for c in self.clips:
            self._apply_advice_to(c)
        self._refresh_all(select=self._selected_index())

    def _full_range(self):
        i = self._selected_index()
        if i is None or self.running:
            return
        self.clips[i].start, self.clips[i].end = 0.0, None
        self._refresh_all(select=i)

    # ---------------- 사진 표시 시간 · 움직임 ----------------
    def _apply_still(self, all_images: bool = False) -> bool:
        """선택한 사진(또는 모든 사진)의 표시 시간·움직임 효과를 바꾼다. 시간이 바뀐 사진은 다시 분석 (아주 빠름)."""
        if self.running:
            return False
        i = self._selected_index()
        sel = self.clips[i] if i is not None else None
        if sel is None or not sel.is_image:
            if not all_images:
                return False
        try:
            secs = F.parse_still(self.still_sel.get())
        except ValueError as e:
            if not getattr(self, "quiet_errors", False):
                messagebox.showwarning("사진 표시 시간", str(e), parent=self)
            return False
        motion = self.motion_key(self.motion_sel.get())
        targets = [c for c in self.clips if c.is_image] if all_images else [sel]
        if not targets:
            return False
        changed = []
        for c in targets:
            if abs(c.info.duration - secs) > 1e-6:
                c.info = replace(c.info, duration=secs)
                c.start, c.end = 0.0, None
                self.analyses.pop(c.path, None)
                changed.append(c)
            c.motion = motion
        if changed:
            tools = self._tools()
            if tools:
                self._spawn(self._analyze, tools[0], self._analysis_items(changed))
        self._refresh_all(select=i)
        return True

    def _toggle_advanced(self):
        try:
            if self.show_advanced.get():
                self.adv.pack(fill="x", pady=(4, 0), after=self.fit_row)
            else:
                self.adv.pack_forget()
        except tk.TclError:
            pass

    def fit_key(self) -> str:
        return self.fit.get() if self.fit.get() in F.FIT_CHOICES else F.DEFAULT_FIT

    def blur_key(self) -> int:
        return next((k for k, v in F.BLUR_LABELS.items() if v == self.blur.get()), F.DEFAULT_BLUR)

    def pad_key(self) -> str:
        return F.PAD_COLORS.get(self.pad.get(), F.DEFAULT_PAD)

    def current_spec(self):
        """지금 설정 그대로의 출력 규격 (계획이 없을 때도 미리보기용으로)."""
        if self.plan is not None:
            return self.plan.spec
        infos = [c.info for c in self.clips if c.enabled] or [c.info for c in self.clips]
        return choose_output_spec(infos, self.quality.get(), hevc_available=False, profile=self.profile_key(),
                                  fit=self.fit_key(), blur=self.blur_key(), pad=self.pad_key())

    # ---------------- 화면 맞춤 미리보기 ----------------
    def preview_fit(self) -> bool:
        """선택한 영상·사진 1장면이 16:9 출력에서 어떻게 보이는지 (실제 제작과 같은 필터·해상도로 1프레임 → 축소).
        꽉 채우기면 원본에서 남는 영역/잘리는 영역도 함께 보여 준다."""
        i = self._selected_index()
        if i is None or self.running or self.fit_previewing or self.previewing:
            return False
        tools = self._tools()
        if not tools:
            return False
        try:
            spec = self.current_spec()
        except ValueError as e:
            messagebox.showwarning("화면 맞춤 미리보기", str(e), parent=self)
            return False
        if not FFMPEG_GUARD.try_acquire(GUARD_OWNER):
            messagebox.showwarning("FFmpeg 사용 중", _busy_text(FFMPEG_GUARD.owner) + "\n끝난 뒤 다시 시도하세요.", parent=self)
            return False
        c = self.clips[i]
        self.fit_previewing = True
        self.preview_cancel.clear()
        self.status.set(f"화면 맞춤 미리보기 만드는 중… {c.path.name}")
        self._refresh_buttons()
        t = threading.Thread(target=self._fit_preview_worker, args=(tools[0], c, spec), daemon=True)
        self._threads.append(t)
        t.start()
        return True

    def _fit_preview_worker(self, ff, c, spec):
        import base64
        try:
            res = fit_preview_png(Path(ff), c, spec, self.preview_cancel, size=(FIT_PREVIEW_W, FIT_PREVIEW_H))
            src = None
            if spec.fit == F.FIT_FILL and not F.is_169(c.info.width, c.info.height):
                src = crop_preview_png(Path(ff), c, spec, self.preview_cancel, box_h=FIT_PREVIEW_H)
            self.events.put(("fit_preview", c.path,
                             base64.b64encode(res).decode("ascii"), base64.b64encode(src).decode("ascii") if src else None))
        except BuildCancelled:
            self.events.put(("fit_preview_error", ""))
        except Exception as e:
            self.events.put(("fit_preview_error", str(e)))
        finally:
            FFMPEG_GUARD.release(GUARD_OWNER)

    def _fit_preview_ready(self, path, res_b64, src_b64):
        self.fit_previewing = False
        self.previewed = True
        c = next((x for x in self.clips if x.path == path), None)
        try:
            imgs = [tk.PhotoImage(master=self, data=res_b64)]
            spec = self.current_spec()
            g = F.fit_geometry(c.info.width, c.info.height, spec.width, spec.height, spec.fit) if c else None
            cap = (f"결과 16:9 ({spec.width}×{spec.height} · {F.FIT_SHORT.get(spec.fit, '')}) — "
                   f"{path.name}" + (f" · {g.scale:.2f}배" if g else ""))
            self.fit_img_result.configure(image=imgs[0], text=cap)
            if src_b64:
                imgs.append(tk.PhotoImage(master=self, data=src_b64))
                lost = f"원본의 {1 - g.kept:.0%} 잘림 ({g.lost_axis})" if g else ""
                self.fit_img_source.configure(image=imgs[1], text=f"원본 전체 — 빨간 상자 안만 화면에 남음 · {lost}")
            else:
                self.fit_img_source.configure(image="", text="")
            self.fit_preview_imgs = imgs
        except tk.TclError:
            pass
        self.status.set(f"화면 맞춤 미리보기: {path.name} (실제 제작과 같은 필터·해상도로 만든 1장면을 줄여서 표시)")
        self._refresh_buttons()

    def _pick_outdir(self):
        p = filedialog.askdirectory(parent=self, initialdir=self.outdir.get() or None)
        if p:
            self.outdir.set(p)
            self._options_changed()

    # ---------------- 계산 / 표시 ----------------
    def dissolve_seconds(self) -> float:
        try:
            return float(self.dissolve.get().replace("초", "").strip())
        except ValueError:
            return DEFAULT_DISSOLVE

    @staticmethod
    def _custom_range(c: Clip) -> bool:
        return c.start > 0.05 or c.end is not None

    def verdict_advice(self, c: Clip) -> A.LoopAdvice | None:
        """사용자가 구간을 바꿨으면 그 구간 그대로의 평가, 아니면 자동 추천 결과."""
        if self._custom_range(c) and c.path in self.range_eval:
            return self.range_eval[c.path]
        return self.advice.get(c.path)

    def _advice_for(self, an: A.ClipAnalysis, d: float, rng=None) -> A.LoopAdvice:
        key = (id(an), d, rng)
        adv = self._advice_cache.get(key)
        if adv is None:
            adv = A.recommend_loop(an, d) if rng is None else A.evaluate_range(an, rng[0], rng[1], d)
            if len(self._advice_cache) > 400:
                self._advice_cache.clear()
            self._advice_cache[key] = adv
        return adv

    def effective_dissolve(self) -> tuple[float, str]:
        """영상 1개 반복인데 반복 지점이 애매하면 디졸브를 2초로 늘려 이음새를 부드럽게 (대안 자동 적용)."""
        d = self.dissolve_seconds()
        active = [c for c in self.clips if c.enabled]
        if len(active) == 1 and d < 2.0:
            adv = self.verdict_advice(active[0])
            if adv is not None and adv.verdict != A.VERDICT_GOOD:
                return 2.0, "영상 1개의 반복 경계가 어색할 수 있어 디졸브를 2초로 늘려 부드럽게 만듭니다."
        return d, ""

    def _options_changed(self):
        self._save_settings()
        self._refresh_all(select=self._selected_index())

    # ---------------- 음악 길이 직접 입력 ----------------
    def project_fps_value(self) -> float:
        try:
            v = float(self.project_fps.get())
        except (TypeError, ValueError):
            return DEFAULT_PROJECT_FPS
        return v if v in PROJECT_FPS_CHOICES else DEFAULT_PROJECT_FPS

    def target_seconds(self) -> float:
        """출력 길이(초). 직접 입력이면 HH:MM:SS(.mmm / :FF)를 해석 — 잘못되면 ValueError."""
        m = int(self.minutes.get())
        if m != CUSTOM:
            return float(m * 60)
        return parse_length(self.custom_length.get(), self.project_fps_value())

    def _use_custom(self):
        if int(self.minutes.get()) != CUSTOM:
            self.minutes.set(CUSTOM)
            self._options_changed()

    def _length_changed(self, typed: bool = False):
        """입력하는 즉시 형식 검사, 계산(예상 반복·용량·시간)은 입력이 멈춘 뒤 0.3초에 한 번만."""
        if typed and int(self.minutes.get()) != CUSTOM:
            self.minutes.set(CUSTOM)
        self._update_length_check()
        if self._length_job is not None:
            try:
                self.after_cancel(self._length_job)
            except tk.TclError:
                pass
        self._length_job = self.after(300, self._length_settled)

    def _length_settled(self):
        self._length_job = None
        try:
            self._save_settings()
            self._recalc(self.duplicate_findings())
            self._refresh_buttons()
        except tk.TclError:
            pass

    def _update_length_check(self):
        if int(self.minutes.get()) != CUSTOM:
            self.length_check.set("")
            return
        try:
            t = parse_length(self.custom_length.get(), self.project_fps_value())
        except ValueError as e:
            self.length_check.set(f"⚠ {e}")
            return
        self.length_check.set(f"✓ {format_precise(t)} ({t:,.3f}초)")

    def _save_settings(self):
        try:
            update_settings(**{SETTINGS_KEY: {"dissolve": self.dissolve_seconds(), "minutes": int(self.minutes.get()),
                                              "quality": self.quality.get(), "outdir": self.outdir.get().strip(),
                                              "profile": self.profile_key(), "codec": self.codec_key(),
                                              "custom_length": self.custom_length.get().strip()[:32],
                                              "project_fps": self.project_fps_value(),
                                              "fit": self.fit_key(), "blur": self.blur_key(), "pad": self.pad_key(),
                                              "still_seconds": self.still_default_seconds(),
                                              "motion": self.motion_key(self.motion_default.get())}})
        except Exception:
            pass

    def profile_key(self) -> str:
        label = self.profile.get()
        return next((p.key for p in ENCODE_PROFILES if p.label == label), ENCODE_PROFILES[0].key)

    def codec_key(self) -> str:
        return CODEC_HEVC if self.codec.get() == CODEC_HEVC else CODEC_H264

    def _next_cell(self, c):
        if not c.enabled:
            return ""
        t = self.transitions.get(c.path)
        return t.verdict if t else ("분석 중…" if self.loading else "-")

    @staticmethod
    def plan_key(plan: BgPlan) -> tuple:
        """실측값을 다시 쓸 수 있는지 판단하는 서명 (설정·영상·구간이 바뀌면 다시 재야 함). 길이는 무관."""
        return (plan.spec, plan.dissolve, tuple((p.kind, str(p.a.path), round(p.a.start, 3), round(p.length, 3))
                                                for p in plan.pieces))

    def _hevc_ok(self) -> bool:
        if self._hevc is None:
            try:
                ff, _ = self.tools()
                self._hevc = bool(ff) and hevc_available(Path(ff))
            except Exception:
                self._hevc = False
        return self._hevc

    def _loop_cell(self, c):
        if c.path in self.analysis_errors:
            return "분석 실패"
        adv = self.verdict_advice(c)
        if adv is None:
            return "분석 중…" if self.loading else "-"
        return adv.verdict + (" (내 구간)" if self._custom_range(c) else "")

    def _audio_cell(self, c):
        ac = self.audio_checks.get(c.path)
        return ac.short if ac else "검사 중…"

    def recheck_audio(self) -> bool:
        """선택한 영상(선택이 없으면 '확인 불가' 전부)의 원본 오디오를 다시 검사."""
        tools = self._tools()
        if not tools:
            return False
        i = self._selected_index()
        if i is not None:
            targets = [self.clips[i].path]
        else:
            targets = [c.path for c in self.clips
                       if c.path not in self.audio_checks or self.audio_checks[c.path].status == MA.UNKNOWN]
        if not targets:
            return False
        for p in targets:
            self.audio_checks.pop(p, None)
        fp = tools[1]

        def work():
            for p in targets:
                self.events.put(("audio", p, MA.check_audio(fp, p)))
        t = threading.Thread(target=work, daemon=True)
        self._threads.append(t)
        t.start()
        self._refresh_all()
        return True

    def save_silent_copy(self, out=None) -> bool:
        """선택한 영상의 '오디오 없는 사본' (영상 스트림 복사, 재인코딩 없음). 원본 옆 새 파일 — 덮어쓰지 않음."""
        i = self._selected_index()
        quiet = getattr(self, "quiet_errors", False)
        if i is None:
            if not quiet:
                messagebox.showinfo("무음 MP4", "목록에서 영상을 선택하세요.", parent=self)
            return False
        if self.running or self.silent_running:
            return False
        c = self.clips[i]
        if c.is_image:  # 사진에는 오디오가 없다
            return False
        ac = self.audio_checks.get(c.path)
        if ac is not None and ac.status == MA.NONE and not quiet:
            if not messagebox.askyesno("무음 MP4", "이 영상에는 원본 오디오가 없습니다. 그래도 사본을 만들까요?\n"
                                                  "(같은 영상이 하나 더 생겨 저장 공간만 늘어납니다)", parent=self):
                return False
        tools = self._tools()
        if not tools:
            return False
        if not FFMPEG_GUARD.try_acquire(GUARD_OWNER):
            messagebox.showwarning("FFmpeg 사용 중", _busy_text(FFMPEG_GUARD.owner) + "\n끝난 뒤 다시 시도하세요.", parent=self)
            return False
        self.silent_running = True
        self.silent_cancel = threading.Event()
        self.status.set(f"무음 사본 만드는 중… {c.path.name} (영상만 복사, 재인코딩 없음)")
        self._refresh_buttons()
        cancel = self.silent_cancel

        def work():
            try:
                r = MA.make_silent_copy(ffmpeg=Path(tools[0]), ffprobe=Path(tools[1]), src=c.path, out=out,
                                        cancel=cancel,
                                        progress=lambda f, t: self.events.put(("status", f"무음 사본: {t}")))
                self.events.put(("silent_done", r))
            except BuildCancelled:
                self.events.put(("silent_error", True, ""))
            except Exception as e:
                self.events.put(("silent_error", False, str(e)))
            finally:
                FFMPEG_GUARD.release(GUARD_OWNER)
        t = threading.Thread(target=work, daemon=True)
        self._threads.append(t)
        t.start()
        return True

    def _silent_finished(self, r):
        self.silent_running = False
        self.last_silent = r
        self.status.set(f"무음 사본 완성: {r.output.name} · {r.codec.upper()} {r.width}×{r.height} · {r.fps:g} FPS · "
                        f"{format_duration(r.duration)} · 오디오 {r.removed_streams}개 제외 · 원본은 그대로")
        self._refresh_buttons()

    def _quality_cell(self, c, dup_map):
        if c.path in self.analysis_errors:
            return "⚠ 분석 실패"
        an = self.analyses.get(c.path)
        if an is None:
            return ""
        parts = []
        if an.quality_warnings:
            parts.append("⚠ " + an.quality_warnings[0] + (f" 외 {len(an.quality_warnings) - 1}" if len(an.quality_warnings) > 1 else ""))
        else:
            parts.append("양호")
        if c.path in dup_map:
            parts.append(f"· {dup_map[c.path]}")
        return " ".join(parts)

    def duplicate_findings(self):
        ans = [self.analyses[c.path] for c in self.clips if c.path in self.analyses]
        return A.find_duplicates(ans)

    def _refresh_all(self, select=None):
        if select is None:
            select = self._selected_index()
        d = self.dissolve_seconds()
        self.advice = {p: self._advice_for(an, d) for p, an in self.analyses.items()}
        self.range_eval = {c.path: self._advice_for(self.analyses[c.path], d, (c.used_start, c.end))
                           for c in self.clips if self._custom_range(c) and c.path in self.analyses}
        dups = self.duplicate_findings()
        dup_map: dict[Path, str] = {}
        for f in dups:
            dup_map.setdefault(f.a, f"{f.kind}: {f.b.name}")
            dup_map.setdefault(f.b, f"{f.kind}: {f.a.name}")
        active = [c for c in self.clips if c.enabled]
        self.transitions = {}
        if len(active) >= 2 and all(c.path in self.analyses for c in active):
            for c, t in zip(active, A.transition_advices([self.analyses[c.path] for c in active],
                                                         self.effective_dissolve()[0],
                                                         [(c.used_start, c.end) for c in active])):
                self.transitions[c.path] = t
        for x in self.tree.get_children():
            self.tree.delete(x)
        for i, c in enumerate(self.clips):
            if c.is_image:
                rng = f"🖼 사진 {c.info.duration:g}초" + ("" if c.motion == F.MOTION_NONE else " · 움직임")
            else:
                rng = "전체" if c.start <= 0 and c.end is None else f"{c.used_start:.1f}~{c.used_end:.1f}초"
            kw = {"image": self.thumbs[c.path]} if c.path in self.thumbs else {}
            self.tree.insert("", "end", iid=str(i), text="" if kw else f"#{i + 1}", **kw, values=(
                "✓" if c.enabled else "제외", c.path.name, format_duration(c.info.duration),
                f"{c.info.width}×{c.info.height} {F.aspect_label(c.info.width, c.info.height)}",
                "사진" if c.is_image else f"{c.info.fps:.2f}", rng, self._loop_cell(c),
                self._next_cell(c), self._quality_cell(c, dup_map), self._audio_cell(c)))
        if select is not None and 0 <= select < len(self.clips):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))
        active = [c for c in self.clips if c.enabled]
        checks = [self.audio_checks.get(c.path) for c in self.clips]
        with_audio = sum(1 for a in checks if a is not None and a.status == MA.PRESENT)
        unknown = sum(1 for a in checks if a is not None and a.status == MA.UNKNOWN)
        audio_txt = (f" · 🔊 오디오 있는 영상 {with_audio}개 — 배경 제작 시 자동 제외" if with_audio else "") + (
            f" · ⚠ 오디오 확인 불가 {unknown}개 — [오디오 다시 검사]" if unknown else "")
        n_img = sum(1 for c in self.clips if c.is_image)
        kinds = f"영상 {len(self.clips) - n_img}개" + (f" · 사진 {n_img}장" if n_img else "")
        self.summary.set(f"{kinds} · 사용 {len(active)}개 · 사용 구간 합계 "
                         f"{format_duration(sum(c.used_length for c in active))}{audio_txt}" if self.clips
                         else "영상·사진을 불러오세요 (영상 30초~2분 길이 권장, 세로 숏츠·사진도 가능).")
        self._recalc(dups)
        self._show_detail()
        self._refresh_buttons()

    def _recalc(self, dups=None):
        self.plan = None
        self.profile_note.set(get_profile(self.profile_key()).note)
        active = [c for c in self.clips if c.enabled]
        if not active:
            self.plan_text.set("")
            self.size_text.set("")
            self.warn_text.set("")
            if self._name_auto:
                self.outname.set("")
            return
        self._update_length_check()
        try:
            target = self.target_seconds()
        except ValueError as e:
            self.plan_text.set(f"⚠ 음악 길이: {e}")
            self.size_text.set("")
            self.warn_text.set("")
            return
        if self._name_auto:
            custom = int(self.minutes.get()) == CUSTOM
            self.outname.set(default_bg_output_name(self.clips, int(self.minutes.get()),
                                                    seconds=target if custom else None))
        warns: list[str] = []
        d, note = self.effective_dissolve()
        if note:
            warns.append(note)
        prof = get_profile(self.profile_key())
        self.profile_note.set(prof.note)
        codec = self.codec_key()
        try:
            spec = choose_output_spec([c.info for c in active], self.quality.get(),
                                      hevc_available=self._hevc_ok() if codec == CODEC_HEVC else False,
                                      profile=prof.key, codec=codec, fit=self.fit_key(), blur=self.blur_key(),
                                      pad=self.pad_key())
            self._update_fit_note(active, spec)
            plan = build_bg_plan(active, target, d, spec)
        except ValueError as e:
            self.plan_text.set(f"⚠ {e}")
            self.size_text.set("")
            self.warn_text.set("")
            return
        bps = self.measured.get(self.plan_key(plan))
        if bps:
            plan = with_measured_rate(plan, bps)
        self.plan = plan
        free_txt = ""
        short = False
        try:
            folder = Path(self.outdir.get().strip()) if self.outdir.get().strip() else active[0].path.parent
            probe_dir = folder if folder.exists() else next((p for p in folder.parents if p.exists()), folder)
            free = free_bytes(probe_dir)
            _, _, need = enough_disk_space(probe_dir, plan.required_bytes)  # 제작 시작 때와 같은 기준 (여유 1 GB 포함)
            short = free < need
            free_txt = f" · 여유 공간 {_gb(free)}" + (f" ⚠ 부족 (필요 약 {_gb(need)})" if short else " ✓ 충분")
        except Exception:
            pass
        laps = plan.target / plan.cycle_duration
        whole = int(laps + 1e-9)
        lap_txt = (f"전체 장면 순서 {whole}바퀴" + (" 후 정확히 끝남" if abs(laps - whole) < 1e-6
                                                 else f" + {whole + 1}번째 바퀴 중간에서 끝남") if whole
                   else "한 바퀴가 끝나기 전에 끝남")
        secs = estimate_build_seconds(plan, self.measured_speed.get(self.plan_key(plan)))
        self.plan_text.set(
            f"{plan.spec.label} · {plan.spec.fps:g} FPS · 디졸브 {plan.dissolve:.2f}초 · 한 바퀴 {format_duration(plan.cycle_duration)}"
            f" × 약 {laps:.1f}바퀴 → {format_precise(plan.target)} ({plan.target_frames:,}프레임)\n"
            f"예상 반복: {lap_txt} · 예상 제작 시간 {_time_text(secs)} (대략, PC 성능에 따라 다름"
            f"{' · 실측 속도 기준' if self.plan_key(plan) in self.measured_speed else ''})")
        if secs > 3 * 3600:
            warns.append(f"제작 시간이 {_time_text(secs)} 걸릴 수 있습니다. 제작 중에는 PC 절전이 자동으로 막히며, "
                         "[■ 중단]을 누르면 임시 파일만 지우고 멈춥니다.")
        if plan.measured_bps:
            lo, hi = plan.size_range
            size = f"예상 최종 크기 약 {_mb(lo)} ~ {_mb(hi)} (실측 {plan.measured_bps * 8 / 1e6:.1f} Mbps 기준)"
        else:
            size = f"예상 최종 크기 최대 약 {_gb(plan.expected_size)} (넉넉히 잡은 값 — 왼쪽 버튼으로 실측하면 더 정확)"
        self.size_text.set(f"{size} · 작업 중 최대 약 {_gb(plan.required_bytes)} 사용{free_txt}")
        warns.append("예상 용량은 영상 내용에 따라 달라집니다 (CRF·상한 모두 정확한 크기를 보장하지 않음). 제작 중에도 실제 "
                     "크기로 다시 확인해 공간이 모자라면 디스크가 가득 차기 전에 자동으로 멈춥니다 (원본·기존 파일은 그대로).")
        if short:
            warns.append("저장 공간이 부족합니다. 다른 저장 폴더를 고르거나, 길이를 줄이거나, [화질·용량]에서 용량이 작은 설정을 고르세요.")
        if prof.key != ENCODE_PROFILES[0].key:
            warns.append(f"[{prof.short or prof.label}] {prof.note}")
        if not plan.spec.live_compatible:
            warns.append("H.265 출력은 ② LIVE DIRECT COPY 조건(H.264)에 맞지 않아 LIVE READY가 아닙니다. 저장·업로드용으로 쓰세요.")
        warns += list(plan.warnings)
        if len(active) == 1:
            adv = self.verdict_advice(active[0])
            if adv is not None and adv.verdict != A.VERDICT_GOOD:
                warns.append(f"[{adv.verdict}] {' '.join(adv.reasons)}")
            elif adv is None:
                warns.append("반복 지점 분석이 끝나지 않았습니다 (확인 필요).")
        odd = [t for t in self.transitions.values() if t.verdict != A.TRANS_OK]
        for t in odd:
            warns.append(f"전환 {t.a.name} → {t.b.name}: [{t.verdict}] {' '.join(t.reasons)}")
        if odd:
            warns.append("전환이 걱정되면 목록에서 앞 영상을 선택하고 [▶ 다음 장면 전환 미리보기]로 확인하거나 [자동 순서 추천]을 누르세요.")
        for f in dups or []:
            warns.append(f"중복 의심 ({f.kind}): {f.a.name} ↔ {f.b.name}{' — ' + f.note if f.note else ''} "
                         "— 원본은 삭제하지 않습니다. 사용 여부를 직접 정하세요.")
        bad = [c.path.name for c in active if c.path in self.analysis_errors]
        if bad:
            warns.append("분석 실패(손상 가능): " + ", ".join(bad))
        self.warn_text.set("\n".join("⚠ " + w for w in warns))

    def _update_fit_note(self, active, spec):
        """화면 맞춤 요약: 비율별 개수 + 지금 방식 설명 + 꽉 채우기 잘림 최대치."""
        odd = [c for c in active if not F.is_169(c.info.width, c.info.height)]
        if not odd:
            self.fit_note.set(f"모든 원본이 16:9입니다 → 출력 {spec.width}×{spec.height} 화면을 그대로 채웁니다 "
                              "(화면 맞춤 방식과 관계없이 같은 결과).")
            return
        kinds: dict[str, int] = {}
        for c in odd:
            k = F.aspect_label(c.info.width, c.info.height)
            kinds[k] = kinds.get(k, 0) + 1
        mix = ", ".join(f"{k} {n}개" for k, n in kinds.items())
        desc = {F.FIT_BLUR: "원본 전체를 가운데에 두고 남는 곳은 같은 화면을 확대·흐림 처리한 배경 (머리·발이 잘리지 않음)",
                F.FIT_FILL: "확대해 16:9를 꽉 채우고 가장자리를 자름",
                F.FIT_CONTAIN: "원본 전체 + 단색 여백"}[spec.fit]
        txt = f"16:9가 아닌 원본: {mix} → {desc}. 출력 {spec.width}×{spec.height} 16:9."
        if spec.fit == F.FIT_FILL:
            worst = min(odd, key=lambda c: F.fit_geometry(c.info.width, c.info.height, spec.width, spec.height,
                                                          spec.fit).kept)
            g = F.fit_geometry(worst.info.width, worst.info.height, spec.width, spec.height, spec.fit)
            txt += (f" 가장 많이 잘리는 원본: {worst.path.name} — {1 - g.kept:.0%} 잘림 ({g.lost_axis})"
                    + (" ⚠ 심각" if g.kept < F.CROP_SEVERE else "") + ". [▶ 화면 맞춤 미리보기]로 확인하세요.")
        self.fit_note.set(txt)

    def _show_detail(self):
        i = self._selected_index()
        if i is None or i >= len(self.clips):
            self.detail.set("목록에서 영상을 선택하면 분석 결과가 보입니다.")
            self.range_start.set("")
            self.range_end.set("")
            return
        c = self.clips[i]
        self.range_start.set(f"{c.used_start:.2f}")
        self.range_end.set(f"{c.used_end:.2f}")
        info = c.info
        m = c.meta
        if c.is_image:
            self.still_sel.set(_still_label(info.duration))
            self.motion_sel.set(F.MOTION_LABELS.get(c.motion, F.MOTION_LABELS[F.MOTION_NONE]))
            lines = [f"🖼 {c.path.name} · {F.STILL_TAG} (실제 촬영 영상 아님) · 표시 {info.duration:g}초 · "
                     f"{info.width}×{info.height} ({F.aspect_label(info.width, info.height)}) · {info.video_codec.upper()}"
                     + (f" · EXIF 방향 {m.exif_orientation} 자동 보정" if m and m.exif_orientation != 1 else "")]
        else:
            lines = [f"{c.path.name} · {format_duration(info.duration)} · {info.width}×{info.height} "
                     f"({F.aspect_label(info.width, info.height)}) · {info.fps:.2f} FPS · {info.video_codec.upper()}"
                     + (f" · 회전 {m.rotation}° 반영" if m and m.rotation else "")
                     + (f" · 화소 비율(SAR) {m.sar:.3f} 보정" if m and abs(m.sar - 1) > 0.01 else "")]
        try:
            spec = self.current_spec()
            g = F.fit_geometry(info.width, info.height, spec.width, spec.height, spec.fit)
            if not g.plain:
                lines.append(f"화면 맞춤: {F.FIT_SHORT[spec.fit]} · {g.scale:.2f}배"
                             + (f" · 원본의 {1 - g.kept:.0%} 잘림 ({g.lost_axis})" if g.kept < 0.995 else " · 원본 전체 보임"))
            elif g.scale > F.UPSCALE_NOTICE:
                lines.append(f"화면 맞춤: 16:9 · {g.scale:.2f}배 확대 (선명도가 조금 낮아질 수 있음)")
        except ValueError:
            pass
        ac = self.audio_checks.get(c.path)
        lines.append(ac.detail if ac else "원본 오디오 검사 중…")
        if c.path in self.analysis_errors:
            lines.append("⚠ 분석 실패: " + self.analysis_errors[c.path].splitlines()[0])
        an = self.analyses.get(c.path)
        adv = self.advice.get(c.path)
        if an is None:
            lines.append("분석 중…" if self.loading else "분석 결과 없음 — [↻ 다시 분석]")
        else:
            lines.append(f"촬영 형태: {an.camera} · 밝기 {an.brightness:.0f}/255 · 움직임 {an.motion:.1f} · "
                         f"장면 변화(4초) {an.drift:.1f}" + (f" · 선명도 {an.sharpness:.0f}" if an.sharpness is not None else ""))
            if adv:
                lines.append(f"자동 추천: {adv.verdict} (경계 차이 {adv.score:.1f} = 밝기 {adv.luma_score:.1f} / 구도 "
                             f"{adv.struct_score:.1f} 중 큰 값, 낮을수록 자연스러움) — " + " ".join(adv.reasons))
            mine = self.range_eval.get(c.path) if self._custom_range(c) else None
            if mine:
                lines.append(f"지금 사용 구간 {c.used_start:.1f}~{c.used_end:.1f}초 그대로 반복: {mine.verdict} "
                             f"(경계 차이 {mine.score:.1f} = 밝기 {mine.luma_score:.1f} / 구도 {mine.struct_score:.1f}) — "
                             + " ".join(mine.reasons))
            t = self.transitions.get(c.path)
            if t:
                lines.append(f"다음 장면 전환 → {t.b.name}: {t.verdict} (밝기 차이 {t.luma_jump:.0f} · 색 차이 {t.color_jump:.0f} · "
                             f"움직임 {t.motion_end:.1f}→{t.motion_start:.1f}) — " + " ".join(t.reasons))
            lines.append("품질 참고: " + (" / ".join(an.quality_warnings) if an.quality_warnings else "특이사항 없음"))
        self.detail.set("\n".join(lines))

    def _refresh_buttons(self):
        busy = self.running or bool(self.loading) or self.previewing or self.measuring or self.fit_previewing
        try:
            i = self._selected_index()
            sel = i is not None
            img = sel and i < len(self.clips) and self.clips[i].is_image
            for b in (self.btn_preview_loop, self.btn_preview_next):
                b.configure(state="disabled" if busy or not sel else "normal")
            self.btn_fit_preview.configure(state="disabled" if busy or not sel else "normal")
            for w in (self.cb_still_sel, self.btn_still_apply):
                w.configure(state="disabled" if self.running or not img else "normal")
            self.cb_motion_sel.configure(state="disabled" if self.running or not img else "readonly")
            self.btn_still_all.configure(state="normal" if not self.running and any(c.is_image for c in self.clips)
                                         else "disabled")
            self.btn_silent.configure(state="disabled" if self.running or self.silent_running or not sel or img
                                      else "normal")
            self.btn_recheck_audio.configure(state="disabled" if not self.clips else "normal")
            self.btn_start.configure(state="disabled" if busy or not any(c.enabled for c in self.clips) else "normal")
            self.btn_stop.configure(state="normal" if self.running else "disabled")
            self.btn_measure.configure(state="disabled" if busy or self.plan is None else "normal")
            self.btn_order.configure(state="disabled" if busy or sum(c.enabled for c in self.clips) < 3 else "normal")
            done = self.last_output is not None and self.last_output.exists()
            for b in (self.btn_open, self.btn_folder):
                b.configure(state="normal" if done else "disabled")
            self.btn_send.configure(state="normal" if done and self.send_to_long_cb else "disabled")
        except tk.TclError:
            pass
        self._update_steps()

    # ---------------- ⑥ 실제 용량 측정 ----------------
    def measure_size(self) -> bool:
        """지금 설정 그대로 장면 몇 곳을 2초씩 시험 인코딩해 예상 크기를 실측한다 (원본은 읽기만, 시험 파일은 즉시 삭제)."""
        if (self.running or self.loading or self.previewing or self.measuring or self.fit_previewing
                or self.plan is None):
            return False
        tools = self._tools()
        if not tools:
            return False
        if not FFMPEG_GUARD.try_acquire(GUARD_OWNER):
            messagebox.showwarning("FFmpeg 사용 중", _busy_text(FFMPEG_GUARD.owner) + "\n끝난 뒤 다시 시도하세요.", parent=self)
            return False
        plan, key = self.plan, self.plan_key(self.plan)
        self.measuring = True
        self.measure_cancel.clear()
        self.status.set("예상 용량 실측 중… (장면 몇 곳을 실제 설정으로 시험 인코딩)")
        self._refresh_buttons()
        work = Path(tempfile.gettempdir()) / "PLVM_BG_MEASURE"
        t = threading.Thread(target=self._measure_worker, args=(tools[0], plan, key, work), daemon=True)
        self._threads.append(t)
        t.start()
        return True

    def _measure_worker(self, ff, plan, key, work):
        try:
            t0 = time.monotonic()
            bps = measure_bitrate(Path(ff), plan, self.measure_cancel, work,
                                  progress_cb=lambda f, t: self.events.put(("status", t)))
            spent = time.monotonic() - t0   # FFmpeg 시작 시간까지 포함 → 실제보다 느리게(=안전하게) 나온다
            encoded = sum(p.length for p in measurement_pieces(plan))
            if spent > 0 and encoded > 0:
                self.measured_speed[key] = encoded / spent
            self.events.put(("measured", key, bps))
        except BuildCancelled:
            self.events.put(("measure_error", ""))
        except Exception as e:
            self.events.put(("measure_error", str(e)))
        finally:
            FFMPEG_GUARD.release(GUARD_OWNER)

    # ---------------- 제작 ----------------
    def start(self, *, confirm=True):
        if self.running or self.loading or self.previewing or self.measuring or self.fit_previewing:
            return False
        tools = self._tools()
        if not tools:
            return False
        self._recalc(self.duplicate_findings())
        plan = self.plan
        if plan is None:
            messagebox.showwarning("설정 확인", self.plan_text.get().lstrip("⚠ ") or "사용할 영상을 추가하세요.", parent=self)
            return False
        active = [c for c in self.clips if c.enabled]
        if confirm and len(active) == 1:
            adv = self.verdict_advice(active[0])
            verdict = adv.verdict if adv else A.VERDICT_CHECK
            if verdict != A.VERDICT_GOOD and not messagebox.askyesno(
                    verdict, f"이 영상은 '{verdict}'입니다.\n{' '.join(adv.reasons) if adv else '반복 지점 분석 결과가 없습니다.'}\n\n"
                    "다른 영상을 섞으면 장면 전환으로 더 자연스럽게 반복됩니다.\n그래도 만들까요?", parent=self):
                return False
        outdir_txt = self.outdir.get().strip()
        name = self.outname.get().strip()
        if not outdir_txt or not name:
            messagebox.showwarning("저장", "저장 폴더와 파일명을 확인하세요.", parent=self)
            return False
        if not name.lower().endswith(".mp4"):
            name += ".mp4"
        outdir = Path(outdir_txt)
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("저장 폴더", f"저장 폴더를 만들 수 없습니다: {e}", parent=self)
            return False
        output = ensure_unique_output(outdir / name)
        if output.resolve() in {c.path for c in self.clips}:
            messagebox.showerror("저장", "원본 영상과 같은 파일로 저장할 수 없습니다.", parent=self)
            return False
        if not FFMPEG_GUARD.try_acquire(GUARD_OWNER):
            messagebox.showwarning("FFmpeg 사용 중", _busy_text(FFMPEG_GUARD.owner) + "\n끝난 뒤 다시 시작하세요.", parent=self)
            return False
        cleanup_stale_temp(outdir)
        self._save_settings()
        self.running = True
        self.cancel.clear()
        self.last_output = None
        self.bar["value"] = 0
        self.status.set("제작 준비 중…")
        self._refresh_buttons()
        t = threading.Thread(target=self._build_worker, args=(tools, plan, output), daemon=True)
        self._threads.append(t)
        t.start()
        return True

    def _build_worker(self, tools, plan, output):
        ff, fp = tools
        try:
            with prevent_windows_sleep(True):
                r = run_background_build(ffmpeg=ff, ffprobe=fp, plan=plan, output=output, cancel_event=self.cancel,
                                         progress_cb=lambda f, t: self.events.put(("progress", f, t)))
            self.events.put(("done", r))
        except BuildCancelled as e:
            self.events.put(("failed", True, str(e)))
        except Exception as e:
            self.events.put(("failed", False, str(e)))
        finally:
            FFMPEG_GUARD.release(GUARD_OWNER)

    def stop(self):
        if self.running:
            self.cancel.set()
            self.btn_stop.configure(state="disabled")
            self.status.set("중단하는 중… (임시 파일 정리)")

    def _finished(self, r):
        self.running = False
        self.last_output = r.output
        self.bar["value"] = 100
        exact = ""
        if r.target_frames:
            exact = (f" · 목표 {format_precise(r.target)} 대비 {r.error_seconds * 1000:+.1f} ms "
                     f"({r.frames:,}/{r.target_frames:,}프레임{' 일치' if r.frames == r.target_frames else ''}) · 오디오 없음")
        self.status.set(f"완성: {r.output.name} · {format_precise(r.duration)} · {r.width}×{r.height} · "
                        f"{r.fps:g} FPS · {r.size / 1024**2:.0f} MB · 걸린 시간 {format_duration(r.elapsed)}{exact}")
        self._refresh_buttons()
        self.result = r

    def _failed(self, cancelled, msg):
        self.running = False
        self.bar["value"] = 0
        self.status.set("중단됨 (임시 파일 정리 완료, 원본은 그대로)" if cancelled else "제작 실패 (원본은 그대로)")
        self._refresh_buttons()
        self.error = msg
        if not cancelled and not getattr(self, "quiet_errors", False):
            messagebox.showerror("제작 실패", msg, parent=self)

    # ---------------- 반복 경계 미리보기 ----------------
    def preview_seam(self, which: str = "self") -> bool:
        """선택한 영상의 반복 경계(끝 → 처음) 또는 다음 장면으로의 전환을 앞뒤 약 2.5초씩 저화질로 만들어 연다."""
        i = self._selected_index()
        if i is None or self.running or self.loading or self.previewing or self.fit_previewing:
            return False
        a = self.clips[i]
        b = a
        if which == "next":
            active = [c for c in self.clips if c.enabled]
            if a not in active or len(active) < 2:
                messagebox.showinfo("미리보기", "사용 중인 영상이 2개 이상이어야 다음 장면 전환을 볼 수 있습니다.\n"
                                            "(영상 1개면 [반복 경계 미리보기]를 쓰세요.)", parent=self)
                return False
            b = active[(active.index(a) + 1) % len(active)]
        tools = self._tools()
        if not tools:
            return False
        if not FFMPEG_GUARD.try_acquire(GUARD_OWNER):
            messagebox.showwarning("FFmpeg 사용 중", _busy_text(FFMPEG_GUARD.owner) + "\n끝난 뒤 다시 시도하세요.", parent=self)
            return False
        d = self.effective_dissolve()[0]
        try:
            spec = self.current_spec()  # 화면 맞춤(흐림·꽉 채우기·원본 전체)을 실제 제작과 같게
        except ValueError:
            spec = None
        folder = Path(tempfile.gettempdir()) / "PLVM_BG_PREVIEW"
        out = folder / f"seam_{uuid.uuid4().hex[:8]}.mp4"
        self.previewing = True
        self.preview_cancel.clear()
        label = (f"{a.path.name} 끝 → 처음" if b is a else f"{a.path.name} → {b.path.name}")
        self.status.set(f"미리보기 만드는 중… ({label})")
        self._refresh_buttons()
        t = threading.Thread(target=self._preview_worker, args=(tools[0], a, b, d, out, label, spec), daemon=True)
        self._threads.append(t)
        t.start()
        return True

    def _preview_worker(self, ff, a, b, d, out, label, spec=None):
        try:
            p = make_seam_preview(Path(ff), a, b, d, out, self.preview_cancel, spec=spec)
            self.events.put(("preview", p, label))
        except BuildCancelled:
            self.events.put(("preview_error", ""))
        except Exception as e:
            self.events.put(("preview_error", str(e)))
        finally:
            FFMPEG_GUARD.release(GUARD_OWNER)

    def _preview_ready(self, path: Path, label: str):
        self.previewing = False
        old = [p for p in self.preview_files if p != path]
        for p in old:  # 이전 미리보기는 지운다 (재생 중이라 잠겨 있으면 창을 닫을 때 다시 시도)
            try:
                p.unlink()
                self.preview_files.remove(p)
            except OSError:
                pass
        self.preview_files.append(path)
        self.last_preview = path
        self.previewed = True
        self.status.set(f"미리보기: {label} · 앞 {PREVIEW_CONTEXT:g}초 → 디졸브 → 뒤 {PREVIEW_CONTEXT:g}초 (저화질, 소리 없음)")
        self._refresh_buttons()
        _open_path(path)

    # ---------------- 결과 ----------------
    def _open_output(self):
        if self.last_output and self.last_output.exists():
            self.played = True
            _open_path(self.last_output)
            self._update_steps()

    def _open_folder(self):
        if self.last_output and self.last_output.exists():
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(self.last_output)])
            else:  # pragma: no cover
                _open_path(self.last_output.parent)

    def _send_to_long(self):
        if self.last_output and self.last_output.exists() and self.send_to_long_cb:
            self.send_to_long_cb(self.last_output)

    # ---------------- 닫기 ----------------
    def _on_close(self):
        if self.running and not messagebox.askyesno("제작 중", "배경 영상 제작을 중단하고 창을 닫을까요?", parent=self):
            return
        self.destroy()

    def destroy(self):
        self.cancel.set()
        self.stop_loading.set()
        self.preview_cancel.set()
        self.measure_cancel.set()
        self.silent_cancel.set()
        if self._length_job is not None:
            try:
                self.after_cancel(self._length_job)
            except tk.TclError:
                pass
            self._length_job = None
        for t in self._threads:  # FFmpeg를 남기지 않도록 잠깐 기다린다 (작업 스레드가 FFmpeg를 종료)
            t.join(timeout=8)
        for p in self.preview_files:  # 이 창이 만든 미리보기 파일만 지운다
            try:
                p.unlink()
            except OSError:
                pass
        try:
            super().destroy()
        finally:
            release_tk_variables(self)


def _gb(n: float) -> str:
    return f"{n / 1024**3:.2f} GB"


def _mb(n: float) -> str:
    return f"{n / 1024**2:,.0f} MB" if n < 1024**3 else _gb(n)


def _open_path(p: Path):
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:  # pragma: no cover
            subprocess.Popen(["xdg-open", str(p)])
    except OSError as e:
        messagebox.showerror("열기", str(e))
