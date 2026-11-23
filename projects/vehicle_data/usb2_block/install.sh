#!/bin/bash
# Install, inspect or remove vanpi's USB 2.0 block for the disk hub chain.
# Run as pi from anywhere; the privileged steps use sudo. Contract: docs/agent-context.md,
# section "USB 2.0 block for the disk hub chain".
#
#   status     read-only; exit 0 only when the installed copies match this checkout (content,
#              root ownership, mode), the watcher is enabled and active, and the block holds
#   install    copy the three files root-owned, reload udev and systemd, enable and start
#   uninstall  stop and disable the watcher, remove the three installed files and verify it;
#              exit 1 with the remaining step when anything is left, a boot image included
#              (the port itself stays off until the next boot or hub re-enumeration)
set -u

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
unit=vanpi-usb2-disk-hub-off.service
rule_name=70-vanpi-usb2-disk-hub-off.rules
program=/usr/local/sbin/vanpi-usb2-disk-hub-off
rule=/etc/udev/rules.d/$rule_name
installed_unit=/etc/systemd/system/$unit

# source name : installed path : mode
copies=(
  "vanpi-usb2-disk-hub-off:$program:755"
  "$rule_name:$rule:644"
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

# initramfs-tools copies custom rules from /etc/udev/rules.d into every image it builds, so a
# kernel update while the rule is installed puts a second copy where uninstall cannot reach.
# Prints the images that carry the rule (or cannot be read); returns 0 when there is one.
boot_images_with_rule() {
  local image listing found=1
  for image in /boot/firmware/initramfs* /boot/initrd.img-*; do
    [[ -f "$image" ]] || continue
    if ! listing=$(lsinitramfs "$image" 2>/dev/null); then
      echo "$image (could not be listed)"
      found=0
    elif grep -q -- "$rule_name" <<< "$listing"; then
      echo "$image"
      found=0
    fi
  done
  return "$found"
}

status() {
  local problems=0 entry source target mode program_verified=1 block images
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
  if images=$(boot_images_with_rule); then
    echo "boot images that also carry the rule: $(tr '\n' ' ' <<< "$images")"
  else
    echo "boot images that also carry the rule: none"
  fi
  return "$problems"
}

install_all() {
  local entry source target mode
  for entry in "${copies[@]}"; do
    IFS=: read -r source target mode <<< "$entry"
    sudo install -o root -g root -m "0$mode" -- "$here/$source" "$target" || return 1
  done
  # After the program is in place: verify checks that ExecStart exists.
  systemd-analyze verify "$installed_unit" || return 1
  sudo udevadm control --reload || return 1
  sudo systemctl daemon-reload || return 1
  sudo systemctl enable "$unit" || return 1
  # restart, not start: an update must replace a running watcher.
  sudo systemctl restart "$unit" || return 1
  sleep 3
  status
}

uninstall_all() {
  local failed=0 target images
  if [[ -e "$installed_unit" ]] || systemctl is-active --quiet "$unit"; then
    sudo systemctl disable --now "$unit" || failed=1
  fi
  sudo rm -f -- /usr/local/sbin/vanpi-usb2-disk-hub-off || failed=1
  sudo rm -f -- /etc/udev/rules.d/70-vanpi-usb2-disk-hub-off.rules || failed=1
  sudo rm -f -- /etc/systemd/system/vanpi-usb2-disk-hub-off.service || failed=1
  sudo udevadm control --reload || failed=1
  sudo systemctl daemon-reload || failed=1
  if systemctl is-active --quiet "$unit"; then
    echo "the watcher is still running: $unit"
    failed=1
  fi
  for target in "$program" "$rule" "$installed_unit"; do
    if [[ -e "$target" || -L "$target" ]]; then
      echo "still present: $target"
      failed=1
    fi
  done
  if images=$(boot_images_with_rule); then
    echo "the rule is still inside a boot image and would power the port off at the next boot:"
    echo "$images"
    echo "remaining step: sudo update-initramfs -u -k all, then run this uninstall again"
    failed=1
  fi
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
