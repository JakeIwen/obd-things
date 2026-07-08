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

## Other

- Each project under `projects/<name>/` keeps a handoff-quality `README.md` — read it
  before touching that project (TPMS one doubles as the campaign handoff doc).
- Before manual bus work: `sudo systemctl stop tpms-logger` (restart after); see
  `projects/tpms/README.md` § Infrastructure.
