"""Promotional API-credit vocabulary shared by the research worker, the advisor and the dashboard.

``api_credits.json`` is the one copy of the billing values, reasons, failure codes, routes, paths,
the hard stop (``until``), the verified grant table and the ledger, snapshot and fallback limits.
This module only loads it; the worker's rules live in ``api_credits.py`` and the advisor's
balance view in ``api_credits_view.py``. The contract is in docs/dtc-research.md section
"Promotional API credits".
"""

from datetime import datetime
import json
from pathlib import Path
import re

CONTRACT = json.loads((Path(__file__).with_name("api_credits.json")).read_text())


def _instant(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


UNTIL = _instant(CONTRACT["until"])
GRANTS = tuple((grant["label"], _instant(grant["expires"])) for grant in CONTRACT["grants"])
BILLING = CONTRACT["billing"]
API_CREDITS = BILLING["apiCredits"]
SUBSCRIPTION = BILLING["subscription"]
BILLING_REASONS = frozenset(CONTRACT["billingReasons"])
CREDIT_CODES = CONTRACT["creditCodes"]  # code -> latch scope ("account" or "model")
ACCOUNT_CODES = frozenset(code for code, scope in CREDIT_CODES.items() if scope == "account")
MODEL_CODES = frozenset(code for code, scope in CREDIT_CODES.items() if scope == "model")
PROTOCOL = CONTRACT["protocol"]
ROUTES = CONTRACT["routes"]
ADMIN_ROUTES = CONTRACT["adminRoutes"]
PATHS = CONTRACT["paths"]
KEY_RE = re.compile(CONTRACT["keyPattern"])
KEY_FILE_MAX_BYTES = CONTRACT["keyFileMaxBytes"]
LEDGER = CONTRACT["ledger"]
SNAPSHOT = CONTRACT["snapshot"]
FALLBACK = CONTRACT["fallback"]
BALANCE_STATES = CONTRACT["balanceStates"]
