"""Flask 配置接口回归：仅读写临时配置，热加载回调用 Mock，不启动监控。"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

root = Path(__file__).resolve().parents[1]
source = root / "src"
if not source.is_dir():
    source = root / "cmd/fnmessagebots/src"
sys.path.insert(0, str(source))


@unittest.skipUnless(importlib.util.find_spec("flask"), "接口回归需要安装项目运行依赖")
class WebAccessIntegrationTests(unittest.TestCase):
    def setUp(self):
        from web import ui_app
        from web import access_diagnostics
        self.ui = ui_app
        self.diagnostics = access_diagnostics
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "config.json"
        self.db = self.base / "logger.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE log(id INTEGER)")
        from web.auth_service import apply_new_password
        self.config.write_text(json.dumps(apply_new_password(
            {"monitor_events": ["LoginSucc"], "logger_db_path": str(self.db)}, "integration-password")))
        for manager in (
            patch.dict(os.environ, {"FNMB_GATEWAY_SOCKET": "/mock/app.sock"}, clear=True),
            patch.object(ui_app, "CONFIG_FILE", self.config),
            patch.object(ui_app, "BASE_DIR", self.base),
            patch.object(access_diagnostics, "in_container", return_value=False),
        ):
            manager.start()
            self.addCleanup(manager.stop)
        self.callback = Mock()
        self.app = ui_app.create_app(on_config_saved=self.callback)
        self.client = self.app.test_client()
        self.assertEqual(self.client.post("/api/auth/login", json={"password": "integration-password"}).status_code, 200)

    def save(self, events=None, patrol_enabled=False):
        return self.client.post("/api/save-config", json={
            "events": events or ["LoginSucc"],
            "channels": [{"type": "wechat", "url": "https://example.invalid/webhook", "enabled": True}],
            "nas_patrol_enabled": patrol_enabled, "nas_patrol_cron": "0 12 * * *",
        })

    def test_save_persists_configuration_even_with_source_permission_warning(self):
        with patch.object(self.diagnostics.os, "access", side_effect=lambda p, mode: Path(p) != self.db):
            response = self.save()
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["warning_details"][0]["code"], "permission")
        self.assertIn("FnMessageBot", body["warning_details"][0]["commands"])
        self.assertIsInstance(body["warnings"][0], str)
        self.assertEqual(json.loads(self.config.read_text())["monitor_events"], ["LoginSucc"])
        self.callback.assert_called_once()

    def test_readable_sources_and_unused_default_cron_have_no_warning(self):
        response = self.save()
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["warnings"], [])
        self.assertEqual(body["warning_details"], [])

    def test_load_reports_current_access_issue(self):
        with patch.object(self.diagnostics.os, "access", side_effect=lambda p, mode: Path(p) != self.db):
            body = self.client.get("/api/config").get_json()
        self.assertEqual(body["warning_details"][0]["code"], "permission")

    def test_save_returns_combined_smart_authorization_commands(self):
        from monitor import nas_patrol as patrol
        from types import SimpleNamespace
        from utils import access_guidance
        with patch.object(patrol, "_read_system_version", return_value="Debian"), \
             patch.object(patrol, "_read_fnos_version", return_value="FnOS"), \
             patch.object(patrol, "_patrol_list_visible_whole_disks", return_value=["nvme0n1", "sda", "sdb"]) as disks, \
             patch.object(patrol, "_hwmon_temp1_for_block", return_value="38") as hwmon, \
             patch.object(patrol, "_resolve_cmd", side_effect=lambda name: {"smartctl": "/usr/sbin/smartctl", "setcap": "/usr/sbin/setcap", "setfacl": "/usr/bin/setfacl"}[name]), \
             patch.object(access_guidance, "in_container", return_value=False), \
             patch.object(self.diagnostics, "_run_readonly", return_value=SimpleNamespace(
                 returncode=2, stdout="Smartctl open device failed: Permission denied", stderr="")):
            # 使用真实事件目录与页面提交方式：普通事件 + 独立巡检开关。
            loaded = self.client.get("/api/config").get_json()
            ids = {e["id"] for category in loaded["data"]["events_by_category"] for e in category["events"]}
            self.assertNotIn("NAS_PATROL_REPORT", ids)
            disabled = self.save(patrol_enabled=False).get_json()
            self.assertEqual(disabled["warning_details"], [])
            enabled = self.save(patrol_enabled=True).get_json()
            reloaded = self.client.get("/api/config").get_json()
            self.smart_save_fixture = {"data": reloaded["data"], "save": enabled}
            self.save(patrol_enabled=False)
            with patch.object(self.ui, "get_runtime_monitor_app", return_value=Mock()), \
                 patch.object(patrol, "run_nas_patrol_now", return_value=(True, "巡检推送已发送")):
                manual = self.client.post("/api/nas-patrol/run", json={}).get_json()
            for label, body in [("保存", enabled), ("重新打开", reloaded), ("手动巡检", manual)]:
                with self.subTest(route=label):
                    self.assertTrue(body["ok"])
                    self.assertEqual(len(body["warning_details"]), 1)
                    warning = body["warning_details"][0]
                    self.assertEqual(warning["code"], "permission")
                    self.assertEqual(warning["faq"], "faq-patrol")
                    self.assertIn("cap_sys_rawio,cap_sys_admin+ep", warning["commands"])
                    self.assertIn("/dev/nvme0n1 /dev/sda /dev/sdb", warning["commands"])
                    self.assertIn("SMART", body["warnings"][0])
            self.assertNotIn("NAS_PATROL_REPORT", json.loads(self.config.read_text())["monitor_events"])
            disks.return_value = ["sda", "sdb", "sdc", "sdd", "sde"]
            hwmon.return_value = ""
            arm = self.save(patrol_enabled=True).get_json()
            self.assertTrue(arm["ok"])
            self.assertEqual(len(arm["warning_details"]), 1)
            self.assertIn("温度也会显示 --", arm["warning_details"][0]["message"])
            self.assertIn("cap_sys_rawio+ep", arm["warning_details"][0]["commands"])
            self.assertNotIn("cap_sys_admin", arm["warning_details"][0]["commands"])
            self.smart_save_fixture = {"data": reloaded["data"], "save": arm}

    def test_arm_save_warns_before_grant_and_clears_after_actual_smart_success(self):
        from monitor import nas_patrol as patrol
        from types import SimpleNamespace
        disks = ["sda", "sdb", "sdc", "sdd", "sde"]
        devices = ["/dev/" + disk for disk in disks]
        original_exists = Path.exists
        original_access = os.access
        paths = {"smartctl": "/usr/sbin/smartctl", "setcap": "/usr/sbin/setcap", "setfacl": "/usr/bin/setfacl"}
        readings = []
        for disk, temp in zip(disks, [40, 33, 35, 35, 36]):
            readings.append(SimpleNamespace(
                returncode=32 if disk == "sdb" else 0, stderr="", stdout=(
                    "SMART overall-health self-assessment test result: PASSED\n"
                    f"194 Temperature_Celsius 0x0022 100 058 000 Old_age Always - {temp} (0 20 0 0 0)\n")))
        with patch.object(patrol, "_read_system_version", return_value="Debian"), \
             patch.object(patrol, "_read_fnos_version", return_value="FnOS"), \
             patch.object(patrol, "_patrol_list_visible_whole_disks", return_value=disks), \
             patch.object(patrol, "_hwmon_temp1_for_block", return_value=""), \
             patch.object(patrol, "_resolve_cmd", side_effect=lambda name: paths[name]), \
             patch.object(Path, "exists", lambda path: str(path) in devices + list(paths.values()) or original_exists(path)), \
             patch.object(self.diagnostics.os, "access", side_effect=lambda path, mode: False if str(path) in devices else original_access(path, mode)) as access, \
             patch.object(self.diagnostics, "_run_readonly", side_effect=readings * 2) as probe:
            denied = self.save(patrol_enabled=True).get_json()
            self.assertTrue(denied["ok"])
            self.assertEqual(len(denied["warning_details"]), 1)
            warning = denied["warning_details"][0]
            self.assertEqual(warning["code"], "permission")
            self.assertIn("温度也会显示 --", warning["message"])
            self.assertIn("sudo /usr/sbin/setcap cap_sys_rawio+ep /usr/sbin/smartctl", warning["commands"])
            self.assertIn("sudo /usr/bin/setfacl -m u:FnMessageBot:r -- " + " ".join(devices), warning["commands"])
            self.assertNotIn("cap_sys_admin", warning["commands"])
            probe.assert_not_called()
            # 模拟用户执行命令后，应用账号实际可读取全部五块盘。
            access.side_effect = lambda path, mode: True if str(path) in devices else original_access(path, mode)
            for body in [self.save(patrol_enabled=True).get_json(), self.client.get("/api/config").get_json()]:
                self.assertTrue(body["ok"])
                self.assertEqual(body["warnings"], [])
                self.assertEqual(body["warning_details"], [])
            self.assertEqual(probe.call_count, 10)

    def test_post_save_form_refresh_does_not_probe_hardware_again(self):
        with patch.object(self.ui, "_collect_external_db_access_warnings", side_effect=AssertionError("duplicate probe")):
            response = self.client.get("/api/config?check_access=0")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(response.get_json()["warning_details"], [])

    def test_incompatible_scheduler_schema_returns_no_permission_commands(self):
        raw = json.loads(self.config.read_text())
        raw["scheduler_db_path"] = str(self.db)
        self.config.write_text(json.dumps(raw))
        body = self.save(["SCHEDULER_TASK_SUCCESS"]).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["warning_details"][0]["code"], "schema")
        self.assertEqual(body["warning_details"][0]["commands"], "")

    def test_reload_failure_preserves_saved_config_and_diagnostics(self):
        self.callback.side_effect = RuntimeError("mock reload failure")
        with patch.object(self.diagnostics.os, "access", side_effect=lambda p, mode: Path(p) != self.db):
            body = self.save().get_json()
        self.assertTrue(body["ok"])
        self.assertIn("热加载失败", body["message"])
        self.assertEqual(body["warning_details"][0]["code"], "permission")
        self.assertTrue(json.loads(self.config.read_text())["push_channels"])

    def test_save_failure_is_not_reported_as_success(self):
        with patch.object(self.ui, "_save_raw_config", side_effect=PermissionError("mock config permission")):
            response = self.save()
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.get_json()["ok"])
        self.callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
