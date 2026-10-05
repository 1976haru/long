"""세로 스크롤 컨테이너 — 작은 화면(1366×768 등)에서도 창 아래쪽 버튼까지 닿게 한다.

- 내용은 self.body 안에 기존과 같은 순서로 pack/grid 한다 (화면 구조를 바꾸지 않음).
- 창이 내용보다 크면 body가 창 높이만큼 늘어나 기존처럼 expand가 동작한다.
- 마우스 휠: 그 창 안에서만. Treeview/Text/Listbox 위에서는 그 위젯이 스스로 스크롤할 수 있으면 양보하고,
  Combobox/Spinbox 위에서는 값이 바뀌지 않도록 페이지를 스크롤하지 않는다.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

_SELF_SCROLLING = ("Treeview", "Text", "Listbox")
_VALUE_WIDGETS = ("TCombobox", "TSpinbox", "Spinbox")


class ScrollFrame(ttk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self._layout())
        self.canvas.bind("<Configure>", lambda e: self._layout())
        top = self.winfo_toplevel()
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            top.bind(seq, self._on_wheel, add="+")
        try:
            bg = ttk.Style(self).lookup("TFrame", "background")
            if bg:
                self.canvas.configure(background=bg)
        except tk.TclError:
            pass

    def _layout(self) -> None:
        try:
            w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
            need = self.body.winfo_reqheight()
            self.canvas.itemconfigure(self._win, width=max(1, w), height=max(need, h))
            self.canvas.configure(scrollregion=(0, 0, w, max(need, h)))
        except tk.TclError:
            pass

    @property
    def scrollable(self) -> bool:
        return self.body.winfo_reqheight() > self.canvas.winfo_height() + 1

    def _inside(self, widget) -> bool:
        w = widget
        while w is not None:
            if w is self:
                return True
            w = getattr(w, "master", None)
        return False

    def _on_wheel(self, event):
        w = event.widget
        if not isinstance(w, tk.Misc) or not self._inside(w) or not self.scrollable:
            return None
        cls = w.winfo_class()
        if cls in _VALUE_WIDGETS:
            return None
        if cls in _SELF_SCROLLING:
            try:
                if tuple(w.yview()) != (0.0, 1.0):
                    return None  # 목록이 스스로 스크롤
            except tk.TclError:
                pass
        if getattr(event, "num", 0) == 4:
            step = -3
        elif getattr(event, "num", 0) == 5:
            step = 3
        else:
            step = -3 if event.delta > 0 else 3
        self.canvas.yview_scroll(step, "units")
        return "break"

    def scroll_to(self, widget) -> None:
        """widget이 보이도록 스크롤 (테스트/키보드 이동용)."""
        self.update_idletasks()
        total = max(1, self.body.winfo_height())
        y = widget.winfo_rooty() - self.body.winfo_rooty()
        self.canvas.yview_moveto(max(0.0, min(1.0, (y - 20) / total)))
