"""代码审查发现的问题：临时配置/数据库及模拟响应，不访问 NAS 或发送通知。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from main import Application
from monitor.db_log_poller import DBLogPoller
from notifier.multi_platform_notifier import MultiPlatformMessage, MultiPlatformNotifier
from web import config_store, ui_app
from web.access_guard import SetupCodeStore
from web.auth_service import apply_new_password, hash_password, password_matches


class DeliveryAuditTests(unittest.TestCase):
    def notifier(self, **kwargs):
        with patch.object(MultiPlatformNotifier, "_start_merge_timer"):
            instance = MultiPlatformNotifier(**kwargs)
        self.addCleanup(instance.close)
        return instance

    def response(self, body):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        return response

    def test_native_channels_reject_http_200_business_errors(self):
        cases = [
            ("feishu_webhook_url", "https://example.invalid/hook", "_send_to_feishu", {"code": 19001, "msg": "bad token"}),
            ("bark_url", "https://example.invalid/device", "_send_to_bark", {"code": 400, "message": "invalid key"}),
            ("pushplus_params", '{"token":"test-token"}', "_send_to_pushplus", {"code": 500, "msg": "quota exceeded"}),
        ]
        for key, config, method, body in cases:
            with self.subTest(channel=key):
                notifier = self.notifier(**{key: config})
                with patch.object(notifier.connection_pool.session, "request", return_value=self.response(body)):
                    ok, result = getattr(notifier, method)(MultiPlatformMessage("title", "body"))
                self.assertFalse(ok)
                self.assertFalse(result["success"])
                self.assertIn(body.get("msg") or body.get("message"), result["error"])

    def test_native_success_and_legacy_feishu_responses(self):
        cases = [("_send_to_feishu", {"code": 0}), ("_send_to_feishu", {"StatusCode": 0}),
                 ("_send_to_bark", {"code": 200}), ("_send_to_pushplus", {"code": 200})]
        notifier = self.notifier(feishu_webhook_url="https://example.invalid/hook",
                                 bark_url="https://example.invalid/device", pushplus_params='{"token":"test"}')
        for method, body in cases:
            with self.subTest(method=method, body=body), patch.object(
                    notifier.connection_pool.session, "request", return_value=self.response(body)):
                self.assertTrue(getattr(notifier, method)(MultiPlatformMessage("title", "body"))[0])

    def test_generic_webhook_does_not_interpret_foreign_business_codes(self):
        notifier = self.notifier(webhook_params='{"url":"https://example.invalid/hook"}')
        with patch.object(notifier.connection_pool.session, "request", return_value=self.response({"errcode": 123})):
            self.assertTrue(notifier._send_to_webhook(MultiPlatformMessage("title", "body"))[0])

    def test_unresolved_redirect_is_not_reported_as_a_successful_webhook(self):
        notifier = self.notifier(webhook_params='{"url":"https://example.invalid/hook"}')
        response = self.response({})
        response.status_code = 302
        with patch.object(notifier.connection_pool.session, "request", return_value=response):
            ok, result = notifier._send_to_webhook(MultiPlatformMessage("title", "body"))
        self.assertFalse(ok)
        self.assertIn("HTTP 302", result["error"])

    def test_multiple_destinations_preserve_failures_and_count_actual_requests(self):
        notifier = self.notifier(wechat_webhook_url="https://example.invalid/a|https://example.invalid/b")
        with patch.object(notifier.connection_pool.session, "request", side_effect=[
            self.response({"errcode": 0}), self.response({"errcode": 40001, "errmsg": "invalid credential"})]):
            out = notifier.send_system_notification("TEST_PUSH", "test")
        self.assertTrue(out["success"])
        self.assertEqual(out["success_count"], 1)
        self.assertEqual(out["fail_count"], 1)
        self.assertIn("invalid credential", out["channel_results"][0]["error"])

    def test_merge_worker_flushes_all_elapsed_windows_and_prunes_dedup(self):
        notifier = self.notifier()
        notifier.disk_wakeup_cache = {5: [{"disk": "a"}], 8: [{"disk": "b"}], 10: [{"disk": "c"}]}
        notifier.sent_events = {"expired": 0, "recent": 49}
        notifier.dedup_window = 10
        def stop(_seconds):
            notifier._stop_flag = True
        def deliver(*_args):
            acquired = notifier._cache_lock.acquire(blocking=False)
            self.assertTrue(acquired, "发送期间不能持有磁盘事件缓存锁")
            if acquired:
                notifier._cache_lock.release()
            return True, []
        with patch("notifier.multi_platform_notifier.time.time", return_value=50), \
             patch("notifier.multi_platform_notifier.time.sleep", side_effect=stop), \
             patch.object(notifier, "_send_merged_disk_event", side_effect=deliver) as send:
            notifier._merge_timer_worker()
        self.assertEqual([call.args[2] for call in send.call_args_list], [5, 8])
        self.assertEqual(list(notifier.disk_wakeup_cache), [10])
        self.assertNotIn("expired", notifier.sent_events)
        self.assertIn("recent", notifier.sent_events)

    def test_dnd_summary_and_status_do_not_deduplicate_each_other(self):
        notifier = self.notifier(wechat_webhook_url="https://example.invalid/hook")
        with patch("notifier.multi_platform_notifier.time.time", return_value=1000), patch.object(
                notifier.connection_pool.session, "request", return_value=self.response({"errcode": 0})) as request:
            self.assertTrue(notifier.send_system_notification("DND_SUMMARY", "summary")["success"])
            self.assertTrue(notifier.send_system_notification("DND_SUMMARY", "delivery status")["success"])
            self.assertEqual(notifier.send_system_notification("DND_SUMMARY", "summary")["skipped"], "duplicate")
            self.assertEqual(request.call_count, 2)


class WebAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps(apply_new_password({}, "audit-password")))
        for manager in [patch.dict(os.environ, {}, clear=True), patch.object(ui_app, "CONFIG_FILE", self.config),
                        patch.object(ui_app, "_setup_codes", SetupCodeStore(self.base / ".setup_code")),
                        patch.object(ui_app, "_collect_external_db_access_warnings", return_value=[])]:
            manager.start()
            self.addCleanup(manager.stop)
        self.app = ui_app.create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.post("/api/auth/login", json={"password": "audit-password"})

    def test_non_object_config_remains_readable_as_an_error_and_is_not_overwritten(self):
        for value in [[], None, "bad config", 42]:
            with self.subTest(value=value):
                original = json.dumps(value)
                self.config.write_text(original)
                self.assertIsInstance(config_store.load_raw_config(self.config), dict)
                self.assertTrue(config_store.config_load_error(self.config))
                with patch.dict(os.environ, {"FNMB_WEB_PASSWORD": "environment-password"}):
                    client = ui_app.create_app().test_client()
                result = client.get("/api/auth/status")
                self.assertEqual(result.status_code, 500)
                self.assertTrue(result.get_json()["config_error"])
                self.assertEqual(self.config.read_text(), original)

    def test_non_object_api_payload_is_rejected_without_side_effects(self):
        original = self.config.read_text()
        for value in [[1], "bad payload", 42, None]:
            with self.subTest(value=value):
                result = self.client.post("/api/save-config", data=json.dumps(value), content_type="application/json")
                self.assertEqual(result.status_code, 400)
                self.assertEqual(self.config.read_text(), original)

    def test_failed_runtime_reload_is_reported_in_save_response(self):
        app = Application()
        app.config = Mock()
        app.config.reload_from_file.return_value = False
        client = ui_app.create_app(on_config_saved=app.reload_config).test_client()
        client.post("/api/auth/login", json={"password": "audit-password"})
        result = client.post("/api/save-config", json={"events": ["LoginSucc"], "channels": [
            {"type": "wechat", "url": "https://example.invalid/hook"}]})
        self.assertEqual(result.status_code, 200)
        self.assertIn("热加载失败", result.get_json()["message"])

    def test_invalid_save_field_types_do_not_write_partial_configuration(self):
        valid = {"events": ["LoginSucc"], "channels": [{"type": "wechat", "url": "https://example.invalid/hook"}]}
        original = self.config.read_text()
        for invalid in [{"events": [{}]}, {"channels": [{"type": "wechat", "url": 123}]},
                        {"channels": "wrong"}, {"dnd_start_time": 2200}, {"bark_icon": []}]:
            with self.subTest(invalid=invalid):
                response = self.client.post("/api/save-config", json={**valid, **invalid})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.config.read_text(), original)

    def test_partial_test_push_is_reported_to_the_user(self):
        notifier = Mock()
        notifier.send_system_notification.return_value = {
            "success": True, "fail_count": 1,
            "channel_results": [{"channel": "企业微信", "success": True, "error": "invalid credential"}]}
        with patch.object(ui_app, "_build_notifier_from_raw", return_value=notifier):
            response = self.client.post("/api/test", json={"content": "test"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["partial"])
        self.assertIn("invalid credential", response.get_json()["message"])
        notifier.close.assert_called_once()

    def test_test_push_exception_still_closes_the_temporary_notifier(self):
        notifier = Mock()
        notifier.send_system_notification.side_effect = RuntimeError("test failure")
        with patch.object(ui_app, "_build_notifier_from_raw", return_value=notifier):
            response = self.client.post("/api/test", json={"content": "test"})
        self.assertEqual(response.status_code, 500)
        notifier.close.assert_called_once()

    def test_password_first_letter_is_case_sensitive_in_both_directions(self):
        salt = "ab" * 16
        for original, different in [("audit-password", "Audit-password"), ("Audit-password", "audit-password")]:
            with self.subTest(original=original):
                stored = hash_password(original, bytes.fromhex(salt))
                self.assertTrue(password_matches(original, salt, stored))
                self.assertFalse(password_matches(different, salt, stored))


class PollerAuditTests(unittest.TestCase):
    def test_non_object_log_parameter_does_not_block_later_events(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "logger.db3"
            with sqlite3.connect(db) as writer:
                writer.execute("CREATE TABLE log(id INTEGER PRIMARY KEY, eventId TEXT, parameter TEXT, uname TEXT, uid INTEGER, "
                               "serviceId TEXT, logtime INTEGER, loglevel INTEGER, category TEXT)")
                for index, parameter in enumerate(["[]", "null", '"plain text"', "123", "{}"], 1):
                    writer.execute("INSERT INTO log (id, eventId, parameter, uname, uid) VALUES (?, 'LoginSucc', ?, 'test-user', 1)", (index, parameter))
            poller = DBLogPoller(str(db), directory, monitor_events=["LoginSucc"])
            handler = Mock()
            poller.add_handler("LoginSucc", handler)
            self.assertEqual(poller._poll_once(0), 5)
            self.assertEqual(handler.call_count, 5)
            self.assertEqual(handler.call_args_list[0].args[0]["raw"], "[]")
            self.assertEqual(poller._cursor_file.read_text(), "5")
