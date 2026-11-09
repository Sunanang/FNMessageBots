"""2026-11-09 深度审计发现的确定性 bug 的回归测试。

不访问 NAS、不发通知。注意：主日志轮询「启动即对齐、不补发停机期间历史」是既有设计
（见 tests/test_no_history_on_start.py），不属于缺陷，这里不再断言游标沿用行为。
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from notifier.multi_platform_notifier import (
    MultiPlatformMessage,
    MultiPlatformNotifier,
    WECOM_TEXT_MAX_BYTES,
)
from notifier.unified_notifier import UnifiedNotifier


class WecomByteLimitTests(unittest.TestCase):
    """企业微信群机器人文本上限 2048 字节：超限是整条拒收，必须按字节截断。"""

    def test_wechat_payload_stays_within_byte_limit(self):
        long_text = "磁盘" * 1500  # 3000 个汉字 ≈ 9000 字节
        message = MultiPlatformMessage(title="汇总", content=long_text)
        payload = message.to_wechat_format()
        content = payload["text"]["content"]
        self.assertLessEqual(len(content.encode("utf-8")), WECOM_TEXT_MAX_BYTES)
        self.assertTrue(content.strip(), "截断后不应为空")

    def test_dingtalk_and_feishu_keep_full_text(self):
        """钉钉/飞书上限远大于此，不该被企业微信的限制砍掉。"""
        long_text = "磁盘" * 1500
        message = MultiPlatformMessage(title="汇总", content=long_text)
        self.assertGreater(len(message.to_dingtalk_format()["text"]["content"]), 1000)
        self.assertGreater(len(message.to_feishu_format()["content"]["text"]), 1000)


class DndSameTimeTests(unittest.TestCase):
    """勿扰起止时间相同，过去会被判成全天静音且永不汇总。"""

    def notifier(self):
        class Cfg:
            dnd_enabled = True
            dnd_start_time = "08:00"
            dnd_end_time = "08:00"
            notification_events = []
            push_history_enabled = False

            def __getattr__(self, name):  # 其余配置项按未配置处理
                return None

        return UnifiedNotifier(Cfg())

    def test_same_start_and_end_is_not_all_day(self):
        n = self.notifier()
        self.assertFalse(n._in_dnd_window())

    def test_normal_window_still_works(self):
        n = self.notifier()
        n.config.dnd_start_time, n.config.dnd_end_time = "00:00", "23:59"
        self.assertTrue(n._in_dnd_window())


if __name__ == "__main__":
    unittest.main()
