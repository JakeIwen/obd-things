#!/bin/sh
# First deploy of DTC research on vanpi (run with sudo; idempotent). --dry-run prints each step and
# changes nothing. The steps and the checks after them are in .agent/handoffs/dtc-research.md
# ("Deploy runbook") and docs/dtc-research.md.
set -eu

DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; fi
HERE=$(cd "$(dirname "$0")" && pwd)
IMAGE=/var/lib/vanresearch.img
MOUNTPOINT=/var/lib/vanresearch
CLI_ROOT=/opt/van-research-cli

run() {
  echo "+ $*"
  if [ "$DRY" -eq 0 ]; then "$@"; fi
}

if [ "$DRY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  echo "run this with sudo (or pass --dry-run)" >&2
  exit 2
fi

if [ -e "$IMAGE" ]; then
  echo "= $IMAGE exists; keeping it"
else
  run fallocate -l 224M "$IMAGE"
  run chmod 600 "$IMAGE"
  run mkfs.ext4 -q -m 0 -L vanresearch "$IMAGE"
fi
run install -m 0644 "$HERE/van-assistant.tmpfiles.conf" /etc/tmpfiles.d/van-assistant.conf
run systemd-tmpfiles --create /etc/tmpfiles.d/van-assistant.conf
run install -m 0644 "$HERE/var-lib-vanresearch.mount" /etc/systemd/system/var-lib-vanresearch.mount
run install -m 0644 "$HERE/van-telemetry-research.service" /etc/systemd/system/van-telemetry-research.service
run install -m 0644 "$HERE/van-telemetry-advisor.service" /etc/systemd/system/van-telemetry-advisor.service
run systemctl daemon-reload
run systemctl enable --now var-lib-vanresearch.mount
run chown pi:pi "$MOUNTPOINT"
run chmod 700 "$MOUNTPOINT"
run install -d -o root -g root -m 0755 "$CLI_ROOT"
run systemctl enable --now van-telemetry-research.service
run systemctl restart van-telemetry-advisor.service
run systemctl --no-pager --lines=0 status var-lib-vanresearch.mount van-telemetry-research.service van-telemetry-advisor.service
