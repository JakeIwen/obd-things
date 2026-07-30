# Machine-data retention and EXFAT archive

This document records where untracked evidence lives and how to distinguish cold archived data
from disposable `tmp/` output. It complements the root README's data convention; it does not make
anything under either `tmp/` tree tracked or backed up.

## Retention rules

- Preserve raw data that required this van, a drive, an engine start, an ignition cycle, a
  controlled physical action, AlfaOBD, or a live diagnostic exchange. Promote only reviewed,
  redacted evidence subsets into git.
- Keep active locks/service state, small inventories, current benchmark inputs, executable ECU
  databases, and near-term working results in the Pi's local `tmp/`.
- Put large completed captures, cold raw evidence, and compute history in
  `/mnt/EXFAT512/obd-things/tmp/` after checking the mount and verifying the copy.
- Delete caches, virtual environments, decompiler output, failed staging copies, and other
  mechanical derivatives only when their source remains available.
- A symlink under local `tmp/` is a convenience pointer, not redundancy. If it appears broken,
  inspect `findmnt -T /mnt/EXFAT512` before deciding the artifact is gone.

## Current archive layout

The 2026-07-30 storage audit moved seven large CAN logs into the same relative paths below the
EXFAT tree:

```text
/mnt/EXFAT512/obd-things/tmp/captures/bcan/events/
/mnt/EXFAT512/obd-things/tmp/captures/ccan/events/
/mnt/EXFAT512/obd-things/tmp/captures/ccan/pair6-14_500k_*.log
```

These are 2,132,080,699 bytes of B-CAN/C-CAN drive, ignition, AlfaOBD, and controlled-event
evidence. Each source/destination pair was SHA-256 checked before the local regular file was
replaced with a symlink. Existing finding paths therefore still resolve while EXFAT512 is mounted.
For van-compute inputs, pass the real `/mnt/EXFAT512/...` path because the queue deliberately
rejects symlink inputs.

Three large PROXI/CAN-CH safety captures (298,970,794 bytes) were handled the same way below:

```text
/mnt/EXFAT512/obd-things/tmp/proxi_safety/
```

Their local `tmp/proxi_safety/...` names remain symlinks so the configuration-recovery findings
retain working paths.

## Compute history

The complete compute history at the audit checkpoint is preserved as real directories under:

```text
/mnt/EXFAT512/obd-things/tmp/compute/done/    # 420 jobs
/mnt/EXFAT512/obd-things/tmp/compute/failed/  # 117 jobs
```

Local `tmp/compute/archive` points to that root. The 61 completed jobs whose IDs are cited by
tracked findings/configuration also remain as real local directories under `tmp/compute/done/`,
so their normal `pi_compute.py status` and `result` commands continue to work.

Van-compute intentionally ignores symlinked job directories. Consequently, an archived-only job
is read directly through `tmp/compute/archive/{done,failed}/<job-id>/`; it is not visible to
`pi_compute.py status`, `result`, or `list`. To restore normal queue access, enter van-compute
maintenance, copy the complete archived job directory back as a real
`tmp/compute/{done,failed}/<job-id>/` directory, verify its manifest/results, and then leave
maintenance. Do not replace the queue's `done/` or `failed/` state directory with a symlink:
completion depends on same-filesystem atomic moves.

EXFAT's allocation unit makes the many small compute source snapshots consume substantially more
allocated space than their logical byte count. That is expected; the archive prioritizes direct
per-file access and provenance over compact packing.

## 2026-07-30 cleanup ledger

The audit reduced physical local `tmp/` use from 5,606,412,288 bytes to 1,431,855,104 bytes and
increased root free space from about 2.3 GiB to 5.9 GiB.

The following 697,573,376 bytes were deleted rather than archived because they are mechanical
derivatives:

- two unused APK/androguard Python environments;
- two JADX partial-output trees, one baksmali tree, and one extracted DEX tree, all reproducible
  from the retained owner-supplied APK;
- `tmp/ecu_mapping/compute-inputs/`, whose eight staged compressed captures were byte-identical to
  the canonical EXFAT drive-correlation captures.

The active `tmp/venvs/obd-research/` search environment and `tmp/playwright/` browser runtime were
kept local. ECU catalog databases, benchmark inputs, inventories, sweeps, TPMS/radar data, and all
other smaller or near-term project artifacts were also retained locally.
