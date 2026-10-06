"""v1.3.1 가독성: 공통 글꼴 테마(보통/크게/아주 크게), 초보자 기본 '크게', 저장·즉시 적용, Primary/Secondary/Danger 버튼,
Treeview 행 높이, 작은 화면 스크롤, 모든 주요 창이 같은 글꼴 체계를 쓰는지. 기능은 바꾸지 않는다."""
import time
from pathlib import Path
from tkinter import font as tkfont

import pytest

from app import ui_theme
from app.settings import load_settings
from app.youtube_accounts import ChannelProfile, ProfileStore, new_profile_id
from test_youtube_upload_queue import JP, KR, Env
from youtube_fakes import FakeYouTube


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from tkinter import messagebox
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


def font_size(widget) -> int:
    """위젯이 실제로 쓰는 글자 크기 (font 옵션 → 없으면 ttk 스타일 → 기본 글꼴)."""
    from tkinter import TclError, ttk
    try:
        f = widget.cget("font")
    except TclError:  # ttk.Button 등은 font 옵션이 없고 스타일을 쓴다
        f = ""
    if not f:
        style = ""
        try:
            style = str(widget.cget("style"))
        except TclError:
            pass
        f = ttk.Style(widget).lookup(style or widget.winfo_class(), "font") or "TkDefaultFont"
    try:
        return int(tkfont.nametofont(str(f), root=widget).cget("size"))
    except Exception:
        return int(tkfont.Font(root=widget, font=f).actual("size"))


def test_default_is_large_and_presets(root):
    assert ui_theme.current_size() == ui_theme.LARGE  # 초보자 기본 = 크게
    ui_theme.ensure(root)
    assert ui_theme.size_of("PLS.Body", root) == 12 and ui_theme.size_of("TkDefaultFont", root) == 12
    assert ui_theme.size_of("PLS.Hero", root) >= 17 and ui_theme.size_of("PLS.Section", root) == 14
    ui_theme.apply(root, ui_theme.NORMAL)
    assert ui_theme.size_of("PLS.Body", root) == 10
    ui_theme.apply(root, ui_theme.XLARGE)
    assert ui_theme.size_of("PLS.Body", root) == 14 and ui_theme.size_of("PLS.Hero", root) == 20
    assert ui_theme.size_of("PLS.Button", root) == 13


def test_size_persists_and_steps(root):
    ui_theme.change(root, ui_theme.XLARGE)
    assert load_settings()["ui_font_size"] == "xlarge" and ui_theme.current_size() == ui_theme.XLARGE
    assert ui_theme.step(root, 1) == ui_theme.XLARGE  # 더 커지지 않음
    assert ui_theme.step(root, -1) == ui_theme.LARGE and ui_theme.step(root, -1) == ui_theme.NORMAL
    assert ui_theme.step(root, -1) == ui_theme.NORMAL
    assert load_settings()["ui_font_size"] == "normal"
    with pytest.raises(ValueError):
        ui_theme.save_size("huge")


def test_treeview_rowheight_and_button_styles(root):
    from tkinter import ttk
    ui_theme.apply(root, ui_theme.LARGE)
    st = ttk.Style(root)
    rh_large = int(st.lookup("Treeview", "rowheight"))
    assert rh_large >= 26 and st.lookup("Treeview.Heading", "font") == "PLS.Strong"
    assert st.lookup("Primary.TButton", "font") == "PLS.ButtonBold" and st.lookup("Secondary.TButton", "font") == "PLS.Button"
    assert str(st.lookup("Danger.TButton", "foreground")) != ""
    ui_theme.apply(root, ui_theme.XLARGE)
    assert int(st.lookup("Treeview", "rowheight")) > rh_large


def test_wizard_readable_styles_and_primary_next(root):
    from app.help_ui import SetupWizard
    w = SetupWizard(root, profiles=ProfileStore(), ffmpeg_finder=lambda: ("f", "p"), internet=lambda: True)
    assert str(w.lbl_step.cget("font")) == "PLS.Hero" and ui_theme.size_of("PLS.Hero", root) >= 17
    assert str(w.btn_next.cget("style")) == "Primary.TButton" and str(w.btn_back.cget("style")) == "Secondary.TButton"
    w.next()
    w.choose_preset("kr")
    styles = {str(b.cget("text")): str(b.cget("style")) for b in walk(w) if b.winfo_class() == "TButton"}
    assert styles["Google 계정 처음 연결하기"] == "Primary.TButton" and styles["한국 채널"] == "Primary.TButton"
    labels = [x for x in walk(w.body) if x.winfo_class() == "TLabel"]
    assert all(font_size(x) >= 11 for x in labels)  # 본문 최소 11~12pt (크게 기준 12)
    assert any(str(x.cget("font")) == "PLS.Section" and "2. Google 계정 연결 준비" in str(x.cget("text")) for x in labels)
    w.destroy()


def test_font_change_keeps_form_values_and_applies_live(root, fake, tmp_path):
    from app.youtube_upload_ui import MultiChannelUploadWindow
    ps = ProfileStore()
    kr = ps.add(ChannelProfile(new_profile_id(), "한국 시니어", channel_id=KR["id"], channel_title=KR["title"]))
    env = Env(fake, ps, kr, kr)
    w = MultiChannelUploadWindow(root, upload_queue=env.queue(ps), clock=env.clock)
    w.pub_time.set("21:00")
    w.tags.set("샹송, 가을")
    before = font_size(w.btn_start)
    ui_theme.change(root, ui_theme.XLARGE)  # 다시 시작하지 않고 바로
    root.update_idletasks()
    assert font_size(w.btn_start) > before
    assert w.pub_time.get() == "21:00" and w.tags.get() == "샹송, 가을"  # 입력값 그대로
    assert str(w.btn_start.cget("style")) == "Primary.TButton"
    w.destroy()


@pytest.mark.parametrize("which", ["channels", "upload", "comments", "help", "assistant", "preview"])
def test_all_windows_use_theme(root, fake, which, tmp_path):
    from app.help_ui import GoogleConnectionAssistant, HelpWindow
    from app.youtube_channels_ui import ChannelManagerWindow
    from app.youtube_comments import CommentService
    from app.youtube_comments_ui import CommentManagerWindow
    from app.youtube_upload_ui import MultiChannelUploadWindow
    ps = ProfileStore()
    kr = ps.add(ChannelProfile(new_profile_id(), "한국 시니어", channel_id=KR["id"], channel_title=KR["title"]))
    env = Env(fake, ps, kr, kr)
    if which == "channels":
        w = ChannelManagerWindow(root, profiles=ps)
    elif which == "upload":
        w = MultiChannelUploadWindow(root, upload_queue=env.queue(ps), clock=env.clock)
    elif which == "comments":
        w = CommentManagerWindow(root, service=CommentService(ps, api_factory=env.api_factory), profile_id=kr.profile_id)
    elif which == "help":
        w = HelpWindow(root)
        assert str(w.text.cget("font")) == "PLS.Body" or font_size(w.text) >= 12
    elif which == "assistant":
        w = GoogleConnectionAssistant(root, open_url=lambda u: None)
    else:
        up = MultiChannelUploadWindow(root, upload_queue=env.queue(ps), clock=env.clock)
        up.add_videos([str(_mp4(tmp_path / "a.mp4"))])
        w = up.preview()
    root.update_idletasks()
    sizes = [font_size(x) for x in walk(w) if x.winfo_class() in ("TLabel", "TButton", "TCheckbutton", "TRadiobutton")]
    assert sizes and min(sizes) >= 11, (which, min(sizes))  # 특정 창만 작게 보이지 않음
    trees = [x for x in walk(w) if x.winfo_class() == "Treeview"]
    from tkinter import ttk
    assert all(int(ttk.Style(root).lookup("Treeview", "rowheight")) >= 26 for _ in trees)
    w.destroy()


def _mp4(p):
    p.write_bytes(b"x" * 3000)
    return p


@pytest.mark.parametrize("size", ["large", "xlarge"])
def test_small_screen_800px_scrolls_in_large_fonts(root, fake, size):
    from app.help_ui import SetupWizard
    from app.youtube_upload_ui import MultiChannelUploadWindow
    ui_theme.change(root, size)
    ps = ProfileStore()
    kr = ps.add(ChannelProfile(new_profile_id(), "한국 시니어", channel_id=KR["id"], channel_title=KR["title"]))
    env = Env(fake, ps, kr, kr)
    for make, target in ((lambda: MultiChannelUploadWindow(root, upload_queue=env.queue(ps), clock=env.clock), "btn_start"),
                         (lambda: SetupWizard(root, profiles=ps, ffmpeg_finder=lambda: ("f", "p"), internet=lambda: True),
                          "btn_next")):
        w = make()
        w.geometry("1000x800+0+0")
        w.deiconify()
        w.update()
        if hasattr(w, "scroll") and target == "btn_start":
            w.scroll.canvas.yview_moveto(1.0)
            w.update()
            cv = w.scroll.canvas
            top, bottom = cv.winfo_rooty(), cv.winfo_rooty() + cv.winfo_height()
        else:
            top, bottom = w.winfo_rooty(), w.winfo_rooty() + w.winfo_height()
        b = getattr(w, target)
        assert top <= b.winfo_rooty() and b.winfo_rooty() + b.winfo_height() <= bottom + 1, (size, target)
        assert max(b.winfo_height(), b.winfo_reqheight()) >= 30  # 누르기 쉬운 높이
        w.destroy()


def test_main_window_font_buttons_and_ctrl_keys(tmp_path, monkeypatch):
    import app.ui as ui
    monkeypatch.setattr(ui, "discover_ffmpeg", lambda r: None)
    try:
        a = ui.MainWindow(tmp_path)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    try:
        a.withdraw()
        assert set(a.font_buttons) == {"normal", "large", "xlarge"}
        assert [str(b.cget("text")) for b in a.font_buttons.values()] == ["가", "가+", "가++"]
        assert a.font_size_text.get() == "(크게)" and "pressed" in a.font_buttons["large"].state()
        a.font_buttons["xlarge"].invoke()
        assert ui_theme.size_of("PLS.Body", a) == 14 and load_settings()["ui_font_size"] == "xlarge"
        assert a.font_size_text.get() == "(아주 크게)"
        a._step_font(-1)
        assert a.font_size_text.get() == "(크게)"
        assert a.bind_all("<Control-plus>") and a.bind_all("<Control-minus>")
    finally:
        a._finish_close()
