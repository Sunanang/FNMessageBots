"""去重链路：并发占位、失败可重试、批次指纹覆盖全部条目。"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notifier.multi_platform_notifier import MultiPlatformNotifier


class FingerprintReserveTests(unittest.TestCase):
    def setUp(self):
        self.notifier = MultiPlatformNotifier(wechat_webhook_url="https://example.invalid/hook", dedup_window=300)
        self.addCleanup(self.notifier.close)

    def test_concurrent_reserve_only_one_wins(self):
        """多线程同时处理同一条事件，只能有一个线程拿到发送权。"""
        fingerprint = self.notifier._generate_fingerprint(
            "APP_STARTED", {"_source": "logger_db", "_source_cursor": "42"}
        )
        barrier = threading.Barrier(8)
        winners = []

        def worker():
            barrier.wait()
            if self.notifier._reserve_fingerprint(fingerprint):
                winners.append(threading.current_thread().name)

        threads = [threading.Thread(target=worker, name=f"t{i}") for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(winners), 1, f"并发占位应只有一个赢家，实际 {winners}")

    def test_sent_fingerprint_blocks_then_window_expires(self):
        self.assertTrue(self.notifier._reserve_fingerprint("fp-1"))
        self.notifier._mark_fingerprint_sent("fp-1")
        self.assertFalse(self.notifier._reserve_fingerprint("fp-1"), "窗口内应判重")

        with patch("notifier.multi_platform_notifier.time.time", return_value=time.time() + 400):
            self.assertTrue(self.notifier._reserve_fingerprint("fp-1"), "窗口过期后应可再发")

    def test_release_allows_retry_after_failure(self):
        self.assertTrue(self.notifier._reserve_fingerprint("fp-2"))
        self.notifier._release_fingerprint("fp-2")
        self.assertTrue(self.notifier._reserve_fingerprint("fp-2"), "发送失败后必须允许重试")

    def test_stale_inflight_self_heals(self):
        """占位没被释放（例如发送线程异常退出）时，超时后应能自愈，不能永久堵死。"""
        with patch("notifier.multi_platform_notifier.time.time", return_value=1000.0):
            self.assertTrue(self.notifier._reserve_fingerprint("fp-3"))
        with patch("notifier.multi_platform_notifier.time.time", return_value=1000.0 + 121):
            self.assertTrue(self.notifier._reserve_fingerprint("fp-3"))

    def test_empty_fingerprint_never_blocks(self):
        self.assertTrue(self.notifier._reserve_fingerprint(""))
        self.assertTrue(self.notifier._reserve_fingerprint(""))

    def test_batch_fingerprint_covers_items_beyond_first_five(self):
        """两个批次只有第 6 条不同，也必须算两个不同的批次（否则会漏推）。"""
        def payload(extra_event_type):
            items = [
                {"event_type": "A", "timestamp": f"t{i}", "brief": f"b{i}"}
                for i in range(6)
            ]
            items.append({"event_type": extra_event_type, "timestamp": "tX", "brief": "bx"})
            return {"count": 7, "by_type": {"A": 7}, "items": items}

        first = self.notifier._generate_fingerprint("POLL_BATCH_SUMMARY", payload("B"))
        second = self.notifier._generate_fingerprint("POLL_BATCH_SUMMARY", payload("C"))
        self.assertNotEqual(first, second, "第 6 条之后内容不同，指纹必须不同")

    def test_duplicate_batch_same_payload_merges(self):
        payload = {
            "count": 3,
            "by_type": {"A": 3},
            "items": [{"event_type": "A", "timestamp": f"t{i}", "brief": "b"} for i in range(3)],
        }
        first = self.notifier._generate_fingerprint("POLL_BATCH_SUMMARY", payload)
        second = self.notifier._generate_fingerprint("POLL_BATCH_SUMMARY", dict(payload))
        self.assertEqual(first, second, "同一批内容应判为重复")


class SendPathDedupTests(unittest.TestCase):
    """端到端：失败要能重试，成功要在窗口内拦住。"""

    def setUp(self):
        self.notifier = MultiPlatformNotifier(wechat_webhook_url="https://example.invalid/hook", dedup_window=300)
        self.addCleanup(self.notifier.close)
        for manager in (patch("utils.push_history.add_record"),
                        patch("utils.push_stats.record")):
            manager.start()
            self.addCleanup(manager.stop)

    def test_failed_send_can_be_retried(self):
        fail = Mock(return_value=(False, [{"channel": "企业微信", "success": False}]))
        ok = Mock(return_value=(True, [{"channel": "企业微信", "success": True}]))
        event = {"_source": "logger_db", "_source_cursor": "9001"}

        self.notifier._send_to_wechat = fail
        first, _ = self.notifier.send_notification("APP_STARTED", event, "{}", "now")
        self.assertFalse(first)

        self.notifier._send_to_wechat = ok
        second, _ = self.notifier.send_notification("APP_STARTED", event, "{}", "now")
        self.assertTrue(second, "上一次全渠道失败后，同一事件应能重试成功")

        third, results = self.notifier.send_notification("APP_STARTED", event, "{}", "now")
        self.assertFalse(third)
        self.assertEqual(results[0]["skipped"], "duplicate")


if __name__ == "__main__":
    unittest.main()
