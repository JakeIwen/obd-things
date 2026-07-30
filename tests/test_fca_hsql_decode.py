from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import struct
import tempfile
import unittest

from tools.fca_hsql_decode import (
    DecodeError,
    DecodedDatabase,
    decode_database,
    decode_modified_utf8,
    joined_command_report,
    main,
    parse_label_properties,
    parse_row,
    parse_script,
    validate_database,
)


SYNTHETIC_SCRIPT = """\
CREATE SCHEMA PUBLIC AUTHORIZATION DBA
CREATE CACHED TABLE PARENT(ID SMALLINT NOT NULL PRIMARY KEY,NAME VARCHAR(40))
CREATE CACHED TABLE CHILD(ID INTEGER NOT NULL PRIMARY KEY,PARENT_ID SMALLINT,\
COUNT BIGINT,RATIO FLOAT,ACTIVE BOOLEAN,NOTE LONGVARCHAR,\
CONSTRAINT FK_CHILD_PARENT FOREIGN KEY(PARENT_ID) REFERENCES PARENT(ID))
SET TABLE PARENT INDEX'4 0'
SET TABLE CHILD INDEX'5 0'
"""

SYNTHETIC_PROPERTIES = """\
#HSQL database
hsqldb.cache_file_scale=8
hsqldb.cache_version=1.8.0.x
"""


def encode_modified_utf8(value):
    utf16 = value.encode("utf-16-be", errors="surrogatepass")
    output = bytearray()
    for pos in range(0, len(utf16), 2):
        unit = struct.unpack_from(">H", utf16, pos)[0]
        if 0x0001 <= unit <= 0x007F:
            output.append(unit)
        elif unit > 0x07FF:
            output.extend(
                (
                    0xE0 | ((unit >> 12) & 0x0F),
                    0x80 | ((unit >> 6) & 0x3F),
                    0x80 | (unit & 0x3F),
                )
            )
        else:
            output.extend(
                (
                    0xC0 | ((unit >> 6) & 0x1F),
                    0x80 | (unit & 0x3F),
                )
            )
    return bytes(output)


def encode_row(schema, values):
    body = bytearray()
    for name, kind in schema:
        value = values.get(name)
        if value is None:
            body.append(0)
            continue
        body.append(1)
        if kind == "smallint":
            body.extend(struct.pack(">h", value))
        elif kind == "integer":
            body.extend(struct.pack(">i", value))
        elif kind == "bigint":
            body.extend(struct.pack(">q", value))
        elif kind == "double":
            body.extend(struct.pack(">d", value))
        elif kind == "boolean":
            body.append(int(value))
        elif kind == "string":
            encoded = encode_modified_utf8(value)
            body.extend(struct.pack(">I", len(encoded)))
            body.extend(encoded)
        else:
            raise AssertionError(kind)
    size = ((4 + len(body) + 7) // 8) * 8
    return struct.pack(">I", size) + body + bytes(size - 4 - len(body))


def index_record(row_count):
    return struct.pack(">II", 8, row_count)


class SyntheticDatabase:
    def __init__(self, test_case):
        self.temporary = tempfile.TemporaryDirectory()
        test_case.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.data = self.directory / "db.data"
        self.script = self.directory / "db.script"
        self.properties = self.directory / "db.properties"

        definition = parse_script(SYNTHETIC_SCRIPT)
        parent = definition.schemas["PARENT"]
        child = definition.schemas["CHILD"]
        payload = (
            bytes(32)
            + index_record(2)
            + index_record(1)
            + encode_row(parent, {"ID": 1, "NAME": "alpha"})
            + encode_row(parent, {"ID": 2, "NAME": "A\x00\U0001f600"})
            + encode_row(
                child,
                {
                    "ID": 10,
                    "PARENT_ID": 2,
                    "COUNT": -5,
                    "RATIO": 1.25,
                    "ACTIVE": True,
                    "NOTE": None,
                },
            )
        )
        self.data.write_bytes(payload)
        self.script.write_text(SYNTHETIC_SCRIPT, encoding="ascii")
        self.properties.write_text(SYNTHETIC_PROPERTIES, encoding="ascii")


class FcaHsqlDecodeTests(unittest.TestCase):
    def test_parse_script_derives_types_keys_foreign_keys_and_roots(self):
        definition = parse_script(SYNTHETIC_SCRIPT)
        self.assertEqual(
            definition.schemas["PARENT"],
            (("ID", "smallint"), ("NAME", "string")),
        )
        self.assertEqual(definition.primary_keys["PARENT"], ("ID",))
        self.assertEqual(definition.primary_keys["CHILD"], ("ID",))
        self.assertEqual(definition.roots, (("PARENT", 4), ("CHILD", 5)))
        self.assertEqual(len(definition.foreign_keys), 1)
        key = definition.foreign_keys[0]
        self.assertEqual(key.child_columns, ("PARENT_ID",))
        self.assertEqual(key.parent_table, "PARENT")
        self.assertEqual(key.parent_columns, ("ID",))

        composite = parse_script(
            "CREATE CACHED TABLE PAIR("
            "A SMALLINT NOT NULL,B SMALLINT NOT NULL,PRIMARY KEY(A,B))\n"
        )
        self.assertEqual(composite.primary_keys["PAIR"], ("A", "B"))

        with self.assertRaisesRegex(DecodeError, "unsupported SQL type"):
            parse_script(
                "CREATE CACHED TABLE BAD(ID SMALLINT PRIMARY KEY,"
                "AMOUNT DECIMAL(10,2))\n"
            )

    def test_modified_utf8_and_row_types_decode_exactly(self):
        schema = (
            ("SHORT", "smallint"),
            ("INT", "integer"),
            ("LONG", "bigint"),
            ("DOUBLE", "double"),
            ("FLAG", "boolean"),
            ("TEXT", "string"),
            ("EMPTY", "string"),
            ("MISSING", "integer"),
        )
        values = {
            "SHORT": -12,
            "INT": 123456,
            "LONG": -(2**40),
            "DOUBLE": 0.125,
            "FLAG": True,
            "TEXT": "nul:\x00 euro:\u20ac face:\U0001f600",
            "EMPTY": "",
            "MISSING": None,
        }
        record = encode_row(schema, values)
        self.assertEqual(parse_row(record, schema), values)
        self.assertEqual(
            decode_modified_utf8(encode_modified_utf8(values["TEXT"])),
            values["TEXT"],
        )

        bad_marker = bytearray(record)
        bad_marker[4] = 2
        with self.assertRaisesRegex(DecodeError, "bad marker"):
            parse_row(bytes(bad_marker), schema)

        bad_padding = bytearray(record)
        self.assertEqual(bad_padding[-1], 0)
        bad_padding[-1] = 1
        with self.assertRaisesRegex(DecodeError, "nonzero trailing"):
            parse_row(bytes(bad_padding), schema)

        non_finite = encode_row(schema, {**values, "DOUBLE": float("nan")})
        with self.assertRaisesRegex(DecodeError, "non-finite"):
            parse_row(non_finite, schema)

        with self.assertRaisesRegex(DecodeError, "lead byte"):
            decode_modified_utf8(b"\x00")

    def test_full_decode_validates_synthetic_database_and_is_read_only(self):
        fixture = SyntheticDatabase(self)
        before = {
            path: path.read_bytes()
            for path in (fixture.data, fixture.script, fixture.properties)
        }
        database = decode_database(
            fixture.data, fixture.script, fixture.properties
        )

        self.assertEqual(database.metadata["index_record_count"], 2)
        self.assertEqual(database.metadata["row_record_count"], 3)
        self.assertEqual(database.metadata["row_start"], 48)
        self.assertEqual(
            database.metadata["table_counts"], {"PARENT": 2, "CHILD": 1}
        )
        self.assertEqual(database.rows["PARENT"][1]["NAME"], "A\x00\U0001f600")
        self.assertEqual(database.rows["CHILD"][0]["RATIO"], 1.25)
        checks = validate_database(database)
        self.assertEqual(len(checks), 3)
        self.assertIn("PARENT primary key unique (2 rows)", checks)
        self.assertTrue(
            any("CHILD.PARENT_ID -> PARENT.ID" in check for check in checks)
        )

        after = {
            path: path.read_bytes()
            for path in (fixture.data, fixture.script, fixture.properties)
        }
        self.assertEqual(after, before)
        self.assertEqual(
            set(fixture.directory.iterdir()),
            {fixture.data, fixture.script, fixture.properties},
        )

    def test_validation_rejects_duplicate_primary_and_missing_foreign_key(self):
        fixture = SyntheticDatabase(self)
        database = decode_database(
            fixture.data, fixture.script, fixture.properties
        )

        duplicate_rows = {
            table: [dict(row) for row in table_rows]
            for table, table_rows in database.rows.items()
        }
        duplicate_rows["PARENT"].append({"ID": 1, "NAME": "duplicate"})
        duplicate = DecodedDatabase(
            database.definition,
            duplicate_rows,
            database.row_offsets,
            database.metadata,
        )
        with self.assertRaisesRegex(DecodeError, "duplicate primary key"):
            validate_database(duplicate)

        missing_rows = {
            table: [dict(row) for row in table_rows]
            for table, table_rows in database.rows.items()
        }
        missing_rows["CHILD"][0]["PARENT_ID"] = 99
        missing = DecodedDatabase(
            database.definition,
            missing_rows,
            database.row_offsets,
            database.metadata,
        )
        with self.assertRaisesRegex(DecodeError, "missing PARENT references"):
            validate_database(missing)

    def test_rejects_stock_or_spoofed_cache_version(self):
        fixture = SyntheticDatabase(self)
        fixture.properties.write_text(
            SYNTHETIC_PROPERTIES.replace("1.8.0.x", "1.7.0"),
            encoding="ascii",
        )
        with self.assertRaisesRegex(DecodeError, "custom marker"):
            decode_database(fixture.data, fixture.script, fixture.properties)

    def test_label_parser_and_complete_command_joins(self):
        labels = parse_label_properties(
            "100=Synthetic Command\n"
            "101=Synthetic Field\n"
            "102=unit\\: demo\n"
            "103=Choice \\u20ac\n"
            "104=String Choice\n"
        )
        rows = {
            "ECU": [{"ID": 1, "NAME": "DEMO"}],
            "ECU_TO_BUS": [
                {
                    "ECU_ID": 1,
                    "BUS_ID": 1,
                    "REQUEST": 0x123,
                    "RESPONSE": 0x456,
                    "BROADCAST": None,
                }
            ],
            "VAR_VER": [{"ID": 2, "ECU_ID": 1}],
            "COM_SER_VAR_VER": [
                {
                    "ID": 3,
                    "XMIT_STR": "22ABCD",
                    "COM_SER_NAME_ID": 100,
                }
            ],
            "MSG": [
                {
                    "SER_MSG_ID": 4,
                    "COM_SER_VAR_VER_ID": 3,
                    "IS_REQ": False,
                    "BIT_POS": 24,
                    "DDE_NAME_ID": 101,
                    "QUAL_SET_ID": 50,
                    "LINEAR_CONV_ID": 60,
                    "TABLE_CONV_ID": 70,
                    "STR_TABLE_CONV_ID": 80,
                    "ALG_CONV_ID": 90,
                    "IDENTICAL_CONV_ID": 1000,
                }
            ],
            "QUAL_SET": [{"ID": 50, "EXP_VAL": True}],
            "LINEAR_CONV": [
                {
                    "LINEAR_CONV_ID": 60,
                    "SCALE_ID": 1,
                    "SLOPE": 0.5,
                    "OFFSET": -1.0,
                    "UNIT_NAME_ID": 102,
                }
            ],
            "ENCODING": [
                {
                    "ID": 61,
                    "NAME_ID": 103,
                    "LOWER_BOUND": 0,
                    "UPPER_BOUND": 1,
                }
            ],
            "ENCODING_TO_LINEAR_CONV": [
                {"ENCODING_ID": 61, "LINEAR_CONV_ID": 60}
            ],
            "ENCODING_SEQ": [
                {
                    "TABLE_CONV_ID": 70,
                    "ENCODING_ID": 61,
                    "SEQ": 1,
                    "BIT_MASK": 1,
                }
            ],
            "STR_ENCODING": [
                {
                    "ID": 62,
                    "NAME_ID": 104,
                    "LOWER_BOUND": "A",
                    "UPPER_BOUND": "Z",
                }
            ],
            "STR_ENCODING_SEQ": [
                {
                    "STR_TABLE_CONV_ID": 80,
                    "STR_ENCODING_ID": 62,
                    "SEQ": 1,
                }
            ],
            "ALG_CONV": [{"ID": 90, "FILE_NAME": "synthetic.esu"}],
            "IDENTICAL_CONV": [
                {"ID": 1000, "CONV_TYPE_ID": 5, "LEN_TYPE_ID": 1}
            ],
        }

        report = joined_command_report(
            rows, labels, ["22abcd", "22EEEE"]
        )
        self.assertEqual(report["unmatched_xmits"], ["22EEEE"])
        command = report["commands"][0]
        self.assertEqual(command["name_label"], "Synthetic Command")
        self.assertEqual(command["messages"][0]["dde_label"], "Synthetic Field")
        conversions = report["resolved_conversions"]
        self.assertEqual(conversions["linear"]["60"][0]["unit_label"], "unit: demo")
        self.assertEqual(
            conversions["table"]["70"][0]["encoding"]["name_label"],
            "Choice \u20ac",
        )
        self.assertEqual(
            conversions["table"]["70"][0]["linear_conversions"][0]["SLOPE"],
            0.5,
        )
        self.assertEqual(
            conversions["string_table"]["80"][0]["encoding"]["name_label"],
            "String Choice",
        )
        self.assertTrue(
            all(not missing for missing in report["unresolved_references"].values())
        )
        self.assertTrue(
            all(not missing for missing in report["unresolved_label_ids"].values())
        )

        wrong_labels = joined_command_report(
            rows, {999: "Wrong module dictionary"}, ["22ABCD"]
        )
        self.assertEqual(
            wrong_labels["unresolved_label_ids"],
            {
                "command": [100],
                "dde": [101],
                "unit": [102],
                "encoding": [103, 104],
            },
        )
        self.assertTrue(
            all(
                not missing
                for missing in wrong_labels["unresolved_references"].values()
            )
        )

    def test_cli_defaults_to_json_summary_without_creating_output(self):
        fixture = SyntheticDatabase(self)
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    str(fixture.data),
                    "--script",
                    str(fixture.script),
                    "--properties",
                    str(fixture.properties),
                ]
            )
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["mode"], "summary")
        self.assertEqual(report["metadata"]["row_record_count"], 3)
        self.assertNotIn("output", report)


if __name__ == "__main__":
    unittest.main()
