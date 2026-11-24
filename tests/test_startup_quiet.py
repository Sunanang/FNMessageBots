"""启动静默期：窗口折叠、白名单放行、只在开机首启生效、不重复补发汇总。"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notifier.multi_platform_notifier import MultiPlatformNotifier
from notifier.unified_notifier import UnifiedNotifier
from utils.startup_quiet import (
    DEFAULT_SECONDS,
    MAX_SECONDS,
    MIN_SECONDS,
    StartupQuietWindow,
    clamp_seconds,
)


class StartupQuietWindowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.now = 1_700_000_000.0
        self.uptime = 5.0  # 开机 5 秒，属于「开机首启」

    def window(self, **config):
        params = {"enabled": True, "seconds": 180, "keep_urgent": True, "only_after_boot": True}
        params.update(config)
        window = StartupQuietWindow(
            self.dir,
            clock=lambda: self.now,
            uptime_reader=lambda: self.uptime,
        )
        window.configure(**params)
        return window

    def test_disabled_window_never_suppresses(self):
        window = self.window(enabled=False)
        self.assertFalse(window.should_suppress("APP_STARTED"))
        self.assertFalse(window.in_window())
        self.assertIsNone(window.take_summary())

    def test_window_folds_events_and_summarizes_once(self):
        window = self.window()
        for _ in range(18):
            self.assertTrue(window.should_suppress("APP_STARTED"))
        for _ in range(6):
            self.assertTrue(window.should_suppress("DOCKER_CONTAINER_START"))
        for _ in range(3):
            self.assertTrue(window.should_suppress("DiskWakeup"))
        self.assertTrue(window.in_window())
        self.assertIsNone(window.take_summary(), "窗口结束前不该发汇总")

        self.now += 200  # 窗口结束
        summary = window.take_summary()
        self.assertIsNotNone(summary)
        self.assertIn("折叠 27 条事件", summary)
        self.assertIn("应用启动 18", summary)
        self.assertIn("容器 6", summary)
        self.assertIn("磁盘存储 3", summary)
        self.assertIsNone(window.take_summary(), "同一次静默期只补发一条汇总")

    def test_urgent_events_keep_pushing(self):
        window = self.window()
        for event in ("LoginFail", "SSH_AUTH_FAILED", "UPS_ONBATT", "DISK_IO_ERR", "APP_CRASH"):
            self.assertFalse(window.should_suppress(event), event)
        self.assertIsNone(window.take_summary(), "白名单事件不该走进折叠计数")

    def test_urgent_whitelist_can_be_turned_off(self):
        window = self.window(keep_urgent=False)
        self.assertTrue(window.should_suppress("LoginFail"))
        self.now += 200
        self.assertIn("折叠 1 条事件", window.take_summary() or "")

    def test_manual_restart_after_boot_is_not_quiet(self):
        self.uptime = 3600.0  # 开机一小时了，只是手动重启应用
        window = self.window()
        self.assertFalse(window.in_window())
        self.assertFalse(window.should_suppress("APP_STARTED"))
        self.assertEqual(window.status()["inactive_reason"], "not_boot_launch")

    def test_only_after_boot_off_quietens_manual_restart_too(self):
        self.uptime = 3600.0
        window = self.window(only_after_boot=False)
        self.assertTrue(window.in_window())
        self.assertTrue(window.should_suppress("APP_STARTED"))

    def test_restart_inside_same_boot_continues_and_summarizes_once(self):
        window = self.window()
        window.should_suppress("APP_STARTED")
        window.should_suppress("APP_STARTED")
        self.now += 30
        # 进程重启：新对象读同一份状态文件，窗口仍在
        again = self.window()
        self.assertTrue(again.in_window())
        again.should_suppress("APP_STARTED")
        self.now += 200
        summary = again.take_summary()
        self.assertIn("折叠 3 条事件", summary or "")
        self.assertIsNone(again.take_summary(), "重启后不得重复补发")
        # 再重启一次也已经发过，不再补发
        third = self.window()
        self.assertIsNone(third.take_summary())

    def test_summary_batch_events_fold_by_their_own_count(self):
        window = self.window()
        self.assertTrue(window.should_suppress("POLL_BATCH_SUMMARY", count=7))
        self.now += 200
        self.assertIn("折叠 7 条事件", window.take_summary() or "")

    def test_seconds_are_clamped_to_supported_range(self):
        self.assertEqual(clamp_seconds("120"), 120)
        self.assertEqual(clamp_seconds(1), MIN_SECONDS)
        self.assertEqual(clamp_seconds(99999), MAX_SECONDS)
        self.assertEqual(clamp_seconds("bad"), DEFAULT_SECONDS)
        window = self.window(seconds=5)
        self.assertEqual(window.seconds, MIN_SECONDS)


class StartupQuietNotifierTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.config = SimpleNamespace(
            cursor_dir=str(self.dir),
            monitor_events=["APP_STARTED", "LoginFail"],
            dnd_enabled=False, dnd_start_time="22:00", dnd_end_time="07:00",
            wechat_webhook_url="https://example.invalid/hook", dingtalk_webhook_url="",
            feishu_webhook_url="", bark_url="", pushplus_params="", dedup_window=300,
            http_pool_size=2, http_retry_count=0, http_timeout=1, poll_batch_summary_enabled=False,
            startup_quiet_enabled=True, startup_quiet_seconds=180,
            startup_quiet_keep_urgent=True, startup_quiet_only_after_boot=True,
        )
        self.now = 1_700_000_000.0
        for manager in (patch.object(MultiPlatformNotifier, "_start_merge_timer"),
                        patch("utils.push_history.add_record"),
                        patch("utils.push_stats.record")):
            manager.start()
            self.addCleanup(manager.stop)
        self.notifier = UnifiedNotifier(self.config)
        self.addCleanup(self.notifier.close)
        self.notifier._startup_quiet = StartupQuietWindow(
            self.dir, clock=lambda: self.now, uptime_reader=lambda: 5.0
        )
        self.notifier._configure_startup_quiet(self.notifier._startup_quiet)
        self.notifier.multi_platform_notifier.send_notification = Mock(
            return_value=(True, [{"channel": "wechat", "success": True}])
        )
        self.notifier.multi_platform_notifier.send_system_notification = Mock(
            return_value={"success": True, "success_count": 1, "fail_count": 0}
        )

    def test_quiet_window_skips_pushes_then_sends_one_summary(self):
        result = self.notifier.send_notification("APP_STARTED", {"name": "相册"}, "{}", "now")
        self.assertEqual(result.method, "startup_quiet_skipped")
        self.assertEqual(self.notifier.multi_platform_notifier.send_notification.call_count, 0)

        # 安全事件照常推送
        urgent = self.notifier.send_notification("LoginFail", {"user": "land"}, "{}", "now")
        self.assertNotEqual(urgent.method, "startup_quiet_skipped")
        self.assertEqual(self.notifier.multi_platform_notifier.send_notification.call_count, 1)

        self.notifier.flush_startup_quiet_if_needed()
        self.assertEqual(self.notifier.multi_platform_notifier.send_system_notification.call_count, 0,
                         "窗口未结束不该发汇总")

        self.now += 200
        self.notifier.flush_startup_quiet_if_needed()
        self.assertEqual(self.notifier.multi_platform_notifier.send_system_notification.call_count, 1)
        event_type, message = self.notifier.multi_platform_notifier.send_system_notification.call_args[0][:2]
        self.assertEqual(event_type, "STARTUP_QUIET_SUMMARY")
        self.assertIn("折叠 1 条事件", message)

        # 再检查一次不会重复补发
        self.notifier.flush_startup_quiet_if_needed()
        self.assertEqual(self.notifier.multi_platform_notifier.send_system_notification.call_count, 1)

    def test_disabled_config_keeps_previous_behaviour(self):
        self.config.startup_quiet_enabled = False
        self.notifier._configure_startup_quiet(self.notifier._startup_quiet)
        result = self.notifier.send_notification("APP_STARTED", {"name": "相册"}, "{}", "now")
        self.assertNotEqual(result.method, "startup_quiet_skipped")
        self.assertEqual(self.notifier.multi_platform_notifier.send_notification.call_count, 1)

    def test_failed_summary_keeps_counts_for_retry(self):
        self.notifier.send_notification("APP_STARTED", {"name": "相册"}, "{}", "now")
        self.now += 200
        self.notifier.multi_platform_notifier.send_system_notification.return_value = {
            "success": False, "success_count": 0, "fail_count": 1,
            "channel_results": [{"channel": "wechat", "success": False, "error": "down"}],
        }
        self.notifier.flush_startup_quiet_if_needed()
        self.assertEqual(self.notifier.multi_platform_notifier.send_system_notification.call_count, 1)
        status = self.notifier._startup_quiet.status()
        self.assertEqual(status["folded"].get("APP_STARTED"), 1)
        self.assertFalse(status["summary_sent"])

        self.notifier.multi_platform_notifier.send_system_notification.return_value = {
            "success": True, "success_count": 1, "fail_count": 0,
        }
        self.notifier.flush_startup_quiet_if_needed()
        self.assertEqual(self.notifier.multi_platform_notifier.send_system_notification.call_count, 2)
        status = self.notifier._startup_quiet.status()
        self.assertEqual(status["folded"], {})
        self.assertTrue(status["summary_sent"])

    def test_disabling_mid_window_flushes_folded_summary(self):
        self.notifier.send_notification("APP_STARTED", {"name": "相册"}, "{}", "now")
        self.notifier.send_notification("DOCKER_CONTAINER_START", {"name": "x"}, "{}", "now")
        self.assertTrue(self.notifier._startup_quiet.in_window())
        self.config.startup_quiet_enabled = False
        self.notifier._configure_startup_quiet(self.notifier._startup_quiet)
        self.assertEqual(self.notifier.multi_platform_notifier.send_system_notification.call_count, 1)
        event_type, message = self.notifier.multi_platform_notifier.send_system_notification.call_args[0][:2]
        self.assertEqual(event_type, "STARTUP_QUIET_SUMMARY")
        self.assertIn("折叠 2 条事件", message)
        self.assertEqual(self.notifier._startup_quiet.status()["folded"], {})


class StartupQuietPeekConfirmTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.now = 1_700_000_000.0

    def test_peek_does_not_clear_until_confirm(self):
        window = StartupQuietWindow(
            self.dir, clock=lambda: self.now, uptime_reader=lambda: 5.0
        )
        window.configure(enabled=True, seconds=180, keep_urgent=True, only_after_boot=True)
        window.should_suppress("APP_STARTED")
        self.now += 200
        first = window.peek_summary()
        second = window.peek_summary()
        self.assertEqual(first, second)
        self.assertIn("折叠 1 条事件", first or "")
        self.assertEqual(window.status()["folded"].get("APP_STARTED"), 1)
        window.confirm_summary()
        self.assertIsNone(window.peek_summary())
        self.assertEqual(window.status()["folded"], {})


if __name__ == "__main__":
    unittest.main()
