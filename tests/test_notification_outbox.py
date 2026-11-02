"""持久化待发通知：模拟时钟/渠道，真实临时 SQLite，不访问 NAS。"""
import concurrent.futures
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from main import Application
from monitor.event_processor import EventProcessor
from monitor.models import JournalEntry
from notifier.multi_platform_notifier import MultiPlatformNotifier
from notifier.unified_notifier import NotificationResult, UnifiedNotifier
from utils.notification_outbox import NotificationOutbox, OutboxWriteError


class NotificationOutboxTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.now = datetime(2026, 11, 1, 23).timestamp()
        owner = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(owner.now, tz)

        for manager in (
            patch("notifier.unified_notifier.datetime", Clock),
            patch("notifier.unified_notifier.time.time", side_effect=lambda: self.now),
            patch.object(MultiPlatformNotifier, "_start_merge_timer"),
            patch("utils.push_history.add_record"),
            patch("utils.push_stats.record"),
            patch("monitor.event_processor.LogStorage"),
        ):
            manager.start()
            self.addCleanup(manager.stop)
        self.config = SimpleNamespace(
            cursor_dir=str(self.base), monitor_events=["LoginSucc", "MUSIC_TRACK_ADDED", "MUSIC_FAVORITED", "SSH_LOGIN_SUCCESS"],
            dnd_enabled=True, dnd_start_time="22:00", dnd_end_time="07:00",
            wechat_webhook_url="https://example.invalid/hook", dingtalk_webhook_url="",
            feishu_webhook_url="", bark_url="", pushplus_params="", dedup_window=300,
            http_pool_size=2, http_retry_count=0, http_timeout=1, poll_batch_summary_enabled=True,
        )

    def notifier(self):
        notifier = UnifiedNotifier(self.config)
        self.addCleanup(notifier.close)
        notifier._query_dnd_events_summary = Mock(return_value={})
        notifier.multi_platform_notifier.send_system_notification = Mock(return_value={
            "success": True, "success_count": 1, "fail_count": 0,
        })
        return notifier

    def buffer(self, notifier, count=3, batch="batch-a"):
        return notifier.send_notification("MUSIC_LIBRARY_SUMMARY", {
            "batch_id": batch, "by_type": {"MUSIC_TRACK_ADDED": count},
        }, "{}", "2026-11-01 23:00:00")

    def end_dnd(self):
        self.now = datetime(2026, 11, 2, 7, 1).timestamp()

    def item(self, number=1):
        return {
            "event_type": "MUSIC_FAVORITED", "source": "music_db", "row_id": number,
            "event_data": {"title": f"歌{number}", "_source": "music_db", "_source_cursor": f"fav:{number}"},
            "entry": JournalEntry(str(number), "2026-11-01 23:00:00", "nas", "music", "event", 0, 0, "{}"),
            "handler": Mock(),
        }

    def app(self):
        app = Application()
        app.config = self.config
        app.logger = Mock()
        app.event_processor = Mock()
        app.event_processor.process_batch_events.return_value = True
        return app

    def test_dnd_counts_survive_restart_and_same_batch_is_not_counted_twice(self):
        first = self.notifier()
        self.assertTrue(self.buffer(first).success)
        restored = self.notifier()
        self.assertTrue(self.buffer(restored).success)
        self.assertEqual(restored._dnd_light_buffer_counts, {"MUSIC_TRACK_ADDED": 3})
        self.end_dnd()
        restored.flush_dnd_buffer_if_needed()
        send = restored.multi_platform_notifier.send_system_notification
        self.assertIn("3 次", send.call_args_list[0].args[1])
        self.assertEqual(restored._dnd_light_buffer_counts, {})
        third = self.notifier()
        third.flush_dnd_buffer_if_needed()
        third.multi_platform_notifier.send_system_notification.assert_not_called()

    def test_dnd_failure_retains_frozen_batch_and_retry_delay_across_restart(self):
        notifier = self.notifier()
        self.buffer(notifier)
        self.end_dnd()
        notifier._query_dnd_events_summary.return_value = {"LoginSucc": 2}
        notifier.multi_platform_notifier.send_system_notification.return_value = {"success": False}
        notifier.flush_dnd_buffer_if_needed()
        original = notifier.multi_platform_notifier.send_system_notification.call_args.args[1]
        self.assertEqual(notifier._last_dnd_summary_end, "")
        restored = self.notifier()
        restored.flush_dnd_buffer_if_needed()
        restored.multi_platform_notifier.send_system_notification.assert_not_called()
        self.now += 59
        restored.flush_dnd_buffer_if_needed()
        restored.multi_platform_notifier.send_system_notification.assert_not_called()
        self.now += 1
        restored.flush_dnd_buffer_if_needed()
        self.assertEqual(restored.multi_platform_notifier.send_system_notification.call_args_list[0].args[1], original)
        restored._query_dnd_events_summary.assert_not_called()
        self.assertEqual(restored._last_dnd_summary_end, "2026-11-02T07:00:00")

    def test_dnd_exception_retains_counts_and_does_not_advance_cursor(self):
        notifier = self.notifier()
        self.buffer(notifier)
        self.end_dnd()
        notifier.multi_platform_notifier.send_system_notification.side_effect = RuntimeError("network")
        notifier.flush_dnd_buffer_if_needed()
        self.assertEqual(notifier._dnd_light_buffer_counts, {"MUSIC_TRACK_ADDED": 3})
        self.assertEqual(self.notifier()._last_dnd_summary_end, "")

    def test_partial_success_acknowledges_without_resending_whole_summary(self):
        notifier = self.notifier()
        self.buffer(notifier)
        self.end_dnd()
        notifier.multi_platform_notifier.send_system_notification.return_value = {
            "success": True, "success_count": 1, "fail_count": 1,
        }
        notifier.flush_dnd_buffer_if_needed()
        notifier.flush_dnd_buffer_if_needed()
        self.assertEqual(notifier.multi_platform_notifier.send_system_notification.call_count, 2)
        self.assertFalse(notifier._read_dnd_state()["periods"])

    def test_old_failed_period_and_new_period_stay_separate(self):
        notifier = self.notifier()
        self.buffer(notifier, 3)
        self.end_dnd()
        notifier.multi_platform_notifier.send_system_notification.return_value = {"success": False}
        notifier.flush_dnd_buffer_if_needed()
        self.now = datetime(2026, 11, 2, 23).timestamp()
        self.buffer(notifier, 5, "batch-b")
        notifier.multi_platform_notifier.send_system_notification.reset_mock()
        notifier.flush_dnd_buffer_if_needed()
        notifier.multi_platform_notifier.send_system_notification.assert_not_called()
        self.now = datetime(2026, 11, 3, 7, 1).timestamp()
        notifier.multi_platform_notifier.send_system_notification.return_value = {"success": True}
        notifier.flush_dnd_buffer_if_needed()
        self.assertIn("3 次", notifier.multi_platform_notifier.send_system_notification.call_args_list[0].args[1])
        notifier.multi_platform_notifier.send_system_notification.reset_mock()
        notifier.flush_dnd_buffer_if_needed()
        message = notifier.multi_platform_notifier.send_system_notification.call_args_list[0].args[1]
        self.assertIn("5 次", message)
        self.assertIn("2026-11-03", message)

    def test_disabled_dnd_cancels_pending_in_persistent_storage(self):
        notifier = self.notifier()
        self.buffer(notifier)
        self.config.dnd_enabled = False
        notifier._handle_dnd_config_transition_after_reload()
        restored = self.notifier()
        self.assertFalse(restored._dnd_light_buffer_counts)
        self.assertFalse(restored._read_dnd_state()["periods"])

    def test_dnd_database_query_failure_keeps_external_counts(self):
        notifier = self.notifier()
        self.buffer(notifier)
        self.end_dnd()
        notifier._query_dnd_events_summary.side_effect = RuntimeError("db locked")
        with self.assertRaisesRegex(RuntimeError, "db locked"):
            notifier.flush_dnd_buffer_if_needed()
        self.assertEqual(self.notifier()._dnd_light_buffer_counts, {"MUSIC_TRACK_ADDED": 3})

    def test_dnd_storage_failure_is_not_accepted_as_delivered(self):
        notifier = self.notifier()
        with patch.object(notifier._dnd_store(), "update_dnd", side_effect=OSError("disk full")):
            with self.assertRaises(OutboxWriteError):
                self.buffer(notifier)
        self.assertEqual(self.notifier()._dnd_light_buffer_counts, {})

    def test_dnd_prepare_storage_failure_prevents_network_delivery(self):
        notifier = self.notifier()
        self.buffer(notifier)
        self.end_dnd()
        with patch.object(notifier._dnd_store(), "update_dnd", side_effect=OSError("disk full")):
            with self.assertRaises(OutboxWriteError):
                notifier.flush_dnd_buffer_if_needed()
        notifier.multi_platform_notifier.send_system_notification.assert_not_called()
        self.assertEqual(self.notifier()._dnd_light_buffer_counts, {"MUSIC_TRACK_ADDED": 3})

    def test_main_db_only_grouped_events_are_not_double_counted_in_dnd(self):
        notifier = self.notifier()
        result = notifier.send_notification("POLL_BATCH_SUMMARY", {
            "by_type": {"SSH_LOGIN_SUCCESS": 1},
            "grouped_events": {"SSH_LOGIN_SUCCESS": [{"event_data": {"_source": "logger_db"}}]},
        }, "{}", "2026-11-01 23:00:00")
        self.assertTrue(result.success)
        self.assertEqual(notifier._dnd_light_buffer_counts, {})

    def test_poll_queue_serializes_entries_and_survives_restart(self):
        first = self.app()
        first._enqueue_unified_batch_items([self.item()])
        restored = self.app()
        restored._flush_unified_batch_pending_only()
        items = restored.event_processor.process_batch_events.call_args.args[0]
        self.assertIsInstance(items[0]["entry"], JournalEntry)
        self.assertNotIn("handler", items[0])
        self.assertEqual(items[0]["event_data"]["title"], "歌1")
        self.assertEqual(restored._poll_outbox().poll_items(), [])

    def test_poll_failure_keeps_exact_batch_and_new_arrivals_for_next_batch(self):
        first = self.app()
        first._enqueue_unified_batch_items([self.item()])
        first.event_processor.process_batch_events.return_value = False
        first._flush_unified_batch_pending_only()
        first._enqueue_unified_batch_items([self.item(2)])
        restored = self.app()
        restored._flush_unified_batch_pending_only()
        restored.event_processor.process_batch_events.assert_not_called()
        self.now += 60
        restored._flush_unified_batch_pending_only()
        self.assertEqual([item["row_id"] for item in restored.event_processor.process_batch_events.call_args.args[0]], [1])
        restored._flush_unified_batch_pending_only()
        self.assertEqual([item["row_id"] for item in restored.event_processor.process_batch_events.call_args.args[0]], [2])

    def test_poll_exception_keeps_batch_on_disk(self):
        app = self.app()
        app._enqueue_unified_batch_items([self.item()])
        app.event_processor.process_batch_events.side_effect = RuntimeError("network")
        app._flush_unified_batch_pending_only()
        self.assertEqual(len(self.app()._poll_outbox().poll_items()), 1)

    def test_poll_enqueue_storage_failure_propagates_without_false_acceptance(self):
        app = self.app()
        with patch.object(app._poll_outbox(), "enqueue_poll", side_effect=OSError("disk full")):
            with self.assertRaises(OutboxWriteError):
                app._enqueue_unified_batch_items([self.item()])
        self.assertEqual(app._poll_outbox().poll_items(), [])

    def test_poll_claim_storage_failure_prevents_delivery(self):
        app = self.app()
        app._enqueue_unified_batch_items([self.item()])
        with patch.object(app._poll_outbox(), "claim_poll", side_effect=OSError("disk full")):
            app._flush_unified_batch_pending_only()
        app.event_processor.process_batch_events.assert_not_called()

    def test_poll_cancelled_event_selection_is_not_delivered(self):
        app = self.app()
        app._enqueue_unified_batch_items([self.item()])
        self.config.monitor_events = ["LoginSucc"]
        app._flush_unified_batch_pending_only()
        app.event_processor.process_batch_events.assert_not_called()
        self.assertFalse(app._poll_outbox().poll_items())

    def test_new_events_arriving_during_send_survive_acknowledgement(self):
        app = self.app()
        app._enqueue_unified_batch_items([self.item()])
        app.event_processor.process_batch_events.side_effect = lambda *_: app._enqueue_unified_batch_items([self.item(2)])
        app._flush_unified_batch_pending_only()
        self.assertEqual([item["row_id"] for item in app._poll_outbox().poll_items()], [2])

    def test_parallel_enqueues_are_atomic_and_deduplicated(self):
        app = self.app()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: app._enqueue_unified_batch_items([self.item(n % 10)]), range(40)))
        self.assertEqual(len(app._poll_outbox().poll_items()), 10)

    def test_poll_to_dnd_transfer_is_persisted_before_poll_queue_ack(self):
        app = self.app()
        notifier = self.notifier()
        app.event_processor = EventProcessor(notifier, self.config)
        app._enqueue_unified_batch_items([self.item()])
        app._flush_unified_batch_pending_only()
        self.assertFalse(app._poll_outbox().poll_items())
        self.assertEqual(self.notifier()._dnd_light_buffer_counts, {"MUSIC_FAVORITED": 1})

    def test_real_batch_processor_propagates_notification_failure(self):
        notifier = Mock()
        notifier.send_notification.return_value = NotificationResult(False, "mock", {})
        processor = EventProcessor(notifier, self.config)
        self.assertFalse(processor.process_batch_events([self.item()]))

    def test_outbox_transactions_rollback_on_failure(self):
        outbox = NotificationOutbox(str(self.base))
        def fail(state):
            state["cursor"] = "bad"
            raise OSError("disk full")
        with self.assertRaises(OSError):
            outbox.update_dnd(fail)
        self.assertEqual(outbox.dnd_state(), {})
        self.assertEqual(outbox.path.stat().st_mode & 0o777, 0o600)

    def test_dnd_handoff_receipt_prevents_replay_after_poll_ack_failure_and_restart(self):
        app = self.app()
        notifier = self.notifier()
        app.event_processor = EventProcessor(notifier, self.config)
        app._enqueue_unified_batch_items([self.item()])
        with patch.object(app._poll_outbox(), "ack_poll", side_effect=OSError("disk full")):
            app._flush_unified_batch_pending_only()
        self.end_dnd()
        restored = self.app()
        notifier2 = self.notifier()
        notifier2.multi_platform_notifier.send_notification = Mock()
        restored.event_processor = EventProcessor(notifier2, self.config)
        restored._flush_unified_batch_pending_only()
        notifier2.multi_platform_notifier.send_notification.assert_not_called()
        self.assertFalse(restored._poll_outbox().poll_items())
        self.assertEqual(notifier2._dnd_light_buffer_counts, {"MUSIC_FAVORITED": 1})
        notifier2.flush_dnd_buffer_if_needed()
        self.assertIn("1 次", notifier2.multi_platform_notifier.send_system_notification.call_args_list[0].args[1])

    def test_disabling_dnd_after_poll_ack_failure_retries_real_delivery_after_restart(self):
        app = self.app()
        notifier = self.notifier()
        app.event_processor = EventProcessor(notifier, self.config)
        app._enqueue_unified_batch_items([self.item()])
        with patch.object(app._poll_outbox(), "ack_poll", side_effect=OSError("disk full")):
            app._flush_unified_batch_pending_only()
        self.assertTrue(notifier._read_dnd_state()["receipts"])
        self.assertEqual(len(app._poll_outbox().poll_items()), 1)

        self.config.dnd_enabled = False
        notifier._handle_dnd_config_transition_after_reload()
        self.now += 60
        restored = self.app()
        notifier2 = self.notifier()
        notifier2.multi_platform_notifier.send_notification = Mock(return_value=(True, []))
        restored.event_processor = EventProcessor(notifier2, self.config)
        restored._flush_unified_batch_pending_only()

        notifier2.multi_platform_notifier.send_notification.assert_called_once()
        self.assertFalse(restored._poll_outbox().poll_items())
        self.assertFalse(notifier2._read_dnd_state().get("receipts"))

    def test_disabling_dnd_invalidates_receipt_without_dropping_failed_queue_retry(self):
        app = self.app()
        notifier = self.notifier()
        app.event_processor = EventProcessor(notifier, self.config)
        app._enqueue_unified_batch_items([self.item()])
        with patch.object(app._poll_outbox(), "ack_poll", side_effect=OSError("disk full")):
            app._flush_unified_batch_pending_only()
        self.config.dnd_enabled = False
        notifier._handle_dnd_config_transition_after_reload()
        notifier.multi_platform_notifier.send_notification = Mock(return_value=(False, []))
        self.now += 60
        app._flush_unified_batch_pending_only()
        notifier.multi_platform_notifier.send_notification.assert_called_once()
        self.assertEqual(len(app._poll_outbox().poll_items()), 1)
        notifier.multi_platform_notifier.send_notification.return_value = (True, [])
        self.now += 60
        app._flush_unified_batch_pending_only()
        self.assertEqual(notifier.multi_platform_notifier.send_notification.call_count, 2)
        self.assertFalse(app._poll_outbox().poll_items())

    def test_startup_with_dnd_disabled_clears_receipt_left_before_reload_cleanup(self):
        app = self.app()
        notifier = self.notifier()
        app.event_processor = EventProcessor(notifier, self.config)
        app._enqueue_unified_batch_items([self.item()])
        with patch.object(app._poll_outbox(), "ack_poll", side_effect=OSError("disk full")):
            app._flush_unified_batch_pending_only()
        # 模拟配置已保存为关闭，但进程在热加载清理前退出。
        self.config.dnd_enabled = False
        self.now += 60
        restored = self.app()
        notifier2 = self.notifier()
        self.assertFalse(notifier2._read_dnd_state()["periods"])
        self.assertFalse(notifier2._read_dnd_state()["receipts"])
        notifier2.multi_platform_notifier.send_notification = Mock(return_value=(True, []))
        restored.event_processor = EventProcessor(notifier2, self.config)
        restored._flush_unified_batch_pending_only()
        notifier2.multi_platform_notifier.send_notification.assert_called_once()
        self.assertFalse(restored._poll_outbox().poll_items())

    def test_external_queue_uses_stable_source_cursor_instead_of_python_hash_row_id(self):
        app = self.app()
        first, replay = self.item(), self.item()
        replay["row_id"] = 123456789
        app._enqueue_unified_batch_items([first])
        self.app()._enqueue_unified_batch_items([replay])
        self.assertEqual(len(app._poll_outbox().poll_items()), 1)

    def test_dnd_delivery_does_not_clear_next_period_arrivals(self):
        notifier = self.notifier()
        self.buffer(notifier)
        self.end_dnd()

        def send(_event, message, _info):
            if message.startswith("【勿扰时段汇总】"):
                self.now = datetime(2026, 11, 2, 23).timestamp()
                self.buffer(notifier, 5, "next-night")
            return {"success": True}

        notifier.multi_platform_notifier.send_system_notification.side_effect = send
        notifier.flush_dnd_buffer_if_needed()
        self.assertEqual(self.notifier()._dnd_light_buffer_counts, {"MUSIC_TRACK_ADDED": 5})

    def test_concurrent_flushes_deliver_only_one_dnd_summary(self):
        notifier = self.notifier()
        self.buffer(notifier)
        self.end_dnd()
        entered, release = threading.Event(), threading.Event()

        def send(_event, message, _info):
            if message.startswith("【勿扰时段汇总】"):
                entered.set()
                self.assertTrue(release.wait(5))
            return {"success": True}

        notifier.multi_platform_notifier.send_system_notification.side_effect = send
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(notifier.flush_dnd_buffer_if_needed)
            self.assertTrue(entered.wait(5))
            second = pool.submit(notifier.flush_dnd_buffer_if_needed)
            release.set()
            first.result(5)
            second.result(5)
        self.assertEqual(notifier.multi_platform_notifier.send_system_notification.call_count, 2)

    def test_legacy_completed_cursor_prevents_historical_summary(self):
        (self.base / "dnd_summary_cursor.txt").write_text("2026-11-02T07:00:00")
        notifier = self.notifier()
        self.end_dnd()
        notifier.flush_dnd_buffer_if_needed()
        notifier._query_dnd_events_summary.assert_not_called()
        notifier.multi_platform_notifier.send_system_notification.assert_not_called()

    def test_log_watermark_does_not_advance_after_repeated_outbox_write_failures(self):
        from monitor.db_log_poller import DBLogPoller
        poller = DBLogPoller(str(self.base / "logger.db"), str(self.base), monitor_events=["LoginSucc"])
        poller._fetch_new_rows = Mock(return_value=[{
            "id": 1, "eventId": "LoginSucc", "parameter": "{}", "logtime": 1000,
        }])
        poller.add_handler("LoginSucc", Mock())
        poller.set_batch_handler(Mock(side_effect=OutboxWriteError("disk full")))
        with patch.object(poller, "_write_last_id") as write:
            for _ in range(5):
                self.assertEqual(poller._poll_once(0), 0)
        write.assert_not_called()

    def test_dnd_real_http_business_failure_then_success_retries_frozen_summary(self):
        import requests
        notifier = self.notifier()
        del notifier.multi_platform_notifier.send_system_notification
        self.buffer(notifier)
        self.end_dnd()
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"errcode":40001,"errmsg":"invalid credential"}'
        with patch.object(notifier.multi_platform_notifier.connection_pool.session, "request", return_value=response) as request:
            notifier.flush_dnd_buffer_if_needed()
            self.assertEqual(request.call_count, 1)
            self.assertEqual(notifier._last_dnd_summary_end, "")
            original = json.dumps(request.call_args.kwargs["json"], ensure_ascii=False)
            self.now += 60
            response._content = b'{"errcode":0}'
            notifier.flush_dnd_buffer_if_needed()
            self.assertEqual(request.call_count, 3)  # 正文重试成功后另发一条状态消息。
            self.assertIn("3 次", original)
            self.assertFalse(notifier._read_dnd_state()["periods"])

    def test_parallel_poll_flushes_do_not_send_the_same_claim_twice(self):
        app = self.app()
        app._enqueue_unified_batch_items([self.item()])
        entered, release = threading.Event(), threading.Event()
        def send(_items):
            entered.set()
            self.assertTrue(release.wait(5))
            return True
        app.event_processor.process_batch_events.side_effect = send
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(app._flush_unified_batch_pending_only)
            self.assertTrue(entered.wait(5))
            pool.submit(app._flush_unified_batch_pending_only).result(5)
            release.set()
            first.result(5)
        app.event_processor.process_batch_events.assert_called_once()

    def test_docker_stream_does_not_advance_cursor_before_durable_acceptance(self):
        from monitor import docker_events_poller as docker_mod
        poller = docker_mod.DockerEventsPoller("/mock/docker.sock", str(self.base), ["DOCKER_CONTAINER_START"])
        event = {"time": 1234, "timeNano": 1234000000000}
        client = Mock()
        client.events.return_value = iter([event])
        def fail(_event):
            poller.running = False
            raise OutboxWriteError("disk full")
        poller.running = True
        with patch.object(docker_mod, "docker", Mock(DockerClient=Mock(return_value=client))), patch.object(
            poller, "_event_before_start_cutoff", return_value=False
        ), patch.object(poller, "_handle_one", side_effect=fail), patch.object(
            poller, "_advance_since_from_stream_event"
        ) as advance:
            poller._run_loop()
        advance.assert_not_called()
