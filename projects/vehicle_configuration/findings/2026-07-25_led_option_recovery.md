# 2026-07-25 LED option attempt and PROXI recovery

This is the sanitized durable record of the owner-supervised AlfaOBD
configuration campaign. It intentionally excludes the VIN, the raw 250-byte
PROXI payload, vehicle-specific digests, screenshots, and unredacted logs.
Unless stated otherwise, results below are owner-observed AlfaOBD/UI and
vehicle behavior rather than an independent CAN decode.

## Attempt

The owner changed AlfaOBD's single labeled BCM option:

`Headlamp LED Management: Absent -> Present`

PROXI alignment reached the configured modules but repeatedly reported
`Failure connecting to module` for DASM. Grey-adapter modules aligned
successfully. Direct DASM status remained available, ACC appeared to work, no
ACC fault was shown, and the odometer did not flash. The BCM configuration
snapshot nevertheless marked DASM EOL not OK.

After BCM DTCs were cleared, the existing headlamp circuit DTCs returned and
the headlamp warning remained. BCM DTC `B10AA-00` also returned, so the
changed state was not accepted as a clean outcome.

## AlfaOBD restore-path behavior

AlfaOBD 2.4.4.0's backup chooser closes immediately after a file is selected,
which initially appeared to be a failed selection. Inspection of the installed
application established the usable expert path:

1. open `Proxy tools -> Write custom configuration`;
2. use `Read From File` and select the retained backup;
3. allow the chooser to close and populate the configuration editor;
4. use `Verify Custom Proxy`; and
5. write only after confirming the retained known-good 250-byte backup.

`Verify Custom Proxy` renders a long decoded vehicle-configuration listing
rather than a short success message. An invalid configuration instead produces
an explicit validation error. The decoded wall of text was therefore expected
successful behavior, not data that needed to be copied manually.

## Recovery outcome

The retained pre-change backup
`ProxyBackup_2026_07_24_19_48_12.txt` was loaded, decoded by AlfaOBD, and
written as the recovery configuration. That native backup had already been
independently shown to decode to the same 250 bytes as both preserved
pre-change DID `0x2023` reads.

The subsequent PROXI alignment required approximately five attempts before an
attempt completed without another failed module. DASM reported a connection
failure on every attempt.

After the recovery alignment and an engine start, BCM DTC `B10AA-00` did not
return. The separate no-odometer-flash, no-ACC-fault, and apparently functional
ACC observations were made earlier while diagnosing the DASM alignment
failure; they support treating that failure as an AlfaOBD-path anomaly but
were not reported as a new post-recovery functional test.

This is strong operational evidence that the BCM and vehicle returned to the
pre-change configuration-consistency state despite AlfaOBD's persistent DASM
result. It does not prove that AlfaOBD completed DASM EOL programming, nor
does it replace a fresh byte-for-byte post-recovery DID `0x2023` comparison.

## Current boundary

Do not repeat the configuration write or alignment merely to make AlfaOBD show
a successful DASM row. Preserve the recovered state. Any further confirmation
should begin with read-only BCM PROXI status/counters, `Headlamp LED
Management`, non-clearing DTC reads, and a fresh DID `0x2023` comparison with
the two retained baseline binaries.

The LED-management option did not produce a usable result in this campaign.
Do not retry it until the headlamp behavior and AlfaOBD's DASM alignment path
have been reviewed.
