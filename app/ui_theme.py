"""화면 글자/버튼 테마 (가독성) — 한 곳에서 글자 크기를 정하고 모든 창에 똑같이 적용한다.

- Tk named font(이름 붙은 글꼴)를 쓰므로 크기를 바꾸면 열려 있는 창에도 바로 적용된다 (다시 시작할 필요 없음).
- 글자 크기는 포인트(pt) 단위 → Windows 배율(DPI)은 Tk가 그대로 반영한다. tk scaling은 건드리지 않는다.
- 초보자 기본값은 '크게'. 설정(ui_font_size)에 저장되어 다음 실행에도 유지된다.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from .settings import load_settings, update_settings

FAMILY = "Segoe UI"
SIZE_KEY = "ui_font_size"
NORMAL, LARGE, XLARGE = "normal", "large", "xlarge"
SIZE_LABELS = {NORMAL: "보통", LARGE: "크게", XLARGE: "아주 크게"}
ORDER = (NORMAL, LARGE, XLARGE)
DEFAULT_SIZE = LARGE  # 초보자 기본: 크게

# 역할별 글자 크기 (pt)
PRESETS = {
    NORMAL: {"body": 10, "small": 9, "lead": 11, "strong": 10, "section": 12, "title": 15, "hero": 17, "button": 10},
    LARGE: {"body": 12, "small": 11, "lead": 12, "strong": 12, "section": 14, "title": 17, "hero": 18, "button": 12},
    XLARGE: {"body": 14, "small": 12, "lead": 14, "strong": 14, "section": 16, "title": 19, "hero": 20, "button": 13},
}
# 이 프로그램 전용 named font: (이름, 역할, 굵게)
NAMED = (("PLS.Body", "body", False), ("PLS.Small", "small", False), ("PLS.SmallBold", "small", True),
         ("PLS.Lead", "lead", False), ("PLS.Strong", "strong", True), ("PLS.Section", "section", True),
         ("PLS.Title", "title", True), ("PLS.Hero", "hero", True), ("PLS.Button", "button", False),
         ("PLS.ButtonBold", "button", True))
# Tk 기본 글꼴도 같은 크기로 (라벨/입력칸/목록/메뉴 등 font를 따로 지정하지 않은 모든 위젯)
TK_DEFAULTS = (("TkDefaultFont", "body"), ("TkTextFont", "body"), ("TkMenuFont", "body"), ("TkHeadingFont", "body"),
               ("TkCaptionFont", "section"), ("TkSmallCaptionFont", "small"), ("TkIconFont", "body"),
               ("TkTooltipFont", "small"))
TEXT_COLOR_MUTED = "#4a4a4a"  # 설명용 회색 (너무 연하지 않게)
READ_WIDTH = 720  # 긴 설명의 최대 폭 (px)


def current_size() -> str:
    v = load_settings().get(SIZE_KEY)
    return v if v in PRESETS else DEFAULT_SIZE


def save_size(size: str) -> None:
    if size not in PRESETS:
        raise ValueError(size)
    update_settings(**{SIZE_KEY: size})


def _font(root, name: str):
    try:
        return tkfont.nametofont(name, root=root)
    except tk.TclError:
        return None


def apply(root, size: str | None = None) -> str:
    """named font + ttk 스타일을 이 크기로. 열려 있는 모든 창에 바로 반영된다."""
    size = size if size in PRESETS else current_size()
    p = PRESETS[size]
    # Font 객체를 root에 붙잡아 둔다: 파이썬 객체가 사라지면 Tk named font도 지워진다
    keep = getattr(root, "_pls_fonts", None)
    if keep is None:
        keep = {}
        root._pls_fonts = keep  # type: ignore[attr-defined]
    for name, role, bold in NAMED:
        opts = {"family": FAMILY, "size": p[role], "weight": "bold" if bold else "normal"}
        if name in keep:
            keep[name].configure(**opts)
        elif _font(root, name) is not None:
            keep[name] = tkfont.Font(root=root, name=name, exists=True)
            keep[name].configure(**opts)
        else:
            keep[name] = tkfont.Font(root=root, name=name, exists=False, **opts)
    for name, role in TK_DEFAULTS:
        f = _font(root, name)
        if f is not None:
            f.configure(family=FAMILY, size=p[role])
    body = _font(root, "PLS.Body")
    row = body.metrics("linespace") + 12 if body is not None else 28
    st = ttk.Style(root)
    pad = {NORMAL: (8, 4), LARGE: (12, 6), XLARGE: (14, 8)}[size]
    st.configure("TButton", font="PLS.Button", padding=pad)
    st.configure("Primary.TButton", font="PLS.ButtonBold", padding=(pad[0] + 6, pad[1] + 3))
    st.configure("Secondary.TButton", font="PLS.Button", padding=pad)
    st.configure("Danger.TButton", font="PLS.Button", padding=pad, foreground="#a01818")
    st.configure("TCheckbutton", font="PLS.Body")
    st.configure("TRadiobutton", font="PLS.Body")
    st.configure("TLabel", font="PLS.Body")
    st.configure("TLabelframe.Label", font="PLS.Strong")
    st.configure("TEntry", padding=4)
    st.configure("TCombobox", padding=3)
    st.configure("Treeview", font="PLS.Body", rowheight=row)
    st.configure("Treeview.Heading", font="PLS.Strong")
    st.configure("Title.TLabel", font="PLS.Title")
    st.configure("Section.TLabel", font="PLS.Section")
    st.configure("Strong.TLabel", font="PLS.Strong")
    st.configure("Hint.TLabel", font="PLS.Body", foreground=TEXT_COLOR_MUTED)
    # 콤보박스 펼침 목록도 같은 크기
    try:
        root.option_add("*TCombobox*Listbox.font", "PLS.Body")
        root.option_add("*Listbox.font", "PLS.Body")
        root.option_add("*Text.font", "PLS.Body")
    except tk.TclError:
        pass
    root._pls_theme_size = size  # type: ignore[attr-defined]
    return size


def ensure(widget) -> None:
    """창을 만들 때 호출: 이 Tk에 아직 테마가 없으면 저장된 크기로 적용 (있으면 그대로)."""
    root = widget._root() if hasattr(widget, "_root") else widget
    if getattr(root, "_pls_theme_size", None) is None or _font(root, "PLS.Body") is None:
        apply(root)


def change(root, size: str) -> str:
    """[가]/[가+]/[가++] 또는 Ctrl +/−: 저장하고 바로 적용."""
    save_size(size)
    return apply(root, size)


def step(root, delta: int) -> str:
    cur = getattr(root, "_pls_theme_size", None) or current_size()
    i = max(0, min(len(ORDER) - 1, ORDER.index(cur) + delta))
    return change(root, ORDER[i])


def size_of(name: str, root=None) -> int:
    f = tkfont.nametofont(name, root=root)
    return int(f.cget("size"))
