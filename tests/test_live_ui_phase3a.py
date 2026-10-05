"""Gate 6: LIVE 창 GUI smoke — 단일/Playlist, 계속/보관 안전, 카운트다운, 다음 세션, Cloud 다중 업로드."""
import time
from pathlib import Path

import pytest

from cloud_fakes import FakeRemote, make_client

from app.cloud_client import CloudLiveController, CloudStatus
from app.cloud_model import REMOTE_MEDIA, save_cloud_profile
from app.live_controller import LiveController
from app.live_profile import MODE_COPY, LiveConfig
from app.live_ready import LiveReadyReport
from app.live_secrets import SessionStreamKeyStore
from app.live_session import ARCHIVE_SAFE_SECONDS, SESSION_ARCHIVE_SAFE
from app.live_supervisor import LiveState
from app.tooling import FfmpegExecutionGuard, KeepAwake


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
def quiet_dialogs(monkeypatch):
    from tkinter import messagebox
    shown = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: shown.append(a))
    return shown


def rep(path, fps=30.0):
    return LiveReadyReport(path=Path(path), width=1920, height=1080, fps=fps, duration=600.0, video_codec="h264",
                           audio_codec="aac", sample_rate=44100, channels=2)


class Clock:
    t = 5000.0

    def __call__(self):
        return self.t


class Proc:
    running = False
    rc = None

    def start(self):
        self.running = True

    def stop(self, *a, **k):
        self.running, self.rc = False, 0
        return 0

    def is_running(self):
        return self.running

    def return_code(self):
        return self.rc

    def uptime(self):
        return 0.0


def window(root, controller=None, cloud=None):
    from app.live_ui import LiveWindow
    return LiveWindow(root, tools=lambda: (None, None), controller=controller, key_store=SessionStreamKeyStore(),
                      cloud=cloud or CloudLiveController(lambda: None, poll_seconds=3600))


def fill_playlist(w, tmp_path, n=3, fps_of=lambda i: 30.0):
    paths = []
    for i in range(n):
        p = tmp_path / f"{i + 1:02d}_LIVE_READY.mp4"
        p.write_bytes(bytes([i]) * 3000)
        w.playlist.add(p)
        w.playlist.set_report(p, rep(p, fps_of(i)))
        paths.append(p)
    w._refresh_playlist()
    return paths


def test_defaults_single_and_continuous(root):
    w = window(root)
    root.update()
    assert w.source_mode.get() == "single" and w.session_mode.get() == "continuous"
    assert w.single_frame.winfo_manager() == "pack" and not w.playlist_frame.winfo_manager()
    assert not w.next_frame.winfo_manager()
    w.destroy()


def test_playlist_mode_table_summary_and_reorder(root, tmp_path):
    w = window(root)
    w.source_mode.set("playlist")
    w._on_source_mode()
    assert w.playlist_frame.winfo_manager() == "pack" and not w.single_frame.winfo_manager()
    paths = fill_playlist(w, tmp_path)
    rows = [w.ptree.item(x, "values") for x in w.ptree.get_children()]
    assert [r[1] for r in rows] == [p.name for p in paths]
    assert rows[0][2] == "00:10:00" and rows[0][3] == "1920×1080" and rows[0][5] == "✓ LIVE READY"
    s = w.playlist_summary.get()
    assert "총 3개" in s and "총 재생시간 00:30:00" in s and "✓ 모두 LIVE READY" in s and "✓ DIRECT COPY Playlist 가능" in s
    assert w.effective_mode() == MODE_COPY
    w.ptree.selection_set(w.ptree.get_children()[2])
    w._pl_move(-1)
    assert [p.name for p in w.playlist.paths] == [paths[0].name, paths[2].name, paths[1].name]
    w.ptree.selection_set(w.ptree.get_children()[0])
    w._pl_remove()
    assert len(w.playlist) == 2
    w._pl_clear()
    assert len(w.playlist) == 0 and "영상 추가" in w.playlist_summary.get()
    w.destroy()


def test_playlist_incompatible_message(root, tmp_path):
    w = window(root)
    w.source_mode.set("playlist")
    w._on_source_mode()
    fill_playlist(w, tmp_path, fps_of=lambda i: 29.97 if i == 1 else 30.0)
    s = w.playlist_summary.get()
    assert "2번 영상의 FPS가 29.97fps" in s and "LIVE READY 파일 만들기" in s
    status = [w.ptree.item(x, "values")[5] for x in w.ptree.get_children()]
    assert status[0] == "✓ LIVE READY" and status[1].startswith("✗")
    w.destroy()


def test_archive_countdown_and_next_session(root, monkeypatch):
    clock = Clock()

    def builder(ffmpeg, cfg):
        return Proc
    c = LiveController(guard=FfmpegExecutionGuard(), keep_awake=KeepAwake(setter=lambda f: None), clock=clock,
                       factory_builder=builder)
    w = window(root, controller=c)
    w.location.set("local")
    w.session_mode.set(SESSION_ARCHIVE_SAFE)
    cfg = LiveConfig(input_path=Path("a.mp4"), ingest_url="rtmps://x/live2", stream_key="dummy-ui3a", mode=MODE_COPY)
    c.start(ffmpeg=Path("ffmpeg"), config=cfg, session_limit=ARCHIVE_SAFE_SECONDS, background=False,
            playlist=[("A.mp4", 10.0), ("B.mp4", 10.0)])
    clock.t += 8 * 3600 + 31 * 60 + 22
    w._tick()
    assert w.st["session_time"].get() == "08:31:22 / 11:50:00"
    assert w.st["session_left"].get() == "03:18:38"
    clock.t = 5000.0 + ARCHIVE_SAFE_SECONDS - 590
    w._tick()
    assert "약 10분 후 보관 안전 종료" in w.st["session_left"].get()
    clock.t = 5000.0 + ARCHIVE_SAFE_SECONDS - 55
    w._tick()
    assert "약 1분 후 종료" in w.st["session_left"].get()
    assert not w.next_frame.winfo_manager()
    clock.t = 5000.0 + ARCHIVE_SAFE_SECONDS
    c.supervisor.poll()
    w._tick()
    assert w.st["state"].get().startswith("보관 안전 종료")
    assert w.next_frame.winfo_manager() == "pack" and str(w.btn_next_session.cget("state")) == "normal"
    assert "[다음 세션 시작]" in w.next_session_msg.get()
    assert str(w.btn_start.cget("state")) == "normal"  # 잠금 해제
    started = []
    monkeypatch.setattr(w, "_start", lambda: started.append(True))
    w.btn_next_session.invoke()
    assert started == [True]
    w.destroy()


def test_continuous_rows(root):
    clock = Clock()
    c = LiveController(guard=FfmpegExecutionGuard(), keep_awake=KeepAwake(setter=lambda f: None), clock=clock,
                       factory_builder=lambda f, cfg: Proc)
    w = window(root, controller=c)
    w.location.set("local")
    cfg = LiveConfig(input_path=Path("a.mp4"), ingest_url="rtmps://x/live2", stream_key="dummy-ui3b", mode=MODE_COPY)
    c.start(ffmpeg=Path("ffmpeg"), config=cfg, background=False)
    clock.t += 20 * 3600
    w._tick()
    assert w.st["session_time"].get() == "20:00:00 (계속 방송)" and w.st["session_left"].get() == "-"
    c.stop_blocking()
    w.destroy()


def test_cloud_status_playlist_and_session_complete(root):
    w = window(root)
    w.location.set("cloud")
    w.cloud.status = CloudStatus(reachable=True, installed=True, service_active=True, state="RUNNING",
                                 runtime_seconds=3600, media="03_LIVE_READY.mp4", mode="DIRECT COPY",
                                 playlist_count=8, current_playlist_index=2, playlist_round=4,
                                 session_mode="archive_safe", session_limit=ARCHIVE_SAFE_SECONDS,
                                 session_remaining=ARCHIVE_SAFE_SECONDS - 3600)
    w._refresh()
    assert w.st["state"].get() == "● CLOUD LIVE"
    assert w.st["media"].get() == "3/8 03_LIVE_READY.mp4" and w.st["playlist"].get() == "4회"
    assert w.st["session_time"].get() == "01:00:00 / 11:50:00" and w.st["session_left"].get() == "10:50:00"
    w.cloud.status = CloudStatus(reachable=True, installed=True, service_active=False, state="SESSION_LIMIT_REACHED",
                                 session_mode="archive_safe", session_limit=ARCHIVE_SAFE_SECONDS)
    w._refresh()
    assert w.st["state"].get() == "보관 안전 종료 · 다음 세션 대기"
    assert w.next_frame.winfo_manager() == "pack"
    w.destroy()


def test_cloud_multi_upload_from_ui(root, tmp_path, quiet_dialogs):
    remote = FakeRemote()
    client = make_client(tmp_path, remote)
    client.prepare()
    save_cloud_profile(client.profile)
    ctl = CloudLiveController(lambda: client, poll_seconds=3600)
    w = window(root, cloud=ctl)
    w.source_mode.set("playlist")
    w._on_source_mode()
    paths = fill_playlist(w, tmp_path)
    client.upload_media(paths[1])  # 1개는 이미 서버에 있음
    w._cloud_upload()
    end = time.monotonic() + 10
    while time.monotonic() < end and (ctl.busy or not any("개 완료" in str(a) for a in quiet_dialogs)):
        root.update()
        w._tick()
        time.sleep(0.05)
    assert any("3개 완료 — 새로 보냄 2개 / 이미 있음 1개" in str(a) for a in quiet_dialogs), quiet_dialogs
    assert sum(1 for k in remote.files if k.startswith(REMOTE_MEDIA)) == 3
    w.destroy()
