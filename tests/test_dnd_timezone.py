"""隔离进程验证真实 TZ 行为，避免修改测试主进程或后台线程的全局时区。"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DndTimezoneTests(unittest.TestCase):
    def check_process(self, actual, expected):
        return subprocess.run([sys.executable, str(ROOT / "tests/check_container_timezone.py"), expected],
                              cwd=ROOT, env={**os.environ, "TZ": actual}, capture_output=True, text=True, timeout=30)

    def test_dnd_in_real_runtime_timezones(self):
        for zone in ["Asia/Shanghai", "UTC", "America/New_York"]:
            with self.subTest(zone=zone):
                result = self.check_process(zone, zone)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_detects_reported_utc_container_mismatch(self):
        result = self.check_process("UTC", "Asia/Shanghai")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("运行时区与预期不符", result.stderr)
