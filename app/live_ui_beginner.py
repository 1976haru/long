"""LIVE 창 초보자 화면 조각 (Tk): 채널 A/B 카드 · 빠른 시작 버튼 · 간단 시작 마법사 · 준비 체크리스트 · 방송 정보 안내.

계산은 live_readiness.py (Tk 비의존). 이 파일은 LiveWindow(w)의 기존 동작(_switch_channel/_start/_stop)을 부를 뿐
송출 로직을 새로 만들지 않는다. Stream Key 값은 어디에도 표시하지 않는다 (저장 여부만).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .cloud_model import DEFAULT_LIVE_PROFILE, MAX_CONCURRENT_LIVE
from .core import format_duration
from .live_readiness import (
    DEFAULT_SERVER, MAX_TEXT, STATE_COLORS, ST_LIVE, card_state, channel_label, missing, summary_line,
)
from .tooling import release_tk_variables
from .ui_theme import ensure as ensure_theme

CARD_STACK_WIDTH = 760  # 이보다 좁으면 카드를 세로로 쌓는다 (작은 화면)


# ---------------- 채널 카드 ----------------

class ChannelCard:
    """채널 1개 카드. pid=None이면 '채널 B 만들기' 자리."""

    def __init__(self, w, parent, slot: int):
        self.w, self.slot, self.pid = w, slot, None
        self.frame = tk.Frame(parent, bd=0, highlightthickness=2, highlightbackground="#c4c4c4", bg="#fafafa")
        self.title = tk.StringVar()
        self.state = tk.StringVar()
        self.lines = tk.StringVar()
        self.ready = tk.StringVar()
        bg = "#fafafa"
        top = tk.Frame(self.frame, bg=bg); top.pack(fill="x", padx=10, pady=(8, 0))
        self.lbl_title = tk.Label(top, textvariable=self.title, bg=bg, font="PLS.Section", anchor="w")
        self.lbl_title.pack(side="left")
        self.lbl_now = tk.Label(top, text="◀ 지금 설정 중", bg=bg, fg="#1d4fa8", font="PLS.Strong")
        self.cmb_secondary = None
        self._secondary_ids: list[str] = []
        if slot == 1:  # 두 번째 카드: 등록된 채널 중 '어느 채널을 보여 줄지' 선택 (채널 만들기와 별개)
            self.secondary_var = tk.StringVar()
            self.sec_row = tk.Frame(self.frame, bg=bg); self.sec_row.pack(fill="x", padx=10, pady=(2, 0))
            self.cmb_secondary = ttk.Combobox(self.sec_row, textvariable=self.secondary_var, state="readonly", width=24)
            self.cmb_secondary.pack(side="left")
            self.cmb_secondary.bind("<<ComboboxSelected>>", lambda e: self._on_secondary())
            self.btn_new = ttk.Button(self.sec_row, text="＋ 새 채널", command=self.create)
            self.btn_new.pack(side="left", padx=(5, 0))
        self.lbl_state = tk.Label(self.frame, textvariable=self.state, bg=bg, font="PLS.Section", anchor="w")
        self.lbl_state.pack(fill="x", padx=10)
        self.lbl_lines = tk.Label(self.frame, textvariable=self.lines, bg=bg, justify="left", anchor="w")
        self.lbl_lines.pack(fill="x", padx=10)
        self.lbl_ready = tk.Label(self.frame, textvariable=self.ready, bg=bg, justify="left", anchor="w", wraplength=330,
                                  font="PLS.Strong")
        self.lbl_ready.pack(fill="x", padx=10, pady=(2, 0))
        btns = ttk.Frame(self.frame); btns.pack(fill="x", padx=10, pady=(6, 8))
        self.btn_select = ttk.Button(btns, text="이 채널 설정하기", command=self.select)
        self.btn_select.pack(side="left")
        self.btn_start = ttk.Button(btns, text="▶ 시작", style="Primary.TButton", command=self.start)
        self.btn_start.pack(side="left", padx=(5, 0))
        self.btn_stop = ttk.Button(btns, text="■ 중지", style="Danger.TButton", command=self.stop)
        self.btn_stop.pack(side="left", padx=(5, 0))
        self.btn_rename = ttk.Button(btns, text="이름 바꾸기", command=self.rename)
        self.btn_rename.pack(side="right")
        self.btn_create = ttk.Button(self.frame, text="＋ 두 번째 채널 만들기", style="Primary.TButton", command=self.create)

    def _on_secondary(self):
        i = self.cmb_secondary.current()
        if 0 <= i < len(self._secondary_ids):
            self.w.set_secondary(self._secondary_ids[i])  # 카드 표시만 바뀐다 (어떤 LIVE도 멈추지 않음)

    # actions
    def select(self):
        if self.pid and self.pid != self.w.channel_id:
            why = self.w._channel_switch_blocked()
            if why:
                messagebox.showinfo("채널 바꾸기", why, parent=self.w)
                return
            self.w._switch_channel(self.pid)
        self.w.scroll_to(self.w.f_video)

    def start(self):
        if self.pid:
            self.w.start_channel(self.pid)

    def stop(self):
        if self.pid:
            self.w.stop_channel(self.pid)

    def rename(self):
        if not self.pid:
            return
        prof = self.w.channels.get(self.pid)
        if prof is None:
            return
        name = simpledialog.askstring("채널 이름 바꾸기", "이 채널을 무엇이라고 부를까요? (예: 시니어 채널)",
                                      initialvalue=prof.display_name, parent=self.w)
        if name is None or not name.strip():
            return
        self.w.rename_channel(self.pid, name.strip())

    def create(self):
        name = simpledialog.askstring("새 채널 만들기", "채널 이름을 입력하세요 (예: 도쿄칠)", parent=self.w)
        if name and name.strip():
            self.w.create_channel(name.strip())

    # view
    def show(self, pid, index: int, is_current: bool):
        self.pid = pid
        for x in (self.lbl_state, self.lbl_lines, self.lbl_ready, self.btn_create):
            x.pack_forget()
        if self.cmb_secondary is not None:
            others = [p for p in self.w._channel_ids if p != DEFAULT_LIVE_PROFILE]
            self._secondary_ids = others
            self.cmb_secondary.configure(values=[self.w.channel_name(p) for p in others])
            if pid in others:
                self.cmb_secondary.current(others.index(pid))
            if others:
                self.sec_row.pack(fill="x", padx=10, pady=(2, 0))
            else:
                self.sec_row.pack_forget()
        if pid is None:
            self.title.set("두 번째 송출 채널" if self.slot == 1 else f"채널 {chr(ord('A') + self.slot)}")
            self.lbl_now.pack_forget()
            self.lbl_lines.pack(fill="x", padx=10)
            self.lines.set("아직 없습니다. 두 번째 YouTube 채널로도 송출하려면 만드세요.")
            self.btn_create.pack(anchor="w", padx=10, pady=(4, 8))
            for b in (self.btn_select, self.btn_start, self.btn_stop, self.btn_rename):
                b.configure(state="disabled")
            self.frame.configure(highlightbackground="#c4c4c4")
            return
        prof = self.w.channels.get(pid)
        self.title.set("두 번째 송출 채널" if self.slot == 1 else channel_label(index, prof.display_name if prof else "", pid))
        self.lbl_state.pack(fill="x", padx=10)
        self.lbl_lines.pack(fill="x", padx=10)
        self.lbl_ready.pack(fill="x", padx=10, pady=(2, 0))
        if is_current:
            self.lbl_now.pack(side="right")
        else:
            self.lbl_now.pack_forget()
        self.frame.configure(highlightbackground="#2f6fdf" if is_current else "#c4c4c4")
        for b in (self.btn_select, self.btn_rename):
            b.configure(state="normal")

    def update(self, info: dict):
        state = info["state"]
        text = f"● {state}" + (f"  {format_duration(info['seconds'])}" if state == ST_LIVE and info.get("seconds") else "")
        if self.state.get() != text:
            self.state.set(text)
            self.lbl_state.configure(fg=STATE_COLORS.get(state, "black"))
        lines = "\n".join(info["lines"])
        if self.lines.get() != lines:
            self.lines.set(lines)
        if self.ready.get() != info["ready_text"]:
            self.ready.set(info["ready_text"])
            self.lbl_ready.configure(fg="darkgreen" if info["ready_ok"] else "darkorange")
        self.btn_start.configure(state="normal" if info["can_start"] else "disabled")
        self.btn_stop.configure(state="normal" if info["can_stop"] else "disabled")


def build_channel_cards(w, root) -> None:
    box = ttk.LabelFrame(root, text="채널 (채널 A와 두 번째 송출 채널을 한 화면에서 봅니다)", padding=6)
    box.pack(fill="x", pady=(0, 6))
    ttk.Label(box, text=MAX_TEXT + " 각 채널은 영상·Stream Key가 따로 저장됩니다.", foreground="gray30",
              wraplength=780, justify="left").pack(anchor="w")
    quick = ttk.Frame(box); quick.pack(fill="x", pady=(4, 4))
    ttk.Label(quick, text="빠른 시작", font="PLS.Strong").pack(side="left", padx=(0, 6))
    w.btn_quick_a = ttk.Button(quick, text="▶ 채널 A만 시작", command=lambda: w.open_quick_start(["A"]))
    w.btn_quick_a.pack(side="left")
    w.btn_quick_b = ttk.Button(quick, text="▶ 두 번째 채널만 시작", command=lambda: w.open_quick_start(["B"]))
    w.btn_quick_b.pack(side="left", padx=(5, 0))
    w.btn_quick_both = ttk.Button(quick, text="▶▶ 두 채널 동시 시작", style="Primary.TButton",
                                  command=lambda: w.open_quick_start(["A", "B"]))
    w.btn_quick_both.pack(side="left", padx=(5, 0))
    grid = ttk.Frame(box); grid.pack(fill="x")
    w.channel_cards = [ChannelCard(w, grid, 0), ChannelCard(w, grid, 1)]
    w._cards_stacked = None

    def layout(event=None):
        stacked = grid.winfo_width() < CARD_STACK_WIDTH if grid.winfo_width() > 1 else False
        if stacked == w._cards_stacked:
            return
        w._cards_stacked = stacked
        for i, c in enumerate(w.channel_cards):
            c.frame.grid_forget()
            if stacked:  # 작은 화면: 세로로 쌓고 창 스크롤로 접근
                c.frame.grid(row=i, column=0, sticky="nsew", pady=(0 if i == 0 else 6, 0))
            else:
                c.frame.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=0 if stacked else 1, uniform="" if stacked else "card")
    grid.bind("<Configure>", layout)
    layout()


def card_slots(w) -> list:
    """카드 2칸: A = 기본 채널 (항상), 두 번째 = 사용자가 고른 채널 (저장된 선택, 없으면 첫 채널, 없으면 None)."""
    return [DEFAULT_LIVE_PROFILE, w.secondary_profile_id()]


def update_cards(w) -> None:
    slots = card_slots(w)
    sig = (tuple(slots), w.channel_id, tuple(sorted(w._channel_names.items())))
    if getattr(w, "_cards_sig", None) != sig:
        w._cards_sig = sig
        for card, pid in zip(w.channel_cards, slots):
            card.show(pid, w._channel_ids.index(pid) if pid in w._channel_ids else 1, pid == w.channel_id)
        a = short(w.channel_name(slots[0]), "A")
        b = short(w.channel_name(slots[1]), "B") if slots[1] else None
        w.btn_quick_a.configure(text=f"▶ {a}만 시작")
        w.btn_quick_b.configure(text=f"▶ {b}만 시작" if b else "▶ 두 번째 채널만 시작",
                                state="normal" if slots[1] else "disabled")
        w.btn_quick_both.configure(text=f"▶▶ {a} + {b} 동시 시작" if b else "▶▶ 두 채널 동시 시작",
                                   state="normal" if slots[1] else "disabled")
    for card, pid in zip(w.channel_cards, slots):
        if pid:
            card.update(w.card_info(pid))


def short(name: str | None, letter: str) -> str:
    if not name or name == "기본 채널":
        return f"채널 {letter}"
    return name if len(name) <= 14 else name[:13] + "…"


# ---------------- 준비 체크리스트 ----------------

def build_checklist_panel(w, root) -> None:
    f = ttk.LabelFrame(root, text="준비 상태 — 지금 설정하는 채널", padding=7)
    f.pack(fill="x", pady=(8, 0))
    w.check_title = tk.StringVar()
    ttk.Label(f, textvariable=w.check_title, font="PLS.Strong").pack(anchor="w")
    # Variable은 평범한 list로 (창 destroy 때 release_tk_variables가 main thread에서 정리할 수 있게)
    w.check_vars, w.check_labels = [], []
    for _ in range(6):
        v = tk.StringVar()
        lbl = ttk.Label(f, textvariable=v, justify="left", wraplength=780)
        lbl.pack(anchor="w")
        w.check_vars.append(v)
        w.check_labels.append(lbl)
    w.check_big = tk.StringVar()
    w.lbl_check_big = ttk.Label(f, textvariable=w.check_big, font="PLS.Section")
    w.lbl_check_big.pack(anchor="w", pady=(4, 0))


def update_checklist(w) -> None:
    items = w.channel_readiness(w.channel_id)
    w.check_title.set(w.channel_label_of(w.channel_id))
    for v, lbl, it in zip(w.check_vars, w.check_labels, items):
        text = f"{'☑' if it.ok else '☐'} {it.label} — {it.hint}"
        if v.get() != text:
            v.set(text)
            lbl.configure(foreground="darkgreen" if it.ok else "darkorange")
    miss = missing(items)
    big = "✓ 시작 가능 — [▶ 24H LIVE 시작] 또는 위 채널 카드의 [▶ 시작]을 누르세요." if not miss else \
        "부족한 것: " + " · ".join(i.label for i in miss)
    if w.check_big.get() != big:
        w.check_big.set(big)
        w.lbl_check_big.configure(foreground="darkgreen" if not miss else "darkorange")


# ---------------- 방송 정보 (제목·설명·…) 안내 + 자리 ----------------

META_INFO = (
    "• Stream Key 직접 송출: 제목·설명·썸네일·공개 상태·카테고리·재생목록은 YouTube Studio(라이브 스트리밍 화면)에서 정합니다. "
    "이 앱은 영상만 보냅니다.\n"
    "• YouTube API 자동 세션: 제목·설명·공개 상태는 이 창 ③에서 정할 수 있습니다.\n"
    "• ② 예약 LIVE 창: 제목·설명·태그·썸네일·카테고리·공개 상태를 앱에서 정합니다.\n"
    "• 채널별 방송 정보 저장(태그·썸네일·카테고리·YouTube 재생목록)은 추후 지원 예정입니다.")
META_FIELDS = (("title", "제목"), ("description", "설명"), ("privacy", "공개 상태"), ("thumbnail", "썸네일"),
               ("category", "카테고리"), ("playlist", "YouTube 재생목록"))


def build_metadata_panel(w, root) -> None:
    f = ttk.LabelFrame(root, text="방송 정보는 어디서 정하나요? (제목·설명·태그·썸네일·카테고리·재생목록·공개 상태)", padding=7)
    f.pack(fill="x", pady=(8, 0))
    ttk.Label(f, text=META_INFO, justify="left", wraplength=780, foreground="gray25").pack(anchor="w")
    w.meta_frame = ttk.Frame(f)
    w.meta_widgets = {}  # 향후 채널별 방송 정보 저장용 자리 (지금은 비활성)
    for key, label in META_FIELDS:
        r = ttk.Frame(w.meta_frame); r.pack(fill="x", pady=1)
        ttk.Label(r, text=label, width=14).pack(side="left")
        e = ttk.Entry(r)
        e.insert(0, "추후 지원 예정 · API 연결 시 사용")
        e.configure(state="disabled")
        e.pack(side="left", fill="x", expand=True)
        w.meta_widgets[key] = e
    w.btn_meta = ttk.Button(f, text="채널별 방송 정보 칸 보기 (추후 지원 예정) ▸", command=lambda: toggle_meta(w))
    w.btn_meta.pack(anchor="w", pady=(4, 0))


def toggle_meta(w) -> None:
    if w.meta_frame.winfo_manager():
        w.meta_frame.pack_forget()
        w.btn_meta.configure(text="채널별 방송 정보 칸 보기 (추후 지원 예정) ▸")
    else:
        w.meta_frame.pack(fill="x", pady=(4, 0), before=w.btn_meta)
        w.btn_meta.configure(text="채널별 방송 정보 칸 접기 ▾")


# ---------------- 간단 시작 마법사 ----------------

STEP_TITLES = ("채널 확인", "영상/Playlist 선택", "Stream Key 확인", "서버 주소 확인", "시작")


class QuickStartWizard(tk.Toplevel):
    """[채널 A만 / 채널 B만 / 두 채널 동시 시작] → 채널마다 STEP 1~5. 각 단계에 '지금 할 일' 한 줄."""

    def __init__(self, w, targets: list[str]):
        super().__init__(w)
        ensure_theme(self)
        self.w = w
        self.targets = [p for p in targets if p]
        self.idx, self.step = 0, 1
        self.started: list[str] = []
        self.title("간단 시작" + (" — 두 채널 동시" if len(self.targets) > 1 else ""))
        self.transient(w)
        self.minsize(560, 360)
        self.head = tk.StringVar()
        self.body = tk.StringVar()
        self.todo = tk.StringVar()
        f = ttk.Frame(self, padding=16); f.pack(fill="both", expand=True)
        ttk.Label(f, textvariable=self.head, font="PLS.Title").pack(anchor="w")
        self.steps_lbl = ttk.Label(f, foreground="gray30")
        self.steps_lbl.pack(anchor="w", pady=(2, 8))
        ttk.Label(f, textvariable=self.body, justify="left", wraplength=520).pack(anchor="w")
        self.lbl_todo = ttk.Label(f, textvariable=self.todo, font="PLS.Strong", wraplength=520, justify="left")
        self.lbl_todo.pack(anchor="w", pady=(10, 0))
        row = ttk.Frame(f); row.pack(fill="x", side="bottom", pady=(12, 0))
        self.btn_back = ttk.Button(row, text="◀ 이전", command=self.back)
        self.btn_back.pack(side="left")
        self.btn_go = ttk.Button(row, text="이 단계 하러 가기", command=self.go_to_field)
        self.btn_go.pack(side="left", padx=6)
        self.btn_next = ttk.Button(row, text="다음 ▶", style="Primary.TButton", command=self.next)
        self.btn_next.pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda e: self.destroy())
        self._enter_channel()
        self.render()
        self.after(500, self._poll)

    @property
    def pid(self) -> str:
        return self.targets[self.idx]

    def _enter_channel(self) -> bool:
        if self.pid != self.w.channel_id:
            why = self.w._channel_switch_blocked()
            if why:
                self.todo.set("⚠ " + why)
                return False
            self.w._switch_channel(self.pid)
        return True

    def items(self):
        return {i.key: i for i in self.w.channel_readiness(self.pid)}

    def render(self):
        n = len(self.targets)
        name = self.w.channel_label_of(self.pid)
        self.head.set(f"STEP {self.step} / 5 · {STEP_TITLES[self.step - 1]}" + (f"   ({self.idx + 1}/{n} 채널)" if n > 1 else ""))
        self.steps_lbl.configure(text="  →  ".join(("● " if i == self.step else "") + t for i, t in enumerate(STEP_TITLES, 1)))
        it = self.items()
        live = self.w.channel_is_live(self.pid)
        if self.step == 1:
            body = f"송출할 채널: {name}\n" + ("이미 송출 중인 채널입니다." if live else
                                             "이 채널의 영상과 Stream Key를 차례로 확인합니다.")
            todo = "지금 할 일: 채널 이름이 맞으면 [다음 ▶]을 누르세요. 다르면 창을 닫고 채널 카드에서 고르세요."
        elif self.step == 2:
            m, r = it["media"], it["ready"]
            body = f"영상: {'✓ ' if m.ok else '☐ '}{m.hint}\nLIVE READY: {'✓ ' if r.ok else '☐ '}{r.hint}"
            todo = "지금 할 일: " + ("[다음 ▶]" if m.ok and r.ok else
                                  ("[이 단계 하러 가기]를 눌러 ① LIVE 영상에서 영상을 고르세요." if not m.ok else r.hint))
        elif self.step == 3:
            k = it["key"]
            body = f"Stream Key: {'✓ ' if k.ok else '☐ '}{k.hint}\n(여기에는 Stream Key만 넣습니다. 서버 주소는 넣지 마세요.)"
            todo = "지금 할 일: " + ("[다음 ▶]" if k.ok else "[이 단계 하러 가기]를 눌러 Stream Key 칸에 붙여넣으세요.")
        elif self.step == 4:
            s = it["server"]
            body = (f"서버 주소: {'✓ ' if s.ok else '☐ '}{s.hint}\n기본 주소 {DEFAULT_SERVER} 를 자동으로 씁니다.\n"
                    "(Stream Key와 서버 주소는 서로 다른 값입니다)")
            todo = "지금 할 일: " + ("[다음 ▶]" if s.ok else s.hint)
        else:
            miss = [i for i in it.values() if not i.ok]
            body = "\n".join(f"{'☑' if i.ok else '☐'} {i.label} — {i.hint}" for i in it.values())
            if live:
                todo = f"✓ {name} 송출 중입니다." + (" [다음 채널 ▶]을 누르세요." if self.idx + 1 < n else " [닫기]를 누르세요.")
            elif miss:
                todo = "지금 할 일: 부족한 항목을 채운 뒤 [▶ 이 채널 시작]을 누르세요 — " + ", ".join(i.label for i in miss)
            else:
                todo = f"지금 할 일: [▶ 이 채널 시작]을 누르세요. (최대 {MAX_CONCURRENT_LIVE}채널 동시 송출)"
        self.body.set(body)
        self.todo.set(todo)
        self.btn_back.configure(state="normal" if self.step > 1 else "disabled")
        self.btn_go.configure(state="normal" if self.step in (2, 3, 4) else "disabled")
        if self.step < 5:
            self.btn_next.configure(text="다음 ▶", state="normal", command=self.next)
        elif live:
            more = self.idx + 1 < n
            self.btn_next.configure(text="다음 채널 ▶" if more else "닫기", state="normal",
                                    command=self.next_channel if more else self.destroy)
        else:
            ok = not [i for i in it.values() if not i.ok] and not self.w.cloud.busy
            self.btn_next.configure(text="▶ 이 채널 시작", state="normal" if ok else "disabled", command=self.start)

    def back(self):
        self.step = max(1, self.step - 1)
        self.render()

    def next(self):
        self.step = min(5, self.step + 1)
        self.render()

    def go_to_field(self):
        target = {2: self.w.f_video, 3: self.w.ent_key, 4: self.w.ent_key}.get(self.step)
        self._enter_channel()
        if target is not None:
            self.w.scroll_to(target)
            if self.step == 3:
                try:
                    self.w.ent_key.focus_set()
                except tk.TclError:
                    pass

    def start(self):
        if self._enter_channel() and self.w.start_channel(self.pid):
            self.started.append(self.pid)
        self.render()

    def next_channel(self):
        if self.idx + 1 < len(self.targets):
            self.idx += 1
            self.step = 1
            self._enter_channel()
            self.render()

    def _poll(self):
        if getattr(self, "_destroyed", False):
            return
        try:
            self.render()
            self.after(700, self._poll)
        except tk.TclError:
            return

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        super().destroy()
        release_tk_variables(self)  # 다른 스레드의 GC가 Tcl을 부르지 않게 main thread에서 정리


def release_card_variables(w) -> None:
    """LIVE 창 destroy 때: 카드 객체 안의 StringVar도 main thread에서 정리."""
    for card in getattr(w, "channel_cards", []):
        release_tk_variables(card)


SERVER_HELP = f"""서버 주소와 Stream Key는 서로 다른 값입니다.

• 서버 주소: YouTube가 영상을 받는 '주소'입니다. 모든 채널이 같습니다.
  예: rtmp://a.rtmp.youtube.com/live2
  → 이 프로그램은 기본값을 자동으로 사용합니다 ({DEFAULT_SERVER}).
     초보자는 따로 넣을 필요가 없습니다.

• Stream Key: '어느 채널의 방송인지' 알려주는 비밀 번호입니다. 채널마다 다릅니다.
  예: xxxx-xxxx-xxxx-xxxx-xxxx
  → YouTube Studio → 라이브 스트리밍 → 'Stream Key'를 복사해서 Stream Key 칸에만 붙여넣으세요.

자주 하는 실수
✗ 서버 주소 칸에 'rtmp://…/live2/xxxx-xxxx…' 처럼 Stream Key까지 붙여 넣기
✗ Stream Key 칸에 'rtmp://…' 서버 주소 넣기
Stream Key는 다른 사람에게 보여주지 마세요 (화면에는 ●로 가려집니다)."""
