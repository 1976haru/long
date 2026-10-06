"""② 실시간 스트리밍 · 예약 LIVE 창 — YouTube에 앞으로의 LIVE 방송을 미리 예약 (반복 규칙, 7일/최대 7개 rolling).

- LIVE 창에서 연결한 YouTube 계정(자동 세션 계정)을 그대로 쓴다. 연결은 ② LIVE 창의 [YouTube 연결]에서.
- 규칙(비밀 아님)은 settings.json "live_schedule_rules"에 저장 → [부족분 보충]으로 다음 회차를 이어서 만든다.
- 예약 생성/보충은 백그라운드 스레드에서 하고, 결과만 큐로 받는다 (Tk 객체를 스레드에 넘기지 않음).
"""
from __future__ import annotations

import queue
import secrets
import time
import tkinter as tk
import webbrowser
from datetime import datetime, timedelta, timezone
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .ui_theme import ensure as ensure_theme
from .cloud_setup_ui import _background
from .settings import load_settings, update_settings
from .tooling import release_tk_variables
from .youtube_api import YouTubeApiError
from .youtube_config import build_api_client, is_connected, load_youtube_settings
from .youtube_metadata import (
    DEFAULT_CATEGORIES, PRIVACY_LABELS, THUMB_FIXED, TIMEZONES, MetadataError, MetadataTemplate,
)
from .youtube_oauth import OAuthError
from .youtube_schedule import (
    CUSTOM_WEEKDAYS, DAY_CODES, MODE_LABELS, ReservationStore, ScheduleError, ScheduleRule, get_zone, top_up,
)

RULES_KEY = "live_schedule_rules"
DAY_LABELS = ("월", "화", "수", "목", "금", "토", "일")


def reservation_store() -> ReservationStore:
    return ReservationStore(load_settings, update_settings)


def load_rules() -> list[dict]:
    return [r for r in (load_settings().get(RULES_KEY) or []) if isinstance(r, dict) and r.get("rule_id")]


def save_rule(rule_id: str, rule: ScheduleRule, template: MetadataTemplate) -> None:
    rows = [r for r in load_rules() if r["rule_id"] != rule_id]
    rows.append({"rule_id": rule_id, "rule": rule.to_dict(), "template": template.to_dict()})
    update_settings(**{RULES_KEY: rows})


def _err_text(e: Exception) -> str:
    if isinstance(e, (YouTubeApiError, OAuthError, ScheduleError, MetadataError)):
        return str(e)
    return f"예약 중 오류 ({type(e).__name__})"


class LiveScheduleWindow(tk.Toplevel):
    def __init__(self, master, *, api_factory: Callable = build_api_client, connected: Callable[[], bool] = is_connected,
                 clock: Callable[[], float] = time.time, pick_file: Callable = filedialog.askopenfilename,
                 open_url: Callable[[str], object] = webbrowser.open):
        super().__init__(master)
        ensure_theme(self)  # 글자 크기/버튼 테마 (ui_theme)
        self.title("② 예약 LIVE")
        self.geometry("940x760")
        self.minsize(820, 660)
        self._api_factory = api_factory
        self._connected = connected
        self._clock = clock
        self._pick_file = pick_file
        self._open_url = open_url
        self._q: queue.Queue = queue.Queue()
        self._worker = None
        self.store = reservation_store()

        now_local = datetime.fromtimestamp(clock(), timezone.utc).astimezone(get_zone("Asia/Seoul"))
        self.title_template = tk.StringVar(value="{date} ({weekday}) 24H LIVE #{session}")
        self.tags = tk.StringVar()
        self.thumbnail = tk.StringVar()
        self.privacy = tk.StringVar(value=PRIVACY_LABELS["unlisted"])
        self.category = tk.StringVar(value=DEFAULT_CATEGORIES["10"])
        self.repeat = tk.StringVar(value=MODE_LABELS["DAILY"])
        self.start_date = tk.StringVar(value=(now_local.date() + timedelta(days=1)).isoformat())
        self.start_time = tk.StringVar(value="07:00")
        self.tz = tk.StringVar(value="Asia/Seoul")
        self.duration = tk.IntVar(value=710)
        self.days = {code: tk.BooleanVar(value=False) for code in DAY_CODES}
        self.account = tk.StringVar()
        self.message = tk.StringVar()

        self._ui()
        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(200, self._pump)

    def _ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="② 예약 LIVE", font="PLS.Title").pack(anchor="w")
        ttk.Label(root, textvariable=self.account, foreground="gray30").pack(anchor="w", pady=(0, 8))

        form = ttk.LabelFrame(root, text="① 방송 정보와 반복", padding=8)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        def row(r, text, widget):
            ttk.Label(form, text=text, width=12).grid(row=r, column=0, sticky="nw", pady=2)
            widget.grid(row=r, column=1, sticky="ew", pady=2)
        row(0, "제목", ttk.Entry(form, textvariable=self.title_template))
        ttk.Label(form, foreground="gray30", text="변수: {date} {month} {day} {weekday} {session} {channel}").grid(
            row=1, column=1, sticky="w")
        self.txt_desc = tk.Text(form, height=3, wrap="word")
        row(2, "설명", self.txt_desc)
        row(3, "태그", ttk.Entry(form, textvariable=self.tags))
        tf = ttk.Frame(form)
        ttk.Entry(tf, textvariable=self.thumbnail).pack(side="left", fill="x", expand=True)
        ttk.Button(tf, text="찾기", command=self._pick_thumb).pack(side="left", padx=(4, 0))
        row(4, "썸네일 (선택)", tf)
        pf = ttk.Frame(form)
        ttk.Combobox(pf, textvariable=self.privacy, state="readonly", width=10,
                     values=list(PRIVACY_LABELS.values())).pack(side="left")
        ttk.Label(pf, text="카테고리").pack(side="left", padx=(12, 2))
        ttk.Combobox(pf, textvariable=self.category, state="readonly", width=14,
                     values=list(DEFAULT_CATEGORIES.values())).pack(side="left")
        row(5, "공개 상태", pf)
        rf = ttk.Frame(form)
        cb = ttk.Combobox(rf, textvariable=self.repeat, state="readonly", width=10, values=list(MODE_LABELS.values()))
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_repeat())
        self.day_checks = []
        for code, label in zip(DAY_CODES, DAY_LABELS):
            c = ttk.Checkbutton(rf, text=label, variable=self.days[code])
            c.pack(side="left", padx=(6 if code == "MON" else 0, 0))
            self.day_checks.append(c)
        row(6, "반복", rf)
        sf = ttk.Frame(form)
        ttk.Label(sf, text="첫 날짜").pack(side="left")
        ttk.Entry(sf, textvariable=self.start_date, width=12).pack(side="left", padx=(2, 8))
        ttk.Label(sf, text="시각").pack(side="left")
        ttk.Entry(sf, textvariable=self.start_time, width=7).pack(side="left", padx=(2, 8))
        ttk.Combobox(sf, textvariable=self.tz, width=16, values=list(TIMEZONES)).pack(side="left")
        ttk.Label(sf, text="길이(분)").pack(side="left", padx=(8, 2))
        ttk.Spinbox(sf, from_=1, to=720, width=6, textvariable=self.duration).pack(side="left")
        row(7, "시작", sf)
        act = ttk.Frame(form); act.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.btn_create = ttk.Button(act, text="＋ 예약 만들기 (앞으로 7일 · 최대 7개)", command=self.create)
        self.btn_create.pack(side="left", fill="x", expand=True)
        self.btn_topup = ttk.Button(act, text="저장된 규칙 부족분 보충", command=self.top_up_saved)
        self.btn_topup.pack(side="left", padx=(5, 0))
        self.lbl_msg = ttk.Label(root, textvariable=self.message, justify="left", wraplength=880)
        self.lbl_msg.pack(anchor="w", pady=(6, 0))

        lf = ttk.LabelFrame(root, text="② 예약된 LIVE", padding=8)
        lf.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("start", "title", "privacy", "state")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", height=8, selectmode="browse")
        for c, h, w in zip(cols, ("시작 (현지)", "제목", "공개", "상태"), (190, 440, 80, 130)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c == "title" else "center")
        self.tree.pack(fill="both", expand=True)
        tb = ttk.Frame(lf); tb.pack(fill="x", pady=(4, 0))
        ttk.Button(tb, text="YouTube에서 보기", command=self.open_selected).pack(side="left")
        ttk.Button(tb, text="목록에서 지우기", command=self.remove_selected).pack(side="right")
        ttk.Label(lf, foreground="gray30", text="목록에서 지워도 YouTube 예약은 지워지지 않습니다 (YouTube Studio에서 관리).").pack(anchor="w")
        self._on_repeat()

    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def _say(self, text: str, color: str = "") -> None:
        self.message.set(text)
        self.lbl_msg.configure(foreground=color or "black")

    def _on_repeat(self):
        st = "normal" if self._repeat_mode() == CUSTOM_WEEKDAYS else "disabled"
        for c in self.day_checks:
            c.configure(state=st)

    def _repeat_mode(self) -> str:
        return next((k for k, v in MODE_LABELS.items() if v == self.repeat.get()), "ONCE")

    def _pick_thumb(self):
        p = self._pick_file(parent=self, title="썸네일 선택", filetypes=[("이미지", "*.jpg *.jpeg *.png"), ("모든 파일", "*.*")])
        if p:
            self.thumbnail.set(p)

    def refresh(self) -> None:
        s = load_youtube_settings()
        ok = self._connected()
        self.account.set(f"YouTube 계정: {s.get('channel_title') or '-'}" if ok else
                         "YouTube 계정이 연결되지 않았습니다 → ② LIVE 창에서 [YouTube 연결]을 먼저 하세요.")
        for x in self.tree.get_children():
            self.tree.delete(x)
        for r in self.store.all():
            try:
                start = r.start_local(self.tz.get().strip() or "Asia/Seoul").strftime("%Y-%m-%d %H:%M")
            except (ScheduleError, ValueError):
                start = r.start_utc
            self.tree.insert("", "end", iid=r.broadcast_id, values=(
                start, r.title, PRIVACY_LABELS.get(r.privacy, r.privacy), r.status_label))
        st = "disabled" if self.busy or not ok else "normal"
        self.btn_create.configure(state=st)
        self.btn_topup.configure(state="disabled" if self.busy or not ok or not load_rules() else "normal")

    def build(self) -> tuple[ScheduleRule, MetadataTemplate]:
        mode = self._repeat_mode()
        try:
            duration = int(self.duration.get())
        except (tk.TclError, ValueError):
            raise ScheduleError("방송 길이(분)를 숫자로 입력하세요.") from None
        rule = ScheduleRule(mode=mode, start_date=self.start_date.get().strip(), start_time_local=self.start_time.get().strip(),
                            timezone=self.tz.get().strip(), duration_minutes=duration,
                            custom_weekdays=[c for c in DAY_CODES if self.days[c].get()] if mode == CUSTOM_WEEKDAYS else [])
        privacy = next((k for k, v in PRIVACY_LABELS.items() if v == self.privacy.get()), "unlisted")
        category = next((k for k, v in DEFAULT_CATEGORIES.items() if v == self.category.get()), "10")
        thumb = self.thumbnail.get().strip().strip('"')
        template = MetadataTemplate(name="예약 LIVE", title_template=self.title_template.get(),
                                    description_template=self.txt_desc.get("1.0", "end").strip(), tags=self.tags.get(),
                                    thumbnail_mode=THUMB_FIXED, thumbnail_paths=[thumb] if thumb else [],
                                    category_id=category, privacy_status=privacy)
        return rule.validate(), template.validate()

    def _run(self, jobs: list[tuple[str, dict, dict]], save_new: bool) -> None:
        """jobs: (rule_id, rule dict, template dict) — 스레드에는 dict만 넘긴다."""
        api_factory, clock, store = self._api_factory, self._clock, self.store
        stream_id = load_youtube_settings().get("stream_id") or None
        channel = load_youtube_settings().get("channel_title", "")

        def work():
            api = api_factory()
            now = datetime.fromtimestamp(clock(), timezone.utc)
            made, lines = 0, []
            for rule_id, rd, td in jobs:
                rule, tpl = ScheduleRule.from_dict(rd), MetadataTemplate.from_dict(td)
                if save_new:
                    save_rule(rule_id, rule, tpl)
                for res in top_up(api, store, rule_id=rule_id, rule=rule, template=tpl, now=now, stream_id=stream_id,
                                  channel=channel):
                    made += res.broadcast_ok
                    if not res.complete:
                        lines.extend(res.summary_lines())
            return made, lines

        def result(ok, v):
            return ("done", ok, v if ok else _err_text(v))
        self._worker = _background("youtube-live-schedule", self._q, work, result)
        self._say("YouTube에 예약 중…")
        self.refresh()

    def create(self):
        if self.busy:
            return
        try:
            rule, template = self.build()
        except (ScheduleError, MetadataError) as e:
            messagebox.showerror("예약 LIVE", str(e), parent=self)
            return
        self._run([(secrets.token_hex(6), rule.to_dict(), template.to_dict())], save_new=True)

    def top_up_saved(self):
        if self.busy:
            return
        rules = load_rules()
        if not rules:
            self._say("저장된 반복 규칙이 없습니다.")
            return
        self._run([(r["rule_id"], r.get("rule") or {}, r.get("template") or {}) for r in rules], save_new=False)

    def _selected(self) -> str:
        sel = self.tree.selection()
        return sel[0] if sel else ""

    def open_selected(self):
        bid = self._selected()
        if bid:
            self._open_url(f"https://www.youtube.com/watch?v={bid}")

    def remove_selected(self):
        bid = self._selected()
        if bid and messagebox.askyesno("목록에서 지우기", "이 예약을 목록에서만 지울까요? (YouTube 예약은 그대로 남습니다)",
                                       parent=self):
            self.store.remove(bid)
            self.refresh()

    def _pump(self):
        if getattr(self, "_destroyed", False):
            return
        try:
            while True:
                tag, ok, payload = self._q.get_nowait()
                if tag != "done":
                    continue
                self._worker = None
                if ok:
                    made, lines = payload
                    text = f"✓ 예약 {made}개를 만들었습니다." if made else "새로 만들 회차가 없습니다 (이미 7일/7개 준비됨)."
                    self._say("\n".join([text, *lines]), "darkgreen" if not lines else "darkorange")
                else:
                    self._say(f"✗ {payload}", "firebrick")
                self.refresh()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if self.winfo_exists():
            self.after(200, self._pump)

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)
