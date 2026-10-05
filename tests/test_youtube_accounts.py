"""Gate C/D: Channel Profile 저장/검증, 프로필별 OAuth token(DPAPI) 분리, 업로드 전 채널 확인 (fake Google/YouTube만)."""
import os
import threading
import urllib.parse
import urllib.request

import pytest

from app.settings import load_settings
from app.youtube_accounts import (
    ChannelMismatchError, ChannelProfile, ProfileError, ProfileStore, build_profile_api, connect_profile,
    disconnect_profile, new_profile_id, token_store_for, verify_channel,
)
from app.youtube_api import YouTubeApiClient
from app.youtube_oauth import OAuthClient, OAuthError
from youtube_fakes import FakeYouTube

KR = {"id": "UCkr000000000000000000KR", "title": "한국 시니어"}
JP = {"id": "UCjp000000000000000000JP", "title": "CHILI LAB"}
JP2 = {"id": "UCjp000000000000000chanson", "title": "日本シャンソン"}
DPAPI = pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")


@pytest.fixture
def fake():
    f = FakeYouTube()
    yield f
    f.close()


def client(fake, cid="cid-kr.apps.googleusercontent.com"):
    return OAuthClient(cid, "GOCSPX-fake-secret-0000", token_uri=fake.token_uri)


def consent_browser(code):
    def open_browser(url):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}

        def go():
            urllib.request.urlopen(q["redirect_uri"] + "/?" + urllib.parse.urlencode({"code": code, "state": q["state"]}),
                                   timeout=10).read()
        threading.Thread(target=go, daemon=True).start()
    return open_browser


def kr_profile(**kw):
    return ChannelProfile(new_profile_id(), "🇰🇷 한국 시니어", language="ko", timezone="Asia/Seoul", **kw)


def jp_profile(**kw):
    return ChannelProfile(new_profile_id(), "🇯🇵 CHILI LAB", language="ja", timezone="Asia/Tokyo", category_id="24", **kw)


# ---------------- 프로필 저장 ----------------

def test_profile_add_save_load_rename():
    ps = ProfileStore()
    kr = ps.add(kr_profile())
    jp = ps.add(jp_profile())
    loaded = {p.alias: p for p in ProfileStore().all()}
    assert loaded["🇰🇷 한국 시니어"].language == "ko" and loaded["🇰🇷 한국 시니어"].timezone == "Asia/Seoul"
    assert loaded["🇯🇵 CHILI LAB"].language == "ja" and loaded["🇯🇵 CHILI LAB"].timezone == "Asia/Tokyo"
    assert loaded["🇯🇵 CHILI LAB"].category_id == "24" and loaded["🇰🇷 한국 시니어"].category_id == "10"
    assert kr.privacy == "private" and not kr.made_for_kids  # 기본값
    kr.alias = "🇰🇷 한국 시니어 2"
    ps.save(kr)
    assert ps.get(kr.profile_id).alias == "🇰🇷 한국 시니어 2" and len(ps.all()) == 2
    assert ps.get(jp.profile_id).alias == "🇯🇵 CHILI LAB"


def test_profile_rejects_duplicates_and_bad_values():
    ps = ProfileStore()
    kr = ps.add(kr_profile(channel_id=KR["id"]))
    with pytest.raises(ProfileError, match="같은 ID"):
        ps.add(ChannelProfile(kr.profile_id, "다른 별칭"))
    with pytest.raises(ProfileError, match="별칭"):
        ps.add(ChannelProfile(new_profile_id(), kr.alias))
    with pytest.raises(ProfileError, match="이미"):
        ps.add(jp_profile(channel_id=KR["id"]))  # 같은 채널을 두 프로필에
    for bad in (dict(alias=""), dict(alias="x", timezone="Mars/Base"), dict(alias="x", language="xx"),
                dict(alias="x", category_id="music"), dict(alias="x", privacy="secret")):
        with pytest.raises(ProfileError):
            ChannelProfile(new_profile_id(), **bad).validate()
    with pytest.raises(ProfileError):
        ChannelProfile("../../evil", "x").validate()
    with pytest.raises(ProfileError):
        token_store_for("../evil")


def test_settings_keep_oauth_json_path_only(tmp_path):
    cf = tmp_path / "client_secret_kr.json"
    cf.write_text('{"installed": {"client_id": "x", "client_secret": "GOCSPX-real-looking"}}', encoding="utf-8")
    ps = ProfileStore()
    ps.add(kr_profile(client_file=str(cf)))
    row = load_settings()["channel_profiles"][0]
    assert row["client_file"] == str(cf)
    assert set(row) == set(ChannelProfile.__dataclass_fields__)
    assert "GOCSPX" not in (tmp_path / "_settings" / "settings.json").read_text(encoding="utf-8")


# ---------------- OAuth / token 분리 ----------------

@DPAPI
def test_connect_profiles_tokens_separate_and_encrypted(fake, _isolated_settings):
    ps = ProfileStore()
    kr, jp = ps.add(kr_profile()), ps.add(jp_profile())
    fake.code_refresh = {"code-kr": "refresh-KR-plain-0001", "code-jp": "refresh-JP-plain-0002"}
    fake.refresh_channels = {"refresh-KR-plain-0001": KR, "refresh-JP-plain-0002": JP}
    kr = connect_profile(ps, kr, "C:/x/client_kr.json", open_browser=consent_browser("code-kr"), client=client(fake),
                         api_base=fake.api_base, timeout=10)
    jp = connect_profile(ps, jp, "C:/x/client_jp.json", open_browser=consent_browser("code-jp"),
                         client=client(fake, "cid-jp.apps.googleusercontent.com"), api_base=fake.api_base, timeout=10)
    assert (kr.channel_id, jp.channel_id) == (KR["id"], JP["id"])
    assert ps.is_connected(kr) and ps.is_connected(jp)
    f_kr = _isolated_settings / f"youtube_token_{kr.profile_id}.dat"
    f_jp = _isolated_settings / f"youtube_token_{jp.profile_id}.dat"
    assert f_kr.is_file() and f_jp.is_file() and f_kr != f_jp
    assert not (_isolated_settings / "youtube_token.dat").exists()  # LIVE 자동 세션 token과도 분리
    for f in (f_kr, f_jp):
        raw = f.read_bytes()
        assert b"refresh-" not in raw and b"GOCSPX" not in raw  # DPAPI 암호문만
    assert ps.token_store(kr.profile_id).load()["refresh_token"] == "refresh-KR-plain-0001"
    assert ps.token_store(jp.profile_id).load()["refresh_token"] == "refresh-JP-plain-0002"
    text = (_isolated_settings / "settings.json").read_text(encoding="utf-8")
    assert "refresh-" not in text and "fake-access" not in text and "GOCSPX" not in text
    # 삭제 정책: 프로필 삭제 → 그 프로필 token 파일만 삭제
    ps.delete(kr.profile_id)
    assert not f_kr.exists() and f_jp.exists() and ps.get(kr.profile_id) is None


@DPAPI
def test_connect_rejects_channel_already_used_or_changed(fake):
    ps = ProfileStore()
    kr = ps.add(kr_profile())
    other = ps.add(jp_profile())
    fake.code_refresh = {"c1": "r1", "c2": "r2"}
    fake.refresh_channels = {"r1": KR, "r2": KR}
    kr = connect_profile(ps, kr, "a.json", open_browser=consent_browser("c1"), client=client(fake),
                         api_base=fake.api_base, timeout=10)
    with pytest.raises(ProfileError, match="이미"):
        connect_profile(ps, other, "b.json", open_browser=consent_browser("c2"), client=client(fake),
                        api_base=fake.api_base, timeout=10)
    assert not ps.token_store(other.profile_id).has_saved()  # 실패하면 token을 저장하지 않음
    fake.code_refresh["c3"] = "r3"
    fake.refresh_channels["r3"] = JP
    with pytest.raises(ChannelMismatchError):  # 이미 KR로 연결된 프로필에 JP 계정
        connect_profile(ps, kr, "a.json", open_browser=consent_browser("c3"), client=client(fake),
                        api_base=fake.api_base, timeout=10)
    assert ps.token_store(kr.profile_id).load()["refresh_token"] == "r1"
    disconnect_profile(ps, kr)
    assert not ps.token_store(kr.profile_id).has_saved() and ps.get(kr.profile_id).channel_id == ""


# ---------------- 업로드 전 채널 확인 ----------------

def api_as(fake, channel, token):
    fake.valid_tokens.add(token)
    fake.token_channels[token] = channel
    return YouTubeApiClient(lambda force_refresh=False: token, base_url=fake.api_base, sleep=lambda s: None)


@pytest.mark.parametrize("expected,actual,ok", [(KR, KR, True), (KR, JP, False), (JP, KR, False), (JP, JP2, False)])
def test_verify_channel_before_upload(fake, expected, actual, ok):
    api = api_as(fake, actual, "tok-" + actual["id"])
    if ok:
        assert verify_channel(api, expected["id"]).id == expected["id"]
    else:
        with pytest.raises(ChannelMismatchError) as ei:
            verify_channel(api, expected["id"])
        assert ei.value.kind == "config" and not ei.value.retryable
        assert expected["id"] in str(ei.value) and actual["id"] in str(ei.value)
    assert api.calls == ["channels.list"]
    assert fake.calls[-1][2]["mine"] == "true"


def test_verify_channel_without_expected_blocks(fake):
    with pytest.raises(ChannelMismatchError):
        verify_channel(api_as(fake, KR, "t"), "")


@DPAPI
def test_channel_rechecked_after_oauth_refresh(fake):
    """같은 프로필 token이 refresh 후 다른 채널 계정이 되면(재연결 실수 등) 다음 확인에서 차단."""
    ps = ProfileStore()
    kr = ps.add(kr_profile(channel_id=KR["id"], client_file="x.json"))
    c = client(fake)
    ps.token_store(kr.profile_id).save("refresh-kr", client_id=c.client_id)
    fake.refresh_channels = {"refresh-kr": KR}
    api = build_profile_api(kr, ps.token_store(kr.profile_id), client=c, base_url=fake.api_base, sleep=lambda s: None)
    assert verify_channel(api, KR["id"]).id == KR["id"]
    fake.refresh_channels["refresh-kr"] = JP  # 이후 refresh로 받은 access token은 JP 계정
    api2 = build_profile_api(kr, ps.token_store(kr.profile_id), client=c, base_url=fake.api_base, sleep=lambda s: None)
    with pytest.raises(ChannelMismatchError):
        verify_channel(api2, KR["id"])
    # 같은 client에서 401 → 강제 refresh 후에도 채널 확인 결과는 새 token 기준
    fake.valid_tokens.clear()
    with pytest.raises(ChannelMismatchError):
        verify_channel(api2, KR["id"])
    assert [g for g, _ in fake.token_log].count("refresh_token") == 3


@DPAPI
def test_profile_token_from_other_client_is_refused(fake):
    ps = ProfileStore()
    kr = ps.add(kr_profile(channel_id=KR["id"], client_file="x.json"))
    ps.token_store(kr.profile_id).save("refresh-kr", client_id="some-other-client")
    api = build_profile_api(kr, ps.token_store(kr.profile_id), client=client(fake), base_url=fake.api_base,
                            sleep=lambda s: None)
    with pytest.raises(Exception) as ei:
        api.get_channel()
    assert "다른 Google 연결 파일" in str(ei.value)
    assert fake.token_log == []  # 다른 client의 refresh token을 Google로 보내지도 않음


def test_build_profile_api_requires_connection():
    ps = ProfileStore()
    kr = ps.add(kr_profile())
    with pytest.raises(OAuthError, match="연결"):
        build_profile_api(kr, ps.token_store(kr.profile_id), client=OAuthClient("c", "s"))
