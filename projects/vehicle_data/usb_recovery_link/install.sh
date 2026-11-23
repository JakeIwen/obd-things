#!/bin/bash
# Install, inspect or remove the reviewed standalone link helper; no service is changed.
set -u

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 1
program=/usr/local/sbin/vanpi-can-link-by-index
source_file=$here/vanpi-can-link-by-index
contract_file=$here/../usb_recovery_contract.py

read_pin() {
  # Read the contract as data; do not import or execute any checkout Python.
  local line pin='' pattern='^LINK_HELPER_SHA256[[:blank:]]*=[[:blank:]]*"([0-9a-f]{64})"[[:blank:]]*$'
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == LINK_HELPER_SHA256* ]]; then
      [[ -z "$pin" && "$line" =~ $pattern ]] || return 1
      pin=${BASH_REMATCH[1]}
    fi
  done < "$contract_file"
  [[ -n "$pin" ]] || return 1
  printf '%s\n' "$pin"
}

verify_digest() {
  local file=$1 expected=$2 actual
  if [[ -L "$file" || ! -f "$file" ]]; then
    echo "missing or not a regular file: $file" >&2
    return 1
  fi
  if ! actual=$(sha256sum < "$file") || [[ "${actual%% *}" != "$expected" ]]; then
    echo "differs from the contract's SHA-256 pin: $file" >&2
    return 1
  fi
}

verified_source_pin() {
  local pin
  if ! pin=$(read_pin); then
    echo "missing or ambiguous SHA-256 pin: $contract_file" >&2
    return 1
  fi
  verify_digest "$source_file" "$pin" || return 1
  printf '%s\n' "$pin"
}

status() {
  local pin
  pin=$(verified_source_pin) || return 1
  verify_digest "$program" "$pin" || return 1
  if [[ "$(stat -c %u:%g:%a -- "$program")" != 0:0:755 ]]; then
    echo "not root:root mode 0755: $program" >&2
    return 1
  fi
  echo "verified: $program"
}

main() {
  case "$#:${1:-}" in
    1:status) status ;;
    1:install)
      verified_source_pin >/dev/null || return 1
      sudo install -o root -g root -m 0755 -T -- "$source_file" "$program" && status
      ;;
    1:uninstall)
      sudo rm -f -- /usr/local/sbin/vanpi-can-link-by-index || return 1
      if [[ -e "$program" || -L "$program" ]]; then
        echo "still present: $program" >&2
        return 1
      fi
      echo "uninstalled: $program"
      ;;
    *)
      echo "usage: ${0##*/} status|install|uninstall" >&2
      return 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
