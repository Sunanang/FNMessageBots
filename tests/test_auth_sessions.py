"""密码入口与真实空闲超时：模拟时钟，不等待 15 分钟或启动 NAS 监控。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
source = root / "src"
if not source.is_dir():
    source = root / "cmd/fnmessagebots/src"
sys.path.insert(0, str(source))

from web import session_service, ui_app
from web.access_guard import GatewayPrefixMiddleware, LoginRateLimiter, SetupCodeStore, StripGatewayHeadersMiddleware
from web.auth_service import apply_new_password


class AuthSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps(apply_new_password({}, "test-application-password")))
        self.now = 1000.0
        for manager in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(ui_app, "CONFIG_FILE", self.config),
            patch.object(ui_app, "_setup_codes", SetupCodeStore(self.base / ".setup_code")),
            patch.object(ui_app, "_login_limiter", LoginRateLimiter()),
            patch.object(ui_app, "get_push_history_stats", return_value={}),
            patch.object(session_service.time, "time", side_effect=lambda: self.now),
        ):
            manager.start()
            self.addCleanup(manager.stop)
        self.app = ui_app.create_app()
        self.client = self.app.test_client()
        self.prefix = ""
        self.headers = {}

    def request(self, path, method="get", **kwargs):
        return getattr(self.client, method)(self.prefix + path, headers=self.headers, **kwargs)

    def login(self):
        response = self.request("/api/auth/login", "post", json={"password": "test-application-password"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        return response

    def gateway(self):
        self.prefix = "/app/FnMessageBot"
        self.headers = {"X-Trim-Userid": "1", "X-Trim-Isadmin": "true"}
        self.app.wsgi_app = GatewayPrefixMiddleware(self.app.wsgi_app, self.prefix)

    def test_docker_and_gateway_admin_both_need_application_password(self):
        for gateway in [False, True]:
            with self.subTest(gateway=gateway):
                if gateway:
                    self.gateway()
                    self.request("/api/auth/logout", "post", json={})
                home = self.request("/")
                self.assertEqual(home.status_code, 200)
                self.assertEqual(home.headers["Cache-Control"], "no-store")
                state = self.request("/api/auth/status").get_json()
                self.assertFalse(state["authenticated"])
                self.assertTrue(state["need_login"])
                self.assertEqual(self.request("/api/push-stats").status_code, 401)
                self.assertEqual(self.request("/support").status_code, 302)
                self.login()
                self.assertTrue(self.request("/api/auth/status").get_json()["authenticated"])
                self.assertEqual(self.request("/api/push-stats").status_code, 200)
                self.assertEqual(self.request("/support").status_code, 200)

    def test_reopening_configuration_reuses_unexpired_session_in_both_versions(self):
        for gateway in [False, True]:
            with self.subTest(gateway=gateway):
                if gateway:
                    self.gateway()
                self.login()
                old_id = self.client.get_cookie(ui_app.AUTH_COOKIE_NAME, path=self.prefix + "/").value
                self.now += 899
                response = self.request("/")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.client.get_cookie(ui_app.AUTH_COOKIE_NAME, path=self.prefix + "/").value, old_id)
                state = self.request("/api/auth/status").get_json()
                self.assertTrue(state["authenticated"])
                self.assertEqual(state["remaining_seconds"], 900)
                self.assertEqual(self.request("/api/push-stats").status_code, 200)

    def test_reopening_configuration_cannot_restore_expired_session(self):
        self.login()
        self.now += 900
        self.assertEqual(self.request("/").status_code, 200)
        state = self.request("/api/auth/status").get_json()
        self.assertFalse(state["authenticated"])
        self.assertTrue(state["need_login"])
        self.assertEqual(self.request("/api/push-stats").status_code, 401)

    def test_cookie_is_http_only_and_renewed_with_actual_activity(self):
        cookie = self.login().headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Max-Age=900", cookie)
        self.now += 800
        background = self.request("/api/auth/status")
        self.assertNotIn("Set-Cookie", background.headers)
        renewed = self.request("/api/auth/activity", "post", json={})
        self.assertIn("Max-Age=900", renewed.headers["Set-Cookie"])
        self.now += 200
        self.assertTrue(self.request("/api/auth/status").get_json()["authenticated"])

    def test_background_polling_cannot_extend_idle_timeout(self):
        self.login()
        for elapsed in [30, 300, 600, 899]:
            self.now = 1000 + elapsed
            status = self.request("/api/auth/status").get_json()
            self.assertEqual(status["remaining_seconds"], 900 - elapsed)
            self.assertEqual(self.request("/api/push-stats").status_code, 200)
        self.now = 1900
        self.assertFalse(self.request("/api/auth/status").get_json()["authenticated"])
        self.assertEqual(self.request("/api/push-stats").status_code, 401)
        self.assertEqual(self.request("/api/save-config", "post", json={}).status_code, 401)

    def test_user_activity_extends_valid_session_beyond_original_deadline(self):
        self.login()
        self.now = 1800
        response = self.request("/api/auth/activity", "post", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["remaining_seconds"], 900)
        self.now = 2000
        self.assertTrue(self.request("/api/auth/status").get_json()["authenticated"])
        self.now = 2700
        self.assertEqual(self.request("/api/auth/activity", "post", json={}).status_code, 401)

    def test_expired_session_cannot_be_revived_by_activity(self):
        self.login()
        self.now = 1901
        self.assertEqual(self.request("/api/auth/activity", "post", json={}).status_code, 401)
        self.assertFalse(self.request("/api/auth/status").get_json()["authenticated"])
        self.login()
        self.assertTrue(self.request("/api/auth/status").get_json()["authenticated"])

    def test_invalid_password_does_not_create_session(self):
        response = self.request("/api/auth/login", "post", json={"password": "wrong-password"})
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.client.get_cookie(ui_app.AUTH_COOKIE_NAME, path=self.prefix + "/"))
        self.assertEqual(self.request("/api/push-stats").status_code, 401)

    def test_legacy_disable_auth_cannot_bypass_password(self):
        with patch.dict(os.environ, {"FNMB_DISABLE_AUTH": "1"}):
            self.assertFalse(self.request("/api/auth/status").get_json()["authenticated"])
            self.assertEqual(self.request("/api/push-stats").status_code, 401)

    def test_non_admin_gateway_identity_is_denied_even_with_application_session(self):
        self.gateway()
        self.login()
        self.headers["X-Trim-Isadmin"] = "false"
        for path in ["/", "/api/auth/status", "/api/push-stats", "/support"]:
            self.assertEqual(self.request(path).status_code, 403)

    def test_tcp_forged_gateway_headers_do_not_skip_password(self):
        self.app.wsgi_app = StripGatewayHeadersMiddleware(self.app.wsgi_app)
        self.headers = {"X-Trim-Userid": "1", "X-Trim-Isadmin": "true"}
        state = self.request("/api/auth/status").get_json()
        self.assertFalse(state["via_gateway"])
        self.assertFalse(state["authenticated"])

    def test_logout_revokes_session(self):
        self.login()
        old_id = self.client.get_cookie(ui_app.AUTH_COOKIE_NAME, path=self.prefix + "/").value
        self.assertEqual(self.request("/api/auth/logout", "post", json={}).status_code, 200)
        self.client.set_cookie(ui_app.AUTH_COOKIE_NAME, old_id)
        self.assertEqual(self.request("/api/push-stats").status_code, 401)

    def test_late_write_response_does_not_renew_a_revoked_cookie(self):
        def revoke_during_write():
            session_service.delete_session(old_id)
            return {"ok": True}
        self.app.add_url_rule("/api/review-test-write", view_func=revoke_during_write, methods=["POST"])
        self.login()
        old_id = self.client.get_cookie(ui_app.AUTH_COOKIE_NAME, path=self.prefix + "/").value
        response = self.request("/api/review-test-write", "post", json={})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.request("/api/push-stats").status_code, 401)

    def test_first_password_setup_creates_authenticated_session(self):
        self.config.write_text("{}")
        state = self.request("/api/auth/status").get_json()
        self.assertTrue(state["need_setup"])
        response = self.request("/api/auth/set-password", "post", json={
            "password": "new-test-password", "password_confirm": "new-test-password",
            "setup_code": ui_app._setup_codes.read()})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.request("/api/auth/status").get_json()["authenticated"])
        self.assertNotIn("new-test-password", self.config.read_text())

    def test_secondary_pages_and_images_require_valid_session_after_timeout(self):
        self.gateway()
        self.login()
        self.now += 900
        for path in ["/history", "/support", "/faq"]:
            response = self.request(path)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], self.prefix + "/")
        self.assertEqual(self.request("/support/img/wechat_pay.jpg").status_code, 403)


if __name__ == "__main__":
    unittest.main()
