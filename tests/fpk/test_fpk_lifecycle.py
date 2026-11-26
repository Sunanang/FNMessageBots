"""在临时安装目录验证 FPK 进程生命周期和迁移提示，不修改 NAS 权限。"""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[2]
fpk = root / "fpk"


@unittest.skipUnless((fpk / "cmd/main").is_file(), "需要 FPK 项目脚本")
class FpkLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.data = self.base / "FnMessageBot"
        self.data.mkdir()
        # 商店 Python 的替身：真的解释器照常跑，只有 pip 安装直接返回成功（测试不联网）
        self.stub_prefix = self.base / "store-python"
        (self.stub_prefix / "bin").mkdir(parents=True)
        stub = self.stub_prefix / "bin/python3"
        stub.write_text(
            '#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n    exit 0\nfi\nexec "'
            + sys.executable + '" "$@"\n')
        stub.chmod(0o755)
        self.env = {**os.environ, "TRIM_PKGVAR": str(self.data), "TRIM_USERNAME": getpass.getuser(), "TRIM_APPNAME": "FnMessageBot", "TRIM_APPDEST": str(self.base / "install"), "FNMB_STORE_PYTHON_PREFIX": str(self.stub_prefix)}

    def test_recorded_pid_is_python_and_stop_removes_pid_file(self):
        # NAS 存储路径可能带空格或 $；这些字符必须作为路径本身传给 Python。
        self.data = self.base / "storage $literal" / "FnMessageBot"
        self.data.mkdir(parents=True)
        self.env["TRIM_PKGVAR"] = str(self.data)
        install = self.base / "install"
        code = install / "cmd/fnmessagebots"
        (code / "src").mkdir(parents=True)
        (code / "requirements.txt").write_text("")
        # 启动脚本不再导出 UI_PORT：记录环境变量内容，应为空
        (code / "src/main.py").write_text("import os,signal,sys,time\nfrom pathlib import Path\nPath(os.environ['APP_HOME'],'reported.env').write_text(os.environ.get('UI_PORT',''))\nPath(os.environ['APP_HOME'],'reported.pid').write_text(str(os.getpid()))\nsignal.signal(signal.SIGTERM,lambda *_: sys.exit(0))\nwhile True: time.sleep(.1)\n")
        main = install / "cmd/main"
        shutil.copy2(fpk / "cmd/main", main)
        shutil.copy2(fpk / "cmd/python_env", main.parent / "python_env")
        (self.data / "venv/bin").mkdir(parents=True)
        (self.data / "venv/bin/python").symlink_to(sys.executable)
        try:
            subprocess.run(["bash", str(main), "start"], env=self.env, check=True, capture_output=True, timeout=5)
            for _ in range(50):
                if (self.data / "reported.pid").exists():
                    break
                time.sleep(.02)
            self.assertEqual((self.data / "app.pid").read_text(), (self.data / "reported.pid").read_text())
            self.assertEqual((self.data / "reported.env").read_text(), "")
            subprocess.run(["bash", str(main), "stop"], env=self.env, check=True, capture_output=True, timeout=15)
            self.assertFalse((self.data / "app.pid").exists())
            status = subprocess.run(["bash", str(main), "status"], env=self.env, capture_output=True, timeout=5)
            self.assertEqual(status.returncode, 3)
        finally:
            subprocess.run(["bash", str(main), "stop"], env=self.env, capture_output=True, timeout=15)

    def run_permissions(self, extra_env=None):
        env = {**self.env, **(extra_env or {})}
        script = 'fail_user_visible() { printf "%s\\n" "$1"; exit 1; }; source "$1"; prepare_app_data_permissions'
        return subprocess.run(["bash", "-c", script, "test", str(fpk / "cmd/data_permissions")], env=env, capture_output=True, text=True, timeout=5)

    def test_existing_normal_user_data_passes_preflight(self):
        (self.data / "config").mkdir()
        (self.data / "config/config.json").write_text("{}")
        result = self.run_permissions()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_root_owned_data_produces_command_before_installing_dependencies(self):
        stubs = self.base / "stubs"
        stubs.mkdir()
        fake_find = stubs / "find"
        fake_find.write_text('#!/bin/sh\nprintf "%s\\n" "$2/config/config.json"\n')
        fake_find.chmod(0o755)
        result = self.run_permissions({"PATH": str(stubs) + os.pathsep + self.env["PATH"]})
        self.assertEqual(result.returncode, 1)
        self.assertIn("sudo chown -hR -P --", result.stdout)
        self.assertIn(str(self.data), result.stdout)
        self.assertIn("停止应用", result.stdout)

    def test_permission_helper_rejects_unexpected_directory(self):
        result = self.run_permissions({"TRIM_PKGVAR": str(self.base)})
        self.assertEqual(result.returncode, 1)
        self.assertIn("拒绝修改权限", result.stdout)

    def test_root_variant_preflight_does_not_require_package_account(self):
        cmd = self.base / "root-cmd"
        cmd.mkdir()
        shutil.copy2(fpk / "cmd/data_permissions", cmd / "data_permissions")
        (cmd / "fpk-variant").write_text("root\n")
        stubs = self.base / "root-stubs"
        stubs.mkdir()
        (stubs / "id").write_text('#!/bin/sh\nif [ "$1" = "-u" ]; then echo 0; elif [ "$2" = root ]; then echo rootgroup; else exit 1; fi\n')
        (stubs / "chown").write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CHOWN_CAPTURE"\n')
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        capture = self.base / "chown-args"
        result = subprocess.run(["bash", "-c",
            'fail_user_visible() { echo "$1"; exit 1; }; source "$1"; prepare_app_data_permissions',
            "test", str(cmd / "data_permissions")], env={
                **self.env, "TRIM_USERNAME": "missing-package-account", "CHOWN_CAPTURE": str(capture),
                "PATH": str(stubs) + os.pathsep + self.env["PATH"],
            }, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("root:rootgroup", capture.read_text())

    def test_launcher_and_callbacks_no_longer_depend_on_tcp_port(self):
        # FPK 只走飞牛网关 Socket：启动脚本与回调不得再读写端口配置，也不得导出 UI_PORT
        for name in ("main", "install_callback", "upgrade_callback"):
            text = (fpk / "cmd" / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertNotIn("ui_port", text)
                self.assertNotIn("UI_PORT", text)
                self.assertNotIn("wizard_ui_port", text)
        self.assertFalse((fpk / "cmd/ui_port").exists())

    def test_install_callback_sets_password_without_any_port(self):
        install = self.base / "install"
        code = install / "cmd/fnmessagebots"
        (code / "src").mkdir(parents=True)
        (code / "config").mkdir()
        (code / "src/main.py").write_text("")
        (code / "requirements.txt").write_text("")
        (code / "config/config.json").write_text("{}")
        for name in ("install_callback", "upgrade_callback", "data_permissions", "python_env"):
            shutil.copy2(fpk / "cmd" / name, install / "cmd" / name)
        # 依赖安装走商店 Python 替身（pip 直接成功），其余完整运行真实安装、升级回调。
        result = subprocess.run(["bash", str(install / "cmd/install_callback")], env={
            **self.env, "wizard_web_password": "first-password-123",
            "wizard_web_password_confirm": "first-password-123",
        }, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.data / "config/ui_port").exists())
        import hashlib
        configured = json.loads((self.data / "config/config.json").read_text())
        self.assertEqual(configured["web_password_hash"], hashlib.pbkdf2_hmac(
            "sha256", b"first-password-123", bytes.fromhex(configured["web_password_salt"]), 100000).hex())
        self.assertNotIn("first-password-123", (self.data / "config/config.json").read_text())
        result = subprocess.run(["bash", str(install / "cmd/upgrade_callback")], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.data / "config/ui_port").exists())

    def test_wizard_asks_only_for_access_password(self):
        wizard = json.loads((fpk / "wizard/install").read_text())
        fields = [item.get("field") for step in wizard for item in step["items"] if item.get("field")]
        self.assertNotIn("wizard_ui_port", fields)
        self.assertEqual(fields, ["wizard_web_password", "wizard_web_password_confirm"])
        password = next(item for step in wizard for item in step["items"] if item.get("field") == "wizard_web_password")
        self.assertTrue(any("8" in rule.get("message", "") for rule in password["rules"]))

    def test_reinstall_preserves_config_but_applies_wizard_password(self):
        import hashlib
        import sqlite3
        install = self.base / "install"
        code = install / "cmd/fnmessagebots"
        (code / "src").mkdir(parents=True)
        (code / "config").mkdir()
        (code / "src/main.py").write_text("")
        (code / "requirements.txt").write_text("")
        (code / "config/config.json").write_text('{"monitor_events":["LoginFail"]}')
        for name in ("install_callback", "upgrade_callback", "data_permissions", "python_env"):
            shutil.copy2(fpk / "cmd" / name, install / "cmd" / name)
        (self.data / "config").mkdir()
        original = {"monitor_events": ["LoginSucc"], "wechat_webhook_url": "https://example.invalid/keep",
                    "web_password_salt": "old-salt", "web_password_hash": "old-hash", "web_password_enabled": False}
        (self.data / "config/config.json").write_text(json.dumps(original))
        (self.data / "config/ui_preferences.json").write_text('{"theme":"dark"}')
        cursor = self.data / "data/cursor"
        cursor.mkdir(parents=True)
        (cursor / "push_stats.json").write_text('{"total":{"success":123,"fail":4},"daily":{}}')
        for name in ("push_history.db", "notification_outbox.db"):
            with sqlite3.connect(cursor / name) as conn:
                conn.execute("CREATE TABLE saved (value TEXT)")
                conn.execute("INSERT INTO saved VALUES ('keep me')")
        log = self.data / "data/logs/old.log"
        log.parent.mkdir()
        log.write_text("keep log\n")
        # 2.9.0 起依赖统一放 python_modules，旧版 venv 会在安装/升级时被主动清掉，不计入「必须原样保留」
        (self.data / "venv/bin").mkdir(parents=True)
        (self.data / "venv/bin/python").write_text("#!/bin/sh\nexit 0\n")
        (self.data / "venv/bin/python").chmod(0o755)
        preserve = {
            p.relative_to(self.data): p.read_bytes()
            for p in self.data.rglob("*")
            if p.is_file() and "venv" not in p.parts and p.name != "config.json"
        }

        # 覆盖安装：向导密码必须写入；推送配置与历史数据保留
        result = subprocess.run(["bash", str(install / "cmd/install_callback")], env={
            **self.env, "wizard_web_password": "new-password-123",
            "wizard_web_password_confirm": "new-password-123",
        }, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path, contents in preserve.items():
            self.assertEqual((self.data / path).read_bytes(), contents, str(path))
        configured = json.loads((self.data / "config/config.json").read_text())
        self.assertEqual(configured["monitor_events"], ["LoginSucc"])
        self.assertEqual(configured["wechat_webhook_url"], "https://example.invalid/keep")
        self.assertTrue(configured["web_password_enabled"])
        self.assertEqual(
            configured["web_password_hash"],
            hashlib.pbkdf2_hmac(
                "sha256", b"new-password-123", bytes.fromhex(configured["web_password_salt"]), 100000
            ).hex(),
        )
        self.assertNotEqual(configured["web_password_hash"], "old-hash")
        self.assertNotIn("new-password-123", (self.data / "config/config.json").read_text())
        self.assertFalse((self.data / "venv").exists(), "旧版 venv 应被清理")

        # 升级回调：即使环境里有向导密码变量，也不改密码（升级向导不收集密码）
        before_upgrade = (self.data / "config/config.json").read_bytes()
        result = subprocess.run(["bash", str(install / "cmd/upgrade_callback")], env={
            **self.env, "wizard_web_password": "another-password-999",
            "wizard_web_password_confirm": "another-password-999",
        }, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.data / "config/config.json").read_bytes(), before_upgrade)


if __name__ == "__main__":
    unittest.main()
