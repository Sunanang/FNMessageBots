"""推送记录时间与「今日」统计必须跟随 TZ，不能写死北京时间。"""
import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from utils import push_history
from utils.logtime_display import display_now_str, get_display_tz


class PushHistoryTimezoneTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name
        for manager in (
            patch.dict(os.environ, {"TZ": "Europe/Berlin"}),
            patch("utils.logtime_display._tz_applied", False),
        ):
            manager.start()
            self.addCleanup(manager.stop)
        push_history.init(self.dir)

    def test_created_at_follows_display_timezone(self):
        push_history.add_record(True, "APP_STARTED", "测试")
        rows = push_history.get_records(limit=1)
        self.assertEqual(len(rows), 1)
        expected = display_now_str()[:13]  # 精确到小时即可，避开跨秒抖动
        self.assertEqual(rows[0]["created_at"][:13], expected,
                         "推送记录时间应与通知正文用同一个展示时区")

    def test_today_counts_uses_display_timezone(self):
        push_history.add_record(True, "APP_STARTED", "今天")
        today = datetime.now(get_display_tz()).strftime("%Y-%m-%d")
        push_history.bulk_insert([
            {"created_at": f"{today} 00:00:01", "event_type": "X", "success": True, "summary": "今天更早"},
        ])
        counts = push_history.get_today_counts()
        self.assertEqual(counts["total"], 2, counts)
        self.assertEqual(counts["success"], 2, counts)

    def test_today_counts_excludes_other_days(self):
        push_history.bulk_insert([
            {"created_at": "2000-01-01 10:00:00", "event_type": "X", "success": False, "summary": "很久以前"},
        ])
        counts = push_history.get_today_counts()
        self.assertEqual(counts["total"], 0, counts)

    def test_today_counts_query_uses_range_scan(self):
        """substr(created_at,1,10)=? 用不上索引，这里锁住「必须用范围比较」。"""
        import inspect
        source = inspect.getsource(push_history.get_today_counts)
        self.assertNotIn("substr(", source)
        self.assertIn("created_at >= ?", source)


class OutboxCapacityTests(unittest.TestCase):
    """全渠道长期失败时，待发队列不能无限堆积。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name

    def test_queue_is_capped_and_drops_oldest(self):
        from monitor.models import JournalEntry
        from utils import notification_outbox

        outbox = notification_outbox.NotificationOutbox(self.dir)
        cap = notification_outbox.MAX_POLL_EVENTS
        entry = JournalEntry(
            cursor="", timestamp="", hostname="", syslog_identifier="",
            message="m", priority=0, pid=0, raw_data="{}", original_line="",
        )
        items = [
            {"event_type": "APP_STARTED", "event_data": {"_source_cursor": str(i)}, "entry": entry}
            for i in range(cap + 5)
        ]
        with patch.object(notification_outbox, "MAX_POLL_EVENTS", 100):
            outbox.enqueue_poll(items)
            remaining = outbox.poll_items()
        self.assertEqual(len(remaining), 100, "超出上限时应裁剪到上限")
        kept = {item["event_data"]["_source_cursor"] for item in remaining}
        self.assertNotIn("0", kept, "应丢弃最老的待发项")
        self.assertIn(str(cap + 4), kept, "最新的待发项必须保留")


if __name__ == "__main__":
    unittest.main()
