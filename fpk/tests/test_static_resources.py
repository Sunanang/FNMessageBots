"""验证 FPK 安装目录与数据目录分离时，静态图片和网关路径仍可访问。"""
import importlib.util
import json
import os
import shutil
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

from web import ui_app
from web.access_guard import GatewayPrefixMiddleware
from web.auth_service import apply_new_password


class StaticResourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.installed = self.base / "installed/fnmessagebots"
        self.data = self.base / "var"
        self.data.mkdir()
        self.env = patch.dict(os.environ, {"APP_HOME": str(self.data)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        paths_file = self.installed / "src/web/app_paths.py"
        paths_file.parent.mkdir(parents=True)
        shutil.copy2(source / "web/app_paths.py", paths_file)
        shutil.copytree(source.parent / "assets", self.installed / "assets")
        spec = importlib.util.spec_from_file_location("fnmb_test_resource_paths", paths_file)
        assert spec is not None and spec.loader is not None
        self.paths = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.paths)
        config = self.paths.CONFIG_FILE
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(apply_new_password({}, "temporary-test-password")))
        for key in ["BASE_DIR", "CONFIG_FILE", "ASSETS_DIR", "ICON_FILE", "SUPPORT_QR_DIR"]:
            manager = patch.object(ui_app, key, getattr(self.paths, key))
            manager.start()
            self.addCleanup(manager.stop)
        self.app = ui_app.create_app()
        self.client = self.app.test_client()
        self.admin = {"X-Trim-Userid": "1", "X-Trim-Isadmin": "true"}

    def gateway(self):
        self.app.wsgi_app = GatewayPrefixMiddleware(self.app.wsgi_app, "/app/FnMessageBot")
        self.assertEqual(self.client.post("/app/FnMessageBot/api/auth/login", headers=self.admin,
                                         json={"password": "temporary-test-password"}).status_code, 200)

    def test_resources_come_from_installation_while_config_stays_in_data_dir(self):
        self.assertEqual(self.paths.BASE_DIR, self.data)
        self.assertEqual(self.paths.CONFIG_FILE, self.data / "config/config.json")
        self.assertEqual(self.paths.ASSETS_DIR, self.installed / "assets")
        self.assertFalse((self.data / "assets").exists())
        self.assertTrue(self.paths.ICON_FILE.is_file())
        self.assertTrue((self.paths.SUPPORT_QR_DIR / "wechat_pay.jpg").is_file())

    def test_direct_logged_in_support_links_load_real_jpegs(self):
        with patch.object(ui_app, "_is_authenticated", return_value=True):
            response = self.client.get("/support")
            self.assertEqual(response.status_code, 200)
            for name in ["wechat_pay.jpg", "ali_pay.jpg"]:
                url = "/support/img/" + name
                self.assertIn('src="' + url + '"', response.data.decode())
                image = self.client.get(url)
                self.addCleanup(image.close)
                self.assertEqual(image.status_code, 200)
                self.assertEqual(image.mimetype, "image/jpeg")
                self.assertEqual(image.data, (self.paths.SUPPORT_QR_DIR / name).read_bytes())

    def test_gateway_support_links_include_prefix_and_load_images(self):
        self.gateway()
        response = self.client.get("/app/FnMessageBot/support", headers=self.admin)
        self.assertEqual(response.status_code, 200)
        for name in ["wechat_pay.jpg", "ali_pay.jpg"]:
            url = "/app/FnMessageBot/support/img/" + name
            self.assertIn('src="' + url + '"', response.data.decode())
            image = self.client.get(url, headers=self.admin)
            self.addCleanup(image.close)
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.data, (self.paths.SUPPORT_QR_DIR / name).read_bytes())

    def test_app_icon_and_favicon_load_from_packaged_assets(self):
        self.gateway()
        for url in ["/app/FnMessageBot/assets/icons/app-icon.png", "/app/FnMessageBot/favicon.ico"]:
            image = self.client.get(url, headers=self.admin)
            self.addCleanup(image.close)
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.mimetype, "image/png")
            self.assertEqual(image.data, self.paths.ICON_FILE.read_bytes())

    def test_support_images_remain_protected_without_login(self):
        self.assertEqual(self.client.get("/support").status_code, 302)
        self.assertEqual(self.client.get("/support/img/wechat_pay.jpg").status_code, 403)

    def test_gateway_non_admin_cannot_access_support_page_or_images(self):
        self.gateway()
        headers = {"X-Trim-Userid": "2", "X-Trim-Isadmin": "false"}
        for url in ["/app/FnMessageBot/support", "/app/FnMessageBot/support/img/wechat_pay.jpg"]:
            self.assertEqual(self.client.get(url, headers=headers).status_code, 403)

    def test_support_image_route_still_enforces_filename_allowlist(self):
        self.gateway()
        response = self.client.get("/app/FnMessageBot/support/img/config.json", headers=self.admin)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
