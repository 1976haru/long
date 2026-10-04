#!/usr/bin/env bash
# Long Live worker 설치 (Ubuntu/Debian). root로 실행: sudo bash install.sh <업로드용 SSH 사용자>
# 서비스는 설치만 하고 켜지 않는다. LIVE 시작 시 PC 프로그램이 enable --now 한다.
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
UPLOAD_USER="${1:-${SUDO_USER:-ubuntu}}"
if [ "$(id -u)" -ne 0 ]; then echo "ERROR root required" >&2; exit 1; fi
id "$UPLOAD_USER" >/dev/null 2>&1 || { echo "ERROR unknown user $UPLOAD_USER" >&2; exit 1; }
if ! id longlive >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir /opt/long-live --shell /usr/sbin/nologin longlive
fi
install -d -m 0755 -o root -g root /opt/long-live /opt/long-live/worker
install -d -m 0755 -o "$UPLOAD_USER" -g longlive /opt/long-live/media
install -d -m 0755 -o longlive -g longlive /opt/long-live/state /opt/long-live/logs
install -d -m 0750 -o root -g longlive /etc/long-live
install -m 0644 -o root -g root "$SRC_DIR/long_live_worker.py" /opt/long-live/worker/long_live_worker.py
install -m 0644 -o root -g root "$SRC_DIR/long-live.service" /etc/systemd/system/long-live.service
install -m 0755 -o root -g root "$SRC_DIR/uninstall.sh" /opt/long-live/worker/uninstall.sh
systemctl daemon-reload
echo INSTALL_OK
