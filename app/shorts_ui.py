"""⑧ 숏츠 만들기 — 독립 창 (Tkinter).

흐름(위에서 아래로): 1) 곡 → 2) 배경 영상 → 3) 설정 → 4) [▶ 숏츠 만들기] → 결과 재생·폴더·③ 예약 업로드로 보내기.
숏츠 엔진은 별도 프로세스(app.shorts_bridge)로만 실행한다. 엔진 이벤트는 읽기 스레드 → 이벤트 큐 → Tk main thread.
FFMPEG_GUARD로 ①④⑤⑥·LIVE와 동시에 FFmpeg를 돌리지 않는다.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import shorts_bridge as SB
from .settings import load_settings, update_settings
from .tooling import FFMPEG_GUARD, prevent_windows_sleep, release_tk_variables
from .ui_scroll import ScrollFrame

TITLE = "⑧ 숏츠 만들기"
GUARD_OWNER = "⑧ 숏츠 만들기"
SETTINGS_KEY = "shorts_maker"
DEFAULT_ENGINE_DIR = Path(r"D:\03_shorts")
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".webm")  # 숏츠 엔진 배경 영상 확장자 (docs/CLI.md)
DURATIONS = (30, 45, 60)
DEFAULTS = {"duration": 45, "fit_mode": "blur", "assign": "random", "use_nvenc": True,
            "name_template": "{stem}_short", "output_dir": "", "engine_dir": ""}
STATUS_TEXT = {"ok": "완료", "failed": "실패", "skipped": "건너뜀"}


def _busy_text(owner):
    from .live_supervisor import busy_message
    return busy_message(owner)


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def load_options() -> dict:
    saved = load_settings().get(SETTINGS_KEY)
    saved = saved if isinstance(saved, dict) else {}
    o = dict(DEFAULTS)
    if saved.get("duration") in DURATIONS:
        o["duration"] = saved["duration"]
    if saved.get("fit_mode") in ("blur", "crop"):
        o["fit_mode"] = saved["fit_mode"]
    if saved.get("assign") in ("random", "rotate"):
        o["assign"] = saved["assign"]
    if isinstance(saved.get("use_nvenc"), bool):
        o["use_nvenc"] = saved["use_nvenc"]
    for k in ("name_template", "output_dir", "engine_dir"):
        if isinstance(saved.get(k), str) and saved[k].strip():
            o[k] = saved[k]
    if not o["engine_dir"] and DEFAULT_ENGINE_DIR.is_dir():
        o["engine_dir"] = str(DEFAULT_ENGINE_DIR)
    return o


class Entry:
    """목록의 한 파일 (곡 또는 배경). 원본은 읽기만 한다."""

    def __init__(self, path: Path, title: str = ""):
        self.path = Path(path)
        self.title = title.strip() or self.path.stem


class ShortsMakerWindow(tk.Toplevel):
    def __init__(self, master, *, send_to_upload=None):
        super().__init__(master)
        self.title(TITLE)
        sh = self.winfo_screenheight()
        self.geometry(f"1000x{max(560, min(960, sh - 90))}")
        self.minsize(820, 520)
        self.send_to_upload = send_to_upload
        self.songs: list[Entry] = []
        self.bgs: list[Entry] = []
        self.events: queue.Queue = queue.Queue()
        self.run: SB.ShortsRun | None = None
        self.running = False
        self.run_items: list[Entry] = []
        self.results: list[dict] = []
        self.total = 0
        self.last_exit: dict | None = None
        self._stopping = False
        self.error_message = ""
        self.quiet_errors = False  # 테스트용: 오류 대화상자 대신 상태 글자만
        self._threads: list[threading.Thread] = []
        o = load_options()
        self.duration = tk.IntVar(value=o["duration"])
        self.fit_mode = tk.StringVar(value=o["fit_mode"])
        self.assign = tk.StringVar(value=o["assign"])
        self.use_nvenc = tk.BooleanVar(value=o["use_nvenc"])
        self.name_template = tk.StringVar(value=o["name_template"])
        self.outdir = tk.StringVar(value=o["output_dir"])
        self.engine_dir = tk.StringVar(value=o["engine_dir"])
        self.engine_text = tk.StringVar(value="[연결 확인]을 눌러 숏츠 엔진을 확인하세요.")
        self.song_text = tk.StringVar()
        self.bg_text = tk.StringVar()
        self.now_text = tk.StringVar(value="대기 중")
        self.status = tk.StringVar(value="곡과 배경 영상을 추가하세요.")
        self._ui()
        self._refresh_lists()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._pump)

    # ---------------- 화면 ----------------
    def _ui(self):
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        root = ttk.Frame(self.scroll.body, padding=10)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text=TITLE, font="PLS.Title").pack(anchor="w")
        ttk.Label(root, text="곡마다 하이라이트를 자동으로 찾아 배경 영상 위에 9:16 숏츠를 1개씩 만듭니다. "
                             "원본 음원·영상은 바꾸지 않습니다. 위에서 아래로 차례대로 따라 하세요.",
                  style="Hint.TLabel", wraplength=940, justify="left").pack(anchor="w", pady=(0, 6))

        self.song_tree = self._list_box(root, "1) 곡", self.song_text, self._add_song_files, self._add_song_folder,
                                        "songs", "지원: " + " ".join(e[1:] for e in AUDIO_EXTS))
        self.bg_tree = self._list_box(root, "2) 배경 영상", self.bg_text, self._add_bg_files, self._add_bg_folder,
                                      "bgs", "④ 내 영상으로 배경 만들기 결과(소리 없는 배경)를 권장")

        f3 = ttk.LabelFrame(root, text="3) 설정", padding=7)
        f3.pack(fill="x", pady=(6, 0))
        r = ttk.Frame(f3); r.pack(fill="x", pady=1)
        ttk.Label(r, text="길이", width=14).pack(side="left")
        for sec in DURATIONS:
            ttk.Radiobutton(r, text=f"{sec}초" + (" (기본)" if sec == 45 else ""), value=sec,
                            variable=self.duration).pack(side="left", padx=(0, 10))
        r = ttk.Frame(f3); r.pack(fill="x", pady=1)
        ttk.Label(r, text="화면", width=14).pack(side="left")
        ttk.Radiobutton(r, text="흐림 배경 (기본)", value="blur", variable=self.fit_mode).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(r, text="꽉 채우기", value="crop", variable=self.fit_mode).pack(side="left")
        r = ttk.Frame(f3); r.pack(fill="x", pady=1)
        ttk.Label(r, text="배경 배정", width=14).pack(side="left")
        ttk.Radiobutton(r, text="랜덤 (기본)", value="random", variable=self.assign).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(r, text="순서대로", value="rotate", variable=self.assign).pack(side="left")
        r = ttk.Frame(f3); r.pack(fill="x", pady=1)
        ttk.Label(r, text="GPU 가속", width=14).pack(side="left")
        ttk.Checkbutton(r, text="NVENC 켬 (NVIDIA가 아니면 엔진이 CPU로 인코딩)", variable=self.use_nvenc).pack(side="left")
        r = ttk.Frame(f3); r.pack(fill="x", pady=1)
        ttk.Label(r, text="파일 이름", width=14).pack(side="left")
        ttk.Entry(r, textvariable=self.name_template, width=28).pack(side="left")
        ttk.Label(r, text="{stem}=곡 파일명 · {index}=01,02… · {bg}=배경 이름", style="Hint.TLabel").pack(side="left", padx=8)
        r = ttk.Frame(f3); r.pack(fill="x", pady=1)
        ttk.Label(r, text="저장 폴더", width=14).pack(side="left")
        ttk.Entry(r, textvariable=self.outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="찾기", command=self._pick_outdir).pack(side="left", padx=(4, 0))
        r = ttk.Frame(f3); r.pack(fill="x", pady=1)
        ttk.Label(r, text="숏츠 엔진 폴더", width=14).pack(side="left")
        ttk.Entry(r, textvariable=self.engine_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="찾기", command=self._pick_engine).pack(side="left", padx=(4, 0))
        self.btn_check = ttk.Button(r, text="연결 확인", command=self.check_engine)
        self.btn_check.pack(side="left", padx=(4, 0))
        ttk.Label(f3, textvariable=self.engine_text, font="PLS.Strong").pack(anchor="w", pady=(2, 0))

        f4 = ttk.LabelFrame(root, text="4) 제작", padding=7)
        f4.pack(fill="both", expand=True, pady=(6, 0))
        r = ttk.Frame(f4); r.pack(fill="x")
        self.btn_start = ttk.Button(r, text="▶ 숏츠 만들기", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(r, text="■ 중지", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        ttk.Label(r, textvariable=self.status).pack(side="left", padx=8)
        ttk.Label(f4, text="전체 진행률").pack(anchor="w", pady=(6, 0))
        self.bar = ttk.Progressbar(f4, maximum=100)
        self.bar.pack(fill="x")
        ttk.Label(f4, textvariable=self.now_text, font="PLS.Strong").pack(anchor="w", pady=(4, 0))
        self.item_bar = ttk.Progressbar(f4, maximum=100)
        self.item_bar.pack(fill="x")
        ttk.Label(f4, text="결과").pack(anchor="w", pady=(6, 0))
        self.result_tree = ttk.Treeview(f4, columns=("song", "state", "out"), show="headings", height=6,
                                        selectmode="browse")
        for c, t, w in (("song", "곡", 260), ("state", "상태", 70), ("out", "출력 파일 / 실패 이유", 560)):
            self.result_tree.heading(c, text=t)
            self.result_tree.column(c, width=w, stretch=c != "state")
        self.result_tree.pack(fill="x")
        self.result_tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_buttons())
        self.result_tree.bind("<Double-1>", lambda e: self._play())
        r = ttk.Frame(f4); r.pack(fill="x", pady=(4, 0))
        self.btn_play = ttk.Button(r, text="재생", command=self._play)
        self.btn_play.pack(side="left")
        self.btn_folder = ttk.Button(r, text="폴더 열기", command=self._open_folder)
        self.btn_folder.pack(side="left", padx=4)
        self.btn_upload = ttk.Button(r, text="③ 예약 업로드로 보내기", command=self.send_selected_to_upload)
        self.btn_upload.pack(side="left")
        ttk.Label(f4, text="로그").pack(anchor="w", pady=(6, 0))
        self.log = tk.Listbox(f4, height=6)
        self.log.pack(fill="both", expand=True)

    def _list_box(self, root, title, text_var, add_files, add_folder, which, hint):
        f = ttk.LabelFrame(root, text=title, padding=7)
        f.pack(fill="x", pady=(6, 0))
        br = ttk.Frame(f); br.pack(fill="x", pady=(0, 4))
        btns = [ttk.Button(br, text="＋ 파일 추가", command=add_files),
                ttk.Button(br, text="＋ 폴더 추가", command=add_folder)]
        edit = [ttk.Button(br, text="선택 삭제", command=lambda: self._remove(which)),
                ttk.Button(br, text="▲ 위로", command=lambda: self._move(which, -1)),
                ttk.Button(br, text="▼ 아래로", command=lambda: self._move(which, 1)),
                ttk.Button(br, text="모두 지우기", command=lambda: self._clear(which))]
        for k, b in enumerate(btns + edit):
            b.pack(side="left", padx=(0 if k == 0 else (8 if k == 2 else 2), 0))
        setattr(self, f"_edit_btns_{which}", edit)
        ttk.Label(br, textvariable=text_var, style="Hint.TLabel").pack(side="right")
        ttk.Label(f, text=hint, style="Hint.TLabel").pack(anchor="w")
        tree = ttk.Treeview(f, columns=("no", "name", "folder"), show="headings", height=5, selectmode="browse")
        for c, t, w in (("no", "순서", 50), ("name", "파일명", 380), ("folder", "폴더", 460)):
            tree.heading(c, text=t)
            tree.column(c, width=w, stretch=c != "no", anchor="center" if c == "no" else "w")
        tree.pack(fill="x")
        return tree

    # ---------------- 목록 ----------------
    def _list(self, which) -> list[Entry]:
        return self.songs if which == "songs" else self.bgs

    def _tree(self, which):
        return self.song_tree if which == "songs" else self.bg_tree

    def _add(self, which, paths, exts, titles=None) -> int:
        items = self._list(which)
        have = {_norm(e.path) for e in items}
        added = 0
        for k, p in enumerate(paths):
            p = Path(p)
            if p.suffix.lower() not in exts or not p.is_file() or _norm(p) in have:
                continue
            items.append(Entry(p, (titles or {}).get(k, "")))
            have.add(_norm(p))
            added += 1
        self._refresh_lists()
        return added

    def add_audio(self, path, title: str = "") -> bool:
        """⑦ 등 다른 기능에서 곡 1개를 목록 끝에 추가. 이미 있으면 그대로 (중복 무시).
        제작 중이면 목록에 추가만 하고 자동으로 시작하지 않는다 (지금 제작에는 들어가지 않음).
        반환: 곡이 목록에 있으면 True, 파일이 없거나 지원하지 않는 형식이면 False."""
        p = Path(path)
        if p.suffix.lower() not in AUDIO_EXTS or not p.is_file():
            return False
        self._add("songs", [p], AUDIO_EXTS, {0: title})
        if self.running:
            self.status.set(f"곡을 목록에 추가했습니다 (다음 제작부터): {p.name}")
        return any(_norm(e.path) == _norm(p) for e in self.songs)

    def add_background(self, path) -> bool:
        p = Path(path)
        self._add("bgs", [p], VIDEO_EXTS)
        return any(_norm(e.path) == _norm(p) for e in self.bgs)

    @staticmethod
    def _scan(folder, exts) -> list[Path]:
        try:
            files = [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in exts]
        except OSError:
            return []
        return sorted(files, key=lambda p: natural_key(p.name))

    def _add_song_files(self):
        fs = filedialog.askopenfilenames(parent=self, title="곡 추가",
                                         filetypes=[("음원", " ".join("*" + e for e in AUDIO_EXTS)), ("모든 파일", "*.*")])
        if fs:
            self._add("songs", sorted(fs, key=lambda p: natural_key(Path(p).name)), AUDIO_EXTS)

    def _add_song_folder(self):
        d = filedialog.askdirectory(parent=self, title="곡 폴더 추가")
        if d:
            self._report_folder(self._add("songs", self._scan(d, AUDIO_EXTS), AUDIO_EXTS), d)

    def _add_bg_files(self):
        fs = filedialog.askopenfilenames(parent=self, title="배경 영상 추가",
                                         filetypes=[("영상", " ".join("*" + e for e in VIDEO_EXTS)), ("모든 파일", "*.*")])
        if fs:
            self._add("bgs", sorted(fs, key=lambda p: natural_key(Path(p).name)), VIDEO_EXTS)

    def _add_bg_folder(self):
        d = filedialog.askdirectory(parent=self, title="배경 영상 폴더 추가")
        if d:
            self._report_folder(self._add("bgs", self._scan(d, VIDEO_EXTS), VIDEO_EXTS), d)

    def _report_folder(self, n, d):
        self.status.set(f"{Path(d).name} 폴더에서 {n}개 추가" if n else f"{Path(d).name} 폴더에 새로 추가할 파일이 없습니다.")

    def _sel(self, which) -> int | None:
        sel = self._tree(which).selection()
        return self._tree(which).index(sel[0]) if sel else None

    def _remove(self, which):
        i = self._sel(which)
        if i is not None:
            del self._list(which)[i]
            self._refresh_lists(which, min(i, len(self._list(which)) - 1))

    def _move(self, which, d):
        i = self._sel(which)
        items = self._list(which)
        if i is None or not 0 <= i + d < len(items):
            return
        items[i], items[i + d] = items[i + d], items[i]
        self._refresh_lists(which, i + d)

    def _clear(self, which):
        self._list(which).clear()
        self._refresh_lists()

    def _refresh_lists(self, which=None, select=None):
        for w in ("songs", "bgs"):
            tree = self._tree(w)
            tree.delete(*tree.get_children())
            for k, e in enumerate(self._list(w), 1):
                name = e.path.name if e.title == e.path.stem else f"{e.title}  ({e.path.name})"
                tree.insert("", "end", values=(k, name, str(e.path.parent)))
            if w == which and select is not None and select >= 0:
                iid = tree.get_children()[select]
                tree.selection_set(iid)
                tree.see(iid)
        self.song_text.set(f"{len(self.songs)}곡")
        self.bg_text.set(f"{len(self.bgs)}개")
        self._refresh_buttons()

    # ---------------- 설정 ----------------
    def _pick_outdir(self):
        d = filedialog.askdirectory(parent=self, title="숏츠 저장 폴더")
        if d:
            self.outdir.set(d)
            self._save_settings()

    def _pick_engine(self):
        d = filedialog.askdirectory(parent=self, title="숏츠 엔진 폴더 (ShortsMaker Studio, 예: D:\\03_shorts)")
        if d:
            self.engine_dir.set(d)
            self._save_settings()

    def options(self) -> dict:
        try:
            duration = int(self.duration.get())
        except (tk.TclError, ValueError):
            duration = DEFAULTS["duration"]
        return {"duration": duration if duration in DURATIONS else DEFAULTS["duration"],
                "fit_mode": self.fit_mode.get() if self.fit_mode.get() in ("blur", "crop") else "blur",
                "assign": self.assign.get() if self.assign.get() in ("random", "rotate") else "random",
                "use_nvenc": bool(self.use_nvenc.get()),
                "name_template": self.name_template.get().strip() or DEFAULTS["name_template"],
                "output_dir": self.outdir.get().strip(),
                "engine_dir": self.engine_dir.get().strip()}

    def _save_settings(self):
        update_settings(**{SETTINGS_KEY: self.options()})  # shorts_maker 키만 바꾼다

    def check_engine(self) -> None:
        """[연결 확인]: 엔진 실행(최대 20초)은 작업 스레드에서."""
        d = self.engine_dir.get().strip()
        self.engine_text.set("숏츠 엔진 확인 중…")
        self.btn_check.configure(state="disabled")
        self._save_settings()

        def work():
            try:
                self.events.put(("engine", True, SB.engine_version(d)))
            except Exception as e:
                self.events.put(("engine", False, str(e)))
        self._spawn(work)

    def _spawn(self, fn, *args):
        t = threading.Thread(target=fn, args=args, daemon=True)
        self._threads.append(t)
        t.start()

    # ---------------- 제작 ----------------
    def start(self) -> bool:
        if self.running:
            return False
        if not self.songs or not self.bgs:
            return self._warn("곡과 배경 영상을 각각 1개 이상 추가하세요.")
        opts = self.options()
        if not opts["output_dir"]:
            return self._warn("저장 폴더를 지정하세요.")
        try:
            SB.find_engine(opts["engine_dir"])  # 파일 확인만 (빠름). 버전 확인은 작업 스레드에서
        except SB.ShortsEngineError as e:
            self.engine_text.set(str(e).splitlines()[0])
            return self._warn(str(e))
        if not FFMPEG_GUARD.try_acquire(GUARD_OWNER):
            msg = _busy_text(FFMPEG_GUARD.owner) + "\n끝난 뒤 다시 시작하세요."
            self.status.set("FFmpeg 사용 중 — 시작하지 않았습니다.")
            if not self.quiet_errors:
                messagebox.showwarning("FFmpeg 사용 중", msg, parent=self)
            return False
        self._save_settings()
        self.run_items = list(self.songs)
        bgs = [e.path for e in self.bgs]
        self.results = []
        self.total = len(self.run_items)
        self.last_exit = None
        self.error_message = ""
        self.result_tree.delete(*self.result_tree.get_children())
        self.log.delete(0, "end")
        self.running = True
        self.bar["value"] = 0
        self.item_bar["value"] = 0
        self.now_text.set(f"0/{self.total} · 준비 중")
        self.status.set("숏츠 엔진 확인 중…")
        self._refresh_buttons()
        self.run = SB.ShortsRun(opts["engine_dir"])
        self._spawn(self._worker, self.run, opts, [e.path for e in self.run_items], bgs)
        return True

    def _worker(self, run, opts, audio, bgs):
        """작업 스레드: 버전 확인 → 작업 JSON 저장 → 엔진 실행 → 끝날 때까지 대기. 잠금은 어떤 경우에도 푼다."""
        try:
            with prevent_windows_sleep(True):
                version = SB.engine_version(opts["engine_dir"])
                self.events.put(("engine", True, version))
                job_opts = {k: v for k, v in opts.items() if k != "engine_dir"}
                job_opts.update(start="auto", video_offset="random", skip_existing=True)
                job = SB.write_batch_job(job_opts, audio, bgs)
                self.events.put(("log", f"작업 파일: {job}"))
                run.start(job, lambda ev: self.events.put(("engine_event", ev)))
                run.wait()
        except Exception as e:
            self.events.put(("fatal", "중지했습니다." if run.cancel_requested else str(e)))
        finally:
            FFMPEG_GUARD.release(GUARD_OWNER)
            self.events.put(("released",))

    def stop(self):
        if not self.running:
            return
        self._stopping = True
        self.btn_stop.configure(state="disabled")
        self.status.set("중지하는 중… (최대 10초, 만들던 숏츠의 임시 파일은 엔진이 지웁니다)")
        if self.run is not None:
            self.run.cancel()  # 기다리지 않는다: 10초 뒤 강제 종료는 별도 스레드

    def _warn(self, msg) -> bool:
        self.status.set(msg.splitlines()[0])
        if not self.quiet_errors:
            messagebox.showwarning(TITLE, msg, parent=self)
        return False

    def _log(self, text):
        self.log.insert("end", text)
        self.log.see("end")

    def _title_of(self, index) -> str:
        try:
            return self.run_items[int(index) - 1].title
        except (ValueError, TypeError, IndexError):
            return f"{index}번 곡"

    def _engine_event(self, ev: dict):
        kind = ev.get("event")
        if kind == "batch_start":
            self.total = int(ev.get("total") or self.total)
            self._log(f"시작: {self.total}곡 · 배정 {ev.get('assign')} · seed {ev.get('seed')} · CSV {ev.get('csv')}")
            self.status.set("제작 중…")
        elif kind == "item_start":
            i = ev.get("index")
            self.item_bar["value"] = 0
            self.now_text.set(f"{i}/{self.total} · {self._title_of(i)}")
            self._log(f"{i}/{self.total} {self._title_of(i)} · 배경 {Path(str(ev.get('background', ''))).name}"
                      f" · 시작 위치 {ev.get('video_offset')}초")
        elif kind == "progress":
            self.item_bar["value"] = float(ev.get("value") or 0) * 100
            self.bar["value"] = float(ev.get("overall") or 0) * 100
        elif kind == "item_result":
            self._add_result(ev)
        elif kind == "batch_result":
            self._log(f"끝: 완료 {ev.get('done', 0)} · 실패 {ev.get('failed', 0)} · 건너뜀 {ev.get('skipped', 0)}"
                      + (f" · 경고 {', '.join(ev['warnings'])}" if ev.get("warnings") else ""))
        elif kind == "error":
            self._log(f"오류 ({ev.get('code')}): {ev.get('message')}")
            self.error_message = str(ev.get("message", ""))
        elif kind == "log":
            self._log(str(ev.get("line", "")))

    def _add_result(self, ev):
        i = ev.get("index")
        if ev.get("skipped"):
            state = "skipped"
        else:
            state = "ok" if ev.get("ok") else "failed"
        out = str(ev.get("output") or "")
        r = {"index": i, "song": self._title_of(i), "status": state, "output": out,
             "message": str(ev.get("message") or ""), "code": ev.get("code")}
        self.results.append(r)
        shown = out if state != "failed" else r["message"]  # 실패: 엔진 message 그대로
        self.result_tree.insert("", "end", values=(f"{i}. {r['song']}", STATUS_TEXT[state], shown))
        self._log(f"{i}/{self.total} {r['song']} → {STATUS_TEXT[state]}" + (f": {r['message']}" if state == "failed" else ""))
        if self.total:
            self.bar["value"] = max(float(self.bar["value"]), len(self.results) / self.total * 100)
        self._refresh_buttons()

    def _exit(self, ev: dict):
        self.last_exit = ev
        code = ev.get("code")
        br = ev.get("batch_result") or {}
        counts = f"완료 {br.get('done', 0)} · 실패 {br.get('failed', 0)} · 건너뜀 {br.get('skipped', 0)}"
        if code == SB.EXIT_OK:
            self.status.set(f"모두 끝났습니다 · {counts}")
            self.bar["value"] = 100
        elif code == SB.EXIT_PARTIAL:
            self.status.set(f"일부 실패 · {counts} · 실패 이유는 결과 목록을 보세요.")
        elif code == SB.EXIT_CANCELLED:
            self.status.set("중지했습니다 · 완성된 숏츠는 그대로 남아 있습니다."
                            + (" (응답이 없어 강제 종료)" if ev.get("killed") else ""))
        else:
            msg = self.error_message or "\n".join(ev.get("stderr_tail") or [])[-500:]
            self.status.set(f"제작 실패 (종료 코드 {code})" + (f" · {msg.splitlines()[0]}" if msg else ""))
        self._log(f"숏츠 엔진 종료 (코드 {code})")
        if code != SB.EXIT_CANCELLED:
            self.now_text.set(f"{len(self.results)}/{self.total} 처리됨")

    def _pump(self):
        try:
            while True:
                kind, *rest = self.events.get_nowait()
                if kind == "engine":
                    ok, text = rest
                    self.engine_text.set(f"엔진 v{text} 연결됨" if ok else text.splitlines()[0])
                    try:
                        self.btn_check.configure(state="normal")
                    except tk.TclError:
                        pass
                elif kind == "engine_event":
                    ev = rest[0]
                    if ev.get("event") == "exit":
                        self._exit(ev)
                    else:
                        self._engine_event(ev)
                elif kind == "exit":
                    self._exit(rest[0])
                elif kind == "log":
                    self._log(rest[0])
                elif kind == "fatal":
                    self.status.set("시작하지 못했습니다 · " + rest[0].splitlines()[0])
                    self._log(rest[0])
                    if not self.quiet_errors:
                        messagebox.showerror(TITLE, rest[0], parent=self)
                elif kind == "released":
                    self.running = False
                    self._stopping = False
                    self._refresh_buttons()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.after(100, self._pump)
        except tk.TclError:
            pass

    def _refresh_buttons(self):
        try:
            self.btn_start.configure(state="disabled" if self.running else "normal")
            self.btn_stop.configure(state="normal" if self.running and not self._stopping else "disabled")
            for b in getattr(self, "_edit_btns_songs", []) + getattr(self, "_edit_btns_bgs", []):
                b.configure(state="disabled" if self.running else "normal")
            r = self.selected_result()
            have = bool(r and r["output"] and Path(r["output"]).is_file())
            for b in (self.btn_play, self.btn_folder):
                b.configure(state="normal" if have else "disabled")
            self.btn_upload.configure(state="normal" if have and self.send_to_upload else "disabled")
        except (tk.TclError, AttributeError):
            pass

    # ---------------- 결과 ----------------
    def selected_result(self) -> dict | None:
        sel = self.result_tree.selection()
        if not sel:
            return None
        i = self.result_tree.index(sel[0])
        return self.results[i] if i < len(self.results) else None

    def _selected_file(self) -> Path | None:
        r = self.selected_result()
        p = Path(r["output"]) if r and r["output"] else None
        return p if p is not None and p.is_file() else None

    def _play(self):
        p = self._selected_file()
        if p is not None:
            _open_path(p)

    def _open_folder(self):
        p = self._selected_file()
        if p is not None and os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(p)])

    def send_selected_to_upload(self):
        """③ 예약 업로드 창을 열고 영상 경로를 채운다 (업로드는 시작하지 않음)."""
        p = self._selected_file()
        if p is None or not self.send_to_upload:
            return None
        return self.send_to_upload(p)

    # ---------------- 닫기 ----------------
    def _on_close(self):
        if self.running and not messagebox.askyesno("숏츠 제작 중", "숏츠 제작을 중지하고 닫을까요?", parent=self):
            return
        self.destroy()

    def destroy(self):
        if self.running:
            self.stop()  # 엔진에 cancel → 10초 안에 안 끝나면 프로세스 트리 종료 (별도 스레드, 창은 바로 닫힘)
        try:
            self._save_settings()
        except Exception:
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
