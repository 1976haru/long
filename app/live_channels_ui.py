"""[채널 관리] — 여러 채널 동시 Cloud LIVE의 채널 Profile 추가/삭제, 채널별 YouTube(Google) 연결 지정.

로직/저장은 live_channels.py. Google 계정 연결(브라우저 로그인 → 실제 YouTube 채널 이름 확인)은
기존 YouTube 채널 관리 창(youtube_channels_ui.ChannelManagerWindow)을 그대로 쓴다.
이 창은 Stream Key/token을 표시하지 않는다 (저장 여부만).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from .cloud_model import DEFAULT_LIVE_PROFILE, MAX_CONCURRENT_LIVE
from .live_channels import ChannelError, LiveChannelStore, key_store_for, oauth_profile_for
from .ui_theme import ensure as ensure_theme

NO_LINK = "(연결 안 함)"


class LiveChannelsDialog(tk.Toplevel):
    def __init__(self, master, *, store: LiveChannelStore | None = None, on_change: Callable[[], None] | None = None,
                 is_live: Callable[[str], bool] = lambda pid: False, profiles=None):
        super().__init__(master)
        ensure_theme(self)
        self.title("LIVE 채널 관리")
        self.geometry("760x520")
        self.minsize(640, 420)
        self.transient(master)
        self.store = store or LiveChannelStore()
        self._on_change = on_change or (lambda: None)
        self._is_live = is_live
        self._profiles = profiles
        self.new_name = tk.StringVar()
        self.new_id = tk.StringVar()
        self.link = tk.StringVar()
        self.message = tk.StringVar()
        self._link_ids: list[str] = []
        self._ui()
        self.refresh()

    def profiles(self):
        if self._profiles is None:
            from .youtube_accounts import ProfileStore
            self._profiles = ProfileStore()
        return self._profiles

    def _ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="LIVE 채널", font="PLS.Title").pack(anchor="w")
        ttk.Label(root, foreground="gray30", justify="left", wraplength=720, text=(
            f"채널마다 영상 Playlist · Stream Key · YouTube 연결이 따로 저장됩니다. "
            f"무료 Cloud에서는 동시에 최대 {MAX_CONCURRENT_LIVE}채널까지 송출합니다.\n"
            "Stream Key만 있으면 Google 연결 없이도 채널별 LIVE가 됩니다 (예약 LIVE에는 Google 연결 필요).")
        ).pack(anchor="w", pady=(0, 8))
        cols = ("name", "id", "youtube", "key", "live")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=8, selectmode="browse")
        for c, h, wd in zip(cols, ("채널 이름", "채널 ID", "YouTube 연결 (실제 채널)", "Stream Key", "Cloud"),
                            (170, 110, 230, 90, 80)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=wd, anchor="w" if c in ("name", "youtube") else "center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        add = ttk.LabelFrame(root, text="채널 추가", padding=8)
        add.pack(fill="x", pady=(8, 0))
        ttk.Label(add, text="이름").pack(side="left")
        ttk.Entry(add, textvariable=self.new_name, width=22).pack(side="left", padx=(4, 10))
        ttk.Label(add, text="ID (영문, 선택)").pack(side="left")
        ttk.Entry(add, textvariable=self.new_id, width=14).pack(side="left", padx=(4, 10))
        ttk.Button(add, text="＋ 채널 추가", style="Primary.TButton", command=self.add).pack(side="left")

        link = ttk.LabelFrame(root, text="선택한 채널의 YouTube 연결", padding=8)
        link.pack(fill="x", pady=(8, 0))
        self.cmb_link = ttk.Combobox(link, textvariable=self.link, state="readonly", width=40)
        self.cmb_link.pack(side="left")
        ttk.Button(link, text="적용", command=self.apply_link).pack(side="left", padx=(5, 0))
        ttk.Button(link, text="Google 계정 연결…", command=self.open_google).pack(side="left", padx=(5, 0))

        bar = ttk.Frame(root)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Label(bar, textvariable=self.message, wraplength=560, justify="left").pack(side="left")
        ttk.Button(bar, text="닫기", command=self.destroy).pack(side="right")
        self.btn_delete = ttk.Button(bar, text="선택 채널 삭제", command=self.delete_selected)
        self.btn_delete.pack(side="right", padx=(0, 5))

    # ---------------- data ----------------
    def _say(self, text: str, color: str = "") -> None:
        self.message.set(text)

    @staticmethod
    def _key_saved(store_id: str) -> bool:
        """저장 여부만 (복호화하지 않음, 값은 화면에 표시하지 않는다)."""
        try:
            ks = key_store_for(store_id)
            has = getattr(ks, "has_saved", None)
            return bool(has()) if has else ks.get() is not None
        except Exception:
            return False

    def selected(self) -> str:
        sel = self.tree.selection()
        return sel[0] if sel else ""

    def refresh(self) -> None:
        sel = self.selected()
        for x in self.tree.get_children():
            self.tree.delete(x)
        profiles = self.profiles()
        for p in self.store.all():
            op = oauth_profile_for(p, profiles)
            if op is not None:
                yt = (op.channel_title or op.alias) + ("" if profiles.is_connected(op) else " (연결 끊김)")
            else:
                yt = "기존 LIVE 연결 사용" if p.is_default else "-"
            key = "기존 저장 방식" if p.is_default else ("저장됨" if self._key_saved(p.key_store_id) else "없음")
            self.tree.insert("", "end", iid=p.channel_profile_id, values=(
                p.display_name, p.channel_profile_id, yt, key, "● LIVE" if self._is_live(p.channel_profile_id) else "-"))
        if sel and self.tree.exists(sel):
            self.tree.selection_set(sel)
        accounts = profiles.all()
        self._link_ids = [""] + [a.profile_id for a in accounts]
        self.cmb_link.configure(values=[NO_LINK] + [
            f"{a.alias} → {a.channel_title}" if a.channel_title else f"{a.alias} (연결 안 됨)" for a in accounts])
        self._on_select()

    def _on_select(self) -> None:
        pid = self.selected()
        p = self.store.get(pid) if pid else None
        self.btn_delete.configure(state="normal" if p is not None and not p.is_default else "disabled")
        if p is None:
            self.link.set("")
            return
        idx = self._link_ids.index(p.oauth_profile_id) if p.oauth_profile_id in self._link_ids else 0
        self.cmb_link.current(idx)

    # ---------------- actions ----------------
    def add(self) -> bool:
        try:
            p = self.store.add(self.new_name.get(), profile_id=self.new_id.get().strip() or None)
        except (ChannelError, ValueError) as e:
            messagebox.showwarning("채널 추가", str(e), parent=self)
            return False
        self.new_name.set("")
        self.new_id.set("")
        self.refresh()
        self.tree.selection_set(p.channel_profile_id)
        self._say(f"✓ '{p.display_name}' 채널을 추가했습니다. LIVE 창에서 채널을 고르고 영상/Stream Key를 넣으세요.")
        self._on_change()
        return True

    def apply_link(self) -> bool:
        pid = self.selected()
        p = self.store.get(pid) if pid else None
        i = self.cmb_link.current()
        if p is None or i < 0:
            return False
        op_id = self._link_ids[i] if i < len(self._link_ids) else ""
        op = self.profiles().get(op_id) if op_id else None
        if op is not None:
            if not op.channel_id:
                messagebox.showwarning("YouTube 연결", "이 Google 연결은 아직 YouTube 채널과 연결되지 않았습니다.\n"
                                       "[Google 계정 연결…]에서 먼저 연결하세요.", parent=self)
                return False
            # 실제 YouTube 채널 이름을 사용자가 확인하고 승인
            if not messagebox.askyesno("YouTube 연결 확인", f"'{p.display_name}' LIVE 채널을\n"
                                       f"실제 YouTube 채널 '{op.channel_title}' 에 연결할까요?", parent=self):
                return False
        p.oauth_profile_id = op_id
        p.youtube_channel_id = op.channel_id if op is not None else ""
        try:
            self.store.save(p)
        except (ChannelError, ValueError) as e:
            messagebox.showwarning("YouTube 연결", str(e), parent=self)
            return False
        self.refresh()
        self._say("✓ YouTube 연결을 저장했습니다." if op else "YouTube 연결을 해제했습니다 (Google 연결 정보는 그대로).")
        self._on_change()
        return True

    def delete_selected(self) -> bool:
        pid = self.selected()
        p = self.store.get(pid) if pid else None
        if p is None or p.channel_profile_id == DEFAULT_LIVE_PROFILE:
            return False
        if self._is_live(pid):
            messagebox.showwarning("채널 삭제", "Cloud에서 LIVE 중인 채널은 삭제할 수 없습니다. 먼저 LIVE를 종료하세요.", parent=self)
            return False
        if not messagebox.askyesno("채널 삭제", f"'{p.display_name}' 채널과 이 PC에 저장된 이 채널의 Stream Key를 삭제할까요?\n"
                                   "(다른 채널과 Google 연결 정보, YouTube 방송은 그대로입니다)", parent=self):
            return False
        self.store.delete(pid)
        self.refresh()
        self._say(f"'{p.display_name}' 채널을 삭제했습니다.")
        self._on_change()
        return True

    def open_google(self):
        from .youtube_channels_ui import ChannelManagerWindow
        ChannelManagerWindow(self, profiles=self.profiles(), on_change=self.refresh)
