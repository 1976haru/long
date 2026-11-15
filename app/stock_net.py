"""⑤ 배경 영상 검색·다운로드 — 네트워크 안전 계층 (표준 라이브러리만).

- 제공처별 HTTPS 허용 호스트(API / 썸네일 / 다운로드)만 접속한다. redirect는 자동으로 따라가지 않고
  매 단계 허용 목록 + 공용 IP(DNS) 검사를 다시 한다.
- API 키는 헤더(Pexels) 또는 쿼리(Pixabay)로만 보내고, 로그·오류·캐시 키에 절대 넣지 않는다.
- 제공처별 요청 간격 + 429 Retry-After + 지수 백오프, API 응답은 24시간 디스크 캐시 (Pixabay 약관: 24시간 캐시).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

USER_AGENT = "YouTubePlaylistStudio/0.3 (personal desktop app; https://github.com/1976haru/long) Python-urllib"

PEXELS, PIXABAY, COMMONS, ARCHIVE = "Pexels", "Pixabay", "Wikimedia Commons", "Internet Archive"
PROVIDERS = (PEXELS, PIXABAY, COMMONS, ARCHIVE)

API_HOSTS = {
    PEXELS: ("api.pexels.com",),
    PIXABAY: ("pixabay.com",),
    COMMONS: ("commons.wikimedia.org",),
    ARCHIVE: ("archive.org",),
}
POSTER_HOSTS = {
    PEXELS: ("images.pexels.com",),
    PIXABAY: ("cdn.pixabay.com", "i.vimeocdn.com"),
    COMMONS: ("upload.wikimedia.org", "thumb.wikimedia.org"),
    ARCHIVE: ("archive.org", ".archive.org"),
}
DOWNLOAD_HOSTS = {
    PEXELS: ("videos.pexels.com", "player.vimeo.com", ".vimeocdn.com"),
    PIXABAY: ("cdn.pixabay.com",),
    COMMONS: ("upload.wikimedia.org",),
    ARCHIVE: ("archive.org", ".archive.org"),
}
PAGE_HOSTS = {  # 원본 페이지 (브라우저로 열기만)
    PEXELS: ("www.pexels.com", "pexels.com"),
    PIXABAY: ("pixabay.com", "www.pixabay.com"),
    COMMONS: ("commons.wikimedia.org",),
    ARCHIVE: ("archive.org",),
}
_TABLES = {"api": API_HOSTS, "poster": POSTER_HOSTS, "download": DOWNLOAD_HOSTS, "page": PAGE_HOSTS}

# 요청 간격 (초). Pexels 200회/시간, Pixabay 100회/60초, Commons·Archive는 직렬 + 여유
MIN_SPACING = {PEXELS: 1.0, PIXABAY: 1.0, COMMONS: 1.0, ARCHIVE: 1.0}
CACHE_TTL = 24 * 3600
MAX_JSON_BYTES = 4_000_000
MAX_RETRY_AFTER = 60.0


class NetError(RuntimeError):
    """사용자에게 보여줄 안정적인 오류 코드 + 한국어 설명 (URL·키·응답 원문은 담지 않는다)."""

    MESSAGES = {
        "URL_DISALLOWED": "허용되지 않은 주소라 접속하지 않았습니다.",
        "DNS_UNSAFE": "주소가 내부망/특수 주소로 연결되어 접속하지 않았습니다.",
        "KEY_MISSING": "API 키가 없습니다. [API 키 설정]에서 넣어 주세요.",
        "AUTH": "API 키가 거부되었습니다 (키를 다시 확인하세요).",
        "RATE_LIMIT": "요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요.",
        "HTTP_ERROR": "제공처 서버 오류입니다.",
        "NOT_FOUND": "파일을 찾을 수 없습니다 (삭제되었거나 주소가 바뀜).",
        "NETWORK": "네트워크 연결 실패 또는 시간 초과입니다.",
        "INVALID": "제공처 응답 형식이 예상과 달라 사용하지 않았습니다.",
        "TOO_LARGE": "응답이 너무 커서 중단했습니다.",
        "CANCELLED": "취소했습니다.",
        "REDIRECT": "허용되지 않은 곳으로 이동하려 해서 중단했습니다.",
    }

    def __init__(self, code: str, detail: str = "", retry_after: float = 0.0, status: int = 0):
        self.code = code if code in self.MESSAGES else "NETWORK"
        self.detail = redact(detail)
        self.retry_after = retry_after
        self.status = status
        super().__init__(self.code)

    @property
    def message(self) -> str:
        m = self.MESSAGES[self.code]
        if self.status:
            m += f" (HTTP {self.status})"
        if self.code == "RATE_LIMIT" and self.retry_after:
            m += f" 약 {int(self.retry_after)}초 후 가능."
        return m


def _host_ok(host: str, rules: tuple[str, ...]) -> bool:
    return any(host.endswith(r) and len(host) > len(r) if r.startswith(".") else host == r for r in rules)


def url_allowed(url: object, provider: str, purpose: str) -> bool:
    """https + 허용 호스트 + 443 + 사용자정보·조각·제어문자·경로 탈출 없음."""
    if not isinstance(url, str) or not url or len(url) > 4096:
        return False
    if "\\" in url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        return False
    if re.search(r"%(?:00|0a|0d)", url, flags=re.IGNORECASE):
        return False
    table = _TABLES.get(purpose, {})
    if provider not in table:
        return False
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").lower()
        if (p.scheme != "https" or not host or "@" in p.netloc or p.port not in (None, 443) or p.fragment
                or not host.isascii() or host.endswith(".")):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        if not _host_ok(host, table[provider]):
            return False
        part = p.path
        for _ in range(3):
            if re.search(r"(?:^|/)\.{2}(?:/|$)", part) or "\\" in part:
                return False
            nxt = urllib.parse.unquote(part)
            if nxt == part:
                break
            part = nxt
    except ValueError:
        return False
    return True


def dns_is_public(url: str, resolver=None) -> bool:
    try:
        host = urllib.parse.urlsplit(url).hostname
        answers = (resolver or socket.getaddrinfo)(host, 443, type=socket.SOCK_STREAM)
        if not answers:
            return False
        return all(ipaddress.ip_address(a[4][0].split("%", 1)[0]).is_global for a in answers)
    except (OSError, ValueError, IndexError, TypeError):
        return False


_SECRET = re.compile(r"(?i)\b(key|api[_-]?key|token|authorization|signature|sig)\s*[=:]\s*[^\s&;,'\"]+")
_URL = re.compile(r"(?i)\bhttps?://[^\s<>\"']+")


def redact(text: object) -> str:
    if not isinstance(text, str):
        return ""
    t = _URL.sub("[URL]", text)
    return _SECRET.sub(lambda m: m.group(1) + "=[숨김]", t)[:300]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None


_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes = b""
    stream: object = None  # 다운로드: 열린 응답 객체 (호출자가 닫음)
    url: str = ""


def _retry_after(headers) -> float:
    try:
        v = float((headers or {}).get("Retry-After", "0"))
        return max(0.0, min(v, 3600.0))
    except (TypeError, ValueError):
        return 0.0


def open_url(url: str, provider: str, purpose: str, headers: dict | None = None, *, timeout: float = 20.0,
             max_redirects: int = 5, opener=None, resolver=None, check_dns: bool = True) -> Response:
    """허용 목록 URL을 열고 redirect는 직접 따라가며 매 단계 다시 검사한다. 응답 본문은 읽지 않는다 (stream)."""
    op = opener or _OPENER
    cur = url
    for _ in range(max_redirects + 1):
        if not url_allowed(cur, provider, purpose):
            raise NetError("URL_DISALLOWED" if cur == url else "REDIRECT")
        if check_dns and not dns_is_public(cur, resolver):
            raise NetError("DNS_UNSAFE")
        req = urllib.request.Request(cur, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            resp = op.open(req, timeout=timeout)
            return Response(resp.status, dict(resp.headers), stream=resp, url=cur)
        except urllib.error.HTTPError as e:
            hdrs = dict(e.headers or {})
            if e.code in (301, 302, 303, 307, 308) and hdrs.get("Location"):
                cur = urllib.parse.urljoin(cur, hdrs["Location"])
                e.close()
                continue
            e.close()
            if e.code == 429:
                raise NetError("RATE_LIMIT", retry_after=_retry_after(hdrs), status=429) from None
            if e.code in (401, 403):
                raise NetError("AUTH" if purpose == "api" else "HTTP_ERROR", status=e.code) from None
            if e.code == 404:
                raise NetError("NOT_FOUND", status=404) from None
            if e.code == 416:
                raise NetError("HTTP_ERROR", status=416) from None
            raise NetError("HTTP_ERROR", status=e.code, retry_after=_retry_after(hdrs)) from None
        except NetError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise NetError("NETWORK", str(e)) from None
    raise NetError("REDIRECT")


class ProviderClient:
    """제공처 1곳의 API 요청 (직렬화 + 간격 + 429/5xx 재시도 + 24시간 캐시). 작업 스레드에서만 호출한다."""

    def __init__(self, provider: str, cache_dir: Path | None, *, opener=None, resolver=None, check_dns: bool = True,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.provider = provider
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.opener, self.resolver, self.check_dns = opener, resolver, check_dns
        self.sleep, self.clock = sleep, clock
        self._lock = threading.Lock()
        self._next = 0.0
        self.last_rate: dict = {}
        self.calls = 0

    def _cache_path(self, url: str, params: dict, secret_params: tuple[str, ...]) -> Path | None:
        if not self.cache_dir:
            return None
        public = sorted((k, str(v)) for k, v in params.items() if k not in secret_params)
        h = hashlib.sha256(json.dumps([self.provider, url, public], ensure_ascii=False).encode("utf-8")).hexdigest()
        return self.cache_dir / f"{h[:32]}.json"

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None, *,
                 secret_params: tuple[str, ...] = (), retries: int = 2, use_cache: bool = True,
                 cancel: threading.Event | None = None) -> dict:
        params = dict(params or {})
        cp = self._cache_path(url, params, secret_params) if use_cache else None
        if cp and cp.is_file():
            try:
                saved = json.loads(cp.read_text(encoding="utf-8"))
                if time.time() - float(saved.get("t", 0)) < CACHE_TTL and isinstance(saved.get("data"), dict):
                    return saved["data"]
            except (OSError, ValueError):
                pass
        full = url + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        attempt = 0
        with self._lock:  # 같은 제공처는 한 번에 1개 요청 (병렬 금지)
            while True:
                if cancel is not None and cancel.is_set():
                    raise NetError("CANCELLED")
                wait = self._next - self.clock()
                if wait > 0:
                    self.sleep(wait)
                self._next = self.clock() + MIN_SPACING.get(self.provider, 1.0)
                self.calls += 1
                try:
                    r = open_url(full, self.provider, "api", {"Accept": "application/json", **(headers or {})},
                                 opener=self.opener, resolver=self.resolver, check_dns=self.check_dns)
                    try:
                        self.last_rate = {k: v for k, v in r.headers.items() if k.lower().startswith("x-ratelimit")}
                        ctype = str(r.headers.get("Content-Type", r.headers.get("content-type", ""))).lower()
                        body = r.stream.read(MAX_JSON_BYTES + 1)
                    finally:
                        r.stream.close()
                    if len(body) > MAX_JSON_BYTES:
                        raise NetError("TOO_LARGE")
                    if "json" not in ctype and not body.lstrip().startswith((b"{", b"[")):
                        raise NetError("INVALID")
                    try:
                        data = json.loads(body.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        raise NetError("INVALID") from None
                    if not isinstance(data, dict):
                        raise NetError("INVALID")
                    break
                except NetError as e:
                    retryable = e.code == "NETWORK" or (e.code == "HTTP_ERROR" and e.status >= 500) or (
                        e.code == "RATE_LIMIT" and 0 < e.retry_after <= MAX_RETRY_AFTER)
                    if not retryable or attempt >= retries:
                        raise
                    attempt += 1
                    self.sleep(e.retry_after if e.retry_after else min(8.0, 1.5 * 2 ** attempt))
        if cp:
            try:
                cp.parent.mkdir(parents=True, exist_ok=True)
                tmp = cp.with_suffix(".tmp")
                tmp.write_text(json.dumps({"t": time.time(), "data": data}, ensure_ascii=False), encoding="utf-8")
                tmp.replace(cp)
            except OSError:
                pass
        return data


def fetch_bytes(url: str, provider: str, purpose: str = "poster", *, limit: int = 3_000_000, timeout: float = 15.0,
                opener=None, resolver=None, check_dns: bool = True) -> bytes:
    """썸네일 같은 작은 파일 (허용 호스트, 크기 제한)."""
    r = open_url(url, provider, purpose, timeout=timeout, opener=opener, resolver=resolver, check_dns=check_dns)
    try:
        data = r.stream.read(limit + 1)
    finally:
        r.stream.close()
    if len(data) > limit:
        raise NetError("TOO_LARGE")
    return data
