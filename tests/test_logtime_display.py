"""Unix 时间戳只转换时区一次；保留旧系统显式配置的存库偏差补偿。"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from monitor.db_log_poller import _logtime_to_datetime
from utils.logtime_display import get_logtime_display_offset_seconds


class LogtimeDisplayTests(unittest.TestCase):
    # InstaBox 真机 2026-11-05 的标准 Unix 时间戳，对应北京时间 11:54:50。
    timestamp = 1793850890

    def test_standard_unix_timestamp_has_no_default_compensation(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_logtime_display_offset_seconds(), 0)
            self.assertEqual(_logtime_to_datetime(self.timestamp), '2026-11-05 11:54:50')

    def test_invalid_compensation_falls_back_to_standard_unix_time(self):
        for raw in ('', 'invalid', '28800.0'):
            with self.subTest(raw=raw), patch.dict(os.environ, {'LOGTIME_DISPLAY_OFFSET_SECONDS': raw}):
                self.assertEqual(get_logtime_display_offset_seconds(), 0)
                self.assertEqual(_logtime_to_datetime(self.timestamp), '2026-11-05 11:54:50')

    def test_explicit_legacy_compensation_remains_supported(self):
        for raw, expected in (
            ('0', '2026-11-05 11:54:50'),
            ('28800', '2026-11-05 19:54:50'),
            ('-28800', '2026-11-05 03:54:50'),
        ):
            with self.subTest(raw=raw), patch.dict(os.environ, {'LOGTIME_DISPLAY_OFFSET_SECONDS': raw}):
                self.assertEqual(get_logtime_display_offset_seconds(), int(raw))
                self.assertEqual(_logtime_to_datetime(self.timestamp), expected)
