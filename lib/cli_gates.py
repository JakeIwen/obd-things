"""Output-only execution gates for dry-run-first diagnostic commands.

This module neither grants CAN authority nor acquires resources. Callers retain
scope validation, planning output and all route/transport ownership. In particular,
checks that historically ran before a dry run must stay before this helper.
"""

from collections.abc import Iterable
import sys


def execution_gate(
    execute: bool,
    *,
    dry_run_message: str,
    failures: Iterable[tuple[object, str]],
) -> int | None:
    """Return 0 for a printed dry run, 2 for the first failure, else None.

    Each ordered pair is (failure_condition, exact_message). Messages include
    any caller-specific prefix. Dry runs print to stdout without iterating the
    failures; live failures print to stderr. No exception/exit is introduced:
    the caller returns or exits with the result using its existing mechanism.

    A generator can defer checks with function calls until the live path. Keep
    interleaved side effects outside this helper rather than moving checks.
    """
    if not execute:
        print(dry_run_message)
        return 0
    for failed, message in failures:
        if failed:
            print(message, file=sys.stderr)
            return 2
    return None
