#!/bin/bash
# Install, inspect or remove vanpi's USB 2.0 block for the disk hub chain.
# Run as pi from anywhere; the privileged steps use sudo. Contract: docs/agent-context.md,
# section "USB 2.0 block for the disk hub chain".
#
#   status     read-only; exit 0 only when the installed copies match this checkout, the
#              watcher is enabled and active, and the block holds
#   install    copy the three files root-owned, reload udev and systemd, enable and start
#   uninstall  stop and disable the watcher and remove the three installed files
#              (the port stays off until the next boot or hub re-enumeration)
set -u

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
unit=vanpi-usb2-disk-hub-off.service
program=/usr/local/sbin/vanpi-usb2-disk-hub-off
rule=/etc/udev/rules.d/70-vanpi-usb2-disk-hub-off.rules
installed_unit=/etc/systemd/system/$unit

status() {
  local problems=0 pair source target
  for pair in "vanpi-usb2-disk-hub-off:$program" "70-vanpi-usb2-disk-hub-off.rules:$rule" "$unit:$installed_unit"; do
    source=$here/${pair%%:*}
    target=${pair#*:}
    if [[ -L "$target" || ! -f "$target" ]]; then
      echo "missing or not a regular file: $target"
      problems=1
    elif ! cmp -s -- "$source" "$target"; then
      echo "differs from this checkout: $target"
      problems=1
    elif [[ "$(stat -c %U:%G "$target")" != root:root ]]; then
      echo "not owned by root: $target"
      problems=1
    fi
  done
  echo "watcher: $(systemctl is-enabled "$unit" 2>&1), $(systemctl is-active "$unit" 2>&1)"
  [[ "$(systemctl is-enabled "$unit" 2>/dev/null)" == enabled ]] || problems=1
  [[ "$(systemctl is-active "$unit" 2>/dev/null)" == active ]] || problems=1
  if [[ -x "$program" ]]; then
    echo "block: $("$program" status)"
    "$program" status > /dev/null || problems=1
  fi
  return "$problems"
}

install_all() {
  sudo install -o root -g root -m 0755 -- "$here/vanpi-usb2-disk-hub-off" "$program" || return 1
  # After the program is in place: verify checks that ExecStart exists.
  systemd-analyze verify "$here/$unit" || return 1
  sudo install -o root -g root -m 0644 -- "$here/70-vanpi-usb2-disk-hub-off.rules" "$rule" || return 1
  sudo install -o root -g root -m 0644 -- "$here/$unit" "$installed_unit" || return 1
  sudo udevadm control --reload || return 1
  sudo systemctl daemon-reload || return 1
  sudo systemctl enable "$unit" || return 1
  # restart, not start: an update must replace a running watcher.
  sudo systemctl restart "$unit" || return 1
  sleep 3
  status
}

uninstall_all() {
  sudo systemctl disable --now "$unit"
  sudo rm -f -- /usr/local/sbin/vanpi-usb2-disk-hub-off
  sudo rm -f -- /etc/udev/rules.d/70-vanpi-usb2-disk-hub-off.rules
  sudo rm -f -- /etc/systemd/system/vanpi-usb2-disk-hub-off.service
  sudo udevadm control --reload
  sudo systemctl daemon-reload
}

case "${1:-}" in
  status) status ;;
  install) install_all ;;
  uninstall) uninstall_all ;;
  *)
    echo "usage: ${0##*/} status|install|uninstall" >&2
    exit 2
    ;;
esac
