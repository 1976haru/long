"""EXE 화면 자가 점검 (`PlaylistLongVideoMaker_v0.3.exe --self-test 결과.json`).

배포한 EXE 안에서 초보자 화면(Welcome → 처음 설정 Wizard → 도움말 → 매뉴얼 파일 → 예약 업로드 → 채널 연결 → 댓글
→ LIVE → 예약 LIVE → 일본어 도우미)이
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
        # LIVE 창 (Playlist 일괄 LIVE READY 버튼) → 예약 LIVE 창 (초보자 빠른 예약 · Cloud 준비 단계) → 일본어 도우미
        for w in (getattr(app, "upload_win", None), getattr(app, "comment_win", None)):
            try:
                if w is not None and w.winfo_exists():
                    w.destroy()
            except Exception:
                pass
        try:
            app._open_live()
            lw = app._live_window()
            ok("live_window", lw is not None and lw.winfo_exists() and hasattr(lw, "btn_pl_fix_all"),
               lw.title() if lw is not None else "")
            # 여러 채널 Cloud LIVE: 채널 선택/관리 창 + EXE에 worker v4와 채널 서비스(template)가 들어 있는지
            from .cloud_model import worker_files
            from .live_channels_ui import LiveChannelsDialog
            files = worker_files()
            worker_v4 = 'WORKER_VERSION = "4"' in files["long_live_worker.py"].read_text(encoding="utf-8")
            dlg = LiveChannelsDialog(lw, store=lw.channels)
            ok("multi_channel", hasattr(lw, "cmb_channel") and dlg.winfo_exists() and worker_v4
               and files["long-live@.service"].is_file(), f"worker_v4={worker_v4}")
            dlg.destroy()
            # 초보자 LIVE 화면: 채널 A/B 카드 · 빠른 시작 · 준비 체크리스트 · 서버 주소 자동 (고급 모드는 꺼진 상태가 기본)
            lw._tick()
            beginner = (len(getattr(lw, "channel_cards", [])) == 2 and hasattr(lw, "btn_quick_both")
                        and len(getattr(lw, "check_vars", [])) == 6 and bool(lw.server_auto.winfo_manager())
                        and not lw.ent_custom.winfo_ismapped())
            ok("live_beginner", beginner, lw.title_text.get())
            sw = app._open_live_schedule()
            ok("live_schedule", sw.winfo_exists() and len(sw.step_vars) == 12 and sw.winfo_class() == "Toplevel",
               sw.title())
            jp = app._open_japanese_helper()
            ok("japanese_assistant", jp.winfo_exists(), jp.title())
        except Exception as e:  # 한 화면 오류로 결과 JSON이 안 남는 일이 없게
            ok("live_steps", False, repr(e)[:300])
        app.after(step_ms, s6)

    def s6():
        for w in (getattr(app, "jp_helper_win", None), getattr(app, "live_schedule_win", None), app._live_window()):
            try:
                if w is not None and w.winfo_exists():
                    w.destroy()
            except Exception:
                pass
        results["seconds"] = round(time.monotonic() - t0, 1)
        results["all_ok"] = all(v["ok"] for v in results.values() if isinstance(v, dict))
        Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        app._finish_close()

    app.after(step_ms, s1)
