"""启动对齐失败、停机后重启与空库场景的回归；不发送真实通知。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

root = Path(__file__).resolve().parents[1]
source = root / "src"
if not source.is_dir():
    source = root / "cmd/fnmessagebots/src"
sys.path.insert(0, str(source))

from monitor.db_log_poller import DBLogPoller
from monitor.backup_db_poller import BackupDBPoller
from monitor.scheduler_db_poller import SchedulerDBPoller
from monitor import media_db_poller as media, photo_db_poller as photo, trim_activity_poller as activity


class NoHistoryOnStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "events.db"
        with sqlite3.connect(self.db) as writer:
            writer.execute("CREATE TABLE log (id INTEGER PRIMARY KEY, serviceId TEXT, uid INTEGER, uname TEXT, logtime INTEGER, loglevel INTEGER, eventId TEXT, parameter TEXT, category TEXT)")
            writer.execute("INSERT INTO log(id,eventId,parameter) VALUES(42,'LoginSucc','{}')")
        self.poller = DBLogPoller(str(self.db), self.temp.name, poll_interval=1)

    def insert_log(self, row_id):
        with sqlite3.connect(self.db) as writer:
            writer.execute("INSERT INTO log(id,eventId,parameter) VALUES(?,'LoginSucc','{}')", (row_id,))

    def test_failed_initial_query_retries_before_polling_and_skips_waiting_history(self):
        self.poller._cursor_file.write_text("4")
        handler = Mock()
        self.poller.add_handler("LoginSucc", handler)
        self.poller.running = True
        connection_count = 0
        ticks = 0

        def connect(*args, **kwargs):
            nonlocal connection_count
            connection_count += 1
            if connection_count == 1:
                raise sqlite3.OperationalError("database is locked")
            return sqlite3.connect(self.db)

        def tick(_seconds):
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                # 对齐失败时不轮询，不覆盖旧游标；等待期间的日志也视作存量。
                handler.assert_not_called()
                self.assertEqual(self.poller._cursor_file.read_text(), "4")
                self.insert_log(50)
            elif ticks == 2:
                handler.assert_not_called()
                self.assertEqual(self.poller._cursor_file.read_text(), "50")
                self.insert_log(51)
            else:
                self.poller.running = False

        with patch("monitor.db_log_poller.connect_readonly_with_fallback", side_effect=connect), \
             patch("monitor.db_log_poller.time.sleep", side_effect=tick):
            self.poller._run_loop()
        handler.assert_called_once()
        self.assertEqual(handler.call_args.args[0]["_source_cursor"], "51")
        self.assertEqual(self.poller._cursor_file.read_text(), "51")

    def test_empty_database_is_a_valid_zero_baseline(self):
        with sqlite3.connect(self.db) as writer:
            writer.execute("DELETE FROM log")
        self.assertEqual(self.poller._get_max_log_id(), 0)

    def test_failed_queries_close_connections_and_do_not_become_zero(self):
        for method in (self.poller._get_max_log_id, lambda: self.poller._fetch_new_rows(42)):
            with self.subTest(method=method):
                conn = Mock()
                conn.execute.side_effect = sqlite3.OperationalError("database is locked")
                with patch("monitor.db_log_poller.connect_readonly_with_fallback", return_value=conn):
                    result = method()
                self.assertIn(result, (None, []))
                conn.close.assert_called_once()

    def test_unavailable_database_never_polls_or_overwrites_cursor(self):
        self.poller._cursor_file.write_text("42")
        self.poller.running = True
        with patch.object(self.poller, "_get_max_log_id", return_value=None), \
             patch.object(self.poller, "_poll_once") as poll, \
             patch("monitor.db_log_poller.time.sleep", side_effect=lambda _: setattr(self.poller, "running", False)):
            self.poller._run_loop()
        poll.assert_not_called()
        self.assertEqual(self.poller._cursor_file.read_text(), "42")

    def test_backup_and_scheduler_retry_without_using_saved_cursor(self):
        cases = [(BackupDBPoller, {"last_finished_time": 100, "last_id": 42}),
                 (SchedulerDBPoller, {"last_finished_at": "2026-10-10 12:00:00", "last_id": 42})]
        for cls, latest in cases:
            with self.subTest(poller=cls.__name__):
                poller = object.__new__(cls)
                poller.running = True
                poller.poll_interval = 1
                poller.logger = Mock()
                poller._load_dedup = Mock()
                poller._read_cursor = Mock(return_value={"last_id": 1})
                poller._get_latest_watermark = Mock(side_effect=[None, latest])
                poller._write_cursor = Mock()

                def poll(cursor):
                    self.assertEqual(cursor, latest)
                    poller.running = False
                    return cursor

                poller._poll_once = Mock(side_effect=poll)

                def wait(_seconds):
                    poller._poll_once.assert_not_called()
                    poller._write_cursor.assert_not_called()

                with patch("time.sleep", side_effect=wait):
                    poller._run_loop()
                poller._poll_once.assert_called_once_with(latest)
                poller._read_cursor.assert_not_called()
                self.assertEqual(poller._get_latest_watermark.call_count, 2)

    def test_watermark_errors_are_distinct_from_empty_databases_and_close_connection(self):
        for cls in (BackupDBPoller, SchedulerDBPoller):
            for fail in (False, True):
                with self.subTest(poller=cls.__name__, fail=fail):
                    poller = object.__new__(cls)
                    poller.logger = Mock()
                    conn = Mock()
                    conn.execute.return_value.fetchone.return_value = None
                    if fail:
                        conn.execute.side_effect = sqlite3.OperationalError("database is locked")
                    poller._connect = Mock(return_value=conn)
                    result = poller._get_latest_watermark()
                    if fail:
                        self.assertIsNone(result)
                    else:
                        self.assertEqual(result["last_id"], 0)
                    conn.close.assert_called_once()

    def test_snapshot_pollers_discard_saved_baseline_and_retry_before_emitting(self):
        schema = """
        CREATE TABLE item(create_time INTEGER,update_time INTEGER,guid TEXT,fetch_status INTEGER);
        INSERT INTO item VALUES(100,100,'old-item',1);
        CREATE TABLE item_media(create_time INTEGER,update_time INTEGER);
        CREATE TABLE media_delete(update_time INTEGER);
        CREATE TABLE user_token(create_time INTEGER,update_time INTEGER,token TEXT,user_guid TEXT,ip TEXT,app_name TEXT,status INTEGER);
        INSERT INTO user_token VALUES(100,100,'old-token','user','192.168.1.1','lite.video',1);
        CREATE TABLE share_link(id INTEGER,share_id TEXT,valid_to INTEGER);
        INSERT INTO share_link VALUES(42,'expired',1);
        CREATE TABLE device(id INTEGER);
        CREATE TABLE face_task_log(id INTEGER);
        """
        with sqlite3.connect(self.db) as writer:
            writer.executescript(schema)
        cases = [(media.MediaDBPoller, media._TRIM_EVENTS),
                 (activity.TrimActivityPoller, activity._ACTIVITY_EVENTS),
                 (photo.PhotoDBPoller, photo.PHOTO_POLL_EVENTS)]
        for cls, events in cases:
            with self.subTest(poller=cls.__name__):
                kwargs = {"app_name_patterns": ["lite.video"]} if cls is activity.TrimActivityPoller else {}
                poller = cls(str(self.db), self.temp.name, poll_interval=1, monitor_events=list(events), **kwargs)
                state = poller._load_state()
                state["initialized"] = True
                if cls is photo.PhotoDBPoller:
                    state["face_pending"] = {"count": 3, "first_task_log_id": 1, "last_task_log_id": 3}
                poller._save_state(state)
                poller.running = True
                poller._emit = Mock()
                original_connect = poller._connect
                attempts = 0
                ticks = 0

                def connect():
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise sqlite3.OperationalError("unable to open database file")
                    return original_connect()

                def tick(_seconds):
                    nonlocal ticks
                    ticks += 1
                    poller._emit.assert_not_called()
                    if ticks == 2:
                        poller.running = False

                with patch.object(poller, "_connect", side_effect=connect), patch("time.sleep", side_effect=tick):
                    poller._run_loop()
                self.assertEqual(attempts, 2)
                self.assertTrue(poller._load_state()["initialized"])
                poller._emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
