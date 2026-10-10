"""调度与特殊数据库路径回归，不执行真实巡检或发送消息。"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

root = Path(__file__).resolve().parents[1]
source = root / "src"
if not source.is_dir():
    source = root / "cmd/fnmessagebots/src"
sys.path.insert(0, str(source))

from monitor import nas_patrol as patrol
from monitor.sqlite_uri import connect_readonly_with_fallback, sqlite_readonly_immutable_uri, sqlite_readonly_uri
from utils import cron_util


class ReviewRegressionTests(unittest.TestCase):
    def test_database_paths_with_uri_delimiters_and_at_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ["@appdata/库 #1.db", "@appshare/library?backup.db", "@appdata/100%20.db"]:
                with self.subTest(name=name):
                    path = Path(directory) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with sqlite3.connect(path) as writer:
                        writer.execute("CREATE TABLE log(id INTEGER)")
                        writer.execute("INSERT INTO log VALUES (42)")
                    for uri in [sqlite_readonly_uri(str(path)), sqlite_readonly_immutable_uri(str(path))]:
                        self.assertIn("@app", uri)
                        reader = sqlite3.connect(uri, uri=True)
                        try:
                            self.assertEqual(reader.execute("SELECT id FROM log").fetchone()[0], 42)
                            with self.assertRaises(sqlite3.OperationalError):
                                reader.execute("INSERT INTO log VALUES (43)")
                        finally:
                            reader.close()
                    reader = connect_readonly_with_fallback(str(path), table_probe_sql="SELECT id FROM log")
                    reader.close()

    def worker_once(self, now, last_success, anchored=True):
        app = SimpleNamespace(running=True, notifier=object(), config=SimpleNamespace(
            nas_patrol_enabled=True, nas_patrol_cron="*/45 * * * *", cursor_dir="/unused-test-state"))
        state = {"patrol_anchor_done": anchored, "last_success_ts": last_success}
        logger = Mock()
        def stop(_seconds):
            app.running = False
        with patch.object(patrol.logging, "getLogger", return_value=logger), \
             patch.object(patrol.time, "time", return_value=now), \
             patch.object(patrol.time, "sleep", side_effect=stop), \
             patch.object(patrol, "_state_path", return_value=Path("/unused-test-state.json")), \
             patch.object(patrol, "_load_state", return_value=state), \
             patch.object(patrol, "_save_state"), \
             patch.object(patrol, "_send_patrol_notification", return_value=True) as send:
            patrol.nas_patrol_worker_loop(app)
        return logger, send

    def test_cron_step_uses_hour_boundary_not_elapsed_interval(self):
        last = datetime(2026, 10, 10, 10, 45, 30).timestamp()
        now = datetime(2026, 10, 10, 10, 55).timestamp()
        logger, send = self.worker_once(now, last)
        due_calls = [call for call in logger.info.call_args_list if call.args[0].startswith("NAS 巡检下次触发")]
        self.assertEqual(due_calls[0].args[1], "2026-10-10 11:00:00")
        send.assert_not_called()

    def test_cron_step_is_due_at_zero_minutes_even_after_45_minute_run(self):
        last = datetime(2026, 10, 10, 10, 45, 30).timestamp()
        now = datetime(2026, 10, 10, 11).timestamp()
        _, send = self.worker_once(now, last)
        send.assert_called_once()

    def test_first_cron_cycle_reports_next_boundary(self):
        now = datetime(2026, 10, 10, 10, 55).timestamp()
        logger, send = self.worker_once(now, 0.0, anchored=False)
        messages = [str(call.args[0]) for call in logger.info.call_args_list]
        self.assertTrue(any("5.0 分钟后" in msg and "2026-10-10 11:00:00" in msg for msg in messages))
        send.assert_not_called()

    def test_fallback_weekday_range_accepts_sunday_as_seven(self):
        with patch.object(cron_util, "croniter", None):
            base = datetime(2026, 10, 10, 12).timestamp()
            result = cron_util.next_cron_timestamp("0 12 * * 1-7", base)
        self.assertEqual(datetime.fromtimestamp(result), datetime(2026, 10, 11, 12))

    @unittest.skipIf(cron_util.croniter is None, "命名星期需要 croniter")
    def test_supported_named_weekday_does_not_fail_in_fallback_parser(self):
        base = datetime(2026, 10, 10, 12).timestamp()
        result = cron_util.next_cron_timestamp("0 12 * * MON", base)
        self.assertEqual(datetime.fromtimestamp(result), datetime(2026, 10, 12, 12))


if __name__ == "__main__":
    unittest.main()
