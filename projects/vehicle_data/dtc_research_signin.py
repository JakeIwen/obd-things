"""Owner sign-in for DTC research (run as pi: ``s dtc_research_signin claude``).

``claude`` asks for the long-lived token that ``claude setup-token`` printed, without echoing it,
and saves it mode 0600 on the research volume, where only the research worker reads it. ``status``
says which providers are signed in, never printing a token.
"""

import argparse
import getpass
import os
from pathlib import Path
import sys

from projects.vehicle_data.dtc_research_contract import WORKER
from projects.vehicle_data.research_cli import TOKEN_MAX

VOLUME = Path(WORKER["volume"])


def save_token(volume, token):
    token = token.strip()
    if not token or len(token) > TOKEN_MAX or any(not (ch.isalnum() or ch in "._-") for ch in token):
        raise ValueError("that does not look like the token `claude setup-token` printed")
    if not volume.is_dir():
        raise ValueError(f"{volume} is not mounted; run the deploy steps first")
    path = volume / "claude-oauth-token"
    temporary = volume / ".claude-oauth-token.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token + "\n")
    os.replace(temporary, path)
    return path


def status(volume):
    token = volume / "claude-oauth-token"
    lines = [f"Claude: {'signed in' if token.is_file() else 'not signed in'} ({token})",
             f"Codex: {'signed in' if (volume / 'codex-home' / 'auth.json').is_file() else 'not signed in'}"
             " (disabled for research this round)"]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("what", choices=("claude", "status"))
    parser.add_argument("--volume", type=Path, default=VOLUME)
    args = parser.parse_args(argv)
    if args.what == "status":
        print(status(args.volume))
        return 0
    try:
        path = save_token(args.volume, getpass.getpass("Paste the token from `claude setup-token` (hidden): "))
    except ValueError as exc:
        print(f"Not saved: {exc}", file=sys.stderr)
        return 1
    print(f"Saved to {path} (mode 0600). The dashboard's model list picks it up within a minute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
