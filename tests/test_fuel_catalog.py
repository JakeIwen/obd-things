"""Fuel catalog reporting preserves the APK's separate label namespaces."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from projects.ecu_mapping.fuel_catalog import main, expand_plain_references, plain_label_bank


class FuelCatalogTests(unittest.TestCase):
    def test_bcm_table_without_optional_group_column_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'catalog.db'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE FGA_BCM_DATA (request TEXT, response_name TEXT)')
                connection.execute("INSERT INTO FGA_BCM_DATA VALUES ('22ABCD','(0)')")
            labels = root / 'labels.txt'
            labels.write_text('enum\n######\nFuel consumption\n', encoding='utf-16')
            output = root / 'report.json'
            main([str(db), str(labels), '--output', str(output)])
            fields = json.loads(output.read_text())['fields']
            self.assertEqual(len(fields), 1)
            self.assertEqual((fields[0]['label'], fields[0]['group']), ('Fuel consumption', ''))

    def test_output_cannot_replace_catalog_or_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'catalog.db'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE FGA_PETROL_DATA '
                                   '(request TEXT, response_name TEXT, group_name TEXT)')
            labels = root / 'labels.txt'
            labels.write_text('enum\n######\nFuel consumption\n', encoding='utf-16')
            before = {path: path.read_bytes() for path in (db, labels)}
            for output in (db, labels):
                with self.subTest(output=output.name), self.assertRaisesRegex(
                        ValueError, 'output must differ'):
                    main([str(db), str(labels), '--output', str(output)])
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_incompatible_schema_is_not_an_empty_candidate_result(self):
        for columns in (None, 'request TEXT, renamed_label TEXT, group_name TEXT'):
            with self.subTest(columns=columns), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                db = root / 'catalog.db'
                with sqlite3.connect(db) as connection:
                    if columns:
                        connection.execute(f'CREATE TABLE FGA_PETROL_DATA ({columns})')
                labels = root / 'labels.txt'
                labels.write_text('enum\n######\nFuel consumption\n', encoding='utf-16')
                output = root / 'report.json'
                with self.assertRaisesRegex(ValueError, 'catalog table|missing required columns'):
                    main([str(db), str(labels), '--output', str(output)])
                self.assertFalse(output.exists())

    def test_valid_catalog_can_have_no_fuel_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'catalog.db'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE FGA_PETROL_DATA '
                                   '(request TEXT, response_name TEXT, group_name TEXT)')
                connection.execute("INSERT INTO FGA_PETROL_DATA VALUES ('222185','(0)','')")
            labels = root / 'labels.txt'
            labels.write_text('enum\n######\nOil life\n', encoding='utf-16')
            output = root / 'report.json'
            main([str(db), str(labels), '--output', str(output)])
            self.assertEqual(json.loads(output.read_text())['fields'], [])

    def test_plain_references_do_not_use_enum_or_whole_file_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'labels.txt'
            path.write_text('enum value\n######\nFuel consumption\nOil life\n', encoding='utf-16')
            labels = plain_label_bank(path)
            self.assertEqual(expand_plain_references('(0)', labels), 'Fuel consumption')
            self.assertEqual(expand_plain_references('(1) [EEPROM]', labels), 'Oil life [EEPROM]')
            self.assertEqual(expand_plain_references('(0,1)', labels), '(0,1)')
            self.assertEqual(expand_plain_references('(99)', labels), '(99)')

    def test_report_links_fields_but_does_not_assert_vehicle_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'catalog.db'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE FGA_PETROL_DATA (request TEXT, '
                                   'response_name TEXT, group_name TEXT, slope TEXT, device_id TEXT)')
                connection.executemany('INSERT INTO FGA_PETROL_DATA VALUES (?,?,?,?,?)', [
                    ('221942', '(0)', '', '0.001953125', '123,456,'),
                    ('222185', '(1)', '', '1', '789,'),
                ])
            labels = root / 'labels.txt'
            labels.write_text('enum\n######\nFuel consumption\nOil life\n', encoding='utf-16')
            out = root / 'result.json'
            main([str(db), str(labels), '--output', str(out)])
            report = json.loads(out.read_text())
            self.assertEqual(report['classification'], 'catalog_candidates_not_vehicle_validated')
            self.assertEqual(len(report['fields']), 1)
            row = report['fields'][0]
            self.assertEqual(row['label'], 'Fuel consumption')
            self.assertEqual(row['raw']['request'], '221942')
            self.assertEqual(row['raw']['device_id'], '123,456,')
            self.assertEqual(row['raw']['slope'], '0.001953125')
            self.assertEqual(report['label_candidates'], [{'index': 0, 'label': 'Fuel consumption'}])

    def test_missing_or_duplicate_separator_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'labels.txt'
            for content in ('Fuel consumption\n', '######\nFuel consumption\n######\n'):
                path.write_text(content, encoding='utf-16')
                with self.assertRaisesRegex(ValueError, 'separator'):
                    plain_label_bank(path)
