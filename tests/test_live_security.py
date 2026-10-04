import logging
from pathlib import Path

import pytest

from app.live_core import build_live_command, build_output_url, describe_live_command, prepare_live
from app.live_profile import (
    MASK, LiveConfig, LiveConfigError, MemoryStreamKeyStore, mask_secret, redact,
)

# 테스트 전용 가짜 key (실제 Stream Key 아님)
FAKE_KEY = "test-fake-key0-0000-zzzz"
INGEST = "rtmps://a.rtmps.youtube.com:443/live2"


def cfg(**kw):
    base = dict(input_path=Path("CHILI_LAB_EP001.mp4"), ingest_url=INGEST, stream_key=FAKE_KEY)
    base.update(kw)
    return LiveConfig(**base)


def test_mask_secret_partial_and_full():
    assert mask_secret("abcd-efgh-ijkl-mnop") == "abcd••••••••mnop"
    assert mask_secret("short") == MASK
    assert mask_secret("") == ""
    assert mask_secret(None) == ""
    assert "efgh" not in mask_secret("abcd-efgh-ijkl-mnop")


def test_redact_removes_secret():
    assert redact(f"error at {INGEST}/{FAKE_KEY}: I/O", [FAKE_KEY]) == f"error at {INGEST}/{MASK}: I/O"
    assert redact("nothing", [None, ""]) == "nothing"


def test_key_not_in_repr_or_str():
    c = cfg()
    assert FAKE_KEY not in repr(c)
    assert FAKE_KEY not in str(c)
    assert FAKE_KEY not in repr(MemoryStreamKeyStore(FAKE_KEY))


def test_key_not_in_dry_run_log(caplog):
    cmd, text = prepare_live(ffmpeg=Path("ffmpeg"), config=cfg(), check_input=False)
    assert cmd[-1].endswith(FAKE_KEY)  # 실제 명령에는 key가 있어야 송출 가능
    assert FAKE_KEY not in text
    assert "stream_key=********" in text
    assert "input=CHILI_LAB_EP001.mp4" in text
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").info(describe_live_command(cmd, cfg()))
    assert FAKE_KEY not in caplog.text


@pytest.mark.parametrize("url", [
    "", "   ", None, "http://a.example.com/live2", "https://x/live2",
    "srt://x:9000", "rtmp://", "a.rtmp.youtube.com/live2", "rtmp://host/live2?x=1",
])
def test_invalid_ingest_rejected(url):
    with pytest.raises(LiveConfigError) as ei:
        build_output_url(url, FAKE_KEY)
    assert FAKE_KEY not in str(ei.value)


@pytest.mark.parametrize("key", ["", "   ", None, "has space", "a/b", "x?y", "tab\tkey"])
def test_invalid_key_rejected_without_leaking(key):
    with pytest.raises(LiveConfigError) as ei:
        build_output_url(INGEST, key)
    if key and key.strip():
        assert key not in str(ei.value)


def test_output_url_trailing_slash_and_whitespace():
    assert build_output_url(INGEST + "/", FAKE_KEY) == f"{INGEST}/{FAKE_KEY}"
    assert build_output_url(f"  {INGEST}//  ", f"  {FAKE_KEY} ") == f"{INGEST}/{FAKE_KEY}"
    assert build_output_url("RTMP://a.rtmp.youtube.com/live2", "k").startswith("RTMP://")


def test_invalid_config_errors_do_not_leak_key():
    with pytest.raises(LiveConfigError) as ei:
        build_live_command(ffmpeg=Path("ffmpeg"), config=cfg(fps=0))
    assert FAKE_KEY not in str(ei.value)
