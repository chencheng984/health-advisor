import ast
import json
import re
import sys
import unittest
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

from make_demo import PHONE, WATCH, make_demo, quantity, record, timestamp, write_export
from temp_workspace import temporary_directory
from test_health import CLI, cli


PROFILE = CLI.parents[1] / "templates" / "profile.example.json"


def workbench_data(page):
    pattern = r'<script id="workbench-data" type="application/json">(.*?)</script>'
    return json.loads(re.search(pattern, page, re.DOTALL)[1])


def score(page, dimension):
    pattern = rf'id="score-{dimension}".*?<span class="score-value">(.*?)</span>'
    return re.search(pattern, page, re.DOTALL)[1]


class PageElements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class WorkbenchDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = temporary_directory()
        cls.addClassCleanup(temporary.cleanup)
        cls.folder = Path(temporary.name)
        cls.database = cls.folder / "合成健康.sqlite3"
        archive = cls.folder / "合成导出.zip"
        make_demo(archive)
        result = cli(cls.database, "import", archive)
        if result.returncode:
            raise AssertionError(result.stderr)

    def render(self, name, *args, database=None):
        output = self.folder / f"{name}.html"
        result = cli(database or self.database, "workbench", "--output", output, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(output.resolve()), result.stdout)
        return output.read_text(encoding="utf-8")

    def test_standalone_example_and_personal_scores(self):
        page = self.render("工作台", "--profile", PROFILE)
        visible = page.split('<script id="workbench-data"', 1)[0]
        for title in ("补充记录与目标", "六维评分", "保命层", "恢复层", "成长层", "逐日趋势",
                      "行动优先级", "今天", "本周", "本月", "隐私", "不诊断、不调药、不解读设备波形",
                      "这份导出里还没用上的记录", "本页含示例补充资料", "静态结果", "输入后即时试算"):
            with self.subTest(title=title):
                self.assertIn(title, visible)
        self.assertTrue(workbench_data(page)["example"])
        self.assertIsNone(workbench_data(page)["training"]["load"])
        self.assertNotIn("已确认目标", visible)
        self.assertIn('id="source-goal" data-initial="示例补充资料 · 待确认目标"', visible)
        self.assertIn('id="source-energy" data-initial="示例补充资料"', visible)
        self.assertIn('id="input-status" role="status">示例试算', visible)
        for state in ("待确认目标", "数据不足，待补充", "待确认计划", "精力自评"):
            self.assertIn(f'class="score-state">示例试算 · {state}</p>', visible)
        self.assertIn("HKCorrelationTypeIdentifierBloodPressure</code><span>1 条", page)
        self.assertIn("Apple Watch #1", page)
        self.assertIn("iPhone #2", page)
        self.assertNotIn(WATCH, page)
        self.assertNotIn(PHONE, page)
        self.assertNotIn("示例体重秤", page)
        for raw in ("sourceName", "start_utc", "records_used", "17:30:00"):
            self.assertNotIn(raw, page)
        values = {"sleep": "88", "activity": "80", "recovery": "93", "load": "97", "energy": "50", "adherence": "67"}
        for key, value in {**values, "load": "—", "adherence": "—"}.items():
            self.assertEqual(score(page, key), value, key)
        confirmed = json.loads(PROFILE.read_text(encoding="utf-8"))
        self.assertIs(confirmed.pop("_example"), True)
        actual_profile = self.folder / "个人资料.json"
        actual_profile.write_text(json.dumps(confirmed, ensure_ascii=False), encoding="utf-8")
        personal = self.render("个人工作台", "--profile", actual_profile)
        self.assertIn('id="source-goal" data-initial="已确认目标"', personal)
        for key, value in values.items():
            self.assertEqual(score(personal, key), value, key)
        parser = PageElements()
        parser.feed(page)
        self.assertGreaterEqual(sum(tag == "svg" for tag, _ in parser.elements), 3)
        self.assertTrue(any(tag == "meter" and attrs.get("value") == "88" for tag, attrs in parser.elements))
        self.assertFalse(any(tag in {"iframe", "link", "object", "embed", "img"} for tag, _ in parser.elements))
        self.assertFalse(any({"src", "href", "srcset", "action"} & attrs.keys() for _, attrs in parser.elements))
        self.assertNotRegex(page, r"(?i)https?://|@import|\bfetch\(|\bXMLHttpRequest\b|\bWebSocket\b")
        self.assertIn("default-src 'none'", page)

    def test_no_profile_uses_local_aggregates_and_leaves_unknowns_blank(self):
        page = self.render("默认")
        data = workbench_data(page)
        self.assertFalse(data["example"])
        self.assertIsNone(data["initial"]["goal"])
        self.assertEqual(data["initial"]["sleep_hours"], 6.5)
        self.assertEqual(data["initial"]["training_sessions"], 3)
        self.assertEqual(data["initial"]["training_minutes"], 105)
        self.assertEqual(data["initial"]["resting_hr"], 64)
        for key in ("sleep_goal_hours", "steps_goal", "energy", "energy_days", "stress", "sedentary_hours",
                    "caffeine", "alcohol", "weeks_remaining"):
            self.assertIsNone(data["initial"][key], key)
        self.assertIn('<option value="" selected>待确认</option>', page)
        self.assertIn('id="source-goal" data-initial="待填"', page)
        self.assertIn('class="layer-title">目标待确认', page)
        self.assertIn("数据不足，待补充", page)
        self.assertEqual(score(page, "sleep"), "—")
        self.assertEqual(score(page, "recovery"), "93")

    def test_invalid_profile_errors_are_readable(self):
        path = self.folder / "invalid.json"
        output = self.folder / "invalid.html"
        for text, fragment in (
            ('{"goal":', "不是有效的 JSON"),
            ("[]", "须为 JSON 对象"),
            ('{"energy":NaN}', "不是有效的 JSON"),
            ('{"energy":11}', "主观精力"),
            ('{"training_sessions":1.5}', "训练次数"),
            ('{"load_low":10,"load_high":2}', "负荷上限"),
            ('{"urgent_symptoms":"false"}', "true、false 或 null"),
        ):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                result = cli(self.database, "workbench", "--profile", path, "--output", output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(fragment, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(output.exists())
        missing = cli(self.database, "workbench", "--profile", self.folder / "missing.json", "--output", output)
        self.assertIn("无法读取补充资料", missing.stderr)

    def test_missing_and_exclusive_output(self):
        missing = cli(self.database, "workbench")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("必须提供 --output PATH.html", missing.stderr)
        output = self.folder / "已有.html"
        output.write_bytes(b"existing content")
        collision = cli(self.database, "workbench", "--output", output)
        self.assertNotEqual(collision.returncode, 0)
        self.assertIn("输出文件已存在", collision.stderr)
        self.assertEqual(output.read_bytes(), b"existing content")

    def test_profile_escapes_values_and_unknown_fields_are_not_copied(self):
        path = self.folder / "loose.json"
        supplement = "茶 <半杯> & 14:00"
        path.write_text(json.dumps({"caffeine": supplement, "private": "私密未支持字段", "weeks_remaining": "3"}),
                        encoding="utf-8-sig")
        page = self.render("文字", "--profile", path)
        initial = workbench_data(page)["initial"]
        self.assertEqual(initial["caffeine"], supplement)
        self.assertEqual(initial["weeks_remaining"], 3)
        self.assertIn("茶 &lt;半杯&gt; &amp; 14:00", page)
        self.assertNotIn("私密未支持字段", page)

    def test_target_caps_sleep_duration_and_current_symptoms_take_priority(self):
        path = self.folder / "urgent.json"
        path.write_text(json.dumps({"sleep_goal_hours": 7, "sleep_hours": 10.75, "energy": 10, "energy_days": 6,
                                    "urgent_symptoms": True}), encoding="utf-8")
        page = self.render("急症", "--profile", path)
        self.assertEqual(score(page, "sleep"), "100")
        self.assertEqual(score(page, "energy"), "100")
        self.assertIn('id="priority-title">立即联系当地急救', page)
        self.assertIn('name="urgent_symptoms" type="checkbox" checked disabled', page)
        self.assertLess(page.index("立即联系当地急救"), page.index("六维评分"))
        self.assertIn("主睡眠通常为 22:30 至 05:30", page)
        self.assertIn("补充资料 · 设备汇总 6.5", page)

    def test_activity_valid_zero_and_source_change_with_missing_workout(self):
        old = date(2026, 9, 1)
        new_watch = "合成第二只 Apple Watch"
        records = []
        for index in range(14):
            instant = timestamp(old + timedelta(days=index), "09:00")
            if index < 7 or index in {7, 8, 10, 13}:
                records.append(quantity("StepCount", instant, instant, 320 if index < 7 else 0,
                                        "count", WATCH if index < 7 else new_watch))
        instant = timestamp(old + timedelta(days=13), "10:00")
        records.append(quantity("AppleExerciseTime", instant, instant, 20, "min"))
        archive, database = self.folder / "换表.zip", self.folder / "换表.sqlite3"
        write_export(archive, records)
        imported = cli(database, "import", archive)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        path = self.folder / "步数目标.json"
        path.write_text('{"steps_goal": 3600}', encoding="utf-8")
        output = self.folder / "换表.html"
        result = cli(database, "workbench", "--output", output, "--profile", path, "--days", 7, "--to", "2026-09-14",
                     "--metric", "steps", "--metric", "exercise_minutes", "--metric", "workout_minutes")
        self.assertEqual(result.returncode, 0, result.stderr)
        page = output.read_text(encoding="utf-8")
        self.assertEqual(score(page, "activity"), "0")
        self.assertEqual(score(page, "load"), "—")
        self.assertIsNone(workbench_data(page)["initial"]["training_sessions"])
        self.assertIn("无训练记录（可能是没戴表或器械训练未记录）", page)
        self.assertIn("Apple 运动时间", page)
        self.assertIn("来源有变化，差值仅供参考", page)
        self.assertIn("Apple Watch #2", page)
        self.assertNotIn(new_watch, page)
        self.assertIn("缺测 3 天", page)
        self.assertIn("2026-09-10", page)
        self.assertRegex(page, r'class="trace" d="M[^"]+ L[^"]+ M[^"]+ M[^"]+"')
        self.assertIn("指标未选", page)

    def test_scoring_closes_yesterday_and_honors_explicit_windows(self):
        today = date.today()
        devices = [f"合成匿名夹具 #{index} Apple Watch" for index in range(28)]
        identifiers = [f"HKCategoryTypeIdentifierSynthetic{index:02}" for index in range(43)]
        records = []
        for offset in range(9, -1, -1):
            day = today - timedelta(days=offset)
            for index, source in enumerate(devices):
                stamp = timestamp(day, f"08:{index:02}")
                records.append(quantity("StepCount", stamp, stamp, 100 if offset else 400, "count", source))
            stamp = timestamp(day, "18:00")
            records.append(quantity("AppleExerciseTime", stamp, stamp, 10, "min"))
        inventory_stamp = timestamp(today, "18:00")
        records.extend(record(code, inventory_stamp, inventory_stamp, "合成未使用原记录") for code in identifiers)
        database, archive = self.folder / "今天.sqlite3", self.folder / "今天.zip"
        write_export(archive, records)
        imported = cli(database, "import", archive)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        sources = json.loads(imported.stdout)["sources"]
        self.assertGreaterEqual(len(sources), 28)
        profile = self.folder / "计分测试.json"
        profile.write_text('{"steps_goal":3500}', encoding="utf-8")

        page = self.render("默认含当天", "--profile", profile, database=database)
        trend_start = today - timedelta(days=6)
        score_start = today - timedelta(days=7)
        yesterday = today - timedelta(days=1)
        self.assertIn(f"<dt>趋势窗口</dt><dd>{trend_start} 至 {today}</dd>", page)
        self.assertIn(f"<dt>评分窗口</dt><dd>{score_start} 至 {yesterday}</dd>", page)
        self.assertEqual(workbench_data(page)["score_period"], {"from": str(score_start), "to": str(yesterday)})
        self.assertEqual(score(page, "activity"), "80")
        self.assertIn(f"<title>{today} · 11,200 步</title>", page)
        self.assertIn("Apple 运动时间", page)
        self.assertIn("无训练记录（可能是没戴表或器械训练未记录）", page)
        self.assertEqual(score(page, "load"), "—")
        load_card = re.search(r'<article class="score" id="score-load">.*?</article>', page, re.S)[0]
        self.assertIn('class="score-state">数据不足，待补充', load_card)
        self.assertNotIn("合成匿名夹具", page)
        self.assertNotIn("合成未使用原记录", page)
        self.assertTrue(all(item["label"] in page for item in sources))
        self.assertIn("这份导出里还没用上的记录（43 类）", page)
        self.assertTrue(all(f"<code>{code}</code><span>1 条</span>" in page for code in identifiers))

        historic_end = today - timedelta(days=2)
        historic_start = historic_end - timedelta(days=6)
        historic = self.render("已结束窗口", "--to", historic_end, "--profile", profile, database=database)
        self.assertIn(f"<dt>评分窗口</dt><dd>{historic_start} 至 {historic_end}</dd>", historic)
        self.assertEqual(score(historic, "activity"), "80")

        short = self.render("短窗口", "--metric", "steps", "--from", today - timedelta(days=4), "--to", today,
                            "--profile", profile, database=database)
        self.assertIn("<dt>评分窗口</dt><dd>所选趋势窗口少于 7 天，暂不计分。</dd>", short)
        self.assertIn("5/5 天", short)
        self.assertEqual(short.count("<svg"), 1)
        self.assertEqual(score(short, "activity"), "—")

    def test_scripts_use_python_310_syntax_and_local_or_standard_imports(self):
        files = tuple(CLI.parent.rglob("*.py"))
        modules = {path.stem for path in files} | {path.parent.name for path in files} | sys.stdlib_module_names
        network = {"ftplib", "http", "socket", "ssl", "urllib"}
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name, feature_version=(3, 10))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = {node.module.split(".")[0]}
                else:
                    continue
                self.assertLessEqual(imported, modules, path.name)
                self.assertFalse(imported & network, path.name)


if __name__ == "__main__":
    unittest.main()
