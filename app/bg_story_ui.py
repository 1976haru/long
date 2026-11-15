"""④ [＋ 주인공 이미지 넣기] 창 (통합 08 + UX HOTFIX) — 초보자 3단계.

① 주인공 사진 고르기 (JPG·PNG·WebP) → ② 장면 분위기 카드 고르기 → ③ 큰 16:9 미리보기 확인 후 [✓ 이 모습으로 적용].
숫자 설정·간격 적용·프리셋/사진 관리는 [고급 설정]에 접어 둔다 (기능은 그대로).

- 사진은 실제 알파로 판단한다: 배경이 투명한 인물은 바로 미리보기. 배경이 남아 있는 사진(JPG·불투명 PNG)은 오류로
  멈추지 않고 화면 안에서 [배경 제거하기] / [사진 그대로 넣기] / [다른 사진 선택]을 고르게 한다.
  배경 제거는 검증된 로컬 모델이 있을 때만 동작하고, 없으면 버튼을 비활성으로 두고 이유를 보여 준다 (app/bg_remove.py).
- 미리보기는 실제 제작과 같은 해상도·필터·등장 시간으로 만든 화면을 줄인 것 (합성 좌표 동일). 백그라운드 스레드에서
  만들고, 화면 갱신은 이벤트 큐로 Tk main thread에서만 한다. 사진·분위기·위치를 바꾸면 자동으로 다시 만든다.
- 오류·안내는 모달 창보다 화면 안 안내 줄을 먼저 쓴다.
"""
from __future__ import annotations

import base64
import queue
import shutil
import tempfile
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from . import bg_remove as R
from . import bg_story as S
from .bg_core import GUARD_OWNER, fit_preview_png, make_story_preview, story_preview_png, story_preview_time
from .core import BuildCancelled
from .tooling import FFMPEG_GUARD, release_tk_variables
from .ui_scroll import ScrollFrame

TITLE = "④ 주인공 이미지 넣기"
IMAGE_TYPES = [("사진 (JPG·PNG·WebP)", "*.jpg *.jpeg *.png *.webp *.bmp"), ("모든 파일", "*.*")]
STEPS = "① 주인공 사진 고르기  →  ② 장면 분위기 고르기  →  ③ 미리보기 확인 후 [✓ 이 모습으로 적용]"
CARD_SHORT = {
    "christmas_couple": "거리 풍경이 보이게\n한쪽에 · 페이드",
    "fireworks_couple": "화면 아래 뒷모습\n위쪽 불꽃 하늘은 비움",
    "tokyo_night": "옆쪽 인물\n아주 느린 확대",
    "cafe_story": "왼쪽 아래 상반신\n따뜻한 색감",
}
SIZE_STEP = 0.06
NOTE_COLORS = {"ok": "#1d7a3a", "warn": "#9a5b00", "error": "#b3261e", "info": "#333333"}

# 고급 설정 숫자 입력: (변수 이름, 화면 이름, 최소, 최대, 간격, 화면 단위 → StoryStyle 값 배율)
NUMBERS = (
    ("size", "크기 (화면 높이 %)", 15, 110, 1, 0.01),
    ("lift", "하단 높이 % (−: 아래로)", -50, 50, 1, 0.01),
    ("margin", "가장자리 여백 %", 0, 30, 1, 0.01),
    ("opacity", "불투명도 %", 10, 100, 5, 0.01),
    ("fade", "페이드인·아웃 (초)", 0, 5, 0.5, 1.0),
    ("duration", "한 장면 등장 (초)", 5, 600, 5, 1.0),
    ("delay", "등장 지연 (초)", 0, 60, 0.5, 1.0),
    ("brightness", "밝기 %", 30, 160, 5, 0.01),
    ("warmth", "색온도 (−차게/+따뜻)", -30, 30, 1, 0.01),
    ("saturation", "채도 %", 0, 160, 5, 0.01),
    ("feather", "가장자리 부드럽게 0~3", 0, 3, 1, 1.0),
)


def _fmt(v: float) -> str:
    return f"{v:g}" if abs(v - round(v)) > 1e-9 else str(int(round(v)))


class StoryDialog(tk.Toplevel):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner                     # BackgroundMakerWindow
        self.title(TITLE)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        width, height = min(1240, sw - 40), min(880, sh - 80)
        self.geometry(f"{width}x{height}")
        self.minsize(min(900, sw - 40), min(520, sh - 80))
        # 큰 16:9 미리보기: 화면 폭에 맞춰 (1366 폭 → 576×324, 1920 폭 → 768×432)
        self.pv_w = 768 if width >= 1200 else 576 if width >= 1080 else 480
        self.pv_h = self.pv_w * 9 // 16
        self.lib = S.StoryLibrary()
        self.presets = S.load_presets()
        self.remover = R.detect()
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.remove_cancel = threading.Event()
        self.busy = False          # 미리보기 만드는 중
        self.checking = False      # 사진 확인 중
        self.removing = False      # 배경 제거 중
        self._threads: list[threading.Thread] = []
        self.preview_files: list[Path] = []
        self.image: S.StoryImage | None = None
        self.photo_ok = False      # 배경이 남은 사진을 '사진 그대로 넣기'로 확정했는가
        self.thumb_img = None
        self.pv_imgs: dict[str, tk.PhotoImage] = {}
        self.pv_info: dict = {}
        self._pv_job = None
        self._pv_pending = False
        self._loading_style = False
        self._drag = None
        self.last_preview: Path | None = None
        self.preview_count = 0
        self.applied = 0
        self.notice_kind = "info"
        last = owner.story_last if isinstance(getattr(owner, "story_last", None), dict) else {}

        self.entry_id = tk.StringVar(value="")
        self.name = tk.StringVar()
        self.tags = {t: tk.BooleanVar(value=False) for t in S.TAGS}
        p0 = S.get_preset(last.get("preset") or S.DEFAULT_PRESET)
        self.preset_key = tk.StringVar(value=p0.key)
        self.preset = tk.StringVar(value=p0.name)        # 고급 설정 콤보 (이름)
        self.preset_desc = tk.StringVar()
        self.align = tk.StringVar(value=S.ALIGN_RIGHT)
        self.motion = tk.StringVar(value=S.SMOTION_LABELS[S.SMOTION_NONE])
        self.nums = {k: tk.StringVar() for k, *_ in NUMBERS}
        mode = last.get("mode") if last.get("mode") in S.APPLY_LABELS else S.DEFAULT_APPLY
        self.mode = tk.StringVar(value=mode)
        try:
            ev = int(last.get("every", S.DEFAULT_EVERY))
        except (TypeError, ValueError):
            ev = S.DEFAULT_EVERY
        self.every = tk.StringVar(value=str(max(2, min(10, ev))))
        self.scene = tk.StringVar()
        self.recent = tk.StringVar()
        self.show_before = tk.BooleanVar(value=False)
        self.show_adv = tk.BooleanVar(value=False)
        self.notice = tk.StringVar(value="① [사진 고르기]로 주인공 사진을 고르세요. JPG·PNG·WebP 모두 됩니다.")
        self.image_info = tk.StringVar(value="아직 고른 사진이 없습니다.")
        self.count_text = tk.StringVar()
        self.pv_caption = tk.StringVar(value="")
        self.remove_progress = tk.DoubleVar(value=0.0)
        self.protect_top = p0.style.protect_top
        self.scene_tag = p0.scene

        self._ui()
        self._fit_preview_size(width)
        self._load_style(p0.style)
        self._refresh_library()
        self._refresh_scenes()
        self._mode_changed()
        if last.get("entry") and self.lib.get(str(last.get("entry"))):
            self.use_entry(str(last["entry"]), load_style=False)
        self._wire_traces()
        self._update_buttons()
        if self.image is None:
            self._draw_placeholder()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(100, self._pump)

    # ================= 화면 =================
    def _ui(self):
        # 실행 버튼(적용)과 안내 줄은 항상 창 아래에 고정 — 작은 화면에서도 잘리지 않게
        bottom = ttk.Frame(self, padding=(12, 6, 12, 8))
        bottom.pack(side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        self.notice_label = tk.Label(bottom, textvariable=self.notice, anchor="w", justify="left", wraplength=1100,
                                     fg=NOTE_COLORS["info"], font="PLS.Strong")
        self.notice_label.pack(fill="x")
        self.notice_label.bind("<Configure>", lambda e: self.notice_label.configure(wraplength=max(300, e.width - 10)))
        ar = ttk.Frame(bottom); ar.pack(fill="x", pady=(6, 0))
        self.btn_apply = ttk.Button(ar, text="✓ 이 모습으로 적용", command=self.apply, style="Primary.TButton")
        self.btn_apply.pack(side="left", fill="x", expand=True, ipady=8)
        self.btn_close = ttk.Button(ar, text="닫기", command=self.destroy)
        self.btn_close.pack(side="left", padx=(8, 0), ipady=8)
        ttk.Label(bottom, textvariable=self.count_text, style="Hint.TLabel").pack(anchor="w", pady=(4, 0))

        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        root = ttk.Frame(self.scroll.body, padding=(12, 8))
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="주인공 이미지 넣기 — 움직이는 배경 위에 주인공 사진 겹치기", font="PLS.Title").pack(anchor="w")
        ttk.Label(root, text=STEPS, font="PLS.Strong").pack(anchor="w", pady=(2, 8))

        main = ttk.Frame(root)
        main.pack(fill="both", expand=True)
        left = self.left = ttk.Frame(main)
        left.pack(side="left", fill="y", anchor="n")
        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0), anchor="n")

        # ① 주인공 사진
        f1 = ttk.LabelFrame(left, text="① 주인공 사진", padding=8)
        f1.pack(fill="x")
        self.btn_pick = ttk.Button(f1, text="📂 사진 고르기 (JPG·PNG·WebP)", command=self._pick_image,
                                   style="Primary.TButton")
        self.btn_pick.pack(fill="x", ipady=4)
        rr = ttk.Frame(f1); rr.pack(fill="x", pady=(6, 0))
        ttk.Label(rr, text="최근 사진").pack(side="left")
        self.cb_recent = ttk.Combobox(rr, textvariable=self.recent, state="readonly", width=24)
        self.cb_recent.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.cb_recent.bind("<<ComboboxSelected>>", lambda e: self._pick_recent())
        tr = ttk.Frame(f1); tr.pack(fill="x", pady=(6, 0))
        self.thumb = ttk.Label(tr, text="(사진 없음)", anchor="center", width=12)
        self.thumb.pack(side="left")
        ttk.Label(tr, textvariable=self.image_info, wraplength=200, justify="left").pack(side="left", padx=(8, 0),
                                                                                       anchor="n")
        # 배경이 남아 있는 사진일 때만 보이는 선택지
        self.photo_box = ttk.Frame(f1)
        ttk.Label(self.photo_box, text="배경이 남아 있는 사진입니다. 어떻게 할까요?", font="PLS.Strong").pack(anchor="w")
        pb = ttk.Frame(self.photo_box); pb.pack(fill="x", pady=(4, 0))   # 세로로 쌓아 왼쪽 칸 폭이 늘지 않게
        self.btn_remove = ttk.Button(pb, text="배경 제거하기", command=self.remove_background)
        self.btn_remove.pack(fill="x")
        self.btn_as_is = ttk.Button(pb, text="사진 그대로 넣기 (네모난 사진)", command=self.use_photo_as_is)
        self.btn_as_is.pack(fill="x", pady=2)
        self.btn_other = ttk.Button(pb, text="다른 사진 선택", command=self._pick_image)
        self.btn_other.pack(fill="x")
        self.remove_note = ttk.Label(self.photo_box, text="" if self.remover else R.UNAVAILABLE_NOTE,
                                     style="Hint.TLabel", wraplength=320, justify="left")
        self.remove_note.pack(anchor="w", pady=(4, 0))
        self.remove_row = ttk.Frame(self.photo_box)
        ttk.Progressbar(self.remove_row, variable=self.remove_progress, maximum=1.0).pack(side="left", fill="x",
                                                                                        expand=True)
        ttk.Button(self.remove_row, text="취소", command=self.remove_cancel.set).pack(side="left", padx=(6, 0))

        # ② 장면 분위기 (카드형)
        f2 = ttk.LabelFrame(left, text="② 장면 분위기", padding=8)
        f2.pack(fill="x", pady=(8, 0))
        cards = ttk.Frame(f2); cards.pack(fill="x")
        self.cards: dict[str, tk.Radiobutton] = {}
        for i, p in enumerate(S.BUILTIN_PRESETS):
            b = tk.Radiobutton(cards, text=f"{p.name}\n{CARD_SHORT.get(p.key, '')}", value=p.key,
                               variable=self.preset_key, indicatoron=0, command=self.choose_preset,
                               height=3, justify="center", relief="raised", bd=2, selectcolor="#cfe0ff",
                               padx=4, pady=4)
            b.grid(row=i // 2, column=i % 2, padx=3, pady=3, sticky="nsew")
            self.cards[p.key] = b
        cards.columnconfigure(0, weight=1, uniform="card")
        cards.columnconfigure(1, weight=1, uniform="card")
        ttk.Label(f2, text="고르면 위치·크기·페이드·밝기가 자동으로 맞춰집니다 (숫자 입력 필요 없음).",
                  style="Hint.TLabel", wraplength=320, justify="left").pack(anchor="w", pady=(4, 0))

        # 넣을 장면 (기본: 모든 장면)
        f3 = ttk.LabelFrame(left, text="넣을 장면", padding=8)
        f3.pack(fill="x", pady=(8, 0))
        mr = ttk.Frame(f3); mr.pack(fill="x")
        ttk.Radiobutton(mr, text="모든 장면 (기본)", value=S.APPLY_ALL, variable=self.mode,
                        command=self._mode_changed).pack(side="left")
        ttk.Radiobutton(mr, text="선택한 장면만", value=S.APPLY_SELECTED, variable=self.mode,
                        command=self._mode_changed).pack(side="left", padx=(10, 0))
        self.scene_box = ttk.Frame(f3)
        self.lst_scenes = tk.Listbox(self.scene_box, height=4, selectmode="multiple", exportselection=False, width=38)
        self.lst_scenes.pack(fill="x")
        self.lst_scenes.bind("<<ListboxSelect>>", lambda e: self._update_count())
        ttk.Label(self.scene_box, text="넣을 장면을 눌러 고르세요 (여러 개 가능).", style="Hint.TLabel").pack(anchor="w")

        # ③ 미리보기
        f4 = ttk.LabelFrame(right, text="③ 미리보기 — 실제 제작과 같은 화면을 줄여서 보여 줍니다", padding=8)
        f4.pack(fill="both", expand=True)
        sr = ttk.Frame(f4); sr.pack(fill="x")
        ttk.Label(sr, text="장면").pack(side="left")
        self.cb_scene = ttk.Combobox(sr, textvariable=self.scene, state="readonly", width=24)
        self.cb_scene.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.cb_scene.bind("<<ComboboxSelected>>", lambda e: self.schedule_preview(50))
        self.canvas = tk.Canvas(f4, width=self.pv_w, height=self.pv_h, bg="#202020", highlightthickness=0,
                                takefocus=0)
        self.canvas.pack(pady=(6, 0), anchor="w")
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.pv_caption_label = ttk.Label(f4, textvariable=self.pv_caption, style="Hint.TLabel",
                                          wraplength=self.pv_w, justify="left")
        self.pv_caption_label.pack(anchor="w", pady=(2, 0))
        br = ttk.Frame(f4); br.pack(fill="x", pady=(4, 0))
        ttk.Label(br, text="위치").pack(side="left", padx=(0, 4))
        self.btn_left = ttk.Button(br, text="◀ 왼쪽", command=lambda: self.set_align(S.ALIGN_LEFT))
        self.btn_center = ttk.Button(br, text="가운데", command=lambda: self.set_align(S.ALIGN_CENTER))
        self.btn_right = ttk.Button(br, text="오른쪽 ▶", command=lambda: self.set_align(S.ALIGN_RIGHT))
        for b in (self.btn_left, self.btn_center, self.btn_right):
            b.pack(side="left", padx=(0, 4))
        br2 = ttk.Frame(f4); br2.pack(fill="x", pady=(4, 0))
        ttk.Label(br2, text="크기").pack(side="left", padx=(0, 4))
        self.btn_bigger = ttk.Button(br2, text="＋ 크게", command=lambda: self.resize(+1))
        self.btn_smaller = ttk.Button(br2, text="－ 작게", command=lambda: self.resize(-1))
        for b in (self.btn_bigger, self.btn_smaller):
            b.pack(side="left", padx=(0, 4))
        self.chk_before = ttk.Checkbutton(br2, text="합성 전 보기", variable=self.show_before, command=self._redraw)
        self.chk_before.pack(side="left", padx=(8, 0))
        mr2 = ttk.Frame(f4); mr2.pack(fill="x", pady=(4, 0))
        self.btn_motion = ttk.Button(mr2, text="▶ 움직임·페이드 보기 (짧은 영상)", command=self.preview_motion)
        self.btn_motion.pack(side="left")
        self.drag_hint = ttk.Label(f4, text="노란 상자(인물)를 마우스로 끌어 옮길 수 있습니다.", style="Hint.TLabel",
                                   wraplength=self.pv_w, justify="left")
        self.drag_hint.pack(anchor="w", pady=(2, 0))

        # 고급 설정 (기본 접힘)
        self.adv_toggle = ttk.Checkbutton(root, text="고급 설정 펼치기 (숫자 조정 · N장면마다 · 프리셋·사진 관리)",
                                          variable=self.show_adv, command=self._toggle_adv)
        self.adv_toggle.pack(anchor="w", pady=(10, 0))
        self.adv = ttk.Frame(root)
        self._advanced_ui(self.adv)

    def _fit_preview_size(self, width: int):
        """왼쪽 칸이 실제로 차지하는 폭(글꼴·Windows 배율에 따라 달라짐)을 재고 남는 폭에 맞춰 16:9 미리보기 크기를 정한다."""
        try:
            self.update_idletasks()
            room = width - self.left.winfo_reqwidth() - 90      # 여백·테두리·스크롤바
        except tk.TclError:
            return
        pv = max(384, min(self.pv_w, int(room) // 16 * 16))
        if pv != self.pv_w:
            self.pv_w, self.pv_h = pv, pv * 9 // 16
            self.canvas.configure(width=self.pv_w, height=self.pv_h)
            self.pv_caption_label.configure(wraplength=self.pv_w)
            self.drag_hint.configure(wraplength=self.pv_w)

    def _advanced_ui(self, adv):
        a1 = ttk.LabelFrame(adv, text="세부 조정 (바꾸면 미리보기가 자동으로 다시 만들어집니다)", padding=6)
        a1.pack(fill="x", pady=(4, 0))
        ar = ttk.Frame(a1); ar.pack(fill="x")
        ttk.Label(ar, text="위치").pack(side="left")
        for k in S.ALIGNS:
            ttk.Radiobutton(ar, text=S.ALIGN_LABELS[k], value=k, variable=self.align).pack(side="left", padx=2)
        ttk.Label(ar, text="   움직임").pack(side="left")
        ttk.Combobox(ar, textvariable=self.motion, state="readonly", width=16,
                     values=[S.SMOTION_LABELS[m] for m in S.SMOTIONS]).pack(side="left", padx=4)
        grid = ttk.Frame(a1); grid.pack(fill="x", pady=(4, 0))
        for i, (k, label, lo, hi, step, _) in enumerate(NUMBERS):
            r, c = divmod(i, 3)
            ttk.Label(grid, text=label).grid(row=r, column=c * 2, sticky="w", padx=(0 if c == 0 else 12, 2), pady=1)
            ttk.Spinbox(grid, textvariable=self.nums[k], from_=lo, to=hi, increment=step, width=6).grid(
                row=r, column=c * 2 + 1, sticky="w")
        a2 = ttk.LabelFrame(adv, text="장면 간격", padding=6)
        a2.pack(fill="x", pady=(6, 0))
        ir = ttk.Frame(a2); ir.pack(fill="x")
        ttk.Radiobutton(ir, text="일정 간격 (N장면마다)", value=S.APPLY_INTERVAL, variable=self.mode,
                        command=self._mode_changed).pack(side="left")
        ttk.Label(ir, text="  N").pack(side="left")
        self.sp_every = ttk.Spinbox(ir, textvariable=self.every, from_=2, to=10, increment=1, width=4)
        self.sp_every.pack(side="left", padx=4)
        ttk.Label(a2, text="N=2: 한 장면 걸러 1번. 장면마다 켜고 끄기는 ④ 목록의 [👤 선택 장면 주인공 켜기/끄기].",
                  style="Hint.TLabel", wraplength=900, justify="left").pack(anchor="w")
        a3 = ttk.LabelFrame(adv, text="프리셋 관리 (기본 프리셋은 복제해서 수정)", padding=6)
        a3.pack(fill="x", pady=(6, 0))
        pr = ttk.Frame(a3); pr.pack(fill="x")
        ttk.Label(pr, text="프리셋").pack(side="left")
        self.cb_preset = ttk.Combobox(pr, textvariable=self.preset, state="readonly", width=26)
        self.cb_preset.pack(side="left", padx=4)
        self.cb_preset.bind("<<ComboboxSelected>>", lambda e: self._preset_by_name())
        ttk.Button(pr, text="복제", command=self.duplicate).pack(side="left")
        ttk.Button(pr, text="지금 값을 내 프리셋으로 저장", command=self.save_as_preset).pack(side="left", padx=4)
        self.btn_del_preset = ttk.Button(pr, text="삭제", command=self.delete_user_preset)
        self.btn_del_preset.pack(side="left")
        ttk.Label(a3, textvariable=self.preset_desc, style="Hint.TLabel", wraplength=900,
                  justify="left").pack(anchor="w", pady=(4, 0))
        a4 = ttk.LabelFrame(adv, text="내 주인공 사진 관리 (사본은 설정 폴더에 보관 · 원본은 그대로)", padding=6)
        a4.pack(fill="x", pady=(6, 0))
        lr = ttk.Frame(a4); lr.pack(fill="x")
        self.lst = tk.Listbox(lr, height=5, exportselection=False, width=38)
        self.lst.pack(side="left", fill="x")
        self.lst.bind("<<ListboxSelect>>", lambda e: self._on_list_pick())
        side = ttk.Frame(lr); side.pack(side="left", padx=(8, 0), fill="x", expand=True)
        nr = ttk.Frame(side); nr.pack(fill="x")
        ttk.Label(nr, text="이름").pack(side="left")
        ttk.Entry(nr, textvariable=self.name, width=22).pack(side="left", padx=(4, 8))
        tg = ttk.Frame(side); tg.pack(fill="x", pady=(2, 0))
        ttk.Label(tg, text="태그").pack(side="left")
        for t in S.TAGS:
            ttk.Checkbutton(tg, text=t, variable=self.tags[t]).pack(side="left", padx=(4, 0))
        bb = ttk.Frame(side); bb.pack(fill="x", pady=(4, 0))
        self.btn_meta = ttk.Button(bb, text="이름·태그 저장", command=self.save_meta)
        self.btn_meta.pack(side="left")
        self.btn_unlist = ttk.Button(bb, text="목록에서 빼기", command=self.unlist)
        self.btn_unlist.pack(side="left", padx=4)
        self.btn_remember = ttk.Button(bb, text="이 사진에 위치·크기 기억", command=self.remember_placement)
        self.btn_remember.pack(side="left")
        ttk.Button(adv, text="모든 장면에서 주인공 빼기", command=self.clear_all).pack(anchor="w", pady=(6, 0))

    def _toggle_adv(self):
        if self.show_adv.get():
            self.adv.pack(fill="x")
            self.adv_toggle.configure(text="고급 설정 접기")
        else:
            self.adv.pack_forget()
            self.adv_toggle.configure(text="고급 설정 펼치기 (숫자 조정 · N장면마다 · 프리셋·사진 관리)")

    def say(self, text: str, kind: str = "info"):
        """화면 아래 안내 줄 (모달 오류 대신)."""
        self.notice.set(text)
        self.notice_kind = kind
        try:
            self.notice_label.configure(fg=NOTE_COLORS.get(kind, NOTE_COLORS["info"]))
        except tk.TclError:
            pass

    # ================= 값 ↔ 화면 =================
    def _wire_traces(self):
        for v in list(self.nums.values()) + [self.align, self.motion]:
            v.trace_add("write", lambda *a: self.schedule_preview(700))
        self.every.trace_add("write", lambda *a: self._update_count())

    def _load_style(self, st: S.StoryStyle):
        st = st.clamped()
        self._loading_style = True
        try:
            self.align.set(st.align)
            self.motion.set(S.SMOTION_LABELS[st.motion])
            for k, _, _, _, _, mul in NUMBERS:
                self.nums[k].set(_fmt(round(getattr(st, k) / mul, 2)))
        finally:
            self._loading_style = False
        self.protect_top = st.protect_top
        self._refresh_presets()

    def style(self) -> S.StoryStyle:
        """화면 값 → StoryStyle (잘못 입력한 숫자는 범위 안으로/기본값으로)."""
        vals = {}
        for k, _, _, _, _, mul in NUMBERS:
            try:
                vals[k] = float(self.nums[k].get().replace("%", "").strip()) * mul
            except ValueError:
                vals[k] = getattr(S.StoryStyle(), k)
        vals["feather"] = int(round(vals["feather"]))
        motion = next((k for k, v in S.SMOTION_LABELS.items() if v == self.motion.get()), S.SMOTION_NONE)
        return S.StoryStyle(align=self.align.get(), motion=motion, protect_top=self.protect_top, **vals).clamped()

    def current_preset(self) -> S.StoryPreset:
        key = self.preset_key.get()
        return next((p for p in self.presets if p.key == key), self.presets[0])

    def _preset_by_name(self):
        p = next((x for x in self.presets if x.name == self.preset.get()), None)
        if p is not None:
            self.choose_preset(p.key)

    def _refresh_presets(self):
        try:
            self.cb_preset.configure(values=[p.name for p in self.presets])
        except tk.TclError:
            return
        p = self.current_preset()
        self.preset.set(p.name)
        self.preset_desc.set(("[기본] " if p.builtin else "[내 프리셋] ") + (p.desc or ""))
        self.btn_del_preset.configure(state="disabled" if p.builtin else "normal")

    def entry(self) -> S.LibraryEntry | None:
        return self.lib.get(self.entry_id.get()) if self.entry_id.get() else None

    def layer(self) -> S.StoryLayer | None:
        """지금 미리보기·적용에 쓸 주인공. 배경이 남은 사진은 '사진 그대로 넣기'를 고른 뒤에만."""
        if self.image is None or (self.image.is_photo and not self.photo_ok):
            return None
        return S.StoryLayer(self.image, self.style(), self.current_preset().name)

    # ================= ① 사진 =================
    def _pick_image(self):
        p = filedialog.askopenfilename(parent=self, title="주인공 사진 고르기 (JPG·PNG·WebP)", filetypes=IMAGE_TYPES)
        if p:
            self.select_path(Path(p))

    def select_path(self, path: Path, name: str = "") -> bool:
        """사진 확인 → 투명 인물이면 바로 미리보기(라이브러리에 자동 보관), 배경이 남은 사진이면 화면 안 선택지."""
        if self.checking or self.removing:
            return False
        path = Path(path)
        if path.suffix.lower() not in S.STORY_EXTS:
            self.say(f"'{path.name}'은(는) 쓸 수 없는 형식입니다. JPG·PNG·WebP 사진을 고르세요.", "error")
            return False
        tools = self._tools()
        if not tools:
            return False
        self.checking = True
        self.say(f"사진 확인 중… {path.name}")
        self._update_buttons()
        self._spawn(self._inspect_worker, tools, path, name)
        return True

    register_path = select_path      # 통합 08 이름 호환

    def _inspect_worker(self, tools, path, name):
        ff, fp = tools
        try:
            img = S.inspect_image(ff, fp, path, name)
            thumb = base64.b64encode(S.thumb_png(ff, img)).decode("ascii")
            self.events.put(("inspected", img, thumb))
        except Exception as e:
            self.events.put(("inspect_error", str(e)))

    def _image_ready(self, img: S.StoryImage, thumb_b64: str | None):
        self.checking = False
        self._show_thumb(thumb_b64)
        if img.is_photo:
            self.image = img           # 원본은 읽기만 (미리보기용) — 확정하면 라이브러리에 사본
            self.photo_ok = False
            self.entry_id.set("")
            self.image_info.set(f"{img.name}\n{img.width}×{img.height} · 배경이 남아 있는 사진")
            self.photo_box.pack(fill="x", pady=(8, 0))
            self.pv_imgs = {}
            self.pv_info = {}
            self.pv_caption.set("")
            self.say("⚠ " + S.BG_REMOVAL_NOTE, "warn")
            self._draw_placeholder("배경이 남아 있는 사진입니다.\n왼쪽에서 [사진 그대로 넣기] 또는 [다른 사진 선택]을 고르세요.")
        else:
            e = self.lib.add(img, img.name, self._default_tags())
            self._refresh_library()
            self.use_entry(e.id, load_style=False, thumb=False)
            self.say("✓ 배경이 투명한 인물 사진입니다. ② 분위기를 고르고 미리보기를 확인한 뒤 [✓ 이 모습으로 적용]을 누르세요.",
                     "ok")
        self._update_buttons()

    def _default_tags(self) -> list[str]:
        tags = [t for t, v in self.tags.items() if v.get()]
        return tags or ([self.scene_tag] if self.scene_tag in S.TAGS else [])

    def use_entry(self, entry_id: str, load_style: bool = True, thumb: bool = True) -> bool:
        """라이브러리 사진 사용 (최근 사진 / 관리 목록 / 새로 등록)."""
        e = self.lib.get(entry_id)
        if e is None or not self.lib.available(e):
            self.say("이 사진의 보관 사본이 없습니다. [사진 고르기]로 다시 골라 주세요.", "warn")
            return False
        self.entry_id.set(e.id)
        self.image = self.lib.image_of(e)
        self.photo_ok = self.image.is_photo        # 목록에 있는 사진은 이미 '그대로 넣기'로 확정한 것
        self.photo_box.pack_forget()
        self.name.set(e.name)
        for t, v in self.tags.items():
            v.set(t in e.tags)
        if load_style and e.style:                 # 이 사진에 기억한 위치·크기
            fav = S.StoryStyle.from_dict(e.style)
            self._load_style(replace(self.style(), size=fav.size, align=fav.align, margin=fav.margin, lift=fav.lift))
        kind = "네모난 사진 그대로" if self.image.is_photo else "배경이 투명한 인물"
        self.image_info.set(f"{e.name}\n{e.width}×{e.height} · {kind}")
        names = self._recent_names()
        self.recent.set(names[[x.id for x in self.lib.entries].index(e.id)])
        self._sync_list_selection()
        if thumb:
            self._thumb(e)
        self._update_buttons()
        self.schedule_preview(50)
        return True

    def use_photo_as_is(self) -> bool:
        if self.image is None or not self.image.is_photo:
            return False
        e = self.lib.add(self.image, self.image.name, self._default_tags())
        self._refresh_library()
        self.use_entry(e.id, load_style=False, thumb=False)
        self.say(S.PHOTO_NOTE, "info")
        return True

    def remove_background(self) -> bool:
        if self.remover is None:
            self.say(R.UNAVAILABLE_NOTE, "warn")
            return False
        if self.image is None or self.removing:
            return False
        tools = self._tools()
        if not tools:
            return False
        self.removing = True
        self.remove_cancel.clear()
        self.remove_progress.set(0.0)
        self.remove_row.pack(fill="x", pady=(4, 0))
        self.say(f"배경 제거 중… ({self.remover.name} · 이 PC에서만 처리, 원본은 그대로)")
        self._update_buttons()
        self._spawn(self._remove_worker, tools, self.image.path)
        return True

    def _remove_worker(self, tools, src):
        ff, fp = tools
        out = Path(tempfile.gettempdir()) / "PLVM_STORY_BGREMOVE" / uuid.uuid4().hex[:8]
        try:
            img = R.remove_background(self.remover, ff, fp, src, out, self.remove_cancel,
                                      lambda f: self.events.put(("remove_progress", f)))
            thumb = base64.b64encode(S.thumb_png(ff, img)).decode("ascii")
            self.events.put(("removed", img, thumb, out))
        except BuildCancelled:
            self.events.put(("remove_error", "배경 제거를 취소했습니다 (원본은 그대로).", out))
        except Exception as e:
            self.events.put(("remove_error", str(e), out))

    def _pick_recent(self):
        names = self._recent_names()
        if self.recent.get() in names:
            self.use_entry(self.lib.entries[names.index(self.recent.get())].id)

    def _recent_names(self) -> list[str]:
        return [("🖼 " if e.kind == S.KIND_PHOTO else "👤 ") + e.name + ("" if self.lib.available(e) else " (사본 없음)")
                for e in self.lib.entries]

    def _refresh_library(self):
        names = self._recent_names()
        self.cb_recent.configure(values=names)
        self.lst.delete(0, "end")
        for e, n in zip(self.lib.entries, names):
            self.lst.insert("end", n + (f"  [{', '.join(e.tags)}]" if e.tags else ""))
        self._sync_list_selection()

    def _sync_list_selection(self):
        ids = [e.id for e in self.lib.entries]
        self.lst.selection_clear(0, "end")
        if self.entry_id.get() in ids:
            self.lst.selection_set(ids.index(self.entry_id.get()))

    def _on_list_pick(self):
        sel = self.lst.curselection()
        if sel and sel[0] < len(self.lib.entries):
            self.use_entry(self.lib.entries[sel[0]].id)

    def _thumb(self, e: S.LibraryEntry):
        tools = self._tools(quiet=True)
        if tools and self.lib.available(e):
            self._spawn(self._thumb_worker, tools[0], self.lib.image_of(e), e.id)

    def _thumb_worker(self, ff, img, eid):
        try:
            self.events.put(("thumb", eid, base64.b64encode(S.thumb_png(ff, img)).decode("ascii")))
        except Exception:
            pass

    def _show_thumb(self, b64: str | None):
        if not b64:
            return
        try:
            self.thumb_img = tk.PhotoImage(master=self, data=b64)
            self.thumb.configure(image=self.thumb_img, text="")
        except tk.TclError:
            pass

    def save_meta(self) -> bool:
        e = self.entry()
        if e is None:
            return False
        self.lib.update(e.id, name=self.name.get(), tags=[t for t, v in self.tags.items() if v.get()])
        self._refresh_library()
        self.say(f"저장했습니다: {self.name.get()}", "ok")
        return True

    def unlist(self) -> bool:
        e = self.entry()
        if e is None or not messagebox.askyesno("목록에서 빼기", f"'{e.name}'을(를) 목록에서 뺄까요?\n"
                                                "(원본 사진과 설정 폴더의 사본 파일은 지우지 않습니다.)", parent=self):
            return False
        self.lib.remove(e.id)
        self.entry_id.set("")
        self.image = None
        self.thumb_img = None
        self.thumb.configure(image="", text="(사진 없음)")
        self.image_info.set("아직 고른 사진이 없습니다.")
        self.pv_imgs, self.pv_info = {}, {}
        self._refresh_library()
        self._update_buttons()
        self._draw_placeholder()
        return True

    def remember_placement(self) -> bool:
        e = self.entry()
        if e is None:
            return False
        self.lib.update(e.id, style=self.style(), preset=self.current_preset().key)
        self.say(f"'{e.name}'의 위치·크기를 기억했습니다 (다음에 이 사진을 고르면 자동으로 적용).", "ok")
        return True

    # ================= ② 분위기 =================
    def choose_preset(self, key: str | None = None):
        if key:
            self.preset_key.set(key)
        p = self.current_preset()
        self.scene_tag = p.scene
        self._load_style(p.style)
        self.schedule_preview(50)

    def duplicate(self) -> S.StoryPreset:
        p = S.duplicate_preset(self.current_preset().key)
        self.presets = S.load_presets()
        self.preset_key.set(p.key)
        self._refresh_presets()
        self.say(f"'{p.name}'을(를) 만들었습니다. 값을 바꾼 뒤 [지금 값을 내 프리셋으로 저장]을 누르면 수정됩니다.", "ok")
        return p

    def save_as_preset(self) -> S.StoryPreset:
        cur = self.current_preset()
        if cur.builtin:      # 기본 프리셋은 그대로 두고 새 내 프리셋으로
            p = S.save_preset(f"{cur.name} (내 설정)", self.style(), cur.scene, cur.desc)
        else:
            p = S.save_preset(cur.name, self.style(), cur.scene, cur.desc, key=cur.key)
        self.presets = S.load_presets()
        self.preset_key.set(p.key)
        self._refresh_presets()
        self.say(f"내 프리셋 저장: {p.name}", "ok")
        return p

    def delete_user_preset(self) -> bool:
        p = self.current_preset()
        if p.builtin or not messagebox.askyesno("내 프리셋 삭제", f"'{p.name}'을(를) 삭제할까요?", parent=self):
            return False
        S.delete_preset(p.key)
        self.presets = S.load_presets()
        self.preset_key.set(self.presets[0].key)
        self._refresh_presets()
        return True

    # ================= 장면 =================
    def _scene_names(self):
        return [(i, c) for i, c in enumerate(self.owner.clips) if c.enabled]

    def _refresh_scenes(self):
        self.lst_scenes.delete(0, "end")
        items = self._scene_names()
        sel_owner = self.owner._selected_index()
        names = []
        for k, (i, c) in enumerate(items):
            name = f"장면 {k + 1} · {'🖼 ' if c.is_image else ''}{c.path.name} ({c.used_length:.0f}초)" + (
                " 👤" if c.story else "")
            names.append(name)
            self.lst_scenes.insert("end", name)
            if c.story is not None or i == sel_owner:
                self.lst_scenes.selection_set(k)
        self.cb_scene.configure(values=names)
        if names and self.scene.get() not in names:
            pick = next((k for k, (i, _) in enumerate(items) if i == sel_owner), 0)
            self.scene.set(names[pick])
        if not names:
            self.scene.set("")
        self._update_count()

    def _mode_changed(self):
        if self.mode.get() == S.APPLY_SELECTED:
            self.scene_box.pack(fill="x", pady=(6, 0))
        else:
            self.scene_box.pack_forget()
        self._update_count()
        self._update_buttons()

    def selected_scenes(self) -> list[int]:
        """'선택한 장면만'에서 고른 장면의 ④ 목록 위치."""
        items = self._scene_names()
        return [items[k][0] for k in self.lst_scenes.curselection() if k < len(items)]

    def every_value(self) -> int:
        try:
            return max(2, min(10, int(self.every.get())))
        except ValueError:
            return S.DEFAULT_EVERY

    def planned_count(self) -> tuple[int, int, int]:
        """(넣을 장면 수, 사용 중 장면 수, 그중 너무 짧아 빠지는 수) — 적용 전에 보여 줌."""
        dummy = self.layer() or S.StoryLayer(S.StoryImage(Path("x.png"), 10, 10, (0, 0, 10, 10)), self.style())
        clips = S.assign_story(self.owner.clips, dummy, self.mode.get(), self.selected_scenes(), self.every_value())
        on = [c for c in clips if c.story is not None]
        try:
            fps = self.owner.current_spec().fps
        except ValueError:
            fps = 30.0
        d = self.owner.effective_dissolve()[0]
        short = sum(1 for c in on if S.clip_window(c, d, fps) is None)
        return len(on), sum(1 for c in clips if c.enabled), short

    def _update_count(self):
        try:
            n, total, short = self.planned_count()
        except Exception:
            return
        if not total:
            self.count_text.set("④ 창에서 배경 영상을 먼저 불러오세요.")
            return
        self.count_text.set(f"적용 예정: {n}개 장면 (사용 중인 장면 {total}개)"
                            + (f" · 그중 {short}개는 너무 짧아 주인공이 나오지 않음" if short else ""))

    def preview_clip(self):
        """미리보기할 장면 (지금 설정 적용). 넣지 않기로 한 장면이어도 미리보기는 보여 준다."""
        layer = self.layer()
        items = self._scene_names()
        names = list(self.cb_scene.cget("values") or ())
        if layer is None or not items:
            return None
        k = names.index(self.scene.get()) if self.scene.get() in names else 0
        return replace(items[min(k, len(items) - 1)][1], story=layer)

    # ================= ③ 미리보기 =================
    def _tools(self, quiet: bool = False):
        try:
            ff, fp = self.owner.tools()
        except Exception:
            ff = fp = None
        if not ff or not fp:
            if not quiet:
                self.say("FFmpeg를 찾지 못했습니다. 메인 화면의 [FFmpeg 설정]에서 지정하세요.", "error")
            return None
        return Path(ff), Path(fp)

    def _draw_placeholder(self, text: str | None = None):
        try:
            self.canvas.delete("all")
            if text is None:
                text = ("① 주인공 사진을 고르면 여기에 합성 결과가 크게 나옵니다." if self.owner.clips else
                        "④ 창에서 배경 영상을 먼저 불러오세요.\n그다음 주인공 사진을 고르면 합성 결과가 여기에 나옵니다.")
            self.canvas.create_text(self.pv_w // 2, self.pv_h // 2, text=text, fill="#dddddd", justify="center",
                                    width=self.pv_w - 40)
        except tk.TclError:
            pass

    def schedule_preview(self, delay: int = 400):
        """설정이 바뀔 때마다 미리보기 예약 (연달아 바뀌면 마지막 것만)."""
        if self._loading_style:
            return
        self._update_count()
        if self._pv_job is not None:
            try:
                self.after_cancel(self._pv_job)
            except tk.TclError:
                pass
        try:
            self._pv_job = self.after(delay, self._run_preview)
        except tk.TclError:
            self._pv_job = None

    def _run_preview(self) -> bool:
        self._pv_job = None
        if self.busy:
            self._pv_pending = True
            return False
        clip = self.preview_clip()
        if clip is None:
            if self.image is not None and self.image.is_photo and not self.photo_ok:
                self._draw_placeholder("배경이 남아 있는 사진입니다.\n왼쪽에서 [사진 그대로 넣기] 또는 [다른 사진 선택]을 고르세요.")
            else:
                self._draw_placeholder()
            return False
        tools = self._tools()
        if not tools:
            return False
        try:
            spec = self.owner.current_spec()
        except ValueError as e:
            self.say(str(e), "warn")
            return False
        d = self.owner.effective_dissolve()[0]
        if S.clip_window(clip, d, spec.fps) is None:
            self.pv_imgs, self.pv_info = {}, {}
            self._draw_placeholder("이 장면은 너무 짧아 주인공이 나오지 않습니다 (장면 전환 바깥 2초 미만).\n위에서 다른 장면을 고르세요.")
            return False
        if self.owner.running or not FFMPEG_GUARD.try_acquire(GUARD_OWNER):
            self.pv_caption.set("다른 FFmpeg 작업(제작·미리보기) 중이라 미리보기를 잠시 뒤에 다시 만듭니다.")
            self.schedule_preview(1500)
            return False
        self.busy = True
        self.cancel.clear()
        self.pv_caption.set("미리보기 만드는 중…")
        self._update_buttons()
        self._spawn(self._preview_worker, tools[0], clip, spec, d, self.scene.get())
        return True

    def preview_still(self) -> bool:      # 통합 08 이름 호환: 지금 바로 미리보기
        if self._pv_job is not None:
            try:
                self.after_cancel(self._pv_job)
            except tk.TclError:
                pass
            self._pv_job = None
        return self._run_preview()

    def _preview_worker(self, ff, clip, spec, d, scene_name):
        try:
            at = story_preview_time(clip, d, spec.fps)
            size = (self.pv_w, self.pv_h)
            after = story_preview_png(ff, clip, spec, d, self.cancel, size=size, at=at)
            before = fit_preview_png(ff, replace(clip, story=None), spec, self.cancel, size=size, at=at, dissolve=d)
            self.events.put(("pv", base64.b64encode(after).decode("ascii"), base64.b64encode(before).decode("ascii"),
                             scene_name, spec, clip.story))
        except BuildCancelled:
            self.events.put(("pv_error", ""))
        except Exception as e:
            self.events.put(("pv_error", str(e)))
        finally:
            FFMPEG_GUARD.release(GUARD_OWNER)

    def _preview_ready(self, after_b64, before_b64, scene_name, spec, layer):
        self.busy = False
        self.preview_count += 1
        try:
            self.pv_imgs = {"after": tk.PhotoImage(master=self, data=after_b64),
                            "before": tk.PhotoImage(master=self, data=before_b64)}
        except tk.TclError:
            return
        # 상자 = 실제 출력 좌표(spec 해상도)를 미리보기 크기로 줄인 것 → 미리보기와 최종 합성 위치가 같다
        pl = S.placement(layer, spec.width, spec.height)
        k = self.pv_w / spec.width
        self.pv_info = {"spec": spec, "layer": layer,
                        "box": (pl.x * k, pl.y * k, (pl.x + pl.w) * k, (pl.y + pl.h) * k)}
        kind = " · 네모난 사진 그대로 (인물만 오려낸 합성 아님)" if layer.image.is_photo else ""
        self.pv_caption.set(f"표시 중: {scene_name} · 주인공이 완전히 보이는 순간 · {spec.width}×{spec.height} 제작 화면을 "
                            f"줄여서 표시{kind}")
        self._redraw()
        self._update_buttons()
        if self._pv_pending:
            self._pv_pending = False
            self.schedule_preview(50)

    def _redraw(self):
        try:
            self.canvas.delete("all")
            img = self.pv_imgs.get("before" if self.show_before.get() else "after")
            if img is None:
                self._draw_placeholder()
                return
            self.canvas.create_image(0, 0, image=img, anchor="nw")
            if self.show_before.get():
                self.canvas.create_text(10, 10, text="합성 전 (원래 장면)", anchor="nw", fill="#ffffff")
            elif "box" in self.pv_info:
                x0, y0, x1, y1 = self.pv_info["box"]
                self.canvas.create_rectangle(x0, y0, x1, min(y1, self.pv_h - 1), outline="#ffd400", dash=(4, 3),
                                             width=2, tags="box")
        except tk.TclError:
            pass

    # ---- 위치·크기 간단 조절 ----
    def set_align(self, align: str):
        self._loading_style = True
        self.align.set(align)
        if align != S.ALIGN_CENTER and self.style().margin < 0.02:
            self.nums["margin"].set("6")
        self._loading_style = False
        self.schedule_preview(50)

    def resize(self, step: int):
        st = self.style()
        self._loading_style = True
        self.nums["size"].set(_fmt(round(max(0.15, min(1.1, st.size + step * SIZE_STEP)) * 100)))
        self._loading_style = False
        self.schedule_preview(50)

    def _drag_start(self, ev):
        box = self.pv_info.get("box")
        if self.show_before.get() or not box or self.busy:
            return
        x0, y0, x1, y1 = box
        if x0 - 6 <= ev.x <= x1 + 6 and y0 - 6 <= ev.y <= y1 + 6:
            self._drag = (ev.x, ev.y, box)

    def _drag_move(self, ev):
        if not self._drag:
            return
        sx, sy, (x0, y0, x1, y1) = self._drag
        dx, dy = ev.x - sx, ev.y - sy
        self.canvas.delete("box")
        self.canvas.create_rectangle(x0 + dx, y0 + dy, x1 + dx, min(y1 + dy, self.pv_h - 1), outline="#00e0ff",
                                     dash=(4, 3), width=2, tags="box")

    def _drag_end(self, ev):
        if not self._drag:
            return
        sx, sy, (x0, y0, x1, y1) = self._drag
        self._drag = None
        dx, dy = ev.x - sx, ev.y - sy
        if abs(dx) < 3 and abs(dy) < 3:
            self._redraw()
            return
        self.move_to(x0 + dx, y0 + dy, x1 + dx, y1 + dy)

    def move_to(self, x0: float, y0: float, x1: float, y1: float):
        """미리보기 위 상자 위치(미리보기 화소) → 위치·여백·하단 높이 (화면 비율이라 출력 해상도와 무관)."""
        W, H = self.pv_w, self.pv_h
        cx = (x0 + x1) / 2 / W
        if cx < 0.4:
            align, margin = S.ALIGN_LEFT, max(0.0, x0 / W)
        elif cx > 0.6:
            align, margin = S.ALIGN_RIGHT, max(0.0, (W - x1) / W)
        else:
            align, margin = S.ALIGN_CENTER, self.style().margin
        lift = (H - y1) / H
        self._loading_style = True
        self.align.set(align)
        self.nums["margin"].set(_fmt(round(min(40.0, margin * 100))))
        self.nums["lift"].set(_fmt(round(max(-60.0, min(60.0, lift * 100)))))
        self._loading_style = False
        self.schedule_preview(50)

    def preview_motion(self) -> bool:
        if self.busy or self.owner.running:
            return False
        clip = self.preview_clip()
        if clip is None:
            self.say("주인공 사진과 ④ 목록의 배경 영상이 필요합니다.", "warn")
            return False
        tools = self._tools()
        if not tools:
            return False
        try:
            spec = self.owner.current_spec()
        except ValueError as e:
            self.say(str(e), "warn")
            return False
        d = self.owner.effective_dissolve()[0]
        if S.clip_window(clip, d, spec.fps) is None:
            self.say("이 장면은 너무 짧아 주인공이 나오지 않습니다. 다른 장면을 고르세요.", "warn")
            return False
        if not FFMPEG_GUARD.try_acquire(GUARD_OWNER):
            self.say("다른 FFmpeg 작업 중입니다. 끝난 뒤 다시 눌러 주세요.", "warn")
            return False
        self.busy = True
        self.cancel.clear()
        self.say("움직임·페이드 영상 만드는 중… (등장 1초 전부터 약 9초, 저화질)")
        self._update_buttons()
        out = Path(tempfile.gettempdir()) / "PLVM_BG_PREVIEW" / f"story_{uuid.uuid4().hex[:8]}.mp4"
        self._spawn(self._motion_worker, tools[0], clip, spec, d, out)
        return True

    def _motion_worker(self, ff, clip, spec, d, out):
        try:
            self.events.put(("motion", make_story_preview(ff, clip, spec, d, out, self.cancel)))
        except BuildCancelled:
            self.events.put(("pv_error", ""))
        except Exception as e:
            self.events.put(("pv_error", str(e)))
        finally:
            FFMPEG_GUARD.release(GUARD_OWNER)

    # ================= 적용 =================
    def apply(self) -> bool:
        if self.owner.running:
            self.say("배경 영상 제작 중에는 바꿀 수 없습니다.", "warn")
            return False
        layer = self.layer()
        if layer is None:
            if self.image is not None and self.image.is_photo:
                self.say("배경이 남아 있는 사진입니다. 왼쪽에서 [사진 그대로 넣기] 또는 [다른 사진 선택]을 먼저 골라 주세요.",
                         "warn")
            else:
                self.say("먼저 ① [사진 고르기]로 주인공 사진을 골라 주세요.", "warn")
            return False
        if not self.owner.clips:
            self.say("④ 창에서 배경 영상을 먼저 불러오세요.", "warn")
            return False
        mode = self.mode.get()
        sel = self.selected_scenes()
        if mode == S.APPLY_SELECTED and not sel:
            self.say("'선택한 장면만'을 고르셨습니다. 넣을 장면을 하나 이상 눌러 주세요.", "warn")
            return False
        n = self.owner.apply_story(layer, mode, sel, self.every_value(),
                                   last={"entry": self.entry_id.get(), "preset": self.current_preset().key,
                                         "mode": mode, "every": self.every_value()})
        e = self.entry()
        if e is not None:
            self.lib.update(e.id, preset=self.current_preset().key)
        self.applied = n
        try:  # ④ 배경 만들기 화면으로 돌아가 다음 단계(음악 길이 → 제작) 안내
            self.owner.status.set(f"👤 주인공을 {n}개 장면에 넣었습니다 ({layer.label}). 음악 길이를 확인하고 "
                                  "[⑦ 제작 시작]을 누르세요. 장면마다 켜고 끄기: [👤 선택 장면 주인공 켜기/끄기]")
        except (AttributeError, tk.TclError):
            pass
        owner = self.owner
        self.destroy()
        try:
            owner.lift()
            owner.focus_set()
        except tk.TclError:
            pass
        return True

    def clear_all(self) -> bool:
        if self.owner.running:
            return False
        self.owner.clear_story()
        self._refresh_scenes()
        self.say("모든 장면에서 주인공을 뺐습니다 (기존 배경 만들기 그대로).", "info")
        self._update_buttons()
        return True

    # ================= 이벤트 =================
    def _spawn(self, fn, *args):
        t = threading.Thread(target=fn, args=args, daemon=True)
        self._threads.append(t)
        t.start()

    def _pump(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "thumb":
                    if ev[1] == self.entry_id.get():
                        self._show_thumb(ev[2])
                elif kind == "inspected":
                    self._image_ready(ev[1], ev[2])
                elif kind == "inspect_error":
                    self.checking = False
                    self.say("⚠ " + ev[1], "error")
                    self._update_buttons()
                elif kind == "pv":
                    self._preview_ready(*ev[1:])
                elif kind == "pv_error":
                    self.busy = False
                    self.pv_caption.set("")
                    if ev[1]:
                        self.say("미리보기를 만들지 못했습니다: " + ev[1].splitlines()[0], "error")
                    self._update_buttons()
                    if self._pv_pending:
                        self._pv_pending = False
                        self.schedule_preview(300)
                elif kind == "motion":
                    self.busy = False
                    self.preview_files.append(ev[1])
                    self.last_preview = ev[1]
                    self.say("움직임·페이드 영상을 열었습니다 (등장 1초 전부터 약 9초, 저화질, 소리 없음).", "ok")
                    self._update_buttons()
                    from .bg_ui import _open_path
                    if not getattr(self.owner, "quiet_errors", False):
                        _open_path(ev[1])
                elif kind == "remove_progress":
                    self.remove_progress.set(ev[1])
                elif kind == "removed":
                    self.removing = False
                    self.remove_row.pack_forget()
                    img, thumb, tmp = ev[1], ev[2], ev[3]
                    e = self.lib.add(img, img.name, self._default_tags())
                    shutil.rmtree(tmp, ignore_errors=True)
                    self._refresh_library()
                    self._show_thumb(thumb)
                    self.use_entry(e.id, load_style=False, thumb=False)
                    self.say("✓ 배경을 지운 투명 사본을 만들었습니다 (원본은 그대로). 미리보기를 확인하세요.", "ok")
                elif kind == "remove_error":
                    self.removing = False
                    self.remove_row.pack_forget()
                    shutil.rmtree(ev[2], ignore_errors=True)
                    self.say("⚠ " + ev[1], "error")
                    self._update_buttons()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.after(100, self._pump)
        except tk.TclError:
            pass

    def _update_buttons(self):
        try:
            has = self.entry() is not None
            ready = self.layer() is not None
            for b in (self.btn_meta, self.btn_unlist, self.btn_remember):
                b.configure(state="normal" if has else "disabled")
            busy = self.busy or self.checking or self.removing
            self.btn_motion.configure(state="normal" if ready and not busy and self.owner.clips else "disabled")
            for b in (self.btn_left, self.btn_center, self.btn_right, self.btn_bigger, self.btn_smaller):
                b.configure(state="normal" if ready else "disabled")
            self.btn_pick.configure(state="disabled" if self.checking or self.removing else "normal")
            self.btn_apply.configure(state="normal" if ready and not self.owner.running else "disabled")
            # 배경 제거: 검증된 로컬 제거기가 있을 때만 누를 수 있다 (작동하는 것처럼 보이지 않게)
            self.btn_remove.configure(state="normal" if self.remover and not busy else "disabled")
            self.btn_as_is.configure(state="disabled" if busy else "normal")
            self.sp_every.configure(state="normal" if self.mode.get() == S.APPLY_INTERVAL else "disabled")
        except tk.TclError:
            pass

    def destroy(self):
        self.cancel.set()
        self.remove_cancel.set()
        if self._pv_job is not None:
            try:
                self.after_cancel(self._pv_job)
            except tk.TclError:
                pass
            self._pv_job = None
        for t in self._threads:
            t.join(timeout=8)
        for p in self.preview_files:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            if getattr(self.owner, "story_dialog", None) is self:
                self.owner.story_dialog = None
            super().destroy()
        finally:
            release_tk_variables(self)
