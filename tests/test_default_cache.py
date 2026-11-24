import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from make_demo import make_demo
from temp_workspace import temporary_directory
from test_health import CLI


class DefaultCache(unittest.TestCase):
    def test_default_cache_uses_legacy_then_prefers_new_and_keeps_memory_beside_it(self):
        temporary = temporary_directory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        export = root / "export.zip"
        make_demo(export)

        def command(home, *args):
            home.mkdir(exist_ok=True)
            env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
            result = subprocess.run([sys.executable, str(CLI), *map(str, args)], env=env,
                                    capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout), result.stderr

        fresh_home = root / "fresh-home"
        new_directory = ".health-advisor"
        fresh_db = fresh_home / new_directory / "health.sqlite3"
        self.assertFalse(fresh_db.exists())
        fresh, warning = command(fresh_home, "import", export)
        self.assertEqual(fresh["state"], "imported")
        self.assertTrue(fresh_db.is_file())
        self.assertEqual(warning, "")

        existing_home = root / "existing-home"
        new_db = existing_home / new_directory / "health.sqlite3"
        legacy_db = existing_home / f".apple-{new_directory[1:]}" / new_db.name
        legacy, warning = command(existing_home, "--db", legacy_db, "import", export)
        self.assertEqual(warning, "")
        status, warning = command(existing_home, "status")
        self.assertEqual(legacy["inventory"], status["inventory"])
        lines = warning.splitlines()
        self.assertEqual(lines, [f"正在使用旧位置缓存 ~/{legacy_db.parent.name}/{legacy_db.name}；可用 --db 指定新位置"])
        command(existing_home, "memory", "init")
        self.assertTrue(legacy_db.with_name("memory.json").is_file())
        self.assertFalse(new_db.parent.exists())

        command(existing_home, "--db", new_db, "import", export)
        _, warning = command(existing_home, "status")
        self.assertEqual(warning, "")
        command(existing_home, "memory", "init")
        self.assertTrue(new_db.with_name("memory.json").is_file())
        self.assertTrue(legacy_db.is_file())
