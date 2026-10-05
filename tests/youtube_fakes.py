"""가짜 YouTube Data API v3 + Google OAuth token 서버 (실제 로컬 HTTP). 실제 Google/YouTube에 접속하지 않는다."""
import http.server
import json
import threading
import urllib.parse
from collections import deque

FAKE_STREAM_NAME = "fake-stream-name-0000-not-real"
FAKE_ACCESS = "fake-access-token-0000"
FAKE_REFRESH = "fake-refresh-token-0000-not-real"


class FakeYouTube:
    def __init__(self):
        self.channel = {"id": "UCfake0001", "title": "Old Pop Lounge"}
        self.streams: dict[str, dict] = {}
        self.broadcasts: dict[str, dict] = {}
        self.stream_active = True
        self.valid_tokens = {FAKE_ACCESS}
        self.calls: list[tuple] = []  # (method, op, query, body)
        self.auth_headers: list[str] = []
        self.fail: deque = deque()  # (op_prefix, status, reason)
        self.token_mode = "ok"  # ok | invalid_grant | server
        self.token_forms: list[dict] = []
        self.issued = 0
        self._n = 0
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def _handle(self, method):
                parts = urllib.parse.urlsplit(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
                raw = self._read()
                if parts.path == "/token":
                    form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                    fake.token_forms.append(form)
                    status, payload = fake.token(form)
                    return self._send(status, payload)
                body = json.loads(raw.decode() or "null") if raw else None
                op = parts.path.replace("/youtube/v3/", "")
                fake.calls.append((method, op, q, body))
                auth = self.headers.get("Authorization", "")
                fake.auth_headers.append(auth)
                if auth.removeprefix("Bearer ") not in fake.valid_tokens:
                    return self._send(401, {"error": {"code": 401, "errors": [{"reason": "authError"}]}})
                if fake.fail and op.startswith(fake.fail[0][0]):
                    _, status, reason = fake.fail.popleft()
                    return self._send(status, {"error": {"code": status, "errors": [{"reason": reason}]}})
                status, payload = fake.route(method, op, q, body)
                self._send(status, payload)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.api_base = self.base + "/youtube/v3"
        self.token_uri = self.base + "/token"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # ---------- oauth ----------
    def token(self, form):
        if self.token_mode == "invalid_grant":
            return 400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
        if self.token_mode == "server":
            return 503, {"error": "temporarily_unavailable"}
        self.issued += 1
        access = f"fake-access-token-{self.issued:04d}"
        self.valid_tokens.add(access)
        out = {"access_token": access, "expires_in": 3599, "scope": "https://www.googleapis.com/auth/youtube",
               "token_type": "Bearer"}
        if form.get("grant_type") == "authorization_code":
            out["refresh_token"] = FAKE_REFRESH
        return 200, out

    # ---------- api ----------
    def _id(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def _err(self, status, reason):
        return status, {"error": {"code": status, "errors": [{"reason": reason}]}}

    def stream_view(self, s):
        s = json.loads(json.dumps(s))
        s["status"]["streamStatus"] = "active" if self.stream_active else "inactive"
        return s

    def route(self, method, op, q, body):
        if op == "channels" and method == "GET":
            return 200, {"items": [{"id": self.channel["id"], "snippet": {"title": self.channel["title"]}}]}
        if op == "liveStreams" and method == "GET":
            if "id" in q:
                items = [self.stream_view(self.streams[q["id"]])] if q["id"] in self.streams else []
            else:
                items = [self.stream_view(s) for s in self.streams.values()]
            return 200, {"items": items}
        if op == "liveStreams" and method == "POST":
            sid = self._id("stream")
            self.streams[sid] = {"id": sid, "snippet": body["snippet"], "contentDetails": body["contentDetails"],
                                 "cdn": {**body["cdn"], "ingestionInfo": {
                                     "ingestionAddress": "rtmp://a.rtmp.youtube.com/live2",
                                     "rtmpsIngestionAddress": "rtmps://a.rtmps.youtube.com/live2",
                                     "streamName": FAKE_STREAM_NAME}},
                                 "status": {"streamStatus": "ready", "healthStatus": {"status": "good"}}}
            return 200, self.stream_view(self.streams[sid])
        if op == "liveBroadcasts" and method == "POST":
            bid = self._id("bcast")
            self.broadcasts[bid] = {"id": bid, "snippet": body["snippet"], "contentDetails": dict(body["contentDetails"]),
                                    "status": {**body["status"], "lifeCycleStatus": "created"}, "body": body}
            return 200, self.broadcasts[bid]
        if op == "liveBroadcasts" and method == "GET":
            b = self.broadcasts.get(q.get("id", ""))
            if not b:
                return 200, {"items": []}
            if b["status"]["lifeCycleStatus"] == "liveStarting":
                b["status"]["lifeCycleStatus"] = "live"  # 다음 조회에서 live
            return 200, {"items": [b]}
        if op == "liveBroadcasts/bind" and method == "POST":
            b = self.broadcasts.get(q["id"])
            if not b:
                return self._err(404, "liveBroadcastNotFound")
            if q["streamId"] not in self.streams:
                return self._err(404, "liveStreamNotFound")
            b["contentDetails"]["boundStreamId"] = q["streamId"]
            if b["status"]["lifeCycleStatus"] == "created":
                b["status"]["lifeCycleStatus"] = "ready"
            return 200, b
        if op == "liveBroadcasts/transition" and method == "POST":
            b = self.broadcasts.get(q["id"])
            if not b:
                return self._err(404, "liveBroadcastNotFound")
            want, cur = q["broadcastStatus"], b["status"]["lifeCycleStatus"]
            if want == "live":
                if cur in ("live", "liveStarting"):
                    return self._err(400, "redundantTransition")
                if cur not in ("ready", "testing"):
                    return self._err(400, "invalidTransition")
                if not self.stream_active:
                    return self._err(403, "errorStreamInactive")
                b["status"]["lifeCycleStatus"] = "liveStarting"
            elif want == "complete":
                if cur == "complete":
                    return self._err(400, "redundantTransition")
                if cur not in ("live", "liveStarting", "testing"):
                    return self._err(400, "invalidTransition")
                b["status"]["lifeCycleStatus"] = "complete"
            return 200, b
        return self._err(404, "notFound")

    def ops(self):
        return [(m, op) for m, op, _, _ in self.calls]

    def status_of(self, bid):
        return self.broadcasts[bid]["status"]["lifeCycleStatus"]


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s
