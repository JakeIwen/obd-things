"""Owner sign-in for DTC research (run as pi: ``s dtc_research_signin claude``).

``claude`` asks for the long-lived token that ``claude setup-token`` printed, without echoing it,
and saves it mode 0600 on the research volume, where only the research worker reads it.
``api-key`` installs (or, with ``--remove``, deletes) the promotional Anthropic model API key the
same way: typed with hidden input, or read with ``--from-file`` from one regular file holding the
bare key or an ``ANTHROPIC_PROMO_API_KEY=`` line (never sourced or echoed). Both writers refuse to
write unless the research image is really mounted at the volume. ``status`` says which credentials
are installed, never printing one.
"""

import argparse
import getpass
import os
from pathlib import Path
import secrets
import sys
import warnings

from projects.vehicle_data.api_credits import check_key, read_key_file
from projects.vehicle_data.dtc_research_contract import WORKER
from projects.vehicle_data.research_cli import TOKEN_MAX
from projects.vehicle_data.research_volume import Volume, mount_problem

VOLUME = Path(WORKER["volume"])


def require_mounted(volume):
    """Raise ``ValueError`` unless ``volume`` is the mounted research image (not a bare directory on the SD card)."""
    problem = mount_problem(volume)
    if problem:
        raise ValueError(f"{problem}; run the deploy steps first")


def write_secret(directory, name, text):
    """Replace ``directory/name`` with ``text`` mode 0600: a fresh ``O_EXCL|O_NOFOLLOW`` temporary file,
    fsync, rename, then fsync of the directory, so a crash leaves the old file or the whole new one."""
    path = directory / name
    temporary = directory / f".{name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return path


def save_token(volume, token, *, mounted=require_mounted):
    token = token.strip()
    if not token or len(token) > TOKEN_MAX or any(not (ch.isalnum() or ch in "._-") for ch in token):
        raise ValueError("that does not look like the token `claude setup-token` printed")
    mounted(volume)
    return write_secret(volume, Volume(volume).token.name, token)


def save_api_key(volume, key, *, mounted=require_mounted):
    """Install a model API key (admin and OAuth keys and anything else are refused)."""
    key, problem = check_key(key.strip())
    if problem:
        raise ValueError(problem)
    mounted(volume)
    return write_secret(volume, Volume(volume).api_key.name, key)


def remove_api_key(volume, *, mounted=require_mounted):
    mounted(volume)
    path = Volume(volume).api_key
    existed = path.exists() or path.is_symlink()
    path.unlink(missing_ok=True)
    return existed


def read_hidden(prompt):
    """Hidden terminal input. A terminal that cannot hide input is an error, not a fallback to echoing."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ValueError("no terminal that can hide input is available; run this from a terminal") from None


def read_key_source(path):
    """The key in ``path`` (bounded, regular, not a link; the file's own mode is not required to be 0600)."""
    key, problem = read_key_file(path, require_private=False)
    if problem:
        raise ValueError(problem)
    return key


def status(volume):
    volume = Path(volume)
    paths = Volume(volume)
    key, problem = read_key_file(paths.api_key)
    key_state = "installed" if key else ("not installed" if problem == "no API key is installed"
                                         else f"unusable ({problem})")
    lines = [f"Claude: {'signed in' if paths.token.is_file() else 'not signed in'} ({paths.token})",
             f"Claude API key (promotional credits): {key_state} ({paths.api_key})",
             f"Codex: {'signed in' if (volume / 'codex-home' / 'auth.json').is_file() else 'not signed in'}"
             " (disabled for research this round)"]
    return "\n".join(lines)


def main(argv=None, *, prompt=read_hidden, mounted=require_mounted):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("what", choices=("claude", "api-key", "status"))
    parser.add_argument("--volume", type=Path, default=VOLUME)
    parser.add_argument("--from-file", type=Path, help="api-key: read the key from this file")
    parser.add_argument("--remove", action="store_true", help="api-key: delete the installed key")
    args = parser.parse_args(argv)
    if args.what == "status":
        print(status(args.volume))
        return 0
    try:
        if args.what == "api-key" and args.remove:
            existed = remove_api_key(args.volume, mounted=mounted)
            print("The API key was removed; research uses the subscription." if existed
                  else "No API key was installed.")
            return 0
        if args.what == "api-key":
            key = read_key_source(args.from_file) if args.from_file else \
                prompt("Paste the Anthropic API key for research credits (hidden): ")
            path = save_api_key(args.volume, key, mounted=mounted)
            print(f"Saved to {path} (mode 0600). The worker reads it at its next credit launch; no restart.")
            return 0
        path = save_token(args.volume, prompt("Paste the token from `claude setup-token` (hidden): "),
                          mounted=mounted)
    except (ValueError, OSError) as exc:
        print(f"Not saved: {exc}", file=sys.stderr)
        return 1
    print(f"Saved to {path} (mode 0600). The dashboard's model list picks it up within a minute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
