"""回归：水位失败不推进、历史/测试脱敏、immutable 策略、魔法推送路径。"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from monitor.backup_db_poller import BACKUP_FAILED_EVENT, BackupDBPoller
from monitor.sqlite_uri import connect_readonly_with_fallback
from notifier.connection_pool import ConnectionPool
from utils import push_history
from utils.secret_redact import redact_secrets
from web.config_store import mask_channels


TG_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
TG_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
MAGIC_TOKEN = "mp_live_super_secret_token_xyz"
MAGIC_URL = f"https://push.example.com/api/push/{MAGIC_TOKEN}?key=abc"


class SecretRedactFixesTests(unittest.TestCase):
    def test_redact_before_truncate_keeps_no_partial_token(self):
        long = "x" * 50 + TG_URL + "y" * 50
        preview = ConnectionPool._safe_preview(long, TG_URL)
        self.assertNotIn(TG_TOKEN, preview)
        self.assertNotIn(TG_TOKEN[:20], preview)

    def test_encoded_telegram_and_magic_path(self):
        encoded = TG_URL.replace(":", "%3A")
        self.assertNotIn(TG_TOKEN, redact_secrets(encoded))
        self.assertNotIn(MAGIC_TOKEN, redact_secrets(MAGIC_URL))
        self.assertNotIn(MAGIC_TOKEN, ConnectionPool._safe_url(MAGIC_URL))

    def test_config_masks_magic_push_path_token(self):
        stored = [{"type": "webhook", "url": MAGIC_URL, "enabled": True}]
        displayed = mask_channels(stored)
        self.assertNotIn(MAGIC_TOKEN, str(displayed))
        self.assertIn("******", displayed[0]["url"])

    def test_push_history_redacts_on_write_and_read(self):
        with tempfile.TemporaryDirectory() as directory:
            push_history.init(directory)
            push_history.add_record(
                False,
                "TEST",
                summary=f"fail {TG_URL}",
                detail={
                    "failure_summary": MAGIC_URL,
                    "channel_results": [{"channel": "tg", "success": False, "error": TG_URL}],
                },
            )
            rows = push_history.get_records(limit=1)
            self.assertEqual(len(rows), 1)
            blob = json.dumps(rows[0], ensure_ascii=False)
            self.assertNotIn(TG_TOKEN, blob)
            self.assertNotIn(MAGIC_TOKEN, blob)

    def test_bare_telegram_token_without_bot_prefix(self):
        """历史/错误摘要里常只有裸 token，无 /bot 路径。"""
        self.assertNotIn(TG_TOKEN, redact_secrets(f"e {TG_TOKEN}"))
        self.assertNotIn(TG_TOKEN, redact_secrets(TG_TOKEN))
        with tempfile.TemporaryDirectory() as directory:
            push_history.init(directory)
            push_history.add_record(
                False,
                "TEST",
                summary=f"e {TG_TOKEN}",
                detail={"channel_results": [{"channel": "tg", "success": False, "error": TG_TOKEN}]},
            )
            blob = json.dumps(push_history.get_records(limit=1)[0], ensure_ascii=False)
            self.assertNotIn(TG_TOKEN, blob)


class BackupWatermarkOnFailureTests(unittest.TestCase):
    def test_handler_failure_does_not_advance_past_failed_row(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "basic_backup.db3"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE t(x INTEGER)")
                conn.commit()
            cursor_dir = Path(directory) / "cursor"
            cursor_dir.mkdir()
            poller = BackupDBPoller(
                str(db),
                str(cursor_dir),
                poll_interval=1,
                monitor_events=[BACKUP_FAILED_EVENT],
            )
            rows = [
                {
                    "id": 2,
                    "uid": 1,
                    "task_id": 9,
                    "status": 4,
                    "error_code": 1,
                    "error_message": "boom",
                    "start_time": 2000,
                    "finished_time": 2000,
                    "actual_count": 0,
                    "task_name": "t2",
                }
            ]
            calls = []

            def boom(event_data, entry):
                calls.append(int(entry.cursor))
                raise RuntimeError("delivery failed")

            poller.event_handlers[BACKUP_FAILED_EVENT] = boom
            with patch.object(poller, "_fetch_rows_with_lookback", return_value=rows):
                next_cursor = poller._poll_once({"last_finished_time": 0, "last_id": 0})
            self.assertEqual(calls, [2])
            self.assertEqual(next_cursor["last_id"], 0)
            self.assertEqual(next_cursor["last_finished_time"], 0)


class ImmutablePolicyTests(unittest.TestCase):
    def test_allow_immutable_false_connects_ro(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE t(id INTEGER)")
                conn.commit()
            conn = connect_readonly_with_fallback(str(path), allow_immutable=False)
            try:
                row = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()
                self.assertEqual(row[0], "t")
            finally:
                conn.close()


class PhotoFacePendingPersistTests(unittest.TestCase):
    def test_face_pending_survives_reload(self):
        from monitor.photo_db_poller import PhotoDBPoller

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "photo.db"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE t(x INTEGER)")
                conn.commit()
            poller = PhotoDBPoller(str(db), directory, poll_interval=1, monitor_events=[])
            state = poller._load_state()
            state["initialized"] = True
            state["face_pending"] = {
                "count": 2,
                "first_task_log_id": 10,
                "last_task_log_id": 12,
                "photo_ids": [1, 2],
                "user_ids": [7],
                "user_photo_ids": [],
            }
            poller._save_state(state)
            restored = PhotoDBPoller(str(db), directory, poll_interval=1, monitor_events=[])
            pending = restored._load_state()["face_pending"]
            self.assertEqual(pending["count"], 2)
            self.assertEqual(pending["last_task_log_id"], 12)
            self.assertEqual(pending["photo_ids"], [1, 2])


if __name__ == "__main__":
    unittest.main()
