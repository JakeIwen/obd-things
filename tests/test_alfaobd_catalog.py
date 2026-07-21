from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.alfaobd_catalog import build_export, candidate_zero_based_refs, load_labels


class AlfaObdCatalogTests(unittest.TestCase):
    def test_load_labels_and_expand_zero_based_references(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "labels.txt"
            path.write_text("Active~|Not active\nDoor status\n", encoding="utf-8")
            labels = load_labels(path)
            self.assertEqual(candidate_zero_based_refs("(0,1)", labels), "Not active")
            self.assertEqual(candidate_zero_based_refs("State: (1)", labels), "State: Door status")
            self.assertEqual(candidate_zero_based_refs("(99)", labels), "(99)")

    def test_export_uses_exact_csv_membership_and_read_only_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            database = root / "catalog.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE ver(version_code INTEGER, version_name TEXT);
                INSERT INTO ver VALUES(134, '2.4.4.0');
                CREATE TABLE ECUUnits(
                    ID INTEGER, unit_name TEXT, ECUNAME TEXT, inittype INTEGER,
                    ecuaddress INTEGER, canid TEXT, adapter TEXT, baudrate INTEGER,
                    canidresp TEXT
                );
                INSERT INTO ECUUnits VALUES(73, 'Body', 'BCM', 5, 64, '', '3', 0, '');
                INSERT INTO ECUUnits VALUES(357, 'RF Hub', 'RFH_CUSW', 6, 199, '', '0', 0, '');
                CREATE TABLE ECUList(
                    ID INTEGER, ECUNAME TEXT, ecutype TEXT, Device_ID INTEGER,
                    Option TEXT, Alfa TEXT, Fiat_Fiat_Pro TEXT, Lancia TEXT,
                    Abarth TEXT, Dodge_RAM TEXT, Chrysler TEXT, Jeep TEXT,
                    Peugeot TEXT, Citroen TEXT, Group_ID INTEGER,
                    Function_ID INTEGER, adapter INTEGER
                );
                INSERT INTO ECUList VALUES(
                    1, 'BCM', 'BCM', 55851, '', '', '', '', '', ',88,', '', '', '', '', 1, 2, 0
                );
                INSERT INTO ECUList VALUES(
                    2, 'RFH_CUSW', 'RFH_FGA', 8887, '', '', '', '', '', ',88,', '', '', '', '', 1, 2, 0
                );
                CREATE TABLE FGA_BCM_DATA(
                    request TEXT, response_name TEXT, device_id TEXT
                );
                INSERT INTO FGA_BCM_DATA VALUES('220001', '(1)', '55851,56056,');
                INSERT INTO FGA_BCM_DATA VALUES('220002', '(1)', '155851,');
                CREATE TABLE isocodes(iso_code TEXT, device_id INTEGER, device_type TEXT);
                INSERT INTO isocodes VALUES('00000000', 55851, 'encrypted');
                """
            )
            connection.commit()
            connection.close()
            labels_path = root / "labels.txt"
            labels_path.write_text("unused\nDoor status\n", encoding="utf-8")

            report = build_export(database, labels_path, 88, [55851])
            device = report["devices"]["55851"]
            self.assertEqual(device["table_row_counts"], {"FGA_BCM_DATA": 1})
            row = device["tables"]["FGA_BCM_DATA"][0]
            self.assertEqual(row["candidate_zero_based"]["response_name"], "Door status")
            self.assertEqual(report["model_rows"][0]["ecuaddress"], 64)
            self.assertEqual(report["model_rows"][1]["ecuaddress"], 199)
            self.assertEqual(device["isocodes"][0]["iso_code"], "00000000")
            self.assertEqual(report["sources"]["label_reference_status"], "unresolved")


if __name__ == "__main__":
    unittest.main()
