#!/bin/bash
# One-command parked C-CAN campaigns for verified, non-mutating diagnostic reads.
#
# Baseline dry run (default; no sudo, interface change, or CAN traffic):
#   ./tools/ccan_inventory_campaign.sh
# Candidate-DID dry run:
#   ./tools/ccan_inventory_campaign.sh --candidate-dids
# Populated BCM-page dry run:
#   ./tools/ccan_inventory_campaign.sh --bcm-pages
# BCM default-vs-extended-session comparison dry run:
#   ./tools/ccan_inventory_campaign.sh --bcm-session-compare
# Exact AlfaOBD-derived PCM presence-probe dry run:
#   ./tools/ccan_inventory_campaign.sh --pcm-probe
# Both bounded session probes in one ignition-on window:
#   ./tools/ccan_inventory_campaign.sh --session-probes
# Follow-up: padded PCM retry plus one BCM session-03 page:
#   ./tools/ccan_inventory_campaign.sh --session-followup
#
# Live use adds the same gates to either mode:
#   ./tools/ccan_inventory_campaign.sh --execute --confirm-parked \
#     --conditions "ignition ON, engine OFF, PCAN on pigtail C-CAN DB9"
#
# Live mode uses only noninteractive passwordless sudo; it never prompts for an account password.
# Each child diagnostic tool restores the interface to listen-only; this driver explicitly re-arms
# before the next child. It preserves tpms-logger's initial state. If that service was active, its
# normal startup may leave can0 armed while the logger itself remains zero-TX until ignition is seen.
set -euo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$REPO"

EXECUTE=0
PARKED=0
SESSION_CONFIRMED=0
CONDITIONS=""
MODE="baseline"
CAPTURE_PID=""
CAPTURE_PATH=""
MODULES=(tcm shifter bcm_ccan cluster telematics rf_hub radar_acc)
F1_PAGE_MODULES=(tcm shifter bcm_ccan cluster telematics)
BCM_PAGES=(0100 2000 2900 4000)
# Exact current-van AlfaOBD positives from the DA40F1 section of
# projects/ecu_mapping/findings/promaster_2022/module_did_map.txt. F1A5 is omitted because the
# complete F100-F1FF page is scanned separately. These are per-BCM candidates, never global DIDs.
BCM_ALFA_DIDS=(
  0133 0136 2013 0103 2001 2002 2003 1008 2008 1009 2009 200A 200B 200C 2010
  1921 013B 013C 1000 1002 1004 1204 2949 2050 0130 0131 0132 0135 0137 0138
  0151 0144 0150 0152 0153 0154 2920 2921 2922 2923 2946 2944 2962 2A50 3000
  3001 3500 3DDD 3DDE 3FFD 3FFE 3FFF 40A1 40A2 40A3 40A6 40AA 292C 102A 292D 292E
)

usage() {
  sed -n '2,/^set -euo pipefail$/{/^#/ {s/^# \{0,1\}//;p;}}' "$0"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --execute) EXECUTE=1; shift ;;
    --confirm-parked) PARKED=1; shift ;;
    --candidate-dids) MODE="candidate-dids"; shift ;;
    --bcm-pages) MODE="bcm-pages"; shift ;;
    --bcm-session-compare) MODE="bcm-session-compare"; shift ;;
    --pcm-probe) MODE="pcm-probe"; shift ;;
    --session-probes) MODE="session-probes"; shift ;;
    --bcm-extended-page) MODE="bcm-extended-page"; shift ;;
    --session-followup) MODE="session-followup"; shift ;;
    --confirm-session-change) SESSION_CONFIRMED=1; shift ;;
    --conditions)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "ERROR: --conditions requires a non-empty description" >&2
        exit 2
      fi
      CONDITIONS=$2
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

dry_run_baseline() {
  echo "DRY RUN: no sudo, interface change, CAN socket, or transmission will occur."
  python3 tools/ecu_discover.py --address-byte-range F2 FF
  for module in "${MODULES[@]}"; do
    python3 tools/dtc_inventory.py "$module"
  done
  for module in "${MODULES[@]}"; do
    python3 tools/routine_scan.py "$module" 0200 020F
  done
}

did_args=()
for did in "${BCM_ALFA_DIDS[@]}"; do
  did_args+=(--did "$did")
done

dry_run_candidate_dids() {
  echo "DRY RUN: no sudo, interface change, CAN socket, or transmission will occur."
  echo "Candidate DID mode: inherited session only; no DiagnosticSessionControl or TesterPresent."
  for module in "${F1_PAGE_MODULES[@]}"; do
    python3 tools/did_sweep.py "$module" F100 F1FF
  done
  python3 tools/identity_inventory.py bcm_ccan "${did_args[@]}"
}

dry_run_bcm_pages() {
  echo "DRY RUN: no sudo, interface change, CAN socket, or transmission will occur."
  echo "BCM-page mode: inherited session only; no DiagnosticSessionControl or TesterPresent."
  for page in "${BCM_PAGES[@]}"; do
    start=$page
    end=$(printf '%02XFF' $((16#${page:0:2})))
    python3 tools/did_sweep.py bcm_ccan "$start" "$end"
  done
}

dry_run_bcm_session_compare() {
  echo "DRY RUN: no sudo, interface change, CAN socket, or transmission will occur."
  echo "BCM comparison: 40A3-40A6 first in inherited/default state, then explicit session 03."
  python3 tools/did_sweep.py bcm_ccan 40A3 40A6
  python3 tools/did_sweep.py bcm_ccan 40A3 40A6 --session 03
}

dry_run_pcm_probe() {
  echo "DRY RUN: no sudo, interface change, CAN socket, or transmission will occur."
  python3 tools/ecu_discover.py \
    --target pcm_candidate=18DA10F1:18DAF110 \
    --probe legacy-1a87 --session 92 --tx-padding 00
}

dry_run_bcm_extended_page() {
  echo "DRY RUN: no sudo, interface change, CAN socket, or transmission will occur."
  echo "BCM extended page: explicit session 03, then 4000-40FF physical DID reads."
  python3 tools/did_sweep.py bcm_ccan 4000 40FF --session 03
}

if [ "$EXECUTE" -eq 0 ]; then
  case "$MODE" in
    candidate-dids) dry_run_candidate_dids ;;
    bcm-pages) dry_run_bcm_pages ;;
    bcm-session-compare) dry_run_bcm_session_compare ;;
    pcm-probe) dry_run_pcm_probe ;;
    session-probes) dry_run_pcm_probe; dry_run_bcm_session_compare ;;
    bcm-extended-page) dry_run_bcm_extended_page ;;
    session-followup) dry_run_pcm_probe; dry_run_bcm_extended_page ;;
    baseline) dry_run_baseline ;;
  esac
  exit 0
fi

if [ "$PARKED" -ne 1 ] || [ -z "$CONDITIONS" ]; then
  echo "ERROR: --execute requires --confirm-parked and --conditions" >&2
  exit 2
fi
case "$MODE" in
  bcm-session-compare|pcm-probe|session-probes|bcm-extended-page|session-followup)
    if [ "$SESSION_CONFIRMED" -ne 1 ]; then
      echo "ERROR: $MODE live use requires --confirm-session-change" >&2
      exit 2
    fi
    ;;
  *)
    if [ "$SESSION_CONFIRMED" -eq 1 ]; then
      echo "ERROR: --confirm-session-change applies only to a session-changing mode" >&2
      exit 2
    fi
    ;;
esac

case "$MODE" in
  baseline)
    echo "This campaign will send only physical 22 F187, 19 read-DTC, and 31 03"
    echo "requestRoutineResults traffic."
    ;;
  candidate-dids)
    echo "This campaign will send only physical 22 ReadDataByIdentifier traffic for"
    echo "five F100-F1FF pages and 61 exact current-van AlfaOBD BCM candidates."
    ;;
  bcm-pages)
    echo "This campaign will send only physical 22 ReadDataByIdentifier traffic to the"
    echo "BCM for pages 0100-01FF, 2000-20FF, 2900-29FF, and 4000-40FF."
    ;;
  bcm-session-compare)
    echo "This campaign will read physical BCM DIDs 40A3-40A6 in the inherited/default"
    echo "state, then send physical 10 03 and repeat those four reads in extended session."
    ;;
  pcm-probe)
    echo "This campaign will send physical 10 92 to the single PCM candidate at 0x10 and,"
    echo "only after exact 50 92 validation, send physical legacy identity request 1A 87."
    ;;
  session-probes)
    echo "This campaign combines the exact PCM 10 92 -> 1A 87 presence probe with the"
    echo "BCM 40A3-40A6 inherited/default-versus-10 03 comparison."
    ;;
  bcm-extended-page)
    echo "This campaign will enter physical BCM session 03 and read only DIDs 4000-40FF."
    ;;
  session-followup)
    echo "This campaign combines the fixed-DLC padded PCM 10 92 -> 1A 87 retry with one"
    echo "BCM session-03 4000-40FF ReadDataByIdentifier page."
    ;;
esac
if [ "$MODE" = "bcm-session-compare" ]; then
  echo "It may send validated physical 3E 00 keepalive while session 03 is active. It sends no"
  echo "functional broadcast, routine start/stop, IO control, write, DTC clear, or security request."
elif [ "$MODE" = "pcm-probe" ]; then
  echo "It sends no functional broadcast, TesterPresent, routine, IO control, write, DTC clear,"
  echo "security request, or any request beyond the exact AlfaOBD-observed two-message sequence."
elif [ "$MODE" = "session-probes" ]; then
  echo "It may send validated physical 3E 00 keepalive during the brief BCM session. It sends"
  echo "no functional broadcast, routine start/stop, IO control, write, DTC clear, or security request."
elif [ "$MODE" = "bcm-extended-page" ] || [ "$MODE" = "session-followup" ]; then
  echo "It may send validated physical 3E 00 keepalive during the BCM page. It sends no"
  echo "functional broadcast, routine start/stop, IO control, write, DTC clear, or security request."
else
  echo "It sends no functional broadcast, session change, TesterPresent, routine start/stop,"
  echo "IO control, write, DTC clear, or security request."
fi
echo "Checking noninteractive passwordless sudo (no password prompt)..."
if ! sudo -n true; then
  echo "ERROR: passwordless sudo is unavailable for this shell." >&2
  echo "Run 'sudo -n -l' to inspect the applicable sudoers rules; do not enter an SSH-key" >&2
  echo "passphrase or guess an account password for this campaign." >&2
  exit 2
fi

TPMS_WAS_ACTIVE=0
if systemctl is-active --quiet tpms-logger; then
  TPMS_WAS_ACTIVE=1
fi

CAN_TOUCHED=0
cleanup() {
  status=$?
  trap - EXIT INT TERM HUP
  if [ -n "$CAPTURE_PID" ]; then
    kill "$CAPTURE_PID" 2>/dev/null || true
    wait "$CAPTURE_PID" 2>/dev/null || true
    CAPTURE_PID=""
  fi
  if [ "$CAN_TOUCHED" -eq 1 ]; then
    echo "Restoring can0 to passive C-CAN..."
    ./bringup.sh || echo "WARNING: passive C-CAN restoration failed; inspect can0 now" >&2
  fi
  if [ "$TPMS_WAS_ACTIVE" -eq 1 ]; then
    sudo -n systemctl start tpms-logger \
      || echo "WARNING: tpms-logger was initially active but could not be restarted" >&2
  else
    echo "tpms-logger was initially inactive and remains inactive."
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

sudo -n systemctl stop tpms-logger
CAN_TOUCHED=1
./bringup.sh

BUS=$(python3 - <<'PY'
from lib.canbus import identify_bus
print(identify_bus("can0", probe=3.0))
PY
)
if [ "$BUS" != "c-can" ]; then
  echo "ERROR: passive signature check returned '$BUS', not 'c-can'." >&2
  echo "Connect the PCAN to the pigtail's C-CAN DB9 and turn ignition ON." >&2
  exit 2
fi

arm_ccan() {
  ./bringup.sh --tx
}

common=(--execute --confirm-parked --pair 6/14 --conditions "$CONDITIONS")

stop_pcm_capture() {
  if [ -n "$CAPTURE_PID" ]; then
    kill "$CAPTURE_PID" 2>/dev/null || true
    wait "$CAPTURE_PID" 2>/dev/null || true
    CAPTURE_PID=""
  fi
}

run_padded_pcm_probe() {
  mkdir -p tmp/captures/ccan/events
  CAPTURE_PATH="tmp/captures/ccan/events/pcm_probe_$(date +%Y%m%d_%H%M%S_%z).candump"
  candump -L 'can0,18DA10F1:DFFFFFFF,18DAF110:DFFFFFFF' >"$CAPTURE_PATH" &
  CAPTURE_PID=$!
  sleep 0.1
  if ! kill -0 "$CAPTURE_PID" 2>/dev/null; then
    echo "ERROR: filtered PCM candump capture did not start" >&2
    wait "$CAPTURE_PID" 2>/dev/null || true
    CAPTURE_PID=""
    return 1
  fi
  python3 tools/ecu_discover.py \
    --target pcm_candidate=18DA10F1:18DAF110 \
    --probe legacy-1a87 --session 92 --tx-padding 00 \
    --confirm-custom-physical --confirm-session-change "${common[@]}"
  stop_pcm_capture
  echo "Filtered PCM raw capture: $CAPTURE_PATH"
}

if [ "$MODE" = "candidate-dids" ]; then
  echo "Step 1/2: F100-F1FF identity/OEM page on five not-yet-mature modules."
  for module in "${F1_PAGE_MODULES[@]}"; do
    arm_ccan
    python3 tools/did_sweep.py "$module" F100 F1FF "${common[@]}"
  done

  echo "Step 2/2: 61 exact current-van AlfaOBD-positive BCM candidates outside F1xx."
  arm_ccan
  python3 tools/identity_inventory.py bcm_ccan "${did_args[@]}" "${common[@]}"
  echo "Candidate DID campaign complete. Reports are under tmp/inventories."
  exit 0
fi

if [ "$MODE" = "bcm-pages" ]; then
  step=0
  for page in "${BCM_PAGES[@]}"; do
    step=$((step + 1))
    start=$page
    end=$(printf '%02XFF' $((16#${page:0:2})))
    echo "Step $step/${#BCM_PAGES[@]}: BCM $start-$end populated-neighborhood page."
    arm_ccan
    python3 tools/did_sweep.py bcm_ccan "$start" "$end" "${common[@]}"
  done
  echo "BCM page campaign complete. Reports are under tmp/inventories/bcm_ccan."
  exit 0
fi

if [ "$MODE" = "bcm-session-compare" ]; then
  echo "Step 1/2: BCM 40A3-40A6 in inherited/default diagnostic state."
  arm_ccan
  python3 tools/did_sweep.py bcm_ccan 40A3 40A6 "${common[@]}"

  echo "Step 2/2: BCM 40A3-40A6 after explicit DiagnosticSessionControl 10 03."
  arm_ccan
  python3 tools/did_sweep.py bcm_ccan 40A3 40A6 --session 03 \
    --confirm-session-change "${common[@]}"
  echo "BCM session comparison complete. Reports are under tmp/inventories/bcm_ccan."
  exit 0
fi

if [ "$MODE" = "pcm-probe" ]; then
  echo "Step 1/1: exact AlfaOBD-derived PCM 10 92 -> 1A 87 presence sequence."
  arm_ccan
  run_padded_pcm_probe
  echo "PCM probe complete. Report is under tmp/discovery."
  exit 0
fi

if [ "$MODE" = "session-probes" ]; then
  echo "Step 1/3: exact AlfaOBD-derived PCM 10 92 -> 1A 87 presence sequence."
  arm_ccan
  run_padded_pcm_probe

  echo "Step 2/3: BCM 40A3-40A6 in inherited/default diagnostic state."
  arm_ccan
  python3 tools/did_sweep.py bcm_ccan 40A3 40A6 "${common[@]}"

  echo "Step 3/3: BCM 40A3-40A6 after explicit DiagnosticSessionControl 10 03."
  arm_ccan
  python3 tools/did_sweep.py bcm_ccan 40A3 40A6 --session 03 \
    --confirm-session-change "${common[@]}"
  echo "Session probes complete. Reports are under tmp/discovery and tmp/inventories/bcm_ccan."
  exit 0
fi

if [ "$MODE" = "bcm-extended-page" ]; then
  echo "Step 1/1: BCM 4000-40FF after explicit DiagnosticSessionControl 10 03."
  arm_ccan
  python3 tools/did_sweep.py bcm_ccan 4000 40FF --session 03 \
    --confirm-session-change "${common[@]}"
  echo "BCM extended-session page complete. Report is under tmp/inventories/bcm_ccan."
  exit 0
fi

if [ "$MODE" = "session-followup" ]; then
  echo "Step 1/2: fixed-DLC padded PCM 10 92 -> 1A 87 probe with filtered raw capture."
  arm_ccan
  run_padded_pcm_probe

  echo "Step 2/2: BCM 4000-40FF after explicit DiagnosticSessionControl 10 03."
  arm_ccan
  python3 tools/did_sweep.py bcm_ccan 4000 40FF --session 03 \
    --confirm-session-change "${common[@]}"
  echo "Session follow-up complete. Reports are under tmp/discovery and tmp/inventories/bcm_ccan."
  exit 0
fi

echo "Step 1/3: completing the unattempted F2-FF address-byte tail (14 reads)."
arm_ccan
python3 tools/ecu_discover.py --address-byte-range F2 FF \
  --confirm-expanded-scan "${common[@]}"

echo "Step 2/3: bounded non-clearing DTC inventories."
for module in "${MODULES[@]}"; do
  arm_ccan
  python3 tools/dtc_inventory.py "$module" "${common[@]}"
done

echo "Step 3/3: result-only routine sample 0200-020F plus FF00-FF03."
for module in "${MODULES[@]}"; do
  arm_ccan
  python3 tools/routine_scan.py "$module" 0200 020F "${common[@]}"
done

echo "Campaign complete. Reports are under tmp/discovery and tmp/inventories."
