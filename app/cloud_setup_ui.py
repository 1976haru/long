"""무료 Cloud 처음 설정 도우미 (4 STEP). 초보자용: 전문용어는 [상세 보기]에서만 보여준다.

FREE SAFETY: Oracle Cloud 계정 가입/로그인/서버 생성/결제는 사용자가 브라우저에서 직접 한다.
이 도우미는 공식 페이지를 브라우저로 열어주고, 사용자가 만든 서버에 SSH로 연결해 LIVE Worker만 설치한다.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .ui_theme import ensure as ensure_theme
from .cloud_client import CloudClient, CloudError, fix_key_permissions
from .tooling import release_tk_variables
from .cloud_model import (
    FREE_NOTICE, FREE_UNSURE, GIT_FOR_WINDOWS_URL, NO_CAPACITY, ORACLE_CONSOLE_URL, ORACLE_FREE_URL, SSH_INVALID,
    SSH_MISSING, CloudConfigError, CloudProfile, SshTool, discover_ssh, load_cloud_profile, load_saved_ssh,
    probe_ssh_executable, save_cloud_profile, save_ssh_tool,
)

STEP2_TEXT = """Oracle Console에서 서버(인스턴스)를 만들 때 아래 값만 고르세요.

  • 이미지(OS):  Ubuntu (LTS 버전)
  • 모양(Shape) 우선:  Ampere A1 (VM.Standard.A1.Flex) — 1 OCPU, 1GB 메모리 이상
  • A1 무료 자리가 없으면:  VM.Standard.E2.1.Micro
  • SSH 키:  'Private Key 저장'을 눌러 키 파일을 PC에 보관하세요 (3단계에서 사용)
  • 서버를 만든 뒤 '공용 IP 주소'를 확인하세요 (3단계에서 사용)

참고: Always Free 자원은 가입할 때 정한 홈 리전에서만 만들 수 있습니다.
일부 리전(예: South Korea North/Chuncheon)은 A1 무료 자리가 부족하다는 보고가 많습니다.
정확한 조건은 Oracle 공식 안내와 Console 표시를 기준으로 하세요.

주의: Oracle은 오랫동안 사용량이 낮은 Always Free 서버를 회수(정지)할 수 있습니다.
LIVE 송출(DIRECT COPY)은 부하가 매우 낮아 이 기준에 해당할 수 있습니다.
이 프로그램은 회수를 피하기 위한 가짜 부하를 만들지 않습니다. 서버가 회수되면 [내 PC에서 LIVE]를 사용하세요."""


def _background(name: str, q: queue.Queue, fn: Callable, make_event: Callable[[bool, object], tuple]) -> threading.Thread:
    """fn을 백그라운드에서 실행하고 결과 이벤트를 큐에 넣는다.

    중요: fn/make_event는 Tk 객체를 참조하면 안 된다. 스레드가 Tk 창의 마지막 참조를 놓으면
    tkinter Variable이 다른 스레드에서 해제되어 "main thread is not in main loop" 오류로 Tk 상태가 깨진다.
    """
    def run():
        try:
            ev = make_event(True, fn())
        except Exception as e:
            ev = make_event(False, e)
        q.put(ev)
    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    return t


class CloudSetupWizard(tk.Toplevel):
    STEPS = 4

    def __init__(self, master, *, on_done: Callable[[CloudProfile], None] | None = None,
                 client_factory: Callable[[CloudProfile], CloudClient] | None = None,
                 open_url: Callable[[str], object] = webbrowser.open,
                 ssh_discover: Callable[[], SshTool | None] | None = None,
                 ssh_probe: Callable = probe_ssh_executable,
                 pick_file: Callable = filedialog.askopenfilename):
        super().__init__(master)
        ensure_theme(self)  # 글자 크기/버튼 테마 (ui_theme)
        self.title("무료 Cloud 처음 설정 도우미")
        self.geometry("720x600")
        self.minsize(600, 520)
        self.transient(master)
        self._on_done = on_done
        # 기본: 이 화면에서 찾은(또는 직접 지정한) ssh.exe로 연결
        self._client_factory = client_factory or (
            lambda profile: CloudClient(profile, ssh=self.ssh_tool.path if self.ssh_tool else None))
        self._open_url = open_url
        self._ssh_discover = ssh_discover or (lambda: discover_ssh(saved=load_saved_ssh()))
        self._ssh_probe = ssh_probe
        self._pick_file = pick_file
        self.ssh_tool: SshTool | None = None
        self.ssh_searching = False
        self._ssh_detail = False
        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self.step = 1
        self.connected = False
        self.prepared = False
        self.client: CloudClient | None = None

        prev = load_cloud_profile()
        self.host = tk.StringVar(value=prev.host if prev else "")
        self.user = tk.StringVar(value=prev.user if prev else "ubuntu")
        self.key_path = tk.StringVar(value=prev.key_path if prev else "")
        self.conn_msg = tk.StringVar()
        self.ssh_msg = tk.StringVar()
        self.ssh_path_msg = tk.StringVar()
        self.prep_msg = tk.StringVar()
        self.step_title = tk.StringVar()

        banner = ttk.Frame(self, padding=(12, 10, 12, 0)); banner.pack(fill="x")
        ttk.Label(banner, text=f"🛡 {FREE_NOTICE}", foreground="darkgreen", font="PLS.Strong").pack(anchor="w")
        ttk.Label(self, textvariable=self.step_title, font="PLS.Title", padding=(12, 8, 12, 4)).pack(anchor="w")
        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill="both", expand=True)
        nav = ttk.Frame(self, padding=12); nav.pack(fill="x")
        self.btn_back = ttk.Button(nav, text="◀ 이전", command=self._back)
        self.btn_back.pack(side="left")
        self.btn_next = ttk.Button(nav, text="다음 ▶", command=self._next)
        self.btn_next.pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._render()
        self.after(200, self._pump)

    # ---------- steps ----------
    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _render(self):
        self._clear()
        getattr(self, f"_step{self.step}")()
        self.btn_back.configure(state="normal" if self.step > 1 and not self.busy else "disabled")
        if self.step == 4:
            self.btn_next.configure(text="닫기" if self.prepared else "다음 ▶",
                                    state="normal" if self.prepared else "disabled")
        elif self.step == 3:
            self.btn_next.configure(text="다음 ▶", state="normal" if self.connected else "disabled")
        else:
            self.btn_next.configure(text="다음 ▶", state="normal")

    def _step1(self):
        self.step_title.set("STEP 1/4 · 무료 Cloud 서버 준비")
        ttk.Label(self.body, justify="left", wraplength=640, text=(
            "Oracle Cloud의 Always Free 서버를 사용합니다.\n"
            "서버 자체를 이 프로그램이 유료로 만들지는 않습니다.\n\n"
            "Oracle Cloud 가입과 서버 만들기는 브라우저에서 직접 진행합니다.\n"
            "(이 프로그램은 자동 가입·로그인·결제를 하지 않습니다.)")).pack(anchor="w")
        row = ttk.Frame(self.body); row.pack(anchor="w", pady=14)
        ttk.Button(row, text="Oracle Cloud 열기", command=lambda: self._open_url(ORACLE_FREE_URL)).pack(side="left")
        ttk.Button(row, text="서버를 이미 만들었습니다", command=lambda: self._go(3)).pack(side="left", padx=8)

    def _step2(self):
        self.step_title.set("STEP 2/4 · 서버 만들 때 고를 값")
        ttk.Label(self.body, text="⚠ Always Free Eligible 표시가 있는지 확인하세요.",
                  foreground="firebrick", font="PLS.Section").pack(anchor="w", pady=(0, 8))
        ttk.Label(self.body, text=STEP2_TEXT, justify="left", wraplength=660).pack(anchor="w")
        box = ttk.LabelFrame(self.body, text="무료 서버를 만들 수 없을 때", padding=8)
        box.pack(fill="x", pady=(10, 0))
        ttk.Label(box, text=NO_CAPACITY, justify="left", foreground="firebrick").pack(anchor="w")
        ttk.Button(self.body, text="Oracle Cloud Console 열기", command=lambda: self._open_url(ORACLE_CONSOLE_URL)).pack(anchor="w", pady=10)

    def _step3(self):
        self.step_title.set("STEP 3/4 · 서버 연결")
        g = ttk.Frame(self.body); g.pack(fill="x")
        ttk.Label(g, text="서버 IP", width=16).grid(row=0, column=0, sticky="w", pady=3)
        self.ent_host = ttk.Entry(g, textvariable=self.host, width=40)
        self.ent_host.grid(row=0, column=1, sticky="we")
        ttk.Label(g, text="사용자 이름", width=16).grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(g, textvariable=self.user, width=40).grid(row=1, column=1, sticky="we")
        ttk.Label(g, text="SSH Private Key", width=16).grid(row=2, column=0, sticky="w", pady=3)
        kr = ttk.Frame(g); kr.grid(row=2, column=1, sticky="we")
        ttk.Entry(kr, textvariable=self.key_path, width=40).pack(side="left", fill="x", expand=True)
        ttk.Button(kr, text="찾기", command=self._pick_key).pack(side="left", padx=(4, 0))
        g.columnconfigure(1, weight=1)
        ttk.Label(self.body, text="Ubuntu 서버의 기본 사용자 이름은 ubuntu 입니다. 키 파일 내용은 저장하지 않고 위치만 기억합니다.",
                  foreground="gray30", wraplength=640).pack(anchor="w", pady=(6, 0))
        # SSH 연결 도구 (Windows OpenSSH가 없어도 Git/GitHub Desktop의 ssh.exe 사용)
        sf = ttk.LabelFrame(self.body, text="SSH 연결 도구", padding=6)
        sf.pack(fill="x", pady=(8, 0))
        sr = ttk.Frame(sf); sr.pack(fill="x")
        self.lbl_ssh = ttk.Label(sr, textvariable=self.ssh_msg)
        self.lbl_ssh.pack(side="left")
        ttk.Button(sr, text="상세", width=5, command=self._toggle_ssh_detail).pack(side="right")
        self.btn_ssh_pick = ttk.Button(sr, text="직접 선택", command=self._pick_ssh)
        self.btn_ssh_pick.pack(side="right", padx=(4, 0))
        self.btn_ssh_find = ttk.Button(sr, text="다시 찾기", command=self._start_ssh_discovery)
        self.btn_ssh_find.pack(side="right", padx=(4, 0))
        self.lbl_ssh_path = ttk.Label(sf, textvariable=self.ssh_path_msg, foreground="gray30", wraplength=640, justify="left")
        self.btn_git_help = ttk.Button(sf, text="Git for Windows 설치 안내 (브라우저)", command=lambda: self._open_url(GIT_FOR_WINDOWS_URL))
        row = ttk.Frame(self.body); row.pack(anchor="w", pady=10)
        self.btn_conn = ttk.Button(row, text="연결 검사", command=self._check_conn)
        self.btn_conn.pack(side="left")
        self.btn_keyfix = ttk.Button(row, text="키 파일 권한 고치기", command=self._fix_key)
        self.lbl_conn = ttk.Label(self.body, textvariable=self.conn_msg, justify="left", wraplength=640)
        self.lbl_conn.pack(anchor="w")
        self._update_ssh_row()

    # ---------- SSH 연결 도구 ----------
    def _start_ssh_discovery(self):
        """STEP 3 진입/[다시 찾기]: 새로 설치한 Git/OpenSSH도 재시작 없이 찾는다 (probe는 스레드에서)."""
        if self.ssh_searching:
            return
        self.ssh_searching = True
        self._update_ssh_row()

        _background("ssh-discover", self._q, self._ssh_discover, lambda ok, v: ("ssh", v if ok else None))

    def _pick_ssh(self):
        p = self._pick_file(parent=self, title="ssh.exe 선택",
                            filetypes=[("ssh.exe", "ssh.exe"), ("실행 파일", "*.exe"), ("모든 파일", "*.*")])
        if not p:
            return
        self.ssh_searching = True
        self._update_ssh_row()

        probe = self._ssh_probe
        _background("ssh-probe", self._q, lambda: probe(p), lambda ok, v: ("ssh_manual", p, v if ok else None))

    def _set_ssh_tool(self, tool: SshTool | None, *, save: bool = True):
        self.ssh_tool = tool
        if tool is not None and save:
            try:
                save_ssh_tool(tool)  # 실행 파일 경로만 저장
            except OSError:
                pass

    def _toggle_ssh_detail(self):
        self._ssh_detail = not self._ssh_detail
        self._update_ssh_row()

    def _update_ssh_row(self):
        if self.step != 3 or not hasattr(self, "lbl_ssh"):
            return
        try:
            if self.ssh_searching:
                self.ssh_msg.set("SSH 연결 도구를 찾는 중...")
                self.lbl_ssh.configure(foreground="gray30")
            elif self.ssh_tool is not None:
                self.ssh_msg.set(f"✓ {self.ssh_tool.label}")
                self.lbl_ssh.configure(foreground="darkgreen")
            else:
                self.ssh_msg.set("✗ SSH 연결 도구를 찾지 못했습니다")
                self.lbl_ssh.configure(foreground="firebrick")
            if self.ssh_tool is not None:
                self.ssh_path_msg.set(f"{self.ssh_tool.path}\n{self.ssh_tool.version}")
            if self._ssh_detail and self.ssh_tool is not None:
                self.lbl_ssh_path.pack(anchor="w", pady=(4, 0))
            else:
                self.lbl_ssh_path.pack_forget()
            missing = self.ssh_tool is None and not self.ssh_searching
            if missing:
                self.btn_git_help.pack(anchor="w", pady=(4, 0))
                if not self.conn_msg.get() or self.conn_msg.get().startswith("✓"):
                    self.conn_msg.set(SSH_MISSING)
                    self.lbl_conn.configure(foreground="firebrick")
            else:
                self.btn_git_help.pack_forget()
                if self.conn_msg.get() in (SSH_MISSING, SSH_INVALID):
                    self.conn_msg.set("")
            busy = self.busy or self.ssh_searching
            self.btn_conn.configure(state="normal" if self.ssh_tool is not None and not busy else "disabled")
            self.btn_ssh_find.configure(state="disabled" if busy else "normal")
            self.btn_ssh_pick.configure(state="disabled" if busy else "normal")
        except tk.TclError:
            pass

    def _step4(self):
        self.step_title.set("STEP 4/4 · 무료 Cloud 자동 준비")
        ttk.Label(self.body, text="버튼 하나로 서버에 LIVE 프로그램을 설치합니다. (몇 분 걸릴 수 있습니다)",
                  wraplength=640).pack(anchor="w")
        self.btn_prep = ttk.Button(self.body, text="무료 Cloud 자동 준비", command=self._prepare)
        self.btn_prep.pack(anchor="w", pady=10)
        self.step_labels = []
        for i, name in enumerate(CloudClient.PREPARE_STEPS, 1):
            v = tk.StringVar(value=f"  {i}/6 {name}")
            ttk.Label(self.body, textvariable=v).pack(anchor="w")
            self.step_labels.append(v)
        self.lbl_prep = ttk.Label(self.body, textvariable=self.prep_msg, justify="left", wraplength=640,
                                  font="PLS.Strong")
        self.lbl_prep.pack(anchor="w", pady=(10, 0))
        ttk.Button(self.body, text="상세 보기", command=self._details).pack(anchor="e")
        if self.prepared:
            self._mark_done()

    # ---------- navigation ----------
    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def _go(self, step: int):
        if self.busy:
            return
        self.step = max(1, min(self.STEPS, step))
        self._render()
        if self.step == 3:
            self._start_ssh_discovery()  # 열 때마다 다시 찾기 (새로 설치한 도구도 인식)

    def _back(self):
        self._go(self.step - 1)

    def _next(self):
        if self.step == 4 and self.prepared:
            self._finish()
            return
        self._go(self.step + 1)

    # ---------- actions ----------
    def _pick_key(self):
        p = filedialog.askopenfilename(parent=self, title="SSH Private Key 선택",
                                       filetypes=[("Private Key", "*.key *.pem *"), ("모든 파일", "*.*")])
        if p:
            self.key_path.set(p)

    def _profile(self) -> CloudProfile:
        return CloudProfile(self.host.get(), self.user.get(), self.key_path.get()).validated()

    def _run(self, fn, tag):
        """fn은 Tk 객체(self)를 참조하지 않아야 한다 (CloudClient 메서드 등)."""
        if self.busy:
            return

        def result(ok, v):
            if ok:
                return (tag, True, v)
            if isinstance(v, (CloudError, CloudConfigError)):
                return (tag, False, str(v))
            return (tag, False, f"예상하지 못한 오류 ({type(v).__name__})")
        self._worker = _background(f"cloud-{tag}", self._q, fn, result)
        self._render_buttons()

    def _render_buttons(self):
        self.btn_back.configure(state="disabled" if self.busy else ("normal" if self.step > 1 else "disabled"))

    def _check_conn(self):
        try:
            profile = self._profile()
        except CloudConfigError as e:
            self.conn_msg.set(f"✗ {e}")
            self.lbl_conn.configure(foreground="firebrick")
            return
        self.client = self._client_factory(profile)
        self.conn_msg.set("연결 검사 중...")
        self.lbl_conn.configure(foreground="gray30")
        self.btn_conn.configure(state="disabled")
        self._run(self.client.check_connection, "conn")

    def _fix_key(self):
        if messagebox.askyesno("키 파일 권한", "SSH Key 파일을 '나만 읽기' 권한으로 바꿀까요?\n(Windows OpenSSH 요구사항)", parent=self):
            ok = fix_key_permissions(Path(self.key_path.get().strip().strip('"')))
            self.conn_msg.set("권한을 바꿨습니다. [연결 검사]를 다시 눌러 주세요." if ok else "권한을 바꾸지 못했습니다.")

    def _prepare(self):
        if self.client is None:
            self._go(3)
            return
        self.btn_prep.configure(state="disabled")
        self.prep_msg.set("준비 중...")
        client, q = self.client, self._q  # 스레드에 Tk 창(self)을 넘기지 않는다
        self._run(lambda: client.prepare(lambda i, n: q.put(("prep_step", i, n))), "prep")

    def _mark_done(self):
        for v in self.step_labels:
            v.set("✓" + v.get()[1:] if not v.get().startswith("✓") else v.get())
        self.prep_msg.set(f"✓ 무료 Cloud 준비 완료\n{FREE_UNSURE}")
        self.lbl_prep.configure(foreground="darkgreen")

    def _pump(self):
        if getattr(self, "_destroyed", False):
            return
        try:
            while True:
                tag, *rest = self._q.get_nowait()
                if tag == "ssh":
                    self.ssh_searching = False
                    self._set_ssh_tool(rest[0])
                    self._update_ssh_row()
                elif tag == "ssh_manual":
                    path, version = rest
                    self.ssh_searching = False
                    if version:
                        self._set_ssh_tool(SshTool(Path(path), "manual", version))
                        self.conn_msg.set("✓ SSH 연결 도구를 확인했습니다. [연결 검사]를 눌러 주세요.")
                        self.lbl_conn.configure(foreground="darkgreen")
                    else:
                        self.conn_msg.set(SSH_INVALID)
                        self.lbl_conn.configure(foreground="firebrick")
                    self._update_ssh_row()
                elif tag == "conn":
                    ok, payload = rest
                    self.connected = ok
                    if ok:
                        save_cloud_profile(self._profile())
                    self._render()
                    if ok:
                        self.conn_msg.set("✓ 서버 연결 성공 — [다음]을 눌러 주세요.")
                        self.lbl_conn.configure(foreground="darkgreen")
                    else:
                        self.conn_msg.set(f"✗ {payload}")
                        self.lbl_conn.configure(foreground="firebrick")
                        if "권한" in payload and "Key" in payload:
                            self.btn_keyfix.pack(side="left", padx=6)
                elif tag == "prep_step":
                    i, name = rest
                    for j, v in enumerate(self.step_labels, 1):
                        label = CloudClient.PREPARE_STEPS[j - 1]
                        v.set(f"{'✓' if j < i else '▶' if j == i else ' '} {j}/6 {label}")
                elif tag == "prep":
                    ok, payload = rest
                    self.prepared = ok
                    self._render()  # 성공이면 _step4가 완료 표시
                    if not ok:
                        self.prep_msg.set(f"✗ {payload}\n\n무료 Cloud를 사용할 수 없으면 LIVE 창에서 [내 PC에서 LIVE]를 선택하세요.")
                        self.lbl_prep.configure(foreground="firebrick")
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if self.winfo_exists():
            self.after(200, self._pump)

    def _details(self):
        d = tk.Toplevel(self)
        d.title("상세 보기 (고급)")
        d.geometry("760x420")
        txt = tk.Text(d, wrap="none", font=("Consolas", 9))
        txt.pack(fill="both", expand=True)
        lines = list(self.client.detail) if self.client else ["(아직 기록 없음)"]
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")

    def _finish(self):
        profile = load_cloud_profile()
        self.destroy()
        if self._on_done and profile:
            self._on_done(profile)

    def _close(self):
        if self.busy and not messagebox.askyesno("설정 중", "설정 작업이 진행 중입니다. 창을 닫을까요?\n(서버 작업은 끝까지 진행될 수 있습니다)", parent=self):
            return
        self.destroy()

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)  # 백그라운드 스레드의 GC가 Tcl을 건드리지 않도록 main thread에서 정리
