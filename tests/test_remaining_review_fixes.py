"""访问隔离、启动容错、推送失败和配置并发的回归；不访问真实 NAS。"""
import concurrent.futures
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

root = Path(__file__).resolve().parents[1]
source = root / "src"
if not source.is_dir():
    source = root / "cmd/fnmessagebots/src"
sys.path.insert(0, str(source))

from web import session_service, ui_app
from web.access_guard import GatewayPrefixMiddleware, LoginRateLimiter, SetupCodeStore, client_address
from web.auth_service import apply_new_password
from monitor import wan_ip_monitor
from notifier.unified_notifier import UnifiedNotifier
from notifier.multi_platform_notifier import MultiPlatformNotifier
from main import Application


class RemainingReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps(apply_new_password({}, "regression-password")))
        for manager in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(ui_app, "CONFIG_FILE", self.config),
            patch.object(ui_app, "_setup_codes", SetupCodeStore(self.base / ".setup_code")),
            patch.object(ui_app, "_login_limiter", LoginRateLimiter()),
            patch.object(ui_app, "_collect_external_db_access_warnings", return_value=[]),
        ):
            manager.start()
            self.addCleanup(manager.stop)
        self.app = ui_app.create_app()

    def login(self, client, path="/api/auth/login", **kwargs):
        result = client.post(path, json={"password": "regression-password"}, **kwargs)
        self.assertEqual(result.status_code, 200)
        return result

    def test_gateway_cookie_path_and_session_cannot_authenticate_tcp_entry(self):
        tcp_wsgi = self.app.wsgi_app
        prefix = "/app/FnMessageBot"
        self.app.wsgi_app = GatewayPrefixMiddleware(tcp_wsgi, prefix)
        client = self.app.test_client()
        headers = {"X-Trim-Userid": "1", "X-Trim-Isadmin": "true"}
        result = self.login(client, prefix + "/api/auth/login", headers=headers)
        self.assertIn("Path=/app/FnMessageBot/", result.headers["Set-Cookie"])
        sid = client.get_cookie(ui_app.AUTH_COOKIE_NAME, path=prefix + "/").value
        self.assertTrue(client.get(prefix + "/api/auth/status", headers=headers).get_json()["authenticated"])
        self.app.wsgi_app = tcp_wsgi
        tcp = self.app.test_client()
        tcp.set_cookie(ui_app.AUTH_COOKIE_NAME, sid)
        self.assertEqual(tcp.get("/api/config").status_code, 401)

    def test_loopback_password_setup_requires_code_even_with_forwarded_headers(self):
        self.config.write_text("{}")
        client = self.app.test_client()
        self.assertTrue(client.get("/api/auth/status").get_json()["setup_requires_code"])
        payload = {"password": "new-password-123", "password_confirm": "new-password-123"}
        denied = client.post("/api/auth/set-password", json=payload, headers={"X-Forwarded-For": "192.0.2.1"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.config.read_text(), "{}")
        payload["setup_code"] = ui_app._setup_codes.read()
        self.assertEqual(client.post("/api/auth/set-password", json=payload).status_code, 200)

    def test_trusted_proxy_separates_clients_and_untrusted_forwarded_headers_are_ignored(self):
        environ = {"REMOTE_ADDR": "192.0.2.2", "HTTP_X_FORWARDED_FOR": "198.51.100.1"}
        self.assertEqual(client_address(environ), "192.0.2.2")
        with patch.dict(os.environ, {"FNMB_TRUSTED_PROXIES": "127.0.0.1/32"}):
            attacker = self.app.test_client()
            for _ in range(5):
                attacker.post("/api/auth/login", json={"password": "wrong"}, headers={"X-Forwarded-For": "192.0.2.10"})
            self.assertEqual(attacker.post("/api/auth/login", json={"password": "regression-password"}, headers={"X-Forwarded-For": "192.0.2.10"}).status_code, 429)
            self.login(self.app.test_client(), headers={"X-Forwarded-For": "192.0.2.11"})
            environ = {"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "198.51.100.99, 192.0.2.1, 127.0.0.1"}
            self.assertEqual(client_address(environ), "192.0.2.1")

    def test_gateway_admins_have_separate_login_limits(self):
        prefix = "/app/FnMessageBot"
        self.app.wsgi_app = GatewayPrefixMiddleware(self.app.wsgi_app, prefix)
        client = self.app.test_client()
        headers = {"X-Trim-Userid": "1", "X-Trim-Isadmin": "true"}
        for _ in range(5):
            client.post(prefix + "/api/auth/login", json={"password": "wrong"}, headers=headers)
        headers["X-Trim-Userid"] = "2"
        self.login(client, prefix + "/api/auth/login", headers=headers)

    def test_only_one_concurrent_first_password_setup_can_succeed(self):
        self.config.write_text("{}")
        code = ui_app._setup_codes.ensure()
        barrier = threading.Barrier(2)

        def setup(index):
            client = self.app.test_client()
            barrier.wait(timeout=5)
            return client.post("/api/auth/set-password", json={"password": f"password-{index}", "password_confirm": f"password-{index}", "setup_code": code}).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(setup, [1, 2]))
        self.assertEqual(sorted(statuses), [200, 400])

    def test_get_config_does_not_write_old_config_migration(self):
        raw = apply_new_password({"monitor_events": list(set(ui_app.DEFAULT_SELECTED_EVENTS) | ui_app.OLD_DEFAULT_SELECTED_EVENTS_WITH_EXTRA)}, "regression-password")
        self.config.write_text(json.dumps(raw))
        client = self.app.test_client()
        self.login(client)
        before = self.config.read_bytes()
        with patch.object(ui_app, "_save_raw_config") as save:
            self.assertEqual(client.get("/api/config").status_code, 200)
        save.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_parallel_save_callbacks_are_serialized(self):
        active = 0
        maximum = 0
        entered = threading.Event()
        release = threading.Event()

        def callback():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            active -= 1

        app = ui_app.create_app(on_config_saved=callback)
        clients = [app.test_client(), app.test_client()]
        for client in clients:
            self.login(client)
        payload = {"events": ["LoginSucc"], "channels": [{"type": "wechat", "url": "https://example.invalid/webhook", "enabled": True}]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(clients[0].post, "/api/save-config", json=payload)
            self.assertTrue(entered.wait(timeout=5))
            second = pool.submit(clients[1].post, "/api/save-config", json=payload)
            release.set()
            self.assertEqual(first.result(timeout=5).status_code, 200)
            self.assertEqual(second.result(timeout=5).status_code, 200)
        self.assertEqual(maximum, 1)

    def test_runtime_reload_operations_are_serialized(self):
        app = Application()
        active = 0
        maximum = 0
        entered = threading.Event()
        release = threading.Event()

        def reload():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            active -= 1

        app._reload_config_locked = reload
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(app.reload_config)
            self.assertTrue(entered.wait(timeout=5))
            second = pool.submit(app.reload_config)
            release.set()
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertEqual(maximum, 1)

    def test_tcp_bind_failure_does_not_prevent_gateway_startup(self):
        with patch.dict(os.environ, {"FNMB_GATEWAY_SOCKET": str(self.base / "app.sock")}), \
             patch.object(ui_app, "create_app", return_value=self.app), \
             patch("werkzeug.serving.make_server", side_effect=[SystemExit(1), Mock()]) as server, \
             patch.object(ui_app, "_serve_forever", return_value="gateway-thread") as serve:
            self.assertEqual(ui_app.start_ui_server_in_background(), "gateway-thread")
        self.assertEqual(server.call_count, 2)
        self.assertEqual(serve.call_args.args[1], "FnMessageBots-Gateway")

    def test_gateway_bind_failure_keeps_tcp_and_all_failures_become_runtime_error(self):
        with patch.dict(os.environ, {"FNMB_GATEWAY_SOCKET": str(self.base / "app.sock")}), \
             patch.object(ui_app, "create_app", return_value=self.app), \
             patch("werkzeug.serving.make_server", side_effect=[Mock(), SystemExit(1)]), \
             patch.object(ui_app, "_serve_forever", return_value="tcp-thread"):
            self.assertEqual(ui_app.start_ui_server_in_background(), "tcp-thread")
        with patch.object(ui_app, "create_app", return_value=self.app), \
             patch("werkzeug.serving.make_server", side_effect=SystemExit(1)):
            with self.assertRaises(RuntimeError):
                ui_app.start_ui_server_in_background()

    def test_failed_wan_push_keeps_baseline_and_retries_until_success(self):
        wan_ip_monitor._save_state(wan_ip_monitor._state_path(self.base), {"baseline_done": True, "last_wan_ip": "8.8.8.8"})
        notifier = Mock()
        notifier.send_notification.side_effect = [SimpleNamespace(success=False), SimpleNamespace(success=True)]
        app = SimpleNamespace(config=SimpleNamespace(cursor_dir=str(self.base)), notifier=notifier)
        with patch.object(wan_ip_monitor, "_event_enabled", return_value=True), patch.object(wan_ip_monitor, "_fetch_wan_ip", return_value="1.1.1.1"):
            wan_ip_monitor.check_and_notify_wan_ip_change(app)
            self.assertEqual(wan_ip_monitor._load_state(wan_ip_monitor._state_path(self.base))["last_wan_ip"], "8.8.8.8")
            wan_ip_monitor.check_and_notify_wan_ip_change(app)
            wan_ip_monitor.check_and_notify_wan_ip_change(app)
        self.assertEqual(notifier.send_notification.call_count, 2)
        self.assertEqual(wan_ip_monitor._load_state(wan_ip_monitor._state_path(self.base))["last_wan_ip"], "1.1.1.1")

    def test_abandoned_sessions_and_rate_limit_entries_are_cleaned_on_later_activity(self):
        with patch("time.time", return_value=1000):
            sid = session_service.create_session()
            limiter = LoginRateLimiter()
            for _ in range(5):
                limiter.record_failure("old-ip")
        with patch("time.time", return_value=2000):
            session_service.create_session()
            limiter.retry_after("new-ip")
        self.assertNotIn(sid, session_service._sessions)
        self.assertNotIn("old-ip", limiter._fails)
        self.assertNotIn("old-ip", limiter._locked_until)

    def test_dnd_uses_system_clock_instead_of_shanghai_clock(self):
        notifier = object.__new__(UnifiedNotifier)
        notifier.config = SimpleNamespace(dnd_enabled=True, dnd_start_time="22:00", dnd_end_time="07:00")
        with patch("notifier.unified_notifier.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 10, 23)
            self.assertTrue(notifier._in_dnd_window())
            clock.now.assert_called_with()
            clock.now.return_value = datetime(2026, 10, 11, 10)
            start, end = notifier._calc_latest_dnd_period()
        self.assertEqual(start, datetime(2026, 10, 10, 22))
        self.assertEqual(end, datetime(2026, 10, 11, 7))

    def test_username_query_failure_closes_sqlite_connection(self):
        notifier = object.__new__(MultiPlatformNotifier)
        notifier.logger_user_lookup_db_path = str(self.config)
        notifier._nas_uid_name_cache_loaded_at = 0
        conn = Mock()
        conn.execute.side_effect = RuntimeError("database query failed")
        with patch("notifier.multi_platform_notifier.connect_readonly_with_fallback", return_value=conn):
            self.assertEqual(notifier._lookup_nas_uid_name_from_logger_db(1), "")
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
