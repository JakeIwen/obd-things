"""Compact read-only projections for the Early Warning overview.

Evidence remains in historian records and event detail/export routes. Keep this
projection outside the serialized live snapshot handler; never mutate originals.
"""
import json

OVERVIEW_BUDGET_BYTES = 512 * 1024
DETAIL_ARRAYS = frozenset(('input_buckets', 'observations'))


def _project(value):
    if isinstance(value, dict):
        result = {}
        omitted = []
        for key, item in value.items():
            if key in DETAIL_ARRAYS and isinstance(item, (list, tuple)):
                omitted.append(key)
            else:
                result[key] = _project(item)
        if omitted:
            result['overview_omitted_arrays'] = omitted
        return result
    if isinstance(value, (list, tuple)):
        return [_project(item) for item in value]
    return value


def _size(value):
    # Match the Unix API's ASCII-safe wire serializer, including escaped units.
    return len(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def health_overview(payload, *, byte_budget=OVERVIEW_BUDGET_BYTES):
    result = _project(payload)
    result['evidence_scope'] = 'overview'
    result['event_detail_template'] = '/v1/events/{id}'
    result['overview_limits'] = {
        'byte_budget': byte_budget,
        'detail_arrays_omitted': ['baseline.input_buckets', 'persistence.observations'],
        'omitted_sections': [],
        'detail': 'Detailed evidence is retained in saved event details and exports.',
    }
    # Secondary history can grow independently of the card. If necessary omit it
    # explicitly; never evict an active warning to make the response appear normal.
    optional = [('episodes', 'recent'), ('usb_can_incidents',), ('data_quality', 'recent')]
    for path in optional:
        if _size(result) <= byte_budget:
            break
        parent = result
        for key in path[:-1]:
            parent = parent.get(key, {}) if isinstance(parent, dict) else {}
        if isinstance(parent, dict) and path[-1] in parent:
            parent.pop(path[-1])
            result['overview_limits']['omitted_sections'].append('.'.join(path))
    if _size(result) <= byte_budget:
        return result
    # Defensive failure for unexpectedly large essential data; no false all-clear.
    delivery = payload.get('notification_delivery') or {}
    return {
        'available': False, 'reason': 'health_summary_too_large',
        'detail': 'Early-warning overview exceeds its display budget. Saved events remain available in Event history.',
        'evidence_scope': 'overview', 'event_detail_template': '/v1/events/{id}',
        'notification_delivery': {'enabled': delivery.get('enabled') if type(delivery.get('enabled')) is bool else None},
    }
