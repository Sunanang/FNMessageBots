"""库内无 parameter.datetime 时，用轮询连续期间的新行兜底学习 logtime 差值。"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from monitor.db_log_poller import DBLogPoller  # noqa: E402
from utils import logtime_display  # noqa: E402
from utils.logtime_display import (  # noqa: E402
    format_logger_row_time,
    observe_live_row_wall_clock,
)

NAS_SKEW = 28800


def _reset_logtime_state():
    # 模块级状态必须还原，否则会泄漏到同一进程里的其它测试（如 DND 用例依赖 offset=0）
    logtime_display._learned_logtime_skew_seconds = None
    logtime_display._skew_state_file = None


class WallClockFallbackUnitTests(unittest.TestCase):
    def setUp(self):
        _reset_logtime_state()

    def tearDown(self):
        _reset_logtime_state()

    def test_fresh_row_with_eight_hour_lag_is_learned(self):
        now = 1_800_000_000
        with patch.dict(os.environ, {'TZ': 'Asia/Shanghai'}, clear=True):
            observe_live_row_wall_clock(now - NAS_SKEW + 3, {'user': 'lando'}, now=now)
            self.assertEqual(logtime_display._learned_logtime_skew_seconds, NAS_SKEW)
            self.assertEqual(format_logger_row_time(now - NAS_SKEW, {'user': 'lando'}),
                             logtime_display.format_unix_seconds_for_display(now))

    def test_no_skew_is_not_learned(self):
        now = 1_800_000_000
        observe_live_row_wall_clock(now - 5, {'user': 'lando'}, now=now)
        self.assertIsNone(logtime_display._learned_logtime_skew_seconds)

    def test_differences_within_one_hour_are_ignored(self):
        now = 1_800_000_000
        observe_live_row_wall_clock(now - 3600, {'user': 'lando'}, now=now)
        observe_live_row_wall_clock(now + 1200, {'user': 'lando'}, now=now)
        self.assertIsNone(logtime_display._learned_logtime_skew_seconds)

    def test_estimate_is_quantized_to_quarter_hour(self):
        now = 1_800_000_000
        observe_live_row_wall_clock(now - (NAS_SKEW + 7), {}, now=now)
        self.assertEqual(logtime_display._learned_logtime_skew_seconds, NAS_SKEW)

    def test_row_with_datetime_is_left_to_datetime_path(self):
        now = 1_800_000_000
        observe_live_row_wall_clock(now - NAS_SKEW, {'datetime': now}, now=now)
        self.assertIsNone(logtime_display._learned_logtime_skew_seconds)

    def test_existing_learned_skew_is_not_replaced_by_wall_clock(self):
        now = 1_800_000_000
        logtime_display._learned_logtime_skew_seconds = 0
        observe_live_row_wall_clock(now - NAS_SKEW, {}, now=now)
        self.assertEqual(logtime_display._learned_logtime_skew_seconds, 0)


class SkewDescriptionTests(unittest.TestCase):
    def setUp(self):
        _reset_logtime_state()

    def tearDown(self):
        _reset_logtime_state()

    def test_unknown_skew_is_described_as_pending_not_zero(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(logtime_display.is_logtime_skew_known())
            self.assertIn('待校准', logtime_display.describe_logtime_skew())

    def test_learned_and_env_skew_report_their_source(self):
        with patch.dict(os.environ, {}, clear=True):
            logtime_display._learned_logtime_skew_seconds = NAS_SKEW
            self.assertTrue(logtime_display.is_logtime_skew_known())
            self.assertIn('+28800', logtime_display.describe_logtime_skew())
            self.assertIn('缓存', logtime_display.describe_logtime_skew())
        with patch.dict(os.environ, {'LOGTIME_DISPLAY_OFFSET_SECONDS': '0'}, clear=True):
            self.assertTrue(logtime_display.is_logtime_skew_known())
            self.assertIn('环境变量', logtime_display.describe_logtime_skew())

    def test_cache_loaded_before_banner_is_reported(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            (Path(temp) / 'logtime_skew.json').write_text('{"skew_seconds": 28800}', encoding='utf-8')
            logtime_display.set_logtime_skew_state_path(Path(temp) / 'logtime_skew.json')
            self.assertIn('+28800', logtime_display.describe_logtime_skew())


class WallClockFallbackPollerTests(unittest.TestCase):
    def setUp(self):
        _reset_logtime_state()
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'logger.db3')
        self.cursor_dir = str(Path(self.temp.name) / 'cursor')
        conn = sqlite3.connect(self.db)
        conn.execute(
            'CREATE TABLE log (id INTEGER PRIMARY KEY, serviceId TEXT, uid INT, uname TEXT, logtime INT,'
            ' loglevel INT, eventId TEXT, parameter TEXT, category TEXT)'
        )
        conn.execute("INSERT INTO log VALUES (1,'sys',0,'lando',?,0,'LoginSucc','{}','sys')", (int(time.time()) - 86400,))
        conn.commit()
        conn.close()
        self.env = patch.dict(os.environ, {'TZ': 'Asia/Shanghai'}, clear=True)
        self.env.start()
        self.captured = []
        self.poller = DBLogPoller(db_path=self.db, cursor_dir=self.cursor_dir)
        self.poller.add_handler('LoginSucc', lambda data, entry: self.captured.append(entry.timestamp))
        self.last_id = self.poller._get_max_log_id()
        self.poller._seed_logtime_skew_from_db()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()
        _reset_logtime_state()

    def _insert_login(self, row_id, logtime):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO log VALUES (?,'sys',0,'lando',?,0,'LoginSucc',?,'sys')",
                     (row_id, logtime, '{"user": "lando", "from": "10.0.0.9"}'))
        conn.commit()
        conn.close()

    def test_steady_polling_learns_skew_from_new_login(self):
        self.last_id = self.poller._poll_once(self.last_id)  # 预热：对齐后的第一轮
        self._insert_login(2, int(time.time()) - NAS_SKEW)
        self.last_id = self.poller._poll_once(self.last_id)
        self.assertEqual(logtime_display._learned_logtime_skew_seconds, NAS_SKEW)
        shown = datetime.strptime(self.captured[-1], '%Y-%m-%d %H:%M:%S')
        expected = datetime.fromtimestamp(time.time(), tz=ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
        self.assertLessEqual(abs((shown - expected).total_seconds()), 5)

    def test_backlog_after_long_gap_is_not_used_for_learning(self):
        self.last_id = self.poller._poll_once(self.last_id)
        self.poller._last_poll_wall = time.time() - 3600  # 模拟停机 / 轮询断档后补读积压行
        self._insert_login(2, int(time.time()) - NAS_SKEW)
        self.last_id = self.poller._poll_once(self.last_id)
        self.assertIsNone(logtime_display._learned_logtime_skew_seconds)

    def test_seed_does_not_warn_when_skew_loaded_from_cache(self):
        logtime_display._learned_logtime_skew_seconds = NAS_SKEW  # 模拟已从 logtime_skew.json 加载
        with self.assertLogs('monitor.db_log_poller', level='WARNING') as captured:
            self.poller.logger.warning('sentinel')  # assertLogs 要求至少一条
            self.poller._seed_logtime_skew_from_db()
        self.assertEqual([r.getMessage() for r in captured.records], ['sentinel'])

    def test_seed_warns_when_nothing_is_known(self):
        with self.assertLogs('monitor.db_log_poller', level='WARNING') as captured:
            self.poller._seed_logtime_skew_from_db()
        self.assertTrue(any('未见可用的 parameter.datetime' in r.getMessage() for r in captured.records))

    def test_first_poll_after_alignment_does_not_learn(self):
        self._insert_login(2, int(time.time()) - NAS_SKEW)
        self.poller._poll_once(self.last_id)
        self.assertIsNone(logtime_display._learned_logtime_skew_seconds)


if __name__ == '__main__':
    unittest.main()
