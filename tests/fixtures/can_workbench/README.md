# Synthetic offline workbench fixtures

All data here is synthetic and contains no vehicle observations or identifiers.
`ramp.candump` tests precise decimal timestamps, two channels, SFF/EFF separation,
DLC separation, and a gap. `constant.candump` is a separate clock/condition with
constant input. `reference.json` specifies the independent test equation `2x+5`;
it is not physical calibration evidence.

`correlation-source.candump` and `cluster_wire.jsonl` are a separate minimal,
exact-linked synthetic correlator input pair. The retained `correlation.json` was
generated using the existing `tools.can_timeseries_correlate.run_analysis` with
DID `0x1000`, `reference_field="auto"`, `capture_channel="can0"`, and
`AnalysisConfig(match_mode="nearest", radius_us=50000, minimum_samples=3,
top_count=20)`. Five candidate raw values (2, 4, 7, 9, 12) occur 10 ms after the
corresponding reference values (9, 13, 19, 23, 29). A fixture note was added to the
export. Relative input paths in that report are rooted at the repository.

This small generated report is deliberately promoted as a compatibility fixture.
It does not validate the separate plotting capture by source identity, and the
viewer must retain that distinction. No original production evidence is copied.
