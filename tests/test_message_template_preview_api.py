"""模板预览接口：草稿为 null 时与空串同义，不能渲染出字面量 "None"。"""
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

from web import ui_app  # noqa: E402
from web.access_guard import LoginRateLimiter, SetupCodeStore  # noqa: E402
from web.auth_service import apply_new_password  # noqa: E402


class MessageTemplatePreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps(apply_new_password({}, "preview-password")))
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
        self.client = self.app.test_client()
        login = self.client.post("/api/auth/login", json={"password": "preview-password"})
        self.assertEqual(login.status_code, 200)

    def preview(self, payload):
        result = self.client.post("/api/message-template/preview", json=payload)
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        return result.get_json()["data"]

    def test_null_draft_is_treated_as_empty_not_literal_none(self):
        data = self.preview({"event_type": "LoginSucc", "title": None, "body": None})
        self.assertNotIn("None", data["title"])
        self.assertNotIn("None", data["body"])

    def test_null_draft_matches_empty_draft(self):
        null_draft = self.preview({"event_type": "LoginSucc", "title": None, "body": None})
        empty_draft = self.preview({"event_type": "LoginSucc", "title": "", "body": ""})
        self.assertEqual(null_draft["title"], empty_draft["title"])
        self.assertEqual(null_draft["body"], empty_draft["body"])

    def test_real_draft_still_renders(self):
        data = self.preview({"event_type": "LoginSucc", "title": "T {{event_title}}", "body": "x"})
        self.assertIn("T ", data["title"])
        self.assertIn("登录成功", data["title"])


if __name__ == "__main__":
    unittest.main()
