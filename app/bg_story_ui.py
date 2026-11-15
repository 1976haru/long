"""④ [＋ 주인공 이미지 넣기] 창 (통합 08) — 움직이는 배경 영상 위에 STORY 주인공 이미지(투명 PNG)를 겹친다.

흐름: ① 내 주인공 이미지 선택(또는 투명 PNG 등록) → ② 스타일(프리셋) → ③ 넣을 장면 → ④ 미리보기 → [✓ 적용]
→ ④ 창에서 음악 길이 입력 → [⑦ 제작 시작]. 기본값 그대로 [✓ 적용]만 눌러도 된다.
'사진을 독립 장면으로 넣기'(④ 목록에 사진 추가)와는 다른 기능이다 — 여기서는 장면 위에 인물을 겹친다.
FFmpeg 작업(이미지 확인·미리보기)은 작업 스레드에서, 화면 갱신은 이벤트 큐로 Tk main thread에서만 한다.
"""
from __future__ import annotations

import base64
import queue
import tempfile
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from . import bg_story as S
from .bg_core import GUARD_OWNER, make_story_preview, story_preview_png
from .core import BuildCancelled
from .tooling import FFMPEG_GUARD, release_tk_variables
from .ui_scroll import ScrollFrame

TITLE = "④ 주인공 이미지 넣기 — 움직이는 배경 위에 STORY 인물 겹치기"
IMAGE_TYPES = [("투명 PNG·WebP", "*.png *.webp"), ("모든 이미지", "*.png *.webp *.jpg *.jpeg *.bmp"), ("모든 파일", "*.*")]
PREVIEW_W, PREVIEW_H = 480, 270
STEPS = "① 이미지 선택 → ② 스타일 → ③ 넣을 장면 → ④ 미리보기 → [✓ 적용] → ④ 창에서 음악 길이 입력 → [⑦ 제작 시작]"

# 숫자 입력: (변수 이름, 화면 이름, 최소, 최대, 간격, 화면 단위 → StoryStyle 값 배율)
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
        sh = self.winfo_screenheight()
        self.geometry(f"1060x{max(560, min(900, sh - 100))}")
        self.minsize(820, 480)
        self.lib = S.StoryLibrary()
        self.presets = S.load_presets()
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False                      # 이미지 확인·미리보기 중
        self._threads: list[threading.Thread] = []
        self.preview_files: list[Path] = []
        self.thumb_img = None
        self.preview_img = None
        self.last_preview: Path | None = None
        last = owner.story_last if isinstance(getattr(owner, "story_last", None), dict) else {}

        self.entry_id = tk.StringVar(value=str(last.get("entry") or ""))
        self.name = tk.StringVar()
        self.tags = {t: tk.BooleanVar(value=False) for t in S.TAGS}
        p0 = S.get_preset(last.get("preset") or S.DEFAULT_PRESET)
        self.preset = tk.StringVar(value=p0.name)
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
        self.status = tk.StringVar(value="주인공 이미지를 고르거나 [＋ 투명 PNG 등록]을 누르세요.")
        self.image_info = tk.StringVar()
        self.result_note = tk.StringVar()
        self.protect_top = p0.style.protect_top
        self.scene_tag = p0.scene

        self._ui()
        self._load_style(p0.style)
        self._refresh_library(select=self.entry_id.get() or None)
        self._refresh_scenes()
        self._update_buttons()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(100, self._pump)

    # ---------------- 화면 ----------------
    def _ui(self):
        bottom = ttk.Frame(self, padding=(10, 4, 10, 8))
        bottom.pack(side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        root = ttk.Frame(self.scroll.body, padding=10)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="주인공 이미지 넣기 (움직이는 배경 위에 인물 겹치기)", font="PLS.Title").pack(anchor="w")
        ttk.Label(root, text="크리스마스 거리·도쿄 야경·불꽃축제 같은 실제 배경 영상 위에 남녀 주인공 이미지(투명 PNG)를 겹칩니다. "
                             "④ 목록에 '사진'을 넣는 기능(사진 = 한 장면)과 다릅니다. " + S.STATIC_NOTE,
                  style="Hint.TLabel", wraplength=1000, justify="left").pack(anchor="w")
        ttk.Label(root, text=STEPS, font="PLS.Strong").pack(anchor="w", pady=(4, 6))

        top = ttk.Frame(root)
        top.pack(fill="x")
        # ① 내 주인공 이미지
        f1 = ttk.LabelFrame(top, text="① 내 주인공 이미지 (한 번 등록하면 다음에도 바로 선택)", padding=6)
        f1.pack(side="left", fill="both", expand=True)
        lr = ttk.Frame(f1); lr.pack(fill="both", expand=True)
        self.lst = tk.Listbox(lr, height=8, exportselection=False, width=26)
        self.lst.pack(side="left", fill="both", expand=True)
        self.lst.bind("<<ListboxSelect>>", lambda e: self._on_pick())
        self.thumb = ttk.Label(lr, text="(미리보기)", anchor="center", width=16)
        self.thumb.pack(side="left", padx=(8, 0))
        br = ttk.Frame(f1); br.pack(fill="x", pady=(4, 0))
        self.btn_add = ttk.Button(br, text="＋ 투명 PNG 등록…", command=self._pick_image, style="Primary.TButton")
        self.btn_add.pack(side="left")
        self.btn_meta = ttk.Button(br, text="이름·태그 저장", command=self.save_meta)
        self.btn_meta.pack(side="left", padx=4)
        self.btn_unlist = ttk.Button(br, text="목록에서 빼기", command=self.unlist)
        self.btn_unlist.pack(side="left")
        nr = ttk.Frame(f1); nr.pack(fill="x", pady=(4, 0))
        ttk.Label(nr, text="이름").pack(side="left")
        ttk.Entry(nr, textvariable=self.name, width=22).pack(side="left", padx=(4, 8))
        tr = ttk.Frame(f1); tr.pack(fill="x", pady=(2, 0))
        ttk.Label(tr, text="태그").pack(side="left")
        for t in S.TAGS:
            ttk.Checkbutton(tr, text=t, variable=self.tags[t]).pack(side="left", padx=(4, 0))
        ttk.Label(f1, textvariable=self.image_info, style="Hint.TLabel", wraplength=420,
                  justify="left").pack(anchor="w", pady=(2, 0))
        ttk.Label(f1, text="투명 배경 PNG만 사용합니다. JPG처럼 배경이 있는 이미지는 먼저 배경 제거가 필요합니다. "
                           "원본 이미지는 그대로 두고 사본을 설정 폴더에 보관합니다.", style="Hint.TLabel",
                  wraplength=420, justify="left").pack(anchor="w")

        # ② 스타일
        f2 = ttk.LabelFrame(top, text="② 스타일 (장면에 맞는 프리셋 선택 → 필요하면 조정)", padding=6)
        f2.pack(side="left", fill="both", expand=True, padx=(8, 0))
        pr = ttk.Frame(f2); pr.pack(fill="x")
        ttk.Label(pr, text="프리셋").pack(side="left")
        self.cb_preset = ttk.Combobox(pr, textvariable=self.preset, state="readonly", width=24)
        self.cb_preset.pack(side="left", padx=4)
        self.cb_preset.bind("<<ComboboxSelected>>", lambda e: self.choose_preset())
        pb = ttk.Frame(f2); pb.pack(fill="x", pady=(4, 0))
        ttk.Button(pb, text="복제", command=self.duplicate).pack(side="left")
        ttk.Button(pb, text="지금 값을 내 프리셋으로 저장", command=self.save_as_preset).pack(side="left", padx=4)
        self.btn_del_preset = ttk.Button(pb, text="삭제", command=self.delete_user_preset)
        self.btn_del_preset.pack(side="left")
        ttk.Label(f2, textvariable=self.preset_desc, style="Hint.TLabel", wraplength=440,
                  justify="left").pack(anchor="w", pady=(4, 2))
        ar = ttk.Frame(f2); ar.pack(fill="x")
        ttk.Label(ar, text="위치").pack(side="left")
        for k in S.ALIGNS:
            ttk.Radiobutton(ar, text=S.ALIGN_LABELS[k], value=k, variable=self.align).pack(side="left", padx=2)
        ttk.Label(ar, text="  움직임").pack(side="left")
        ttk.Combobox(ar, textvariable=self.motion, state="readonly", width=16,
                     values=[S.SMOTION_LABELS[m] for m in S.SMOTIONS]).pack(side="left", padx=4)
        grid = ttk.Frame(f2); grid.pack(fill="x", pady=(4, 0))
        for i, (k, label, lo, hi, step, _) in enumerate(NUMBERS):
            r, c = divmod(i, 2)
            ttk.Label(grid, text=label).grid(row=r, column=c * 2, sticky="w", padx=(0 if c == 0 else 10, 2), pady=1)
            ttk.Spinbox(grid, textvariable=self.nums[k], from_=lo, to=hi, increment=step, width=7).grid(
                row=r, column=c * 2 + 1, sticky="w")
        kb = ttk.Frame(f2); kb.pack(fill="x", pady=(4, 0))
        self.btn_remember = ttk.Button(kb, text="이 이미지에 위치·크기 기억", command=self.remember_placement)
        self.btn_remember.pack(side="left")
        ttk.Label(f2, text="그림자·인물 동작은 만들지 않습니다 (2D 겹치기).", style="Hint.TLabel").pack(anchor="w")

        # ③ 장면
        f3 = ttk.LabelFrame(root, text="③ 주인공을 넣을 장면 (장면 사이 디졸브 중에는 넣지 않고, 앞뒤는 페이드)", padding=6)
        f3.pack(fill="x", pady=(8, 0))
        mr = ttk.Frame(f3); mr.pack(fill="x")
        for k in (S.APPLY_ALL, S.APPLY_SELECTED, S.APPLY_INTERVAL):
            ttk.Radiobutton(mr, text=S.APPLY_LABELS[k], value=k, variable=self.mode,
                            command=self._update_buttons).pack(side="left", padx=(0, 8))
        ttk.Label(mr, text="간격 N").pack(side="left")
        self.sp_every = ttk.Spinbox(mr, textvariable=self.every, from_=2, to=10, increment=1, width=4)
        self.sp_every.pack(side="left", padx=4)
        ttk.Label(mr, text="(N=2: 한 장면 걸러 1번)", style="Hint.TLabel").pack(side="left")
        sl = ttk.Frame(f3); sl.pack(fill="x", pady=(4, 0))
        self.lst_scenes = tk.Listbox(sl, height=5, selectmode="multiple", exportselection=False)
        self.lst_scenes.pack(side="left", fill="x", expand=True)
        ttk.Label(sl, text="'선택한 장면에만'일 때 여러 개를 눌러 고르세요.\n나중에 ④ 창 목록에서 장면마다\n"
                           "[👤 주인공 켜기/끄기]로 바꿀 수 있습니다.", style="Hint.TLabel",
                  justify="left").pack(side="left", padx=8)

        # ④ 미리보기
        f4 = ttk.LabelFrame(root, text="④ 미리보기 — 실제 제작과 같은 해상도·필터·등장 시간으로 만든 화면", padding=6)
        f4.pack(fill="x", pady=(8, 0))
        vr = ttk.Frame(f4); vr.pack(fill="x")
        ttk.Label(vr, text="장면").pack(side="left")
        self.cb_scene = ttk.Combobox(vr, textvariable=self.scene, state="readonly", width=40)
        self.cb_scene.pack(side="left", padx=4)
        self.btn_preview = ttk.Button(vr, text="▶ 미리보기 (1장면)", command=self.preview_still)
        self.btn_preview.pack(side="left", padx=4)
        self.btn_motion = ttk.Button(vr, text="▶ 등장·움직임 미리보기 (저화질 영상)", command=self.preview_motion)
        self.btn_motion.pack(side="left")
        self.pv = ttk.Label(f4, compound="top")
        self.pv.pack(anchor="w", pady=(4, 0))

        ttk.Label(bottom, textvariable=self.result_note, style="Hint.TLabel", wraplength=1000,
                  justify="left").pack(anchor="w")
        rb = ttk.Frame(bottom); rb.pack(fill="x", pady=(4, 0))
        self.btn_apply = ttk.Button(rb, text="✓ 적용 (④ 배경 만들기에 반영)", command=self.apply, style="Primary.TButton")
        self.btn_apply.pack(side="left", fill="x", expand=True)
        self.btn_clear = ttk.Button(rb, text="주인공 모두 빼기", command=self.clear_all)
        self.btn_clear.pack(side="left", padx=6)
        ttk.Button(rb, text="닫기", command=self.destroy).pack(side="left")
        ttk.Label(bottom, textvariable=self.status).pack(anchor="w", pady=(4, 0))

    # ---------------- 값 ↔ 화면 ----------------
    def _load_style(self, st: S.StoryStyle):
        st = st.clamped()
        self.align.set(st.align)
        self.motion.set(S.SMOTION_LABELS[st.motion])
        for k, _, _, _, _, mul in NUMBERS:
            self.nums[k].set(_fmt(round(getattr(st, k) / mul, 2)))
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
        return next((p for p in self.presets if p.name == self.preset.get()), self.presets[0])

    def _refresh_presets(self):
        self.cb_preset.configure(values=[p.name for p in self.presets])
        p = self.current_preset()
        self.preset_desc.set(("[기본] " if p.builtin else "[내 프리셋] ") + (p.desc or "") +
                             (f" · 장면 태그 {p.scene}" if p.scene else ""))
        try:
            self.btn_del_preset.configure(state="disabled" if p.builtin else "normal")
        except tk.TclError:
            pass

    def entry(self) -> S.LibraryEntry | None:
        return self.lib.get(self.entry_id.get()) if self.entry_id.get() else None

    def layer(self) -> S.StoryLayer | None:
        e = self.entry()
        if e is None or not self.lib.available(e):
            return None
        return S.StoryLayer(self.lib.image_of(e), self.style(), self.current_preset().name)

    # ---------------- ① 라이브러리 ----------------
    def _refresh_library(self, select: str | None = None):
        self.lst.delete(0, "end")
        for e in self.lib.entries:
            miss = "" if self.lib.available(e) else "  ⚠ 사본 없음"
            self.lst.insert("end", f"{e.name}" + (f"  [{', '.join(e.tags)}]" if e.tags else "") + miss)
        ids = [e.id for e in self.lib.entries]
        if select in ids:
            i = ids.index(select)
            self.lst.selection_clear(0, "end")
            self.lst.selection_set(i)
            self.lst.see(i)
            self._on_pick(load_style=False)
        elif not ids:
            self.entry_id.set("")
            self.image_info.set("아직 등록한 주인공 이미지가 없습니다. [＋ 투명 PNG 등록]으로 추가하세요.")

    def _on_pick(self, load_style: bool = True):
        sel = self.lst.curselection()
        if not sel or sel[0] >= len(self.lib.entries):
            return
        e = self.lib.entries[sel[0]]
        self.entry_id.set(e.id)
        self.name.set(e.name)
        for t, v in self.tags.items():
            v.set(t in e.tags)
        if load_style:
            if e.preset and any(p.key == e.preset for p in self.presets):
                pre = next(p for p in self.presets if p.key == e.preset)
                self.preset.set(pre.name)
                self._load_style(pre.style)
            if e.style:                       # 이 이미지에 기억한 위치·크기
                fav = S.StoryStyle.from_dict(e.style)
                cur = self.style()
                self._load_style(replace(cur, size=fav.size, align=fav.align, margin=fav.margin, lift=fav.lift))
        x, y, w, h = e.crop
        self.image_info.set(f"{e.name} · {e.width}×{e.height} · 보이는 인물 영역 {w}×{h} · 투명 {e.transparent:.0%}"
                            + ("" if self.lib.available(e) else " · ⚠ 라이브러리 사본이 없습니다 (다시 등록하세요)"))
        self._thumb(e)
        self._update_buttons()

    def _thumb(self, e: S.LibraryEntry):
        tools = self._tools(quiet=True)
        if not tools or not self.lib.available(e):
            return
        img = self.lib.image_of(e)
        self._spawn(self._thumb_worker, tools[0], img, e.id)

    def _thumb_worker(self, ff, img, eid):
        try:
            self.events.put(("thumb", eid, base64.b64encode(S.thumb_png(ff, img)).decode("ascii")))
        except Exception:
            pass

    def _pick_image(self):
        p = filedialog.askopenfilename(parent=self, title="주인공 이미지 선택 (투명 배경 PNG)", filetypes=IMAGE_TYPES)
        if p:
            self.register_path(Path(p))

    def register_path(self, path: Path, name: str = "") -> bool:
        """이미지 확인(투명 PNG인지) → 라이브러리에 사본 등록. JPG 등은 배경 제거 안내만 하고 등록하지 않는다."""
        if self.busy:
            return False
        ext = Path(path).suffix.lower()
        if ext in S.OPAQUE_EXTS:     # 바로 안내 (FFmpeg 불필요)
            self.status.set("배경 제거가 필요한 이미지입니다 (등록하지 않음).")
            messagebox.showwarning("배경 제거 필요", f"'{Path(path).name}'\n\n" + S.BG_REMOVAL_NOTE, parent=self)
            return False
        tools = self._tools()
        if not tools:
            return False
        tags = [t for t, v in self.tags.items() if v.get()] or ([self.scene_tag] if self.scene_tag in S.TAGS else [])
        self.busy = True
        self.status.set(f"이미지 확인 중… {Path(path).name}")
        self._update_buttons()
        self._spawn(self._register_worker, tools, Path(path), name, tags)
        return True

    def _register_worker(self, tools, path, name, tags):
        ff, fp = tools
        try:
            img = S.inspect_image(ff, fp, path, name)
            self.events.put(("inspected", img, tags))
        except S.StoryImageError as e:
            self.events.put(("register_error", "배경 제거 필요", str(e)))
        except Exception as e:
            self.events.put(("register_error", "이미지 등록", str(e)))

    def save_meta(self) -> bool:
        e = self.entry()
        if e is None:
            return False
        self.lib.update(e.id, name=self.name.get(), tags=[t for t, v in self.tags.items() if v.get()])
        self._refresh_library(select=e.id)
        self.status.set(f"저장했습니다: {self.name.get()}")
        return True

    def unlist(self) -> bool:
        e = self.entry()
        if e is None or not messagebox.askyesno("목록에서 빼기", f"'{e.name}'을(를) 목록에서 뺄까요?\n"
                                                "(원본 이미지와 설정 폴더의 사본 파일은 지우지 않습니다.)", parent=self):
            return False
        self.lib.remove(e.id)
        self.entry_id.set("")
        self.thumb.configure(image="", text="(미리보기)")
        self._refresh_library()
        self._update_buttons()
        return True

    def remember_placement(self) -> bool:
        e = self.entry()
        if e is None:
            return False
        self.lib.update(e.id, style=self.style(), preset=self.current_preset().key)
        self.status.set(f"'{e.name}'의 위치·크기를 기억했습니다 (다음에 이 이미지를 고르면 자동으로 적용).")
        return True

    # ---------------- ② 프리셋 ----------------
    def choose_preset(self, key: str | None = None):
        if key:
            p = next((x for x in self.presets if x.key == key), None)
            if p:
                self.preset.set(p.name)
        p = self.current_preset()
        self.scene_tag = p.scene
        self._load_style(p.style)

    def duplicate(self) -> S.StoryPreset:
        p = S.duplicate_preset(self.current_preset().key)
        self.presets = S.load_presets()
        self.preset.set(p.name)
        self._refresh_presets()
        self.status.set(f"'{p.name}'을(를) 만들었습니다. 값을 바꾼 뒤 [지금 값으로 내 프리셋 저장]을 누르면 수정됩니다.")
        return p

    def save_as_preset(self) -> S.StoryPreset:
        cur = self.current_preset()
        if cur.builtin:      # 기본 프리셋은 그대로 두고 새 내 프리셋으로
            p = S.save_preset(f"{cur.name} (내 설정)", self.style(), cur.scene, cur.desc)
        else:
            p = S.save_preset(cur.name, self.style(), cur.scene, cur.desc, key=cur.key)
        self.presets = S.load_presets()
        self.preset.set(p.name)
        self._refresh_presets()
        self.status.set(f"내 프리셋 저장: {p.name}")
        return p

    def delete_user_preset(self) -> bool:
        p = self.current_preset()
        if p.builtin or not messagebox.askyesno("내 프리셋 삭제", f"'{p.name}'을(를) 삭제할까요?", parent=self):
            return False
        S.delete_preset(p.key)
        self.presets = S.load_presets()
        self.preset.set(self.presets[0].name)
        self._refresh_presets()
        return True

    # ---------------- ③ 장면 ----------------
    def _scene_names(self):
        return [(i, c) for i, c in enumerate(self.owner.clips) if c.enabled]

    def _refresh_scenes(self):
        self.lst_scenes.delete(0, "end")
        items = self._scene_names()
        sel_owner = self.owner._selected_index()
        names = []
        for k, (i, c) in enumerate(items):
            name = f"{k + 1}. {'🖼 ' if c.is_image else ''}{c.path.name} ({c.used_length:.0f}초)" + (" 👤" if c.story else "")
            names.append(name)
            self.lst_scenes.insert("end", name)
            if c.story is not None or i == sel_owner:
                self.lst_scenes.selection_set(k)
        self.cb_scene.configure(values=names)
        if names and self.scene.get() not in names:
            pick = next((k for k, (i, c) in enumerate(items) if c.story is not None), None)
            if pick is None:
                pick = next((k for k, (i, _) in enumerate(items) if i == sel_owner), 0)
            self.scene.set(names[pick])
        if not names:
            self.scene.set("")

    def selected_scenes(self) -> list[int]:
        """'선택한 장면에만'에서 고른 장면의 ④ 목록 위치."""
        items = self._scene_names()
        return [items[k][0] for k in self.lst_scenes.curselection() if k < len(items)]

    def every_value(self) -> int:
        try:
            return max(2, min(10, int(self.every.get())))
        except ValueError:
            return S.DEFAULT_EVERY

    def preview_clip(self):
        """미리보기할 장면 (지금 설정을 적용한 상태). 넣지 않기로 한 장면이어도 미리보기는 보여 준다."""
        layer = self.layer()
        items = self._scene_names()
        names = list(self.cb_scene.cget("values") or ())
        if layer is None or not items:
            return None
        k = names.index(self.scene.get()) if self.scene.get() in names else 0
        return replace(items[min(k, len(items) - 1)][1], story=layer)

    # ---------------- ④ 미리보기 ----------------
    def _tools(self, quiet: bool = False):
        try:
            ff, fp = self.owner.tools()
        except Exception:
            ff = fp = None
        if not ff or not fp:
            if not quiet:
                messagebox.showwarning("FFmpeg", "FFmpeg/ffprobe를 찾지 못했습니다.", parent=self)
            return None
        return Path(ff), Path(fp)

    def _start_preview(self, motion: bool) -> bool:
        if self.busy or self.owner.running:
            return False
        clip = self.preview_clip()
        if clip is None:
            messagebox.showinfo("미리보기", "주인공 이미지와 ④ 목록의 배경 영상이 필요합니다.", parent=self)
            return False
        tools = self._tools()
        if not tools:
            return False
        try:
            spec = self.owner.current_spec()
        except ValueError as e:
            messagebox.showwarning("미리보기", str(e), parent=self)
            return False
        d = self.owner.effective_dissolve()[0]
        if S.clip_window(clip, d, spec.fps) is None:
            messagebox.showinfo("미리보기", "이 장면은 너무 짧아 주인공을 넣지 못합니다 (디졸브 바깥 2초 미만). "
                                         "다른 장면을 고르거나 사용 구간을 늘리세요.", parent=self)
            return False
        if not FFMPEG_GUARD.try_acquire(GUARD_OWNER):
            messagebox.showwarning("FFmpeg 사용 중", "다른 FFmpeg 작업이 끝난 뒤 다시 시도하세요.", parent=self)
            return False
        self.busy = True
        self.cancel.clear()
        self.status.set("미리보기 만드는 중…")
        self._update_buttons()
        out = Path(tempfile.gettempdir()) / "PLVM_BG_PREVIEW" / f"story_{uuid.uuid4().hex[:8]}.mp4"
        self._spawn(self._preview_worker, tools[0], clip, spec, d, motion, out)
        return True

    def preview_still(self) -> bool:
        return self._start_preview(False)

    def preview_motion(self) -> bool:
        return self._start_preview(True)

    def _preview_worker(self, ff, clip, spec, d, motion, out):
        try:
            if motion:
                p = make_story_preview(ff, clip, spec, d, out, self.cancel)
                self.events.put(("motion", p))
            else:
                png = story_preview_png(ff, clip, spec, d, self.cancel, size=(PREVIEW_W, PREVIEW_H))
                self.events.put(("still", base64.b64encode(png).decode("ascii"), clip.path.name, spec))
        except BuildCancelled:
            self.events.put(("preview_error", ""))
        except Exception as e:
            self.events.put(("preview_error", str(e)))
        finally:
            FFMPEG_GUARD.release(GUARD_OWNER)

    # ---------------- 적용 ----------------
    def apply(self) -> bool:
        layer = self.layer()
        if layer is None:
            messagebox.showinfo("주인공 이미지", "먼저 주인공 이미지를 고르거나 [＋ 투명 PNG 등록]으로 추가하세요.", parent=self)
            return False
        if self.owner.running:
            return False
        mode = self.mode.get()
        sel = self.selected_scenes()
        if mode == S.APPLY_SELECTED and not sel:
            messagebox.showinfo("장면 선택", "'선택한 장면에만'을 고르셨습니다. 아래 목록에서 장면을 하나 이상 고르세요.",
                                parent=self)
            return False
        n = self.owner.apply_story(layer, mode, sel, self.every_value(),
                                   last={"entry": self.entry_id.get(), "preset": self.current_preset().key,
                                         "mode": mode, "every": self.every_value()})
        e = self.entry()
        if e is not None:
            self.lib.update(e.id, preset=self.current_preset().key)
        self._refresh_scenes()
        self.result_note.set(f"✓ {n}개 장면에 '{layer.image.name}'({layer.label})을(를) 넣었습니다. ④ 창에서 음악 길이를 "
                             "입력하고 [⑦ 제작 시작]을 누르세요. 장면마다 켜고 끄려면 ④ 목록의 [👤 주인공 켜기/끄기].")
        self.status.set("적용 완료")
        self._update_buttons()
        return True

    def clear_all(self) -> bool:
        if self.owner.running:
            return False
        self.owner.clear_story()
        self._refresh_scenes()
        self.result_note.set("모든 장면에서 주인공을 뺐습니다 (기존 배경 만들기 그대로).")
        self._update_buttons()
        return True

    # ---------------- 이벤트 ----------------
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
                        try:
                            self.thumb_img = tk.PhotoImage(master=self, data=ev[2])
                            self.thumb.configure(image=self.thumb_img, text="")
                        except tk.TclError:
                            pass
                elif kind == "inspected":
                    self.busy = False
                    img, tags = ev[1], ev[2]
                    e = self.lib.add(img, img.name, tags)
                    self._refresh_library(select=e.id)
                    self._on_pick(load_style=False)
                    self.status.set(f"등록했습니다: {e.name} (원본은 그대로, 사본을 설정 폴더에 보관)")
                    self._update_buttons()
                elif kind == "register_error":
                    self.busy = False
                    self.status.set("이미지를 등록하지 않았습니다.")
                    self.last_error = ev[2]
                    self._update_buttons()
                    messagebox.showwarning(ev[1], ev[2], parent=self)
                elif kind == "still":
                    self.busy = False
                    try:
                        self.preview_img = tk.PhotoImage(master=self, data=ev[1])
                        spec = ev[3]
                        self.pv.configure(image=self.preview_img,
                                          text=f"{ev[2]} · 주인공이 완전히 보이는 순간 ({spec.width}×{spec.height} 제작 화면을 축소)")
                    except tk.TclError:
                        pass
                    self.status.set("미리보기 완료")
                    self._update_buttons()
                elif kind == "motion":
                    self.busy = False
                    self.preview_files.append(ev[1])
                    self.last_preview = ev[1]
                    self.status.set("등장·움직임 미리보기: 등장 1초 전부터 약 9초 (저화질, 소리 없음)")
                    self._update_buttons()
                    from .bg_ui import _open_path
                    if not getattr(self.owner, "quiet_errors", False):
                        _open_path(ev[1])
                elif kind == "preview_error":
                    self.busy = False
                    self.status.set("미리보기를 만들지 못했습니다" if ev[1] else "미리보기 취소됨")
                    self._update_buttons()
                    if ev[1] and not getattr(self.owner, "quiet_errors", False):
                        messagebox.showwarning("미리보기", ev[1], parent=self)
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
            for b in (self.btn_meta, self.btn_unlist, self.btn_remember):
                b.configure(state="normal" if has else "disabled")
            for b in (self.btn_preview, self.btn_motion):
                b.configure(state="disabled" if self.busy or not has or not self.owner.clips else "normal")
            self.btn_add.configure(state="disabled" if self.busy else "normal")
            self.btn_apply.configure(state="disabled" if not has or self.owner.running else "normal")
            self.btn_clear.configure(state="normal" if any(c.story for c in self.owner.clips) else "disabled")
            self.sp_every.configure(state="normal" if self.mode.get() == S.APPLY_INTERVAL else "disabled")
        except tk.TclError:
            pass

    def destroy(self):
        self.cancel.set()
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
