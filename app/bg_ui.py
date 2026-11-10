"""④ 내 영상으로 배경 만들기 — 독립 창 (Tkinter).

흐름: MP4 불러오기 → (자동) 썸네일·길이·해상도·FPS + 반복/중복/품질 분석 → 디졸브·길이·해상도 선택 → 제작 시작.
모든 FFmpeg 작업은 백그라운드 스레드에서 실행하고, 화면 갱신은 이벤트 큐로 Tk main thread에서만 한다.
제작 FFmpeg는 기존 ① 영상 늘리기 / LIVE와 같은 FFMPEG_GUARD를 써서 동시에 돌지 않는다.
"""
from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import bg_analysis as A
from .bg_core import (
    DEFAULT_DISSOLVE, DISSOLVE_CHOICES, ENCODE_PROFILES, GUARD_OWNER, PREVIEW_CONTEXT, QUALITY_4K, QUALITY_FHD,
    TARGET_MINUTES, BgPlan, Clip, build_bg_plan, choose_output_spec, cleanup_stale_temp, default_bg_output_name,
    get_profile, hevc_available, make_seam_preview, run_background_build,
)
from .core import BuildCancelled, enough_disk_space, ensure_unique_output, format_duration, free_bytes, probe_video
from .settings import load_settings, update_settings
from .tooling import FFMPEG_GUARD, prevent_windows_sleep, release_tk_variables
from .ui_scroll import ScrollFrame

TITLE = "④ 내 영상으로 배경 만들기"
SETTINGS_KEY = "bg_maker"
THUMB_W, THUMB_H = 112, 63


def _busy_text(owner):
    from .live_supervisor import busy_message
    return busy_message(owner)


def _dissolve_label(v: float) -> str:
    return f"{v:g}초"


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
        self.last_preview: Path | None = None

        saved = load_settings().get(SETTINGS_KEY)
        saved = saved if isinstance(saved, dict) else {}
        try:
            d0 = float(saved.get("dissolve", DEFAULT_DISSOLVE))
            m0 = int(saved.get("minutes", 10))
        except (TypeError, ValueError):
            d0, m0 = DEFAULT_DISSOLVE, 10
        self.dissolve = tk.StringVar(value=_dissolve_label(d0 if d0 in DISSOLVE_CHOICES else DEFAULT_DISSOLVE))
        self.minutes = tk.IntVar(value=m0 if m0 in TARGET_MINUTES else 10)
        self.quality = tk.StringVar(value=saved.get("quality") if saved.get("quality") in (QUALITY_FHD, QUALITY_4K) else QUALITY_FHD)
        self.profile = tk.StringVar(value=get_profile(saved.get("profile")).label)
        self.profile_note = tk.StringVar()
        self.outdir = tk.StringVar(value=str(saved.get("outdir", "")))
        self.outname = tk.StringVar()
        self.summary = tk.StringVar(value="MP4 영상을 불러오세요 (30초~2분 길이 권장).")
        self.plan_text = tk.StringVar()
        self.warn_text = tk.StringVar()
        self.detail = tk.StringVar(value="목록에서 영상을 선택하면 분석 결과가 보입니다.")
        self.status = tk.StringVar(value="대기 중")
        self.range_start = tk.StringVar()
        self.range_end = tk.StringVar()
        self._name_auto = True

        self._ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._pump)

    # ---------------- 화면 ----------------
    def _ui(self):
        st = ttk.Style(self)
        st.configure("BG.Treeview", rowheight=THUMB_H + 8)
        # 제작 버튼·진행률은 항상 창 아래에 보이게 고정하고, 나머지는 작은 화면에서 세로 스크롤
        bottom = ttk.Frame(self, padding=(10, 4, 10, 8))
        bottom.pack(side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        root = ttk.Frame(self.scroll.body, padding=10)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text=TITLE, font="PLS.Title").pack(anchor="w")
        ttk.Label(root, text="직접 준비한 MP4 여러 개를 자연스러운 디졸브로 이어 3~60분 배경 영상을 만듭니다. "
                             "원본 오디오는 넣지 않습니다 (음악은 나중에 따로).", style="Hint.TLabel",
                  wraplength=1100, justify="left").pack(anchor="w", pady=(0, 6))

        f1 = ttk.LabelFrame(root, text="1. 영상 불러오기 · 순서 · 제외", padding=6)
        f1.pack(fill="both", expand=True)
        br = ttk.Frame(f1); br.pack(fill="x", pady=(0, 4))
        ttk.Button(br, text="＋ MP4 불러오기", command=self._pick_files, style="Primary.TButton").pack(side="left")
        ttk.Button(br, text="▲ 위로", command=lambda: self._move(-1)).pack(side="left", padx=(8, 2))
        ttk.Button(br, text="▼ 아래로", command=lambda: self._move(1)).pack(side="left")
        self.btn_toggle = ttk.Button(br, text="사용/제외 전환", command=self._toggle_enabled)
        self.btn_toggle.pack(side="left", padx=(8, 2))
        ttk.Button(br, text="목록에서 빼기", command=self._remove).pack(side="left", padx=2)
        ttk.Button(br, text="↻ 다시 분석", command=self._reanalyze).pack(side="right")
        ttk.Button(br, text="추천 구간 모두 적용", command=self._apply_all_advice).pack(side="right", padx=4)

        tf = ttk.Frame(f1); tf.pack(fill="both", expand=True)
        cols = ("use", "name", "dur", "res", "fps", "range", "loop", "quality")
        self.tree = ttk.Treeview(tf, columns=cols, show="tree headings", height=4, style="BG.Treeview",
                                 selectmode="browse")
        self.tree.heading("#0", text="미리보기")
        self.tree.column("#0", width=THUMB_W + 26, stretch=False)
        for c, h, w in zip(cols, ("사용", "파일명", "길이", "해상도", "FPS", "사용 구간", "반복 평가", "품질·중복 (참고)"),
                           (48, 230, 70, 100, 55, 120, 110, 230)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("name", "quality", "loop") else "center")
        vs = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: (self._show_detail(), self._refresh_buttons()))
        ttk.Label(f1, textvariable=self.summary).pack(anchor="w", pady=(4, 0))

        f2 = ttk.LabelFrame(root, text="2. 자동 분석 결과 (선택한 영상) — 휴리스틱 참고 지표이며 AI 인물·로고 인식은 하지 않습니다",
                            padding=6)
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
        pr = ttk.Frame(f2); pr.pack(fill="x", pady=(4, 0))
        self.btn_preview_loop = ttk.Button(pr, text="▶ 반복 경계 미리보기 (끝 → 처음)",
                                           command=lambda: self.preview_seam("self"))
        self.btn_preview_loop.pack(side="left")
        self.btn_preview_next = ttk.Button(pr, text="▶ 다음 장면 전환 미리보기",
                                           command=lambda: self.preview_seam("next"))
        self.btn_preview_next.pack(side="left", padx=4)
        ttk.Label(pr, text=f"경계 앞뒤 약 {PREVIEW_CONTEXT:g}초를 저화질로 빠르게 만들어 재생합니다 (현재 사용 구간·디졸브 기준).",
                  style="Hint.TLabel").pack(side="left", padx=(8, 0))

        f3 = ttk.LabelFrame(root, text="3. 만들기 설정", padding=6)
        f3.pack(fill="x", pady=(6, 0))
        o1 = ttk.Frame(f3); o1.pack(fill="x")
        ttk.Label(o1, text="장면 전환(디졸브)").pack(side="left")
        cb = ttk.Combobox(o1, textvariable=self.dissolve, state="readonly", width=7,
                          values=[_dissolve_label(v) for v in DISSOLVE_CHOICES])
        cb.pack(side="left", padx=(4, 16))
        cb.bind("<<ComboboxSelected>>", lambda e: self._options_changed())
        ttk.Label(o1, text="출력 길이").pack(side="left")
        for m in TARGET_MINUTES:
            ttk.Radiobutton(o1, text=f"{m}분", value=m, variable=self.minutes,
                            command=self._options_changed).pack(side="left", padx=2)
        ttk.Label(o1, text="   해상도").pack(side="left", padx=(12, 0))
        ttk.Radiobutton(o1, text="FHD (기본·빠름)", value=QUALITY_FHD, variable=self.quality,
                        command=self._options_changed).pack(side="left", padx=2)
        ttk.Radiobutton(o1, text="4K (원본이 4K일 때)", value=QUALITY_4K, variable=self.quality,
                        command=self._options_changed).pack(side="left", padx=2)
        o3 = ttk.Frame(f3); o3.pack(fill="x", pady=(4, 0))
        ttk.Label(o3, text="화질·용량").pack(side="left")
        pc = ttk.Combobox(o3, textvariable=self.profile, state="readonly", width=26,
                          values=[p.label for p in ENCODE_PROFILES])
        pc.pack(side="left", padx=(4, 8))
        pc.bind("<<ComboboxSelected>>", lambda e: self._options_changed())
        self.cmb_profile = pc
        ttk.Label(o3, textvariable=self.profile_note, style="Hint.TLabel", wraplength=820,
                  justify="left").pack(side="left", fill="x", expand=True)
        o2 = ttk.Frame(f3); o2.pack(fill="x", pady=(4, 0))
        ttk.Label(o2, text="저장 폴더", width=9).pack(side="left")
        ttk.Entry(o2, textvariable=self.outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(o2, text="변경", command=self._pick_outdir).pack(side="left", padx=4)
        ttk.Label(o2, text="파일명").pack(side="left", padx=(8, 2))
        e = ttk.Entry(o2, textvariable=self.outname, width=34)
        e.pack(side="left")
        e.bind("<Key>", lambda ev: setattr(self, "_name_auto", False))
        ttk.Label(f3, textvariable=self.plan_text, font="PLS.Strong", wraplength=1100, justify="left").pack(anchor="w", pady=(4, 0))
        self.warn_label = ttk.Label(f3, textvariable=self.warn_text, foreground="#9a5b00", wraplength=1100, justify="left")
        self.warn_label.pack(anchor="w")

        ar = ttk.Frame(bottom); ar.pack(fill="x")
        self.btn_start = ttk.Button(ar, text="▶ 제작 시작", command=self.start, style="Primary.TButton")
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(ar, text="■ 중단", command=self.stop, state="disabled", style="Danger.TButton")
        self.btn_stop.pack(side="left", padx=(6, 0))
        self.bar = ttk.Progressbar(bottom, maximum=100)
        self.bar.pack(fill="x", pady=(6, 0))
        ttk.Label(bottom, textvariable=self.status).pack(anchor="w")
        rr2 = ttk.Frame(bottom); rr2.pack(fill="x", pady=(4, 0))
        self.btn_open = ttk.Button(rr2, text="▶ 완성 영상 열기", command=self._open_output, state="disabled")
        self.btn_open.pack(side="left")
        self.btn_folder = ttk.Button(rr2, text="폴더 열기", command=self._open_folder, state="disabled")
        self.btn_folder.pack(side="left", padx=4)
        self.btn_send = ttk.Button(rr2, text="① 영상 늘리기로 보내기", command=self._send_to_long, state="disabled")
        self.btn_send.pack(side="left", padx=4)

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
        paths = filedialog.askopenfilenames(parent=self, title="배경으로 쓸 MP4 선택 (여러 개 가능)",
                                            filetypes=[("MP4", "*.mp4"), ("영상", "*.mov *.m4v *.mkv"), ("모든 파일", "*.*")])
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
        self._spawn(self._load_worker, tools, fresh)
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

    def _load_worker(self, tools, paths):
        ff, fp = tools
        probed = []
        for p in paths:
            if self.stop_loading.is_set():
                return
            self.events.put(("status", f"영상 정보 읽는 중 · {p.name}"))
            try:
                info = probe_video(p, fp)
            except Exception as e:
                self.events.put(("load_error", p, str(e)))
                continue
            self.events.put(("clip", info))
            try:
                self.events.put(("thumb", info.path, A.thumbnail_b64(ff, info.path, info.duration)))
            except Exception:
                pass
            probed.append(info)
        self._analyze(ff, [(i.path, i.duration) for i in probed])

    def _analyze(self, ff, items):
        for k, (path, dur) in enumerate(items, 1):
            if self.stop_loading.is_set():
                return
            self.events.put(("status", f"자동 분석 중 ({k}/{len(items)}) · {path.name}"))
            try:
                an = A.analyze_clip(ff, path, dur, self.stop_loading)
                self.events.put(("analysis", path, an))
            except Exception as e:
                self.events.put(("analysis_error", path, str(e)))

    def _reanalyze(self):
        if self.running or not self.clips:
            return
        tools = self._tools()
        if tools:
            self._spawn(self._analyze, tools[0], [(c.path, c.info.duration) for c in self.clips])

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
                    info = ev[1]
                    if all(c.path != info.path for c in self.clips):
                        self.clips.append(Clip(info))
                        changed = True
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
                elif kind == "analysis_error":
                    self.analysis_errors[ev[1]] = ev[2]
                    changed = True
                elif kind == "load_error":
                    name = ev[1].name if ev[1] else "영상"
                    messagebox.showerror("영상을 불러올 수 없습니다", f"{name}\n\n{ev[2]}\n\n이 파일은 건너뜁니다.", parent=self)
                elif kind == "loaded":
                    self.loading = max(0, self.loading - 1)
                    if not self.loading and not self.running:
                        self.status.set("분석 완료 · 설정을 확인하고 [▶ 제작 시작]을 누르세요." if self.clips else "대기 중")
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
        try:
            c.start, c.end = self._parse_range(c)
        except ValueError as e:
            messagebox.showwarning("사용 구간", str(e), parent=self)
            return
        self._refresh_all(select=i)

    def _apply_advice_to(self, c):
        adv = self.advice.get(c.path)
        if adv:
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

    def _save_settings(self):
        try:
            update_settings(**{SETTINGS_KEY: {"dissolve": self.dissolve_seconds(), "minutes": int(self.minutes.get()),
                                              "quality": self.quality.get(), "outdir": self.outdir.get().strip(),
                                              "profile": self.profile_key()}})
        except Exception:
            pass

    def profile_key(self) -> str:
        label = self.profile.get()
        return next((p.key for p in ENCODE_PROFILES if p.label == label), ENCODE_PROFILES[0].key)

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
        for x in self.tree.get_children():
            self.tree.delete(x)
        for i, c in enumerate(self.clips):
            rng = "전체" if c.start <= 0 and c.end is None else f"{c.used_start:.1f}~{c.used_end:.1f}초"
            kw = {"image": self.thumbs[c.path]} if c.path in self.thumbs else {}
            self.tree.insert("", "end", iid=str(i), text="" if kw else f"#{i + 1}", **kw, values=(
                "✓" if c.enabled else "제외", c.path.name, format_duration(c.info.duration),
                f"{c.info.width}×{c.info.height}", f"{c.info.fps:.2f}", rng, self._loop_cell(c),
                self._quality_cell(c, dup_map)))
        if select is not None and 0 <= select < len(self.clips):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))
        active = [c for c in self.clips if c.enabled]
        self.summary.set(f"영상 {len(self.clips)}개 · 사용 {len(active)}개 · 사용 구간 합계 "
                         f"{format_duration(sum(c.used_length for c in active))}" if self.clips
                         else "MP4 영상을 불러오세요 (30초~2분 길이 권장).")
        self._recalc(dups)
        self._show_detail()
        self._refresh_buttons()

    def _recalc(self, dups=None):
        self.plan = None
        self.profile_note.set(get_profile(self.profile_key()).note)
        active = [c for c in self.clips if c.enabled]
        if not active:
            self.plan_text.set("")
            self.warn_text.set("")
            if self._name_auto:
                self.outname.set("")
            return
        if self._name_auto:
            self.outname.set(default_bg_output_name(self.clips, int(self.minutes.get())))
        warns: list[str] = []
        d, note = self.effective_dissolve()
        if note:
            warns.append(note)
        prof = get_profile(self.profile_key())
        self.profile_note.set(prof.note)
        try:
            spec = choose_output_spec([c.info for c in active], self.quality.get(),
                                      hevc_available=self._hevc_ok() if self.quality.get() == QUALITY_4K else False,
                                      profile=prof.key)
            plan = build_bg_plan(active, int(self.minutes.get()) * 60, d, spec)
        except ValueError as e:
            self.plan_text.set(f"⚠ {e}")
            self.warn_text.set("")
            return
        self.plan = plan
        free_txt = ""
        short = False
        try:
            folder = Path(self.outdir.get().strip()) if self.outdir.get().strip() else active[0].path.parent
            probe_dir = folder if folder.exists() else next((p for p in folder.parents if p.exists()), folder)
            free = free_bytes(probe_dir)
            _, _, need = enough_disk_space(probe_dir, plan.required_bytes)  # 제작 시작 때와 같은 기준 (여유 1 GB 포함)
            short = free < need
            free_txt = f" · 여유 공간 {free / 1024**3:.1f} GB" + (f" ⚠ 부족 (필요 약 {need / 1024**3:.1f} GB)" if short else "")
        except Exception:
            pass
        self.plan_text.set(
            f"{plan.spec.label} · {plan.spec.fps:g} FPS · 디졸브 {plan.dissolve:.2f}초 · 한 바퀴 {format_duration(plan.cycle_duration)}"
            f" × 약 {plan.target / plan.cycle_duration:.1f}바퀴 → {format_duration(plan.target)} · "
            f"예상 용량 약 {plan.expected_size / 1024**3:.2f} GB · 작업 중 최대 약 {plan.required_bytes / 1024**3:.2f} GB 사용"
            f"{free_txt}")
        warns.append("예상 용량은 넉넉하게 잡은 값입니다. 화면이 세밀할수록(물결·나뭇잎) 커지며, 제작 중 실제 크기로 다시 "
                     "확인해 공간이 모자라면 디스크가 가득 차기 전에 자동으로 멈춥니다 (원본·기존 파일은 그대로).")
        if short:
            warns.append("저장 공간이 부족합니다. 다른 저장 폴더를 고르거나, 길이를 줄이거나, [화질·용량]에서 용량이 작은 설정을 고르세요.")
        if prof.key in ("balanced", "small"):
            warns.append(f"[{prof.label}] {prof.note}")
        warns += list(plan.warnings)
        if len(active) == 1:
            adv = self.verdict_advice(active[0])
            if adv is not None and adv.verdict != A.VERDICT_GOOD:
                warns.append(f"[{adv.verdict}] {' '.join(adv.reasons)}")
            elif adv is None:
                warns.append("반복 지점 분석이 끝나지 않았습니다 (확인 필요).")
        ordered = [self.analyses[c.path] for c in active if c.path in self.analyses]
        if len(ordered) == len(active):
            warns += A.transition_warnings(ordered)
        for f in dups or []:
            warns.append(f"중복 의심 ({f.kind}): {f.a.name} ↔ {f.b.name} — 원본은 삭제하지 않습니다. 사용 여부를 직접 정하세요.")
        bad = [c.path.name for c in active if c.path in self.analysis_errors]
        if bad:
            warns.append("분석 실패(손상 가능): " + ", ".join(bad))
        self.warn_text.set("\n".join("⚠ " + w for w in warns))

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
        lines = [f"{c.path.name} · {format_duration(info.duration)} · {info.width}×{info.height} · {info.fps:.2f} FPS · "
                 f"{info.video_codec.upper()}{' · 오디오 있음(제작 시 제거)' if info.audio_codec else ''}"]
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
            lines.append("품질 참고: " + (" / ".join(an.quality_warnings) if an.quality_warnings else "특이사항 없음"))
        self.detail.set("\n".join(lines))

    def _refresh_buttons(self):
        busy = self.running or bool(self.loading) or self.previewing
        try:
            sel = self._selected_index() is not None
            for b in (self.btn_preview_loop, self.btn_preview_next):
                b.configure(state="disabled" if busy or not sel else "normal")
            self.btn_start.configure(state="disabled" if busy or not any(c.enabled for c in self.clips) else "normal")
            self.btn_stop.configure(state="normal" if self.running else "disabled")
            done = self.last_output is not None and self.last_output.exists()
            for b in (self.btn_open, self.btn_folder):
                b.configure(state="normal" if done else "disabled")
            self.btn_send.configure(state="normal" if done and self.send_to_long_cb else "disabled")
        except tk.TclError:
            pass

    # ---------------- 제작 ----------------
    def start(self, *, confirm=True):
        if self.running or self.loading or self.previewing:
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
        self.status.set(f"완성: {r.output.name} · {format_duration(r.duration)} · {r.width}×{r.height} · "
                        f"{r.fps:g} FPS · {r.size / 1024**2:.0f} MB · 걸린 시간 {format_duration(r.elapsed)}")
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
        if i is None or self.running or self.loading or self.previewing:
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
        folder = Path(tempfile.gettempdir()) / "PLVM_BG_PREVIEW"
        out = folder / f"seam_{uuid.uuid4().hex[:8]}.mp4"
        self.previewing = True
        self.preview_cancel.clear()
        label = (f"{a.path.name} 끝 → 처음" if b is a else f"{a.path.name} → {b.path.name}")
        self.status.set(f"미리보기 만드는 중… ({label})")
        self._refresh_buttons()
        t = threading.Thread(target=self._preview_worker, args=(tools[0], a, b, d, out, label), daemon=True)
        self._threads.append(t)
        t.start()
        return True

    def _preview_worker(self, ff, a, b, d, out, label):
        try:
            p = make_seam_preview(Path(ff), a, b, d, out, self.preview_cancel)
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
        self.status.set(f"미리보기: {label} · 앞 {PREVIEW_CONTEXT:g}초 → 디졸브 → 뒤 {PREVIEW_CONTEXT:g}초 (저화질, 소리 없음)")
        self._refresh_buttons()
        _open_path(path)

    # ---------------- 결과 ----------------
    def _open_output(self):
        if self.last_output and self.last_output.exists():
            _open_path(self.last_output)

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


def _open_path(p: Path):
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:  # pragma: no cover
            subprocess.Popen(["xdg-open", str(p)])
    except OSError as e:
        messagebox.showerror("열기", str(e))
