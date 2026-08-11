# 2026-08-04 broker-drive poll validation

## Result

Two finalized ordinary-driving C-CAN campaigns provide whole-leg validation of
the broker's coordinated PCM and RF Hub polling. Across 8,514 scheduler cycles,
every `01A1` generator-duty request, `06DA` current-torque request, and RF Hub
pressure request has a corresponding physical positive response. No timeout,
orphan positive response, negative response, socket drop, or request-order
anomaly was found.

This establishes that the production scheduler can sustain the three one-hertz
reads while the receive-only drive recorder and passive dashboard telemetry run
on the same PCAN. It does not change the established semantics of the DIDs or
promote a passive torque substitute.

## Capture integrity

| campaign | elapsed | full frames | chunks | termination | socket drops |
|---|---:|---:|---:|---|---:|
| `broker-drive-20260801T225441745239` | 6,151.487 s | 16,711,866 | 11 | `tracked_id_absent` | 0 |
| `broker-drive-20260802T014258086240` | 2,403.975 s | 6,515,928 | 5 | `tracked_id_absent` | 0 |

Both checkpoints report `success=true` and `full_stream_complete=true`. All 16
manifest rows are complete and their compressed streams passed the recorder's
zstd verification. Offline summaries parsed all 23,227,794 lines as CAN frames
with zero unparsed lines. The first campaign's `run.json`, `manifest.jsonl`, and
`checkpoint.json` SHA-256 values are respectively
`319a546670c04fabadf4e2d78dffeb52e84e58e77db28ddee9997a48a4f1a5e2`,
`46471c74ff58df074433ef5d701e24202c4f73997a771ac39463f957d0968075`, and
`8a528b2895090635c4ab3350d920ed88b1b0f43c446037666fb40e27dead0bc0`.
The second campaign's corresponding hashes are
`6bd38dd12ab813db5b13b93a09b455d4b76a300a47a846e5ee5e4e6f1ce1ab9b`,
`fd292825146b88ab878f2fbb3bbc9f148028f972c43d2fe944842a7f36599359`, and
`96d081dc0ce6682d324eeadebc338c89b64b7326deb04db7cb341e34340b8835`.

The earlier `broker-drive-20260801T212724523509` campaign stopped when the
kernel receive-drop count increased from zero to one. Its evidence is retained
for bounded local questions but excluded from every whole-leg count and fit in
this finding; the rejection was relevant, not merely conservative process.

The recorder metadata's condition string names `01A1` and RF Hub polling but
omits the newly deployed `06DA` poll. Its captured initial broker status and the
wire evidence both prove that `06DA` was active; the default condition string
is corrected with this finding.

## Exact diagnostic accounting

The full-stream summaries produce the following whole-campaign counts:

| campaign | PCM requests | PCM positives | RF Hub requests | RF Hub positives |
|---|---:|---:|---:|---:|
| long leg | 12,262 | 12,262 | 6,131 | 6,131 |
| short leg | 4,766 | 4,766 | 2,383 | 2,383 |
| **total** | **17,028** | **17,028** | **8,514** | **8,514** |

The PCM request count is twice the scheduler-cycle count because each cycle
performs `01A1` and then `06DA`. Exact physical-wire extraction split the
evidence into four bounded jobs:

| compute job | exchanges | wire rows | wire SHA-256 |
|---|---:|---:|---|
| `20260804T212652Z-00a5af8a` | 4,800 | 9,600 | `8e81e457b0a642325b2541312207ed36403b25c15e6f198c73e8e494a06c2d50` |
| `20260804T212655Z-63dad476` | 4,800 | 9,600 | `88b5e6ade296400165580d513a60d02f2428057e72765dcc43acac2b1b911c47` |
| `20260804T212656Z-97b4a42c` | 3,862 | 7,724 | `27c1b1b668c2878326cf358422f4df706754701c3f33456071f0c28e1a9e72bf` |
| `20260804T212658Z-c48e16bc` | 3,566 | 7,132 | `b653e429b5f6ccce9a636493b711010f455b7491a4a3c90d58ddbf5d8ec58fd8` |

Each extractor summary reports zero incomplete requests and zero ignored
unpaired positives. Independent recorder-manifest/checkpoint review supplies
the loss accounting because the wire extractor intentionally does not consume
those sidecars itself.

The RF Hub frames are equally explicit: all 8,514 requests are padded single
frames `03 22 31 D0` through `03 22 31 D3`, and all 8,514 responses begin with
the matching positive service bytes `05 62 31 D0` through `05 62 31 D3`.

## Cadence, latency, and value coverage

The two legs contain 6,131 and 2,383 complete scheduler cycles. For both PCM
DIDs, the median response-to-response interval is 1.00004 s. The p99 interval
is 1.030 s for `01A1` and 1.041 s for `06DA`; after excluding the intentional
inter-leg gap, the only interval above 1.1 s is one 1.110 s `06DA` interval.

Across all 17,028 PCM exchanges, request-to-positive-response latency is 3.605
ms median, 6.461 ms p95, 7.519 ms p99, and 9.203 ms maximum. The scheduler
always completed the `01A1` exchange before transmitting `06DA`; the time from
the `01A1` response to the `06DA` response is 10.241 ms median, 25.176 ms p99,
and 57.935 ms maximum. There are no order or pairing anomalies.

The saved responses also expand the observed production range:

| DID | samples | distinct decoded values | minimum | median | maximum |
|---|---:|---:|---:|---:|---:|
| `01A1`, `u16be * 100 / 32768` | 8,514 | 2,019 | 0.000% | 48.175% | 100.000% |
| `06DA`, `i16be * 0.04 Nm` | 8,514 | 3,257 | -67.28 Nm | 239.72 Nm | 269.88 Nm |

These ranges corroborate the existing layouts and demonstrate changing values;
they are not a fresh independent physical calibration experiment.

## New passive current-torque candidate

The old `0x100` torque rejection covered `u13be@4`, the low five bits of byte 0
followed by byte 1. It remains rejected. A bounded 13-bit Motorola search over
the same frame isolated a different adjacent field:

```text
raw13 = ((byte1 & 0x03) << 11) | (byte2 << 3) | (byte3 >> 5)
candidate_Nm = raw13 * 0.125 - 500
```

The discovery slice used long-leg chunks 0–3 and 2,400 exact `06DA` samples.
`u13be@9` ranked first with full coverage, R² 0.99787714, and the fitted raw
equation `DID_raw = 3.125023 * candidate_raw - 12498.652`. Because `06DA` uses
0.04 Nm per raw count, that affine result independently reduces to almost
exactly the candidate formula above. Compute job
`20260804T213308Z-2d5525e0`; report SHA-256
`5327d8c503a246da48fb1a9e1c5112ca0b8c3a7080c402c549ef876a0f337b1d`.

After freezing that equation, the previously unused long-leg chunks 4–7 added
2,400 temporal-validation samples. `u13be@9` again ranked first at full
coverage and R² 0.99892391. Without refitting, the fixed formula produced
2.404 Nm RMSE, 0.646 Nm mean absolute error, 1.905 Nm p95 absolute error, and
0.082 Nm absolute mean bias. The largest residuals were +9.63 and -70.25 Nm.
This is a distinct chronological slice but not a separate drive leg. Compute
job `20260804T213918Z-d4209e7f`; report SHA-256
`6957d9f863fbb363273dd72542e3060b16d3ecd69aca6cfb8555be046b3f0629`.

A separate-drive slice over short-leg chunks 1–4 retained `u13be@9` at rank 1
with 1,783/1,783 coverage and R² 0.99698093. Its fitted raw slope/intercept were
3.111336 and -12429.518. Compute job `20260804T213309Z-aa507a6b`; report
SHA-256 `d05c4b297675966397d93065731af2f9d5fe67afbac07ddfefd4a9b82bab7d97`.

Scoring the frozen physical equation
`predicted_06DA_raw = 3.125 * candidate_raw - 12500` without refitting on that
second slice produced 100% coverage, 5.233 Nm RMSE, 1.076 Nm mean absolute
error, 2.25 Nm p95 absolute error, and 0.127 Nm absolute mean bias. The largest
single signed residuals were +50.515 and -92.69 Nm; the low p95 beside those
outliers is consistent with fast transient/sample-time disagreement but does
not prove that timing is the only cause. Compute job
`20260804T213808Z-96bd387a`; report SHA-256
`446f596d4f6f8664b8c9ac4660498dd00570937f97c84aa09e77f17e102d7b20`.

This is materially stronger than the old `u13be@4` candidate and corrects the
prior over-broad summary that all of frame `0x100` had been rejected. It is
still `candidate_only`: the separate-drive slice was part of the exploratory
search rather than a tolerance-predeclared operational-proxy evaluation, and
correlation does not supply independent semantic identity. It is suitable for
the next frozen approximate-display gate, but it does not yet replace the
guarded `06DA` dashboard source or authorize derived horsepower.

## Bus-duty conclusion

Steady-state active diagnostics add six extended eight-byte CAN frames per
second: two PCM requests, two PCM positives, one RF Hub request, and one RF Hub
positive. Even a conservative 160-bit-per-frame stuffed-wire allowance is 960
bit/s, or 0.192% of the 500-kbit/s C-CAN link. The capture therefore supports
substantial scheduling headroom for a small future allowlist, although each new
DID still needs its own safety, latency, and failure-isolation review.

## B-CAN limitation

The attempted `bcan-drive-20260802T215200Z` campaign contains a zero-byte
`full.candump`; its recorder stderr is `read: Network is down`. It contributes
no B-CAN frames and cannot support any signal conclusion. A future B-CAN drive
must verify the physical tap, 125-kbit/s interface state, and first awake frame
before treating the recorder as armed.

## Evidence classification

- Capture completeness and request/response accounting: `verified` for these
  two campaigns.
- Production cadence and latency: `verified` for these two campaigns.
- Existing `01A1` and `06DA` layouts: corroborated, with their prior evidence
  remaining the semantic authority.
- Any passive carrier found by correlation: remains `candidate_only` until it
  passes the project's independent identity and holdout gates.
