"""Wiring for the advisor's DTC research routes: build the parts and start their threads.

``warning_chat.main`` calls ``add_arguments`` and ``build``; its request handler sends research
paths (``is_research_path``) to the returned manager. Research state lives in
``<advisor state>/dtc-research/``: run records under ``runs/``, the launch ledger, the rollback
pause file and the cache of the Mac's credit snapshot (names from ``api_credits.json``, which
spells them for the default state directory).
"""

from pathlib import Path
import sys

from projects.vehicle_data.api_client import TelemetryClient
from projects.vehicle_data.api_credits_contract import PATHS
from projects.vehicle_data.api_credits_snapshot import SnapshotStore
from projects.vehicle_data.assistant_models import Catalog
from projects.vehicle_data.dtc_research import ResearchManager
from projects.vehicle_data.dtc_research_contract import WORKER, is_admin_path, is_research_path
from projects.vehicle_data.dtc_research_ledger import FILE_NAME as LEDGER_FILE, Ledger
from projects.vehicle_data.dtc_research_reconcile import Reconciler
from projects.vehicle_data.dtc_research_store import Store
from projects.vehicle_data.research_worker_client import WorkerClient

__all__ = ["add_arguments", "build", "is_admin_path", "is_research_path"]
BROKER_TIMEOUT = 2  # two broker reads plus one worker request stay inside the relay's 12 s


def add_arguments(parser):
    parser.add_argument("--research-socket", default=WORKER["socket"],
                        help="research worker socket (van-telemetry-research.service)")
    parser.add_argument("--no-dtc-research", action="store_true", help="serve no DTC research routes")


def build(args, state_dir):
    if args.no_dtc_research:
        return None
    base = state_dir / "dtc-research"
    store = Store(base / "runs")
    store.load(log=lambda line: print(line, file=sys.stderr, flush=True))
    worker = WorkerClient(args.research_socket)
    catalog = Catalog(worker)
    snapshots = SnapshotStore(PATHS["snapshotFile"], base / Path(PATHS["snapshotCache"]).name)
    manager = ResearchManager(store, Ledger(base / LEDGER_FILE), catalog, worker,
                              TelemetryClient(args.broker_socket, timeout=BROKER_TIMEOUT),
                              pause_file=base / Path(PATHS["pauseFile"]).name, snapshots=snapshots)
    Reconciler(manager).start()
    catalog.start()
    return manager
