"""가짜 SSH 서버 + 실제 worker 관리 명령 (예약 LIVE). 서버 파일은 tmp 폴더, 실제 OCI 접속 없음."""
import io
import json
import shlex
import sys
from contextlib import redirect_stdout
from pathlib import Path

from app.cloud_client import KEY_WRITE_CMD
from app.cloud_model import REMOTE_MEDIA, REMOTE_WORKER, SCHEDULER_SERVICE
from cloud_fakes import FakeRemote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cloud"))
import long_live_worker as w  # noqa: E402


class SchedRemote(FakeRemote):
    def __init__(self, root: Path, **kw):
        super().__init__(**kw)
        self.root = Path(root)
        self.jobs_dir, self.state_dir, self.media_dir = self.root / "jobs", self.root / "state", self.root / "media"
        self.key_file = self.root / "stream.key"
        for d in (self.jobs_dir, self.state_dir, self.media_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.installed = True
        self.worker_version = 3
        self.scheduler_enabled = False
        self.fail_add = False
        self.admin_calls: list[list[str]] = []

    def store(self):
        return w.JobStore(self.jobs_dir, self.state_dir)

    def _sync_media(self):
        for name, data in self.files.items():
            if name.startswith(REMOTE_MEDIA + "/") and not name.endswith(".part"):
                (self.media_dir / name.rsplit("/", 1)[1]).write_bytes(data)

    def handle(self, cmd, input):
        if cmd == KEY_WRITE_CMD:
            self.key_file.write_text(input, encoding="utf-8")
            return super().handle(cmd, input)
        p = shlex.split(cmd)
        if p[:4] == ["sudo", "-n", "python3", REMOTE_WORKER]:
            self.admin_calls.append(p[4:])
            if self.fail_add and "--add-job" in p:
                return json.dumps({"ok": False, "error": "서버 저장 실패 (테스트)"}) + "\n", 3, ""
            self._sync_media()
            argv = p[4:] + ["--jobs-dir", str(self.jobs_dir), "--state-dir", str(self.state_dir),
                            "--media-dir", str(self.media_dir), "--key-file", str(self.key_file)]
            old_stdin = sys.stdin
            sys.stdin = io.StringIO(input or "")
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    rc = w.main(argv)
            finally:
                sys.stdin = old_stdin
            return buf.getvalue(), rc, ""
        if cmd == f"sudo -n systemctl enable --now {SCHEDULER_SERVICE}":
            self.scheduler_enabled = True
            return "", 0, ""
        if cmd == f"systemctl is-active {SCHEDULER_SERVICE}":
            return ("active\n", 0, "") if self.scheduler_enabled else ("inactive\n", 3, "")
        return super().handle(cmd, input)

    def uploads(self) -> int:
        return sum(1 for c in self.calls if c.get("input") == "<stream>")
