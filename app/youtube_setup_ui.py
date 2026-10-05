"""YouTube 자동 세션 연결 도우미 (5 STEP). 전문 OAuth 용어는 [상세]에서만.

프로그램은 Google 비밀번호를 받지 않는다 — 로그인/동의는 시스템 브라우저에서 Google이 직접 처리한다.
"""
from __future__ import annotations

import queue
import tkinter as tk
import webbrowser
from tkinter import filedialog, ttk
from typing import Callable

from .cloud_setup_ui import _background
from .tooling import release_tk_variables
from .youtube_config import (
    GOOGLE_API_LIBRARY_URL, GOOGLE_CONSENT_URL, GOOGLE_CREDENTIALS_URL, connect_account, load_youtube_settings,
    save_youtube_settings,
)
from .youtube_api import YouTubeApiError
from .youtube_oauth import TESTING_TOKEN_WARNING, YOUTUBE_SCOPE, OAuthError, load_client_file

STEP2_TEXT = """Google Cloud Console에서 아래 순서로 'OAuth 클라이언트'를 만드세요.

  1. OAuth 동의 화면: 사용자 유형 '외부' → 앱 이름 입력 → 테스트 사용자에 내 Google 계정 추가
  2. 사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID
  3. 애플리케이션 유형: 데스크톱 앱
  4. 만든 뒤 'JSON 다운로드'를 눌러 파일을 PC에 저장 (다음 단계에서 선택)

이 JSON 파일은 다른 사람과 공유하거나 인터넷에 올리지 마세요."""


class YouTubeSetupWizard(tk.Toplevel):
    STEPS = 5

    def __init__(self, master, *, on_done: Callable[[dict], None] | None = None,
                 connect: Callable[..., object] = connect_account, open_url: Callable[[str], object] = webbrowser.open,
                 pick_file: Callable = filedialog.askopenfilename):
        super().__init__(master)
        self.title("YouTube 자동 세션 연결")
        self.geometry("720x600")
        self.minsize(600, 520)
        self.transient(master)
        self._on_done = on_done
        self._connect = connect
        self._open_url = open_url
        self._pick_file = pick_file
        self._q: queue.Queue = queue.Queue()
        self._worker = None
        self._detail = False
        self.step = 1
        prev = load_youtube_settings()
        self.client_file = tk.StringVar(value=prev.get("client_file", ""))
        self.file_msg = tk.StringVar()
        self.conn_msg = tk.StringVar()
        self.channel_title = tk.StringVar(value=prev.get("channel_title", ""))
        self.step_title = tk.StringVar()
        self.client_ok = False
        self.connected = bool(prev.get("channel_id"))

        ttk.Label(self, textvariable=self.step_title, font=("Segoe UI", 14, "bold"), padding=(12, 10, 12, 4)).pack(anchor="w")
        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill="both", expand=True)
        nav = ttk.Frame(self, padding=12); nav.pack(fill="x")
        self.btn_back = ttk.Button(nav, text="◀ 이전", command=lambda: self._go(self.step - 1))
        self.btn_back.pack(side="left")
        ttk.Button(nav, text="상세", command=self._toggle_detail).pack(side="left", padx=8)
        self.btn_next = ttk.Button(nav, text="다음 ▶", command=self._next)
        self.btn_next.pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._render()
        self.after(200, self._pump)

    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _go(self, step: int):
        if self.busy:
            return
        self.step = max(1, min(self.STEPS, step))
        self._render()

    def _next(self):
        if self.step == self.STEPS:
            self._finish()
        else:
            self._go(self.step + 1)

    def _render(self):
        self._clear()
        getattr(self, f"_step{self.step}")()
        self.btn_back.configure(state="normal" if self.step > 1 and not self.busy else "disabled")
        can_next = {3: self.client_ok, 4: self.connected}.get(self.step, True)
        self.btn_next.configure(text="완료" if self.step == self.STEPS else "다음 ▶",
                                state="normal" if can_next and not self.busy else "disabled")
        if self._detail:
            ttk.Label(self.body, foreground="gray30", justify="left", wraplength=660, text=(
                f"[상세] 방식: OAuth 2.0 데스크톱 앱 · 브라우저 + 127.0.0.1 loopback(임의 포트) · PKCE(S256) · state 검사\n"
                f"권한(scope): {YOUTUBE_SCOPE}\n"
                "저장: refresh token은 Windows DPAPI 암호화 파일, Client JSON은 파일 위치만, 비밀번호는 받지 않음.")).pack(anchor="w", pady=(12, 0))

    def _step1(self):
        self.step_title.set("STEP 1/5 · YouTube API 켜기")
        ttk.Label(self.body, wraplength=660, justify="left", text=(
            "YouTube LIVE를 11시간 50분마다 새 방송으로 자동 교체하려면, 내 Google 계정에서\n"
            "'YouTube Data API v3'를 한 번 켜야 합니다. (무료 기본 사용량 안에서 동작합니다)\n\n"
            "Google Cloud Console에서 프로젝트를 고른 뒤 [사용]을 누르세요.")).pack(anchor="w")
        ttk.Button(self.body, text="Google Cloud에서 YouTube API 열기",
                   command=lambda: self._open_url(GOOGLE_API_LIBRARY_URL)).pack(anchor="w", pady=12)

    def _step2(self):
        self.step_title.set("STEP 2/5 · 연결용 클라이언트 만들기")
        ttk.Label(self.body, text=STEP2_TEXT, justify="left", wraplength=660).pack(anchor="w")
        ttk.Label(self.body, text="⚠ " + TESTING_TOKEN_WARNING, foreground="firebrick", justify="left",
                  wraplength=660).pack(anchor="w", pady=(10, 0))
        row = ttk.Frame(self.body); row.pack(anchor="w", pady=10)
        ttk.Button(row, text="OAuth 동의 화면 열기", command=lambda: self._open_url(GOOGLE_CONSENT_URL)).pack(side="left")
        ttk.Button(row, text="사용자 인증 정보 열기", command=lambda: self._open_url(GOOGLE_CREDENTIALS_URL)).pack(side="left", padx=6)

    def _step3(self):
        self.step_title.set("STEP 3/5 · 다운로드한 JSON 파일 선택")
        row = ttk.Frame(self.body); row.pack(fill="x")
        ttk.Entry(row, textvariable=self.client_file).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="찾기", command=self._pick).pack(side="left", padx=(4, 0))
        self.lbl_file = ttk.Label(self.body, textvariable=self.file_msg, justify="left", wraplength=660)
        self.lbl_file.pack(anchor="w", pady=8)
        ttk.Label(self.body, foreground="gray30", text="파일 내용은 프로그램 설정에 복사하지 않고 위치만 기억합니다.").pack(anchor="w")
        if self.client_file.get() and not self.client_ok:
            self._check_file()

    def _pick(self):
        p = self._pick_file(parent=self, title="Google 연결 파일 선택", filetypes=[("JSON", "*.json"), ("모든 파일", "*.*")])
        if p:
            self.client_file.set(p)
            self._check_file()

    def _check_file(self):
        try:
            load_client_file(self.client_file.get())
        except OAuthError as e:
            self.client_ok = False
            self.file_msg.set(f"✗ {e}")
            self.lbl_file.configure(foreground="firebrick")
        else:
            self.client_ok = True
            save_youtube_settings(client_file=self.client_file.get().strip().strip('"'))
            self.file_msg.set("✓ 데스크톱 앱 클라이언트 확인됨")
            self.lbl_file.configure(foreground="darkgreen")
        self.btn_next.configure(state="normal" if self.client_ok else "disabled")

    def _step4(self):
        self.step_title.set("STEP 4/5 · Google 계정 연결")
        ttk.Label(self.body, wraplength=660, justify="left", text=(
            "[Google 계정 연결]을 누르면 브라우저가 열립니다.\n"
            "YouTube 채널 계정으로 로그인하고 권한을 허용하면 프로그램으로 자동으로 돌아옵니다.\n"
            "(프로그램은 Google 비밀번호를 받지 않습니다)")).pack(anchor="w")
        self.btn_conn = ttk.Button(self.body, text="Google 계정 연결", command=self._start_connect)
        self.btn_conn.pack(anchor="w", pady=12)
        self.lbl_conn = ttk.Label(self.body, textvariable=self.conn_msg, justify="left", wraplength=660)
        self.lbl_conn.pack(anchor="w")

    def _start_connect(self):
        if self.busy:
            return
        self.conn_msg.set("브라우저에서 로그인/허용을 진행하세요… (최대 5분)")
        self.btn_conn.configure(state="disabled")
        path, connect, open_url = self.client_file.get(), self._connect, self._open_url

        def result(ok, v):
            if ok:
                return ("conn", True, v)
            msg = str(v) if isinstance(v, (OAuthError, YouTubeApiError)) else f"연결 중 오류 ({type(v).__name__})"
            return ("conn", False, msg)
        self._worker = _background("youtube-connect", self._q, lambda: connect(path, open_browser=open_url), result)
        self.btn_back.configure(state="disabled")

    def _step5(self):
        self.step_title.set("STEP 5/5 · 연결 확인")
        if self.connected:
            ttk.Label(self.body, text="✓ YouTube 연결됨", foreground="darkgreen", font=("Segoe UI", 13, "bold")).pack(anchor="w")
            ttk.Label(self.body, text=f"채널: {self.channel_title.get()}", font=("Segoe UI", 11)).pack(anchor="w", pady=(4, 10))
        ttk.Label(self.body, text="⚠ " + TESTING_TOKEN_WARNING, foreground="firebrick", justify="left",
                  wraplength=660).pack(anchor="w")
        ttk.Label(self.body, wraplength=660, justify="left", foreground="gray30", text=(
            "\n처음에는 비공개/일부공개 방송으로 짧게 테스트하세요.\n"
            "이번 버전의 자동 교체는 PC 프로그램이 켜져 있을 때 동작합니다 (Cloud 단독 자동 교체는 다음 단계).")).pack(anchor="w")

    def _toggle_detail(self):
        self._detail = not self._detail
        self._render()

    def _pump(self):
        if getattr(self, "_destroyed", False):
            return
        try:
            while True:
                tag, ok, payload = self._q.get_nowait()
                if tag == "conn":
                    self.connected = ok
                    if ok:
                        self.channel_title.set(payload.title)
                        self.conn_msg.set(f"✓ 연결됨 — 채널: {payload.title}")
                        self._go(5)
                    else:
                        self.conn_msg.set(f"✗ {payload}")
                        self._render()
                        self.lbl_conn.configure(foreground="firebrick")
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if self.winfo_exists():
            self.after(200, self._pump)

    def _finish(self):
        settings = load_youtube_settings()
        self.destroy()
        if self._on_done and self.connected:
            self._on_done(settings)

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)
