"""통합 02 (C04+C05) — ⑤ 배경 영상 검색·다운로드.

네트워크는 모두 가짜(opener)로 대체한다 (실제 제공처 호출 없음). FFmpeg가 있으면 실제 썸네일 변환·재생·ffprobe 검사.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import stock_download as D
from app import stock_net as N
from app import stock_rights as R
from app import stock_sources as S

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FFPLAY = shutil.which("ffplay")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
needs_ffplay = pytest.mark.skipif(not FFMPEG or not FFPLAY, reason="ffplay not installed")
PUBLIC = lambda host, port, type=None: [(2, 1, 6, "", ("93.184.216.34", 443))]  # noqa: E731 - 가짜 공용 DNS


# ---------------- 가짜 웹 ----------------

class FakeResp(io.BytesIO):
    def __init__(self, body: bytes, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class FakeWeb:
    """url → handler(req) → (status, headers, body). 3xx/4xx/5xx는 urllib처럼 HTTPError로 던진다."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def route(self, prefix, fn):
        self.routes[prefix] = fn

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append((url, dict(req.header_items())))
        for prefix, fn in self.routes.items():
            if url.startswith(prefix):
                status, headers, body = fn(req)
                if status >= 300:
                    raise urllib.error.HTTPError(url, status, "x", headers, io.BytesIO(body))
                return FakeResp(body, status, headers)
        raise urllib.error.URLError("no route")


def js(obj, status=200, headers=None):
    return lambda req: (status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(obj).encode())


def client(provider, web, tmp_path=None, **kw):
    return N.ProviderClient(provider, tmp_path, opener=web, resolver=PUBLIC, sleep=lambda s: None, **kw)


# ---------------- 주제·검색어 ----------------

@pytest.mark.parametrize("text, expect, unknown", [
    ("눈 내리는 호수", "snow lake", ()),
    ("바다 보이는 카페", "sea cafe", ()),
    ("비 오는 일본 카페", "rain japan cafe", ()),
    ("겨울 도쿄 야경", "winter tokyo night", ()),
    ("호수공원 산책로", "lake park trail", ()),       # '산책로' 안의 '산'으로 잘못 바꾸지 않음
    ("신비로운 호수", "lake", ("신비로운",)),          # '신비로운' 안의 '비'로 잘못 바꾸지 않음
    ("호수의 물결", "lake ripple", ()),
])
def test_korean_query_maps_whole_words_only(text, expect, unknown):
    q = S.build_query([], text)
    assert q.text == expect and q.unknown_words == unknown


def test_topic_combination_and_relevance_evidence():
    q = S.build_query(["snow", "lake"], "")
    assert q.text == "snow lake" and [g[0] for g in q.groups] == ["눈", "호수"]
    both = S.score(cand(evidence="Snowfall over a frozen lake"), q)
    one = S.score(cand(evidence="Mountain lake in summer"), q)
    none = S.score(cand(evidence="Lakers basketball snowboard"), q)  # 단어 일부만 같은 것은 근거가 아님
    assert both.match == (2, 2) and S.match_label(both) == "복합 일치 2/2" and "snowfall" in both.match_evidence
    assert S.match_label(one) == "부분 일치 1/2" and "눈✗" in one.match_evidence
    assert none.match == (0, 2) and S.match_label(none) == "근거 없음 0/2"
    ordered = S.sort_results([none, one, both], S.RES_FHD)
    assert ordered == [both, one, none]  # 단순히 섞지 않고 복합 일치를 먼저


def cand(provider=N.COMMONS, dur=60.0, w=1920, h=1080, evidence="", url=None, sid="1", lic=("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"), size=1000):
    u = url or "https://upload.wikimedia.org/wikipedia/commons/a/ab/x.webm"
    return S.Candidate(provider, sid, "https://commons.wikimedia.org/wiki/File:x.webm", evidence or "clip", dur,
                       (S.Rendition(w, h, u, size),), "", "작가", "", lic[0], lic[1], evidence)


# ---------------- 길이 모드·해상도 ----------------

def test_long_and_short_modes_are_strictly_separate():
    assert S.duration_reason(cand(dur=180.0), S.MODE_LONG) == ""
    assert S.duration_reason(cand(dur=179.9), S.MODE_LONG) == "3분 미만"
    assert S.duration_reason(cand(dur=None), S.MODE_LONG) == "길이 미확인"     # 미확인 = 장편 아님
    assert S.duration_reason(cand(dur=None), S.MODE_SHORT) == "길이 미확인"
    assert S.duration_reason(cand(provider=N.PEXELS, dur=600), S.MODE_LONG).startswith("Pexels는 장편 제외")
    assert [S.duration_reason(cand(dur=d), S.MODE_SHORT) for d in (9.9, 10, 120, 120.1)] == \
        ["10초 미만", "", "", "120초 초과"]
    assert S.MODE_LONG not in S.PROVIDER_MODES[N.PEXELS] and S.MODE_SHORT not in S.PROVIDER_MODES[N.ARCHIVE]


def test_resolution_filters_use_metadata_and_mark_unknown():
    k4 = cand(w=3840, h=2160)
    fhd = cand(w=1920, h=1080)
    hd = cand(w=1280, h=720)
    unknown = cand(w=0, h=0)
    assert S.res_reason(k4, S.RES_4K_ONLY) == "" and S.res_reason(fhd, S.RES_4K_ONLY) == "4K 아님 (메타데이터)"
    assert S.res_reason(fhd, S.RES_4K_PREF) == "" and S.res_reason(hd, S.RES_FHD) == "FHD 미만"
    assert S.res_reason(unknown, S.RES_4K_ONLY) == "해상도 미확인" and S.res_reason(unknown, S.RES_FHD) == ""
    assert "미확인" in S.res_text(unknown, S.RES_FHD) and "(메타)" in S.res_text(k4, S.RES_4K_PREF)
    multi = S.Candidate(N.PIXABAY, "9", "https://pixabay.com/videos/x-9/", "x", 30.0, (
        S.Rendition(3840, 2160, "https://cdn.pixabay.com/video/a_large.mp4", 300),
        S.Rendition(1920, 1080, "https://cdn.pixabay.com/video/a_medium.mp4", 100),
        S.Rendition(640, 360, "https://cdn.pixabay.com/video/a_tiny.mp4", 10)))
    assert multi.best(S.RES_4K_PREF).width == 3840 and multi.best(S.RES_FHD).width == 1920
    assert multi.preview().width == 640  # 미리보기는 가벼운 파일


# ---------------- 제공처 파서 (공식 API 형식) ----------------

PEXELS_VIDEO = {
    "id": 857195, "width": 3840, "height": 2160, "duration": 24,
    "url": "https://www.pexels.com/video/snow-falling-on-a-lake-857195/",
    "image": "https://images.pexels.com/videos/857195/free-video-857195.jpg",
    "user": {"id": 1, "name": "Kim", "url": "https://www.pexels.com/@kim"},
    "video_files": [
        {"id": 1, "quality": "uhd", "file_type": "video/mp4", "width": 3840, "height": 2160, "fps": 29.97,
         "link": "https://videos.pexels.com/video-files/857195/uhd.mp4"},
        {"id": 2, "quality": "hd", "file_type": "video/mp4", "width": 1920, "height": 1080, "fps": 29.97,
         "link": "https://videos.pexels.com/video-files/857195/hd.mp4"},
        {"id": 3, "quality": "hls", "file_type": "video/mp4", "width": None, "height": None,
         "link": "https://videos.pexels.com/hls.m3u8"},
        {"id": 4, "quality": "hd", "file_type": "video/mp4", "width": 1280, "height": 720,
         "link": "https://evil.example.com/x.mp4"},
    ],
}


def test_parse_pexels_uses_slug_as_title_and_rejects_unsafe_files():
    c = S.parse_pexels(PEXELS_VIDEO)
    assert c.title.lower().startswith("snow falling on a lake") and c.duration == 24.0 and c.creator == "Kim"
    assert {r.width for r in c.renditions} == {3840, 1920}  # null 크기·허용 안 된 호스트 제외
    assert c.thumb_url.startswith("https://images.pexels.com/")
    assert S.parse_pexels({**PEXELS_VIDEO, "url": "https://evil.example.com/video/x-1/"}) is None


def test_parse_pixabay_large_label_is_not_4k_proof():
    hit = {"id": 125, "pageURL": "https://pixabay.com/videos/lake-snow-125/", "tags": "lake, snow, winter",
           "duration": 200, "user": "Lee",
           "videos": {"large": {"url": "", "width": 0, "height": 0, "size": 0, "thumbnail": ""},
                      "medium": {"url": "https://cdn.pixabay.com/video/m.mp4", "width": 1920, "height": 1080,
                                 "size": 9_000_000, "thumbnail": "https://cdn.pixabay.com/video/m.jpg"},
                      "tiny": {"url": "http://cdn.pixabay.com/video/t.mp4", "width": 640, "height": 360, "size": 1}}}
    c = S.parse_pixabay(hit)
    assert [r.label for r in c.renditions] == ["medium"]  # 빈 large · http 주소 제외
    assert c.duration == 200.0 and S.res_reason(c, S.RES_4K_ONLY) == "4K 아님 (메타데이터)"
    assert any("large" in n for n in c.notes)


def test_parse_commons_duration_license_and_thumb():
    page = {"ns": 6, "title": "File:Snowy lake.webm", "videoinfo": [{
        "mediatype": "VIDEO", "mime": "video/webm", "width": 1920, "height": 1080, "size": 5_000_000,
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Snowy_lake.webm",
        "thumburl": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Snowy_lake.webm/320px--x.jpg",
        "metadata": [{"name": "playtime_seconds", "value": 245.5}],
        "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"},
                        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
                        "Artist": {"value": "<a href='x'>Park</a>"}}}]}
    c = S.parse_commons(page)
    assert c.duration == 245.5 and c.creator == "Park" and c.thumb_url.startswith("https://thumb.wikimedia.org/")
    assert c.license_name == "CC BY-SA 4.0" and S.duration_reason(c, S.MODE_LONG) == ""
    page["videoinfo"][0]["metadata"] = [{"name": "playtime_seconds", "value": 245}, {"name": "length", "value": 10}]
    page["videoinfo"][0].pop("duration", None)
    assert S.parse_commons(page).duration is None  # 서로 다른 길이 정보는 믿지 않음


def test_parse_archive_picks_video_file_and_license():
    meta = {"metadata": {"title": "Winter forest walk", "licenseurl": "http://creativecommons.org/licenses/by/4.0/",
                         "creator": "Choi", "subject": ["snow", "forest"]},
            "files": [{"name": "walk.ogv", "format": "Ogg Video", "length": "612.4", "width": "640", "height": "480"},
                      {"name": "walk.mp4", "format": "h.264", "length": "10:12", "width": "1920", "height": "1080",
                       "size": "900000000"},
                      {"name": "../evil.mp4", "format": "h.264"}]}
    c = S.parse_archive("winter-walk_01", {}, meta)
    r = c.renditions[0]
    assert r.url == "https://archive.org/download/winter-walk_01/walk.mp4" and (r.width, r.height) == (1920, 1080)
    assert c.duration == 612.0 and c.license_url.startswith("https://") and c.license_name == "CC BY 4.0"
    assert S.parse_archive("x", {}, {"metadata": {}, "files": [{"name": "a.pdf", "format": "PDF"}]}) is None


# ---------------- 검색 조정: 모드·오류·자동 대체 없음 ----------------

class FakeAdapter:
    def __init__(self, name, cands=(), err=None):
        self.name, self.cands, self.err, self.calls = name, list(cands), err, 0

    def search(self, plan, mode, res, cancel=None):
        self.calls += 1
        if self.err:
            raise self.err
        return len(self.cands), self.cands


def test_run_search_isolates_providers_and_never_falls_back():
    q = S.build_query(["lake"], "")
    ads = {N.PEXELS: FakeAdapter(N.PEXELS, [cand(provider=N.PEXELS, dur=30)]),
           N.PIXABAY: FakeAdapter(N.PIXABAY, err=N.NetError("KEY_MISSING")),
           N.COMMONS: FakeAdapter(N.COMMONS, [cand(dur=60), cand(dur=None, sid="2"), cand(dur=400, sid="3")]),
           N.ARCHIVE: FakeAdapter(N.ARCHIVE, err=N.NetError("RATE_LIMIT", retry_after=30, status=429))}
    long = {r.provider: r for r in S.run_search(ads, list(N.PROVIDERS), q, S.MODE_LONG, S.RES_FHD)}
    assert long[N.PEXELS].status == "이 모드 미지원" and ads[N.PEXELS].calls == 0  # API 요청 전에 차단
    assert long[N.PIXABAY].status == "키 없음" and "API 키" in long[N.PIXABAY].message
    assert [c.source_id for c in long[N.COMMONS].candidates] == ["3"]
    assert long[N.COMMONS].excluded == {"3분 미만": 1, "길이 미확인": 1}
    assert long[N.ARCHIVE].status == "오류" and "429" in long[N.ARCHIVE].message and "30초" in long[N.ARCHIVE].message
    short = {r.provider: r for r in S.run_search(ads, list(N.PROVIDERS), q, S.MODE_SHORT, S.RES_FHD)}
    assert [c.source_id for c in short[N.COMMONS].candidates] == ["1"] and short[N.ARCHIVE].status == "이 모드 미지원"
    empty = S.run_search({N.COMMONS: FakeAdapter(N.COMMONS, [cand(dur=60)])}, [N.COMMONS], q, S.MODE_LONG, S.RES_FHD)
    r = next(x for x in empty if x.provider == N.COMMONS)
    assert r.candidates == [] and r.message.startswith("확인된 장편 원본 없음")  # 짧은 영상으로 대체하지 않음


# ---------------- 네트워크 안전 ----------------

@pytest.mark.parametrize("url, ok", [
    ("https://videos.pexels.com/video-files/1/a.mp4", True),
    ("http://videos.pexels.com/video-files/1/a.mp4", False),
    ("https://videos.pexels.com.evil.com/a.mp4", False),
    ("https://user@videos.pexels.com/a.mp4", False),
    ("https://videos.pexels.com:8443/a.mp4", False),
    ("https://videos.pexels.com/a/%2e%2e/%2e%2e/b.mp4", False),
    ("https://93.184.216.34/a.mp4", False),
    ("https://ia800300.us.archive.org/1/items/x/a.mp4", False),  # Pexels 다운로드 목록에는 없음
])
def test_url_allowlist(url, ok):
    assert N.url_allowed(url, N.PEXELS, "download") is ok


def test_archive_storage_subdomain_allowed_and_dns_private_blocked():
    assert N.url_allowed("https://ia800300.us.archive.org/1/items/x/a.mp4", N.ARCHIVE, "download")
    private = lambda h, p, type=None: [(2, 1, 6, "", ("10.0.0.5", 443))]  # noqa: E731
    with pytest.raises(N.NetError) as e:
        N.open_url("https://archive.org/download/x/a.mp4", N.ARCHIVE, "download", resolver=private, opener=FakeWeb())
    assert e.value.code == "DNS_UNSAFE"


def test_redirect_to_disallowed_host_is_blocked():
    web = FakeWeb()
    web.route("https://archive.org/download/x/a.mp4", lambda r: (302, {"Location": "https://evil.example.com/a.mp4"}, b""))
    with pytest.raises(N.NetError) as e:
        N.open_url("https://archive.org/download/x/a.mp4", N.ARCHIVE, "download", opener=web, resolver=PUBLIC)
    assert e.value.code == "REDIRECT" and len(web.calls) == 1
    web.route("https://archive.org/download/y/a.mp4",
              lambda r: (302, {"Location": "https://ia801.us.archive.org/items/y/a.mp4"}, b""))
    web.route("https://ia801.us.archive.org/", lambda r: (200, {}, b"ok"))
    assert N.open_url("https://archive.org/download/y/a.mp4", N.ARCHIVE, "download", opener=web,
                      resolver=PUBLIC).stream.read() == b"ok"


def test_client_cache_retry_after_and_key_never_stored(tmp_path):
    web = FakeWeb()
    seq = iter([(429, {"Retry-After": "3"}, b""), (200, {"Content-Type": "application/json"}, b'{"hits": []}')])
    web.route("https://pixabay.com/api/videos/", lambda r: next(seq))
    slept = []
    c = N.ProviderClient(N.PIXABAY, tmp_path / "cache", opener=web, resolver=PUBLIC, sleep=slept.append)
    params = {"key": "SECRET-KEY-123", "q": "lake"}
    assert c.get_json("https://pixabay.com/api/videos/", params, secret_params=("key",)) == {"hits": []}
    assert 3.0 in slept and len(web.calls) == 2
    assert c.get_json("https://pixabay.com/api/videos/", {**params, "key": "OTHER"}, secret_params=("key",)) == {"hits": []}
    assert len(web.calls) == 2  # 24시간 캐시 (키가 달라도 같은 검색이면 캐시)
    blob = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "cache").glob("*.json"))
    assert "SECRET-KEY-123" not in blob and "OTHER" not in blob
    err = N.NetError("NETWORK", "failed https://pixabay.com/api/videos/?key=SECRET-KEY-123&q=x")
    assert "SECRET" not in err.detail and "SECRET" not in err.message


def test_pexels_key_goes_in_header_only(tmp_path):
    web = FakeWeb()
    web.route("https://api.pexels.com/v1/videos/search", js({"videos": [PEXELS_VIDEO]}))
    ad = S.PexelsAdapter(client(N.PEXELS, web), lambda: "PEXELS-SECRET")
    raw, cands = ad.search(S.build_query(["snow", "lake"]), S.MODE_SHORT, S.RES_FHD)
    url, headers = web.calls[0]
    assert "PEXELS-SECRET" not in url and headers.get("Authorization") == "PEXELS-SECRET"
    assert raw == 1 and cands[0].provider == N.PEXELS
    with pytest.raises(N.NetError) as e:
        S.PexelsAdapter(client(N.PEXELS, web), lambda: None).search(S.build_query(["lake"]), S.MODE_SHORT, S.RES_FHD)
    assert e.value.code == "KEY_MISSING"


def test_auth_error_and_invalid_json_are_reported(tmp_path):
    web = FakeWeb()
    web.route("https://api.pexels.com/", lambda r: (401, {}, b""))
    with pytest.raises(N.NetError) as e:
        client(N.PEXELS, web).get_json("https://api.pexels.com/v1/videos/search", {"query": "x"})
    assert e.value.code == "AUTH" and "401" in e.value.message
    web.route("https://commons.wikimedia.org/", lambda r: (200, {"Content-Type": "text/html"}, b"<html>"))
    with pytest.raises(N.NetError) as e:
        client(N.COMMONS, web).get_json("https://commons.wikimedia.org/w/api.php", {"a": "b"})
    assert e.value.code == "INVALID"


# ---------------- 권리 ----------------

@pytest.mark.parametrize("provider, name, url, state", [
    (N.PEXELS, "", "", R.OK),
    (N.PIXABAY, "", "", R.OK),
    (N.COMMONS, "CC0", "https://creativecommons.org/publicdomain/zero/1.0/", R.OK),
    (N.COMMONS, "Public domain", "", R.OK),
    (N.COMMONS, "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0", R.ATTRIBUTION),
    (N.COMMONS, "CC BY-SA 3.0", "https://creativecommons.org/licenses/by-sa/3.0", R.REVIEW),
    (N.ARCHIVE, "", "https://creativecommons.org/licenses/by-nc/4.0/", R.BLOCKED),
    (N.ARCHIVE, "", "https://creativecommons.org/licenses/by-nd/4.0/", R.BLOCKED),
    (N.COMMONS, "CC BY-NC-SA 2.0", "", R.BLOCKED),
    (N.ARCHIVE, "", "", R.UNKNOWN),
    (N.ARCHIVE, "free", "https://example.com/my-license", R.UNKNOWN),
    (N.COMMONS, "GFDL", "https://www.gnu.org/copyleft/fdl.html", R.REVIEW),
])
def test_rights_states(provider, name, url, state):
    rv = R.review(provider, name, url, "작가", "제목")
    assert rv.state == state
    assert rv.downloadable == (state in (R.OK, R.ATTRIBUTION, R.REVIEW))
    assert R.THIRD_PARTY in rv.third_party
    if state == R.ATTRIBUTION:
        assert rv.attribution_required and "작가" in rv.attribution_text
    rec = rv.record(provider=provider, source_url="https://x", creator="작가")
    assert {"provider", "source_url", "creator", "license_name", "license_url", "license_version", "checked_at",
            "attribution_required", "third_party", "commercial_status", "state", "disclaimer"} <= set(rec)
    text = json.dumps(rec, ensure_ascii=False)
    assert "보증하지 않습니다" in text and "허가합니다" not in text


# ---------------- 다운로드 ----------------

@pytest.fixture(scope="module")
def sample_mp4(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    p = tmp_path_factory.mktemp("src") / "샘플.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25", "-t", "6",
                    "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


def range_server(data: bytes, honor_range=True, fail_after=None):
    state = {"n": 0}

    def fn(req):
        state["n"] += 1
        rng = req.headers.get("Range") or dict(req.header_items()).get("Range")
        if rng and honor_range:
            start = int(rng.split("=")[1].rstrip("-"))
            return 206, {"Content-Length": str(len(data) - start)}, data[start:]
        body = data if fail_after is None or state["n"] > 1 else data[:fail_after]
        return 200, {"Content-Length": str(len(data))}, body
    return fn, state


def dl_cand(url="https://upload.wikimedia.org/wikipedia/commons/a/ab/Sample.webm", sid="File:Sample.webm", w=320, h=180,
            dur=6.0, lic=("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"), size=0):
    return S.Candidate(N.COMMONS, sid, "https://commons.wikimedia.org/wiki/File:Sample.webm", "눈 내리는 호수 샘플", dur,
                       (S.Rendition(w, h, url, size),), "", "작가", "", lic[0], lic[1], "snow lake")


@needs_ffmpeg
def test_download_verify_record_duplicate_and_korean_path(tmp_path, sample_mp4):
    data = sample_mp4.read_bytes()
    web = FakeWeb()
    fn, _ = range_server(data)
    web.route("https://upload.wikimedia.org/", fn)
    lib = D.Library(tmp_path / "라이브러리 폴더")
    c = dl_cand()
    e = D.download(c, c.renditions[0], lib, mode=S.MODE_SHORT, ffprobe=Path(FFPROBE), ffmpeg=Path(FFMPEG),
                   cancel=threading.Event(), opener=web, resolver=PUBLIC, sleep=lambda s: None)
    p = Path(e["path"])
    assert e["status"] == "완료" and p.read_bytes() == data and e["sha256"] == hashlib.sha256(data).hexdigest()
    assert e["probe"]["width"] == 320 and e["probe"]["fps"] == 25.0 and e["probe"]["codec"] == "h264"
    assert e["probe"]["playable"] and abs(e["probe"]["duration"] - 6.0) < 0.2
    side = json.loads(p.with_name(p.name + ".rights.json").read_text(encoding="utf-8"))
    assert side["state"] == R.ATTRIBUTION and side["source_url"] == c.page_url and side["checked_at"]
    assert [x["key"] for x in lib.entries()] == [c.key]
    again = D.download(c, c.renditions[0], lib, mode=S.MODE_SHORT, ffprobe=Path(FFPROBE), ffmpeg=Path(FFMPEG),
                       cancel=threading.Event(), opener=web, resolver=PUBLIC)
    assert again["status"] == "이미 받음" and len(lib.entries()) == 1
    other = dl_cand(url="https://upload.wikimedia.org/wikipedia/commons/b/bc/Copy.webm", sid="File:Copy.webm")
    dup = D.download(other, other.renditions[0], lib, mode=S.MODE_SHORT, ffprobe=Path(FFPROBE), ffmpeg=Path(FFMPEG),
                     cancel=threading.Event(), opener=web, resolver=PUBLIC)
    assert dup["status"] == "중복" and p.is_file() and len(lib.entries()) == 1  # 기존 파일 그대로, 새 사본만 버림
    assert not list(p.parent.glob("*.part"))
    long = D.download(dl_cand(url="https://upload.wikimedia.org/wikipedia/commons/c/cd/L.webm", sid="File:L.webm",
                              dur=200.0), S.Rendition(320, 180, "https://upload.wikimedia.org/wikipedia/commons/c/cd/L.webm"),
                      D.Library(tmp_path / "lib2"), mode=S.MODE_LONG, ffprobe=Path(FFPROBE), ffmpeg=Path(FFMPEG),
                      cancel=threading.Event(), opener=web, resolver=PUBLIC)
    assert any("3분 미만" in w for w in long["warnings"]) and any("제공처 정보" in w for w in long["warnings"])


@needs_ffmpeg
def test_cancel_keeps_part_and_resume_uses_range(tmp_path, sample_mp4):
    data = sample_mp4.read_bytes()
    web = FakeWeb()
    fn, _ = range_server(data)
    web.route("https://upload.wikimedia.org/", fn)
    lib = D.Library(tmp_path / "lib")
    c = dl_cand()
    cancel = threading.Event()
    seen = []

    def prog(f, t):
        seen.append(f)
        cancel.set()  # 첫 1 MB 조각 뒤 취소
    old = D.CHUNK
    D.CHUNK = 64 * 1024
    try:
        with pytest.raises(D.DownloadError, match="취소"):
            D.download(c, c.renditions[0], lib, mode=S.MODE_SHORT, ffprobe=Path(FFPROBE), ffmpeg=None, cancel=cancel,
                       progress=prog, opener=web, resolver=PUBLIC)
        parts = list((tmp_path / "lib").rglob("*.part"))
        assert len(parts) == 1 and 0 < parts[0].stat().st_size < len(data)
        e = D.download(c, c.renditions[0], lib, mode=S.MODE_SHORT, ffprobe=Path(FFPROBE), ffmpeg=None,
                       cancel=threading.Event(), opener=web, resolver=PUBLIC)
    finally:
        D.CHUNK = old
    assert Path(e["path"]).read_bytes() == data
    assert any("Range" in h for _, h in web.calls[1:])  # 이어받기 요청


@needs_ffmpeg
def test_server_without_range_restarts_and_429_retries(tmp_path, sample_mp4):
    data = sample_mp4.read_bytes()
    web = FakeWeb()
    calls = {"n": 0}

    def fn(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {"Retry-After": "7"}, b""
        return 200, {"Content-Length": str(len(data))}, data
    web.route("https://upload.wikimedia.org/", fn)
    folder = tmp_path / "lib" / "Wikimedia_Commons"
    folder.mkdir(parents=True)
    c = dl_cand()
    stale = folder / "x.part"  # 다른 작업의 .part는 건드리지 않음
    stale.write_bytes(b"other")
    slept = []
    e = D.download(c, c.renditions[0], D.Library(tmp_path / "lib"), mode=S.MODE_SHORT, ffprobe=Path(FFPROBE), ffmpeg=None,
                   cancel=threading.Event(), opener=web, resolver=PUBLIC, sleep=slept.append)
    assert e["status"] == "완료" and 7.0 in slept and stale.read_bytes() == b"other"


def test_blocked_rights_and_disk_shortage_never_download(tmp_path, monkeypatch):
    web = FakeWeb()
    c = dl_cand(lic=("CC BY-NC 4.0", "https://creativecommons.org/licenses/by-nc/4.0/"))
    with pytest.raises(D.DownloadError, match="상업적 이용 차단"):
        D.download(c, c.renditions[0], D.Library(tmp_path), mode=S.MODE_SHORT, ffprobe=None, ffmpeg=None,
                   cancel=threading.Event(), opener=web, resolver=PUBLIC)
    assert web.calls == []
    monkeypatch.setattr(D.shutil, "disk_usage", lambda p: SimpleNamespace(total=1, used=0, free=200 * 1024**2))
    ok = dl_cand(size=500 * 1024**2)
    with pytest.raises(D.DownloadError, match="저장 공간"):
        D.download(ok, ok.renditions[0], D.Library(tmp_path), mode=S.MODE_SHORT, ffprobe=None, ffmpeg=None,
                   cancel=threading.Event(), opener=web, resolver=PUBLIC)
    assert web.calls == [] and not list(tmp_path.rglob("*.part"))


def test_existing_file_is_never_overwritten(tmp_path, monkeypatch):
    web = FakeWeb()
    web.route("https://upload.wikimedia.org/", lambda r: (200, {"Content-Length": "4"}, b"NEW!"))
    monkeypatch.setattr(D, "sha256_file", lambda p, cancel=None: hashlib.sha256(Path(p).read_bytes()).hexdigest())
    c = dl_cand()
    lib = D.Library(tmp_path)
    folder = tmp_path / "Wikimedia_Commons"
    folder.mkdir()
    name = f"{D._safe_name(c.title, 50)}_{D._safe_name('Sample.webm', 40)}_320x180.webm"
    (folder / name).write_bytes(b"OLD")
    e = D.download(c, c.renditions[0], lib, mode=S.MODE_SHORT, ffprobe=None, ffmpeg=None, cancel=threading.Event(),
                   opener=web, resolver=PUBLIC)
    assert (folder / name).read_bytes() == b"OLD" and Path(e["path"]).name != name and Path(e["path"]).read_bytes() == b"NEW!"


# ---------------- API 키 ----------------

@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows only")
def test_keys_are_dpapi_files_not_settings(tmp_path):
    from app.settings import load_settings
    from app.stock_keys import KeyStore
    ks = KeyStore(tmp_path)
    ks.save(N.PEXELS, "abcdefghij-SECRET")
    raw = (tmp_path / "stock_key_pexels.dat").read_bytes()
    assert b"SECRET" not in raw and ks.get(N.PEXELS) == "abcdefghij-SECRET" and ks.source(N.PEXELS).startswith("저장됨")
    assert "SECRET" not in json.dumps(load_settings())
    ks.clear(N.PEXELS)
    assert ks.get(N.PEXELS) is None
    with pytest.raises(ValueError):
        ks.save(N.PEXELS, "short")


def test_legacy_key_is_read_only():
    from app.stock_keys import LEGACY_SERVICE, read_legacy_key
    calls = []

    class FakeKeyring:
        def get_password(self, service, user):
            calls.append(("get", service, user))
            return "LEGACY-KEY-1234" if user == "pixabay" else None

        def set_password(self, *a):  # pragma: no cover - 호출되면 실패
            raise AssertionError("must not write")

        def delete_password(self, *a):  # pragma: no cover
            raise AssertionError("must not delete")
    key, msg = read_legacy_key(N.PIXABAY, FakeKeyring())
    assert key == "LEGACY-KEY-1234" and "LEGACY" not in msg and calls == [("get", LEGACY_SERVICE, "pixabay")]
    assert read_legacy_key(N.PEXELS, FakeKeyring())[0] is None


# ---------------- 실제 동영상 재생 ----------------

@needs_ffplay
def test_player_play_pause_seek_restart_and_errors(tmp_path, sample_mp4):
    from app.stock_player import ENDED, ERROR, PAUSED, PLAYING, PreviewPlayer, classify_error
    env = dict(os.environ, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy")
    pl = PreviewPlayer(Path(FFPLAY), ffmpeg=Path(FFMPEG), env=env)
    pl.open(str(sample_mp4), "시험", 6.0)

    def wait(cond, t=15):
        end = time.monotonic() + t
        while time.monotonic() < end and not cond():
            time.sleep(0.05)
        return cond()
    try:
        assert pl.play() and wait(lambda: pl.confirmed and pl.state == PLAYING), pl.message
        pl.pause()
        assert pl.state == PAUSED and not pl.running and pl.position > 0.4
        assert pl.seek(3.5) and pl.play()
        assert wait(lambda: pl.confirmed and pl.position >= 3.5), (pl.state, pl.position)
        assert wait(lambda: pl.state == ENDED, 15)
        assert pl.restart() and wait(lambda: pl.confirmed and pl.position < 3.0)
    finally:
        pl.stop()
    bad = PreviewPlayer(Path(FFPLAY), env=env)
    bad.STALL_SECONDS = 6
    bad.open(str(tmp_path / "없는파일.mp4"), "x", 0)
    bad.play()
    assert wait(lambda: bad.state == ERROR, 15) and not bad.confirmed  # 정지 화면을 재생으로 보지 않음
    assert classify_error("Server returned 403 Forbidden").startswith("제공처 접근 거부")
    assert classify_error("Connection timed out").startswith("네트워크")
    assert classify_error("Invalid data found when processing input").startswith("코덱")
    assert classify_error("Server returned 429 Too Many Requests").startswith("제공처 요청 한도")


# ---------------- 화면 ----------------

def pump(root, cond, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.03)
    return cond()


@pytest.fixture
def tkroot():
    tk = pytest.importorskip("tkinter")
    from app.ui_theme import ensure
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    r.withdraw()
    ensure(r)
    yield r
    r.destroy()


@pytest.fixture
def quiet(monkeypatch):
    from tkinter import messagebox
    shown = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: shown.append(a))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    return shown


@needs_ffmpeg
def test_window_search_modes_select_download_and_send_to_bg(tkroot, tmp_path, quiet, sample_mp4, monkeypatch):
    import app.stock_ui as U
    from app.bg_ui import BackgroundMakerWindow
    jpg = tmp_path / "poster.jpg"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180", "-frames:v", "1", str(jpg)],
                   check=True)
    monkeypatch.setattr(U, "fetch_bytes", lambda url, prov, purpose="poster", **k: jpg.read_bytes())
    data = sample_mp4.read_bytes()
    web = FakeWeb()
    fn, _ = range_server(data)
    web.route("https://upload.wikimedia.org/", fn)
    real_open = D.open_url
    monkeypatch.setattr(D, "open_url", lambda *a, **k: real_open(*a, **{**k, "opener": web, "resolver": PUBLIC}))
    short_ok = S.Candidate(N.COMMONS, "File:A.webm", "https://commons.wikimedia.org/wiki/File:A.webm", "Snow lake A", 30.0,
                           (S.Rendition(1920, 1080, "https://upload.wikimedia.org/wikipedia/commons/a/aa/A.webm"),),
                           "https://upload.wikimedia.org/x/a.jpg", "작가", "", "CC0", "https://creativecommons.org/publicdomain/zero/1.0/",
                           "snow lake")
    nc = S.Candidate(N.COMMONS, "File:B.webm", "https://commons.wikimedia.org/wiki/File:B.webm", "Lake B", 40.0,
                     (S.Rendition(1920, 1080, "https://upload.wikimedia.org/wikipedia/commons/b/bb/B.webm"),), "", "x", "",
                     "CC BY-NC 4.0", "https://creativecommons.org/licenses/by-nc/4.0/", "lake")
    long_ = S.Candidate(N.COMMONS, "File:C.webm", "https://commons.wikimedia.org/wiki/File:C.webm", "Snow lake long", 400.0,
                        (S.Rendition(1920, 1080, "https://upload.wikimedia.org/wikipedia/commons/c/cc/C.webm"),), "", "y", "",
                        "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/", "snow lake")
    fake = {p: FakeAdapter(p) for p in N.PROVIDERS}
    fake[N.COMMONS] = FakeAdapter(N.COMMONS, [short_ok, nc, long_])
    sent = []
    bgw = BackgroundMakerWindow(tkroot, tools=lambda: (FFMPEG, FFPROBE))
    bgw.withdraw()
    w = U.StockSearchWindow(tkroot, tools=lambda: (FFMPEG, FFPROBE), adapters=fake,
                            send_to_bg=lambda paths: (sent.extend(paths), bgw.add_paths(paths)), check_dns=False,
                            player_env=dict(os.environ, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy"))
    try:
        w.withdraw()
        w.libdir.set(str(tmp_path / "⑤ 라이브러리"))
        for k, v in w.topic_vars.items():
            v.set(k in ("snow", "lake"))
        for p, v in w.prov_vars.items():
            v.set(True)
        w.mode.set(S.MODE_SHORT)
        w._mode_changed()
        assert w.search() and pump(w, lambda: not w.searching, 30)
        rows = [w.tree.item(i) for i in w.tree.get_children()]
        assert [r["values"][1] for r in rows] == ["Snow lake A", "Lake B"]  # 복합 일치 먼저, 400초는 짧은 모드에서 제외
        assert rows[0]["values"][7] == "복합 일치 2/2" and rows[1]["values"][6] == R.BLOCKED
        assert "이 모드 미지원" in str(w.prov_labels[N.ARCHIVE].cget("text"))
        assert pump(w, lambda: short_ok.key in w.thumbs, 20)  # 실제 FFmpeg 썸네일 변환
        # 모드 분리: 장편으로 바꾸면 자동 검색·대체 없이 빈 목록, 다시 검색하면 장편만
        w.mode.set(S.MODE_LONG)
        w._mode_changed()
        assert w.tree.get_children() == () and fake[N.COMMONS].calls == 1
        assert w.search() and pump(w, lambda: not w.searching, 30)
        assert [w.tree.item(i)["values"][1] for i in w.tree.get_children()] == ["Snow lake long"]
        w.mode.set(S.MODE_SHORT)
        w._mode_changed()
        assert len(w.tree.get_children()) == 2  # 짧은 모드 결과는 그대로 보존
        w.tree.selection_set("1")
        assert not w.toggle_selected() and quiet  # NC → 선택 차단
        w.tree.selection_set("0")
        assert w.toggle_selected() and w.tree.set("0", "sel") == "☑"
        assert w.download_selected() and pump(w, lambda: not w.downloading, 60)
        assert len(w.lib.get_children()) == 1 and "320×180" in w.lib.set(w.lib.get_children()[0], "probe")
        w.lib.selection_set(w.lib.get_children()[0])
        assert w.send_to_bg() and len(sent) == 1 and Path(sent[0]).is_file()
        assert pump(bgw, lambda: not bgw.loading and len(bgw.clips) == 1, 60)  # 기존 ④ 목록에 추가
        assert bgw.clips[0].path == Path(sent[0]).resolve()
    finally:
        w.destroy()
        bgw.destroy()


@needs_ffplay
def test_window_preview_plays_real_video(tkroot, tmp_path, quiet, sample_mp4, monkeypatch):
    import app.stock_ui as U
    monkeypatch.setattr(S, "url_allowed", lambda *a, **k: True)
    monkeypatch.setattr(U, "url_allowed", lambda *a, **k: True)
    c = S.Candidate(N.COMMONS, "File:P.mp4", "https://commons.wikimedia.org/wiki/File:P.mp4", "P", 12.0,
                    (S.Rendition(1920, 1080, str(sample_mp4)),), "", "", "", "CC0",
                    "https://creativecommons.org/publicdomain/zero/1.0/", "lake")
    fake = {p: FakeAdapter(p) for p in N.PROVIDERS}
    fake[N.COMMONS] = FakeAdapter(N.COMMONS, [c])
    w = U.StockSearchWindow(tkroot, tools=lambda: (FFMPEG, FFPROBE), adapters=fake, check_dns=False,
                            player_env=dict(os.environ, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy"))
    try:
        w.withdraw()
        w.free_text.set("lake")
        for k, v in w.topic_vars.items():
            v.set(False)
        w.mode.set(S.MODE_SHORT)
        w._mode_changed()
        assert w.search() and pump(w, lambda: not w.searching, 30)
        w.tree.selection_set("0")
        assert w.preview_selected()
        assert pump(w, lambda: w.player.confirmed and "재생 확인" in w.player_text.get(), 20), w.player_text.get()
        assert "/ 00:06" in w.time_text.get()
        w.player_pause()
        assert w.player.state == "일시정지"
    finally:
        w.destroy()


def test_main_window_has_stock_card_and_sends_to_bg(tmp_path, monkeypatch):
    import app.ui as ui
    monkeypatch.setattr(ui, "discover_ffmpeg", lambda root: None)
    try:
        app = ui.MainWindow(tmp_path)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    try:
        assert {"bg", "stock"} <= set(app.mode_cards) and "stock" in app.card_help_links
        got = []
        fake_bg = SimpleNamespace(running=False, add_paths=lambda ps: got.extend(ps) or True,
                                  deiconify=lambda: None, lift=lambda: None)
        monkeypatch.setattr(app, "_open_bg", lambda: fake_bg)
        assert app._receive_stock([tmp_path / "a.mp4"]) and got == [tmp_path / "a.mp4"]
        fake_bg.running = True
        from tkinter import messagebox
        monkeypatch.setattr(messagebox, "showwarning", lambda *a, **k: None)
        assert app._receive_stock([tmp_path / "b.mp4"]) is False
    finally:
        app._finish_close()


def test_main_window_stock_never_prompts_for_ffmpeg_on_open(tmp_path, monkeypatch):
    """FFmpeg를 못 찾아도 ⑤ 창 열기·썸네일은 FFmpeg 선택 창을 띄우지 않는다 (self-test 멈춤 회귀)."""
    import app.ui as ui
    monkeypatch.setattr(ui, "discover_ffmpeg", lambda root: None)
    asked = []
    monkeypatch.setattr(ui.filedialog, "askopenfilename", lambda *a, **k: asked.append(k.get("title")) or "")
    try:
        app = ui.MainWindow(tmp_path)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no display: {e}")
    try:
        w = app._open_stock()
        app.update()
        assert w._ff == (None, None) and asked == []
        w.destroy()
    finally:
        app._finish_close()


def test_winvault_reader_layout_without_keyring_package():
    """keyring 패키지 없이 WinVault 배치(target=service, 또는 'user@service')를 읽기만 한다 (가짜 reader)."""
    from app.stock_keys import LEGACY_SERVICE, _WinVaultReader
    store = {LEGACY_SERVICE: ("pexels", "AAA"), f"pixabay@{LEGACY_SERVICE}": ("pixabay", "BBB")}
    calls = []
    r = _WinVaultReader(reader=lambda t: calls.append(t) or store.get(t))
    assert r.get_password(LEGACY_SERVICE, "pexels") == "AAA"
    assert r.get_password(LEGACY_SERVICE, "pixabay") == "BBB"
    assert r.get_password(LEGACY_SERVICE, "Pexels") is None
    assert all(t in (LEGACY_SERVICE, f"pexels@{LEGACY_SERVICE}", f"pixabay@{LEGACY_SERVICE}", f"Pexels@{LEGACY_SERVICE}") for t in calls)
