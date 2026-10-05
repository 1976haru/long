"""댓글 관리 창 — 채널별 새 댓글 확인, 검토 후 답글/안전형 자동답글, 첫 댓글 자동등록 상태.

- 모든 YouTube 호출은 백그라운드 스레드(CommentService)에서 하고, 이 창은 결과만 표시한다.
- 프로그램이 꺼져 있으면 댓글을 달 수 없다. 다시 켜면 밀린 첫 댓글/새 댓글을 확인한다 (화면에 명시).
"""
from __future__ import annotations

import queue
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Callable

from .cloud_setup_ui import _background
from .tooling import release_tk_variables
from .ui_scroll import ScrollFrame
from .youtube_comments import (
    C_DONE, C_EXCLUDED, C_SELF, DAILY_CAPS, OPEN_STATES, POSTED, REPLY_AUTO, REPLY_MODE_LABELS, CommentService,
    template_warnings, _ts,
)
from .youtube_usage import comment_usage_text
from .help_content import TOOLTIPS
from .help_ui import InfoTip, show_usage
from .ui_text import friendly_error, is_beginner

OFFLINE_NOTE = ("프로그램이 꺼져 있는 동안에는 댓글을 달 수 없습니다. 다시 켜면 밀린 첫 댓글을 확인해 등록하고 새 댓글을 가져옵니다. "
                "새 댓글은 프로그램이 켜져 있을 때 10분마다 확인합니다.")
STATE_COLORS = {"NEW": "#1d4fa8", "REVIEW_REQUIRED": "darkorange", "REPLIED": "darkgreen",
                "ALREADY_REPLIED_MANUALLY": "darkgreen", "HELD": "gray40", "COMMENTS_OFF": "gray40",
                "EXCLUDED": "gray45", "DONE": "gray45"}


def _local(iso: str) -> str:
    ts = _ts(iso)
    return datetime.fromtimestamp(ts).strftime("%m/%d %H:%M") if ts else "-"


class CommentManagerWindow(tk.Toplevel):
    def __init__(self, master, *, service: CommentService, profile_id: str = "",
                 ask_text: Callable | None = None, open_channels: Callable | None = None):
        super().__init__(master)
        self.title("댓글 관리")
        sh = self.winfo_screenheight()
        self.geometry(f"1040x{max(520, min(820, sh - 90))}")
        self.minsize(820, 480)
        self.service = service
        self.profiles = service.profiles
        self.store = service.store
        self._ask_text = ask_text
        self._open_channels = open_channels
        self.channel_win = None
        self._q: queue.Queue = queue.Queue()
        self._worker = None
        self._profile_ids: list[str] = []

        self.profile = tk.StringVar()
        self.reply_mode = tk.StringVar(value=REPLY_MODE_LABELS["review"])
        self.daily_cap = tk.StringVar(value="20")
        self.monitor = tk.BooleanVar(value=True)
        self.show_closed = tk.BooleanVar(value=False)
        self.exclude = tk.StringVar()
        self.counts_text = tk.StringVar()
        self.message = tk.StringVar()
        self.usage = tk.StringVar()
        self.warn_text = tk.StringVar()
        self.templates_open = False

        self._ui()
        self.refresh_profiles(profile_id)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(300, self._pump)

    # ================= 화면 =================
    def _ui(self):
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        root = ttk.Frame(self.scroll.body, padding=12)
        root.pack(fill="both", expand=True)
        hd = ttk.Frame(root); hd.pack(fill="x")
        ttk.Label(hd, text="댓글 관리", font=("Segoe UI", 15, "bold")).pack(side="left")
        ttk.Button(hd, text="? 사용법", command=lambda: show_usage(self, "comments")).pack(side="right")
        ttk.Label(root, text=OFFLINE_NOTE, foreground="gray30", wraplength=980, justify="left").pack(anchor="w", pady=(0, 6))
        # 댓글 권한 부족 → 쉬운 안내 + [채널 다시 연결] (필요할 때만 보임)
        self.reauth_frame = rf = ttk.Frame(root)
        self.reauth_text = tk.StringVar()
        ttk.Label(rf, textvariable=self.reauth_text, foreground="firebrick", wraplength=760, justify="left").pack(side="left")
        ttk.Button(rf, text="채널 다시 연결", command=self.reconnect).pack(side="right")
        self._reauth_anchor = ttk.Frame(root)
        self._reauth_anchor.pack(fill="x")

        top = ttk.Frame(root); top.pack(fill="x")
        ttk.Label(top, text="채널").pack(side="left")
        self.cb_profile = ttk.Combobox(top, textvariable=self.profile, state="readonly", width=34)
        self.cb_profile.pack(side="left", padx=(4, 12))
        self.cb_profile.bind("<<ComboboxSelected>>", lambda e: self._on_profile())
        ttk.Label(top, text="자동답글").pack(side="left")
        cb = ttk.Combobox(top, textvariable=self.reply_mode, state="readonly", width=16,
                          values=list(REPLY_MODE_LABELS.values()))
        cb.pack(side="left", padx=(4, 2))
        cb.bind("<<ComboboxSelected>>", lambda e: self.save_settings())
        InfoTip(top, TOOLTIPS["auto_reply"]).pack(side="left", padx=(0, 12))
        ttk.Label(top, text="하루 최대").pack(side="left")
        cc = ttk.Combobox(top, textvariable=self.daily_cap, state="readonly", width=4, values=[str(x) for x in DAILY_CAPS])
        cc.pack(side="left", padx=(4, 12))
        cc.bind("<<ComboboxSelected>>", lambda e: self.save_settings())
        self.btn_check = ttk.Button(top, text="지금 새 댓글 확인", command=self.check_now)
        self.btn_check.pack(side="right")
        op = ttk.Frame(root); op.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(op, text="새 댓글 자동 확인 (프로그램이 켜져 있을 때 10분마다)", variable=self.monitor,
                        command=self.save_settings).pack(side="left")
        ttk.Checkbutton(op, text="완료/제외된 댓글도 보기", variable=self.show_closed, command=self.refresh).pack(side="left", padx=12)
        ttk.Button(op, text="답글 문구·제외 키워드", command=self.toggle_templates).pack(side="right")
        ttk.Label(root, text="자동답글: " + TOOLTIPS["auto_reply"].replace("\n", " ") + " (기본: 검토 후 답글)",
                  foreground="gray30", wraplength=980, justify="left").pack(anchor="w", pady=(4, 0))
        self.lbl_counts = ttk.Label(root, textvariable=self.counts_text, font=("Segoe UI", 10, "bold"))
        self.lbl_counts.pack(anchor="w", pady=(6, 0))
        self.lbl_msg = ttk.Label(root, textvariable=self.message, wraplength=980, justify="left")
        self.lbl_msg.pack(anchor="w")

        self.tpl_frame = tf = ttk.LabelFrame(root, text="답글 문구 (한 줄에 하나 · 5개 이상 권장) / 자동답글 제외 키워드", padding=8)
        self.txt_templates = tk.Text(tf, height=7, wrap="word")
        self.txt_templates.pack(fill="x")
        er = ttk.Frame(tf); er.pack(fill="x", pady=(4, 0))
        ttk.Label(er, text="제외 키워드 (쉼표)").pack(side="left")
        ttk.Entry(er, textvariable=self.exclude).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(er, text="저장", command=self.save_settings).pack(side="left")
        ttk.Label(tf, textvariable=self.warn_text, foreground="darkorange", wraplength=960, justify="left").pack(anchor="w")
        self._tpl_anchor = ttk.Frame(root)
        self._tpl_anchor.pack(fill="x")

        cf = ttk.LabelFrame(root, text="시청자 댓글 (채널 전체 · 최신순)", padding=8)
        cf.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("when", "video", "author", "text", "state", "reply")
        self.tree = ttk.Treeview(cf, columns=cols, show="headings", height=10, selectmode="extended")
        for c, h, w in zip(cols, ("시각", "영상", "작성자", "댓글", "상태", "답글"), (90, 170, 120, 330, 150, 200)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center" if c == "when" else "w")
        for st, color in STATE_COLORS.items():
            self.tree.tag_configure(st, foreground=color)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.show_full())
        bb = ttk.Frame(cf); bb.pack(fill="x", pady=(4, 0))
        ttk.Button(bb, text="답글 작성", command=self.reply_selected).pack(side="left")
        ttk.Button(bb, text="추천 답글 사용", command=self.use_recommended).pack(side="left", padx=4)
        ttk.Button(bb, text="자동답글 제외", command=lambda: self.mark_selected(C_EXCLUDED)).pack(side="left")
        ttk.Button(bb, text="완료 처리", command=lambda: self.mark_selected(C_DONE)).pack(side="left", padx=4)
        ttk.Label(bb, text="더블클릭: 댓글 전체 보기", foreground="gray40").pack(side="right")

        ff = ttk.LabelFrame(root, text="첫 댓글 자동등록 (업로드한 영상)", padding=8)
        ff.pack(fill="x", pady=(8, 0))
        fcols = ("video", "publish", "state", "text")
        self.task_tree = ttk.Treeview(ff, columns=fcols, show="headings", height=4, selectmode="browse")
        for c, h, w in zip(fcols, ("영상", "공개 예정 (UTC)", "첫 댓글 상태", "댓글"), (240, 150, 260, 330)):
            self.task_tree.heading(c, text=h)
            self.task_tree.column(c, width=w, anchor="w")
        self.task_tree.pack(fill="x")
        tb = ttk.Frame(ff); tb.pack(fill="x", pady=(4, 0))
        ttk.Button(tb, text="지금 확인 (비공개 대기 포함)", command=self.check_tasks).pack(side="left")
        ttk.Button(tb, text="첫 댓글 취소", command=self.cancel_task).pack(side="left", padx=4)
        self.lbl_usage = ttk.Label(root, textvariable=self.usage, foreground="gray40", wraplength=980)
        self.apply_mode()

    def apply_mode(self) -> None:
        """초보자 모드에서는 API 사용량(기술 정보)을 숨긴다."""
        if is_beginner():
            self.lbl_usage.pack_forget()
        elif not self.lbl_usage.winfo_manager():
            self.lbl_usage.pack(anchor="w", pady=(6, 0))

    def toggle_templates(self):
        if self.templates_open:
            self.tpl_frame.pack_forget()
        else:
            self.tpl_frame.pack(fill="x", pady=(6, 0), before=self._tpl_anchor)
        self.templates_open = not self.templates_open

    def _say(self, text: str, color: str = "") -> None:
        self.message.set(text)
        self.lbl_msg.configure(foreground=color or "black")

    # ================= 채널 / 설정 =================
    def refresh_profiles(self, select: str = "") -> None:
        profiles = [p for p in self.profiles.all() if p.channel_id]
        self._profile_ids = [p.profile_id for p in profiles]
        self.cb_profile.configure(values=[p.label for p in profiles])
        pid = select if select in self._profile_ids else (self._profile_ids[0] if self._profile_ids else "")
        self.profile.set(next((p.label for p in profiles if p.profile_id == pid), ""))
        if not profiles:
            self._say("연결된 채널이 없습니다. ③ 예약 업로드 → [YouTube 채널 관리]에서 채널을 연결하세요.", "darkorange")
        self._on_profile()

    def selected_profile(self):
        values = list(self.cb_profile.cget("values") or ())
        label = self.profile.get()
        if label in values and values.index(label) < len(self._profile_ids):
            return self.profiles.get(self._profile_ids[values.index(label)])
        return None

    def select_profile(self, profile_id: str) -> None:
        p = self.profiles.get(profile_id)
        if p:
            self.profile.set(p.label)
            self._on_profile()

    def _on_profile(self) -> None:
        p = self.selected_profile()
        if p is None:
            self.refresh()
            return
        cs = self.store.settings_for(p)
        self.reply_mode.set(REPLY_MODE_LABELS[cs.reply_mode])
        self.daily_cap.set(str(cs.daily_cap))
        self.monitor.set(bool(cs.monitor))
        self.exclude.set(", ".join(cs.exclude_keywords))
        self.txt_templates.delete("1.0", "end")
        self.txt_templates.insert("1.0", "\n".join(cs.reply_templates))
        self.warn_text.set("\n".join(template_warnings(cs.reply_templates)))
        self._show_reauth(cs.needs_reauth)
        self.refresh()

    def _show_reauth(self, on: bool) -> None:
        if on:
            fe = friendly_error(reason="insufficientPermissions")
            self.reauth_text.set(f"⚠ {fe.problem}\n{fe.action}")
            if not self.reauth_frame.winfo_manager():
                self.reauth_frame.pack(fill="x", pady=(0, 6), before=self._reauth_anchor)
        elif self.reauth_frame.winfo_manager():
            self.reauth_frame.pack_forget()

    def reconnect(self):
        """[채널 다시 연결]: 채널 관리 창을 열고 '댓글 기능 권한도 함께 요청'이 켜진 상태로 그 채널을 선택."""
        p = self.selected_profile()
        if self._open_channels:
            win = self._open_channels()
        else:
            from .youtube_channels_ui import ChannelManagerWindow
            from .help_ui import ask_connect_guide
            win = ChannelManagerWindow(self, profiles=self.profiles, connect_guide=ask_connect_guide,
                                       on_change=lambda: self._on_profile())
        self.channel_win = win
        if p is not None and win is not None and win.tree.exists(p.profile_id):
            win.tree.selection_set(p.profile_id)
            win.selected_id = ""
            win._on_select()
        return win

    def save_settings(self) -> None:
        p = self.selected_profile()
        if p is None:
            return
        cs = self.store.settings_for(p)
        mode = next((k for k, v in REPLY_MODE_LABELS.items() if v == self.reply_mode.get()), "review")
        if mode == REPLY_AUTO and cs.reply_mode != REPLY_AUTO:
            if not messagebox.askyesno("자동답글 (안전형)", (
                    "감사·응원 같은 짧고 안전한 댓글에만 등록한 문구로 자동 답글을 답니다.\n"
                    "질문·링크·긴 댓글·제외 키워드는 '검토 필요'로 남깁니다.\n"
                    f"채널당 24시간 최대 {self.daily_cap.get()}개, 답글 사이 60초 이상.\n\n켤까요?"), parent=self):
                self.reply_mode.set(REPLY_MODE_LABELS[cs.reply_mode])
                return
        cs.reply_mode = mode
        cs.daily_cap = int(self.daily_cap.get())
        cs.monitor = bool(self.monitor.get())
        cs.reply_templates = [t.strip() for t in self.txt_templates.get("1.0", "end").splitlines() if t.strip()]
        cs.exclude_keywords = [k.strip() for k in self.exclude.get().replace("\n", ",").split(",") if k.strip()]
        self.store.save_settings(cs)
        warns = template_warnings(cs.reply_templates)
        self.warn_text.set("\n".join(warns))
        self._say(f"✓ '{p.alias}' 댓글 설정 저장 · 자동답글: {REPLY_MODE_LABELS[cs.reply_mode]}", "darkgreen")
        self.refresh()

    # ================= 목록 =================
    def refresh(self) -> None:
        p = self.selected_profile()
        pid = p.profile_id if p else None
        keep = set(self.tree.selection())
        for x in self.tree.get_children():
            self.tree.delete(x)
        recs = [r for r in self.store.records(pid) if r.status != C_SELF] if pid else []
        if not self.show_closed.get():
            recs = [r for r in recs if r.status not in (C_EXCLUDED, C_DONE)]
        for r in recs:
            self.tree.insert("", "end", iid=r.comment_id, tags=(r.status,), values=(
                _local(r.published_at), r.video_title or r.video_id, r.author, r.text.replace("\n", " ")[:120],
                r.label, (("자동: " if r.auto else "") + r.reply_text) if r.reply_text else (
                    f"추천: {r.recommended}" if r.status in OPEN_STATES and r.recommended else "")))
        sel = [k for k in keep if self.tree.exists(k)]
        if sel:
            self.tree.selection_set(sel)
        for x in self.task_tree.get_children():
            self.task_tree.delete(x)
        for t in self.store.tasks():
            if pid and t.profile_id != pid:
                continue
            self.task_tree.insert("", "end", iid=t.task_id, values=(
                t.title, t.publish_at_utc.replace("T", " ").replace("Z", "") if t.publish_at_utc else "즉시 공개",
                t.label + (f" · {t.error}" if t.error and t.status != POSTED else ""), t.text.replace("\n", " ")[:80]))
        c = self.service.counts(pid) if pid else {"new": 0, "auto_replied": 0, "review": 0, "waiting_first": 0}
        self.counts_text.set(f"새 댓글 {c['new']} · 자동답글 완료 {c['auto_replied']} · 검토 필요 {c['review']} · "
                             f"첫 댓글 대기 {c['waiting_first']}")
        self.usage.set(comment_usage_text(self.service.clock))
        self.btn_check.configure(state="disabled" if self.busy else "normal")

    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def _selected(self) -> list[str]:
        return list(self.tree.selection())

    def _run(self, name: str, fn, done_text: Callable[[object], str]) -> None:
        if self.busy:
            return

        def result(ok, v):
            if ok:
                return ("done", True, done_text(v))
            return ("done", False, str(v) if isinstance(v, (ValueError, RuntimeError)) else f"오류 ({type(v).__name__})")
        self._worker = _background(name, self._q, fn, result)
        self.btn_check.configure(state="disabled")

    def check_now(self) -> None:
        p = self.selected_profile()
        if p is None:
            return
        cs = self.store.settings_for(p)
        if not self.store.has_settings(p.profile_id):
            self.store.save_settings(cs)  # 이 채널 댓글 확인 시작 (설정 저장 = 감시 대상)
        svc = self.service
        self._say("새 댓글 확인 중…")

        def text(res):
            if res.error:
                raise RuntimeError(res.error)
            extra = f" · 자동답글 {res.auto_replied}개" if res.auto_replied else ""
            return f"✓ 새 댓글 {res.new}개 (검토 필요 {res.review}){extra}"
        self._run("comments-check", lambda: svc.poll_profile(p), text)

    def check_tasks(self) -> None:
        svc = self.service
        self._say("첫 댓글 작업 확인 중…")
        self._run("comments-tasks", lambda: (svc.sync_tasks(), svc.process_tasks(force=True))[1],
                  lambda n: f"✓ 첫 댓글 작업 {n}개 확인")

    def cancel_task(self) -> None:
        sel = self.task_tree.selection()
        if sel and messagebox.askyesno("첫 댓글 취소", "선택한 영상의 첫 댓글 자동등록을 취소할까요?", parent=self):
            self.service.cancel_task(sel[0])
            self.refresh()

    def _current(self):
        sel = self._selected()
        return self.store.record(sel[0]) if sel else None

    def reply_selected(self, text: str | None = None) -> None:
        rec = self._current()
        if rec is None:
            messagebox.showinfo("답글", "답글을 달 댓글을 선택하세요.", parent=self)
            return
        if text is None:
            text = (self._ask_text or ask_reply_text)(self, rec.text, rec.recommended)
            if not text:
                return
        recent = [r.reply_text for r in self.store.records(rec.profile_id) if r.reply_text][:5]
        if text.strip() in recent and not messagebox.askyesno(
                "같은 답글", "최근에 쓴 답글과 같은 문장입니다. 같은 답글을 반복하면 스팸처럼 보일 수 있습니다. 그래도 보낼까요?",
                parent=self):
            return
        svc, cid = self.service, rec.comment_id
        self._say("답글 보내는 중…")
        self._run("comments-reply", lambda: svc.reply(cid, text), lambda r: "✓ 답글을 달았습니다.")

    def use_recommended(self) -> None:
        rec = self._current()
        if rec is None or not rec.recommended:
            return
        if messagebox.askyesno("추천 답글", f"이 답글을 보낼까요?\n\n{rec.recommended}", parent=self):
            self.reply_selected(rec.recommended)

    def mark_selected(self, status: str) -> None:
        for cid in self._selected():
            self.service.mark(cid, status)
        self.refresh()

    def show_full(self) -> None:
        rec = self._current()
        if rec is None:
            return None
        d = tk.Toplevel(self)
        d.title(f"댓글 · {rec.author}")
        d.transient(self)
        t = tk.Text(d, height=12, width=70, wrap="word")
        t.insert("1.0", f"{rec.author} · {_local(rec.published_at)} · {rec.video_title or rec.video_id}\n\n{rec.text}"
                 + (f"\n\n── 답글 ──\n{rec.reply_text}" if rec.reply_text else ""))
        t.configure(state="disabled")
        t.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(d, text="닫기", command=d.destroy).pack(pady=(0, 8))
        self.full_win = d
        return d

    def _pump(self) -> None:
        if getattr(self, "_destroyed", False):
            return
        changed = False
        try:
            while True:
                tag, ok, payload = self._q.get_nowait()
                self._worker = None
                self._say(("" if ok else "✗ ") + str(payload), "darkgreen" if ok else "firebrick")
                changed = True
        except queue.Empty:
            pass
        try:
            while True:
                self.service.events.get_nowait()
                changed = True
        except queue.Empty:
            pass
        try:
            if changed:
                self.refresh()
            if self.winfo_exists():
                self.after(300, self._pump)
        except tk.TclError:
            return

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)


def ask_reply_text(parent, comment: str, suggested: str) -> str:
    """답글 입력 창 (추천 답글이 미리 들어 있음). 취소하면 ''."""
    out = {"text": ""}
    d = tk.Toplevel(parent)
    d.title("답글 작성")
    d.transient(parent)
    ttk.Label(d, text=comment[:400], wraplength=520, justify="left", padding=10).pack(anchor="w")
    t = tk.Text(d, height=5, width=64, wrap="word")
    t.insert("1.0", suggested or "")
    t.pack(fill="both", expand=True, padx=10)
    row = ttk.Frame(d, padding=10); row.pack(fill="x")

    def send():
        out["text"] = t.get("1.0", "end").strip()
        d.destroy()
    ttk.Button(row, text="보내기", command=send).pack(side="left")
    ttk.Button(row, text="취소", command=d.destroy).pack(side="right")
    try:
        d.grab_set()
    except tk.TclError:
        pass
    parent.wait_window(d)
    return out["text"]
