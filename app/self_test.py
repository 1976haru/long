"""EXE 화면 자가 점검 (`PlaylistLongVideoMaker_v0.3.exe --self-test 결과.json`).

배포한 EXE 안에서 초보자 화면(Welcome → 처음 설정 Wizard → 도움말 → 매뉴얼 파일 → 예약 업로드 → 채널 연결)이
실제로 열리는지 차례로 확인하고 결과를 JSON으로 남긴 뒤 종료한다. 인터넷/YouTube/Google에는 접속하지 않는다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def run(app, out_path: str, step_ms: int = 1200) -> None:
    from . import diagnostics
    diagnostics._internet = lambda timeout=2.0: False  # 자가 점검은 네트워크를 쓰지 않는다
    t0 = time.monotonic()
    results: dict[str, object] = {}

    def ok(name, cond, detail=""):
        results[name] = {"ok": bool(cond), "detail": str(detail)}

    def s1():
        w = app._open_welcome()
        ok("welcome", w.winfo_exists(), w.title())
        app.after(step_ms, s2)

    def s2():
        app.welcome_win.destroy()
        wz = app._open_wizard()
        wz.next()
        p = wz.choose_preset("kr")
        ok("setup_wizard", wz.winfo_exists() and "STEP 2" in wz.step_title.get() and p.timezone == "Asia/Seoul",
           wz.step_title.get())
        app.after(step_ms, s3)

    def s3():
        app.wizard_win.destroy()
        h = app._open_help("trouble")
        from . import help_content
        ok("help", h.winfo_exists() and len(h.topics) == len(help_content.TOPICS) >= 10, h.title())
        m = diagnostics.manual_path()
        ok("manual", m is not None and m.is_file() and "Manual version" in m.read_text(encoding="utf-8"), m)
        report = app._diagnostics()
        ok("diagnostics", "Manual" in report, len(report))
        app.after(step_ms, s4)

    def s4():
        app.help_win.destroy()
        u = app._open_upload()
        ok("upload_window", u.winfo_exists() and len(u.step_labels) == 5, u.title())
        cm = u.open_channels()
        ok("channel_manager", cm.winfo_exists(), cm.title())
        c = app._open_comments()
        ok("comment_manager", c.winfo_exists(), c.title())
        from . import ui_theme
        body = ui_theme.size_of("PLS.Body", app)
        ok("readable_font", body >= 12 and getattr(app, "_pls_theme_size", "") == ui_theme.LARGE,
           f"{getattr(app, '_pls_theme_size', '')} body={body}pt")
        app.after(step_ms, s5)

    def s5():
        results["seconds"] = round(time.monotonic() - t0, 1)
        results["all_ok"] = all(v["ok"] for v in results.values() if isinstance(v, dict))
        Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        app._finish_close()

    app.after(step_ms, s1)
