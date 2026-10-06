"""초보자용 창 모음 — ⓘ 도움말, [? 사용법], 쉬운 오류 창, 첫 실행 Welcome, 처음 설정 Wizard, 도움말 센터, 설정 점검.

규칙: 버튼은 행동 이름, 오류는 '문제/해결' + 해결 버튼, 기술 내용은 [자세히 보기]에서만.
Enter = 기본 버튼, Esc = 닫기/취소.
"""
from __future__ import annotations

import queue
import tkinter as tk
import webbrowser
from tkinter import filedialog, ttk
from typing import Callable

from .ui_theme import READ_WIDTH, ensure as ensure_theme
from . import help_content as hc
from .cloud_setup_ui import _background
from .diagnostics import FAIL, MARKS, OK, WARN, build_report, check_environment, check_settings, manual_path, open_file
from .tooling import release_tk_variables
from .ui_text import (
    ACTION_LABELS, CONNECT_STEPS, CONNECTING_TEXT, FriendlyError, is_beginner, mark_first_run_done,
)
from .youtube_client_provider import has_bundled_client

STUDIO_URL = "https://studio.youtube.com/"
PRESETS = {  # [한국 채널]/[일본 채널] 자동 설정
    "kr": {"alias": "내 한국 채널", "language": "ko", "timezone": "Asia/Seoul", "label": "한국 YouTube 채널"},
    "jp": {"alias": "내 일본 채널", "language": "ja", "timezone": "Asia/Tokyo", "label": "일본 YouTube 채널"},
}
LANGUAGE_NAMES = {"ko": "한국어", "ja": "日本語", "en": "English", "fr": "Français", "": "설정 안 함"}


def _keys(win, ok: Callable | None, cancel: Callable | None) -> None:
    if ok:
        win.bind("<Return>", lambda e: ok())
    if cancel:
        win.bind("<Escape>", lambda e: cancel())


def channel_kind_label(language: str) -> str:
    return {"ko": "한국 YouTube 채널", "ja": "일본 YouTube 채널"}.get(language, "YouTube 채널")


# ---------------- ⓘ ----------------

class InfoTip(ttk.Label):
    """'ⓘ' — 마우스를 올리거나 누르면 짧은 설명."""

    def __init__(self, master, text: str, **kw):
        super().__init__(master, text="ⓘ", foreground="#1d4fa8", cursor="hand2", **kw)
        ensure_theme(self)  # 글자 크기/버튼 테마 (ui_theme)
        self.tip_text = text
        self._tip = None
        self.bind("<Enter>", lambda e: self.show())
        self.bind("<Leave>", lambda e: self.hide())
        self.bind("<Button-1>", lambda e: self.show())

    def show(self):
        self.hide()
        t = tk.Toplevel(self)
        t.wm_overrideredirect(True)
        x, y = self.winfo_rootx() + 16, self.winfo_rooty() + 18
        t.wm_geometry(f"+{x}+{y}")
        tk.Label(t, text=self.tip_text, justify="left", background="#ffffe8", relief="solid", borderwidth=1,
                 wraplength=360, padx=8, pady=6).pack()
        self._tip = t
        return t

    def hide(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


# ---------------- 작은 대화상자 ----------------

class _Dialog(tk.Toplevel):
    def __init__(self, master, title: str):
        super().__init__(master)
        ensure_theme(self)  # 글자 크기/버튼 테마 (ui_theme)
        self.title(title)
        self.transient(master)
        self.resizable(False, False)
        self.result = None
        self.protocol("WM_DELETE_WINDOW", self.close)

    def close(self, result=None):
        self.result = result
        if not getattr(self, "_destroyed", False):
            self._destroyed = True
            self.destroy()
            release_tk_variables(self)

    def run(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.master.wait_window(self)
        return self.result


def show_usage(master, key: str):
    """각 창의 [? 사용법] — 그 기능 설명만, 5단계 이하."""
    title, text = hc.usage_text(key)
    d = _Dialog(master, title)
    ttk.Label(d, text=title, font="PLS.Section", padding=(14, 12, 14, 4)).pack(anchor="w")
    ttk.Label(d, text=text, justify="left", wraplength=480, padding=(14, 0, 14, 8)).pack(anchor="w")
    row = ttk.Frame(d, padding=(14, 0, 14, 12)); row.pack(fill="x")
    b = ttk.Button(row, text="닫기", command=d.close)
    b.pack(side="right")
    ttk.Button(row, text="자세한 도움말", command=lambda: (d.close(), HelpWindow(master, topic=key))).pack(side="left")
    _keys(d, d.close, d.close)
    b.focus_set()
    d.usage_title = title
    return d


def show_friendly_error(master, fe: FriendlyError, *, actions: dict[str, Callable] | None = None, title: str = "확인 필요",
                        wait: bool = False):
    """'무슨 문제가 생겼나요? / 무엇을 하면 되나요?' + 해결 버튼 + [자세히 보기]."""
    d = _Dialog(master, title)
    body = ttk.Frame(d, padding=16); body.pack(fill="both", expand=True)
    ttk.Label(body, text="무슨 문제가 생겼나요?", foreground="gray35").pack(anchor="w")
    ttk.Label(body, text="⚠ " + fe.problem, font="PLS.Strong", wraplength=480, justify="left").pack(anchor="w", pady=(0, 8))
    ttk.Label(body, text="무엇을 하면 되나요?", foreground="gray35").pack(anchor="w")
    ttk.Label(body, text=fe.action, wraplength=480, justify="left").pack(anchor="w")
    detail = ttk.Label(body, text=fe.detail, foreground="#555555", wraplength=480, justify="left")
    row = ttk.Frame(body); row.pack(fill="x", pady=(12, 0))
    d.action_button = None
    act = (actions or {}).get(fe.action_key)
    if act:
        d.action_button = ttk.Button(row, text=ACTION_LABELS.get(fe.action_key, "해결하기"),
                                     command=lambda: (d.close("action"), act()))
        d.action_button.pack(side="left")
    if fe.detail:
        def toggle():
            if detail.winfo_manager():
                detail.pack_forget()
            else:
                detail.pack(anchor="w", pady=(8, 0), before=row)
        d.detail_button = ttk.Button(row, text="자세히 보기", command=toggle)
        d.detail_button.pack(side="left", padx=6)
    ttk.Button(row, text="닫기", command=d.close).pack(side="right")
    _keys(d, (lambda: d.action_button.invoke()) if d.action_button else d.close, d.close)
    d.detail_label = detail
    return d.run() if wait else d


def ask_connect_guide(master) -> bool:
    """[Google 계정 연결] 전에 무슨 일이 일어나는지 보여준다. [연결 시작]이면 True."""
    d = _Dialog(master, "Google 계정 연결")
    f = ttk.Frame(d, padding=16); f.pack()
    ttk.Label(f, text="Google 계정 연결 순서", font="PLS.Strong").pack(anchor="w", pady=(0, 6))
    for s in CONNECT_STEPS:
        ttk.Label(f, text=s).pack(anchor="w")
    ttk.Label(f, text="비밀번호는 이 프로그램에 입력하지 않습니다. 로그인은 Google 화면에서만 합니다.",
              foreground="gray35", wraplength=420).pack(anchor="w", pady=(8, 0))
    row = ttk.Frame(f); row.pack(fill="x", pady=(12, 0))
    b = ttk.Button(row, text="연결 시작", style="Primary.TButton", command=lambda: d.close(True))
    b.pack(side="left")
    ttk.Button(row, text="취소", command=lambda: d.close(False)).pack(side="right")
    _keys(d, lambda: d.close(True), lambda: d.close(False))
    b.focus_set()
    return bool(d.run())


def show_oauth_help(master, on_pick: Callable | None = None):
    """[이 파일이 뭔가요?] → 단순 설명이 아니라 Google 연결 파일 만들기 도우미."""
    return GoogleConnectionAssistant(master, on_pick=on_pick)


def ask_connection_file(master, *, on_have: Callable, on_first: Callable):
    """[Google 계정 처음 연결하기]: 'Google 연결 파일이 이미 있나요?'"""
    d = _Dialog(master, "Google 계정 연결")
    f = ttk.Frame(d, padding=18); f.pack()
    ttk.Label(f, text="Google 계정 연결", font="PLS.Section").pack(anchor="w")
    ttk.Label(f, text="Google 연결 파일이 이미 있나요?", font="PLS.Lead").pack(anchor="w", pady=(6, 12))
    d.btn_have = ttk.Button(f, text="있어요 - 파일 선택", command=lambda: (d.close("have"), on_have()))
    d.btn_have.pack(fill="x", ipady=4)
    d.btn_first = ttk.Button(f, text="처음이에요 - 만드는 방법 보기", command=lambda: (d.close("first"), on_first()))
    d.btn_first.pack(fill="x", ipady=4, pady=(6, 0))
    ttk.Label(f, text="잘 모르겠으면 '처음이에요'를 누르세요.", foreground="#555555").pack(anchor="w", pady=(10, 0))
    _keys(d, None, d.close)
    d.btn_first.focus_set()
    return d


class GoogleConnectionAssistant(tk.Toplevel):
    """'Google 연결 파일 만들기' — 브라우저 옆에 두고 따라 하는 번호식 안내."""

    def __init__(self, master, *, on_pick: Callable | None = None, open_url: Callable[[str], object] = webbrowser.open):
        super().__init__(master)
        ensure_theme(self)  # 글자 크기/버튼 테마 (ui_theme)
        self.title("Google 연결 파일 만들기")
        sh = self.winfo_screenheight()
        self.geometry(f"640x{max(460, min(680, sh - 120))}")
        self.minsize(520, 420)
        self._on_pick = on_pick
        self._open_url = open_url
        self.keep_on_top = tk.BooleanVar(value=False)  # 강제하지 않음 — 사용자가 선택
        self.copied = tk.StringVar()
        f = ttk.Frame(self, padding=14); f.pack(fill="both", expand=True)
        ttk.Label(f, text="Google 연결 파일 만들기", font="PLS.Title").pack(anchor="w")
        ttk.Label(f, text=hc.GOOGLE_FILE_INTRO, justify="left", wraplength=590, foreground="gray25").pack(anchor="w", pady=(4, 8))
        row = ttk.Frame(f); row.pack(fill="x")
        ttk.Button(row, text="Google Cloud 열기", command=lambda: self._open_url(hc.GOOGLE_CLOUD_URL)).pack(side="left")
        ttk.Button(row, text="Google 공식 설명 열기", command=lambda: self._open_url(hc.GOOGLE_OFFICIAL_URL)).pack(side="left", padx=6)
        ttk.Button(row, text="설정 순서 복사", command=self.copy_steps).pack(side="left")
        ttk.Checkbutton(f, text="브라우저를 보는 동안 안내창을 위에 표시", variable=self.keep_on_top,
                        command=self._apply_top).pack(anchor="w", pady=(6, 0))
        body = ttk.Frame(f); body.pack(fill="both", expand=True, pady=(8, 0))
        self.text = tk.Text(body, wrap="word", height=14, font="PLS.Body", padx=8, pady=6)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", hc.google_steps_text())
        self.text.configure(state="disabled")
        bottom = ttk.Frame(f); bottom.pack(fill="x", pady=(8, 0))
        self.btn_pick = ttk.Button(bottom, text="다운로드한 연결 파일 선택", command=self.pick)
        if on_pick:
            self.btn_pick.pack(side="left", ipady=3)
        ttk.Label(bottom, textvariable=self.copied, foreground="darkgreen").pack(side="left", padx=8)
        ttk.Button(bottom, text="닫기", command=self.destroy).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _keys(self, None, self.destroy)

    def _apply_top(self):
        self.attributes("-topmost", bool(self.keep_on_top.get()))

    def copy_steps(self) -> str:
        text = "Google 연결 파일 만들기\n\n" + hc.google_steps_text()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.copied.set("✓ 설정 순서를 복사했습니다.")
        return text

    def pick(self):
        cb = self._on_pick
        self.destroy()
        if cb:
            cb()

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)


def ask_exit(master, lines: list[str]) -> bool:
    """실행 중인 작업이 있을 때 종료 확인. 거짓 '백그라운드 계속'은 없다 — 종료하면 멈춘다고 정확히 안내."""
    d = _Dialog(master, "현재 작업이 진행 중입니다")
    f = ttk.Frame(d, padding=16); f.pack()
    ttk.Label(f, text="현재 작업이 진행 중입니다.", font="PLS.Strong").pack(anchor="w")
    for line in lines:
        ttk.Label(f, text=line).pack(anchor="w")
    ttk.Label(f, text="\n프로그램을 종료하면 현재 업로드와 댓글 자동 확인이 중단됩니다.\n"
                      "(업로드는 다음에 [▶ 예약 업로드 시작]을 누르면 받은 곳부터 이어서 올립니다.)",
              justify="left", wraplength=440).pack(anchor="w")
    row = ttk.Frame(f); row.pack(fill="x", pady=(12, 0))
    ttk.Button(row, text="종료", command=lambda: d.close(True)).pack(side="left")
    c = ttk.Button(row, text="취소", command=lambda: d.close(False))
    c.pack(side="right")
    _keys(d, lambda: d.close(False), lambda: d.close(False))
    c.focus_set()
    return bool(d.run())


def show_done(master, *, count: int, failed: int, alias: str, on_list: Callable | None = None,
              open_url: Callable[[str], object] = webbrowser.open):
    """업로드가 끝난 뒤 다음 행동."""
    d = _Dialog(master, "예약 업로드 완료")
    f = ttk.Frame(d, padding=16); f.pack()
    head = "✓ 예약 업로드가 완료되었습니다." if not failed else f"⚠ 예약 업로드가 끝났습니다 (확인 필요 {failed}개)."
    ttk.Label(f, text=head, font="PLS.Strong").pack(anchor="w")
    ttk.Label(f, text=f"{count}개 영상 · {alias}").pack(anchor="w", pady=(4, 0))
    row = ttk.Frame(f); row.pack(fill="x", pady=(12, 0))
    b = ttk.Button(row, text="YouTube Studio에서 확인", command=lambda: (open_url(STUDIO_URL), d.close("studio")))
    b.pack(side="left")
    ttk.Button(row, text="업로드 목록 보기", command=lambda: (d.close("list"), on_list() if on_list else None)).pack(side="left", padx=6)
    ttk.Button(row, text="닫기", command=d.close).pack(side="right")
    _keys(d, d.close, d.close)
    d.head = head
    return d


# ---------------- 첫 실행 ----------------

class WelcomeDialog(tk.Toplevel):
    def __init__(self, master, *, on_setup: Callable, on_quick: Callable):
        super().__init__(master)
        ensure_theme(self)  # 글자 크기/버튼 테마 (ui_theme)
        self.title(f"{hc.APP_NAME}에 오신 것을 환영합니다")
        self.transient(master)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=22); f.pack()
        ttk.Label(f, text=hc.APP_NAME, font="PLS.Title").pack(anchor="w")
        ttk.Label(f, text="처음 사용하시나요?", font="PLS.Lead").pack(anchor="w", pady=(6, 0))
        ttk.Label(f, text="영상 만들기 · 예약 업로드 · LIVE를\n한 프로그램에서 할 수 있습니다.", justify="left").pack(anchor="w", pady=(4, 14))
        self.btn_setup = ttk.Button(f, text="처음부터 설정하기", style="Primary.TButton", command=lambda: self._choose(on_setup))
        self.btn_setup.pack(fill="x", ipady=4)
        ttk.Button(f, text="5분 빠른 사용법", command=lambda: self._choose(on_quick)).pack(fill="x", ipady=4, pady=6)
        ttk.Button(f, text="나중에 하기", command=lambda: self._choose(None)).pack(fill="x", ipady=4)
        ttk.Label(f, text=f"언제든 메인의 [{hc.BUTTONS['guide']}]로 다시 볼 수 있습니다.", foreground="#555555").pack(anchor="w", pady=(10, 0))
        self.protocol("WM_DELETE_WINDOW", lambda: self._choose(None))
        _keys(self, lambda: self._choose(on_setup), lambda: self._choose(None))
        self.btn_setup.focus_set()

    def _choose(self, fn):
        mark_first_run_done()  # 다음 실행부터 자동으로 보이지 않음
        self.destroy()
        if fn:
            fn()

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)


class SetupWizard(tk.Toplevel):
    STEPS = 4
    TITLES = {1: "기본 프로그램 점검", 2: "YouTube 채널 연결", 3: "기본 업로드 설정", 4: "완료"}

    def __init__(self, master, *, profiles, connect: Callable | None = None, ffmpeg_finder: Callable,
                 pick_ffmpeg: Callable[[], bool] | None = None, internet: Callable[[], bool] | None = None,
                 pick_file: Callable = filedialog.askopenfilename, guide: Callable = ask_connect_guide,
                 on_open_upload: Callable | None = None, open_channels: Callable | None = None):
        super().__init__(master)
        ensure_theme(self)  # 글자 크기/버튼 테마 (ui_theme)
        self.title("처음 설정")
        sh = self.winfo_screenheight()
        self.geometry(f"840x{max(480, min(720, sh - 100))}")
        self.minsize(640, 460)
        self.transient(master)
        from .youtube_accounts import connect_profile
        self.profiles = profiles
        self._connect = connect or connect_profile
        self._ffmpeg_finder = ffmpeg_finder
        self._pick_ffmpeg = pick_ffmpeg
        self._internet = internet
        self._pick_file = pick_file
        self._guide = guide
        self._on_open_upload = on_open_upload
        self._open_channels = open_channels
        self._q: queue.Queue = queue.Queue()
        self._worker = None
        self.step = 1
        self.env: list = []
        self.profile = None  # 이번에 만든/연결 중인 YouTube 채널
        self.client_file = tk.StringVar()
        self.alias_var = tk.StringVar()  # 프로그램 안에서 부르는 이름 (실제 YouTube 채널 이름과 달라도 됨)
        self.alias_msg = tk.StringVar()
        self.connect_msg = tk.StringVar()
        self.result_text = tk.StringVar()
        self.show_advanced = False
        from .settings import load_settings
        self.default_time = tk.StringVar(value=(load_settings().get("upload_defaults") or {}).get("time", "19:00"))
        self.step_title = tk.StringVar()
        self.lbl_step = ttk.Label(self, textvariable=self.step_title, font="PLS.Hero", padding=(18, 14, 18, 6))
        self.lbl_step.pack(anchor="w")
        # 아래쪽 이동 버튼은 항상 보이게 먼저 자리 잡고 (작은 화면), 본문은 세로 스크롤
        nav = ttk.Frame(self, padding=(18, 10, 18, 14)); nav.pack(side="bottom", fill="x")
        ttk.Separator(self).pack(side="bottom", fill="x")
        self.btn_back = ttk.Button(nav, text="◀ 이전", style="Secondary.TButton", width=9,
                                   command=lambda: self.go(self.step - 1))
        self.btn_back.pack(side="left")
        self.btn_next = ttk.Button(nav, text="다음 ▶", style="Primary.TButton", width=10, command=self.next)
        self.btn_next.pack(side="right")
        from .ui_scroll import ScrollFrame
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        self.body = ttk.Frame(self.scroll.body, padding=(18, 8, 18, 14))
        self.body.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _keys(self, self.next, self.destroy)
        self.render()
        self.after(200, self._pump)

    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def go(self, step: int):
        if self.busy:
            return
        self.step = max(1, min(self.STEPS, step))
        self.render()

    def next(self):
        if self.busy:
            return
        if self.step == 2 and self.profile is not None and not self.apply_alias():
            return  # 별칭이 비었거나 겹치면 다음으로 가지 않는다
        if self.step == 3:
            self.save_defaults()
        if self.step == self.STEPS:
            return self.finish()
        self.go(self.step + 1)

    def render(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.step_title.set(f"STEP {self.step} / {self.STEPS} · {self.TITLES[self.step]}")
        getattr(self, f"_step{self.step}")()
        self.btn_back.configure(state="normal" if self.step > 1 and not self.busy else "disabled")
        self.btn_next.configure(text="완료" if self.step == self.STEPS else "다음 ▶",
                                state="disabled" if self.busy else "normal")
        self.btn_next.focus_set()

    # STEP 1
    def run_checks(self):
        kw = {"internet": self._internet} if self._internet else {}
        self.env = check_environment(ffmpeg_finder=self._ffmpeg_finder, **kw)
        return self.env

    def _step1(self):
        ttk.Label(self.body, text="프로그램에 필요한 것들을 자동으로 확인합니다.").pack(anchor="w", pady=(0, 8))
        for item in self.run_checks():
            color = {OK: "darkgreen", WARN: "darkorange", FAIL: "firebrick"}[item.status]
            ttk.Label(self.body, text=item.line, foreground=color, wraplength=640, justify="left").pack(anchor="w")
        if any(i.fix == "ffmpeg" for i in self.env):
            row = ttk.Frame(self.body); row.pack(anchor="w", pady=(10, 0))
            ttk.Button(row, text="자동으로 다시 찾기", command=self.render).pack(side="left")
            ttk.Button(row, text="직접 선택", command=self._pick_ffmpeg_now).pack(side="left", padx=6)
            ttk.Button(row, text="도움말", command=lambda: HelpWindow(self, topic="trouble")).pack(side="left")
            ttk.Label(self.body, text="FFmpeg가 없어도 예약 업로드·댓글은 쓸 수 있습니다. 영상 늘리기와 LIVE에만 필요합니다.",
                      foreground="gray35", wraplength=640).pack(anchor="w", pady=(6, 0))

    def _pick_ffmpeg_now(self):
        if self._pick_ffmpeg:
            self._pick_ffmpeg()
        self.render()

    # STEP 2
    def _step2(self):
        ttk.Label(self.body, text="어떤 YouTube 채널을 연결할까요?", font="PLS.Section").pack(anchor="w")
        row = ttk.Frame(self.body); row.pack(anchor="w", pady=(8, 10))
        self.btn_kr = ttk.Button(row, text="한국 채널", style="Primary.TButton", command=lambda: self.choose_preset("kr"))
        self.btn_kr.pack(side="left")
        ttk.Button(row, text="일본 채널", style="Primary.TButton",
                   command=lambda: self.choose_preset("jp")).pack(side="left", padx=8)
        ttk.Button(row, text="직접 설정", style="Secondary.TButton", command=self._custom).pack(side="left")
        connected = [p for p in self.profiles.all() if p.channel_id]
        if connected:
            ttk.Label(self.body, text="연결된 채널: " + ", ".join(f"{p.alias} ({p.channel_title})" for p in connected),
                      foreground="darkgreen", wraplength=640).pack(anchor="w")
        if self.profile is None:
            ttk.Label(self.body, text="나중에 연결하려면 [다음 ▶]을 누르세요. 메인의 ③ 예약 업로드 → [YouTube 채널 관리]에서도 할 수 있습니다.",
                      foreground="gray35", wraplength=640).pack(anchor="w", pady=(8, 0))
            return
        p = self.profile
        box = ttk.LabelFrame(self.body, text=f"{channel_kind_label(p.language)} · "
                             f"{LANGUAGE_NAMES.get(p.language, p.language)} · {p.timezone}", padding=14)
        box.pack(fill="x", pady=(6, 0))
        W = READ_WIDTH
        ttk.Label(box, text="1. YouTube 채널 이름(별칭)", font="PLS.Section").pack(anchor="w")
        ar = ttk.Frame(box); ar.pack(fill="x", pady=(6, 2))
        self.ent_alias = ttk.Entry(ar, textvariable=self.alias_var, width=26, font="PLS.Body")
        self.ent_alias.pack(side="left")
        self.ent_alias.bind("<Return>", lambda e: (self.apply_alias(), "break")[1])
        ttk.Button(ar, text="이름 저장", style="Secondary.TButton", command=self.apply_alias).pack(side="left", padx=8)
        ttk.Label(box, text="프로그램 안에서 구분하기 위한 이름입니다. 실제 YouTube 채널 이름과 달라도 됩니다.\n"
                            "(실제 YouTube 채널 이름은 Google 연결 후 따로 보여드립니다.)",
                  style="Hint.TLabel", justify="left", wraplength=W).pack(anchor="w")
        ttk.Label(box, textvariable=self.alias_msg, foreground="firebrick", font="PLS.Strong").pack(anchor="w")
        ttk.Separator(box).pack(fill="x", pady=10)
        ttk.Label(box, text="2. Google 계정 연결 준비", font="PLS.Section").pack(anchor="w", pady=(0, 6))
        self.btn_connect = None
        if has_bundled_client():  # 배포용 기본 연결 정보가 있으면 파일 선택 없이 바로 연결
            ttk.Label(box, text="이 프로그램에 들어 있는 Google 연결 정보를 사용합니다.", wraplength=W).pack(anchor="w")
        elif is_beginner() and not self.client_file.get():  # 초보자: 파일부터 요구하지 않는다
            ttk.Label(box, text="Google 계정을 처음 연결하나요?", font="PLS.Strong").pack(anchor="w")
            ttk.Label(box, text="아래 버튼을 누르면 무엇을 해야 하는지 차근차근 안내합니다.", wraplength=W,
                      justify="left").pack(anchor="w")
            self.btn_first_connect = ttk.Button(box, text="Google 계정 처음 연결하기", style="Primary.TButton",
                                                command=self.start_first_connect)
            self.btn_first_connect.pack(anchor="w", pady=(10, 0))
        else:
            ttk.Label(box, text="Google 연결 파일이 필요합니다.", font="PLS.Strong").pack(anchor="w")
            ttk.Label(box, text="Google Cloud에서 받은 '데스크톱 앱용 JSON 파일'입니다.", wraplength=W,
                      justify="left").pack(anchor="w")
            fr = ttk.Frame(box); fr.pack(anchor="w", pady=(10, 4))
            ttk.Button(fr, text=hc.BUTTONS["pick_oauth"], style="Primary.TButton",
                       command=self.pick_client_file).pack(side="left")
            ttk.Button(fr, text=hc.BUTTONS["what_oauth"], style="Secondary.TButton",
                       command=lambda: show_oauth_help(self, on_pick=self.pick_client_file)).pack(side="left", padx=8)
            ttk.Label(box, textvariable=self.client_file, style="Hint.TLabel", wraplength=W).pack(anchor="w")
        if has_bundled_client() or self.client_file.get() or not is_beginner():
            ttk.Label(box, text="3. Google 계정 연결", font="PLS.Section").pack(anchor="w", pady=(12, 0))
            self.btn_connect = ttk.Button(box, text=hc.BUTTONS["connect"], style="Primary.TButton", command=self.start_connect)
            self.btn_connect.pack(anchor="w", pady=(6, 0))
        ttk.Label(box, textvariable=self.connect_msg, font="PLS.Section", wraplength=W).pack(anchor="w", pady=(10, 0))
        ttk.Label(box, textvariable=self.result_text, justify="left", font="PLS.Lead").pack(anchor="w")
        if p.channel_id:
            ttk.Button(box, text="고급 정보 보기", command=self.toggle_advanced).pack(anchor="w", pady=(4, 0))
            if self.show_advanced:
                ttk.Label(box, text=f"Channel ID: {p.channel_id}", foreground="#555555").pack(anchor="w")

    def choose_preset(self, key: str):
        from .youtube_accounts import ChannelProfile, new_profile_id
        pre = PRESETS[key]
        names = {p.alias for p in self.profiles.all()}
        alias, n = pre["alias"], 2
        while alias in names:
            alias, n = f"{pre['alias']} {n}", n + 1
        self.profile = self.profiles.add(ChannelProfile(new_profile_id(), alias, language=pre["language"],
                                                        timezone=pre["timezone"]))
        self.alias_var.set(alias)  # 기본 이름 — 바로 아래 칸에서 자유롭게 바꿀 수 있다
        self.alias_msg.set("")
        self.client_file.set("")
        self.connect_msg.set("")
        self.result_text.set("")
        self.render()
        return self.profile

    def apply_alias(self) -> bool:
        """별칭 저장: 앞뒤 공백 제거, 빈 이름/같은 별칭 거부 (채널 관리와 같은 규칙). 언어·시간대는 그대로."""
        from .youtube_accounts import ProfileError
        if self.profile is None:
            return False
        name = self.alias_var.get().strip()
        self.alias_var.set(name)
        if not name:
            self.alias_msg.set("⚠ 이름을 입력하세요 (예: 한국 시니어).")
            return False
        cur = self.profiles.get(self.profile.profile_id) or self.profile
        if name == cur.alias:
            self.alias_msg.set("")
            return True
        old = cur.alias
        cur.alias = name
        try:
            self.profile = self.profiles.save(cur)
        except (ProfileError, ValueError) as e:
            cur.alias = old
            self.alias_msg.set(f"⚠ {e}")
            return False
        self.alias_msg.set("")
        return True

    def _custom(self):
        if self._open_channels:
            self._open_channels()

    def pick_client_file(self):
        p = self._pick_file(parent=self, title=hc.BUTTONS["pick_oauth"], filetypes=[("JSON", "*.json"), ("모든 파일", "*.*")])
        if p:
            self.client_file.set(p)
            if not getattr(self, "_destroyed", False):
                keep = (self.connect_msg.get(), self.result_text.get())
                self.render()  # 파일을 고르면 [Google 계정 연결] 버튼이 나온다
                self.connect_msg.set(keep[0])
                self.result_text.set(keep[1])

    def start_first_connect(self):
        """[Google 계정 처음 연결하기] → '파일이 이미 있나요?' → 있으면 선택 / 처음이면 만들기 도우미."""
        if not self.apply_alias():
            return None
        self.file_dialog = ask_connection_file(
            self, on_have=self.pick_client_file,
            on_first=lambda: setattr(self, "assistant", GoogleConnectionAssistant(self, on_pick=self.pick_client_file)))
        return self.file_dialog

    def toggle_advanced(self):
        self.show_advanced = not self.show_advanced
        self.render()

    def start_connect(self):
        if self.busy or self.profile is None:
            return
        if not self.apply_alias():
            return
        from .youtube_client_provider import BUNDLED_MARKER, resolve_client
        from .youtube_oauth import OAuthError
        from .ui_text import friendly_error
        path = self.client_file.get().strip() or (BUNDLED_MARKER if has_bundled_client() else "")
        try:
            resolve_client(path)
        except OAuthError as e:
            fe = friendly_error(e)
            self.connect_msg.set(f"⚠ {fe.problem}")
            self.result_text.set(fe.action)
            return
        if self._guide and not self._guide(self):
            return
        self.connect_msg.set(CONNECTING_TEXT)
        profiles, connect, p = self.profiles, self._connect, self.profile

        self._worker = _background("setup-connect", self._q,
                                   lambda: connect(profiles, p, path, open_browser=webbrowser.open),
                                   lambda ok, v: ("conn", ok, v))
        self.btn_next.configure(state="disabled")
        self.btn_back.configure(state="disabled")

    def _pump(self):
        if getattr(self, "_destroyed", False):
            return
        try:
            while True:
                tag, ok, payload = self._q.get_nowait()
                self._worker = None
                if ok:
                    self.profile = payload
                    self.connect_msg.set("✓ 연결 완료")
                    self.result_text.set(f"✓ YouTube 채널 연결 완료\n별칭: {payload.alias}\n"
                                         f"실제 YouTube 채널: {payload.channel_title}\n"
                                         f"언어: {LANGUAGE_NAMES.get(payload.language, payload.language)}\n시간대: {payload.timezone}")
                else:
                    from .ui_text import friendly_error
                    fe = friendly_error(payload)
                    self.connect_msg.set(f"⚠ {fe.problem}")
                    self.result_text.set(fe.action)
                keep = (self.connect_msg.get(), self.result_text.get())
                self.render()
                self.connect_msg.set(keep[0])
                self.result_text.set(keep[1])
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if self.winfo_exists():
            self.after(200, self._pump)

    # STEP 3
    def _step3(self):
        ttk.Label(self.body, text="잘 모르면 그대로 [다음 ▶]을 누르세요. 나중에 언제든 바꿀 수 있습니다.").pack(anchor="w", pady=(0, 8))
        r = ttk.Frame(self.body); r.pack(anchor="w")
        ttk.Label(r, text="기본 예약 시간").pack(side="left")
        ttk.Entry(r, textvariable=self.default_time, width=7).pack(side="left", padx=4)
        for t in ("07:00", "09:00", "18:00", "19:00", "21:00"):
            ttk.Button(r, text=t, width=6, command=lambda v=t: self.default_time.set(v)).pack(side="left")
        for line in ("• 공개 방식: 예약 공개 (정한 날짜·시간에 자동 공개)", "• 썸네일: 같은 이름의 사진 자동 연결",
                     "• 제목: 파일 이름", "• 언어·시간대·카테고리: 채널 설정을 따름", "• 첫 댓글 자동등록: 꺼짐 (필요하면 상세 설정에서)"):
            ttk.Label(self.body, text=line).pack(anchor="w")

    def save_defaults(self):
        from .settings import update_settings
        t = self.default_time.get().strip()
        try:
            h, m = t.split(":")
            assert 0 <= int(h) < 24 and 0 <= int(m) < 60
        except (ValueError, AssertionError):
            t = "19:00"
            self.default_time.set(t)
        update_settings(upload_defaults={"time": t})

    # STEP 4
    def _step4(self):
        ttk.Label(self.body, text="✓ 처음 설정이 끝났습니다.", font="PLS.Strong").pack(anchor="w")
        for item in self.env:
            ttk.Label(self.body, text=item.line, wraplength=640).pack(anchor="w")
        connected = [p for p in self.profiles.all() if p.channel_id]
        ttk.Label(self.body, text=("✓ 연결된 YouTube 채널: " + ", ".join(p.alias for p in connected)) if connected
                  else "⚠ 아직 연결된 YouTube 채널이 없습니다 (나중에 연결할 수 있습니다).").pack(anchor="w", pady=(6, 0))
        ttk.Label(self.body, text=f"✓ 기본 예약 시간: {self.default_time.get()}").pack(anchor="w")
        ttk.Button(self.body, text="③ 예약 업로드 열기", command=lambda: self.finish(open_upload=True)).pack(anchor="w", pady=(12, 0), ipady=3)

    def finish(self, open_upload: bool = False):
        mark_first_run_done()
        cb = self._on_open_upload if open_upload else None
        self.destroy()
        if cb:
            cb()

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)


# ---------------- 도움말 센터 ----------------

class HelpWindow(tk.Toplevel):
    def __init__(self, master, *, topic: str = "quick", diagnostics: Callable[[], str] | None = None):
        super().__init__(master)
        ensure_theme(self)  # 글자 크기/버튼 테마 (ui_theme)
        self.title(f"? 도움말 · {hc.APP_NAME}")
        sh = self.winfo_screenheight()
        sw = self.winfo_screenwidth()
        self.geometry(f"{max(760, min(1000, sw - 80))}x{max(460, min(680, sh - 120))}")
        self.minsize(640, 420)
        self._diagnostics = diagnostics
        self.query = tk.StringVar()
        self.copied = tk.StringVar()
        top = ttk.Frame(self, padding=(14, 12, 14, 0)); top.pack(fill="x")
        ttk.Button(top, text="사용자 매뉴얼 열기", command=self.open_manual).pack(side="right")  # 먼저: 잘리지 않게
        ttk.Label(top, text="검색").pack(side="left")
        e = ttk.Entry(top, textvariable=self.query, width=24, font="PLS.Body")
        e.pack(side="left", padx=6)
        e.bind("<KeyRelease>", lambda ev: self.refresh_list())
        ttk.Label(self, text="예: 채널 연결 · 썸네일 · 댓글 · 업로드 · FFmpeg · 재생목록", style="Hint.TLabel",
                  padding=(14, 4, 14, 0)).pack(anchor="w")
        mid = ttk.Frame(self, padding=14); mid.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(mid, width=26, exportselection=False, activestyle="none", font="PLS.Body")
        self.listbox.pack(side="left", fill="y")
        self.listbox.bind("<<ListboxSelect>>", lambda e: self._on_pick())
        right = ttk.Frame(mid); right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.text = tk.Text(right, wrap="word", font="PLS.Body", padx=10, pady=8)
        sb = ttk.Scrollbar(right, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.diag_row = ttk.Frame(self, padding=(10, 0)); self.diag_row.pack(fill="x")
        self.btn_diag = ttk.Button(self.diag_row, text=hc.BUTTONS["copy_diag"], command=self.copy_diagnostics)
        ttk.Label(self.diag_row, textvariable=self.copied, foreground="darkgreen").pack(side="right")
        bottom = ttk.Frame(self, padding=10); bottom.pack(fill="x")
        ttk.Label(bottom, text=f"{hc.APP_NAME}\nManual version {hc.MANUAL_VERSION}", foreground="#555555",
                  justify="left").pack(side="left")
        ttk.Button(bottom, text="닫기", command=self.destroy).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _keys(self, None, self.destroy)
        self.topics: list = []
        self.refresh_list(select=topic)
        e.focus_set()

    def refresh_list(self, select: str | None = None):
        self.topics = hc.search(self.query.get())
        self.listbox.delete(0, "end")
        for t in self.topics:
            self.listbox.insert("end", t.title)
        keys = [t.key for t in self.topics]
        idx = keys.index(select) if select in keys else 0
        if self.topics:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(idx)
            self.show(self.topics[idx].key)
        else:
            self._set_text("찾는 내용이 없습니다. 다른 말로 검색해 보세요 (예: 채널, 업로드, 댓글).")

    def _on_pick(self):
        sel = self.listbox.curselection()
        if sel and sel[0] < len(self.topics):
            self.show(self.topics[sel[0]].key)

    def _set_text(self, s: str):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", s)
        self.text.configure(state="disabled")

    def show(self, key: str):
        t = hc.topic(key)
        self.current = key
        self._set_text(f"{t.title}\n\n{t.body}")
        if key == "trouble":
            self.btn_diag.pack(side="left")
        else:
            self.btn_diag.pack_forget()

    def copy_diagnostics(self) -> str:
        text = self._diagnostics() if self._diagnostics else build_report()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.copied.set("✓ 복사했습니다. 메신저/메일에 붙여넣으세요 (비밀값은 들어가지 않습니다).")
        self.last_report = text
        return text

    def open_manual(self) -> bool:
        p = manual_path()
        if p is None:
            self.copied.set("사용자 매뉴얼 파일을 찾지 못했습니다.")
            return False
        return open_file(p)

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)


# ---------------- 설정 점검 ----------------

class SettingsCheckWindow(tk.Toplevel):
    def __init__(self, master, *, profiles, ffmpeg_ok: Callable[[], bool], comment_store=None,
                 fixes: dict[str, Callable] | None = None):
        super().__init__(master)
        ensure_theme(self)  # 글자 크기/버튼 테마 (ui_theme)
        self.title("⚙ 설정 점검")
        self.transient(master)
        self.minsize(520, 300)
        self._profiles, self._ffmpeg_ok, self._store = profiles, ffmpeg_ok, comment_store
        self.fixes = fixes or {}
        self.body = ttk.Frame(self, padding=16)
        self.body.pack(fill="both", expand=True)
        row = ttk.Frame(self, padding=(16, 0, 16, 14)); row.pack(fill="x")
        ttk.Button(row, text="다시 점검", command=self.refresh).pack(side="left")
        ttk.Button(row, text="닫기", command=self.destroy).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _keys(self, self.refresh, self.destroy)
        self.items: list = []
        self.refresh()

    def refresh(self):
        for w in self.body.winfo_children():
            w.destroy()
        ttk.Label(self.body, text="설정 점검 결과", font="PLS.Strong").pack(anchor="w", pady=(0, 6))
        self.items = check_settings(profiles=self._profiles, ffmpeg_ok=self._ffmpeg_ok(), comment_store=self._store)
        for item in self.items:
            r = ttk.Frame(self.body); r.pack(fill="x", pady=1)
            color = {OK: "darkgreen", WARN: "darkorange", FAIL: "firebrick"}[item.status]
            ttk.Label(r, text=item.line, foreground=color, wraplength=420, justify="left").pack(side="left")
            if item.status != OK and item.fix in self.fixes:
                ttk.Button(r, text="수정", command=lambda f=self.fixes[item.fix]: (self.destroy(), f())).pack(side="right")
        if all(i.status == OK for i in self.items):
            ttk.Label(self.body, text="\n모두 준비되었습니다.", foreground="darkgreen").pack(anchor="w")
        return self.items

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)


__all__ = ["InfoTip", "show_usage", "show_friendly_error", "ask_connect_guide", "show_oauth_help", "ask_exit",
           "show_done", "WelcomeDialog", "SetupWizard", "HelpWindow", "SettingsCheckWindow", "MARKS"]
