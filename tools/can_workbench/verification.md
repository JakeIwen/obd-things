# Verification — 2026-10-03

Base: `b1eb189`, branch `feat/offline-can-workbench`. Python 3.13.2 on the
owner's M4 MacBook Pro. All source edits are unstaged; no dependency manifests,
vehicle interfaces, broker state, services, deployments, or other worktrees were
changed. Only read-only saved capture access used the primary checkout.

## Automated checks

Before implementation, the parser, field, summary and comparator tests ran:
58 tests, 3 skipped, success. The original full unittest entry point ran
1,298 tests with 49 errors and 5 skips. The final full run executed 1,323 tests
with the **same 49 error signatures**, 5 skips, and no new failures. Error order
varies; comparison sorted the signatures. Existing errors stem from unavailable
`can-isotp`/`pytest` dependencies and two mountpoint assumptions involving macOS's
`/var` → `/private/var` paths. The full suite therefore remains non-green in this
environment, matching baseline.

The focused final suite passed 140 tests, with 3 skips:

```sh
python3 -m unittest tests.test_can_workbench tests.test_candump_io \
  tests.test_signal_fields tests.test_can_capture_summary \
  tests.test_can_capture_compare tests.test_can_timeseries_correlate \
  tests.test_can_signal_benchmark
```

The 25 new tests cover exact channel/SFF/EFF/DLC selection; Motorola/Intel
geometry reuse via the shared extractor; picosecond timestamp retention at real
epoch magnitudes; decreasing timestamp rejection; malformed DLC and unterminated
line accounting; real `.zst` decompression and truncated-stream failure; missing,
constant and insufficient series; missing/empty references; unknown schemas and
unit mismatches; correlation import; bounded downsampling and gap segments;
input hashes; mutation detection; selection; script-closing text escaping;
failed-render cleanup; existing-report preservation; ignored output placement;
and useful CLI errors. Existing geometry tests cover signed and cross-byte fields.

`git diff --check` passes. The index remains unchanged. Full baseline/final logs
and their comparison are in ignored `tmp/can_workbench/`.

## Examples and performance

Generated artifacts live in this worktree's ignored output tree:

| Report directory | Dataset | Final HTML + JSON |
|---|---|---:|
| `tmp/can_workbench/example-final/` | Tiny explicitly synthetic ramp/reference, constant capture and real correlator-format import | 75,775 bytes |
| `tmp/can_workbench/real-final/` | Historical B-CAN `ignition_on.log` and `engine_on.log`, 14,043 frames total | 441,825 bytes |
| `tmp/can_workbench/large-final/` | 250,000 generated frames, 1 kHz, one deliberate 10-second gap; 6,140,000-byte input | 488,389 bytes |

An isolated real-input run took 0.153 seconds with 25,935,872 bytes peak RSS.
An isolated synthetic stress run took 2.520 seconds with 26,787,840 bytes peak
RSS. Measurements use `time.perf_counter()` and macOS
`resource.getrusage(RUSAGE_SELF).ru_maxrss`; `/usr/bin/time -l` could not read
`kern.clockrate` inside the sandbox and is not the memory evidence. Later final
CLI generation recorded 0.158 and 2.539 seconds respectively through analysis
and hashing (before serialization). These are single-run measurements, not
claims about multi-gigabyte throughput.

The large report contains 1,955 plotted samples, all 250,000 observations in
statistics, and two distinct SVG path segments across the gap. Browser load was
164 ms and selecting a missing field took 39 ms in the measured clean-browser
run. Initial tiny-example load was 69 ms. These timings include automation
overhead and are indicative only.

The real-input investigation applies the existing bus-map `0x46C` low-13-bit
voltage geometry (`u13be@36`, scale 0.0025), explicitly mapped to historical
`can0` on B-CAN. The ignition-on file contains 3,511 total frames; the engine-on
file contains 10,532. The selected voltage field is constant at 12.16 V and
14.24 V respectively. Those values describe saved data, not new vehicle
measurements. Conditions are attributed to filenames, and there are no
independent voltage observations for these exact recordings. The synthetic
reference validates extraction/plotting software only. Original files were
hashed before and after; source evidence was not changed.

## Browser verification

Used the existing `browser_clean` service, with a temporary loopback-only
Python HTTP server on port 8876. No browser was launched from the shell.
Checked the actual generated reports:

- Capture/field selection, constant and missing states, valid and invalid time
  windows, reset, and candidate/evidence disclosure navigation.
- The 0..2-second synthetic window shows three candidate points and three exact
  reference points; reset restores the two gap-separated segments.
- The real engine-on selection displays 120 voltage observations at 14.24 V.
- At 390×844, there is no document horizontal overflow; SVG geometry resizes to
  the panel (336-pixel viewBox width) so axis text stays readable. Desktop plots
  and the large downsampled report were also exercised.
- No JavaScript errors or external asset requests. All non-loopback requests
  were blocked during initial navigation. The HTML's CSP independently disallows
  network connections and external scripts/assets.
- With the browser context offline, controls still worked. A fresh `about:blank`
  document loaded the entire saved HTML via `setContent` while already offline,
  executed the viewer, and correctly selected missing/constant states.

**Direct `file://` navigation is unverified:** the MCP driver explicitly returned
`Access to "file:" protocol is blocked`. The service status was healthy. Offline
DOM loading and localhost testing are not counted as direct-file proof. The
artifact has no fetches or external assets, but manually opening it remains the
last environment-specific check. The temporary HTTP server was stopped after
verification; it is not an installed or required service.

## Deferred integration

No final broadcast-catalogue API was available at this worktree's base. Explicit
manifest selections are the current adapter input, not a parallel catalogue.
After the relevant branches land, inspect that API and rerun these pinned checks
against the integrated branch before adding catalogue selection or a dashboard
link. No other branch was merged or rebased here. Benchmark-result imports,
automatic candidate plotting, arbitrary clock alignment, and general search
remain outside this first slice.
