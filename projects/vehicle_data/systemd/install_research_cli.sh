#!/bin/sh
# Install, measure or budget-check the research CLI copy under /opt/van-research-cli.
# Usage: sudo install_research_cli.sh claude [--dry-run | --measure | --budget-check]
# The logic is projects/vehicle_data/research_cli_install.py; see docs/dtc-research.md.
set -eu
REPO=$(cd "$(dirname "$0")/../../.." && pwd)
cd "$REPO"
# -B: running as root must not leave root-owned bytecode in the owner's checkout.
exec /usr/bin/python3 -B -m projects.vehicle_data.research_cli_install "$@"
