"""推送时间一致性：所有事件的展示时间必须走同一个时区出口（跟随 TZ）。

历史问题：登录/应用这类事件走 logtime_display（跟随 TZ），而影视/音乐/照片库
事件写死 Asia/Shanghai，非中国时区部署时同一条推送里时间会差 6~7 小时。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from utils.logtime_display import display_now_str, format_unix_seconds_for_display, get_display_tz

TS = 1_700_000_000  # 真实 Unix 秒


class DisplayTimezoneConsistencyTests(unittest.TestCase):
    def setUp(self):
        for manager in (
            patch.dict(os.environ, {"TZ": "Europe/Berlin"}),
            patch("utils.logtime_display._tz_applied", False),
        ):
            manager.start()
            self.addCleanup(manager.stop)

    def test_pollers_use_display_timezone(self):
        from monitor import media_db_poller, music_db_poller, photo_db_poller

        expected = format_unix_seconds_for_display(TS)
        self.assertEqual(music_db_poller._ts_to_local_str(TS), expected,
                         "音乐库事件时间没有跟随 TZ")
        self.assertEqual(media_db_poller._ms_to_str(TS * 1000), expected,
                         "影视库事件时间没有跟随 TZ")
        self.assertEqual(photo_db_poller._sec_to_local_str(TS), expected,
                         "照片库事件时间没有跟随 TZ")

    def test_pollers_fallback_uses_display_now(self):
        from monitor import media_db_poller, music_db_poller, photo_db_poller

        now_prefix = display_now_str()[:13]
        self.assertEqual(music_db_poller._ts_to_local_str(None)[:13], now_prefix)
        self.assertEqual(media_db_poller._ms_to_str(None)[:13], now_prefix)
        # 非法值也回落到当前展示时间，而不是报错或返回北京时间
        self.assertEqual(music_db_poller._ts_to_local_str("bad")[:13], now_prefix)

    def test_push_stats_today_key_follows_display_timezone(self):
        from utils import push_stats

        self.assertEqual(push_stats._today_key(), display_now_str()[:10],
                         "今日推送统计的日期没有跟随 TZ")

    def test_display_timezone_differs_from_beijing(self):
        """锁住前提：这台机器上 Berlin 与北京确实不同，用例才有意义。"""
        self.assertEqual(str(get_display_tz()), "Europe/Berlin")
        self.assertNotEqual(format_unix_seconds_for_display(TS),
                            "2023-11-15 06:13:20",
                            "展示时间仍是北京时区，说明没走 display 出口")

    def test_no_hardcoded_beijing_timezone_in_src(self):
        """源码里不该再有写死的 Asia/Shanghai；展示时区只能来自 TZ。"""
        root = Path(__file__).resolve().parents[1] / "src"
        offenders = []
        for path in root.rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "Asia/Shanghai" in text:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f"这些文件还在写死北京时间：{offenders}")


if __name__ == "__main__":
    unittest.main()
