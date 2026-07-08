# obd-things — repo conventions

## Data locations (imposed 2026-07-08 — do not resurrect `dumps/` dirs)

**All machine-written output goes under `tmp/` (wholesale gitignored). Nothing a tool
writes is ever committed in place.** Layout:

- `tmp/captures/` — raw candump logs (`tools/dump.sh` default) + condition-named
  bus-state reference captures (`ccan/`, `bcan/`, their `events/` subdirs)
- `tmp/sweeps/` — `tools/did_sweep.py` + `tools/signal_correlate.py` output
- `tmp/<project>/` — per-project logger output (`tmp/radar/`, `tmp/battery/`, `tmp/tpms/`)

**Committing data = deliberate promotion**: move the file into `projects/<x>/findings/`,
next to the analysis that cites it, and commit it there. "Is it tracked?" is answered by
location alone — nothing under `tmp/` ever is. Full rationale: root `README.md`
§ Data convention.

New tools must default their output somewhere under `tmp/`; take an `--out-dir`/-style
override if useful.

## What's already mapped (read before new reverse-engineering)

- **`docs/bus-map.md`** — master reference for both buses: physical params, every verified
  broadcast frame + decode (C-CAN & B-CAN voltage, lock-state, ignition gate), wake/sleep
  semantics, and the UDS module summary. **Check here first** so you don't re-derive a
  known signal.
- **`lib/modules.py`** — source of truth for UDS module *addressing* (tools execute it, so
  it can't silently drift). Now carries `bus` + `note` per module.
- **Per-ECU DID maps** live with their project: `projects/radar/findings/did_map.md`,
  `projects/tpms/README.md`. DID namespaces are per-ECU — never merge them into one list.

**Maintenance rule:** when you verify a new broadcast frame, DID, or wake behavior, update
the relevant map **in the same change** (bus-map.md for frames/wake; the project's DID map
for DIDs; modules.py for addressing) — with provenance. A fact that lives only in session
memory or a lone code constant is one the next agent can't find.

## Other

- Each project under `projects/<name>/` keeps a handoff-quality `README.md` — read it
  before touching that project (TPMS one doubles as the campaign handoff doc).
- Before manual bus work: `sudo systemctl stop tpms-logger` (restart after); see
  `projects/tpms/README.md` § Infrastructure.
