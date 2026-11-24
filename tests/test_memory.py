import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from make_demo import make_demo
from temp_workspace import temporary_directory
from test_health import cli


class Memory(unittest.TestCase):
    def setUp(self):
        temporary = temporary_directory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.db = self.folder / "health.sqlite3"
        self.path = self.folder / "memory.json"

    def command(self, *args):
        result = cli(self.db, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def json_command(self, *args):
        return json.loads(self.command(*args))

    def install_cache(self):
        fixture = self.folder / "demo.zip"
        make_demo(fixture)
        self.command("import", fixture)

    def test_init_is_idempotent(self):
        self.command("memory", "init")
        self.command("memory", "set", "goal", "固定晨练时段")
        initial = self.path.read_bytes()
        self.command("memory", "init")
        self.assertEqual(self.path.read_bytes(), initial)
        data = json.loads(initial)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["user"], {"birth_year": None})
        self.assertEqual(set(data), {"version", "user", "goals", "constraints", "devices", "reviews", "prefs"})
        self.assertFalse(list(self.folder.glob(".memory-*.tmp")))

    def test_set_appends(self):
        first = self.json_command("memory", "set", "goal", "固定晨练时段")
        second = self.json_command("memory", "set", "goal", "改善睡眠")
        constraint = self.json_command("memory", "set", "constraint", "每周三晚有固定安排")
        device = self.json_command("memory", "set", "device", "Apple Watch")
        data = self.json_command("memory", "show")
        self.assertEqual(data["goals"], [first, second])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(data["constraints"], [constraint])
        self.assertEqual(data["devices"], [device])

    def test_set_updates_existing_id(self):
        original = self.json_command("memory", "set", "goal", "固定晨练时段")
        updated = self.json_command("memory", "set", "goal", "增加午休", "--id", original["id"])
        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(updated["text"], "增加午休")
        self.assertIn("updated", updated)
        self.assertEqual(self.json_command("memory", "show")["goals"], [updated])
        rejected = cli(self.db, "memory", "set", "constraint", "每周轮班", "--id", original["id"])
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.json_command("memory", "show")["constraints"], [])

    def test_forget_requires_explicit_confirmation(self):
        goal = self.json_command("memory", "set", "goal", "改善睡眠")
        self.command("memory", "set", "constraint", "每周轮班")
        preview = cli(self.db, "memory", "forget", goal["id"])
        self.assertNotEqual(preview.returncode, 0)
        self.assertIn("确认", preview.stderr)
        self.assertEqual(self.json_command("memory", "show")["goals"], [goal])
        self.command("memory", "forget", goal["id"], "--yes")
        data = self.json_command("memory", "show")
        self.assertEqual(data["goals"], [])
        self.assertEqual(len(data["constraints"]), 1)
        self.assertNotEqual(cli(self.db, "memory", "forget", "--all").returncode, 0)
        self.command("memory", "forget", "--all", "--yes")
        self.assertEqual(self.json_command("memory", "show")["constraints"], [])

    def test_no_memory_skips_bad_file_in_each_consumer(self):
        self.install_cache()
        self.path.write_text("invalid json", encoding="utf-8")
        output = self.folder / "workbench.html"
        for args in (("brief",), ("report",), ("workbench", "--output", output)):
            with self.subTest(command=args[0]):
                self.assertNotEqual(cli(self.db, *args).returncode, 0)
                clean = self.command(*args, "--no-memory")
                self.assertNotIn("上次结论与本次核对", clean)
                if args[0] == "brief":
                    self.assertNotIn("memory", json.loads(clean))
        self.assertNotIn("上次结论与本次核对", output.read_text(encoding="utf-8"))

    def test_due_and_second_brief_use_latest_review(self):
        self.install_cache()
        first = self.json_command("brief")
        self.assertIsNone(first["memory"]["one_line"])
        self.command("memory", "set", "goal", "改善睡眠")
        self.command("memory", "set", "constraint", "每周轮班")
        today = date.today()
        self.command("memory", "review", "先固定就寝时间", "--recheck-on", today + timedelta(days=2))
        self.assertFalse(self.json_command("brief")["memory"]["due"])
        past = today - timedelta(days=2)
        note = "先减少午后咖啡摄入"
        self.command("memory", "review", note, "--recheck-on", past)
        self.assertEqual(self.json_command("brief")["memory"], {
            "goals": ["改善睡眠"], "constraints": ["每周轮班"], "recheck_on": past.isoformat(),
            "one_line": note, "due": True,
        })
        report = self.command("report", "--memory", self.path)
        output = self.folder / "workbench.html"
        self.command("workbench", "--memory", self.path, "--output", output)
        for text in (report, output.read_text(encoding="utf-8")):
            self.assertIn("上次结论与本次核对", text)
            self.assertIn(note, text)
            self.assertIn("复核已到期", text)
            self.assertNotIn("先固定就寝时间", text)
            self.assertNotIn("每周轮班", text)
        self.command("memory", "review", note, "--recheck-on", today)
        self.assertTrue(self.json_command("brief")["memory"]["due"])

    def test_export_is_parseable_and_supports_custom_memory(self):
        path = self.folder / "other.json"
        self.command("memory", "--memory", path, "set", "goal", "改善睡眠")
        exported = self.json_command("memory", "--memory", path, "export")
        self.assertEqual(exported, json.loads(path.read_text(encoding="utf-8")))
        self.assertFalse(self.path.exists())

    def test_privacy_blocks_identifiers_and_non_summary_input(self):
        self.command("memory", "init")
        baseline = self.path.read_bytes()
        for unsafe in ("demo@example.invalid", "张三", "13900000000", "账号：demo", "80.1,80.2,80.3"):
            with self.subTest(unsafe=unsafe):
                rejected = cli(self.db, "memory", "set", "goal", unsafe)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertNotIn(unsafe, rejected.stderr)
                self.assertEqual(self.path.read_bytes(), baseline)
        checks = self.json_command("privacy", "--json")
        self.assertEqual(next(row for row in checks if row["check"] == "memory")["status"], "pass")
        data = json.loads(baseline)
        data["user"]["email"] = "bad@example.invalid"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        privacy = cli(self.db, "privacy", "--json")
        self.assertEqual(privacy.returncode, 1)
        failure = next(row for row in json.loads(privacy.stdout) if row["check"] == "memory")
        self.assertEqual(failure["status"], "fail")
        self.assertNotIn("bad@example.invalid", privacy.stdout)
