"""⑥ 음악 플레이리스트 만들기 — 독립 창 (Tkinter).

흐름: 배경 MP4 선택(④ 결과 권장) → 음원 15곡 추가(실제 길이 자동 분석) → 순서·제목 편집 → 출력 설정 →
[SRT만 저장] 또는 [▶ 플레이리스트 MP4 제작] → 완성 파일 재생 → ①/②/③으로 보내기 (보내기만, 자동 시작 없음).
FFmpeg 작업은 백그라운드 스레드, 화면 갱신은 이벤트 큐로 Tk main thread에서만.
제작 FFmpeg는 FFMPEG_GUARD로 ①④⑤·LIVE와 동시에 돌지 않는다.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import media_audio as MA
from . import pl_core as P
from .bg_core import CODEC_H264, CODEC_HEVC, ENCODE_PROFILES, QUALITY_4K, QUALITY_FHD, get_profile, hevc_available
from .core import BuildCancelled, VideoInfo, ensure_unique_output, format_duration, free_bytes, probe_video
from .settings import load_settings, update_settings
from .tooling import FFMPEG_GUARD, prevent_windows_sleep, release_tk_variables
from .ui_scroll import ScrollFrame

TITLE = "⑥ 음악 플레이리스트 만들기"
SETTINGS_KEY = "playlist_maker"
STEPS = ("배경 MP4", "음원 15곡", "순서·제목", "출력 설정", "SRT·제작", "완성 파일 재생")
CIRCLED = "①②③④⑤⑥"


def _busy_text(owner):
    from .live_supervisor import busy_message
    return busy_message(owner)


def _hms(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Item:
    """목록의 한 곡: 분석 전에는 track=None. 제목은 사용자가 바꿀 수 있다 (원본 파일은 그대로)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.title = P.title_from_filename(self.path.name)
        self.track: P.Track | None = None
        self.error: str | None = None


class PlaylistMakerWindow(tk.Toplevel):
    def __init__(self, master, *, tools, send_to_long=None, send_to_live=None, send_to_upload=None,
                 peek_tools=None):
        super().__init__(master)
        self.title(TITLE)
        sh = self.winfo_screenheight()
        self.geometry(f"1180x{max(560, min(940, sh - 90))}")
        self.minsize(900, 520)
        self.tools = tools                    # 사용자 조작 시: () -> (ffmpeg, ffprobe) | (None, None)
        self.peek_tools = peek_tools or tools
        self.send_cbs = {"long": send_to_long, "live": send_to_live, "upload": send_to_upload}
        self.items: list[Item] = []
        self.bg: VideoInfo | None = None
        self.bg_cycle: float | None = None
        self.bg_error: str | None = None
        self.bg_audio: MA.AudioCheck | None = None
        self.always_exclude = tk.BooleanVar(value=True)
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.stop_loading = threading.Event()
        self.loading = 0
        self.running = False
        self.last_output: Path | None = None
        self.last_srt: Path | None = None
        self.result: P.PlaylistResult | None = None
        self.plan: P.PlaylistPlan | None = None
        self.build_plan: P.PlaylistPlan | None = None
        self.plan_error = ""
        self.played = False
        self._hevc: bool | None = None
        self._subs: bool | None = None
        self._threads: list[threading.Thread] = []
        saved = load_settings().get(SETTINGS_KEY)
        saved = saved if isinstance(saved, dict) else {}
        self.quality = tk.StringVar(value=saved.get("quality") if saved.get("quality") in (QUALITY_FHD, QUALITY_4K) else QUALITY_FHD)
        self.codec = tk.StringVar(value=CODEC_HEVC if saved.get("codec") == CODEC_HEVC else CODEC_H264)
        self.profile = tk.StringVar(value=get_profile(saved.get("profile")).label)
        kb = saved.get("audio_kbps")
        self.audio_kbps = tk.IntVar(value=kb if kb in P.AUDIO_KBPS else P.DEFAULT_AUDIO_KBPS)
        self.crossfade = tk.StringVar(value="없음 (기본)")
        self.burn = tk.BooleanVar(value=bool(saved.get("burn", False)))
        self.save_srt = tk.BooleanVar(value=bool(saved.get("save_srt", True)))
        self.outdir = tk.StringVar(value=str(saved.get("outdir", "")))
        self.outname = tk.StringVar()
        self.bg_text = tk.StringVar(value="배경 MP4를 고르세요 (④ 배경 만들기로 만든 영상 권장).")
        self.summary = tk.StringVar(value=f"음원을 추가하세요 (기본 {P.SET_SIZE}곡 1세트).")
        self.title_edit = tk.StringVar()
        self.plan_text = tk.StringVar()
        self.warn_text = tk.StringVar()
        self.status = tk.StringVar(value="대기 중")
        self.step_hint = tk.StringVar()
        self._auto_name = ""
        self._ui()
        self._refresh_all()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._pump)

    # ---------------- 화면 ----------------
    def _ui(self):
        st = ttk.Style(self)
        st.configure("StepNow.TLabel", foreground="#ffffff", background="#2f6fdf", padding=(6, 2))
        st.configure("StepDone.TLabel", foreground="#1d7a3a", padding=(6, 2))
        st.configure("StepTodo.TLabel", foreground="#777777", padding=(6, 2))
        bottom = ttk.Frame(self, padding=(10, 4, 10, 8))
        bottom.pack(side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        root = ttk.Frame(self.scroll.body, padding=10)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text=TITLE, font="PLS.Title").pack(anchor="w")
        ttk.Label(root, text=f"배경 영상 + 음원 {P.SET_SIZE}곡 → 곡 순서대로 이어진 플레이리스트 MP4와, 곡이 시작할 때 제목을 "
                             f"{P.TITLE_SECONDS:g}초 보여 주는 SRT 자막을 만듭니다. 원본 음원·영상은 바꾸지 않습니다.",
                  style="Hint.TLabel", wraplength=1100, justify="left").pack(anchor="w", pady=(0, 4))
        sb = ttk.Frame(root); sb.pack(fill="x", pady=(0, 4))
        self.step_labels = []
        for k, name in enumerate(STEPS):
            if k:
                ttk.Label(sb, text="→", style="Hint.TLabel").pack(side="left")
            lb = ttk.Label(sb, text=f"{CIRCLED[k]} {name}", style="StepTodo.TLabel")
            lb.pack(side="left")
            self.step_labels.append(lb)
        ttk.Label(root, textvariable=self.step_hint, font="PLS.Strong").pack(anchor="w", pady=(0, 4))

        f0 = ttk.LabelFrame(root, text="① 배경 MP4", padding=6)
        f0.pack(fill="x")
        r0 = ttk.Frame(f0); r0.pack(fill="x")
        ttk.Button(r0, text="① 배경 MP4 선택", command=self._pick_bg, style="Primary.TButton").pack(side="left")
        ttk.Button(r0, text="▶ 배경 미리보기", command=lambda: self.bg and _open_path(self.bg.path)).pack(side="left", padx=6)
        ttk.Label(f0, textvariable=self.bg_text, wraplength=1100, justify="left").pack(anchor="w", pady=(4, 0))
        a0 = ttk.Frame(f0); a0.pack(fill="x", pady=(2, 0))
        ttk.Checkbutton(a0, text=MA.EXCLUDE_OPTION, variable=self.always_exclude, state="disabled").pack(side="left")
        ttk.Label(a0, text="완성 영상의 소리는 아래 음원만 사용합니다 (배경의 자연음·말소리·음악은 넣지 않음).",
                  style="Hint.TLabel").pack(side="left", padx=(6, 0))

        f1 = ttk.LabelFrame(root, text=f"② 음원 (기본 {P.SET_SIZE}곡)   ③ 순서 · 제목 — 곡 시작 시각과 제목 표시 구간은 자동 계산",
                            padding=6)
        f1.pack(fill="both", expand=True, pady=(6, 0))
        br = ttk.Frame(f1); br.pack(fill="x", pady=(0, 4))
        ttk.Button(br, text="② ＋ 음원 추가 (MP3·WAV 등)", command=self._pick_audio, style="Primary.TButton").pack(side="left")
        ttk.Button(br, text="▲ 위로", command=lambda: self._move(-1)).pack(side="left", padx=(8, 2))
        ttk.Button(br, text="▼ 아래로", command=lambda: self._move(1)).pack(side="left")
        ttk.Button(br, text="파일명 순 정렬", command=self._sort_by_name).pack(side="left", padx=(8, 2))
        ttk.Button(br, text="목록에서 빼기", command=self._remove).pack(side="left", padx=2)
        ttk.Button(br, text="모두 비우기", command=self._clear).pack(side="left", padx=2)
        tf = ttk.Frame(f1); tf.pack(fill="both", expand=True)
        cols = ("no", "title", "file", "dur", "start", "sub", "state")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=10, selectmode="browse")
        for c, h, w in zip(cols, ("번호", "곡 제목 (자막에 표시)", "파일", "길이", "시작 시각", f"제목 자막 ({P.TITLE_SECONDS:g}초)", "상태"),
                           (44, 260, 230, 64, 76, 150, 230)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("title", "file", "state") else "center")
        vs = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        er = ttk.Frame(f1); er.pack(fill="x", pady=(4, 0))
        ttk.Label(er, text="선택한 곡 제목").pack(side="left")
        ent = ttk.Entry(er, textvariable=self.title_edit, width=50)
        ent.pack(side="left", padx=4)
        ent.bind("<Return>", lambda e: self._apply_title())
        ttk.Button(er, text="제목 적용", command=self._apply_title).pack(side="left")
        ttk.Button(er, text="파일명에서 다시", command=self._reset_title).pack(side="left", padx=4)
        ttk.Label(f1, textvariable=self.summary, font="PLS.Strong", wraplength=1100, justify="left").pack(anchor="w", pady=(4, 0))

        f3 = ttk.LabelFrame(root, text="④ 출력 설정 · 예상 용량", padding=6)
        f3.pack(fill="x", pady=(6, 0))
        o1 = ttk.Frame(f3); o1.pack(fill="x")
        ttk.Label(o1, text="해상도").pack(side="left")
        ttk.Radiobutton(o1, text="FHD 1920×1080 (기본)", value=QUALITY_FHD, variable=self.quality,
                        command=self._options_changed).pack(side="left", padx=2)
        ttk.Radiobutton(o1, text="4K (배경이 4K 원본일 때만)", value=QUALITY_4K, variable=self.quality,
                        command=self._options_changed).pack(side="left", padx=2)
        ttk.Label(o1, text="   코덱").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(o1, text="H.264 (기본 · LIVE·업로드 호환)", value=CODEC_H264, variable=self.codec,
                        command=self._options_changed).pack(side="left", padx=2)
        ttk.Radiobutton(o1, text="H.265 (저장·업로드용 · LIVE 불가)", value=CODEC_HEVC, variable=self.codec,
                        command=self._options_changed).pack(side="left", padx=2)
        o2 = ttk.Frame(f3); o2.pack(fill="x", pady=(4, 0))
        ttk.Label(o2, text="화질·용량 (영상을 인코딩할 때만)").pack(side="left")
        for p in ENCODE_PROFILES:
            ttk.Radiobutton(o2, text=p.short or p.label, value=p.label, variable=self.profile,
                            command=self._options_changed).pack(side="left", padx=2)
        ttk.Label(o2, text="   음질 AAC").pack(side="left", padx=(10, 0))
        for k in P.AUDIO_KBPS:
            ttk.Radiobutton(o2, text=f"{k}k" + (" (기본)" if k == P.DEFAULT_AUDIO_KBPS else ""), value=k,
                            variable=self.audio_kbps, command=self._options_changed).pack(side="left", padx=2)
        o3 = ttk.Frame(f3); o3.pack(fill="x", pady=(4, 0))
        ttk.Label(o3, text="곡 사이 크로스페이드").pack(side="left")
        cb = ttk.Combobox(o3, textvariable=self.crossfade, state="readonly", width=12,
                          values=["없음 (기본)"] + [f"{v:g}초" for v in P.CROSSFADE_CHOICES if v > 0])
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._options_changed())
        ttk.Checkbutton(o3, text="SRT 파일 따로 저장 (권장)", variable=self.save_srt,
                        command=self._options_changed).pack(side="left", padx=(12, 2))
        ttk.Checkbutton(o3, text="곡 제목을 영상에 입히기 (영상 인코딩 필요 · 시간이 더 걸림)", variable=self.burn,
                        command=self._options_changed).pack(side="left", padx=(12, 2))
        o4 = ttk.Frame(f3); o4.pack(fill="x", pady=(4, 0))
        ttk.Label(o4, text="저장 폴더", width=9).pack(side="left")
        ttk.Entry(o4, textvariable=self.outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(o4, text="변경", command=self._pick_outdir).pack(side="left", padx=4)
        ttk.Label(o4, text="파일명").pack(side="left", padx=(8, 2))
        ttk.Entry(o4, textvariable=self.outname, width=34).pack(side="left")
        ttk.Label(f3, textvariable=self.plan_text, wraplength=1100, justify="left").pack(anchor="w", pady=(4, 0))
        ttk.Label(f3, textvariable=self.warn_text, foreground="#9a5b00", wraplength=1100, justify="left").pack(anchor="w", pady=(2, 0))

        ar = ttk.Frame(bottom); ar.pack(fill="x")
        self.btn_srt = ttk.Button(ar, text="⑤ SRT만 저장 (영상 작업 없음)", command=self.save_srt_only)
        self.btn_srt.pack(side="left")
        self.btn_start = ttk.Button(ar, text="⑤ ▶ 플레이리스트 MP4 제작", command=self.start, style="Primary.TButton")
        self.btn_start.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.btn_stop = ttk.Button(ar, text="■ 중단", command=self.stop, state="disabled", style="Danger.TButton")
        self.btn_stop.pack(side="left", padx=(6, 0))
        self.bar = ttk.Progressbar(bottom, maximum=100)
        self.bar.pack(fill="x", pady=(6, 0))
        ttk.Label(bottom, textvariable=self.status, wraplength=1100, justify="left").pack(anchor="w")
        rr = ttk.Frame(bottom); rr.pack(fill="x", pady=(4, 0))
        self.btn_open = ttk.Button(rr, text="⑥ ▶ 완성 파일 재생", command=self._open_output, state="disabled")
        self.btn_open.pack(side="left")
        self.btn_folder = ttk.Button(rr, text="폴더 열기", command=self._open_folder, state="disabled")
        self.btn_folder.pack(side="left", padx=4)
        self.btn_long = ttk.Button(rr, text="① 영상 늘리기로", command=lambda: self._send("long"), state="disabled")
        self.btn_long.pack(side="left", padx=(12, 2))
        self.btn_live = ttk.Button(rr, text="② LIVE Playlist에 추가", command=lambda: self._send("live"), state="disabled")
        self.btn_live.pack(side="left", padx=2)
        self.btn_upload = ttk.Button(rr, text="③ 예약 업로드로", command=lambda: self._send("upload"), state="disabled")
        self.btn_upload.pack(side="left", padx=2)
        ttk.Label(rr, text="보내기만 합니다 — LIVE·업로드는 각 화면에서 직접 시작해야 합니다.",
                  style="Hint.TLabel").pack(side="left", padx=8)

    # ---------------- 도구 ----------------
    def _tools(self):
        try:
            ff, fp = self.tools() or (None, None)
        except Exception:
            ff = fp = None
        if not ff or not fp:
            messagebox.showerror("FFmpeg", "FFmpeg/ffprobe를 찾지 못했습니다. 메인 화면에서 FFmpeg를 설정하세요.", parent=self)
            return None
        return Path(ff), Path(fp)

    def _subs_ok(self) -> bool:
        if self._subs is None:
            try:
                ff = (self.peek_tools() or (None, None))[0]
                self._subs = bool(ff) and P.subtitles_available(Path(ff))
            except Exception:
                self._subs = False
        return self._subs

    def _hevc_ok(self) -> bool:
        if self._hevc is None:
            try:
                ff = (self.peek_tools() or (None, None))[0]
                self._hevc = bool(ff) and hevc_available(Path(ff))
            except Exception:
                self._hevc = False
        return self._hevc

    # ---------------- 불러오기 ----------------
    def _pick_bg(self):
        p = filedialog.askopenfilename(parent=self, title="배경 MP4 선택 (④에서 만든 영상 권장)",
                                       filetypes=[("MP4", "*.mp4"), ("영상", "*.mov *.mkv *.m4v"), ("모든 파일", "*.*")])
        if p:
            self.set_background(Path(p))

    def set_background(self, path: Path) -> bool:
        if self.running:
            return False
        tools = self._tools()
        if not tools:
            return False
        self.bg = None
        self.bg_cycle = None
        self.bg_error = None
        self.bg_audio = None
        self.bg_text.set(f"배경 영상 정보 읽는 중… {Path(path).name}")
        self._spawn(self._bg_worker, tools[1], Path(path))
        return True

    def _bg_worker(self, fp, path):
        try:
            info = probe_video(path, fp)
            self.events.put(("bg", info, P.read_bg_cycle(path, info), MA.check_audio(fp, path)))
        except Exception as e:
            self.events.put(("bg_error", str(e)))

    def _pick_audio(self):
        paths = filedialog.askopenfilenames(parent=self, title="음원 선택 (여러 개 가능 · 기본 15곡)",
                                            filetypes=[("음원", " ".join(f"*{x}" for x in P.AUDIO_EXTS)), ("모든 파일", "*.*")])
        if paths:
            self.add_audio_paths([Path(p) for p in paths])

    def add_audio_paths(self, paths) -> bool:
        if self.running:
            return False
        tools = self._tools()
        if not tools:
            return False
        have = {i.path.resolve() for i in self.items}
        fresh, skipped = [], []
        for p in sorted((Path(x) for x in paths), key=lambda x: x.name.lower()):
            if p.suffix.lower() not in P.AUDIO_EXTS:
                skipped.append(f"{p.name}: 지원하지 않는 형식")
                continue
            if p.resolve() in have:
                skipped.append(f"{p.name}: 이미 목록에 있음")
                continue
            if len(self.items) + len(fresh) >= P.MAX_TRACKS:
                skipped.append(f"{p.name}: 최대 {P.MAX_TRACKS}곡")
                continue
            have.add(p.resolve())
            fresh.append(Item(p))
        if skipped and not getattr(self, "quiet_errors", False):
            messagebox.showwarning("음원 추가", "\n".join(skipped[:12]), parent=self)
        if not fresh:
            return False
        self.items.extend(fresh)
        if not self.outdir.get().strip():
            self.outdir.set(str(fresh[0].path.parent / "PLAYLIST_OUTPUT"))
        self._spawn(self._audio_worker, tools, list(fresh))
        self._refresh_all()
        return True

    def _audio_worker(self, tools, items):
        ff, fp = tools
        for k, it in enumerate(items, 1):
            if self.stop_loading.is_set():
                return
            self.events.put(("status", f"음원 실제 길이 확인 중 ({k}/{len(items)}) · {it.path.name}"))
            try:
                self.events.put(("track", it, P.analyze_track(ff, fp, it.path, self.stop_loading)))
            except BuildCancelled:
                return
            except Exception as e:
                self.events.put(("track_error", it, str(e)))

    def _spawn(self, fn, *args):
        self.loading += 1
        self.stop_loading.clear()

        def guard():
            try:
                fn(*args)
            finally:
                self.events.put(("loaded",))
        t = threading.Thread(target=guard, daemon=True)
        self._threads.append(t)
        t.start()

    # ---------------- 이벤트 ----------------
    def _pump(self):
        changed = False
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "status":
                    self.status.set(ev[1])
                elif kind == "bg":
                    self.bg, self.bg_cycle, self.bg_audio = ev[1], ev[2], ev[3]
                    changed = True
                elif kind == "bg_error":
                    self.bg_error = ev[1]
                    changed = True
                elif kind == "track":
                    it, tr = ev[1], ev[2]
                    it.track, it.error = tr, None
                    changed = True
                elif kind == "track_error":
                    ev[1].error = ev[2]
                    changed = True
                elif kind == "loaded":
                    self.loading = max(0, self.loading - 1)
                    if not self.loading and not self.running:
                        self.status.set("준비됨 · 순서와 제목을 확인하세요." if self.items else "대기 중")
                    changed = True
                elif kind == "progress":
                    self.bar["value"] = ev[1] * 100
                    self.status.set(ev[2])
                elif kind == "done":
                    self._finished(ev[1])
                elif kind == "failed":
                    self._failed(ev[1], ev[2])
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
    def _sel(self) -> int | None:
        s = self.tree.selection()
        return self.tree.index(s[0]) if s else None

    def _move(self, d: int):
        i = self._sel()
        if i is None or self.running:
            return
        j = i + d
        if 0 <= j < len(self.items):
            self.items[i], self.items[j] = self.items[j], self.items[i]
            self._refresh_all(select=j)

    def move_item(self, i: int, j: int):
        """i번째 곡을 j 위치로 (테스트·자동화용). 시작 시각·SRT는 자동으로 다시 계산된다."""
        it = self.items.pop(i)
        self.items.insert(j, it)
        self._refresh_all(select=j)

    def _sort_by_name(self):
        if not self.running:
            self.items.sort(key=lambda it: it.path.name.lower())
            self._refresh_all()

    def _remove(self):
        i = self._sel()
        if i is not None and not self.running:
            self.items.pop(i)
            self._refresh_all(select=min(i, len(self.items) - 1) if self.items else None)

    def _clear(self):
        if not self.running and self.items and (getattr(self, "quiet_errors", False) or messagebox.askyesno(
                "모두 비우기", "음원 목록을 비울까요? (파일은 지우지 않습니다)", parent=self)):
            self.items.clear()
            self._refresh_all()

    def _on_select(self):
        i = self._sel()
        if i is not None:
            self.title_edit.set(self.items[i].title)

    def set_title(self, i: int, title: str):
        title = " ".join(str(title).split())
        if title:
            self.items[i].title = title
            self._refresh_all(select=i)

    def _apply_title(self):
        i = self._sel()
        if i is not None and not self.running:
            self.set_title(i, self.title_edit.get())

    def _reset_title(self):
        i = self._sel()
        if i is not None and not self.running:
            self.set_title(i, P.title_from_filename(self.items[i].path.name))
            self.title_edit.set(self.items[i].title)

    def _pick_outdir(self):
        d = filedialog.askdirectory(parent=self, title="저장 폴더")
        if d:
            self.outdir.set(d)
            self._refresh_all()

    def _options_changed(self):
        self._save_settings()
        self._refresh_all()

    def _save_settings(self):
        try:
            update_settings(**{SETTINGS_KEY: {
                "quality": self.quality.get(), "codec": self.codec.get(), "profile": self.profile_key(),
                "audio_kbps": int(self.audio_kbps.get()), "burn": bool(self.burn.get()),
                "save_srt": bool(self.save_srt.get()), "outdir": self.outdir.get().strip()}})
        except Exception:
            pass

    def profile_key(self) -> str:
        return next((p.key for p in ENCODE_PROFILES if p.label == self.profile.get()), ENCODE_PROFILES[0].key)

    def crossfade_seconds(self) -> float:
        t = self.crossfade.get()
        try:
            return float(t.replace("초", "")) if "초" in t else 0.0
        except ValueError:
            return 0.0

    # ---------------- 계산 ----------------
    def ready_tracks(self) -> list[P.Track]:
        return [replace(it.track, title=it.title) for it in self.items if it.track is not None]

    def timeline(self):
        tracks = self.ready_tracks()
        if not tracks or len(tracks) != len(self.items):
            return [], 0.0
        return P.build_timeline(tracks, self.crossfade_seconds())

    def _recalc(self):
        self.plan = None
        self.plan_error = ""
        tracks = self.ready_tracks()
        pending = [it for it in self.items if it.track is None and it.error is None]
        bad = [it for it in self.items if it.error]
        if not self.items:
            self.plan_error = "음원을 추가하세요."
        elif pending:
            self.plan_error = f"음원 길이 확인 중… ({len(pending)}곡 남음)"
        elif bad:
            self.plan_error = f"읽을 수 없는 음원 {len(bad)}곡이 있습니다 — 목록에서 빼세요."
        elif self.bg is None:
            self.plan_error = self.bg_error or "배경 MP4를 고르세요."
        elif self.burn.get() and not self._subs_ok():
            self.plan_error = ("이 FFmpeg에는 자막 입히기(libass · subtitles 필터)가 없습니다. '곡 제목을 영상에 입히기'를 끄고 "
                               "SRT 파일만 쓰거나, libass가 포함된 FFmpeg(예: Gyan full/essentials)로 바꾸세요.")
        else:
            try:
                self.plan = P.build_playlist_plan(
                    tracks, self.bg, cycle=self.bg_cycle, quality=self.quality.get(), profile=self.profile_key(),
                    codec=self.codec.get(), burn_titles=bool(self.burn.get()), audio_kbps=int(self.audio_kbps.get()),
                    crossfade=self.crossfade_seconds(), hevc_ok=self._hevc_ok() if self.codec.get() == CODEC_HEVC else False)
            except ValueError as e:
                self.plan_error = str(e)

    def _refresh_all(self, select: int | None = None):
        if select is None:
            select = self._sel() if hasattr(self, "tree") else None
        self._recalc()
        try:
            entries, total = self.timeline()
        except ValueError as e:
            entries, total = [], 0.0
            self.plan_error = str(e)
        dups = P.duplicate_findings([it.track for it in self.items if it.track is not None])
        try:
            self.tree.delete(*self.tree.get_children())
            for k, it in enumerate(self.items):
                e = entries[k] if k < len(entries) else None
                if it.error:
                    state = "⛔ " + it.error.splitlines()[0][:60]
                elif it.track is None:
                    state = "길이 확인 중…"
                else:
                    state = "✓ " + ("; ".join(w[:40] for w in it.track.warnings) if it.track.warnings else "정상")
                self.tree.insert("", "end", iid=str(k), values=(
                    f"{k + 1:02d}", it.title, it.path.name, _hms(it.track.duration) if it.track else "-",
                    _hms(e.start) if e else "-",
                    f"{_hms(e.title_start)} → {_hms(e.title_end)} ({e.title_end - e.title_start:.1f}초)" if e else "-",
                    state))
            if select is not None and 0 <= select < len(self.items):
                self.tree.selection_set(str(select))
                self.tree.see(str(select))
            n = len(self.items)
            if n:
                head = f"{n}곡" + ("" if n == P.SET_SIZE else f" (기본 1세트 {P.SET_SIZE}곡)")
                self.summary.set(f"{head} · 총 재생시간 {format_duration(total) if total else '계산 중'}"
                                 + ("" if not dups else " · ⚠ " + " / ".join(dups[:3])))
            else:
                self.summary.set(f"음원을 추가하세요 (기본 {P.SET_SIZE}곡 1세트).")
            if self.bg is not None:
                cyc = (f" · ④ 한 바퀴 {format_duration(self.bg_cycle)} 경계에서 자연스럽게 반복" if self.bg_cycle
                       else " · ④ 정보 없음: 파일 전체를 반복 (끝 → 처음 이음 확인 권장)")
                self.bg_text.set(f"{self.bg.path.name} · {self.bg.width}×{self.bg.height} · {self.bg.video_codec.upper()} "
                                 f"{self.bg.pix_fmt} · {self.bg.fps:g} FPS · {format_duration(self.bg.duration)}{cyc}"
                                 + (f"\n{self.bg_audio.detail}" if self.bg_audio else ""))
            elif self.bg_error:
                self.bg_text.set("⛔ " + self.bg_error.splitlines()[0])
            if self.plan:
                pl = self.plan
                rep = (f"배경 {pl.loop.repeats}번 반복" if pl.loop.repeats > 1 else "배경 반복 없음 (음악 끝에서 함께 종료)")
                free = ""
                try:
                    if self.outdir.get().strip():
                        free = f" · 저장 폴더 남은 공간 {free_bytes(Path(self.outdir.get().strip())) / 1024**3:.1f} GB"
                except OSError:
                    free = ""
                self.plan_text.set(f"영상: {pl.video_label} · 오디오: AAC {pl.audio_kbps}k {P.AUDIO_RATE // 1000}kHz 스테레오 · "
                                   f"{rep} · 길이 {format_duration(pl.total)} · 예상 용량 약 {pl.expected_size / 1024**2:,.0f} MB "
                                   f"(필요 공간 약 {pl.required_bytes / 1024**2:,.0f} MB){free}")
                self.warn_text.set("\n".join("• " + n for n in pl.notes))
            else:
                self.plan_text.set(self.plan_error)
                self.warn_text.set("")
            cur = self.outname.get().strip()
            if self.items and (not cur or cur == self._auto_name):  # 사용자가 바꾼 이름은 덮어쓰지 않는다
                self._auto_name = P.default_output_name(self.items, self.bg)
                self.outname.set(self._auto_name)
            self._refresh_buttons()
            self._update_steps()
        except tk.TclError:
            pass

    def current_step(self) -> int:
        if self.bg is None:
            return 0
        if not self.items or any(it.track is None and it.error is None for it in self.items):
            return 1
        if self.running:
            return 4
        if self.last_output is not None and self.last_output.exists():
            return 5
        return 4 if self.plan else 2

    def _update_steps(self):
        cur = self.current_step()
        done = set(range(cur))
        if self.played:
            done.add(5)
        hints = {0: "① [배경 MP4 선택] — ④ 배경 만들기로 만든 영상을 쓰면 반복 이음새가 자연스럽습니다.",
                 1: f"② [＋ 음원 추가]로 {P.SET_SIZE}곡을 고르세요. 실제 재생 길이를 자동으로 확인합니다.",
                 2: "③ 순서(▲▼)와 제목을 확인하세요 — 시작 시각과 자막 구간이 바로 다시 계산됩니다.",
                 4: "⑤ [SRT만 저장] 또는 [▶ 플레이리스트 MP4 제작]. 제작 중에는 언제든 [■ 중단]할 수 있습니다.",
                 5: "⑥ 완성! [⑥ 완성 파일 재생]으로 확인한 뒤 필요하면 ①②③으로 보내세요."}
        try:
            for k, lb in enumerate(self.step_labels):
                style = "StepNow.TLabel" if k == cur else ("StepDone.TLabel" if k in done else "StepTodo.TLabel")
                lb.configure(style=style, text=("✓ " if k in done and k != cur else "") + f"{CIRCLED[k]} {STEPS[k]}")
            self.step_hint.set(hints.get(cur, hints[4]))
        except tk.TclError:
            pass

    def _refresh_buttons(self):
        busy = self.running or self.loading > 0
        done = self.last_output is not None and self.last_output.exists() and not self.running
        try:
            self.btn_start.configure(state="normal" if self.plan and not busy else "disabled")
            self.btn_srt.configure(state="normal" if self.timeline()[0] and not self.running else "disabled")
            self.btn_stop.configure(state="normal" if self.running else "disabled")
            for b in (self.btn_open, self.btn_folder):
                b.configure(state="normal" if done else "disabled")
            self.btn_long.configure(state="normal" if done and self.send_cbs["long"] else "disabled")
            self.btn_upload.configure(state="normal" if done and self.send_cbs["upload"] else "disabled")
            live_ok = done and self.send_cbs["live"] and self.result is not None and self.result.codec == "h264"
            self.btn_live.configure(state="normal" if live_ok else "disabled")
        except (tk.TclError, ValueError):
            pass

    # ---------------- SRT · 제작 ----------------
    def _output_dir(self) -> Path | None:
        d = self.outdir.get().strip()
        if not d:
            messagebox.showwarning("저장", "저장 폴더를 정하세요.", parent=self)
            return None
        p = Path(d)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("저장 폴더", f"저장 폴더를 만들 수 없습니다: {e}", parent=self)
            return None
        return p

    def save_srt_only(self) -> Path | None:
        """영상 작업 없이 SRT만 저장 (UTF-8, 새 파일)."""
        try:
            entries, _ = self.timeline()
        except ValueError as e:
            messagebox.showwarning("SRT", str(e), parent=self)
            return None
        if not entries:
            messagebox.showinfo("SRT", "음원 길이 확인이 끝난 뒤 저장할 수 있습니다.", parent=self)
            return None
        d = self._output_dir()
        if d is None:
            return None
        text = P.build_srt(entries)
        probs = P.validate_srt(text, entries)
        if probs:
            messagebox.showerror("SRT", "\n".join(probs[:5]), parent=self)
            return None
        name = Path(self.outname.get().strip() or "playlist").with_suffix(".srt").name
        path = P.write_text_new(d / name, text)
        self.last_srt = path
        self.status.set(f"SRT 저장 완료 (영상 작업 없음): {path.name} · {len(entries)}개 제목 · 각 {P.TITLE_SECONDS:g}초")
        return path

    def start(self) -> bool:
        if self.running or self.loading:
            return False
        self._refresh_all()
        plan = self.plan
        if plan is None:
            messagebox.showwarning("설정 확인", self.plan_error or "배경과 음원을 확인하세요.", parent=self)
            return False
        tools = self._tools()
        if not tools:
            return False
        d = self._output_dir()
        if d is None:
            return False
        name = self.outname.get().strip() or P.default_output_name(self.items, self.bg)
        if not name.lower().endswith(".mp4"):
            name += ".mp4"
        output = ensure_unique_output(d / name)
        if output.resolve() in {t.path for t in plan.tracks} | {plan.bg.path}:
            messagebox.showerror("저장", "원본과 같은 파일로 저장할 수 없습니다.", parent=self)
            return False
        if not FFMPEG_GUARD.try_acquire(P.GUARD_OWNER):
            messagebox.showwarning("FFmpeg 사용 중", _busy_text(FFMPEG_GUARD.owner) + "\n끝난 뒤 다시 시작하세요.", parent=self)
            return False
        P.cleanup_stale_temp(d)
        self._save_settings()
        self.running = True
        self.build_plan = plan
        self.cancel.clear()
        self.last_output = None
        self.result = None
        self.bar["value"] = 0
        self.status.set("제작 준비 중…")
        self._refresh_buttons()
        self._update_steps()
        t = threading.Thread(target=self._build_worker, args=(tools, plan, output, bool(self.save_srt.get())), daemon=True)
        self._threads.append(t)
        t.start()
        return True

    def _build_worker(self, tools, plan, output, save_srt):
        ff, fp = tools
        try:
            with prevent_windows_sleep(True):
                r = P.run_playlist_build(ffmpeg=ff, ffprobe=fp, plan=plan, output=output, cancel_event=self.cancel,
                                         save_srt=save_srt, progress_cb=lambda f, t: self.events.put(("progress", f, t)))
            self.events.put(("done", r))
        except BuildCancelled as e:
            self.events.put(("failed", True, str(e)))
        except Exception as e:
            self.events.put(("failed", False, str(e)))
        finally:
            FFMPEG_GUARD.release(P.GUARD_OWNER)

    def stop(self):
        if self.running:
            self.cancel.set()
            self.btn_stop.configure(state="disabled")
            self.status.set("중단하는 중… (임시 파일 정리)")

    def _finished(self, r: P.PlaylistResult):
        self.running = False
        self.result = r
        self.last_output = r.output
        self.last_srt = r.srt
        self.bar["value"] = 100
        live = "② LIVE 후보 (H.264)" if r.codec == "h264" else "H.265 — LIVE 불가 (저장·업로드용)"
        self.status.set(f"완성: {r.output.name} · {format_duration(r.duration)} · {r.width}×{r.height} {r.codec.upper()} · "
                        f"{r.size / 1024**2:,.0f} MB (예상 {self.build_plan.expected_size / 1024**2:,.0f} MB) · "
                        f"오디오 {r.audio_duration:.2f}초 / 영상 {r.video_duration:.2f}초 · "
                        f"{'영상 무손실 복사' if r.copied_video else '영상 인코딩'} · {live} · "
                        + (f"배경 {r.loop_repeats}회 반복" + ("" if r.natural_loop else " (④ 정보 없음 — 반복 이음 확인 권장)")
                           if r.loop_repeats > 1 else "배경 반복 없음")
                        + (f" · SRT: {r.srt.name}" if r.srt else "") + f" · 걸린 시간 {format_duration(r.elapsed)}")
        self._refresh_buttons()
        self._update_steps()

    def _failed(self, cancelled, msg):
        self.running = False
        self.bar["value"] = 0
        self.status.set("중단됨 (임시 파일 정리 완료, 원본은 그대로)" if cancelled else "제작 실패 (원본은 그대로) · " + msg.splitlines()[0])
        self.error = msg
        self._refresh_buttons()
        self._update_steps()
        if not cancelled and not getattr(self, "quiet_errors", False):
            messagebox.showerror("제작 실패", msg, parent=self)

    # ---------------- 결과 ----------------
    def _open_output(self):
        if self.last_output and self.last_output.exists():
            self.played = True
            _open_path(self.last_output)
            self._update_steps()

    def _open_folder(self):
        if self.last_output and self.last_output.exists() and os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(self.last_output)])

    def _send(self, where: str):
        cb = self.send_cbs.get(where)
        if not (cb and self.last_output and self.last_output.exists()):
            return None
        if where == "live" and (self.result is None or self.result.codec != "h264"):
            self.status.set("H.265 파일은 ② LIVE로 보낼 수 없습니다 (H.264로 다시 만드세요).")
            if not getattr(self, "quiet_errors", False):
                messagebox.showwarning("② LIVE", "H.265 파일은 LIVE로 보낼 수 없습니다 (H.264로 다시 만드세요).", parent=self)
            return None
        return cb(self.last_output)

    # ---------------- 닫기 ----------------
    def _on_close(self):
        if self.running and not messagebox.askyesno("제작 중", "플레이리스트 제작을 중단하고 창을 닫을까요?", parent=self):
            return
        self.destroy()

    def destroy(self):
        self.cancel.set()
        self.stop_loading.set()
        for t in self._threads:
            t.join(timeout=8)
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
