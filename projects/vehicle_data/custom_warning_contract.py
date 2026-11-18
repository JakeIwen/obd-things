"""Custom early-warning vocabulary shared by the broker, the advisor and the dashboard.

``custom_warnings.json`` is the one copy of the rule fields, limits, file bounds, form defaults
and request bodies; the dashboard imports the same file. This module only loads it, with no
metric catalog import, so light callers (the web relay) can use it. The meaning of each field
is in ``docs/custom-warnings.md``.
"""

import json
from pathlib import Path

CONTRACT = json.loads(Path(__file__).with_name("custom_warnings.json").read_text())

FIELDS = frozenset(CONTRACT["fields"])
OPERATORS = tuple(CONTRACT["operators"])
CATALOG_QUALITIES = frozenset(CONTRACT["catalog_qualities"])
MAX_RULES = CONTRACT["max_rules"]
MAX_FILE_BYTES = CONTRACT["max_file_bytes"]
MAX_REMOVED_IDS = CONTRACT["max_removed_ids"]
EVALUATOR_REVISION = CONTRACT["evaluator_revision"]
LIMITS = {key: tuple(bounds) for key, bounds in CONTRACT["limits"].items()}
ENGINE_RUNNING_RPM_ABOVE = CONTRACT["engine_running_rpm_above"]
ENGINE_RUNNING_RPM_MAX_AGE_SECONDS = CONTRACT["engine_running_rpm_max_age_seconds"]
ADVICE_GAP_INTERVAL_MULTIPLE = CONTRACT["advice_gap_interval_multiple"]
FORM_DEFAULTS = CONTRACT["form_defaults"]
REQUESTS = {key: frozenset(fields) for key, fields in CONTRACT["requests"].items()}
