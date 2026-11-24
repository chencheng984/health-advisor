import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path

from make_demo import make_demo
from temp_workspace import temporary_directory
from test_health import cli

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from brief import prepare_brief
from privacy import scan_cache


class Privacy(unittest.TestCase):
    def setUp(self):
        temporary = temporary_directory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.database = self.folder / "cache.sqlite3"
        export = self.folder / "fixture.zip"
        make_demo(export)
        imported = cli(self.database, "import", export)
        self.assertEqual(imported.returncode, 0, imported.stderr)

    def checks(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return {item["check"]: item for item in scan_cache(connection, self.database)}

    def json_cli(self, *args):
        result = cli(self.database, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_byte_scan_matches_and_skips_sanitized_labels(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE fixture(value TEXT)")
            connection.executemany("INSERT INTO fixture VALUES (?)", [
                ("Apple Watch",), ("iPhone #1",), ("OPPO #2",), ("HKQuantityTypeIdentifierHeartRate",),
            ])
        self.assertTrue(all(item["status"] == "pass" for item in self.checks().values()))
        private_values = ("张三", "a@example.invalid", "13900000000", "X8Y7Z6T5R4", "OPPO Find X")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executemany("INSERT INTO fixture VALUES (?)", [(value,) for value in private_values])
            connection.execute("INSERT INTO ecg VALUES (1, '', '', 'H2C3B4D5E6', 0, 0)")
            connection.execute("ALTER TABLE routes ADD COLUMN start_latitude REAL")
            connection.execute("INSERT INTO meta VALUES ('zip_path', 'D:/fixture/export.zip')")
        checks = self.checks()
        for key in ("name", "email", "phone", "serial", "device_label"):
            with self.subTest(check=key):
                self.assertEqual(checks[f"bytes_{key}"]["status"], "fail")
        for key in ("ecg_device", "routes_columns", "meta"):
            self.assertEqual(checks[key]["status"], "fail")
        for value in private_values:
            self.assertNotIn(value, str(checks))

    def test_brief_field_allowlist_filters_nested_values(self):
        result = self.json_cli("query", "--metric", "sleep", "--metric", "workout_minutes", "--daily")
        sleep = result["metrics"]["sleep"]
        private = "fixture@example.invalid"
        sleep["recent"]["sources"][0]["label"] = private
        sleep["comparison"]["token"] = private
        sleep["sleep_details"]["recent"]["schedule"]["location"] = private
        sleep["sleep_details"]["recent"]["stage_minutes"]["coordinate"] = private
        workouts = result["metrics"]["workout_minutes"]["workout_details"]["recent"]["by_type"]
        workouts[0].update(type=private, label=private, serial=private)
        brief = prepare_brief(result, "2026-10-28")
        expected = {"label", "unit", "recent", "comparison_period", "comparison"}
        self.assertEqual(set(brief["metrics"]["sleep"]), expected | {"sleep_details"})
        self.assertEqual(set(brief["metrics"]["workout_minutes"]), expected | {"workout_details"})
        sleep = brief["metrics"]["sleep"]
        period_fields = "covered_days calendar_days first_observation last_observation median period_total"
        period_fields += " utc_offsets_minutes overlapping_records_skipped sources"
        self.assertEqual(set(sleep["recent"]), set(period_fields.split()))
        self.assertNotIn(private, str(brief))

    def test_with_identifiers_only_adds_aggregate_source_count(self):
        args = ("--metric", "sleep")
        default = self.json_cli("brief", *args)
        opted_in = self.json_cli("brief", *args, "--with-identifiers")
        self.assertNotIn("source_count", default["metrics"]["sleep"]["recent"])
        self.assertEqual(opted_in["metrics"]["sleep"]["recent"]["source_count"], 1)
        self.assertFalse(opted_in["privacy"]["identifiers_exposed"])
        self.assertEqual(set(opted_in["privacy"]), {"level", "raw_files_read", "identifiers_exposed", "notes"})
        for flags, expected in (((), default["privacy"]), (("--with-identifiers",), opted_in["privacy"])):
            report = cli(self.database, "report", *args, *flags)
            self.assertEqual(report.returncode, 0, report.stderr)
            _, header, closing_marker, *_ = report.stdout.splitlines()
            self.assertEqual(closing_marker, "---")
            self.assertEqual(json.loads(header.removeprefix("privacy: ")), expected)
            self.assertNotIn("Apple Watch #", report.stdout)
            self.assertEqual("Apple Watch（1 个来源）" in report.stdout, bool(flags))

    def test_purge_dry_run_keeps_files_and_yes_respects_scope(self):
        runtime = self.folder / ".run"
        runtime.mkdir()
        memory = self.folder / "memory.json"
        transient = self.folder / ".health-ignored.sqlite3"
        cache_files = [self.database, runtime / "analysis", transient, Path(str(self.database) + "-journal"),
                       Path(str(transient) + "-wal"), Path(str(self.database) + "-shm")]
        for artifact in [*cache_files[1:], memory]:
            artifact.write_bytes(b"test bytes")
        for flags in ((), ("--dry-run",)):
            preview = cli(self.database, "purge", "--scope", "all", *flags)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn("10 字节", preview.stdout)
            self.assertIn(".run/", preview.stdout)
            self.assertTrue(all(path.exists() for path in [*cache_files, memory]))
        self.assertEqual(cli(self.database, "purge", "--scope", "memory", "--yes").returncode, 0)
        self.assertFalse(memory.exists())
        self.assertTrue(all(path.exists() for path in cache_files))
        self.assertEqual(cli(self.database, "purge", "--scope", "cache", "--yes").returncode, 0)
        self.assertFalse(any(path.exists() for path in cache_files))
        self.assertFalse(runtime.exists())
        self.assertTrue((self.folder / "fixture.zip").is_file())

    def test_journal_and_wal_detection(self):
        with closing(sqlite3.connect(self.database)) as connection:
            journal = Path(str(self.database) + "-journal")
            wal = Path(str(self.database) + "-wal")
            journal.write_bytes(b"\x00")
            wal.write_bytes(b"hidden@example.invalid")
            checks = {item["check"]: item for item in scan_cache(connection, self.database)}
        self.assertEqual(checks["journal"]["status"], "warn")
        self.assertEqual(checks["wal"]["status"], "warn")
        self.assertEqual(checks["bytes_email"]["status"], "fail")

    def test_privacy_json_structure_and_schema_3_exit_status(self):
        checks = self.json_cli("privacy", "--json")
        self.assertEqual(len(checks), 12)
        self.assertTrue(all(item["status"] == "pass" for item in checks))
        for item in checks:
            self.assertEqual(set(item), {"check", "status", "detail", "fix"})
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("ALTER TABLE sources ADD COLUMN name TEXT")
            connection.execute("PRAGMA user_version = 3")
        result = cli(self.database, "privacy", "--json")
        self.assertEqual(result.returncode, 1)
        sources = next(item for item in json.loads(result.stdout) if item["check"] == "sources_schema")
        self.assertEqual(sources["status"], "fail")
        self.assertIn("先 migrate 再复检", sources["fix"])
