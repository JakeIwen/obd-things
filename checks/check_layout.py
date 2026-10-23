#!/usr/bin/env python3
"""Backend layout checks from the backend-layout skill: declared contracts, entry and folder
structure, and import cycles, ratcheted against the `layout` key of .codeshape/baseline.json.
Stdlib only, Python 3.9+; reads source text and never imports or executes project code.

    python3 check_layout.py [check] [--root DIR] [--config FILE] [--baseline FILE] [--json]
    python3 check_layout.py rebaseline [--root DIR] [--allow-dirty]

Rules, settings and exceptions: the skill's checks.md. The modules live in backend_layout/ beside
this file; run it with plain python3 (`-I` on 3.11+ drops this folder from the import path)."""
from backend_layout.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
