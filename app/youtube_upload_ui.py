"""③ 예약 업로드 창 — 한국·일본 등 여러 YouTube 채널에 완성 영상을 예약 업로드 (대기열, 1개씩 순차).

흐름: 채널 선택 → 영상/제목/태그/썸네일 → 예약 공개 시각(채널 시간대) → [대기열에 추가] → [▶ 업로드 시작]
- 업로드 대기열(UploadQueue)은 MainWindow가 가진다 → 이 창을 닫아도 업로드는 계속되고, 다시 열면 상태가 보인다.
- 화면에는 업로드 세션 URL/token을 표시하지 않는다.
"""
from __future__ import annotations

import tkinter as tk
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .tooling import release_tk_variables
from .youtube_accounts import ProfileStore
from .youtube_schedule import ScheduleError, get_zone, local_to_utc
from .youtube_upload_queue import ACTIVE_STATES, MAX_JOBS, QueueError, UploadQueue

SCHEDULE = "schedule"
PUBLISH_MODES = {SCHEDULE: "예약 공개 (지정 시각에 공개)", "private": "지금 비공개로 올리기",
                 "unlisted": "지금 일부공개로 올리기", "public": "지금 공개로 올리기"}


class MultiChannelUploadWindow(tk.Toplevel):
    def __init__(self, master, *, upload_queue: UploadQueue, profiles: ProfileStore | None = None,
                 channel_window: Callable | None = None, pick_file: Callable = filedialog.askopenfilename,
                 video_path: str = "", title: str = "", clock: Callable[[], float] | None = None):
        super().__init__(master)
        self.title("③ 예약 업로드 · 여러 YouTube 채널")
        self.geometry("1000x780")
        self.minsize(880, 680)
        self.q = upload_queue
        self.profiles = profiles or upload_queue.profiles
        self._channel_window_factory = channel_window
        self._pick_file = pick_file
        self._clock = clock or upload_queue.clock
        self.channel_win = None
        self._profile_ids: list[str] = []

        self.profile = tk.StringVar()
        self.tz_text = tk.StringVar()
        self.video = tk.StringVar(value=video_path)
        self.video_title = tk.StringVar(value=title)
        self.tags = tk.StringVar()
        self.thumbnail = tk.StringVar()
        self.publish_mode = tk.StringVar(value=PUBLISH_MODES[SCHEDULE])
        self.pub_date = tk.StringVar()
        self.pub_time = tk.StringVar(value="18:00")
        self.summary = tk.StringVar()
        self.detail = tk.StringVar()

        self._ui()
        self.refresh_profiles()
        self.refresh_jobs()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(300, self._pump)

    # ---------- 화면 ----------
    def _ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        top = ttk.Frame(root); top.pack(fill="x")
        ttk.Label(top, text="③ 예약 업로드", font=("Segoe UI", 15, "bold")).pack(side="left")
        ttk.Button(top, text="YouTube 채널 관리", command=self.open_channels).pack(side="right")
        ttk.Label(root, foreground="gray30", text=(
            "한국·일본 등 여러 채널에 예약 업로드합니다. 업로드 직전마다 채널을 다시 확인하고, "
            "다르면 업로드하지 않습니다. 이 창을 닫아도 업로드는 계속됩니다.")).pack(anchor="w", pady=(0, 8))

        form = ttk.LabelFrame(root, text="① 업로드할 영상", padding=8)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        def row(r, text, widget):
            ttk.Label(form, text=text, width=12).grid(row=r, column=0, sticky="nw", pady=2)
            widget.grid(row=r, column=1, sticky="ew", pady=2)
        ch = ttk.Frame(form)
        self.cb_profile = ttk.Combobox(ch, textvariable=self.profile, state="readonly", width=40)
        self.cb_profile.pack(side="left")
        self.cb_profile.bind("<<ComboboxSelected>>", lambda e: self._on_profile())
        ttk.Label(ch, textvariable=self.tz_text, foreground="gray30").pack(side="left", padx=8)
        row(0, "채널", ch)
        vf = ttk.Frame(form)
        ttk.Entry(vf, textvariable=self.video).pack(side="left", fill="x", expand=True)
        ttk.Button(vf, text="찾기", command=self._pick_video).pack(side="left", padx=(4, 0))
        row(1, "영상 파일", vf)
        row(2, "제목", ttk.Entry(form, textvariable=self.video_title))
        self.txt_desc = tk.Text(form, height=4, wrap="word")
        row(3, "설명", self.txt_desc)
        row(4, "태그", ttk.Entry(form, textvariable=self.tags))
        tf = ttk.Frame(form)
        ttk.Entry(tf, textvariable=self.thumbnail).pack(side="left", fill="x", expand=True)
        ttk.Button(tf, text="찾기", command=self._pick_thumb).pack(side="left", padx=(4, 0))
        row(5, "썸네일 (선택)", tf)
        pf = ttk.Frame(form)
        self.cb_mode = ttk.Combobox(pf, textvariable=self.publish_mode, state="readonly", width=28,
                                    values=list(PUBLISH_MODES.values()))
        self.cb_mode.pack(side="left")
        self.cb_mode.bind("<<ComboboxSelected>>", lambda e: self._on_mode())
        ttk.Label(pf, text="날짜").pack(side="left", padx=(12, 2))
        self.ent_date = ttk.Entry(pf, textvariable=self.pub_date, width=12)
        self.ent_date.pack(side="left")
        ttk.Label(pf, text="시각").pack(side="left", padx=(8, 2))
        self.ent_time = ttk.Entry(pf, textvariable=self.pub_time, width=7)
        self.ent_time.pack(side="left")
        ttk.Label(pf, text="(채널 시간대 기준 · 예: 2026-10-06 18:00)", foreground="gray30").pack(side="left", padx=6)
        row(6, "공개", pf)
        ttk.Button(form, text="＋ 예약 업로드 대기열에 추가", command=self.add_job).grid(row=7, column=0, columnspan=2,
                                                                                 sticky="ew", pady=(6, 0))

        qf = ttk.LabelFrame(root, text=f"② 예약 업로드 대기열 (최대 {MAX_JOBS}개 · 1개씩 순차 업로드)", padding=8)
        qf.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("n", "channel", "title", "publish", "state", "progress")
        self.tree = ttk.Treeview(qf, columns=cols, show="headings", height=8, selectmode="browse")
        for c, h, w in zip(cols, ("#", "채널", "제목", "공개 시각", "상태", "진행"), (36, 170, 330, 170, 130, 60)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("channel", "title") else "center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        ttk.Label(qf, textvariable=self.detail, foreground="gray30", wraplength=900, justify="left").pack(anchor="w")
        tb = ttk.Frame(qf); tb.pack(fill="x", pady=(4, 0))
        ttk.Button(tb, text="▲", width=3, command=lambda: self._move(-1)).pack(side="left")
        ttk.Button(tb, text="▼", width=3, command=lambda: self._move(1)).pack(side="left", padx=(2, 8))
        ttk.Button(tb, text="다시 시도", command=self.retry_selected).pack(side="left")
        ttk.Button(tb, text="취소", command=self.cancel_selected).pack(side="left", padx=4)
        ttk.Button(tb, text="삭제", command=self.remove_selected).pack(side="left")
        ttk.Button(tb, text="완료 정리", command=self.clear_done).pack(side="right")

        ar = ttk.Frame(root); ar.pack(fill="x", pady=(8, 0))
        self.btn_start = ttk.Button(ar, text="▶ 예약 업로드 시작", command=self.start)
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(ar, text="■ 중지 (나중에 이어 올리기)", command=self.stop)
        self.btn_stop.pack(side="left", padx=(5, 0))
        ttk.Label(root, textvariable=self.summary).pack(anchor="w", pady=(4, 0))
        self._on_mode()

    # ---------- 채널 ----------
    def open_channels(self):
        if self.channel_win is not None:
            try:
                if self.channel_win.winfo_exists():
                    self.channel_win.lift()
                    return self.channel_win
            except tk.TclError:
                pass
        if self._channel_window_factory:
            self.channel_win = self._channel_window_factory(self, on_change=self.refresh_profiles)
        else:
            from .youtube_channels_ui import ChannelManagerWindow
            self.channel_win = ChannelManagerWindow(self, profiles=self.profiles, on_change=self.refresh_profiles)
        return self.channel_win

    def refresh_profiles(self) -> None:
        profiles = [p for p in self.profiles.all()]
        cur = self.selected_profile()
        self._profile_ids = [p.profile_id for p in profiles]
        self.cb_profile.configure(values=[p.label for p in profiles])
        keep = cur.profile_id if cur and cur.profile_id in self._profile_ids else (
            self._profile_ids[0] if self._profile_ids else "")
        self.profile.set(next((p.label for p in profiles if p.profile_id == keep), ""))
        if not profiles:
            self.tz_text.set("등록된 채널이 없습니다 → [YouTube 채널 관리]")
        self._on_profile()
        if not self.pub_date.get():
            self._default_date("Asia/Seoul")

    def _default_date(self, tz: str) -> None:
        now = datetime.fromtimestamp(self._clock(), timezone.utc).astimezone(get_zone(tz))
        self.pub_date.set((now.date() + timedelta(days=1)).isoformat())

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
            return
        self.tz_text.set(f"시간대 {p.timezone}" + ("" if p.channel_id else " · ⚠ 연결 안 됨"))
        if not self.pub_date.get():
            self._default_date(p.timezone)

    def _on_mode(self) -> None:
        st = "normal" if self._mode() == SCHEDULE else "disabled"
        self.ent_date.configure(state=st)
        self.ent_time.configure(state=st)

    def _mode(self) -> str:
        return next((k for k, v in PUBLISH_MODES.items() if v == self.publish_mode.get()), SCHEDULE)

    def _pick_video(self):
        p = self._pick_file(parent=self, title="업로드할 영상 선택", filetypes=[("MP4", "*.mp4"), ("모든 파일", "*.*")])
        if p:
            self.video.set(p)
            if not self.video_title.get().strip():
                self.video_title.set(Path(p).stem)

    def _pick_thumb(self):
        p = self._pick_file(parent=self, title="썸네일 선택", filetypes=[("이미지", "*.jpg *.jpeg *.png"), ("모든 파일", "*.*")])
        if p:
            self.thumbnail.set(p)

    def set_video(self, path: str, title: str = "") -> None:
        self.video.set(path)
        self.video_title.set(title or Path(path).stem)

    # ---------- 대기열 ----------
    def _publish_at(self, tz: str) -> datetime:
        try:
            d = date.fromisoformat(self.pub_date.get().strip())
            h, m = self.pub_time.get().strip().split(":")
            t = dtime(int(h), int(m))
        except ValueError:
            raise ScheduleError("예약 날짜/시각 형식이 올바르지 않습니다 (예: 2026-10-06, 18:00).") from None
        return local_to_utc(d, t, tz)

    def add_job(self):
        p = self.selected_profile()
        if p is None:
            messagebox.showwarning("채널", "채널을 선택하세요. 없으면 [YouTube 채널 관리]에서 등록하세요.", parent=self)
            return None
        mode = self._mode()
        try:
            publish_at = self._publish_at(p.timezone) if mode == SCHEDULE else None
            job = self.q.make_job(profile_id=p.profile_id, video_path=self.video.get(), title=self.video_title.get(),
                                  description=self.txt_desc.get("1.0", "end").strip(), tags=self.tags.get(),
                                  thumbnail_path=self.thumbnail.get(),
                                  privacy="private" if mode == SCHEDULE else mode, publish_at=publish_at)
            self.q.add(job)
        except (QueueError, ScheduleError, ValueError) as e:
            messagebox.showerror("예약 업로드", str(e), parent=self)
            return None
        self.video.set("")
        self.video_title.set("")
        self.refresh_jobs(select=job.job_id)
        return job

    def _selected_job(self) -> str:
        sel = self.tree.selection()
        return sel[0] if sel else ""

    def _act(self, fn, *args) -> None:
        jid = self._selected_job()
        if not jid:
            return
        try:
            fn(jid, *args)
        except QueueError as e:
            messagebox.showwarning("예약 업로드", str(e), parent=self)
        self.refresh_jobs(select=jid)

    def retry_selected(self):
        self._act(self.q.retry)

    def cancel_selected(self):
        self._act(self.q.cancel_job)

    def remove_selected(self):
        jid = self._selected_job()
        if jid and messagebox.askyesno("삭제", "선택한 작업을 대기열에서 지울까요? (YouTube에 올라간 영상은 지우지 않습니다)",
                                       parent=self):
            self._act(self.q.remove)

    def _move(self, delta: int):
        self._act(self.q.move, delta)

    def clear_done(self):
        self.q.clear_done()
        self.refresh_jobs()

    def start(self):
        if not any(j.status == "PENDING" for j in self.q.snapshot()):
            messagebox.showinfo("예약 업로드", "대기 중인 작업이 없습니다. 실패/중지 작업은 [다시 시도]를 누르세요.", parent=self)
            return
        self.q.start()
        self.refresh_jobs()

    def stop(self):
        self.q.cancel.set()  # 현재 조각 전송 뒤 멈춘다 (UI를 막지 않도록 join하지 않음)
        self.summary.set("중지 중… 다시 시작하면 받은 위치부터 이어서 올립니다.")

    def refresh_jobs(self, select: str | None = None) -> None:
        sel = select if select is not None else self._selected_job()
        jobs = self.q.snapshot()
        for x in self.tree.get_children():
            self.tree.delete(x)
        for i, j in enumerate(jobs, 1):
            self.tree.insert("", "end", iid=j.job_id, values=(
                i, j.profile_alias, j.title, j.publish_local_text(), j.status_label, f"{j.progress * 100:.0f}%"))
        if sel and self.tree.exists(sel):
            self.tree.selection_set(sel)
        c = self.q.counts()
        running = self.q.running
        self.summary.set(f"대기 {c['waiting']} · 예약 완료 {c['done']} · 확인 필요 {c['attention']} · 전체 {c['total']}/{MAX_JOBS}"
                         + (" · 업로드 중" if running else ""))
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self._on_select()

    def _on_select(self) -> None:
        jid = self._selected_job()
        j = next((x for x in self.q.snapshot() if x.job_id == jid), None)
        if j is None:
            self.detail.set("")
            return
        parts = [f"{j.profile_alias} · {Path(j.video_path).name}"]
        if j.video_id:
            parts.append(f"YouTube video ID {j.video_id}")
        if j.error:
            parts.append(j.error)
        self.detail.set(" · ".join(parts))

    def _pump(self) -> None:
        if getattr(self, "_destroyed", False):
            return
        changed = False
        try:
            while True:
                self.q.events.get_nowait()
                changed = True
        except Exception:
            pass
        try:
            if changed or self.q.running != (str(self.btn_stop.cget("state")) == "normal"):
                self.refresh_jobs()
        except tk.TclError:
            return
        if self.winfo_exists():
            self.after(300, self._pump)

    @property
    def uploading(self) -> bool:
        return self.q.running or any(j.status in ACTIVE_STATES for j in self.q.snapshot())

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        if self.channel_win is not None:
            try:
                self.channel_win.destroy()
            except tk.TclError:
                pass
        super().destroy()
        release_tk_variables(self)
