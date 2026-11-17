"""The frozen context a research run starts from, gathered by the advisor, never by the model.

The browser supplies only the code's key. Everything else comes from the Pi: the broker's cached
DTC response (the record, from whichever of its three sources has it), the owner's ignore note for
the code, the module registry, the description table and the hand-reviewed facts file. VIN-shaped
strings are masked in all of it. The owner's own text (an ignore note, his questions) is passed as
he wrote it apart from that mask: docs/dtc-research.md names that residual.
"""

import json
from pathlib import Path

from lib.modules import MODULES
from projects.vehicle_data.assistant_common import now
from projects.vehicle_data.dtc_descriptions import describe_dtc
from projects.vehicle_data.dtc_ignore import API_PATH as DTC_IGNORE_PATH
from projects.vehicle_data.dtc_research_contract import SIZES, encoded_len
from projects.vehicle_data.dtc_research_prompt import mask_vin

FACTS = json.loads(Path(__file__).with_name("dtc_research_facts.json").read_text())
DTC_PATH = "/v1/diagnostics/dtcs"
RECORD_FIELDS = (
    "display_group", "status", "status_flags", "confirmed", "pending", "current", "present",
    "warning_indicator_requested", "first_seen_at", "last_seen_at", "observation_count",
    "episode_count", "recurrence_count", "logical_bus", "module_name", "observed_at",
)
SOURCES = (("pi_scan", "groups"), ("in_vehicle_scan", "in_vehicle_scan"), ("drive_read", "drive_reads"))


def find_record(response, module_key, fca_display):
    """``(source, record)`` for the first match in the DTC response's three sources, or ``(None, None)``."""
    if not isinstance(response, dict):
        return None, None
    for source, key in SOURCES:
        found = _walk(response.get(key), module_key, fca_display, None)
        if found is not None:
            return source, {name: found[name] for name in RECORD_FIELDS if name in found}
    return None, None


def _walk(node, module_key, fca_display, inherited):
    if isinstance(node, dict):
        here = node.get("module_key") if isinstance(node.get("module_key"), str) else inherited
        if node.get("fca_display") == fca_display and here == module_key:
            return node
        children = node.values()
    elif isinstance(node, list):
        here, children = inherited, node
    else:
        return None
    for child in children:
        found = _walk(child, module_key, fca_display, here)
        if found is not None:
            return found
    return None


def ignore_note(response, module_key, fca_display):
    if not isinstance(response, dict) or response.get("available") is not True:
        return None
    for row in response.get("ignored") or []:
        if isinstance(row, dict) and row.get("module_key") == module_key and row.get("fca_display") == fca_display:
            note = row.get("note")
            return note if isinstance(note, str) and note.strip() else None
    return None


def code_note(module_key, fca_display):
    notes = FACTS["code_notes"]
    return notes.get(f"{module_key}:{fca_display}") or notes.get(f"{module_key}:{fca_display.split('-')[0]}")


def module_entry(module_key):
    module = MODULES.get(module_key)
    if module is None:
        return {"key": module_key, "registered": False}
    return {"key": module_key, "registered": True, "name": module.name, "bus": module.bus,
            "note": module.note or None, "request_id": f"0x{module.txid:X}", "response_id": f"0x{module.rxid:X}"}


def _get(client, path):
    try:
        status, body = client.request("GET", path)
    except (OSError, RuntimeError, ValueError):
        return None
    return body if status == 200 else None


def gather(client, module_key, fca_display):
    """The context snapshot for one code (bounded; see ``SIZES['contextEncodedBytes']``)."""
    source, record = find_record(_get(client, DTC_PATH), module_key, fca_display)
    context = {
        "captured_at": now(),
        "code": {"module_key": module_key, "fca_display": fca_display},
        "module": module_entry(module_key),
        "meaning": describe_dtc(module_key, fca_display),
        "record": record,
        "record_source": source,
        "record_note": None if record else "This code is not in the Pi's current diagnostic data "
                                            "(the broker was unreachable, or the record has aged out).",
        "vehicle": FACTS["vehicle"],
        "owner_code_note": code_note(module_key, fca_display),
        "owner_ignore_note": ignore_note(_get(client, DTC_IGNORE_PATH), module_key, fca_display),
    }
    context = mask_vin(context)
    if encoded_len(context) > SIZES["contextEncodedBytes"]:
        context["record"] = None
        context["record_note"] = "The record was too large to include."
    return context
