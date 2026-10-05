"""YouTube 채널 관리 창 (예약 업로드용 Channel Profile) — 한국·일본 등 여러 채널을 별칭으로 등록/연결.

- 채널마다 OAuth Client JSON 위치와 Google 연결(token은 프로필별 DPAPI 파일)을 따로 가진다.
- 화면에는 token/secret/세션 URL을 표시하지 않는다. Google 로그인은 시스템 브라우저에서 진행한다.
- Tk 위젯만 다루고 저장/연결 규칙은 youtube_accounts에 있다.
"""
from __future__ import annotations

import queue
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .cloud_setup_ui import _background
from .tooling import release_tk_variables
from .youtube_accounts import (
    ChannelProfile, ProfileError, ProfileStore, connect_profile, disconnect_profile, new_profile_id,
)
from .youtube_api import YouTubeApiError
from .youtube_batch import UploadTemplateStore, clone_profile
from .youtube_metadata import DEFAULT_CATEGORIES, LANGUAGES, PRIVACY_LABELS, TIMEZONES
from .youtube_comments import CommentStore
from .youtube_oauth import COMMENT_SCOPES, OAuthError
from .youtube_client_provider import BUNDLED_MARKER, has_bundled_client, resolve_client
from .help_content import TOOLTIPS
from .help_ui import InfoTip, show_oauth_help, show_usage
from .ui_scroll import ScrollFrame
from .ui_text import CONNECTING_TEXT, is_beginner


def _label(mapping: dict, key: str) -> str:
    return f"{mapping.get(key, key)} ({key})" if key else mapping.get(key, "")


def _key(mapping: dict, label: str) -> str:
    return next((k for k in mapping if _label(mapping, k) == label), label.strip())


class ChannelManagerWindow(tk.Toplevel):
    def __init__(self, master, *, profiles: ProfileStore | None = None, connect: Callable[..., ChannelProfile] = connect_profile,
                 open_browser: Callable[[str], object] = webbrowser.open, pick_file: Callable = filedialog.askopenfilename,
                 on_change: Callable[[], None] | None = None, templates: UploadTemplateStore | None = None,
                 connect_guide: Callable | None = None):
        super().__init__(master)
        self.title("YouTube 채널 관리")
        self.geometry(f"860x{max(520, min(660, self.winfo_screenheight() - 90))}")
        self.minsize(760, 520)
        self.transient(master)
        self.profiles = profiles or ProfileStore()
        self.templates = templates or UploadTemplateStore()
        self._template_ids: list[str] = []
        self.default_template = tk.StringVar()
        self.comment_scope = tk.BooleanVar(value=False)  # 댓글 권한 부족이 확인된 채널이면 자동으로 켜진다
        self._connect = connect
        self._connect_guide = connect_guide  # [Google 계정 연결] 전 '무슨 일이 일어나는지' 안내 (화면에서 열 때)
        self.show_advanced_info = not is_beginner()
        self._open_browser = open_browser
        self._pick_file = pick_file
        self._on_change = on_change
        self._q: queue.Queue = queue.Queue()
        self._worker = None
        self.selected_id = ""

        self.alias = tk.StringVar()
        self.language = tk.StringVar(value=_label(LANGUAGES, "ko"))
        self.timezone = tk.StringVar(value="Asia/Seoul")
        self.category = tk.StringVar(value=_label(DEFAULT_CATEGORIES, "10"))
        self.privacy = tk.StringVar(value=_label(PRIVACY_LABELS, "private"))
        self.made_for_kids = tk.BooleanVar(value=False)
        self.client_file = tk.StringVar()
        self.message = tk.StringVar(value="채널을 선택하거나 [＋ 새 채널]을 누르세요.")
        self.channel_text = tk.StringVar(value="연결 안 됨")

        self._ui()
        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(200, self._pump)

    # ---------- 화면 ----------
    def _ui(self):
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        root = ttk.Frame(self.scroll.body, padding=12)
        root.pack(fill="both", expand=True)
        hd = ttk.Frame(root); hd.pack(fill="x")
        ttk.Label(hd, text="YouTube 채널 관리", font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Button(hd, text="? 사용법", command=lambda: show_usage(self, "channels")).pack(side="right")
        ttk.Label(root, foreground="gray30", text=(
            "예약 업로드할 채널을 별칭으로 등록하고 채널마다 Google 계정을 연결하세요. "
            "업로드 직전마다 실제 채널을 다시 확인하고, 다르면 업로드하지 않습니다.")).pack(anchor="w", pady=(0, 8))

        cols = ("alias", "channel", "lang", "tz", "state")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=6, selectmode="browse")
        for c, h, w in zip(cols, ("별칭", "연결된 YouTube 채널", "언어", "시간대", "상태"), (190, 230, 90, 120, 110)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("alias", "channel") else "center")
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        tb = ttk.Frame(root); tb.pack(fill="x", pady=(4, 8))
        ttk.Button(tb, text="＋ 새 채널", command=self.new_profile).pack(side="left")
        self.btn_clone = ttk.Button(tb, text="복제 (설정만, 연결은 복제 안 함)", command=self.clone_selected)
        self.btn_clone.pack(side="left", padx=6)
        self.btn_delete = ttk.Button(tb, text="채널 삭제", command=self.delete_selected)
        self.btn_delete.pack(side="right")

        form = ttk.LabelFrame(root, text="채널 설정", padding=8)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        def row(r, text, widget):
            ttk.Label(form, text=text, width=14).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="ew", pady=2)
        row(0, "별칭", ttk.Entry(form, textvariable=self.alias))
        row(1, "언어", ttk.Combobox(form, textvariable=self.language, state="readonly",
                                   values=[_label(LANGUAGES, k) for k in LANGUAGES]))
        tzf = ttk.Frame(form)
        ttk.Combobox(tzf, textvariable=self.timezone, values=list(TIMEZONES)).pack(side="left", fill="x", expand=True)
        InfoTip(tzf, TOOLTIPS["timezone"]).pack(side="left", padx=4)
        row(2, "시간대", tzf)
        row(3, "카테고리", ttk.Combobox(form, textvariable=self.category,
                                     values=[_label(DEFAULT_CATEGORIES, k) for k in DEFAULT_CATEGORIES]))
        row(4, "기본 공개 상태", ttk.Combobox(form, textvariable=self.privacy, state="readonly",
                                        values=[_label(PRIVACY_LABELS, k) for k in PRIVACY_LABELS]))
        kf = ttk.Frame(form)
        ttk.Checkbutton(kf, text="아동용 영상 (기본값)", variable=self.made_for_kids).pack(side="left")
        InfoTip(kf, TOOLTIPS["kids"]).pack(side="left", padx=4)
        row(5, "", kf)
        cf = ttk.Frame(form)
        ttk.Entry(cf, textvariable=self.client_file).pack(side="left", fill="x", expand=True)
        ttk.Button(cf, text="Google 연결 파일 선택", command=self._pick).pack(side="left", padx=(4, 0))
        ttk.Button(cf, text="이 파일이 뭔가요?", command=lambda: show_oauth_help(self, on_pick=self._pick)).pack(
            side="left", padx=(4, 0))
        row(6, "Google 연결 파일", cf)
        stf = ttk.Frame(form)
        ttk.Label(stf, textvariable=self.channel_text, justify="left").pack(side="left")
        self.btn_adv_info = ttk.Button(stf, text="고급 정보 보기", command=self.toggle_advanced_info)
        self.btn_adv_info.pack(side="right")
        row(7, "연결 상태", stf)
        self.cb_default_template = ttk.Combobox(form, textvariable=self.default_template, state="readonly")
        row(8, "기본 업로드 템플릿", self.cb_default_template)

        act = ttk.Frame(root); act.pack(fill="x", pady=(8, 0))
        self.btn_save = ttk.Button(act, text="저장", command=self.save_form)
        self.btn_save.pack(side="left")
        self.btn_connect = ttk.Button(act, text="Google 계정 연결", command=self.start_connect)
        self.btn_connect.pack(side="left", padx=6)
        self.btn_disconnect = ttk.Button(act, text="연결 해제", command=self.disconnect_selected)
        self.btn_disconnect.pack(side="left")
        ttk.Checkbutton(act, text="댓글 기능 권한도 함께 요청", variable=self.comment_scope).pack(side="left", padx=8)
        ttk.Button(act, text="닫기", command=self.destroy).pack(side="right")
        self.lbl_msg = ttk.Label(root, textvariable=self.message, justify="left", wraplength=800)
        self.lbl_msg.pack(anchor="w", pady=(8, 0))
        ttk.Label(root, foreground="gray30", text=(
            "Google 연결 파일은 위치만 기억합니다. 연결 정보는 채널마다 따로 이 PC에 암호화해서 저장됩니다. "
            "비밀번호는 이 프로그램에 입력하지 않습니다."), wraplength=800).pack(anchor="w")

    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def _say(self, text: str, color: str = "") -> None:
        self.message.set(text)
        self.lbl_msg.configure(foreground=color or "black", font=("Segoe UI", 9))

    def status_text(self, p: ChannelProfile) -> str:
        """연결 확인 결과: 채널 이름·언어·시간대 (channel ID는 [고급 정보 보기]에서만)."""
        if not p.channel_id:
            return "연결 안 됨"
        text = (f"✓ 연결 완료 — 연결된 채널\n채널 이름: {p.channel_title}\n"
                f"언어: {LANGUAGES.get(p.language, p.language)}\n시간대: {p.timezone}")
        return text + (f"\nChannel ID: {p.channel_id}" if self.show_advanced_info else "")

    def toggle_advanced_info(self) -> None:
        self.show_advanced_info = not self.show_advanced_info
        self.btn_adv_info.configure(text="고급 정보 숨기기" if self.show_advanced_info else "고급 정보 보기")
        p = self.profiles.get(self.selected_id) if self.selected_id else None
        if p is not None:
            self.channel_text.set(self.status_text(p))

    def refresh(self, select: str | None = None) -> None:
        sel = self.selected_id if select is None else select
        for x in self.tree.get_children():
            self.tree.delete(x)
        for p in self.profiles.all():
            state = "✓ 연결됨" if self.profiles.is_connected(p) else "연결 안 됨"
            self.tree.insert("", "end", iid=p.profile_id, values=(
                p.alias, p.channel_title or "-", LANGUAGES.get(p.language, p.language), p.timezone, state))
        if sel and self.tree.exists(sel):
            self.tree.selection_set(sel)
            self.tree.see(sel)
        self._buttons()

    def _buttons(self) -> None:
        has = bool(self.selected_id and self.profiles.get(self.selected_id))
        st = "disabled" if self.busy else "normal"
        self.btn_save.configure(state=st)
        self.btn_connect.configure(state=st)
        for b in (self.btn_delete, self.btn_disconnect, self.btn_clone):
            b.configure(state="normal" if has and not self.busy else "disabled")

    def _load_templates(self, profile: ChannelProfile | None) -> None:
        own = self.templates.for_profile(profile.profile_id) if profile else []
        self._template_ids = [""] + [tid for tid, _ in own]
        names = ["(없음 · 채널 기본값)"] + [t.name for _, t in own]
        self.cb_default_template.configure(values=names)
        cur = profile.default_template_id if profile else ""
        self.default_template.set(names[self._template_ids.index(cur)] if cur in self._template_ids else names[0])

    def _template_choice(self) -> str:
        names = list(self.cb_default_template.cget("values") or ())
        v = self.default_template.get()
        return self._template_ids[names.index(v)] if v in names and names.index(v) < len(self._template_ids) else ""

    def _on_select(self) -> None:
        sel = self.tree.selection()
        if not sel or self.busy or sel[0] == self.selected_id:  # 저장/연결 후 refresh로 다시 선택될 때는 그대로
            return
        p = self.profiles.get(sel[0])
        if p is None:
            return
        self.selected_id = p.profile_id
        self.alias.set(p.alias)
        self.language.set(_label(LANGUAGES, p.language))
        self.timezone.set(p.timezone)
        self.category.set(_label(DEFAULT_CATEGORIES, p.category_id))
        self.privacy.set(_label(PRIVACY_LABELS, p.privacy))
        self.made_for_kids.set(bool(p.made_for_kids))
        self.client_file.set(p.client_file)
        self.channel_text.set(self.status_text(p))
        self._load_templates(p)
        needs = CommentStore().settings_for(p).needs_reauth
        self.comment_scope.set(bool(needs))
        self._say(f"'{p.alias}' 선택됨" + (" · ⚠ 댓글 권한 부족: [Google 계정 연결]로 다시 승인하세요." if needs else ""),
                  "darkorange" if needs else "")
        self._buttons()

    def new_profile(self) -> None:
        if self.busy:
            return
        self.selected_id = ""
        self.tree.selection_remove(*self.tree.selection())
        self.alias.set("")
        self.language.set(_label(LANGUAGES, "ko"))
        self.timezone.set("Asia/Seoul")
        self.category.set(_label(DEFAULT_CATEGORIES, "10"))
        self.privacy.set(_label(PRIVACY_LABELS, "private"))
        self.made_for_kids.set(False)
        self.client_file.set("")
        self.channel_text.set("연결 안 됨")
        self._load_templates(None)
        self._say("별칭(예: 🇰🇷 한국 시니어, 🇯🇵 CHILI LAB)과 언어/시간대를 입력하고 [저장]하세요.")
        self._buttons()

    def _pick(self) -> None:
        p = self._pick_file(parent=self, title="Google 연결 파일 선택", filetypes=[("JSON", "*.json"), ("모든 파일", "*.*")])
        if p:
            self.client_file.set(p)

    # ---------- 저장/삭제 ----------
    def _form_profile(self) -> ChannelProfile:
        cur = self.profiles.get(self.selected_id) if self.selected_id else None
        p = ChannelProfile(
            profile_id=cur.profile_id if cur else new_profile_id(), alias=self.alias.get(),
            channel_id=cur.channel_id if cur else "", channel_title=cur.channel_title if cur else "",
            language=_key(LANGUAGES, self.language.get()), timezone=self.timezone.get().strip(),
            category_id=_key(DEFAULT_CATEGORIES, self.category.get()), privacy=_key(PRIVACY_LABELS, self.privacy.get()),
            made_for_kids=bool(self.made_for_kids.get()),
            client_file=self.client_file.get().strip().strip('"') or (cur.client_file if cur else ""),
            default_template_id=self._template_choice() if cur else "")
        return p.validate()

    def clone_selected(self) -> ChannelProfile | None:
        """설정만 복제 → 새 별칭으로 바로 고칠 수 있게 선택. 채널 ID/이름, OAuth JSON, token은 복제하지 않는다."""
        src = self.profiles.get(self.selected_id) if self.selected_id else None
        if src is None or self.busy:
            return None
        names = {p.alias for p in self.profiles.all()}
        alias = f"{src.alias} 복사본"
        k = 2
        while alias in names:
            alias = f"{src.alias} 복사본 {k}"
            k += 1
        try:
            new = clone_profile(self.profiles, self.templates, src, alias)
        except (ProfileError, ValueError) as e:
            self._say(f"✗ {e}", "firebrick")
            return None
        self.refresh(new.profile_id)
        self._on_select()
        self._say(f"✓ '{src.alias}' 설정을 복제했습니다. 별칭/언어를 바꾸고 [저장] → [Google 계정 연결]을 하세요.", "darkgreen")
        self._changed()
        return new

    def save_form(self) -> ChannelProfile | None:
        if self.busy:
            return None
        try:
            p = self.profiles.save(self._form_profile())
        except (ProfileError, ValueError) as e:
            self._say(f"✗ {e}", "firebrick")
            return None
        self.selected_id = p.profile_id
        self.refresh(p.profile_id)
        self._say(f"✓ '{p.alias}' 저장됨", "darkgreen")
        self._changed()
        return p

    def delete_selected(self) -> None:
        p = self.profiles.get(self.selected_id) if self.selected_id else None
        if p is None or self.busy:
            return
        if not messagebox.askyesno("채널 삭제", f"'{p.alias}' YouTube 채널 등록과 저장된 연결 정보를 삭제할까요?\n"
                                   "(YouTube 채널/영상은 삭제되지 않습니다)", parent=self):
            return
        self.profiles.delete(p.profile_id)
        self.new_profile()
        self.refresh("")
        self._say(f"'{p.alias}' 삭제됨")
        self._changed()

    def disconnect_selected(self) -> None:
        p = self.profiles.get(self.selected_id) if self.selected_id else None
        if p is None or self.busy:
            return
        if not messagebox.askyesno("연결 해제", f"'{p.alias}' 채널의 Google 연결을 해제할까요?\n"
                                   "해제하면 다시 [Google 계정 연결]을 해야 예약 업로드·댓글을 쓸 수 있습니다.", parent=self):
            return
        disconnect_profile(self.profiles, p)
        self.channel_text.set("연결 안 됨")
        self.refresh(p.profile_id)
        self._say(f"'{p.alias}' 연결을 해제했습니다.")
        self._changed()

    # ---------- Google 연결 ----------
    def start_connect(self) -> None:
        if self.busy:
            return
        p = self.save_form()
        if p is None:
            return
        path = p.client_file or (BUNDLED_MARKER if has_bundled_client() else "")  # 배포용 기본 연결 정보
        try:
            resolve_client(path)
        except OAuthError as e:
            self._say(f"✗ Google 연결 파일: {e}", "firebrick")
            return
        if self._connect_guide is not None and not self._connect_guide(self):
            return
        self._say(f"{CONNECTING_TEXT}\n('{p.alias}' 채널 계정으로 로그인 → 채널 선택 → [허용], 최대 5분)", "#1d4fa8")
        self.lbl_msg.configure(font=("Segoe UI", 12, "bold"))
        profiles, connect, open_browser = self.profiles, self._connect, self._open_browser
        kw = {"scope": COMMENT_SCOPES} if self.comment_scope.get() else {}  # 기본은 기존 scope 그대로

        def work():
            got = connect(profiles, p, path, open_browser=open_browser, **kw)
            if kw:  # 댓글 권한까지 다시 승인됨 → 경고 해제
                store = CommentStore()
                cs = store.settings_for(got)
                if cs.needs_reauth:
                    cs.needs_reauth = False
                    store.save_settings(cs)
            return got

        def result(ok, v):
            if ok:
                return ("conn", True, v.profile_id)
            if isinstance(v, (ProfileError, OAuthError, YouTubeApiError)):
                return ("conn", False, str(v))
            return ("conn", False, f"연결 중 오류 ({type(v).__name__})")
        self._worker = _background("youtube-profile-connect", self._q, work, result)
        self._buttons()

    def _pump(self) -> None:
        if getattr(self, "_destroyed", False):
            return
        try:
            while True:
                tag, ok, payload = self._q.get_nowait()
                if tag != "conn":
                    continue
                self._worker = None
                if ok:
                    p = self.profiles.get(payload)
                    self.selected_id = payload
                    self.refresh(payload)
                    if p:
                        self.channel_text.set(self.status_text(p))
                        self._say(f"✓ 연결됨 — {p.alias} → {p.channel_title}", "darkgreen")
                    self._changed()
                else:
                    self._say(f"✗ {payload}", "firebrick")
                    self._buttons()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if self.winfo_exists():
            self.after(200, self._pump)

    def _changed(self) -> None:
        if self._on_change:
            self._on_change()

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)
