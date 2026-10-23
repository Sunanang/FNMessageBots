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
from web.auth_service import apply_new_password, hash_password, normalize_password, password_matches


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
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["authenticated"])
        self.assertFalse(payload["need_login"])
        self.assertEqual(payload["remaining_seconds"], 900)
        self.assertTrue(payload.get("session_token"))
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

    def test_mobile_keyboard_variants_match_saved_password(self):
        def fullwidth(text):
            return "".join(chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" else ch for ch in text)

        for label, password in {
            "capital-first": "Test-application-password",
            "fullwidth": fullwidth("test-application-password"),
            "zero-width": "test-application-password\u200b",
            "ideographic-space": "\u3000test-application-password\u3000",
            "exact": "test-application-password",
        }.items():
            with self.subTest(label=label):
                response = self.request("/api/auth/login", "post", json={"password": password})
                self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.request("/api/auth/login", "post", json={"password": "test-application-passworD"}).status_code, 401)
        self.assertEqual(self.request("/api/auth/login", "post", json={"password": "Test-application-passworD"}).status_code, 401)

    def test_legacy_exact_password_still_matches_after_normalization(self):
        salt = "ab" * 16
        legacy = hash_password("nas'pass", bytes.fromhex(salt))
        self.assertTrue(password_matches("nas'pass", salt, legacy))
        self.assertTrue(password_matches("nas\u2019pass", salt, legacy))
        self.assertFalse(password_matches("nas'passx", salt, legacy))
        stored = hash_password("Test-application-password", bytes.fromhex(salt))
        self.assertTrue(password_matches("Test-application-password", salt, stored))
        self.assertFalse(password_matches("test-application-password", salt, stored))
        self.assertEqual(normalize_password(" ｔｅｓｔ\u200b "), "test")

    def test_setup_treats_fullwidth_confirmation_as_the_same_password(self):
        self.config.write_text("{}")
        self.assertTrue(self.request("/api/auth/status").get_json()["need_setup"])
        fullwidth = "".join(chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" else ch for ch in "new-test-password")
        response = self.request("/api/auth/set-password", "post", json={
            "password": fullwidth,
            "password_confirm": "new-test-password",
            "setup_code": ui_app._setup_codes.read(),
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.request("/api/auth/logout", "post", json={}).status_code, 200)
        self.assertEqual(self.request("/api/auth/login", "post", json={"password": "new-test-password"}).status_code, 200)

    def test_login_form_keeps_mobile_keyboard_from_rewriting_password(self):
        html = (root / "src/web/templates/index.html").read_text(encoding="utf-8")
        login = html.split('id="login-password"', 1)[1].split(">", 1)[0]
        self.assertIn('autocapitalize="none"', login)
        self.assertIn('autocorrect="off"', login)
        self.assertIn('spellcheck="false"', login)
        self.assertIn("font-size: 16px", html)
        self.assertIn("readSecret", html)
        self.assertIn("enterAuthenticatedSession", html)
        login_handler = html.split('id="form-login"', 1)[1].split("function setStatus", 1)[0]
        self.assertIn("enterAuthenticatedSession(json)", login_handler)
        self.assertNotIn("initAuth();", login_handler)

    def test_session_header_authenticates_without_cookie(self):
        for gateway in [False, True]:
            with self.subTest(gateway=gateway):
                if gateway:
                    self.gateway()
                token = self.login().get_json()["session_token"]
                self.client.delete_cookie(ui_app.AUTH_COOKIE_NAME, path=(self.prefix + "/" if self.prefix else "/"))
                self.assertFalse(self.request("/api/auth/status").get_json()["authenticated"])
                headers = {**self.headers, ui_app.AUTH_SESSION_HEADER: token}
                status = self.client.get(self.prefix + "/api/auth/status", headers=headers).get_json()
                self.assertTrue(status["authenticated"])
                self.assertEqual(self.client.get(self.prefix + "/api/push-stats", headers=headers).status_code, 200)

    def test_invalid_password_does_not_create_session(self):
        response = self.request("/api/auth/login", "post", json={"password": "wrong-password"})
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.client.get_cookie(ui_app.AUTH_COOKIE_NAME, path=self.prefix + "/"))
        self.assertEqual(self.request("/api/push-stats").status_code, 401)

    def test_legacy_disable_auth_cannot_bypass_password(self):
        with patch.dict(os.environ, {"FNMB_DISABLE_AUTH": "1"}):
            self.assertFalse(self.request("/api/auth/status").get_json()["authenticated"])
            self.assertEqual(self.request("/api/push-stats").status_code, 401)

    def save_password_mode(self, enabled):
        payload = {"events": ["LoginSucc"], "channels": [
            {"type": "wechat", "url": "https://example.invalid/webhook", "enabled": True}]}
        if enabled is not ...:
            payload["web_password_enabled"] = enabled
        with patch.object(ui_app, "_collect_external_db_access_warnings", return_value=[]):
            return self.request("/api/save-config", "post", json=payload)

    def test_password_mode_can_only_be_disabled_after_login(self):
        original = self.config.read_text()
        self.assertEqual(self.save_password_mode(False).status_code, 401)
        self.assertEqual(self.config.read_text(), original)
        self.assertTrue(self.request("/api/auth/status").get_json()["password_enabled"])

    def test_password_mode_survives_idle_restart_and_restores_original_password(self):
        for gateway in [False, True]:
            with self.subTest(gateway=gateway):
                if gateway:
                    self.gateway()
                self.login()
                original = json.loads(self.config.read_text())
                self.assertEqual(self.save_password_mode(False).status_code, 200)
                self.now += 3600
                # 全新应用和客户端模拟重启、浏览器重开，没有应用会话 Cookie。
                self.app = ui_app.create_app()
                if gateway:
                    self.app.wsgi_app = GatewayPrefixMiddleware(self.app.wsgi_app, self.prefix)
                self.client = self.app.test_client()
                state = self.request("/api/auth/status").get_json()
                self.assertTrue(state["authenticated"])
                self.assertFalse(state["password_enabled"])
                self.assertFalse(state["need_login"])
                self.assertIsNone(state["remaining_seconds"])
                self.assertEqual(self.request("/support").status_code, 200)
                self.assertEqual(self.request("/api/push-stats").status_code, 200)
                self.assertEqual(self.request("/api/auth/activity", "post", json={}).status_code, 200)
                config = self.request("/api/config?check_access=0").get_json()["data"]
                self.assertFalse(config["web_password_enabled"])
                self.assertNotIn("web_password_hash", config)
                self.assertEqual(self.save_password_mode(True).status_code, 200)
                self.assertEqual(self.request("/api/push-stats").status_code, 401)
                saved = json.loads(self.config.read_text())
                self.assertEqual(saved["web_password_hash"], original["web_password_hash"])
                self.assertEqual(saved["web_password_salt"], original["web_password_salt"])
                self.login()
                self.now += 900
                self.assertEqual(self.request("/api/push-stats").status_code, 401)

    def test_mode_changes_revoke_other_browsers_old_sessions(self):
        self.login()
        old_cookie = self.client.get_cookie(ui_app.AUTH_COOKIE_NAME).value
        gateway_id = session_service.create_session(scope="/app/FnMessageBot/")
        self.assertEqual(self.save_password_mode(False).status_code, 200)
        self.assertEqual(self.save_password_mode(True).status_code, 200)
        self.client.set_cookie(ui_app.AUTH_COOKIE_NAME, old_cookie)
        self.assertEqual(self.request("/api/push-stats").status_code, 401)
        self.gateway()
        self.client.set_cookie(ui_app.AUTH_COOKIE_NAME, gateway_id, path=self.prefix + "/")
        self.assertEqual(self.request("/api/push-stats").status_code, 401)

    def test_mode_omitted_by_older_clients_is_preserved_and_invalid_values_rejected(self):
        self.login()
        self.assertEqual(self.save_password_mode(False).status_code, 200)
        self.assertEqual(self.save_password_mode(...).status_code, 200)
        self.assertFalse(json.loads(self.config.read_text())["web_password_enabled"])
        for value in ["false", "true", None, 0, [], {}]:
            with self.subTest(value=value):
                self.assertEqual(self.save_password_mode(value).status_code, 400)
                self.assertFalse(json.loads(self.config.read_text())["web_password_enabled"])

    def test_old_opt_out_is_restored_and_gateway_non_admin_stays_denied(self):
        raw = json.loads(self.config.read_text())
        raw["web_password_enabled"] = False
        self.config.write_text(json.dumps(raw))
        self.assertTrue(self.request("/api/auth/status").get_json()["authenticated"])
        self.gateway()
        self.headers["X-Trim-Isadmin"] = "false"
        for path in ["/", "/api/auth/status", "/api/config", "/support"]:
            self.assertEqual(self.request(path).status_code, 403)

    def test_failed_mode_save_does_not_disable_password_or_revoke_session(self):
        self.login()
        with patch.object(ui_app, "_save_raw_config", side_effect=OSError("read only")):
            self.assertEqual(self.save_password_mode(False).status_code, 500)
        state = self.request("/api/auth/status").get_json()
        self.assertTrue(state["password_enabled"])
        self.assertTrue(state["authenticated"])
        self.assertEqual(self.app.test_client().get("/api/push-stats").status_code, 401)

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
