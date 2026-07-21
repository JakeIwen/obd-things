from pathlib import Path
import sqlite3
import tempfile
import unittest
from zipfile import ZipFile

from tools.alfaobd_apk_db import database_members, extract_database, extract_member


def make_database(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE labels (name TEXT)")
    connection.execute("INSERT INTO labels VALUES ('vehicle speed')")
    connection.commit()
    connection.close()


class AlfaObdApkDatabaseTests(unittest.TestCase):
    def test_extract_database_reassembles_ordered_chunks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            database = tmp_path / "source.db"
            make_database(database)
            payload = database.read_bytes()
            apk = tmp_path / "base.apk"
            with ZipFile(apk, "w") as archive:
                archive.writestr("assets/alfaobd.db.002", payload[257:])
                archive.writestr("assets/alfaobd.db.001", payload[:257])

            output = tmp_path / "result.db"
            self.assertEqual(extract_database(apk, output), (2, len(payload)))
            connection = sqlite3.connect(output)
            self.assertEqual(
                connection.execute("SELECT name FROM labels").fetchone(),
                ("vehicle speed",),
            )
            connection.close()

    def test_database_members_rejects_missing_chunk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            apk = Path(tmpdir) / "base.apk"
            with ZipFile(apk, "w") as archive:
                archive.writestr("assets/alfaobd.db.001", b"first")
                archive.writestr("assets/alfaobd.db.003", b"third")

            with ZipFile(apk) as archive, self.assertRaisesRegex(ValueError, "not contiguous"):
                database_members(archive)

    def test_extract_database_rejects_non_sqlite_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            apk = tmp_path / "base.apk"
            with ZipFile(apk, "w") as archive:
                archive.writestr("assets/alfaobd.db.001", b"not a database")

            with self.assertRaisesRegex(ValueError, "SQLite header"):
                extract_database(apk, tmp_path / "result.db")

    def test_extract_member_preserves_resource_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            apk = tmp_path / "base.apk"
            payload = "Active~|Not active\n".encode("utf-16")
            with ZipFile(apk, "w") as archive:
                archive.writestr("res/raw/alfaobd5_en.txt", payload)

            output = tmp_path / "labels.txt"
            self.assertEqual(
                extract_member(apk, "res/raw/alfaobd5_en.txt", output),
                len(payload),
            )
            self.assertEqual(output.read_bytes(), payload)

    def test_extract_member_rejects_missing_resource(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            apk = tmp_path / "base.apk"
            with ZipFile(apk, "w"):
                pass

            with self.assertRaisesRegex(ValueError, "contains no"):
                extract_member(apk, "res/raw/alfaobd5_en.txt", tmp_path / "labels.txt")


if __name__ == "__main__":
    unittest.main()
