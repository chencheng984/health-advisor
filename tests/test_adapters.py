import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path

from make_demo import quantity, write_export
from temp_workspace import temporary_directory
from test_health import cli

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from adapters import sniff


DAY = "2026-10-08"


class Adapters(unittest.TestCase):
    def setUp(self):
        temporary = temporary_directory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.database = self.folder / "cache.sqlite3"

    def text(self, name, contents):
        path = self.folder / name
        path.write_text(contents, encoding="utf-8")
        return path

    def apple(self):
        path = self.folder / "apple.fixture"
        write_export(path, [quantity("StepCount", DAY + " 10:00:00 +0800", DAY + " 11:00:00 +0800", 120, "count")])
        return path

    def normalized(self, *rows, name="normalized.fixture"):
        template = {
            "schema": "normalized-health/1", "metric": "steps", "day": DAY,
            "start": DAY + "T00:00:00Z", "end": DAY + "T00:00:00Z", "value": 300, "unit": "count", "state": "",
            "source": {"brand": "garmin", "kind": "garmin", "label": "Generic"},
        }
        return self.text(name, "\n".join(json.dumps({**template, **row}, ensure_ascii=False) for row in rows or ({},)) + "\n")

    def run_json(self, *args):
        result = cli(self.database, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_sniff_apple_by_zip_contents(self):
        self.assertEqual(sniff(self.apple()), "apple")

    def test_sniff_normalized_by_schema(self):
        self.assertEqual(sniff(self.normalized()), "normalized")

    def test_sniff_garmin_case_insensitive(self):
        path = self.text("vendor.data", "dAtE,sTePs,Resting Heart Rate\n" + DAY + ",300,51\n")
        self.assertEqual(sniff(path), "garmin")

    def test_sniff_huawei_csv_and_json(self):
        csv_file = self.text("mixed.data", "Date,步数\n" + DAY + ",100\n")
        json_file = self.text("json.data", json.dumps({"records": [{"date": DAY, "totalSteps": 300}]}))
        for path in (csv_file, json_file):
            self.assertEqual(sniff(path), "huawei")
        imported = self.run_json("import", json_file)
        self.assertEqual(imported["imports"][0]["format"], "huawei")
        self.assertEqual(imported["sources"][0]["brand"], "huawei")

    def test_normalized_roundtrip_through_query(self):
        original = self.normalized()
        first = self.run_json("import", original, "--format", "normalized")
        self.assertEqual(first["imports"][0]["format"], "normalized")
        query = self.run_json("query", "--metric", "steps", "--daily")["metrics"]["steps"]["daily"][0]
        self.assertEqual(query["records_used"], 1)
        rewritten = self.normalized({"day": query["date"], "value": query["value"]}, name="again.data")
        merged = self.run_json("import", rewritten, "--merge")
        self.assertEqual(merged["imports"][0]["samples_added"], 0)
        self.assertEqual(self.run_json("query", "--metric", "steps", "--daily")["metrics"]["steps"]["daily"], [query])

    def test_normalized_converts_units_and_keeps_offsets(self):
        converted = self.normalized(
            {"metric": "distance", "value": 2, "unit": "mi"},
            {"metric": "oxygen_saturation", "value": 97, "unit": "%"},
            {"metric": "body_fat", "value": 0.24, "unit": "fraction"},
            {"metric": "sleep", "value": 7, "unit": "h", "state": "core",
             "start": DAY + "T23:00:00+08:00", "end": "2026-10-09T06:00:00+08:00"},
        )
        self.run_json("import", converted)
        metrics = self.run_json("query", "--days", 2)["metrics"]
        self.assertAlmostEqual(metrics["distance"]["recent"]["median"], 3.218688)
        self.assertEqual(metrics["oxygen_saturation"]["recent"]["median"], 97)
        self.assertEqual(metrics["body_fat"]["recent"]["median"], 24)
        self.assertEqual(metrics["sleep"]["recent"]["median"], 7)
        self.assertEqual(metrics["sleep"]["recent"]["first_observation"], "2026-10-09")
        self.assertEqual(metrics["sleep"]["recent"]["utc_offsets_minutes"], [480])

    def test_unknown_normalized_metric_is_counted_without_retaining_name(self):
        imported = self.run_json("import", self.normalized({"metric": "fixture-private-name"}, {"metric": "other-unsupported"}, {}))
        item = next(row for row in imported["inventory"] if row["identifier"] == "unsupported_metric")
        self.assertEqual((item["records_seen"], item["skipped"]), (2, 2))
        self.assertEqual(imported["imports"][0]["samples_total"], 1)
        self.assertNotIn(b"fixture-private-name", self.database.read_bytes())

    def test_private_vendor_and_normalized_values_never_reach_database(self):
        hidden = ("张三示例", "demo@example.invalid", "13900000000", "X8Y7Z6T5R4", "30.239500", "fixture-account")
        huawei = self.text("private.data", json.dumps({"patient": hidden[0], "data": [{"Date": DAY, "Steps": 42,
            "user": hidden[0], "email": hidden[1], "phone": hidden[2], "deviceSerial": hidden[3],
            "latitude": hidden[4], "track": [{"lat": hidden[4]}], "accountId": hidden[5]}]}, ensure_ascii=False))
        first = self.run_json("import", huawei)
        branded = self.normalized({"patient": hidden[0], "source": {"kind": "watch", "brand": "huawei", "label": hidden[3]}})
        second = self.run_json("import", branded, "--merge")
        both = json.dumps([first, second, self.run_json("status", "--show-source-names")], ensure_ascii=False)
        blob = self.database.read_bytes()
        for value in hidden:
            self.assertNotIn(value, both)
            self.assertNotIn(value.encode(), blob)
        checks = self.run_json("privacy", "--json")
        self.assertTrue(all(row["status"] == "pass" for row in checks))

    def test_schema_four_readability_and_migration_preserve_counts_and_ids(self):
        self.run_json("import", self.apple())
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("ALTER TABLE sources DROP COLUMN brand")
            db.execute("ALTER TABLE imports DROP COLUMN format")
            db.execute("PRAGMA user_version = 4")
            source_ids = db.execute("SELECT id FROM sources").fetchall()
            samples = db.execute("SELECT * FROM samples").fetchall()
        self.assertEqual(self.run_json("status")["schema_version"], 4)
        self.assertEqual(self.run_json("migrate"), {"state": "migrated", "schema_version": 5})
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT id, brand FROM sources").fetchall(), [(key, "apple") for key, in source_ids])
            self.assertEqual(db.execute("SELECT * FROM samples").fetchall(), samples)
            self.assertEqual(db.execute("SELECT format FROM imports").fetchall(), [("apple",)])
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], 0x41484144)
        self.assertTrue(all(row["status"] == "pass" for row in self.run_json("privacy", "--json")))
        original = self.database.read_bytes()
        self.assertEqual(self.run_json("migrate")["state"], "reused")
        preview = cli(self.database, "purge", "--scope", "cache")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(self.database.read_bytes(), original)

    def test_three_brand_merge_keeps_brand_sources_and_deduplicates_reimport(self):
        garmin = self.text("garmin.csv", "Date,Steps\n" + (DAY + ",300\n") * 2)
        huawei = self.text("huawei.csv", "日期,步数\n" + (DAY + ",300\n") * 2)
        self.run_json("import", self.apple())
        self.run_json("import", garmin, "--merge")
        status = self.run_json("import", huawei, "--merge")
        self.assertEqual({row["format"] for row in status["imports"]}, {"apple", "garmin", "huawei"})
        self.assertEqual([row["samples_added"] for row in status["imports"]], [1, 1, 1])
        self.assertEqual(status["imports"][0]["samples_total"], 3)
        self.assertEqual({row["brand"] for row in status["sources"]}, {"apple", "garmin", "huawei"})
        garmin_copy = self.normalized({"end": "2026-10-09T00:00:00Z"})
        duplicate = self.run_json("import", garmin_copy, "--merge")
        self.assertEqual((duplicate["imports"][0]["samples_added"], duplicate["imports"][0]["samples_total"]), (0, 3))
        steps = self.run_json("query", "--metric", "steps")["metrics"]["steps"]["recent"]
        self.assertEqual((steps["period_total"], steps["overlapping_records_skipped"]), (120, 2))
        for row in self.run_json("privacy", "--json"):
            self.assertEqual(row["status"], "pass", row["check"])

    def test_garmin_csv_activity_sleep_and_heart_fields(self):
        header = "Date,Activity Type,Time,Avg HR,Total Sleep Time,Distance (mi),Resting HR,Ignored\n"
        path = self.text("activities.csv", header + DAY + "T10:00:00,Running,00:30:00,120,07:30,2,55,example\n")
        status = self.run_json("import", path, "--format", "garmin")
        unknown = next(row for row in status["inventory"] if row["identifier"] == "unsupported_columns")
        self.assertGreaterEqual(unknown["skipped"], 1)
        query = self.run_json("query")["metrics"]
        self.assertEqual(query["workout_minutes"]["recent"]["median"], 30)
        self.assertEqual(query["heart_rate"]["recent"]["median"], 120)
        self.assertEqual(query["resting_hr"]["recent"]["median"], 55)
        self.assertEqual(query["sleep"]["recent"]["median"], 7.5)
        self.assertEqual(query["sleep"]["sleep_details"]["recent"]["schedule"]["nights"], 0)
        self.assertAlmostEqual(query["distance"]["recent"]["median"], 3.218688)

    def test_huawei_csv_mixed_labels_and_unit_conversion(self):
        header = "Date,步数,距离(米),睡眠时长(分钟),Active Calories (kJ),用户\n"
        path = self.text("huawei.csv", header + DAY + ",300,1500,420,4184,raw-name\n")
        status = self.run_json("import", path)
        unknown = next(row for row in status["inventory"] if row["identifier"] == "unsupported_columns")
        self.assertEqual(unknown["skipped"], 1)
        query = self.run_json("query")["metrics"]
        for key, expected in {"steps": 300, "distance": 1.5, "sleep": 7, "active_energy": 1000}.items():
            self.assertAlmostEqual(query[key]["recent"]["median"], expected)
        self.assertNotIn(b"raw-name", self.database.read_bytes())
