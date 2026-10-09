#!/usr/bin/env bash
# Long Live worker 제거. 기본은 영상(media)을 남긴다. --purge 시 영상/사용자까지 삭제.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "ERROR root required" >&2; exit 1; fi
systemctl disable --now long-live-scheduler.service 2>/dev/null || true
systemctl disable --now long-live.service 2>/dev/null || true
rm -f /etc/systemd/system/long-live.service /etc/systemd/system/long-live-scheduler.service
systemctl daemon-reload
rm -rf /opt/long-live/worker /opt/long-live/state /opt/long-live/logs /etc/long-live
if [ "${1:-}" = "--purge" ]; then
  rm -rf /opt/long-live
  userdel longlive 2>/dev/null || true
fi
echo UNINSTALL_OK
