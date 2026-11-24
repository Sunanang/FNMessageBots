"""主日志库时间：优先 parameter.datetime；否则用 logtime，并可按库内两字段差值校准。"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from monitor.db_log_poller import _logtime_to_datetime
from utils import logtime_display
from utils.logtime_display import (
    format_logger_row_time,
    get_logtime_display_offset_seconds,
    note_logtime_datetime_pair,
)


class LogtimeDisplayTests(unittest.TestCase):
    # InstaBox 真机 2026-11-05 的标准 Unix 时间戳，对应北京时间 11:54:50。
    timestamp = 1793850890

    def setUp(self):
        logtime_display._learned_logtime_skew_seconds = None

    def test_standard_unix_timestamp_has_no_default_compensation(self):
        with patch.dict(os.environ, {'TZ': 'Asia/Shanghai'}, clear=True):
            self.assertEqual(get_logtime_display_offset_seconds(), 0)
            self.assertEqual(_logtime_to_datetime(self.timestamp), '2026-11-05 11:54:50')

    def test_invalid_compensation_falls_back_to_standard_unix_time(self):
        for raw in ('', 'invalid', '28800.0'):
            with self.subTest(raw=raw), patch.dict(os.environ, {'TZ': 'Asia/Shanghai', 'LOGTIME_DISPLAY_OFFSET_SECONDS': raw}):
                logtime_display._learned_logtime_skew_seconds = None
                self.assertEqual(get_logtime_display_offset_seconds(), 0)
                self.assertEqual(_logtime_to_datetime(self.timestamp), '2026-11-05 11:54:50')

    def test_explicit_legacy_compensation_remains_supported(self):
        for raw, expected in (
            ('0', '2026-11-05 11:54:50'),
            ('28800', '2026-11-05 19:54:50'),
            ('-28800', '2026-11-05 03:54:50'),
        ):
            with self.subTest(raw=raw), patch.dict(
                os.environ,
                {'TZ': 'Asia/Shanghai', 'LOGTIME_DISPLAY_OFFSET_SECONDS': raw},
            ):
                logtime_display._learned_logtime_skew_seconds = None
                self.assertEqual(get_logtime_display_offset_seconds(), int(raw))
                self.assertEqual(_logtime_to_datetime(self.timestamp), expected)

    def test_prefer_parameter_datetime_from_database(self):
        # Lando-Nas：同行列差值恰为 28800，由数据库算出，不是代码写死
        logtime = 1795474357
        param_dt = 1795503157
        with patch.dict(os.environ, {'TZ': 'Asia/Shanghai'}, clear=True):
            text = format_logger_row_time(logtime, {"datetime": param_dt, "eventId": "SshdLoginSucc"})
            self.assertEqual(text, '2026-11-24 14:52:37')
            self.assertEqual(get_logtime_display_offset_seconds(), 28800)

    def test_learned_skew_applies_to_logtime_only_rows(self):
        with patch.dict(os.environ, {'TZ': 'Asia/Shanghai'}, clear=True):
            note_logtime_datetime_pair(1795474357, 1795503157)
            self.assertEqual(
                format_logger_row_time(1795472596, {"cat": 3, "template": "LoginSucc"}),
                '2026-11-24 14:23:16',
            )

    def test_display_follows_tz_env(self):
        ts = 1795503157  # 2026-11-24 06:52:37 UTC
        with patch.dict(os.environ, {'TZ': 'UTC'}, clear=True):
            self.assertEqual(format_logger_row_time(0, {"datetime": ts}), '2026-11-24 06:52:37')
        with patch.dict(os.environ, {'TZ': 'Asia/Shanghai'}, clear=True):
            self.assertEqual(format_logger_row_time(0, {"datetime": ts}), '2026-11-24 14:52:37')
