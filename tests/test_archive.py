import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

from make_demo import WATCH, quantity, tag, write_export
from temp_workspace import temporary_directory

CLI = Path(__file__).resolve().parents[1] / "scripts" / "health.py"

ECG_CSV = """姓名,张三
出生日期,"2000年1月1日"
记录日期,2026-10-08 09:00:00 +0800
分类,窦性心律
症状,
软件版本,1.90
设备,"Watch6,17"
采样速率,512赫兹

导联,导联I
单位,µV

-390.5
-321.4
12.5
"""

GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="health-advisor synthetic fixture" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Route 2026-10-08</name><trkseg>
    <trkpt lon="115.031400" lat="30.239500"><ele>20.0</ele><time>2026-10-08T01:00:00Z</time></trkpt>
    <trkpt lon="115.032400" lat="30.239500"><ele>24.0</ele><time>2026-10-08T01:01:00Z</time></trkpt>
    <trkpt lon="115.033400" lat="30.239500"><ele>22.0</ele><time>2026-10-08T01:02:00Z</time></trkpt>
  </trkseg></trk>
</gpx>
"""


def cli(database, *args):
    return subprocess.run(
        [sys.executable, str(CLI), "--db", str(database), *map(str, args)],
        capture_output=True, encoding="utf-8", timeout=30,
    )


class Archive(unittest.TestCase):
    def setUp(self):
        temporary = temporary_directory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.database = self.folder / "cache.sqlite3"
        self.archive = self.folder / "export.zip"
        write_export(self.archive, [
            quantity("StepCount", "2026-10-08 09:00:00 +0800", "2026-10-08 10:00:00 +0800", 120, "count"),
        ], attachments={
            "apple_health_export/electrocardiograms/ecg_2026-10-08.csv": ECG_CSV,
            "apple_health_export/workout-routes/route_2026-10-08.gpx": GPX,
        })
        imported = cli(self.database, "import", self.archive)
        self.assertEqual(imported.returncode, 0, imported.stderr)

    def read(self, *args):
        result = cli(self.database, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_ecg_and_route_summary(self):
        summary = self.read("archive")
        ecg = summary["ecg"]
        self.assertEqual((ecg["records"], ecg["first_day"], ecg["last_day"]), (1, "2026-10-08", "2026-10-08"))
        self.assertEqual(ecg["classifications"], [{"label": "窦性心律", "count": 1}])
        self.assertEqual(ecg["devices"], [{"label": "Apple Watch", "count": 1}])
        self.assertEqual(ecg["samples"], 3)
        routes = summary["routes"]
        self.assertEqual(routes["records"], 1)
        self.assertAlmostEqual(routes["total_distance_km"], 0.19, places=2)
        self.assertEqual(routes["total_ascent_m"], 4)
        self.assertEqual(routes["longest_km"], 0.19)

    def test_detail_lists_records(self):
        detail = self.read("archive", "--detail")
        self.assertEqual(len(detail["ecg"]["items"]), 1)
        self.assertEqual(detail["ecg"]["items"][0]["classification"], "窦性心律")
        self.assertEqual(len(detail["routes"]["items"]), 1)
        self.assertAlmostEqual(detail["routes"]["items"][0]["distance_km"], 0.19, places=2)

    def test_patient_fields_never_reach_the_cache(self):
        output = cli(self.database, "archive", "--detail").stdout + cli(self.database, "status").stdout
        self.assertNotIn("张三", output)
        self.assertNotIn("2000年1月1日", output)
        connection = sqlite3.connect(self.database)
        try:
            for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
                for row in connection.execute(f"SELECT * FROM {table}"):
                    for value in row:
                        if isinstance(value, str):
                            self.assertNotIn("张三", value)
                            self.assertNotIn("2000年1月1日", value)
        finally:
            connection.close()


class ArchiveInReport(unittest.TestCase):
    def setUp(self):
        temporary = temporary_directory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        self.database = folder / "cache.sqlite3"
        export = folder / "export.zip"
        local = datetime(2023, 3, 15, 12).astimezone()
        ecg = ECG_CSV.replace("2026-10-08 09:00:00 +0800", local.strftime("%Y-%m-%d %H:%M:%S %z"))
        ecg = ecg.replace("症状,", "症状,private-symptom").replace('"Watch6,17"', '"张三的 Apple Watch X8Y7Z6T5R4"')
        route = GPX.replace("Route 2026-10-08", "private-route")
        for minute in range(3):
            route = route.replace(f"2026-10-08T01:0{minute}:00Z", local.replace(minute=minute).isoformat())
        write_export(export, [
            quantity("StepCount", "2023-03-15 09:00:00 +0800", "2023-03-15 10:00:00 +0800", 120, "count"),
            quantity("StepCount", "2026-11-20 09:00:00 +0800", "2026-11-20 10:00:00 +0800", 90, "count"),
        ], attachments={
            "apple_health_export/electrocardiograms/ecg.csv": ecg,
            "apple_health_export/workout-routes/route.gpx": route,
            "apple_health_export/workout-routes/no-track.gpx": "<gpx/>",
        })
        result = cli(self.database, "import", export)
        self.assertEqual(result.returncode, 0, result.stderr)

    def window(self, command, first="2023-03-10", last="2023-03-20"):
        result = cli(self.database, command, "--from", first, "--to", last)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_report_shows_window_counts_but_never_legacy_free_text(self):
        report = self.window("report")
        self.assertIn("### 心电图与运动路线", report)
        self.assertIn("窦性心律 1 条", report)
        self.assertIn("Apple Watch 1 条", report)
        self.assertIn("共 3 个采样点，有症状填写的记录 1 条", report)
        self.assertIn("本期有 1 条运动路线可算", report)
        hidden = ("张三", "2000年1月1日", "X8Y7Z6T5R4", "private-symptom", "private-route", "30.239500", "-390.5")
        for value in hidden:
            self.assertNotIn(value.encode(), self.database.read_bytes())
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE ecg SET classification = ?, device = ?, symptoms = ?",
                       ("窦性心律 private-symptom", "张三的 Apple Watch X8Y7Z6T5R4", "private-symptom"))
            db.execute("UPDATE routes SET name = 'private-route'")
        old_report = self.window("report")
        old_brief = self.window("brief")
        self.assertIn("其他设备分类 1 条", old_report)
        self.assertIn("Apple Watch 1 条", old_report)
        self.assertEqual(json.loads(old_brief)["archive"]["ecg"]["records_with_symptoms"], 1)
        for value in hidden:
            self.assertNotIn(value, report + old_report + old_brief)

    def test_empty_window_explains_archive_totals_without_a_section(self):
        report = self.window("report", "2026-11-16", "2026-11-22")
        self.assertNotIn("### 心电图与运动路线", report)
        limits = report.split("## 变化与数据限制", 1)[1].split("## 生活方式与下次复盘", 1)[0]
        self.assertIn("档案共 1 条，最近日期为 2023-03-15", limits)
        self.assertIn("档案共 1 条可算路线，最近日期为 2023-03-15", limits)
        self.assertIn("2 个路线文件", limits)

    def test_brief_distinguishes_window_all_archive_and_unusable_route_files(self):
        current = json.loads(self.window("brief"))["archive"]
        self.assertEqual(current["ecg"]["classifications"], [{"label": "窦性心律", "count": 1}])
        self.assertEqual(current["routes"]["total_distance_km"], 0.19)
        self.assertEqual(current["routes"]["total_ascent_m"], 4)
        self.assertEqual(current["routes"]["total_duration_h"], 0.03)
        self.assertEqual(current["routes"]["longest_km"], 0.19)
        old = json.loads(self.window("brief", "2026-11-16", "2026-11-22"))["archive"]
        self.assertEqual((old["ecg"]["records"], old["routes"]["records"]), (0, 0))
        self.assertEqual(old["overall"], {"ecg_records": 1, "ecg_last_day": "2023-03-15", "route_records": 1,
                                          "route_last_day": "2023-03-15", "route_files": 2})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("ALTER TABLE sources DROP COLUMN brand")
            db.execute("ALTER TABLE sources ADD COLUMN name TEXT NOT NULL DEFAULT 'fixture source'")
            db.execute("ALTER TABLE sources ADD COLUMN hardware TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE imports DROP COLUMN format")
            db.execute("PRAGMA user_version = 3")
            stamp = db.execute("SELECT MIN(recorded_utc) FROM ecg").fetchone()[0]
            db.executemany("INSERT INTO ecg SELECT recorded_utc+?, classification, symptoms, device, sample_rate, samples"
                           " FROM ecg WHERE recorded_utc=?", ((i, stamp) for i in range(1, 47)))
            stamp = db.execute("SELECT MIN(start_utc) FROM routes").fetchone()[0]
            db.executemany("INSERT INTO routes SELECT start_utc+?, name, points, distance_m, duration_s, ascent_m"
                           " FROM routes WHERE start_utc=?", ((i, stamp) for i in range(1, 41)))
            db.execute("UPDATE meta SET value = '42' WHERE key = 'gpx'")
        legacy = json.loads(self.window("brief", "2026-11-16", "2026-11-22"))["archive"]
        self.assertEqual((legacy["ecg"]["records"], legacy["routes"]["records"]), (0, 0))
        self.assertEqual((legacy["overall"]["ecg_records"], legacy["overall"]["route_records"],
                          legacy["overall"]["route_files"]), (47, 41, 42))
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TABLE ecg")
            db.execute("DROP TABLE routes")
        missing = json.loads(self.window("brief"))["archive"]
        self.assertEqual(missing["overall"], {"ecg_records": 0, "ecg_last_day": None, "route_records": 0,
                                              "route_last_day": None, "route_files": 0})


class MergeImport(unittest.TestCase):
    def setUp(self):
        temporary = temporary_directory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.database = self.folder / "cache.sqlite3"

    def export(self, name, day, value=100, source=WATCH, hardware=""):
        path = self.folder / name
        write_export(path, [tag(
            "Record", type="HKQuantityTypeIdentifierStepCount", sourceName=source,
            device=f"<hardware:{hardware}>", startDate=f"{day} 09:00:00 +0800",
            endDate=f"{day} 10:00:00 +0800", value=value, unit="count",
        )])
        return path

    def status(self, *args):
        result = cli(self.database, "status", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def legacy_cache(self, version, source=WATCH, hardware="", keep_archive_tables=False):
        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.execute("ALTER TABLE sources RENAME TO sources_v4")
                connection.execute("""
                    CREATE TABLE sources (
                        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, hardware TEXT NOT NULL
                    )
                """)
                connection.execute("INSERT INTO sources SELECT id, kind, ?, ? FROM sources_v4", (source, hardware))
                connection.execute("DROP TABLE sources_v4")
                connection.execute(f"PRAGMA user_version = {version}")
                if version == 2 and not keep_archive_tables:
                    for table in ("imports", "ecg", "routes"):
                        connection.execute(f"DROP TABLE {table}")
                else:
                    connection.execute("ALTER TABLE imports DROP COLUMN format")
        finally:
            connection.close()

    def test_merge_adds_days_and_keeps_history(self):
        first = self.export("first.zip", "2026-10-01")
        second = self.export("second.zip", "2026-10-08")
        self.assertEqual(cli(self.database, "import", first).returncode, 0)
        merged = cli(self.database, "import", second, "--merge")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        self.assertEqual(json.loads(merged.stdout)["state"], "merged")
        status = self.status()
        self.assertEqual([item["mode"] for item in status["imports"]], ["merge", "replace"])
        self.assertEqual(status["imports"][0]["samples_added"], 1)
        self.assertEqual(status["imports"][0]["samples_total"], 2)
        self.assertEqual((status["imports"][0]["first_day"], status["imports"][0]["last_day"]),
                         ("2026-10-08", "2026-10-08"))
        self.assertEqual((status["first_indexed_date"], status["last_indexed_date"]),
                         ("2026-10-01", "2026-10-08"))
        self.assertEqual(len(status["sources"]), 1)
        query = cli(self.database, "query", "--metric", "steps", "--daily",
                    "--from", "2026-10-01", "--to", "2026-10-08")
        self.assertEqual(query.returncode, 0, query.stderr)
        daily = json.loads(query.stdout)["metrics"]["steps"]["daily"]
        self.assertEqual([(row["date"], row["value"]) for row in daily],
                         [("2026-10-01", 100), ("2026-10-08", 100)])

    def test_merge_without_cache_equals_full_import(self):
        result = cli(self.database, "import", self.export("only.zip", "2026-10-01"), "--merge")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["state"], "imported")
        self.assertEqual(data["schema_version"], 5)

    def test_merge_into_v2_cache_upgrades_the_schema(self):
        first = self.export("first.zip", "2026-10-01")
        self.assertEqual(cli(self.database, "import", first).returncode, 0)
        self.legacy_cache(2)
        merged = cli(self.database, "import", self.export("second.zip", "2026-10-08"), "--merge")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        status = self.status()
        self.assertEqual(status["schema_version"], 5)
        self.assertEqual(len(status["imports"]), 1)
        self.assertEqual(status["imports"][0]["mode"], "merge")
        self.assertEqual(status["first_indexed_date"], "2026-10-01")
        self.assertEqual(len(status["sources"]), 1)

    def assert_bad_merge_preserves_v2_cache(self, bad_export, expected_error):
        first = self.export("first.zip", "2026-10-01")
        imported = cli(self.database, "import", first)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.legacy_cache(2, keep_archive_tables=True)
        before = self.status()
        self.assertEqual(before["schema_version"], 2)
        self.assertEqual(len(before["imports"]), 1)
        original_bytes = self.database.read_bytes()

        result = cli(self.database, "import", bad_export, "--merge")
        self.assertEqual(result.returncode, 1)
        self.assertIn(expected_error, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.status(), before)
        self.assertEqual(self.database.read_bytes(), original_bytes)
        self.assertFalse(self.database.with_name(self.database.name + "-journal").exists())

    def test_merge_malformed_xml_preserves_v2_cache(self):
        bad_export = self.folder / "bad-xml.zip"
        record = quantity("StepCount", "2026-10-08 09:00:00 +0800", "2026-10-08 10:00:00 +0800",
                          200, "count", source="另一台 Apple Watch")
        with ZipFile(bad_export, "w") as archive:
            # Fill a batch so rollback has to undo sample writes as well as DDL.
            archive.writestr("export.xml", "<HealthData>" + record * 4096 + "<Record")
        self.assert_bad_merge_preserves_v2_cache(bad_export, "导出 XML 损坏或不完整")

    def test_merge_non_zip_preserves_v2_cache(self):
        bad_export = self.folder / "not-a-zip.zip"
        bad_export.write_bytes(b"not a zip file")
        self.assert_bad_merge_preserves_v2_cache(bad_export, "无法读取 ZIP")

    def test_merge_missing_health_xml_preserves_v2_cache(self):
        with ZipFile(self.folder / "missing-health-xml.zip", "w") as archive:
            archive.writestr("other.xml", "<OtherData/>")
        self.assert_bad_merge_preserves_v2_cache(self.folder / "missing-health-xml.zip", "ZIP 内应包含一份 Apple 健康 XML")

    def assert_private_sources(self, *private_fields):
        for args in ((), ("--show-source-names",)):
            result = cli(self.database, "status", *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            for field in private_fields:
                self.assertNotIn(field, result.stdout)
            sources = json.loads(result.stdout)["sources"]
            self.assertEqual(sources[0]["label"], "iPhone #1")
            self.assertEqual(sources[0].get("name"), "iPhone #1" if args else None)
        database_bytes = self.database.read_bytes()
        for field in private_fields:
            self.assertNotIn(field.encode("utf-8"), database_bytes)

    def test_new_import_stores_no_cleartext_source_fields(self):
        name, hardware = "合成用户甲", "iPhone-合成硬件标识"
        export = self.export("private.zip", "2026-10-01", source=f"{name}的 iPhone", hardware=hardware)
        imported = cli(self.database, "import", export)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assert_private_sources(name, hardware)

    def test_migrate_v3_scrubs_sources_and_preserves_merge_identity(self):
        name, hardware, stale = "合成用户乙", "iPhone-旧硬件标识", "早已删除的合成姓名"
        source = f"{name}的 iPhone"
        export = self.export("private.zip", "2026-10-01", source=source, hardware=hardware)
        self.assertEqual(cli(self.database, "import", export).returncode, 0)
        self.legacy_cache(3, source, hardware)
        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.execute("PRAGMA secure_delete = OFF")
                connection.execute("CREATE TABLE discarded_source(value TEXT)")
                connection.execute("INSERT INTO discarded_source VALUES (?)", (stale * 1024,))
                connection.execute("DROP TABLE discarded_source")
        finally:
            connection.close()
        before = self.database.read_bytes()
        for field in (name, hardware, stale):
            self.assertIn(field.encode("utf-8"), before)
        self.assertNotIn(name, cli(self.database, "status", "--show-source-names").stdout)
        migrated = cli(self.database, "migrate")
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.assertEqual(json.loads(migrated.stdout), {"state": "migrated", "schema_version": 5})
        self.assert_private_sources(name, hardware, stale)
        clean = self.database.read_bytes()
        again = cli(self.database, "migrate")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout)["state"], "reused")
        self.assertEqual(self.database.read_bytes(), clean)
        duplicate = self.export("duplicate.zip", "2026-10-01", source=source, hardware=hardware)
        with ZipFile(duplicate, "a") as archive:
            archive.writestr("unused.txt", "different fingerprint, same sample")
        merged = cli(self.database, "import", duplicate, "--merge")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        status = json.loads(merged.stdout)
        self.assertEqual(status["imports"][0]["samples_added"], 0)
        self.assertEqual(len(status["sources"]), 1)
        self.assert_private_sources(name, hardware, stale)

    def test_merge_without_new_records_reports_zero_added(self):
        first = self.export("first.zip", "2026-10-01")
        self.assertEqual(cli(self.database, "import", first).returncode, 0)
        duplicate = self.folder / "duplicate.zip"
        write_export(duplicate, [
            quantity("StepCount", "2026-10-01 09:00:00 +0800", "2026-10-01 10:00:00 +0800", 100, "count"),
            quantity("StepCount", "2026-10-02 09:00:00 +0800", "2026-10-02 10:00:00 +0800", 200, "count"),
        ])
        merged = cli(self.database, "import", duplicate, "--merge")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        data = json.loads(merged.stdout)
        self.assertEqual(data["state"], "merged")
        self.assertEqual(data["imports"][0]["samples_added"], 1)
        self.assertEqual(data["imports"][0]["samples_total"], 2)


if __name__ == "__main__":
    unittest.main()
