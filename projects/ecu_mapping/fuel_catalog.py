"""Offline fuel-economy leads from the owner's AlfaOBD catalog.

Plain labels use the APK loader's J1 bank after ######. Enum references belong
to a different bank and remain raw. This report never asserts installed-ECU
support and never opens CAN or a vehicle interface.
"""

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
import re

from lib.reports import atomic_json, require_distinct_output
from tools.alfaobd_catalog import has_table, load_labels, open_read_only, sha256


TABLES = ('FGA_PETROL_DATA', 'FGA_IPC_DATA', 'FGA_BCM_DATA', 'FGA_ENGINE_DATA')
PLAIN_REFERENCE = re.compile(r'\((\d+)\)')
FUEL_LABEL = re.compile(
    r'consumption|fuel.*(?:rate|used|volume|mass|quantity|flow|economy)|'
    r'injection.*quantity|air.*(?:mass|flow)|\bmpg\b', re.IGNORECASE,
)
MAXIMUM_FIELDS = 2000


@dataclass(frozen=True)
class FuelCatalogField:
    table: str
    rowid: int
    label: str
    group: str
    raw: dict


def plain_label_bank(path):
    labels = load_labels(path)
    separators = [i for i, label in enumerate(labels) if label.strip() == '######']
    if len(separators) != 1:
        raise ValueError('expected the single APK J1/K1 label-bank separator')
    return labels[separators[0] + 1:]


def expand_plain_references(value, labels):
    if not isinstance(value, str):
        return '' if value is None else str(value)

    def replace(match):
        index = int(match.group(1))
        return labels[index].replace('~', '\n') if index < len(labels) else match.group(0)

    return PLAIN_REFERENCE.sub(replace, value)


def extract_fields(database, labels):
    fields = []
    with closing(open_read_only(database)) as connection:
        tables = [table for table in TABLES if has_table(connection, table)]
        if not tables:
            raise ValueError('database has no supported catalog table')
        for table in tables:
            columns = {row['name'] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            missing = {'request', 'response_name'} - columns
            if missing:
                raise ValueError(f'{table} missing required columns: {", ".join(sorted(missing))}')
            for row in connection.execute(f'SELECT rowid AS catalog_rowid, * FROM "{table}"'):
                raw = dict(row)
                label = expand_plain_references(raw['response_name'], labels)
                # BCM rows have no group label in the owner-supplied catalog.
                group = expand_plain_references(raw.get('group_name'), labels)
                if not FUEL_LABEL.search(label + ' ' + group):
                    continue
                fields.append(FuelCatalogField(table, raw.pop('catalog_rowid'), label, group, raw))
                if len(fields) > MAXIMUM_FIELDS:
                    raise ValueError('fuel-field report exceeded its bounded inventory')
    return fields


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('labels', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    require_distinct_output(args.output, (args.database, args.labels))
    labels = plain_label_bank(args.labels)
    fields = extract_fields(args.database, labels)
    report = {
        'schema_version': 1,
        'classification': 'catalog_candidates_not_vehicle_validated',
        'label_namespace': 'J1 plain labels after ######; K1 enum references left raw',
        'sources': {'database_sha256': sha256(args.database),
                    'labels_sha256': sha256(args.labels)},
        'label_anchors': {str(i): labels[i] for i in (7979, 7987, 8051) if i < len(labels)},
        'label_candidates': [{'index': i, 'label': label} for i, label in enumerate(labels)
                             if FUEL_LABEL.search(label)],
        'fields': [asdict(field) for field in fields],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output, report)
    print(f'{len(fields)} candidate fields; {len(labels)} plain labels')


if __name__ == '__main__':
    main()
