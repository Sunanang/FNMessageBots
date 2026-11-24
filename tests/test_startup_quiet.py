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
    event_amount,
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


class StartupQuietContainerTests(unittest.TestCase):
    """Docker 部署：/proc/uptime 是宿主机的，不能拿来判断开机首启。"""

    def test_read_system_uptime_returns_none_in_container(self):
        import utils.startup_quiet as module

        with patch.object(module, "in_container", return_value=True):
            self.assertIsNone(module.read_system_uptime(), "容器内不能返回宿主机开机秒数")
        # 不在容器里：能读就读，读不到（macOS）返回 None，都不该抛异常
        with patch.object(module, "in_container", return_value=False):
            value = module.read_system_uptime()
            self.assertTrue(value is None or value >= 0)

    def test_container_falls_back_to_app_start_anchor(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        now = 1_700_000_000.0
        window = StartupQuietWindow(
            Path(temp.name), clock=lambda: now, uptime_reader=lambda: None
        )
        window.configure(enabled=True, seconds=180, keep_urgent=True, only_after_boot=True)
        self.assertTrue(window.in_window(), "容器内只有应用启动时刻，应当照样静默")
        self.assertIsNone(window.status()["inactive_reason"])
        self.assertAlmostEqual(window.status()["window_end"], now + 180, delta=1)

    def test_container_window_end_does_not_slide_on_each_ensure(self):
        """无 boot_at 时若误判成「新开机」，每次 flush/suppress 都会把窗口往后推，永远结束不了。"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        clock = {"t": 1_700_000_000.0}
        window = StartupQuietWindow(
            Path(temp.name), clock=lambda: clock["t"], uptime_reader=lambda: None
        )
        window.configure(enabled=True, seconds=180, keep_urgent=True, only_after_boot=True)
        end_first = window.status()["window_end"]
        window.should_suppress("APP_STARTED")
        clock["t"] += 30
        window.should_suppress("APP_STARTED")
        self.assertEqual(window.status()["window_end"], end_first)
        self.assertEqual(window.status()["folded"].get("APP_STARTED"), 2)
        clock["t"] += 200
        self.assertFalse(window.in_window())
        self.assertIn("折叠 2 条事件", window.take_summary() or "")


class StartupQuietRobustnessTests(unittest.TestCase):
    """并发、重启、条数解析这些「不显眼但会丢数据」的点。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.now = 1_700_000_000.0

    def window(self, uptime=5.0, **config):
        params = {"enabled": True, "seconds": 180, "keep_urgent": True, "only_after_boot": True}
        params.update(config)
        window = StartupQuietWindow(
            self.dir, clock=lambda: self.now, uptime_reader=lambda: uptime
        )
        window.configure(**params)
        return window

    def test_concurrent_pollers_do_not_lose_counts(self):
        window = self.window()
        import threading

        def blast():
            for _ in range(50):
                window.should_suppress("APP_STARTED")

        threads = [threading.Thread(target=blast) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        folded = window.status()["folded"].get("APP_STARTED")
        self.assertEqual(folded, 400, "多线程同时折叠不该丢计数")

    def test_restart_does_not_shift_window_end(self):
        window = self.window()
        window.should_suppress("APP_STARTED")
        end_first = window.status()["window_end"]
        self.now += 60
        again = self.window()  # 模拟进程重启：新对象读同一份状态
        self.assertEqual(again.status()["window_end"], end_first,
                         "同一次开机内重启应用不得把窗口往后推")
        self.assertTrue(again.in_window())

    def test_early_reboot_opens_fresh_window(self):
        """短 uptime 内再开机：uptime 倒退应识别为新开机，不能因 ±300 漂移被误判成同一次。"""
        uptime = {"v": 50.0}
        window = StartupQuietWindow(
            self.dir, clock=lambda: self.now, uptime_reader=lambda: uptime["v"]
        )
        window.configure(enabled=True, seconds=180, keep_urgent=True, only_after_boot=True)
        window.should_suppress("APP_STARTED")
        end_first = window.status()["window_end"]

        self.now += 120
        uptime["v"] = 10.0  # 真正重启后 uptime 归零再涨
        again = StartupQuietWindow(
            self.dir, clock=lambda: self.now, uptime_reader=lambda: uptime["v"]
        )
        again.configure(enabled=True, seconds=180, keep_urgent=True, only_after_boot=True)
        self.assertNotEqual(again.status()["window_end"], end_first,
                            "早重启必须重开窗口，不能沿用上一次开机的 window_end")
        self.assertTrue(again.in_window())
        self.assertAlmostEqual(again.status()["window_end"], self.now - 10.0 + 180, delta=1)

    def test_new_boot_does_not_carry_previous_fold_counts(self):
        """真开机重开窗口时丢掉上一段未汇总计数，避免汇总掺进上一次开机的条数。"""
        uptime = {"v": 40.0}
        window = StartupQuietWindow(
            self.dir, clock=lambda: self.now, uptime_reader=lambda: uptime["v"]
        )
        window.configure(enabled=True, seconds=180, keep_urgent=True, only_after_boot=True)
        for _ in range(5):
            window.should_suppress("APP_STARTED")
        self.assertEqual(window.status()["folded"].get("APP_STARTED"), 5)

        self.now += 90
        uptime["v"] = 8.0
        again = StartupQuietWindow(
            self.dir, clock=lambda: self.now, uptime_reader=lambda: uptime["v"]
        )
        again.configure(enabled=True, seconds=180, keep_urgent=True, only_after_boot=True)
        again.should_suppress("DOCKER_CONTAINER_START")
        self.now += 200
        summary = again.take_summary() or ""
        self.assertIn("折叠 1 条事件", summary)
        self.assertNotIn("应用启动", summary)
        self.assertIn("容器 1", summary)

    def test_config_change_reopens_window_with_carried_counts(self):
        window = self.window()
        window.should_suppress("APP_STARTED")
        window.should_suppress("APP_STARTED")
        longer = self.window(seconds=600)  # 改时长 = 配置变了
        self.assertTrue(longer.in_window())
        self.now += 700
        summary = longer.take_summary()
        self.assertIn("折叠 2 条事件", summary or "")
        self.assertIn("10 分钟", summary or "")

    def test_event_amount_reads_payload_counters(self):
        self.assertEqual(event_amount("APP_STARTED", {}), 1)
        self.assertEqual(event_amount("POLL_BATCH_SUMMARY", {"count": 12}), 12)
        self.assertEqual(event_amount("MEDIA_LIBRARY_SUMMARY", {"total": 7}), 7)
        self.assertEqual(event_amount("MEDIA_LIBRARY_SUMMARY", {"items": [1, 2, 3]}), 3)
        self.assertEqual(event_amount("MUSIC_LIBRARY_SUMMARY", {"by_type": {"入库": 4, "移出": 2}}), 6)
        self.assertEqual(event_amount("MUSIC_LIBRARY_SUMMARY", {"count": "bad"}), 1)

    def test_media_summary_folds_full_batch_size(self):
        window = self.window()
        self.assertTrue(window.should_suppress("MEDIA_LIBRARY_SUMMARY", count=event_amount(
            "MEDIA_LIBRARY_SUMMARY", {"items": [1] * 9})))
        self.now += 200
        self.assertIn("折叠 9 条事件", window.take_summary() or "")


class StartupQuietAcceptanceTests(unittest.TestCase):
    """对齐真实开机暴涨验收口径：折叠条数、白名单、peek/confirm、容器、早重启。"""

    def test_boot_storm_end_to_end(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        now = {"t": 1_700_000_000.0}
        uptime = {"v": 5.0}
        window = StartupQuietWindow(
            Path(temp.name), clock=lambda: now["t"], uptime_reader=lambda: uptime["v"]
        )
        window.configure(enabled=True, seconds=180, keep_urgent=True, only_after_boot=True)

        for _ in range(18):
            self.assertTrue(window.should_suppress("APP_STARTED"))
        for _ in range(11):
            self.assertTrue(window.should_suppress("DOCKER_CONTAINER_START"))
        for _ in range(4):
            self.assertTrue(window.should_suppress("DiskWakeup"))
        self.assertTrue(window.should_suppress(
            "POLL_BATCH_SUMMARY", count=event_amount("POLL_BATCH_SUMMARY", {"count": 27})))
        self.assertTrue(window.should_suppress(
            "MEDIA_LIBRARY_SUMMARY", count=event_amount("MEDIA_LIBRARY_SUMMARY", {"items": [1] * 6})))
        for event in ("LoginFail", "SSH_LOGIN_SUCCESS", "UPS_ONBATT"):
            self.assertFalse(window.should_suppress(event), event)

        now["t"] += 200
        summary = window.peek_summary()
        self.assertIsNotNone(summary)
        self.assertIn("折叠 66 条事件", summary)
        self.assertIn("合并通知 33", summary)
        self.assertIn("应用启动 18", summary)
        self.assertIn("容器 11", summary)
        self.assertIn("磁盘存储 4", summary)
        self.assertEqual(window.peek_summary(), summary)
        window.confirm_summary()
        self.assertIsNone(window.peek_summary())

        now["t"] += 10
        uptime["v"] = 215.0
        again = StartupQuietWindow(
            Path(temp.name), clock=lambda: now["t"], uptime_reader=lambda: uptime["v"]
        )
        again.configure(enabled=True, seconds=180, keep_urgent=True, only_after_boot=True)
        self.assertIsNone(again.peek_summary())
        self.assertFalse(again.in_window())


if __name__ == "__main__":
    unittest.main()
