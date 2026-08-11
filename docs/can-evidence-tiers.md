# CAN signal evidence tiers

This project separates **usefulness** from **physical verification**. A field
does not need to be a proven physical decode before it can guide research or
serve a bounded, non-critical operational purpose.

The tiers below are orthogonal to the mechanical `candidate_only` flag emitted
by offline correlation tools. That flag remains false-to-promotion because
correlation software cannot prove physical identity by itself.

## Gates that never loosen

Every tier still requires:

- live-CAN safety, ownership, rate, and restoration gates;
- correct vehicle, ECU, bus, channel, identifier namespace, DLC, and source
  provenance;
- honest timestamp and loss-accounting limits;
- no safety-critical use of an unverified field; and
- no promotion into the canonical bus map or verified telemetry registry under
  a stronger label than the evidence supports.

These gates prevent unsafe traffic and false durable knowledge. They are not
statistical acceptance thresholds.

## Tier 1: exploratory candidate

Use this tier for discovery, shortlisting, and hypothesis generation.

- One representative capture or operating regime is sufficient.
- Correlation, visual co-movement, a plausible bit layout, or a repeatable
  state transition can justify retaining the candidate.
- The candidate may be plotted, compared, or recorded in `tmp/`.
- It may be documented in a finding as exploratory, including counterexamples.
- It must not be labeled as the physical quantity, used for a safety alert, or
  promoted as verified telemetry.

An exploratory candidate should be dropped from active pursuit when it has no
plausible use, a better direct source exists, or an inexpensive counterexample
answers the relevant question. A failed universal affine fit means only “not
this universal linear encoding”; it does not make the identifier meaningless.

`tools/can_timeseries_correlate.py` emits
`evidence_tier: exploratory_candidate` for ranked correlations.

## Tier 2: operational proxy

Use this tier when an approximate field is useful even though its physical
identity is unresolved. Allowed intended uses are:

- `trend`
- `state_detection`
- `approximate_display`

Qualification requires:

- one exact, frozen stream/field/formula selected before evaluation;
- at least one complete independent drive leg;
- an explicit intended use and human-readable error unit;
- predeclared minimum coverage, maximum RMSE, and maximum p95 absolute error;
- optional maximum absolute mean bias; and
- passing those tolerances without refitting on the evaluation leg.

The tolerances must come from the intended use, not from whatever the candidate
happens to achieve. A qualifying proxy remains
`physical_identity_verified: false`,
`telemetry_promotion_allowed: false`, and must be visibly labeled approximate
where consumed.

`tools/can_signal_benchmark.py` supports the assertion kind
`operational_proxy`. It accepts only `trend`, `state_detection`, and
`approximate_display`; safety-alert use is rejected.

Example expectation:

```json
{
  "kind": "operational_proxy",
  "stream": {
    "channel": "can0",
    "can_id": "417",
    "id_bits": 11,
    "dlc": 8
  },
  "field": {
    "dbc_start_bit": 18,
    "length_bits": 8,
    "byte_order": "big",
    "signed": false
  },
  "intended_use": "trend",
  "error_unit": "percentage points",
  "units_per_reference_raw": 0.0030517578125,
  "minimum_coverage": 0.95,
  "maximum_rmse": 5.0,
  "maximum_p95_absolute_error": 10.0,
  "maximum_absolute_mean_bias": 3.0
}
```

The supplied correlation report must contain a matching
`fixed_formula_evaluation`. The benchmark converts raw-reference errors using
`units_per_reference_raw`, then reports both the observed errors and declared
limits.

## Tier 3: verified decode

Use this tier for a field asserted to be the named physical or semantic
quantity.

It requires the operational evidence appropriate to the signal plus independent
identity and scaling evidence, such as a controlled physical change, an exact
compatible OEM definition, a trusted labeled source, or a decisive
counterexample against plausible competing meanings. Validation must exercise
the operating regimes relevant to the intended telemetry use.

Only this tier may enter `docs/bus-map.md` as a verified decode or the ordinary
verified telemetry allowlist. Promotion is a reviewed documentation/code
decision; no correlation or benchmark report automatically grants it.

## Fast workflow

1. Run a coarse search on one useful capture and keep the result exploratory.
2. Refine no more than two identifiers when the candidate has a concrete use.
3. Choose the next gate from that use:
   - proxy value: freeze a formula and test declared tolerances on one
     independent whole leg;
   - verified physical decode: collect the independent scaling and identity
     evidence the signal actually requires.
4. Stop after a decisive counterexample to the proposed use. Do not keep
   proving irrelevant session labels, adding random holdout splits, or
   broadening a search solely to rescue a failed hypothesis.
5. State rejections narrowly: reject the proposed identity, formula, operating
   scope, or use—not the entire identifier unless evidence supports that claim.
