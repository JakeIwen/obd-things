# Generic tool contracts

Root `AGENTS.md` remains the repository-wide entry point. Run these programs
as modules from the repository root; keep their CLI modules importable.

## Compute retention

`compute_retention.py` is a thin adapter to the deployed van-compute
administrative entry point. It selects the owner's 30/30-day windows in an
isolated child after checking the known upstream policy. The external project
owns citations, locking, submission fencing and deletion. Do not duplicate them here.
The policy, installation procedure and audit are in `docs/data-archive.md`.

| Public path | Caller | Implementation owner |
| --- | --- | --- |
| `tools.compute_retention` | Owner CLI and `obd-compute-retention.service` | `tools/compute_retention.py`, delegating to installed van-compute |
| `tools/systemd/obd-compute-retention.service` | Systemd timer, after opt-in installation | This repository; installed as a root-owned regular file |
| `tools/systemd/obd-compute-retention.timer` | Systemd timers target | This repository; weekly persistent schedule |

Preview is the default; `--apply` is explicit. No CAN, live telemetry, archive
or cron configuration belongs in this adapter. Unit validation uses
`systemd-analyze verify`. `tests/test_compute_retention.py` exercises the CLI
through a temporary subprocess backend and checks the service contracts.
