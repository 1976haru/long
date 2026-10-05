"""예약 업로드 미리보기 창 + LIVE 대역폭 확인 창.

미리보기: 대기열에 넣기 전에 채널(별칭 + 실제 YouTube 채널 이름 + 시간대), 영상/썸네일 개수, 예약 시각,
오류(있으면 추가 불가)와 경고(표시만)를 보여준다. 실제 YouTube 채널은 그 채널 연결로 channels.list를 한 번 확인한다.
"""
from __future__ import annotations

import queue
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable

from .cloud_setup_ui import _background
from .tooling import release_tk_variables
from .youtube_accounts import ChannelMismatchError
from .youtube_api import YouTubeApiError
from .youtube_batch import BatchPlan
from .youtube_oauth import OAuthError

LIVE_BANDWIDTH_MESSAGE = ("현재 이 PC에서 LIVE 송출 중입니다.\n"
                          "예약 업로드도 인터넷 업로드 대역폭을 사용합니다.\n\n"
                          "업로드가 LIVE 화질/끊김에 영향을 줄 수 있습니다. LIVE가 끝난 뒤 업로드하는 것을 권장합니다.")


def ask_bandwidth(parent) -> bool:
    """True = 그래도 업로드, False = 잠시 기다리기 (기본/권장, 창을 닫아도 기다리기)."""
    result = {"go": False}
    d = tk.Toplevel(parent)
    d.title("LIVE 송출 중")
    d.transient(parent)
    d.resizable(False, False)
    ttk.Label(d, text=LIVE_BANDWIDTH_MESSAGE, justify="left", padding=16, wraplength=440).pack()
    row = ttk.Frame(d, padding=(16, 0, 16, 16)); row.pack(fill="x")

    def done(go: bool):
        result["go"] = go
        d.destroy()
    b = ttk.Button(row, text="잠시 기다리기 (권장)", command=lambda: done(False))
    b.pack(side="left")
    ttk.Button(row, text="그래도 업로드", command=lambda: done(True)).pack(side="right")
    d.protocol("WM_DELETE_WINDOW", lambda: done(False))
    b.focus_set()
    try:
        d.grab_set()
    except tk.TclError:
        pass
    parent.wait_window(d)
    return result["go"]


class PreviewDialog(tk.Toplevel):
    def __init__(self, master, plan: BatchPlan, *, on_confirm: Callable[[BatchPlan], object],
                 verify: Callable[[], object] | None = None):
        super().__init__(master)
        self.title("예약 업로드 미리보기")
        self.geometry("900x620")
        self.minsize(720, 480)
        self.transient(master)
        self.plan = plan
        self._on_confirm = on_confirm
        self._q: queue.Queue = queue.Queue()
        self.verify_state = "pending" if verify else "skipped"  # pending | ok | mismatch | unknown | skipped
        self.verify_text = tk.StringVar(value="실제 YouTube 채널 확인 중…" if verify else "")
        self.extra_errors: list[str] = []
        self.extra_warnings: list[str] = []
        self.result = None

        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="대기열에 넣기 전에 확인하세요", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        info = ttk.Frame(root); info.pack(fill="x", pady=(6, 6))
        rows = (("채널", plan.alias), ("저장된 YouTube 채널", f"{plan.channel_title or '-'} ({plan.channel_id or '연결 안 됨'})"),
                ("시간대", plan.timezone), ("영상", f"{len(plan.items)}개"),
                ("썸네일", f"{plan.thumb_count}/{len(plan.items)}"))
        for r, (k, v) in enumerate(rows):
            ttk.Label(info, text=k, width=18).grid(row=r, column=0, sticky="w")
            ttk.Label(info, text=v, font=("Segoe UI", 10, "bold")).grid(row=r, column=1, sticky="w")
        ttk.Label(info, text="실제 YouTube", width=18).grid(row=len(rows), column=0, sticky="w")
        self.lbl_verify = ttk.Label(info, textvariable=self.verify_text, font=("Segoe UI", 10, "bold"))
        self.lbl_verify.grid(row=len(rows), column=1, sticky="w")

        cols = ("n", "when", "title", "thumb")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=9)
        for c, h, w in zip(cols, ("#", "공개 시각 (채널 시간대)", "제목", "썸네일"), (40, 190, 420, 190)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("title", "thumb") else "center")
        for it in plan.items:
            self.tree.insert("", "end", values=(it.n, it.local_text, it.title or Path(it.video_path).name, it.thumb_label))
        self.tree.pack(fill="both", expand=True)
        self.problems = tk.StringVar()
        self.lbl_problems = ttk.Label(root, textvariable=self.problems, justify="left", wraplength=860)
        self.lbl_problems.pack(anchor="w", pady=(6, 0))
        act = ttk.Frame(root); act.pack(fill="x", pady=(8, 0))
        self.btn_add = ttk.Button(act, text=f"{len(plan.items)}개 대기열에 추가", command=self.confirm)
        self.btn_add.pack(side="left")
        ttk.Button(act, text="취소", command=self.destroy).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._render()
        if verify:
            def result(ok, v):
                if ok:
                    return ("verify", "ok", f"{v.title} ✓ (방금 확인)")
                if isinstance(v, ChannelMismatchError):
                    return ("verify", "mismatch", str(v))
                if isinstance(v, (YouTubeApiError, OAuthError)):
                    return ("verify", "unknown", str(v))
                return ("verify", "unknown", f"확인 중 오류 ({type(v).__name__})")
            self._worker = _background("youtube-preview-verify", self._q, lambda: verify(), result)
            self.after(100, self._pump)

    @property
    def errors(self) -> list[str]:
        return self.plan.all_errors + self.extra_errors

    @property
    def warnings(self) -> list[str]:
        return self.plan.all_warnings + self.extra_warnings

    def _render(self) -> None:
        errs, warns = self.errors, self.warnings
        lines = [f"오류: {len(errs)}" + ("" if not errs else " — 고친 뒤 다시 미리보기 하세요")]
        lines += [f"  ✗ {e}" for e in errs[:8]]
        lines.append(f"경고: {len(warns)}")
        lines += [f"  ⚠ {w}" for w in warns[:8]]
        if len(warns) > 8:
            lines.append(f"  … 외 {len(warns) - 8}개")
        self.problems.set("\n".join(lines))
        self.lbl_problems.configure(foreground="firebrick" if errs else "black")
        ok = not errs and self.verify_state not in ("pending", "mismatch")
        self.btn_add.configure(state="normal" if ok else "disabled")

    def _pump(self) -> None:
        if getattr(self, "_destroyed", False):
            return
        try:
            tag, state, text = self._q.get_nowait()
        except queue.Empty:
            self.after(100, self._pump)
            return
        self.verify_state = state
        if state == "ok":
            self.verify_text.set(text)
            self.lbl_verify.configure(foreground="darkgreen")
        elif state == "mismatch":
            self.verify_text.set("✗ 채널 불일치 — 추가할 수 없습니다")
            self.lbl_verify.configure(foreground="firebrick")
            self.extra_errors.append(text)
        else:
            self.verify_text.set("지금 확인하지 못했습니다 (업로드 직전에 다시 확인합니다)")
            self.lbl_verify.configure(foreground="darkorange")
            self.extra_warnings.append(f"실제 채널 확인 실패: {text}")
        self._render()

    def confirm(self):
        if str(self.btn_add.cget("state")) == "disabled":
            return None
        self.result = self._on_confirm(self.plan)
        if self.result is not None:
            self.destroy()
        return self.result

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)
