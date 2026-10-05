import hashlib
import json
import threading
from pathlib import Path

import pytest

from cloud_fakes import FakeRemote, make_client

from app.cloud_client import CloudError, CloudLiveController, friendly_ssh_error
from app.cloud_model import CLOUD_UNAVAILABLE, REMOTE_MEDIA

FAKE_KEY = "dummy-cloud-0000-not-real"


def media(tmp_path, name="CHILI LAB EP001_LIVE_READY.mp4", data=b"fake mp4 data " * 1000):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_prepare_runs_six_steps_in_order(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    steps = []
    env = c.prepare(lambda i, n: steps.append((i, n)))
    assert [s[0] for s in steps] == [1, 2, 3, 4, 5, 6]
    assert [s[1] for s in steps] == ["서버 연결", "환경 확인", "FFmpeg 준비", "LIVE Worker 설치", "자동 복구 설정", "완료"]
    assert remote.installed
    assert env["shape"] == "VM.Standard.A1.Flex"
    assert "Oracle Console" in env["free_notice"]
    uploaded = {k.rsplit("/", 1)[1] for k in remote.files if k.startswith("/tmp/long-live-install-")}
    assert uploaded == {"long_live_worker.py", "long-live.service", "install.sh", "uninstall.sh"}
    for k, v in remote.files.items():
        if k.startswith("/tmp/long-live-install-"):
            assert b"\r\n" not in v  # Linux 줄바꿈


def test_prepare_rejects_non_ubuntu_and_no_sudo(tmp_path):
    with pytest.raises(CloudError, match="Ubuntu"):
        make_client(tmp_path, FakeRemote(os_id="ol")).prepare()
    with pytest.raises(CloudError, match="sudo"):
        make_client(tmp_path, FakeRemote(sudo=False)).prepare()


def test_unreachable_server_friendly_message(tmp_path):
    c = make_client(tmp_path, FakeRemote(reachable=False))
    with pytest.raises(CloudError) as e:
        c.check_connection()
    assert "22번 포트" in str(e.value)
    st = c.status()
    assert not st.reachable and not st.live


@pytest.mark.parametrize("err,expect", [
    ("Permission denied (publickey).", "사용자 이름"),
    ("WARNING: UNPROTECTED PRIVATE KEY FILE!", "권한"),
    ("Host key verification failed.", "식별"),
    ("ssh: Could not resolve hostname x", "주소"),
    ("sudo: a password is required", "sudo"),
    ("weird", CLOUD_UNAVAILABLE),
])
def test_friendly_errors(err, expect):
    assert expect in friendly_ssh_error(err)


def test_upload_part_verify_atomic_rename(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    src = media(tmp_path)
    progress = []
    r = c.upload_media(src, progress_cb=lambda f, t: progress.append(f))
    assert not r.skipped
    assert r.remote_name == "CHILI_LAB_EP001_LIVE_READY_" + r.remote_name.split("_")[-1]
    final = f"{REMOTE_MEDIA}/{r.remote_name}"
    assert remote.files[final] == src.read_bytes()
    assert f"{final}.part" not in remote.files
    cmds = [c_["args"][-1] for c_ in remote.calls]
    cat_i = next(i for i, x in enumerate(cmds) if x.startswith("cat > ") and ".part" in x)
    mv_i = next(i for i, x in enumerate(cmds) if x.startswith("mv -f --"))
    assert cat_i < mv_i
    assert progress[-1] == 1.0 and progress == sorted(progress)


def test_upload_skipped_when_sha_matches(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    src = media(tmp_path)
    first = c.upload_media(src)
    n = len(remote.calls)
    second = c.upload_media(src)
    assert second.skipped and second.sha256 == hashlib.sha256(src.read_bytes()).hexdigest()
    assert not any(x["input"] == "<stream>" for x in remote.calls[n:])  # 업로드 생략
    assert first.remote_name == second.remote_name


def test_upload_sha_mismatch_keeps_existing_media(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    src = media(tmp_path)
    good = c.upload_media(src)
    final = f"{REMOTE_MEDIA}/{good.remote_name}"
    before = remote.files[final]
    src.write_bytes(b"new content " * 999)
    remote.corrupt_upload = True
    with pytest.raises(CloudError, match="SHA256"):
        c.upload_media(src)
    assert remote.files[final] == before  # 기존 파일 손상 없음
    assert f"{final}.part" not in remote.files  # .part 정리


def test_upload_broken_connection_cleans_part(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    remote.fail_upload = True
    src = media(tmp_path, data=b"x" * (3 * 1024 * 1024))
    with pytest.raises(CloudError):
        c.upload_media(src)
    assert not any(k.endswith(".part") for k in remote.files)


def test_upload_cancel(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    ev = threading.Event()
    ev.set()
    with pytest.raises(CloudError, match="중지"):
        c.upload_media(media(tmp_path), cancel=ev)
    assert not any(k.endswith(".part") for k in remote.files)


def test_upload_insufficient_disk(tmp_path):
    remote = FakeRemote()
    remote.free = 10
    with pytest.raises(CloudError, match="저장 공간"):
        make_client(tmp_path, remote).upload_media(media(tmp_path))


def test_start_live_key_only_via_stdin(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    c.prepare()
    up = c.upload_media(media(tmp_path))
    st = c.start_live(remote_media=up.remote_name, ingest_url="rtmps://a.rtmps.youtube.com:443/live2",
                      stream_key=FAKE_KEY, sleep=lambda s: None)
    assert st.live and st.state == "RUNNING"
    assert remote.key == FAKE_KEY + "\n"
    cfg = json.loads(remote.config)
    # schema v2. 단일 영상은 media가 문자열이라 기존(v1) worker도 그대로 읽는다.
    assert {k: cfg[k] for k in ("media", "ingest_url", "mode")} == \
        {"media": up.remote_name, "ingest_url": "rtmps://a.rtmps.youtube.com:443/live2", "mode": "copy"}
    assert cfg["schema_version"] == 2 and cfg["play_mode"] == "sequential" and cfg["session_mode"] == "continuous"
    for call in remote.calls:
        assert all(FAKE_KEY not in a for a in call["args"])  # 명령줄 인자에 key 없음
        if call["input"] != FAKE_KEY + "\n":
            assert FAKE_KEY not in str(call["input"])
    assert all(FAKE_KEY not in line for line in c.detail)
    c.stop_live()
    assert not remote.active and not remote.enabled


def test_start_live_requires_worker_and_media(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    with pytest.raises(CloudError, match="처음 설정"):
        c.start_live(remote_media="a.mp4", ingest_url="rtmps://x/live2", stream_key=FAKE_KEY)
    remote.installed = True
    with pytest.raises(CloudError, match="영상"):
        c.start_live(remote_media="a.mp4", ingest_url="rtmps://x/live2", stream_key=FAKE_KEY)
    assert remote.key is None  # 검사 실패 시 key를 서버에 쓰지 않음


def test_status_and_bounded_logs(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    c.prepare()
    st = c.status()
    assert st.reachable and st.installed and not st.live
    logs = c.logs(500)
    assert len(logs) == 50
    for i in range(1000):
        c._note(f"line {i}")
    assert len(c.detail) == 200


def test_controller_async_ops_and_events(tmp_path):
    remote = FakeRemote()
    c = make_client(tmp_path, remote)
    c.prepare()
    ctl = CloudLiveController(lambda: c)
    assert ctl.start_async(local=media(tmp_path), ingest_url="rtmps://a.rtmps.youtube.com:443/live2", stream_key=FAKE_KEY)
    ctl._op.join(10)
    evs = ctl.drain_events()
    assert evs[-1][:3] == ("op", "start", True)
    assert ctl.cloud_live_active
    assert FAKE_KEY not in repr(evs)
    ctl.stop_async()
    ctl._op.join(10)
    assert not ctl.cloud_live_active
