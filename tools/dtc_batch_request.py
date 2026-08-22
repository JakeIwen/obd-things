#!/usr/bin/env python3
"""Consume one fixed web request and run the guarded DTC batch in-process."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import threading

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.dtc_batch import JobStore, atomic_json
from lib.dtc_web import (
    DEFAULT_CURRENT_PATH,
    DEFAULT_CANCEL_DIR,
    DEFAULT_REQUEST_PATH,
    DtcWebRequestError,
    cancel_path_for_job,
    claim_request,
    read_cancel_request,
)
from tools import dtc_batch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", default=str(DEFAULT_REQUEST_PATH))
    parser.add_argument("--current-file", default=str(DEFAULT_CURRENT_PATH))
    parser.add_argument("--cancel-dir", default=str(DEFAULT_CANCEL_DIR))
    parser.add_argument("--job-root", default=str(dtc_batch.DEFAULT_JOB_ROOT))
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        request = claim_request(args.request_file)
    except (OSError, DtcWebRequestError, ValueError) as exc:
        print(f"ERROR: refusing DTC web request: {exc}", file=sys.stderr)
        return 2
    job_id = str(request["job_id"])
    cancel_path = cancel_path_for_job(args.cancel_dir, job_id)
    cancel_stop = threading.Event()
    cancel_thread = None
    store = JobStore(Path(args.job_root), job_id)
    claimed = Path(str(request["claimed_path"]))

    def bridge_cancel() -> None:
        while not cancel_stop.wait(0.1):
            try:
                read_cancel_request(cancel_path, expected_job_id=job_id)
            except FileNotFoundError:
                continue
            except (OSError, DtcWebRequestError, ValueError) as exc:
                print(f"WARNING: DTC cancel request is invalid: {exc}", file=sys.stderr)
                return
            if not store.record_path.is_file():
                continue
            try:
                store.request_cancel(reason="web_operator_request")
                cancel_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f"WARNING: could not bridge DTC cancellation: {exc}", file=sys.stderr)
            return

    try:
        atomic_json(
            Path(args.current_file),
            {
                "schema_version": 1,
                "job_id": job_id,
                "state": "starting",
                "requested_at_epoch": request["created_at_epoch"],
                "source": "tailscale_one_use_local_arm",
            },
        )
        command = [
            "--execute",
            "--confirm-parked",
            "--confirm-park-gear",
            "--confirm-ignition-on-engine-off",
            "--job-id",
            job_id,
            "--job-root",
            str(args.job_root),
            "--json",
        ]
        cancel_thread = threading.Thread(
            target=bridge_cancel,
            name="dtc-web-cancel-bridge",
            daemon=True,
        )
        cancel_thread.start()
        code = dtc_batch.main(command)
        if not store.record_path.is_file():
            atomic_json(
                Path(args.current_file),
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "state": "failed",
                    "failure": f"batch worker exited {code} before creating a job ledger",
                    "requested_at_epoch": request["created_at_epoch"],
                },
            )
        return code
    except BaseException as exc:
        if not store.record_path.is_file():
            try:
                atomic_json(
                    Path(args.current_file),
                    {
                        "schema_version": 1,
                        "job_id": job_id,
                        "state": "failed",
                        "failure": (
                            f"batch worker crashed: {type(exc).__name__}: {exc}"
                        ),
                        "requested_at_epoch": request["created_at_epoch"],
                    },
                )
            except OSError as status_exc:
                print(
                    f"WARNING: could not record DTC worker failure: {status_exc}",
                    file=sys.stderr,
                )
        raise
    finally:
        cancel_stop.set()
        if cancel_thread is not None and cancel_thread.is_alive():
            cancel_thread.join(timeout=1.0)
        try:
            read_cancel_request(cancel_path, expected_job_id=job_id)
        except (FileNotFoundError, OSError, DtcWebRequestError, ValueError):
            pass
        else:
            cancel_path.unlink(missing_ok=True)
        destination = store.directory / "web-request.json"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(claimed, destination)
        except OSError as exc:
            print(
                f"WARNING: could not preserve claimed web request: {exc}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())
