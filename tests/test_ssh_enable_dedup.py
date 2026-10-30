"""SSH 启停去重：快速多次开关不应被同分钟指纹吞掉。"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))

from notifier.multi_platform_notifier import MultiPlatformNotifier


class SshEnableDedupTests(unittest.TestCase):
    def test_rapid_enable_disable_have_distinct_fingerprints(self):
        n = MultiPlatformNotifier.__new__(MultiPlatformNotifier)
        n.dedup_window = 300
        n.sent_events = {}
        n._dedup_lock = __import__("threading").RLock()

        fps = []
        for i, et in enumerate(
            ["SSH_ENABLED", "SSH_DISABLED", "SSH_ENABLED", "SSH_DISABLED"]
        ):
            ed = {"unit": "ssh", "_source": "ssh_journal", "_source_cursor": f"cursor-{i}"}
            fp = n._generate_fingerprint(et, ed)
            self.assertFalse(n._is_duplicate(fp), msg=f"第 {i+1} 次应可推送: {et}")
            n.sent_events[fp] = time.time()
            fps.append(fp)
        self.assertEqual(len(set(fps)), 4)

    def test_old_minute_bucket_would_collide_but_cursor_does_not(self):
        n = MultiPlatformNotifier.__new__(MultiPlatformNotifier)
        a = n._generate_fingerprint(
            "SSH_ENABLED", {"unit": "ssh", "_source_cursor": "c1"}
        )
        b = n._generate_fingerprint(
            "SSH_ENABLED", {"unit": "ssh", "_source_cursor": "c2"}
        )
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
