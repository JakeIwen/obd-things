"""The two-step shape of a research launch request, shared by the advisor's launch and fallback paths.

A launch is first reserved (ledger entry and attempt id saved, under the manager lock) and then
transmitted (one worker request, outside any lock when the caller can arrange it).
"""

from dataclasses import dataclass

UNSENT = "unsent"  # ``transmit``'s code for a launch that certainly never left the advisor


@dataclass
class Reserved:
    """A launch whose attempt id is saved and whose request is not sent yet."""
    attempt_id: str
    mode_before: str
    body: dict
