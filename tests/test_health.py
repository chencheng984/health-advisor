import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from zipfile import ZipFile

from make_demo import PHONE, WATCH, make_demo, quantity, record, tag, write_export
from temp_workspace import temporary_directory


CLI = Path(__file__).resolve().parents[1] / "scripts" / "health.py"


def cli(database, *args):
    return subprocess.run(
        [sys.executable, str(CLI), "--db", str(database), *map(str, args)],
        capture_output=True, encoding="utf-8", timeout=30,
    )


class ImportedDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = temporary_directory()
        cls.addClassCleanup(temporary.cleanup)
        cls.folder = Path(temporary.name)
        cls.database = cls.folder / "有空格 的缓存.sqlite3"
        cls.archive = cls.folder / "演示数据.zip"
        make_demo(cls.archive)
        imported = cli(cls.database, "import", cls.archive)
        if imported.returncode != 0:
            raise AssertionError(imported.stderr)
        cls.initial = json.loads(imported.stdout)

    def read(self, *args):
        result = cli(self.database, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("示例用户", result.stdout)
        return json.loads(result.stdout)

    def test_inventory_and_repeat_import(self):
        status = self.read("status")
        self.assertEqual(status, {key: value for key, value in self.initial.items() if key != "state"})
        imported = self.read("import", self.archive)
        self.assertEqual(imported["state"], "reused")
        self.assertEqual(imported["inventory"], status["inventory"])
        steps = next(item for item in status["inventory"] if item["metric"] == "steps")
        self.assertEqual((steps["records_seen"], steps["stored_rows"]), (85, 84))
        pressure = next(item for item in status["inventory"] if item["identifier"].endswith("BloodPressure"))
        self.assertEqual(pressure["status"], "inventory_only")
        self.assertEqual(status["archive"], {"ecg_csv": 1, "gpx": 1, "cda_xml": 1})

    def test_default_period_and_summary(self):
        result = self.read("query", "--daily")
        self.assertEqual(result["report_period"], {"from": "2026-10-22", "to": "2026-10-28"})
        self.assertEqual(result["baseline_period"], {"from": "2026-07-24", "to": "2026-10-21"})
        metrics = result["metrics"]
        for metric, median, baseline in (("steps", 5600, 6900), ("sleep", 6.5, 7.5), ("resting_hr", 64, 60)):
            with self.subTest(metric=metric):
                item = metrics[metric]
                self.assertEqual(item["recent"]["covered_days"], 7)
                self.assertEqual(item["baseline"]["covered_days"], 21)
                self.assertEqual(item["recent"]["median"], median)
                self.assertEqual(item["baseline"]["median"], baseline)
                self.assertEqual(item["comparison"]["difference"], median - baseline)
        self.assertEqual(metrics["steps"]["recent"]["overlapping_records_skipped"], 7)
        self.assertEqual(metrics["steps"]["recent"]["period_total"], 39200)

    def test_brief_defaults_to_two_equal_windows_with_only_aggregate_evidence(self):
        brief = self.read("brief")
        self.assertEqual(brief["comparison_window"], "previous_period")
        self.assertEqual(brief["comparison_period"], {"from": "2026-10-15", "to": "2026-10-21"})
        self.assertEqual(brief["coverage"], {
            "last_indexed_date": "2026-10-28", "days_after_last_indexed_date": 0,
            "metrics_with_recent_data": 6,
        })
        self.assertEqual(set(brief["metrics"]), {
            "sleep", "resting_hr", "hrv", "steps", "exercise_minutes", "workout_minutes",
        })
        sleep = brief["metrics"]["sleep"]
        self.assertEqual(sleep["comparison"]["difference"], -1)
        self.assertEqual(sleep["recent"]["sources"], ["Apple Watch"])
        self.assertEqual(sleep["sleep_details"]["recent"]["schedule"]["main_end"], "05:30")
        workouts = brief["metrics"]["workout_minutes"]
        self.assertEqual(workouts["recent"]["period_total"], 105)
        self.assertEqual(workouts["workout_details"]["recent"]["sessions"], 3)
        self.assertNotIn("daily", sleep)
        self.assertNotIn("daily", workouts)

    def test_brief_keeps_requested_dates_and_preserves_missing_coverage(self):
        brief = self.read("brief", "--metric", "sleep", "--metric", "resting_hr",
                          "--from", "2026-10-29", "--to", "2026-11-02", "--baseline-days", "30")
        self.assertEqual(brief["report_period"], {"from": "2026-10-29", "to": "2026-11-02"})
        self.assertEqual(brief["comparison_period"], {"from": "2026-09-29", "to": "2026-10-28"})
        self.assertEqual(brief["comparison_window"], "personal_baseline")
        self.assertEqual(brief["coverage"]["days_after_last_indexed_date"], 5)
        self.assertEqual(brief["coverage"]["metrics_with_recent_data"], 0)
        self.assertEqual(set(brief["metrics"]), {"sleep", "resting_hr"})
        for item in brief["metrics"].values():
            self.assertIsNone(item["recent"]["median"])
            self.assertEqual(item["comparison"]["state"], "no_recent_data")
            self.assertIsNone(item["comparison"]["difference"])

    def test_expanded_metrics_units_and_missing_days(self):
        metrics = self.read("query")["metrics"]
        self.assertEqual(len(metrics), 15)
        self.assertAlmostEqual(metrics["distance"]["recent"]["median"], 4.02336)
        self.assertAlmostEqual(metrics["active_energy"]["recent"]["median"], 200)
        self.assertEqual(metrics["basal_energy"]["recent"]["median"], 1450)
        self.assertEqual(metrics["oxygen_saturation"]["recent"]["median"], 97)
        self.assertEqual(metrics["vo2_max"]["recent"]["covered_days"], 3)
        self.assertEqual(metrics["vo2_max"]["comparison"]["state"], "insufficient_coverage")
        self.assertEqual(metrics["workout_minutes"]["recent"]["median"], 35)

    def test_explicit_source_priority(self):
        sources = self.read("status")["sources"]
        phone = next(item["id"] for item in sources if item["kind"] == "phone")
        item = self.read("query", "--metric", "steps", "--prefer-source", phone)["metrics"]["steps"]
        self.assertEqual(item["recent"]["median"], 1600)
        self.assertEqual([source["id"] for source in item["recent"]["sources"]], [phone])
        result = cli(self.database, "query", "--prefer-source", 99999)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_adjacent_period_sleep_and_workouts(self):
        result = self.read("query", "--compare-previous", "--metric", "sleep", "--metric", "workout_minutes")
        self.assertEqual(result["comparison_window"], "previous_period")
        self.assertEqual(result["baseline_period"], {"from": "2026-10-15", "to": "2026-10-21"})
        sleep = result["metrics"]["sleep"]["sleep_details"]
        self.assertTrue(sleep["stage_data_available"])
        self.assertEqual(sleep["recent"]["staged_nights"], 7)
        self.assertEqual(sleep["recent"]["stage_minutes"], {
            "core": 1260, "deep": 140, "rem": 315, "unclassified": 1015,
        })
        self.assertAlmostEqual(sleep["recent"]["stage_coverage_percent"], 245 / 390 * 100)
        self.assertEqual(sleep["recent"]["schedule"], {
            "nights": 7, "main_start": "22:30", "main_end": "05:30",
            "start_iqr_minutes": 0, "end_iqr_minutes": 0,
        })
        self.assertEqual(sleep["baseline"]["schedule"]["main_end"], "06:30")
        training = result["metrics"]["workout_minutes"]["workout_details"]
        self.assertEqual(training["recent"]["sessions"], 3)
        self.assertEqual(training["baseline"]["sessions"], 4)
        self.assertEqual({item["type"]: item["minutes"] for item in training["recent"]["by_type"]},
                         {"Walking": 35, "Running": 35, "Cycling": 35})

    def test_report_and_csv(self):
        report = self.folder / "报告.md"
        result = cli(self.database, "report", "--output", report)
        self.assertEqual(result.returncode, 0, result.stderr)
        report_text = report.read_text(encoding="utf-8")
        self.assertEqual(sum(line.startswith("## ") for line in report_text.splitlines()), 5)
        self.assertIn("### 睡眠与作息", report_text)
        self.assertIn("### 训练复盘", report_text)
        self.assertIn("5,600", report_text)
        self.assertIn("39,200", report_text)
        self.assertIn("6.5", report_text)
        self.assertNotIn("示例用户", report_text)
        self.assertNotEqual(cli(self.database, "report", "--output", report).returncode, 0)
        self.assertEqual(report.read_text(encoding="utf-8"), report_text)
        csv = cli(self.database, "query", "--metric", "sleep", "--format", "csv")
        self.assertEqual(csv.returncode, 0, csv.stderr)
        self.assertEqual(len(csv.stdout.splitlines()), 8)
        self.assertIn("sleep,2026-10-22,6.5", csv.stdout)

    def test_invalid_archive_preserves_cache(self):
        broken = self.folder / "broken.zip"
        with ZipFile(broken, "w") as zipped:
            zipped.writestr("export.xml", "<HealthData><Record>")
        rejected = cli(self.database, "import", broken)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(rejected.stdout, "")
        self.assertEqual(self.read("status")["last_indexed_date"], "2026-10-28")
        self.assertFalse(list(self.folder.glob(".health-*.sqlite3")))


class EdgeCases(unittest.TestCase):
    def setUp(self):
        temporary = temporary_directory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.database = self.folder / "cache.sqlite3"

    def import_records(self, records, **kwargs):
        archive = self.folder / "export.zip"
        write_export(archive, records, **kwargs)
        imported = cli(self.database, "import", archive)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        return json.loads(imported.stdout)

    def test_localized_zip_phone_only_and_missing_days(self):
        records = [quantity("StepCount", "2026-10-08 09:00:00 +0800", "2026-10-08 10:00:00 +0800",
                            120, "count", PHONE)]
        self.import_records(records, filename="健康导出/导出.xml", attachments={"health/other.xml": "<Other />"})
        result = cli(self.database, "query", "--metric", "steps", "--metric", "resting_hr")
        self.assertEqual(result.returncode, 0, result.stderr)
        metrics = json.loads(result.stdout)["metrics"]
        self.assertEqual(metrics["steps"]["recent"]["covered_days"], 1)
        self.assertEqual(metrics["steps"]["recent"]["median"], 120)
        self.assertEqual(metrics["steps"]["comparison"]["state"], "insufficient_coverage")
        self.assertIsNone(metrics["resting_hr"]["recent"]["median"])

    def test_overlap_across_date_boundary(self):
        records = [
            quantity("StepCount", "2026-07-23 23:50:00 +0800", "2026-07-24 00:15:00 +0800", 200, "count", WATCH),
            quantity("StepCount", "2026-07-24 00:00:00 +0800", "2026-07-24 00:20:00 +0800", 220, "count", PHONE),
            quantity("StepCount", "2026-07-24 00:20:00 +0800", "2026-07-24 00:40:00 +0800", 50, "count", PHONE),
        ]
        self.import_records(records)
        result = cli(self.database, "query", "--metric", "steps", "--days", "1", "--baseline-days", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        step = json.loads(result.stdout)["metrics"]["steps"]
        self.assertEqual(step["recent"]["median"], 50)
        self.assertEqual(step["baseline"]["median"], 200)
        self.assertEqual(step["recent"]["overlapping_records_skipped"], 1)

    def test_sleep_noon_split_and_offset_duration(self):
        start = "2026-10-08 11:00:00 +0800"
        end = "2026-10-08 16:00:00 +0900"
        self.import_records([record("HKCategoryTypeIdentifierSleepAnalysis", start, end,
                                    "HKCategoryValueSleepAnalysisAsleep")])
        result = cli(self.database, "query", "--metric", "sleep", "--daily", "--days", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(result.stdout)["metrics"]["sleep"]
        self.assertEqual([(row["date"], row["value"]) for row in item["daily"]],
                         [("2026-10-08", 1), ("2026-10-09", 3)])
        self.assertEqual(item["recent"]["utc_offsets_minutes"], [480, 540])
        self.assertEqual(item["sleep_details"]["recent"]["schedule"]["nights"], 0)

    def test_mixed_sleep_stages_do_not_double_count_or_mislabel_naps(self):
        category = "HKCategoryTypeIdentifierSleepAnalysis"
        intervals = [
            ("Asleep", "08 13:00", "08 14:00"),
            ("AsleepUnspecified", "08 22:00", "09 06:00"),
            ("AsleepCore", "08 23:00", "09 02:00"),
            ("AsleepDeep", "09 01:30", "09 03:00"),
            ("Awake", "09 02:00", "09 02:30"),
            ("AsleepREM", "09 04:00", "09 05:00"),
        ]
        self.import_records([
            record(category, f"2026-10-{start}:00 +0800", f"2026-10-{end}:00 +0800",
                   "HKCategoryValueSleepAnalysis" + state)
            for state, start, end in intervals
        ])
        result = cli(self.database, "query", "--metric", "sleep", "--daily")
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(result.stdout)["metrics"]["sleep"]
        self.assertEqual(item["recent"]["median"], 8.5)
        detail = item["daily"][0]["sleep"]
        self.assertEqual(detail["conflict_minutes"], 30)
        self.assertEqual(detail["stage_minutes"], {"core": 150, "deep": 30, "rem": 60, "unclassified": 270})
        self.assertEqual((detail["main_start"], detail["main_end"]), ("22:00", "06:00"))

    def test_workout_events_reconcile_sources_and_duplicate_labels(self):
        start = "2026-10-08 10:00:00 +0800"
        end = "2026-10-08 10:35:00 +0800"

        def workout(sport, source):
            return tag("Workout", startDate=start, endDate=end, duration=30, durationUnit="min",
                       workoutActivityType="HKWorkoutActivityType" + sport, sourceName=source)

        self.import_records([workout("Running", WATCH), workout("Unknown", WATCH), workout("Other", PHONE)])
        result = cli(self.database, "query", "--metric", "workout_minutes", "--daily")
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(result.stdout)["metrics"]["workout_minutes"]
        self.assertEqual(item["recent"]["period_total"], 30)
        self.assertEqual(item["recent"]["overlapping_records_skipped"], 1)
        self.assertEqual(item["daily"][0]["workouts"]["sessions"], 1)
        self.assertEqual(item["workout_details"]["recent"]["by_type"][0]["type"], "Running")

    def test_v1_cache_can_report_and_identical_zip_restores_stage_information(self):
        category = "HKCategoryTypeIdentifierSleepAnalysis"
        self.import_records([record(category, "2026-10-07 23:00:00 +0800", "2026-10-08 06:00:00 +0800",
                                    "HKCategoryValueSleepAnalysisAsleepCore")])
        connection = sqlite3.connect(self.database)
        with connection:
            connection.execute("PRAGMA user_version = 1")
            connection.execute("UPDATE samples SET state = 'asleep'")
        connection.close()
        legacy = cli(self.database, "query", "--metric", "sleep")
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        data = json.loads(legacy.stdout)["metrics"]["sleep"]
        self.assertEqual(data["recent"]["median"], 7)
        self.assertFalse(data["sleep_details"]["stage_data_available"])
        self.assertEqual(data["sleep_details"]["recent"]["schedule"]["main_start"], "23:00")
        upgraded = cli(self.database, "import", self.folder / "export.zip")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        self.assertEqual((json.loads(upgraded.stdout)["state"], json.loads(upgraded.stdout)["schema_version"]),
                         ("imported", 5))
        query = cli(self.database, "query", "--metric", "sleep")
        self.assertEqual(query.returncode, 0, query.stderr)
        summary = json.loads(query.stdout)["metrics"]["sleep"]["sleep_details"]
        self.assertTrue(summary["stage_data_available"])
        self.assertEqual(summary["recent"]["stage_minutes"]["core"], 420)

    def test_source_change_suppresses_baseline_delta_and_zero_is_valid(self):
        records = [
            quantity("StepCount", f"2026-10-{index:02} 10:00:00 +0800",
                     f"2026-10-{index:02} 10:15:00 +0800", 100 if index <= 7 else 0,
                     "count", WATCH if index <= 7 else PHONE)
            for index in range(1, 9)
        ]
        self.import_records(records)
        result = cli(self.database, "query", "--metric", "steps", "--days", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(result.stdout)["metrics"]["steps"]
        self.assertEqual(item["recent"]["median"], 0)
        self.assertEqual(item["comparison"]["state"], "source_changed")
        self.assertIsNone(item["comparison"]["difference"])

    def test_unsupported_unit_is_counted_and_existing_unrelated_file_is_protected(self):
        instant = "2026-10-08 09:00:00 +0800"
        status = self.import_records([quantity("RestingHeartRate", instant, instant, 70, "wrong")])
        self.assertEqual(status["inventory"][0]["skipped"], 1)
        self.assertEqual(status["inventory"][0]["status"], "no_usable_rows")
        unrelated = self.folder / "other.sqlite3"
        unrelated.write_bytes(b"leave unrelated data alone")
        attempt = cli(unrelated, "import", self.folder / "export.zip")
        self.assertNotEqual(attempt.returncode, 0)
        self.assertEqual(unrelated.read_bytes(), b"leave unrelated data alone")

    def test_new_zip_replaces_the_snapshot_in_one_database(self):
        old = "2026-10-02 10:00:00 +0800"
        recent = "2026-10-08 10:00:00 +0800"
        self.import_records([quantity("StepCount", old, old, 100, "count", PHONE)])
        next_archive = self.folder / "next.zip"
        write_export(next_archive, [quantity("StepCount", recent, recent, 200, "count", PHONE)])
        replaced = cli(self.database, "import", next_archive)
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertEqual(json.loads(replaced.stdout)["state"], "imported")
        result = cli(self.database, "query", "--metric", "steps", "--daily")
        self.assertEqual(result.returncode, 0, result.stderr)
        daily = json.loads(result.stdout)["metrics"]["steps"]["daily"]
        self.assertEqual([(row["date"], row["value"]) for row in daily], [("2026-10-08", 200)])


if __name__ == "__main__":
    unittest.main()
