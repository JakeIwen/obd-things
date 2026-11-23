#!/bin/bash
# Install, inspect or remove vanpi's USB 2.0 block for the disk hub chain.
# Run as pi from anywhere; the privileged steps use sudo. Contract: docs/agent-context.md,
# section "USB 2.0 block for the disk hub chain".
#
#   status     read-only; exit 0 only when the installed copies match this checkout (content,
#              root ownership, mode), the watcher is enabled and active, and the block holds
#   install    copy the program and the unit root-owned, enable and (re)start the watcher
#   uninstall  stop and disable the watcher, remove both installed files and verify it; exit 1
#              when anything is left (the port itself stays off until its attribute is written
#              0, the hub re-enumerates or the Pi boots)
set -u

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
unit=vanpi-usb2-disk-hub-off.service
program=/usr/local/sbin/vanpi-usb2-disk-hub-off
installed_unit=/etc/systemd/system/$unit

# source name : installed path : mode
copies=(
  "vanpi-usb2-disk-hub-off:$program:755"
  "$unit:$installed_unit:644"
)

# Prints why an installed copy is not the checkout's file; silent and 0 when it is.
copy_problem() {
  local source=$1 target=$2 mode=$3
  if [[ -L "$target" || ! -f "$target" ]]; then
    echo "missing or not a regular file: $target"
  elif ! cmp -s -- "$source" "$target"; then
    echo "differs from this checkout: $target"
  elif [[ "$(stat -c %U:%G:%a "$target")" != "root:root:$mode" ]]; then
    echo "not root:root mode $mode: $target"
  else
    return 0
  fi
  return 1
}

status() {
  local problems=0 entry source target mode program_verified=1 block
  for entry in "${copies[@]}"; do
    IFS=: read -r source target mode <<< "$entry"
    if ! copy_problem "$here/$source" "$target" "$mode"; then
      problems=1
      [[ "$target" == "$program" ]] && program_verified=0
    fi
  done
  echo "watcher: $(systemctl is-enabled "$unit" 2>&1), $(systemctl is-active "$unit" 2>&1)"
  [[ "$(systemctl is-enabled "$unit" 2>/dev/null)" == enabled ]] || problems=1
  [[ "$(systemctl is-active "$unit" 2>/dev/null)" == active ]] || problems=1
  if (( program_verified )); then
    # One run: the line shown and the verdict come from the same answer.
    if block=$("$program" status); then
      echo "block: $block"
    else
      echo "block: ${block:-no answer}"
      problems=1
    fi
  else
    echo "block: not checked (the installed program is not this checkout's)"
  fi
  return "$problems"
}

install_all() {
  # The program goes first: the unit is verified against it before the unit is published.
  sudo install -o root -g root -m 0755 -- "$here/vanpi-usb2-disk-hub-off" "$program" || return 1
  if ! systemd-analyze verify "$here/$unit"; then
    echo "install STOPPED: the unit failed verification and was not published. The program copy"
    echo "was written; a watcher installed earlier keeps running and would start it at its next"
    echo "restart. See: $0 status"
    return 1
  fi
  sudo install -o root -g root -m 0644 -- "$here/$unit" "$installed_unit" || return 1
  # restart, not start: an update must replace a running watcher.
  if ! { sudo systemctl daemon-reload && sudo systemctl enable "$unit" &&
    sudo systemctl restart "$unit"; }; then
    echo "install INCOMPLETE: both files are in place, but this version of the watcher was not"
    echo "confirmed started. See: $0 status and systemctl status $unit"
    return 1
  fi
  sleep 3
  status
}

uninstall_all() {
  local failed=0 target
  if [[ -e "$installed_unit" ]] || systemctl is-active --quiet "$unit"; then
    sudo systemctl disable --now "$unit" || failed=1
  fi
  sudo rm -f -- /usr/local/sbin/vanpi-usb2-disk-hub-off || failed=1
  sudo rm -f -- /etc/systemd/system/vanpi-usb2-disk-hub-off.service || failed=1
  sudo systemctl daemon-reload || failed=1
  if systemctl is-active --quiet "$unit"; then
    echo "the watcher is still running: $unit"
    failed=1
  fi
  for target in "$program" "$installed_unit"; do
    if [[ -e "$target" || -L "$target" ]]; then
      echo "still present: $target"
      failed=1
    fi
  done
  if (( failed )); then
    echo "uninstall INCOMPLETE"
    return 1
  fi
  echo "uninstalled; port 2 stays off until its disable attribute is written 0, the hub re-enumerates or the Pi boots"
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
