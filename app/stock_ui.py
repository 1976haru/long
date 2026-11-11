"""⑤ 배경 영상 검색·다운로드 — 독립 창 (Tkinter).

흐름: 주제 선택 → 검색 → 썸네일 확인 → 실제 동영상 재생 → 길이·해상도·권리 확인 → 선택 다운로드 → ④로 보내기.
검색·썸네일·미리보기 확인·다운로드는 모두 작업 스레드에서 하고, 화면 갱신은 이벤트 큐로 Tk 스레드에서만 한다.
권리 표시는 참고용이며 법적 사용 허가를 보증하지 않는다.
"""
from __future__ import annotations

import base64
import os
import queue
import subprocess
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import stock_rights as R
from .core import creationflags_no_window
from . import settings as _settings
from .settings import load_settings, update_settings
from .stock_download import DownloadError, Library, default_library_dir, download
from .stock_keys import KEY_PROVIDERS, SIGNUP, KeyStore, read_legacy_key
from .stock_net import PEXELS, PROVIDERS, NetError, ProviderClient, fetch_bytes, url_allowed
from .stock_player import ERROR, PLAYING, PreviewPlayer, find_ffplay, fmt, probe_media
from .stock_sources import (
    ADAPTERS, MODE_LABELS, MODE_LONG, MODE_SHORT, NEEDS_KEY, PROVIDER_MODES, PROVIDER_NOTES, RES_4K_PREF, RES_FHD,
    RES_LABELS, TOPIC_GROUPS, TOPICS, Candidate, build_query, match_label, res_text, run_search, sort_results,
)
from .tooling import release_tk_variables
from .ui_scroll import ScrollFrame

TITLE = "⑤ 배경 영상 검색·다운로드"
SETTINGS_KEY = "stock_search"
THUMB_W, THUMB_H = 112, 63
DEFAULT_TOPICS = ("lake", "snow")


def thumb_png_b64(ffmpeg: Path, data: bytes) -> str:
    """제공처 썸네일(JPEG 등) → Tk가 읽는 PNG (FFmpeg로 변환, Pillow 없이)."""
    p = subprocess.run([str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-i", "pipe:0", "-frames:v", "1", "-vf",
                        f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease,"
                        f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2:color=black",
                        "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                       input=data, capture_output=True, timeout=20, creationflags=creationflags_no_window())
    if p.returncode != 0 or not p.stdout.startswith(b"\x89PNG"):
        raise ValueError("썸네일 이미지 변환 실패")
    return base64.b64encode(p.stdout).decode("ascii")


class StockSearchWindow(tk.Toplevel):
    def __init__(self, master, *, tools, send_to_bg=None, keystore: KeyStore | None = None,
                 adapters: dict | None = None, player_env: dict | None = None, check_dns: bool = True,
                 peek_tools=None):
        super().__init__(master)
        self.title(TITLE)
        sh = self.winfo_screenheight()
        self.geometry(f"1220x{max(560, min(960, sh - 90))}")
        self.minsize(940, 520)
        self.tools = tools  # 사용자가 미리보기·다운로드를 누를 때만 (main thread, FFmpeg 선택 창 가능)
        self.peek_tools = peek_tools or tools  # 창 열기·썸네일용: 선택 창을 띄우지 않는 조회 (주 화면은 반드시 전달)
        self.send_to_bg_cb = send_to_bg
        self.keys = keystore or KeyStore()
        self.check_dns = check_dns
        self.events: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self.cancel_search = threading.Event()
        self.cancel_dl = threading.Event()
        self.stop_thumbs = threading.Event()
        self.searching = False
        self.downloading = False
        saved = load_settings().get(SETTINGS_KEY) or {}
        cache = _settings.settings_dir() / "stock_cache"
        self.adapters = adapters or {
            p: ADAPTERS[p](ProviderClient(p, cache, check_dns=check_dns), self.keys.lookup(p)) for p in PROVIDERS}
        self.mode = tk.StringVar(value=saved.get("mode") if saved.get("mode") in MODE_LABELS else MODE_SHORT)
        # 모드별 독립 상태: 해상도 필터·결과·선택·제공처 상태
        self.mode_res = {MODE_SHORT: saved.get("res_short", RES_FHD), MODE_LONG: saved.get("res_long", RES_FHD)}
        self.results: dict[str, list[Candidate]] = {MODE_SHORT: [], MODE_LONG: []}
        self.selected: dict[str, set[str]] = {MODE_SHORT: set(), MODE_LONG: set()}
        self.prov_status: dict[str, dict[str, str]] = {MODE_SHORT: {}, MODE_LONG: {}}
        self.mode_status: dict[str, str] = {MODE_SHORT: "", MODE_LONG: ""}  # 모드별 마지막 검색 결과 문구
        self.res = tk.StringVar(value=RES_LABELS.get(self.mode_res[self.mode.get()], RES_LABELS[RES_FHD]))
        topics = saved.get("topics") if isinstance(saved.get("topics"), list) else list(DEFAULT_TOPICS)
        self.topic_vars = {t.key: tk.BooleanVar(value=t.key in topics) for t in TOPICS}
        self.free_text = tk.StringVar(value=str(saved.get("text", "")))
        provs = saved.get("providers") if isinstance(saved.get("providers"), list) else list(PROVIDERS)
        self.prov_vars = {p: tk.BooleanVar(value=p in provs) for p in PROVIDERS}
        self.libdir = tk.StringVar(value=str(default_library_dir()))
        self.query_text = tk.StringVar()
        self.status = tk.StringVar(value="주제를 고르고 [🔍 검색]을 누르세요.")
        self.detail = tk.StringVar(value="결과를 선택하면 길이·해상도·권리 정보가 여기에 표시됩니다.")
        self.dl_text = tk.StringVar()
        self.player_text = tk.StringVar(value="미리보기: 결과를 선택하고 [▶ 미리보기 재생]")
        self.time_text = tk.StringVar(value="00:00 / 00:00")
        self.loop = tk.BooleanVar(value=False)
        self.thumbs: dict[str, tk.PhotoImage] = {}
        self.thumb_queue: queue.Queue = queue.Queue()
        self.last_downloaded: list[str] = []
        self.dl_log: list[tuple[str, str, str]] = []
        self._seek_drag = False
        self._ff: tuple = (None, None)
        try:
            self._ff = tuple(self.peek_tools() or (None, None))
        except Exception:
            self._ff = (None, None)
        ff = self._ff[0]
        self.player = PreviewPlayer(find_ffplay(Path(ff)) if ff else find_ffplay(None), ffmpeg=Path(ff) if ff else None,
                                    env=player_env)
        self._ui()
        self._refresh_query()
        self._show_results()
        self._refresh_library()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(100, self._pump)
        self.after(300, self._tick_player)
        t = threading.Thread(target=self._thumb_worker, daemon=True)
        self._threads.append(t)
        t.start()

    # ---------------- 화면 ----------------
    def _ui(self):
        st = ttk.Style(self)
        st.configure("Stock.Treeview", rowheight=THUMB_H + 8)
        bottom = ttk.Frame(self, padding=(10, 4, 10, 8))
        bottom.pack(side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        root = ttk.Frame(self.scroll.body, padding=10)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text=TITLE, font="PLS.Title").pack(anchor="w")
        ttk.Label(root, text="주제 선택 → 검색 → 썸네일 확인 → 실제 동영상 재생 → 길이·해상도·권리 확인 → 선택 다운로드 → "
                             "④ 배경 만들기로 보내기. 권리 표시는 참고용이며 법적 사용 허가를 보증하지 않습니다.",
                  style="Hint.TLabel", wraplength=1150, justify="left").pack(anchor="w", pady=(0, 6))

        f1 = ttk.LabelFrame(root, text="① 검색 주제 (여러 개 고르면 모두 맞는 영상을 '복합 일치'로 먼저 보여줍니다)", padding=6)
        f1.pack(fill="x")
        for g in TOPIC_GROUPS:
            row = ttk.Frame(f1); row.pack(fill="x")
            ttk.Label(row, text=g, width=6, style="Hint.TLabel").pack(side="left")
            for t in TOPICS:
                if t.group == g:
                    ttk.Checkbutton(row, text=t.label, variable=self.topic_vars[t.key],
                                    command=self._refresh_query).pack(side="left", padx=2)
        er = ttk.Frame(f1); er.pack(fill="x", pady=(4, 0))
        ttk.Label(er, text="직접 입력").pack(side="left")
        e = ttk.Entry(er, textvariable=self.free_text, width=40)
        e.pack(side="left", padx=4)
        e.bind("<KeyRelease>", lambda ev: self._refresh_query())
        e.bind("<Return>", lambda ev: self.search())
        ttk.Label(er, text="예: 눈 내리는 호수 · 바다 보이는 카페 · 비 오는 일본 카페 · 겨울 도쿄 야경",
                  style="Hint.TLabel").pack(side="left", padx=6)
        ttk.Label(f1, textvariable=self.query_text, wraplength=1150, justify="left").pack(anchor="w", pady=(2, 0))

        f2 = ttk.LabelFrame(root, text="② 길이 모드 · 해상도 · 제공처", padding=6)
        f2.pack(fill="x", pady=(6, 0))
        m = ttk.Frame(f2); m.pack(fill="x")
        ttk.Label(m, text="길이 모드").pack(side="left")
        for k, lab in MODE_LABELS.items():
            ttk.Radiobutton(m, text=lab, value=k, variable=self.mode, command=self._mode_changed).pack(side="left", padx=4)
        ttk.Label(m, text="   해상도").pack(side="left")
        cb = ttk.Combobox(m, textvariable=self.res, state="readonly", width=18, values=list(RES_LABELS.values()))
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda ev: self._res_changed())
        pr = ttk.Frame(f2); pr.pack(fill="x", pady=(4, 0))
        self.prov_labels: dict[str, ttk.Label] = {}
        for p in PROVIDERS:
            box = ttk.Frame(pr); box.pack(side="left", padx=(0, 14))
            ttk.Checkbutton(box, text=p, variable=self.prov_vars[p]).pack(anchor="w")
            lb = ttk.Label(box, text="", style="Hint.TLabel", wraplength=270, justify="left")
            lb.pack(anchor="w")
            self.prov_labels[p] = lb
        br = ttk.Frame(f2); br.pack(fill="x", pady=(4, 0))
        self.btn_search = ttk.Button(br, text="🔍 검색", command=self.search, style="Primary.TButton")
        self.btn_search.pack(side="left")
        self.btn_cancel_search = ttk.Button(br, text="검색 취소", command=self.cancel_search.set, state="disabled")
        self.btn_cancel_search.pack(side="left", padx=4)
        ttk.Button(br, text="API 키 설정", command=self.key_dialog).pack(side="left", padx=(16, 4))
        self.pexels_credit = tk.Label(br, text="Videos provided by Pexels", fg="#1d4fa8", cursor="hand2")
        self.pexels_credit.bind("<Button-1>", lambda e: webbrowser.open("https://www.pexels.com"))
        ttk.Label(f2, textvariable=self.status, font="PLS.Strong", wraplength=1150, justify="left").pack(anchor="w", pady=(4, 0))

        f3 = ttk.LabelFrame(root, text="③ 검색 결과 — 썸네일은 정지 이미지입니다. 실제 영상은 [▶ 미리보기 재생]으로 확인하세요", padding=6)
        f3.pack(fill="both", expand=True, pady=(6, 0))
        tf = ttk.Frame(f3); tf.pack(fill="both", expand=True)
        cols = ("sel", "title", "prov", "dur", "res", "creator", "rights", "match")
        self.tree = ttk.Treeview(tf, columns=cols, show="tree headings", height=5, style="Stock.Treeview", selectmode="browse")
        self.tree.heading("#0", text="썸네일")
        self.tree.column("#0", width=THUMB_W + 24, stretch=False)
        for c, h, w in zip(cols, ("받기", "제목 (메타데이터)", "제공처", "원본 길이", "해상도 (메타)", "제작자", "권리 상태", "주제 적합도"),
                           (44, 260, 110, 80, 150, 120, 110, 120)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("title", "creator", "match", "rights") else "center")
        vs = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_detail())
        self.tree.bind("<Button-1>", self._tree_click)
        ttk.Label(f3, textvariable=self.detail, wraplength=1150, justify="left").pack(anchor="w", pady=(4, 0))
        ar = ttk.Frame(f3); ar.pack(fill="x", pady=(4, 0))
        self.btn_toggle = ttk.Button(ar, text="받기 선택/해제", command=self.toggle_selected)
        self.btn_toggle.pack(side="left")
        self.btn_preview = ttk.Button(ar, text="▶ 미리보기 재생 (실제 영상)", command=self.preview_selected)
        self.btn_preview.pack(side="left", padx=4)
        ttk.Button(ar, text="원본 페이지 열기", command=self.open_page).pack(side="left", padx=4)
        ttk.Button(ar, text="라이선스 열기", command=self.open_license).pack(side="left", padx=4)

        f4 = ttk.LabelFrame(root, text="④ 동영상 미리보기 (FFmpeg ffplay 창 · 소리 없음)", padding=6)
        f4.pack(fill="x", pady=(6, 0))
        pc = ttk.Frame(f4); pc.pack(fill="x")
        self.btn_play = ttk.Button(pc, text="▶ 재생", command=self.player_play)
        self.btn_play.pack(side="left")
        ttk.Button(pc, text="❚❚ 일시정지", command=self.player_pause).pack(side="left", padx=4)
        ttk.Button(pc, text="⟲ 처음부터", command=self.player_restart).pack(side="left", padx=4)
        ttk.Checkbutton(pc, text="반복 재생", variable=self.loop, command=self._loop_changed).pack(side="left", padx=8)
        self.seek = ttk.Scale(pc, from_=0, to=1, orient="horizontal", length=420)
        self.seek.pack(side="left", padx=8)
        self.seek.bind("<ButtonPress-1>", lambda e: setattr(self, "_seek_drag", True))
        self.seek.bind("<ButtonRelease-1>", lambda e: self._seek_release())
        ttk.Label(pc, textvariable=self.time_text, width=16).pack(side="left")
        ttk.Label(f4, textvariable=self.player_text, wraplength=1150, justify="left").pack(anchor="w", pady=(4, 0))

        f5 = ttk.LabelFrame(root, text="⑤ 받은 영상 (라이브러리) — 실제 파일을 ffprobe로 검사한 값", padding=6)
        f5.pack(fill="both", expand=True, pady=(6, 0))
        lr = ttk.Frame(f5); lr.pack(fill="x")
        ttk.Label(lr, text="저장 폴더").pack(side="left")
        ttk.Entry(lr, textvariable=self.libdir).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(lr, text="변경", command=self._pick_libdir).pack(side="left")
        ttk.Button(lr, text="폴더 열기", command=lambda: _open_path(Path(self.libdir.get()))).pack(side="left", padx=4)
        lf = ttk.Frame(f5); lf.pack(fill="both", expand=True, pady=(4, 0))
        lcols = ("file", "prov", "probe", "rights", "sha", "warn")
        self.lib = ttk.Treeview(lf, columns=lcols, show="headings", height=5, selectmode="extended")
        for c, h, w in zip(lcols, ("파일", "제공처", "실측 (해상도·FPS·코덱·길이)", "권리 상태", "SHA256", "확인 사항"),
                           (260, 110, 240, 110, 100, 330)):
            self.lib.heading(c, text=h)
            self.lib.column(c, width=w, anchor="w")
        lvs = ttk.Scrollbar(lf, orient="vertical", command=self.lib.yview)
        self.lib.configure(yscrollcommand=lvs.set)
        self.lib.pack(side="left", fill="both", expand=True)
        lvs.pack(side="right", fill="y")

        dr = ttk.Frame(bottom); dr.pack(fill="x")
        self.btn_dl = ttk.Button(dr, text="선택 다운로드", command=self.download_selected, style="Primary.TButton")
        self.btn_dl.pack(side="left", fill="x", expand=True)
        self.btn_dl_cancel = ttk.Button(dr, text="■ 다운로드 취소", command=self.cancel_dl.set, state="disabled",
                                        style="Danger.TButton")
        self.btn_dl_cancel.pack(side="left", padx=(6, 0))
        self.btn_send = ttk.Button(dr, text="④ 배경 영상 만들기로 보내기", command=self.send_to_bg)
        self.btn_send.pack(side="left", padx=(6, 0))
        self.bar = ttk.Progressbar(bottom, mode="determinate", maximum=1000)
        self.bar.pack(fill="x", pady=(6, 0))
        ttk.Label(bottom, textvariable=self.dl_text, wraplength=1150, justify="left").pack(anchor="w")
        self._refresh_buttons()

    # ---------------- 상태 ----------------
    def topics(self) -> list[str]:
        return [k for k, v in self.topic_vars.items() if v.get()]

    def res_key(self) -> str:
        return next((k for k, v in RES_LABELS.items() if v == self.res.get()), RES_FHD)

    def providers(self) -> list[str]:
        return [p for p, v in self.prov_vars.items() if v.get()]

    def _refresh_query(self):
        plan = build_query(self.topics(), self.free_text.get())
        txt = f"보낼 검색어(영어): {plan.text or '(없음)'} · 주제 {len(plan.groups)}개"
        if plan.unknown_words:
            txt += f" · 사전에 없는 말 제외: {', '.join(plan.unknown_words)} (영어로 쓰면 더 정확)"
        self.query_text.set(txt)
        self._refresh_providers()

    def _refresh_providers(self):
        mode = self.mode.get()
        for p, lb in self.prov_labels.items():
            st = self.prov_status[mode].get(p)
            if mode not in PROVIDER_MODES[p]:
                txt = "이 모드 미지원 — " + PROVIDER_NOTES[p].split(" · ")[0]
            elif st:
                txt = st
            elif NEEDS_KEY[p]:
                txt = f"API 키: {self.keys.source(p)}"
            else:
                txt = "키 필요 없음"
            try:
                lb.configure(text=txt)
            except tk.TclError:
                pass

    def _mode_changed(self):
        """모드를 바꾸면 그 모드의 결과·해상도 필터만 보여준다 (다른 모드 결과로 대체하거나 자동 검색하지 않음)."""
        self.res.set(RES_LABELS[self.mode_res.get(self.mode.get(), RES_FHD)])
        self.status.set(self.mode_status.get(self.mode.get()) or "이 모드는 아직 검색하지 않았습니다. [🔍 검색]을 누르세요.")
        self._show_results()
        self._refresh_providers()
        self._save()

    def _res_changed(self):
        self.mode_res[self.mode.get()] = self.res_key()
        self.status.set("해상도 조건을 바꿨습니다. 새 조건으로 [🔍 검색]을 다시 누르세요.")
        self._save()

    def _loop_changed(self):
        self.player.loop = bool(self.loop.get())

    def _save(self):
        try:
            update_settings(**{SETTINGS_KEY: {"mode": self.mode.get(), "res_short": self.mode_res[MODE_SHORT],
                                              "res_long": self.mode_res[MODE_LONG], "topics": self.topics(),
                                              "text": self.free_text.get().strip()[:100], "providers": self.providers(),
                                              "library": self.libdir.get().strip()}})
        except Exception:
            pass

    def _refresh_buttons(self):
        try:
            busy = self.searching
            self.btn_search.configure(state="disabled" if busy else "normal")
            self.btn_cancel_search.configure(state="normal" if busy else "disabled")
            n = len(self.selected[self.mode.get()])
            self.btn_dl.configure(text=f"선택 다운로드 ({n}개)", state="disabled" if self.downloading or not n else "normal")
            self.btn_dl_cancel.configure(state="normal" if self.downloading else "disabled")
            self.btn_send.configure(state="normal" if self.send_to_bg_cb and self.lib.get_children() else "disabled")
        except tk.TclError:
            pass

    # ---------------- 검색 ----------------
    def search(self) -> bool:
        if self.searching:
            return False
        plan = build_query(self.topics(), self.free_text.get())
        if not plan.text:
            messagebox.showinfo("검색", "주제를 고르거나 검색어를 입력하세요.", parent=self)
            return False
        provs = self.providers()
        if not provs:
            messagebox.showinfo("검색", "제공처를 1개 이상 고르세요.", parent=self)
            return False
        mode, res = self.mode.get(), self.res_key()
        self.mode_res[mode] = res
        self._save()
        self.searching = True
        self.cancel_search.clear()
        self.results[mode] = []
        self.selected[mode] = set()
        self.prov_status[mode] = {}
        self._show_results()
        self.status.set(f"검색 중… '{plan.text}' · {MODE_LABELS[mode]} · {RES_LABELS[res]}")
        self._refresh_buttons()
        t = threading.Thread(target=self._search_worker, args=(plan, mode, res, provs), daemon=True)
        self._threads.append(t)
        t.start()
        return True

    def _search_worker(self, plan, mode, res, provs):
        try:
            results = run_search(self.adapters, provs, plan, mode, res, self.cancel_search,
                                 on_result=lambda r: self.events.put(("prov", mode, r)))
            self.events.put(("search_done", mode, results))
        except Exception as e:  # pragma: no cover
            self.events.put(("search_done", mode, []))
            self.events.put(("status", f"검색 오류: {type(e).__name__}"))

    def _on_prov(self, mode, r):
        ex = ", ".join(f"{k} {v}" for k, v in r.excluded.items())
        txt = f"[{r.status}] {r.message}" + (f" · 제외: {ex}" if ex else "")
        self.prov_status[mode][r.provider] = txt
        if r.candidates:
            self.results[mode] = sort_results(self.results[mode] + r.candidates, self.mode_res[mode])
            for c in r.candidates:
                if c.thumb_url and c.key not in self.thumbs:
                    self.thumb_queue.put(c)
        if mode == self.mode.get():
            self._show_results()
            self._refresh_providers()

    def _on_search_done(self, mode, results):
        self.searching = False
        n = len(self.results[mode])
        full = sum(1 for c in self.results[mode] if c.match[1] and c.match[0] == c.match[1])
        if n == 0:
            msg = "확인된 장편 원본 없음 — 짧은 영상으로 자동 대체하지 않습니다." if mode == MODE_LONG else "조건에 맞는 클립이 없습니다."
        else:
            msg = f"결과 {n}개 (주제 모두 일치 {full}개) — 썸네일 확인 후 [▶ 미리보기 재생]으로 실제 영상을 보세요."
        if self.cancel_search.is_set():
            msg = "검색을 취소했습니다. " + msg
        self.mode_status[mode] = msg
        if mode == self.mode.get():
            self.status.set(msg)
        self._refresh_buttons()

    def _show_results(self):
        mode = self.mode.get()
        sel = self.tree.selection()
        for x in self.tree.get_children():
            self.tree.delete(x)
        res = self.mode_res[mode]
        for i, c in enumerate(self.results[mode]):
            rv = R.review(c.provider, c.license_name, c.license_url, c.creator, c.title)
            kw = {"image": self.thumbs[c.key]} if c.key in self.thumbs else {}
            dur = fmt(c.duration) if c.duration else "확인 불가"
            self.tree.insert("", "end", iid=str(i), text="" if kw else "(썸네일)", **kw, values=(
                "☑" if c.key in self.selected[mode] else "☐", c.title, c.provider, dur, res_text(c, res),
                c.creator or "-", rv.state, match_label(c)))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        show_pexels = any(c.provider == PEXELS for c in self.results[mode])
        try:
            if show_pexels:
                self.pexels_credit.pack(side="left", padx=12)
            else:
                self.pexels_credit.pack_forget()
        except tk.TclError:
            pass
        self._refresh_buttons()

    def current(self) -> Candidate | None:
        s = self.tree.selection()
        if not s:
            return None
        i = int(s[0])
        rs = self.results[self.mode.get()]
        return rs[i] if 0 <= i < len(rs) else None

    def _show_detail(self):
        c = self.current()
        if not c:
            return
        rv = R.review(c.provider, c.license_name, c.license_url, c.creator, c.title)
        best = c.best(self.mode_res[self.mode.get()])
        lines = [
            f"{c.title} · {c.provider} · 제작자 {c.creator or '미상'}",
            f"원본 길이: {fmt(c.duration) + ' (' + c.duration_source + ')' if c.duration else '확인 불가'} · "
            f"받을 파일: {res_text(c, self.mode_res[self.mode.get()])}"
            + (f" · 약 {best.size / 1024**2:,.0f} MB" if best and best.size else "")
            + " — 해상도는 제공처 메타데이터 기준, 실제 파일은 다운로드 후 ffprobe로 확인",
            f"주제 적합도: {match_label(c)} — 근거(메타데이터 단어): {c.match_evidence or '없음'}",
            f"권리 [{rv.state}] {rv.license_name or '라이선스 미확인'} {rv.license_url} · 상업적 사용: {rv.commercial_status}",
            "· " + " ".join(rv.reasons),
            f"· {rv.third_party}",
        ]
        if rv.attribution_required:
            lines.append(f"· 저작자 표시 문구(예): {rv.attribution_text}")
        lines += [f"· {n}" for n in c.notes]
        lines.append(f"· {R.DISCLAIMER}")
        self.detail.set("\n".join(lines))

    def _tree_click(self, ev):
        if self.tree.identify_region(ev.x, ev.y) == "cell" and self.tree.identify_column(ev.x) == "#1":
            row = self.tree.identify_row(ev.y)
            if row:
                self.tree.selection_set(row)
                self.toggle_selected()
                return "break"
        return None

    def toggle_selected(self):
        c = self.current()
        if not c:
            return False
        mode = self.mode.get()
        rv = R.review(c.provider, c.license_name, c.license_url, c.creator, c.title)
        if c.key in self.selected[mode]:
            self.selected[mode].discard(c.key)
        elif not rv.downloadable:
            messagebox.showwarning("다운로드 차단", f"권리 상태 [{rv.state}] — 기본 다운로드가 차단됩니다.\n" + " ".join(rv.reasons),
                                   parent=self)
            return False
        else:
            self.selected[mode].add(c.key)
        idx = self.tree.selection()[0]
        self.tree.set(idx, "sel", "☑" if c.key in self.selected[mode] else "☐")
        self._refresh_buttons()
        return True

    def open_page(self):
        c = self.current()
        if c and url_allowed(c.page_url, c.provider, "page"):
            webbrowser.open(c.page_url)

    def open_license(self):
        c = self.current()
        if c and c.license_url.startswith("https://"):
            webbrowser.open(c.license_url)

    def _ask_tools(self) -> tuple:
        """main thread 전용: 필요하면 기존 FFmpeg 선택 흐름을 사용하고 결과를 기억한다."""
        try:
            pair = tuple(self.tools() or (None, None))
        except Exception:
            pair = (None, None)
        if pair[0]:
            self._ff = pair
            if self.player.ffmpeg is None:
                self.player.ffmpeg = Path(pair[0])
            if not self.player.ffplay:
                self.player.ffplay = find_ffplay(Path(pair[0]))
        return pair

    # ---------------- 썸네일 ----------------
    def _thumb_worker(self):
        while not self.stop_thumbs.is_set():
            try:
                c = self.thumb_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                ff = self._ff[0]  # 백그라운드 thread에서는 FFmpeg 선택 창을 띄우지 않는다
                if not ff:
                    continue
                data = fetch_bytes(c.thumb_url, c.provider, "poster", check_dns=self.check_dns)
                self.events.put(("thumb", c.key, thumb_png_b64(Path(ff), data)))
            except NetError as e:
                self.events.put(("thumb_err", c.key, e.message))
            except Exception as e:
                self.events.put(("thumb_err", c.key, f"썸네일 오류: {type(e).__name__}"))

    # ---------------- 미리보기 ----------------
    def preview_selected(self) -> bool:
        c = self.current()
        if not c:
            messagebox.showinfo("미리보기", "결과를 먼저 선택하세요.", parent=self)
            return False
        r = c.preview()
        if not r or not url_allowed(r.url, c.provider, "download"):
            self.player_text.set("미리보기: 재생할 수 있는 파일 주소가 없습니다 (허용되지 않은 주소).")
            return False
        fp = self._ask_tools()[1]
        self.player.open(r.url, c.title, c.duration or 0.0)
        self.player.loop = bool(self.loop.get())
        self.player_text.set(f"미리보기: 영상 확인 중… ({r.text} · {c.provider})")
        t = threading.Thread(target=self._preview_worker, args=(fp, r.url, c), daemon=True)
        self._threads.append(t)
        t.start()
        return True

    def _preview_worker(self, fp, url, c):
        dur, codec, err = probe_media(Path(fp), url) if fp else (c.duration or 0.0, "", "")
        self.events.put(("preview_ready", url, dur, codec, err))

    def _on_preview_ready(self, url, dur, codec, err):
        if url != self.player.src:
            return
        if err:
            self.player.state = ERROR
            self.player_text.set(f"미리보기 실패: {err} — [원본 페이지 열기]로 브라우저에서 확인할 수 있습니다.")
            return
        if dur:
            self.player.duration = dur
        self.seek.configure(to=max(1.0, self.player.duration))
        self.player.play(0.0)

    def player_play(self):
        if self.player.src:
            self.player.play()

    def player_pause(self):
        self.player.pause()

    def player_restart(self):
        if self.player.src:
            self.player.restart()

    def _seek_release(self):
        self._seek_drag = False
        if self.player.src:
            self.player.seek(float(self.seek.get()))

    def _tick_player(self):
        try:
            p = self.player
            if p.src:
                self.time_text.set(f"{fmt(p.position)} / {fmt(p.duration) if p.duration else '--:--'}")
                if not self._seek_drag:
                    self.seek.set(min(p.position, p.duration or p.position))
                if p.message:
                    ok = " ✓" if p.confirmed and p.state == PLAYING else ""
                    self.player_text.set(f"미리보기 [{p.state}]{ok}: {p.message}")
            self.after(300, self._tick_player)
        except tk.TclError:
            pass

    # ---------------- 다운로드 ----------------
    def download_selected(self) -> bool:
        if self.downloading:
            return False
        mode = self.mode.get()
        picks = [c for c in self.results[mode] if c.key in self.selected[mode]]
        if not picks:
            messagebox.showinfo("다운로드", "[받기] 칸을 눌러 받을 영상을 고르세요.", parent=self)
            return False
        ff, fp = self._ask_tools()
        lib = Path(self.libdir.get().strip() or default_library_dir())
        self._save()
        self.downloading = True
        self.cancel_dl.clear()
        self._refresh_buttons()
        t = threading.Thread(target=self._dl_worker, args=(picks, mode, self.mode_res[mode], lib, ff, fp), daemon=True)
        self._threads.append(t)
        t.start()
        return True

    def _dl_worker(self, picks, mode, res, lib, ff, fp):
        library = Library(lib)
        done = []
        for k, c in enumerate(picks):
            if self.cancel_dl.is_set():
                break
            rend = c.best(res)
            if rend is None:
                self.events.put(("dl_item", c.key, "건너뜀", "받을 수 있는 파일이 없습니다."))
                continue
            try:
                e = download(c, rend, library, mode=mode, ffprobe=Path(fp) if fp else None,
                             ffmpeg=Path(ff) if ff else None, cancel=self.cancel_dl, check_dns=self.check_dns,
                             progress=lambda f, t, k=k, c=c: self.events.put(
                                 ("dl_prog", (k + f) / len(picks), f"[{k + 1}/{len(picks)}] {c.title[:40]} · {t}")))
                done.append(e["path"])
                self.events.put(("dl_item", c.key, e["status"], e.get("note") or "; ".join(e.get("warnings") or [])))
            except DownloadError as ex:
                self.events.put(("dl_item", c.key, "실패", str(ex)))
            except Exception as ex:  # pragma: no cover
                self.events.put(("dl_item", c.key, "실패", f"{type(ex).__name__}"))
        self.events.put(("dl_done", done))

    def _refresh_library(self):
        for x in self.lib.get_children():
            self.lib.delete(x)
        for i, e in enumerate(Library(Path(self.libdir.get().strip() or default_library_dir())).entries()):
            p = Path(e.get("path", ""))
            if not p.is_file():
                continue
            pr = e.get("probe") or {}
            probe = (f"{pr.get('width')}×{pr.get('height')} · {pr.get('fps')}fps · {pr.get('codec')} · "
                     f"{fmt(float(pr.get('duration') or 0))}" + ("" if pr.get("playable") else " ⚠ 재생 검사 실패"))
            self.lib.insert("", "end", iid=str(i), values=(p.name, e.get("provider"), probe,
                                                         (e.get("rights") or {}).get("state", "?"),
                                                         str(e.get("sha256", ""))[:12], "; ".join(e.get("warnings") or [])))
            if str(p) in self.last_downloaded:
                self.lib.selection_add(str(i))
        self._refresh_buttons()

    def _pick_libdir(self):
        d = filedialog.askdirectory(parent=self, title="⑤ 라이브러리 저장 폴더")
        if d:
            self.libdir.set(d)
            self._save()
            self._refresh_library()

    def library_paths(self, selected_only: bool = True) -> list[Path]:
        ids = self.lib.selection() if selected_only else ()
        ids = ids or self.lib.get_children()
        es = [e for e in Library(Path(self.libdir.get().strip() or default_library_dir())).entries()
              if Path(e.get("path", "")).is_file()]
        names = {self.lib.set(i, "file") for i in ids}
        return [Path(e["path"]) for e in es if Path(e["path"]).name in names]

    def send_to_bg(self) -> bool:
        paths = self.library_paths()
        if not paths or not self.send_to_bg_cb:
            messagebox.showinfo("④로 보내기", "받은 영상 목록에서 보낼 영상을 선택하세요.", parent=self)
            return False
        self.send_to_bg_cb(paths)
        self.dl_text.set(f"④ 배경 만들기로 {len(paths)}개를 보냈습니다 (④의 디졸브·반복·중복·화질 설정은 그대로).")
        return True

    # ---------------- API 키 ----------------
    def key_dialog(self):
        for p in KEY_PROVIDERS:
            act = messagebox.askyesnocancel(
                f"API 키 — {p}",
                f"{p} 검색에는 무료 API 키가 필요합니다 (발급: {SIGNUP[p]}).\n현재: {self.keys.source(p)}\n\n"
                "[예] 새 키 입력 · [아니요] 기존 Nature Video Studio 키 불러오기(읽기만) · [취소] 다음 제공처",
                parent=self)
            if act is True:
                k = simpledialog.askstring(f"{p} API 키", f"{p} API 키를 붙여 넣으세요 (화면·로그에 남기지 않음):",
                                           parent=self, show="•")
                if k:
                    try:
                        self.keys.save(p, k)
                        messagebox.showinfo("API 키", f"{p} 키를 이 Windows 계정 전용(DPAPI)으로 저장했습니다.", parent=self)
                    except Exception as e:
                        messagebox.showerror("API 키", str(e), parent=self)
            elif act is False:
                key, msg = read_legacy_key(p)
                if key:
                    if messagebox.askyesno("기존 키", msg + "\n\n이 키를 이 프로그램에 저장할까요?\n[아니요] = 이번 실행에만 사용",
                                           parent=self):
                        self.keys.save(p, key)
                    else:
                        self.keys.use_for_session(p, key)
                else:
                    messagebox.showinfo("기존 키", msg, parent=self)
        self._refresh_providers()

    # ---------------- 이벤트 ----------------
    def _pump(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "prov":
                    self._on_prov(ev[1], ev[2])
                elif kind == "search_done":
                    self._on_search_done(ev[1], ev[2])
                elif kind == "status":
                    self.status.set(ev[1])
                elif kind == "thumb":
                    self.thumbs[ev[1]] = tk.PhotoImage(master=self, data=ev[2])
                    for iid in self.tree.get_children():
                        c = self.results[self.mode.get()][int(iid)]
                        if c.key == ev[1]:
                            self.tree.item(iid, image=self.thumbs[ev[1]], text="")
                elif kind == "thumb_err":
                    for iid in self.tree.get_children():
                        if self.results[self.mode.get()][int(iid)].key == ev[1]:
                            self.tree.item(iid, text="썸네일 실패")
                elif kind == "preview_ready":
                    self._on_preview_ready(*ev[1:])
                elif kind == "dl_prog":
                    self.bar["value"] = int(ev[1] * 1000)
                    self.dl_text.set(ev[2])
                elif kind == "dl_item":
                    c_key, stt, note = ev[1], ev[2], ev[3]
                    self.dl_text.set(f"{stt}: {c_key.split(':', 1)[-1][:50]} {('— ' + note) if note else ''}")
                    self.dl_log.append((c_key, stt, note))
                elif kind == "dl_done":
                    self.downloading = False
                    self.last_downloaded = [str(p) for p in ev[1]]
                    self.bar["value"] = 1000 if ev[1] else 0
                    ok = sum(1 for _, s, _ in self.dl_log if s in ("완료", "이미 받음", "중복"))
                    bad = [f"{k.split(':', 1)[-1][:30]}: {n}" for k, s, n in self.dl_log if s in ("실패", "건너뜀")]
                    self.dl_text.set(f"다운로드 끝: 성공 {ok}개" + (f" · 실패 {len(bad)}개 — " + " / ".join(bad) if bad else "")
                                     + " — 받은 영상을 선택하고 [④ 배경 영상 만들기로 보내기]")
                    self.dl_log = []
                    self._refresh_library()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.after(100, self._pump)
        except tk.TclError:
            pass

    # ---------------- 닫기 ----------------
    def destroy(self):
        self.cancel_search.set()
        self.cancel_dl.set()
        self.stop_thumbs.set()
        self.player.stop()
        for t in self._threads:
            t.join(timeout=5)
        try:
            super().destroy()
        finally:
            release_tk_variables(self)


def _open_path(p: Path):
    try:
        p.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
    except OSError as e:
        messagebox.showerror("열기", str(e))
