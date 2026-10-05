# Offline CAN investigation reports

`tools/can_workbench_report.py` reads finalized saved captures and explicit field
definitions, then publishes `index.html` and a versioned `bundle.json` together.
The HTML embeds its viewer and data; it needs no network, build step, plotting
package, or persistent service. Python 3.11+ is required; `.zst` inputs additionally
require the `zstd` executable. This is an offline analysis component, separate
from the telemetry dashboard.

From the repository root, generate the supplied synthetic example:

```sh
manifest=tests/fixtures/can_workbench/investigation.json
python3 tools/can_workbench_report.py "$manifest" --out tmp/can_workbench/demo
open tmp/can_workbench/demo/index.html
```

`open` is the macOS command. The generated HTML can also be opened directly in a
browser on other platforms. Keep `bundle.json` beside it for the download link.
The output directory must be new: repeated generation never overwrites an older
report. Without `--out`, output goes under this checkout's ignored
`tmp/can_workbench/report-<UTC timestamp>/`.

Use `--capture NAME` and `--field NAME` repeatedly to select manifest entries.
`--window START END` selects inclusive elapsed seconds from each capture's first
valid frame. `--gap-seconds` defaults to 1; set it to the relevant observation
cadence. `--max-points` defaults to 2,000 per series (range 4–20,000). These are
generation settings, recorded with the exact CLI, working directory, Python
version, tool hashes, input hashes, and original manifest. The browser's time
window filters plotted points only; regenerate with a narrower CLI window when
you need full detail or statistics for that range.

## Input contracts

The complete schema-1 example is
[`tests/fixtures/can_workbench/investigation.json`](../../tests/fixtures/can_workbench/investigation.json).
All relative input paths resolve beside the manifest, independent of shell cwd.
Required top-level keys are `schema_version`, `title`, `summary`, `captures`,
and `fields`. Optional keys are `questions`, `next_checks`, `references`, and
`analyses`; unknown top-level keys are rejected.

Each capture has a unique `name`, a read-only `path`, `conditions` text, and an
explicit `channels` mapping from recorded interface to physical/logical bus.
Use `unknown` conditions when unavailable. A filename is not independent evidence
of a physical condition. Every parsed channel needs a mapping; no CAN ID or live
interface name is used to infer physical routing. Different capture clocks remain
separate. Inventory comparisons use the first selected recording as baseline and
include only interfaces explicitly mapped to the same bus in both recordings.
Renamed channels are conservatively excluded rather than silently joined.

Each field has:

- A unique `name`, exact `bus`, recorded `channel`, hexadecimal-string `can_id`,
  integer `id_bits` (11 or 29), and integer `dlc`.
- A `field` object using the shared `SignalField` contract: `dbc_start_bit`,
  `length_bits`, `byte_order` (`little` or Motorola sawtooth `big`), and `signed`.
- Finite `scale` and `offset`, plus a `unit` label (`raw` when unknown).
  The plotted formula is `raw * scale + offset`.
- `evidence_tier` (`exploratory_candidate`, `operational_proxy`, or
  `verified_decode`) and nonempty `provenance`. Optional
  `supporting_evidence` and `contradicting_evidence` carry narrative evidence.

These are investigation-local selections, **not a second canonical signal
catalogue**. Cite the existing bus map or reviewed analysis. Tier labels are
attributed to supplied provenance, not independently validated by this program;
the program grants no telemetry promotion or safety-alert authority. It performs
no signal search, fit, or automatic candidate-to-plot translation.

`references` lists schema-1 JSON files like
[`reference.json`](../../tests/fixtures/can_workbench/reference.json). Each names
the exact manifest `capture` and `field`, has a display `name`, matching `unit`,
nonempty `provenance`, `timestamp_basis: "capture_clock"`, and `observations` of
`{"timestamp": "decimal seconds", "value": number}`. Record clock alignment in
provenance before import. No implicit time alignment, interpolation, or sample
holding occurs. Timestamp strings preserve up to picosecond precision; numeric
JSON timestamps are rejected. Empty observations mean missing evidence; an
explicit missing file is an error. No supplied reference is visibly reported as
such. References for unselected captures/fields are listed as excluded.

`analyses` lists existing `can_timeseries_correlate` schema-1 JSON outputs. The
adapter checks their schema, classification and stream identity, retains the
complete reports (including affine orientation, rejected candidates, regime
analysis and fixed-formula residuals when present), and displays them as
exploratory. Original source bindings stay intact; an imported report is **not
automatically associated** with the selected capture/field. Other schemas,
benchmark reports, and unversioned field-finder text are not imported in this
slice. Run existing analysis tools separately, never implicitly on report load.

## Parsing, statistics, and limits

- `lib/candump_io.py` owns syntax and streaming zstd; `lib/signal_fields.py` owns
  bit extraction. Summary schema 3 and comparison schema 3 come from the existing
  `can_capture_summary` and `can_capture_compare` tools. No broker/historian
  internals or live CAN modules are imported.
- Exact timestamp strings supplement the existing summaries' float timestamps.
  Equal timestamps are accepted; decreasing timestamps fail with a source line.
  Split discontinuous captures or sort deliberately upstream, keeping provenance.
- Existing summary semantics count malformed nonblank lines as unparsed, and
  accept syntactically complete final frames without a newline. The workbench
  additionally reports unterminated lines. Truncated zstd streams fail publication.
  With a CLI window, valid-frame statistics cover that window; malformed/blank
  counts cover the full scanned input because those lines cannot be time-filtered.
- Field selection includes exact DLC even though the existing summary groups
  different DLCs under one interface/namespace/identifier row. SFF `123` and EFF
  `00000123` are always distinct.
- Captures are streamed, including compressed inputs. Plot storage uses
  deterministic power-of-two stride decimation plus the final observation.
  Counts, minima, maxima, and gap counts cover every selected observation.
  Segment numbers prevent lines bridging a recorded gap even when boundary
  points were discarded. Brief excursions or whole short segments can be absent
  from the plotted subset; decimation is not an envelope or lossless waveform.
  Constant, insufficient (one observation), and missing streams stay distinct.
- Bounds: 16 captures, 64 selected fields, 32 reference files and 32 analysis
  files, 16 MiB per JSON input, 64 KiB per capture line, 4,096 inventory streams
  per selected capture range, 1,000 imported candidate rows per report. Inputs
  and generated data are still private evidence; paths and raw statistics may
  identify owner resources.
- SHA-256 is checked before and after generation to catch input changes. Only
  finalized regular files should be used. The complete output pair is staged
  beside its destination and published with a directory rename after success.
  Failure cleans the temporary directory and leaves prior reports untouched.

## Development and integration

Implementation and tests are isolated in `tools/can_workbench/`,
`tools/can_workbench_report.py`, `tests/test_can_workbench.py`, and
`tests/fixtures/can_workbench/`. No shared dependency manifest was changed.
The small SVG viewer uses only browser APIs and local embedded data.

Run the adapter tests with `python3 -m unittest tests.test_can_workbench`.
The wider pinned compatibility set and measured verification are recorded in
[`verification.md`](verification.md).

The prospective broadcast catalogue API is not present at this worktree's base
(`b1eb189`). After that refactor lands, add an adapter that resolves explicit
catalogue selections into the existing field contract, preserving provenance and
the original namespace. Before integration, compare against the then-current
integrated branch and rerun compatibility tests. A later dashboard change can
link to reviewed static reports; routes, navigation, hosting, `.van-compute.json`,
and shared manifests are deliberately deferred. No merge/rebase or live-service
change is necessary to use this CLI.
