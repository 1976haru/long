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
        # 다채널 업로드: access token → 채널 (없으면 self.channel)
        self.token_channels: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}  # 업로드 세션
        self.videos: dict[str, dict] = {}
        self.thumbnails: dict[str, bytes] = {}
        self.upload_fail: deque = deque()  # ("chunk", status, keep_bytes)
        self.expire_sessions = False
        self.publish_override = None  # 업로드 후 저장되는 publishAt을 일부러 다르게
        self.private_only = False  # 검수 안 된 API 프로젝트: 모든 업로드/수정이 private로 고정
        self.refresh_channels: dict[str, dict] = {}  # refresh token → 채널 (다채널 OAuth)
        self.code_refresh: dict[str, str] = {}  # 인증 code → refresh token
        self.token_log: list[tuple[str, str]] = []  # (grant_type, refresh_token) 순서 기록
        # 댓글: thread id → {"id", "video", "channel"(영상 채널), "moderation", "top": comment, "replies": [comment]}
        self.threads: dict[str, dict] = {}
        self.comments_disabled: set[str] = set()
        self.comment_clock = None  # callable → epoch (없으면 time.time)
        # 재생목록: id → {"id", "title", "channel", "privacy", "items": [video_id]}
        self.playlists: dict[str, dict] = {}
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

            def do_PUT(self):  # noqa: N802
                self._handle("PUT")

            def do_DELETE(self):  # noqa: N802
                self._handle("DELETE")

            def _send_raw(self, status, payload, headers):
                body = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                parts = urllib.parse.urlsplit(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
                raw = self._read()
                if parts.path.startswith("/upload/youtube/v3/"):
                    op = "upload/" + parts.path.removeprefix("/upload/youtube/v3/")
                    auth = self.headers.get("Authorization", "")
                    fake.auth_headers.append(auth)
                    token = auth.removeprefix("Bearer ")
                    fake.calls.append((method, op, q, None))
                    if token not in fake.valid_tokens:
                        return self._send(401, {"error": {"code": 401, "errors": [{"reason": "authError"}]}})
                    if fake.fail and op.startswith(fake.fail[0][0]):
                        _, status, reason = fake.fail.popleft()
                        return self._send(status, {"error": {"code": status, "errors": [{"reason": reason}]}})
                    status, payload, headers = fake.upload_route(method, op, q, raw, dict(self.headers), token)
                    return self._send_raw(status, payload, headers)
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
                fake.current_token = auth.removeprefix("Bearer ")
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
            out["refresh_token"] = self.code_refresh.get(form.get("code", ""), FAKE_REFRESH)
            refresh = out["refresh_token"]
        else:
            refresh = form.get("refresh_token", "")
        self.token_log.append((form.get("grant_type", ""), refresh))
        if refresh in self.refresh_channels:
            self.token_channels[access] = self.refresh_channels[refresh]
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

    def channel_for(self, token):
        return self.token_channels.get(token, self.channel)

    # ---------- resumable upload ----------
    def upload_route(self, method, op, q, raw, headers, token):
        h = {k.lower(): v for k, v in headers.items()}
        if op == "upload/videos" and method == "POST" and q.get("uploadType") == "resumable" and "upload_id" not in q:
            sid = self._id("up")
            self.sessions[sid] = {"total": int(h["x-upload-content-length"]), "data": bytearray(),
                                  "body": json.loads(raw.decode()), "channel": self.channel_for(token)["id"]}
            return 200, None, {"Location": f"{self.base}/upload/youtube/v3/videos?uploadType=resumable&upload_id={sid}"}
        if op == "upload/videos" and method == "PUT":
            s = self.sessions.get(q.get("upload_id", ""))
            if s is None or self.expire_sessions:
                return 404, {"error": {"code": 404, "errors": [{"reason": "notFound"}]}}, {}
            rng = h.get("content-range", "")
            have = len(s["data"])
            ack = {"Range": f"bytes=0-{have - 1}"} if have else {}
            if rng.startswith("bytes */"):
                if "video" in s:
                    return 200, s["video"], {}
                return 308, None, ack
            a_b, total = rng.removeprefix("bytes ").split("/")
            a, b = (int(x) for x in a_b.split("-"))
            if a != have:
                return 308, None, ack
            if self.upload_fail:
                item = self.upload_fail.popleft()  # ("chunk", status, keep_bytes[, headers])
                _, status, keep = item[:3]
                if keep:
                    s["data"] += raw
                return status, {"error": {"code": status, "errors": [{"reason": "backendError"}]}}, (
                    item[3] if len(item) > 3 else {})
            s["data"] += raw
            if len(s["data"]) < s["total"]:
                return 308, None, {"Range": f"bytes=0-{len(s['data']) - 1}"}
            vid = self._id("vid")
            st = dict(s["body"]["status"])
            if self.publish_override and "publishAt" in st:
                st["publishAt"] = self.publish_override
            if self.private_only:
                st = {"privacyStatus": "private", "selfDeclaredMadeForKids": st.get("selfDeclaredMadeForKids", False)}
            self.videos[vid] = {"id": vid, "snippet": dict(s["body"]["snippet"]), "status": st,
                                "channel": s["channel"], "size": len(s["data"]), "bytes": bytes(s["data"])}
            s["video"] = {"id": vid, "snippet": s["body"]["snippet"], "status": st}
            return 200, s["video"], {}
        if op == "upload/thumbnails/set" and method == "POST":
            if q.get("videoId") not in self.videos and q.get("videoId") not in self.broadcasts:
                return 404, {"error": {"code": 404, "errors": [{"reason": "videoNotFound"}]}}, {}
            self.thumbnails[q["videoId"]] = raw
            return 200, {"items": [{"default": {"url": "https://i.ytimg.com/x.jpg"}}]}, {}
        return 404, {"error": {"code": 404, "errors": [{"reason": "notFound"}]}}, {}

    # ---------- comments ----------
    def _now_iso(self):
        import time as _t
        t = self.comment_clock() if self.comment_clock else _t.time()
        return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(t))

    def _comment(self, cid, text, author_channel, author, published, parent=""):
        sn = {"textOriginal": text, "textDisplay": text, "authorDisplayName": author,
              "authorChannelId": {"value": author_channel}, "publishedAt": published, "moderationStatus": "published"}
        if parent:
            sn["parentId"] = parent
        return {"id": cid, "snippet": sn}

    def video_channel(self, vid):
        v = self.videos.get(vid)
        return v["channel"] if v else ""

    def add_viewer_comment(self, video_id, text, *, author_channel="UCviewer000000000000001", author="viewer",
                           published=None, moderation="published", channel=None):
        tid = self._id("thr")
        top = self._comment(tid, text, author_channel, author, published or self._now_iso())
        self.threads[tid] = {"id": tid, "video": video_id, "channel": channel or self.video_channel(video_id),
                             "moderation": moderation, "top": top, "replies": []}
        return tid

    def add_reply(self, thread_id, text, *, author_channel, author="owner"):
        cid = self._id("rep")
        self.threads[thread_id]["replies"].append(
            self._comment(cid, text, author_channel, author, self._now_iso(), thread_id))
        return cid

    def thread_view(self, t):
        top = json.loads(json.dumps(t["top"]))
        top["snippet"]["moderationStatus"] = t["moderation"]
        return {"id": t["id"], "snippet": {"videoId": t["video"], "channelId": t["channel"], "topLevelComment": top,
                                           "totalReplyCount": len(t["replies"]), "canReply": True},
                "replies": {"comments": t["replies"][:5]}}  # 실제 API처럼 replies는 일부만

    def comment_route(self, method, op, q, body):
        token = getattr(self, "current_token", "")
        me = self.channel_for(token)
        if op == "commentThreads" and method == "GET":
            ch = q.get("allThreadsRelatedToChannelId", "")
            rows = [t for t in self.threads.values() if t["channel"] == ch
                    and (q.get("moderationStatus", "published") != "published" or t["moderation"] == "published")]
            rows.sort(key=lambda t: (t["top"]["snippet"]["publishedAt"], t["id"]), reverse=True)
            start = int(q.get("pageToken") or 0)
            n = int(q.get("maxResults", 20))
            page = rows[start:start + n]
            out = {"items": [self.thread_view(t) for t in page]}
            if start + n < len(rows):
                out["nextPageToken"] = str(start + n)
            return 200, out
        if op == "commentThreads" and method == "POST":
            vid = body["snippet"]["videoId"]
            v = self.videos.get(vid)
            if v is None:
                return self._err(404, "videoNotFound")
            if vid in self.comments_disabled:
                return self._err(403, "commentsDisabled")
            if v["status"].get("privacyStatus") == "private":
                return self._err(403, "forbidden")
            tid = self._id("thr")
            top = self._comment(tid, body["snippet"]["topLevelComment"]["snippet"]["textOriginal"], me["id"],
                                me["title"], self._now_iso())
            self.threads[tid] = {"id": tid, "video": vid, "channel": v["channel"], "moderation": "published",
                                 "top": top, "replies": []}
            return 200, self.thread_view(self.threads[tid])
        if op == "comments" and method == "GET":
            t = self.threads.get(q.get("parentId", ""))
            return (200, {"items": list(t["replies"])}) if t else self._err(404, "commentNotFound")
        if op == "comments" and method == "POST":
            parent = body["snippet"]["parentId"]
            t = self.threads.get(parent)
            if t is None:
                return self._err(404, "parentCommentNotFound")
            if t["video"] in self.comments_disabled:
                return self._err(403, "commentsDisabled")
            cid = self.add_reply(parent, body["snippet"]["textOriginal"], author_channel=me["id"], author=me["title"])
            return 200, t["replies"][-1] if t["replies"][-1]["id"] == cid else {"id": cid}
        if op == "comments" and method == "DELETE":
            cid = q.get("id", "")
            top = self.threads.get(cid)
            if top is not None and top["top"]["snippet"]["authorChannelId"]["value"] == me["id"]:
                del self.threads[cid]
                return 204, {}
            for t in self.threads.values():
                for reply in list(t["replies"]):
                    if reply["id"] == cid and reply["snippet"]["authorChannelId"]["value"] == me["id"]:
                        t["replies"].remove(reply)
                        return 204, {}
            return self._err(403, "forbidden")
        return None

    def set_privacy(self, vid, privacy):
        self.videos[vid]["status"] = {k: v for k, v in self.videos[vid]["status"].items() if k != "publishAt"}
        self.videos[vid]["status"]["privacyStatus"] = privacy

    def replies_to(self, thread_id):
        return self.threads[thread_id]["replies"]

    # ---------- playlists ----------
    def add_playlist(self, title, channel_id, privacy="public"):
        pid = self._id("PL")
        self.playlists[pid] = {"id": pid, "title": title, "channel": channel_id, "privacy": privacy, "items": []}
        return pid

    def playlist_view(self, p):
        return {"id": p["id"], "snippet": {"title": p["title"], "channelId": p["channel"]},
                "status": {"privacyStatus": p["privacy"]}, "contentDetails": {"itemCount": len(p["items"])}}

    def playlist_route(self, method, op, q, body):
        me = self.channel_for(getattr(self, "current_token", ""))
        if op == "playlists" and method == "GET":
            if "id" in q:
                rows = [p for p in self.playlists.values() if p["id"] == q["id"]]
            else:
                rows = [p for p in self.playlists.values() if p["channel"] == me["id"]]  # mine=true
            start, n = int(q.get("pageToken") or 0), int(q.get("maxResults", 5))
            out = {"items": [self.playlist_view(p) for p in rows[start:start + n]]}
            if start + n < len(rows):
                out["nextPageToken"] = str(start + n)
            return 200, out
        if op == "playlists" and method == "POST":
            pid = self.add_playlist(body["snippet"]["title"], me["id"], body.get("status", {}).get("privacyStatus", "public"))
            return 200, self.playlist_view(self.playlists[pid])
        if op == "playlistItems" and method == "POST":
            pid = body["snippet"]["playlistId"]
            vid = body["snippet"]["resourceId"]["videoId"]
            p = self.playlists.get(pid)
            if p is None:
                return self._err(404, "playlistNotFound")
            if p["channel"] != me["id"]:
                return self._err(403, "playlistItemsNotAccessible")
            if vid not in self.videos and vid not in self.broadcasts:
                return self._err(404, "videoNotFound")
            if vid in p["items"]:
                return self._err(409, "videoAlreadyInPlaylist")
            p["items"].append(vid)
            return 200, {"id": self._id("PLI"), "snippet": {"playlistId": pid, "resourceId": {"videoId": vid}}}
        return None

    def route(self, method, op, q, body):
        if op in ("playlists", "playlistItems"):
            got = self.playlist_route(method, op, q, body)
            if got is not None:
                return got
        if op in ("commentThreads", "comments"):
            got = self.comment_route(method, op, q, body)
            if got is not None:
                return got
        if op == "videos" and method == "GET":
            v = self.videos.get(q.get("id", ""))
            if v is None and q.get("id") in self.broadcasts:
                b = self.broadcasts[q["id"]]
                v = {"id": b["id"], "snippet": b["snippet"], "status": b["status"]}
            return 200, {"items": [{"id": v["id"], "snippet": v["snippet"], "status": v["status"]}] if v else []}
        if op == "videos" and method == "PUT":
            v = self.videos.get(body["id"]) or self.broadcasts.get(body["id"])
            if v is None:
                return self._err(404, "videoNotFound")
            part = q.get("part", "")
            if part == "status":
                v["status"] = {**v["status"], **body["status"]}
                if self.private_only:
                    v["status"] = {"privacyStatus": "private"}
                return 200, {"id": body["id"], "status": v["status"]}
            # 실제 API처럼: 요청에 없는 '수정 가능' snippet 값은 지워진다 (scheduledStartTime 같은 방송 값은 그대로)
            keep = {k: x for k, x in v["snippet"].items()
                    if k not in ("title", "description", "categoryId", "tags", "defaultLanguage")}
            v["snippet"] = {**keep, **body["snippet"]}
            return 200, {"id": body["id"], "snippet": v["snippet"]}
        if op == "channels" and method == "GET":
            ch = self.channel_for(getattr(self, "current_token", ""))
            return 200, {"items": [{"id": ch["id"], "snippet": {"title": ch["title"]}}]}
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
