"""LIVE 창 ⑦ YouTube 방송 정보 (채널별) — 제목·설명·태그·썸네일·카테고리·YouTube 재생목록·공개 상태 (Tk).

계산/저장/적용은 youtube_metadata_control.py (Tk 비의존). 이 파일은 화면과 백그라운드 작업 연결만 한다.
- [기본값 저장]: 이 PC(settings.json)에만 저장. YouTube에는 아무것도 쓰지 않는다.
- [YouTube에 적용]: YouTube 연결이 있을 때만. 계정 채널 확인 → 진행 중/예정 방송 목록 → 사용자가 방송을 직접 고르고
  [적용]을 눌러야 그 방송 하나에만 적용한다 (방송을 추측해서 바꾸지 않는다).
- Stream Key/token은 어디에도 표시하지 않는다. 작업 스레드는 Tk를 만지지 않고 큐로만 결과를 넘긴다.
"""
from __future__ import annotations

import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .tooling import release_tk_variables
from .ui_theme import ensure as ensure_theme
from .youtube_metadata import MetadataError, PRIVACY_LABELS, validate_thumbnail
from .youtube_metadata_control import (
    LOCAL_ONLY_TEXT, NO_PLAYLIST, SAVED_TEXT, LiveMetadata, apply_metadata_steps, broadcast_label, cached_categories,
    category_id_for, category_label, change_lines, description_count_text, fetch_categories, list_target_broadcasts,
    load_metadata, playlist_choices, privacy_label, privacy_value, save_metadata, title_count_text,
)

PANEL_TITLE = "⑦ YouTube 방송 정보 (채널별 — 제목·설명·태그·썸네일·카테고리·YouTube 재생목록·공개 상태)"
PLAYLIST_NOTE = ("YouTube 재생목록 = YouTube 채널 안에서 방송을 모아 두는 목록입니다. "
                 "위 ①의 '송출 영상 Playlist'(Cloud에서 반복할 MP4 목록)와 다릅니다.")
NO_TARGET_TEXT = ("적용할 방송이 없습니다.\nYouTube Studio에서 LIVE를 시작하거나 예약한 뒤 다시 누르세요.\n"
                  "(방송 정보는 이 PC에 저장되어 있습니다.)")
POLL_MS = 100


def _split_tags(text: str) -> list[str]:
    return [t for t in re.split(r"[,\n]", text or "")]


class MetadataPanel:
    """LIVE 창(w)에 붙는 채널별 방송 정보 칸. w.channel_id가 바뀌면 그 채널 값으로 바뀐다 (입력 중이던 값은 채널별로 보관)."""

    def __init__(self, w, root):
        self.w = w
        self.pid: str | None = None
        self._drafts: dict[str, LiveMetadata] = {}  # 채널별 저장 전 입력 (채널을 바꿔도 섞이지 않게)
        self._playlists: dict[str, list] = {}  # 채널별로 받은 YouTube 재생목록
        self._last: dict[str, dict] = {}  # 채널별 마지막 적용 {"api","md","result","channel_id"}
        self._categories = cached_categories()
        self._q: queue.Queue = queue.Queue()
        self._poll_job = None
        self._busy = False
        self.on_choose_target = None  # 테스트 교체용: (channel_title, broadcasts, changes) → broadcast | None
        self.title_var = tk.StringVar()
        self.title_count = tk.StringVar(value=title_count_text(""))
        self.desc_count = tk.StringVar(value=description_count_text(""))
        self.thumb_var = tk.StringVar()
        self.thumb_note = tk.StringVar(value="선택 안 함")
        self.category_var = tk.StringVar()
        self.playlist_var = tk.StringVar()
        self.privacy_var = tk.StringVar(value=privacy_label("unlisted"))
        self.mode_text = tk.StringVar(value=LOCAL_ONLY_TEXT)
        self.result_text = tk.StringVar()
        self._category_id = "10"
        self._pl_choices: list[tuple[str, str]] = [("", NO_PLAYLIST)]

        f = self.frame = ttk.LabelFrame(root, text=PANEL_TITLE, padding=7)
        f.pack(fill="x", pady=(8, 0))
        self.lbl_mode = ttk.Label(f, textvariable=self.mode_text, justify="left", wraplength=780, foreground="gray25")
        self.lbl_mode.pack(anchor="w", pady=(0, 4))
        r = ttk.Frame(f); r.pack(fill="x", pady=1)
        ttk.Label(r, text="방송 제목", width=14).pack(side="left")
        self.ent_title = ttk.Entry(r, textvariable=self.title_var)
        self.ent_title.pack(side="left", fill="x", expand=True)
        ttk.Label(r, textvariable=self.title_count, width=10).pack(side="left", padx=(5, 0))
        self.title_var.trace_add("write", lambda *a: self.title_count.set(title_count_text(self.title_var.get())))
        r = ttk.Frame(f); r.pack(fill="x", pady=1)
        ttk.Label(r, text="설명", width=14).pack(side="left", anchor="n")
        self.txt_desc = tk.Text(r, height=4, wrap="word", undo=True)
        self.txt_desc.pack(side="left", fill="x", expand=True)
        ttk.Label(r, textvariable=self.desc_count, width=10).pack(side="left", padx=(5, 0), anchor="n")
        self.txt_desc.bind("<KeyRelease>", lambda e: self._update_counts())
        r = ttk.Frame(f); r.pack(fill="x", pady=1)
        ttk.Label(r, text="태그", width=14).pack(side="left", anchor="n")
        self.txt_tags = tk.Text(r, height=2, wrap="word", undo=True)
        self.txt_tags.pack(side="left", fill="x", expand=True)
        ttk.Label(f, text="태그는 쉼표(,) 또는 줄바꿈으로 나눕니다. 예: Tokyo Chill, R&B, Playlist  (빈 값·중복은 자동 정리)",
                  foreground="gray30").pack(anchor="w", padx=(4, 0))
        r = ttk.Frame(f); r.pack(fill="x", pady=(4, 1))
        ttk.Label(r, text="썸네일", width=14).pack(side="left")
        self.btn_thumb = ttk.Button(r, text="썸네일 선택", command=self.pick_thumbnail)
        self.btn_thumb.pack(side="left")
        self.btn_thumb_clear = ttk.Button(r, text="지우기", command=self.clear_thumbnail)
        self.btn_thumb_clear.pack(side="left", padx=(5, 0))
        ttk.Label(r, textvariable=self.thumb_note).pack(side="left", padx=(8, 0))
        r = ttk.Frame(f); r.pack(fill="x", pady=1)
        ttk.Label(r, text="카테고리", width=14).pack(side="left")
        self.cmb_category = ttk.Combobox(r, textvariable=self.category_var, state="readonly", width=24)
        self.cmb_category.pack(side="left")
        self.cmb_category.bind("<<ComboboxSelected>>", lambda e: self._on_category())
        ttk.Label(r, text="공개 상태", width=9).pack(side="left", padx=(16, 0))
        self.cmb_privacy = ttk.Combobox(r, textvariable=self.privacy_var, state="readonly", width=10,
                                        values=list(PRIVACY_LABELS.values()))
        self.cmb_privacy.pack(side="left")
        r = ttk.Frame(f); r.pack(fill="x", pady=1)
        ttk.Label(r, text="YouTube 재생목록", width=14).pack(side="left")
        self.cmb_playlist = ttk.Combobox(r, textvariable=self.playlist_var, state="readonly", width=34)
        self.cmb_playlist.pack(side="left")
        self.btn_refresh = ttk.Button(r, text="YouTube에서 목록 새로고침", command=self.refresh_lists)
        self.btn_refresh.pack(side="left", padx=(5, 0))
        ttk.Label(f, text=PLAYLIST_NOTE, foreground="gray30", wraplength=780, justify="left").pack(anchor="w", padx=(4, 0))
        b = ttk.Frame(f); b.pack(fill="x", pady=(6, 0))
        self.btn_save = ttk.Button(b, text="기본값 저장 (이 PC에만)", command=self.save)
        self.btn_save.pack(side="left")
        self.btn_apply = ttk.Button(b, text="YouTube에 적용…", command=self.apply)
        self.btn_apply.pack(side="left", padx=(8, 0))
        self.btn_retry = ttk.Button(b, text="실패한 항목만 다시 적용", command=self.retry_failed)
        self.lbl_result = ttk.Label(f, textvariable=self.result_text, justify="left", wraplength=780)
        self.lbl_result.pack(anchor="w", pady=(4, 0))
        self._set_category_values()

    # ---------- 값 ↔ 화면 ----------
    def _update_counts(self):
        self.desc_count.set(description_count_text(self.txt_desc.get("1.0", "end-1c")))

    def _set_category_values(self):
        labels = [t for _, t in self._categories]
        if self._category_id not in dict(self._categories):
            labels.append(category_label(self._category_id, self._categories))
        self.cmb_category.configure(values=labels)
        self.category_var.set(category_label(self._category_id, self._categories))

    def _on_category(self):
        self._category_id = category_id_for(self.category_var.get(), self._categories, self._category_id)

    def _set_playlist_values(self, md: LiveMetadata):
        fetched = self._playlists.get(self.pid)
        if fetched is None:  # 아직 YouTube에서 받지 않음: 저장된 이름 그대로
            self._pl_choices = [("", NO_PLAYLIST)] + (
                [(md.youtube_playlist_id, md.youtube_playlist_title or md.youtube_playlist_id)]
                if md.youtube_playlist_id else [])
        else:
            self._pl_choices = playlist_choices(fetched, md.youtube_playlist_id, md.youtube_playlist_title)
        self.cmb_playlist.configure(values=[t for _, t in self._pl_choices])
        self.playlist_var.set(next((t for i, t in self._pl_choices if i == md.youtube_playlist_id), NO_PLAYLIST))

    def fill(self, md: LiveMetadata) -> None:
        self.title_var.set(md.title)
        self.txt_desc.delete("1.0", "end")
        self.txt_desc.insert("1.0", md.description)
        self.txt_tags.delete("1.0", "end")
        self.txt_tags.insert("1.0", ", ".join(md.tags))
        self.thumb_var.set(md.thumbnail_path)
        self._thumb_info()
        self._category_id = str(md.category_id)
        self._set_category_values()
        self.privacy_var.set(privacy_label(md.privacy))
        self._set_playlist_values(md)
        self._update_counts()

    def collect(self) -> LiveMetadata:
        """화면 값 (검증 전). 카테고리/재생목록은 표시명이 아니라 ID로."""
        label = self.playlist_var.get()
        pl_id, pl_title = next(((i, t) for i, t in self._pl_choices if t == label), ("", ""))
        if pl_title.endswith(" (저장됨)"):
            pl_title = pl_title[: -len(" (저장됨)")]
        return LiveMetadata(title=self.title_var.get(), description=self.txt_desc.get("1.0", "end-1c"),
                            tags=_split_tags(self.txt_tags.get("1.0", "end-1c")), thumbnail_path=self.thumb_var.get(),
                            category_id=self._category_id, youtube_playlist_id=pl_id,
                            youtube_playlist_title=pl_title if pl_id else "", privacy=privacy_value(self.privacy_var.get()))

    def _thumb_info(self):
        p = self.thumb_var.get().strip()
        if not p:
            self.thumb_note.set("선택 안 함")
            return
        name = p.replace("\\", "/").rsplit("/", 1)[-1]
        try:
            info = validate_thumbnail(p)
            self.thumb_note.set(f"{name} · {info.width}×{info.height}" + (f" · {info.note}" if info.note else ""))
        except MetadataError as e:
            self.thumb_note.set(f"⚠ {name} — {e}")

    def pick_thumbnail(self):
        p = filedialog.askopenfilename(parent=self.w, title="썸네일 선택 (JPG/PNG)",
                                       filetypes=[("이미지", "*.jpg *.jpeg *.png"), ("모든 파일", "*.*")])
        if not p:
            return
        try:
            validate_thumbnail(p)
        except MetadataError as e:
            messagebox.showwarning("썸네일", str(e), parent=self.w)
            return
        self.thumb_var.set(p)
        self._thumb_info()

    def clear_thumbnail(self):
        self.thumb_var.set("")
        self._thumb_info()

    # ---------- 채널 전환 ----------
    def show_channel(self, pid: str) -> None:
        if pid == self.pid:
            return
        if self.pid is not None:
            self._drafts[self.pid] = self.collect()
        self.pid = pid
        self.fill(self._drafts.get(pid) or load_metadata(pid))
        self._show_result()

    def sync(self) -> None:
        """LIVE 창 상태(채널/YouTube 연결/송출 방식)에 맞춰 문구와 버튼 상태를 맞춘다."""
        if self.w.channel_id != self.pid:
            self.show_channel(self.w.channel_id)
        connected = bool(getattr(self.w, "_yt_ok", False))
        if not connected:
            text = LOCAL_ONLY_TEXT
        else:
            who = self.w._yt_channel_title() or "연결된 채널"
            text = (f"YouTube 연결됨 · 적용 대상 YouTube 채널: {who}\n"
                    + ("YouTube API 자동 세션: LIVE를 시작하면 이 방송 정보로 새 방송을 만듭니다 "
                       "(제목이 비어 있으면 ③의 LIVE 제목 사용)." if self.w.api_mode else
                       "Stream Key 직접 송출: [YouTube에 적용…]을 누르면 적용할 방송을 직접 고릅니다. "
                       "자동으로 방송을 바꾸지 않습니다."))
        if self.mode_text.get() != text:
            self.mode_text.set(text)
        state = "normal" if connected and not self._busy else "disabled"
        for btn in (self.btn_apply, self.btn_refresh):
            if str(btn.cget("state")) != state:
                btn.configure(state=state)

    # ---------- 저장 (로컬만) ----------
    def save(self) -> bool:
        try:
            md = save_metadata(self.pid, self.collect())
        except (MetadataError, ValueError) as e:
            messagebox.showerror("방송 정보 저장", str(e), parent=self.w)
            return False
        self._drafts.pop(self.pid, None)
        self.fill(md)
        self.result_text.set(SAVED_TEXT if not getattr(self.w, "_yt_ok", False)
                             else "✓ 이 채널의 기본값으로 저장했습니다 (YouTube에는 아직 적용하지 않음).")
        return True

    def session_metadata(self) -> LiveMetadata | None:
        """API 자동 세션 시작용: 제목이 있으면 검증한 값, 비어 있으면 None (③의 기존 LIVE 제목 사용)."""
        md = self.collect()
        if not md.title.strip():
            return None
        return md.validate(check_thumbnail=False)

    # ---------- YouTube (백그라운드) ----------
    def _run(self, kind: str, pid: str, fn, ctx=None) -> None:
        self._busy = True
        self.sync()
        q = self._q

        def work():
            try:
                q.put((kind, pid, True, fn(), ctx))
            except Exception as e:  # noqa: BLE001 — 문구만 넘긴다 (token 없음)
                msg = str(e) if isinstance(e, (MetadataError,)) or e.__class__.__module__.startswith("app.") \
                    else f"YouTube 작업 오류 ({type(e).__name__})"
                q.put((kind, pid, False, msg, ctx))
        threading.Thread(target=work, name=f"live-metadata-{kind}", daemon=True).start()
        self._schedule_poll()

    def _schedule_poll(self):
        if self._poll_job is None:
            self._poll_job = self.w.after(POLL_MS, self._poll)

    def _poll(self):
        self._poll_job = None
        try:
            while True:
                self._handle(*self._q.get_nowait())
        except queue.Empty:
            pass
        if self._busy:
            self._schedule_poll()

    def wait_idle(self, timeout: float = 10.0) -> None:
        """테스트용: 백그라운드 작업이 끝날 때까지 처리."""
        import time
        end = time.time() + timeout
        while self._busy and time.time() < end:
            self.w.update()
            try:
                self._handle(*self._q.get(timeout=0.05))
            except queue.Empty:
                pass

    def _api(self):
        return self.w._yt_api()

    def refresh_lists(self) -> None:
        if not getattr(self.w, "_yt_ok", False) or self._busy:
            return
        try:
            api = self._api()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("YouTube", str(e) if e.__class__.__module__.startswith("app.") else
                                 "YouTube 연결을 확인하세요.", parent=self.w)
            return
        self._run("lists", self.pid, lambda: (fetch_categories(api), api.list_playlists()))

    def apply(self) -> None:
        if self._busy:
            return
        try:
            md = self.collect().validate(check_thumbnail=False)
        except MetadataError as e:
            messagebox.showerror("YouTube에 적용", str(e), parent=self.w)
            return
        if not getattr(self.w, "_yt_ok", False):  # 연결 없음: YouTube에 쓰지 않는다
            messagebox.showinfo("YouTube 방송 정보", LOCAL_ONLY_TEXT, parent=self.w)
            return
        try:
            api = self._api()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("YouTube", str(e) if e.__class__.__module__.startswith("app.") else
                                 "YouTube 연결을 확인하세요.", parent=self.w)
            return
        expected = self.w._yt_expected_channel_id()

        def find():
            from .youtube_accounts import verify_channel
            channel = verify_channel(api, expected)  # 다른 계정 token이면 여기서 차단
            return channel, list_target_broadcasts(api)
        self._run("targets", self.pid, find, (api, md))

    def retry_failed(self) -> None:
        last = self._last.get(self.pid)
        if self._busy or not last or last["result"].ok:
            return
        r, api, md, cid = last["result"], last["api"], last["md"], last["channel_id"]
        steps = r.failed_steps()
        self._run("applied", self.pid, lambda: apply_metadata_steps(api, r.video_id, md, steps=steps, result=r,
                                                                    channel_id=cid), last)

    def set_session_result(self, pid: str, api, md: LiveMetadata, result, channel_id: str) -> None:
        """API 자동 세션이 만든 방송의 적용 결과 (실패 항목은 같은 방송에 다시 적용 가능)."""
        if result is None:
            return
        self._last[pid] = {"api": api, "md": md, "result": result, "channel_id": channel_id}
        if pid == self.pid:
            self._show_result()

    def _show_result(self):
        last = self._last.get(self.pid)
        if not last:
            self.result_text.set("")
            self.btn_retry.pack_forget()
            return
        r = last["result"]
        head = f"적용 결과 — 방송: {r.broadcast_title or r.video_id}" + (f" · 채널: {r.channel_title}" if r.channel_title else "")
        self.result_text.set(head + "\n" + "\n".join(r.summary_lines()))
        if r.ok:
            self.btn_retry.pack_forget()
        elif not self.btn_retry.winfo_manager():
            self.btn_retry.pack(side="left", padx=(8, 0))

    def _handle(self, kind, pid, ok, payload, ctx):
        self._busy = False
        if getattr(self.w, "_destroyed", False):
            return
        if kind == "lists":
            if ok:
                (cats, err), playlists = payload
                self._categories = cats
                self._playlists[pid] = list(playlists)
                if pid == self.pid:
                    cur = self.collect()
                    self._set_category_values()
                    self._set_playlist_values(cur)
                    self.result_text.set("✓ YouTube 목록을 새로 받았습니다." + (f" (카테고리: {err})" if err else ""))
            elif pid == self.pid:
                self.result_text.set(f"⚠ 목록을 받지 못했습니다: {payload} (저장된 값은 그대로)")
        elif kind == "targets":
            if not ok:
                messagebox.showerror("YouTube에 적용", payload, parent=self.w)
            elif pid != self.pid:
                pass  # 그 사이 다른 채널로 바뀜 → 적용하지 않음
            else:
                channel, broadcasts = payload
                api, md = ctx
                if not broadcasts:
                    messagebox.showinfo("YouTube에 적용", NO_TARGET_TEXT, parent=self.w)
                else:
                    chooser = self.on_choose_target or self._choose_target
                    target = chooser(channel.title, broadcasts, change_lines(md, self._categories))
                    if target is not None and any(b.id == target.id for b in broadcasts):
                        last = {"api": api, "md": md, "channel_id": channel.id}

                        def go():
                            from .youtube_metadata_control import ApplyResult
                            r = ApplyResult(video_id=target.id, broadcast_title=target.title,
                                            channel_title=channel.title)
                            return apply_metadata_steps(api, target.id, md, result=r, channel_id=channel.id)
                        self._run("applied", pid, go, last)
        elif kind == "applied":
            if ok:
                self._last[pid] = {**ctx, "result": payload}
                if pid == self.pid:
                    self._show_result()
            elif pid == self.pid:
                self.result_text.set(f"⚠ 적용하지 못했습니다: {payload}")
        self.sync()

    def _choose_target(self, channel_title, broadcasts, changes):
        dlg = ApplyTargetDialog(self.w, channel_title, broadcasts, changes)
        self.w.wait_window(dlg)
        return dlg.result

    def destroy(self):
        if self._poll_job is not None:
            try:
                self.w.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        release_tk_variables(self)


class ApplyTargetDialog(tk.Toplevel):
    """적용 확인: 어느 YouTube 채널의 어느 방송에 무엇을 바꾸는지. 방송은 사용자가 직접 골라야 [적용]이 켜진다."""

    def __init__(self, master, channel_title: str, broadcasts, changes: list[str]):
        super().__init__(master)
        ensure_theme(self)
        self.title("YouTube에 적용 — 확인")
        self.transient(master)
        self.result = None
        self._broadcasts = list(broadcasts)
        body = ttk.Frame(self, padding=12); body.pack(fill="both", expand=True)
        ttk.Label(body, text="YouTube 채널", font="PLS.Strong").pack(anchor="w")
        ttk.Label(body, text=channel_title or "(이름 없음)").pack(anchor="w", padx=(8, 0))
        ttk.Label(body, text="적용할 방송 (직접 고르세요)", font="PLS.Strong").pack(anchor="w", pady=(8, 0))
        self.cmb = ttk.Combobox(body, state="readonly", width=60, values=[broadcast_label(b) for b in self._broadcasts])
        self.cmb.pack(anchor="w", padx=(8, 0))
        self.cmb.bind("<<ComboboxSelected>>", lambda e: self.btn_ok.configure(state="normal"))
        ttk.Label(body, text="바뀌는 내용", font="PLS.Strong").pack(anchor="w", pady=(8, 0))
        ttk.Label(body, text="\n".join(changes), justify="left", wraplength=520).pack(anchor="w", padx=(8, 0))
        ttk.Label(body, text="선택한 방송 하나에만 적용합니다. 다른 방송은 바꾸지 않습니다.", foreground="gray30").pack(
            anchor="w", pady=(8, 0))
        b = ttk.Frame(body); b.pack(fill="x", pady=(10, 0))
        self.btn_ok = ttk.Button(b, text="적용", style="Primary.TButton", state="disabled", command=self.ok)
        self.btn_ok.pack(side="right")
        ttk.Button(b, text="취소", command=self.destroy).pack(side="right", padx=(0, 6))
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def select(self, index: int) -> None:
        self.cmb.current(index)
        self.btn_ok.configure(state="normal")

    def ok(self):
        i = self.cmb.current()
        if i < 0:
            return
        self.result = self._broadcasts[i]
        self.destroy()
