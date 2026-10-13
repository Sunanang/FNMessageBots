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

root = Path(__file__).resolve().parents[1]
fpk = root if (root / "cmd/main").is_file() else root.parent / "FNMessageBotForFPK"


@unittest.skipUnless((fpk / "cmd/main").is_file(), "需要 FPK 项目脚本")
class FpkLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.data = self.base / "FnMessageBot"
        self.data.mkdir()
        self.env = {**os.environ, "TRIM_PKGVAR": str(self.data), "TRIM_USERNAME": getpass.getuser(), "TRIM_APPNAME": "FnMessageBot", "TRIM_APPDEST": str(self.base / "install")}

    def test_recorded_pid_is_python_and_stop_removes_pid_file(self):
        # NAS 存储路径可能带空格或 $；这些字符必须作为路径本身传给 Python。
        self.data = self.base / "storage $literal" / "FnMessageBot"
        self.data.mkdir(parents=True)
        self.env["TRIM_PKGVAR"] = str(self.data)
        install = self.base / "install"
        code = install / "cmd/fnmessagebots"
        (code / "src").mkdir(parents=True)
        (code / "requirements.txt").write_text("")
        (code / "src/main.py").write_text("import os,signal,sys,time\nfrom pathlib import Path\nPath(os.environ['APP_HOME'],'reported.port').write_text(os.environ['UI_PORT'])\nPath(os.environ['APP_HOME'],'reported.pid').write_text(str(os.getpid()))\nsignal.signal(signal.SIGTERM,lambda *_: sys.exit(0))\nwhile True: time.sleep(.1)\n")
        main = install / "cmd/main"
        shutil.copy2(fpk / "cmd/main", main)
        shutil.copy2(fpk / "cmd/ui_port", main.parent / "ui_port")
        (self.data / "config").mkdir()
        (self.data / "config/ui_port").write_text("19080\n")
        (self.data / "venv/bin").mkdir(parents=True)
        (self.data / "venv/bin/python").symlink_to(sys.executable)
        try:
            subprocess.run(["bash", str(main), "start"], env=self.env, check=True, capture_output=True, timeout=5)
            for _ in range(50):
                if (self.data / "reported.pid").exists():
                    break
                time.sleep(.02)
            self.assertEqual((self.data / "app.pid").read_text(), (self.data / "reported.pid").read_text())
            self.assertEqual((self.data / "reported.port").read_text(), "19080")
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

    def run_port_helper(self, function, *args, extra_env=None):
        script = 'fail_user_visible() { printf "%s\\n" "$1"; exit 1; }; source "$1"; shift; "$@"'
        return subprocess.run(["bash", "-c", script, "test", str(fpk / "cmd/ui_port"), function, *args], env={**self.env, **(extra_env or {})}, capture_output=True, text=True, timeout=5)

    def test_legacy_install_without_port_uses_default(self):
        result = self.run_port_helper("read_fnmb_ui_port")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "18230")
        self.assertFalse((self.data / "config/ui_port").exists())

    def test_invalid_ports_do_not_replace_saved_port(self):
        result = self.run_port_helper("save_fnmb_ui_port", "19080")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for value in ("", "0", "1023", "65536", "9999999999999", "18080; touch injected", "$(touch injected)", "18 080", "19080\n19081"):
            with self.subTest(value=value):
                result = self.run_port_helper("save_fnmb_ui_port", value)
                self.assertEqual(result.returncode, 1)
                self.assertIn("1024～65535", result.stdout)
                self.assertEqual((self.data / "config/ui_port").read_text(), "19080\n")

    def test_corrupt_saved_port_is_reported(self):
        (self.data / "config").mkdir()
        (self.data / "config/ui_port").write_text("invalid\n")
        result = self.run_port_helper("read_fnmb_ui_port")
        self.assertEqual(result.returncode, 1)
        self.assertIn("端口配置无效", result.stdout)

    def test_install_callback_saves_wizard_port_and_upgrade_preserves_it(self):
        install = self.base / "install"
        code = install / "cmd/fnmessagebots"
        (code / "src").mkdir(parents=True)
        (code / "config").mkdir()
        (code / "src/main.py").write_text("")
        (code / "requirements.txt").write_text("")
        (code / "config/config.json").write_text("{}")
        for name in ("install_callback", "upgrade_callback", "data_permissions", "ui_port"):
            shutil.copy2(fpk / "cmd" / name, install / "cmd" / name)
        # 仅跳过 pip 网络操作，其余完整运行真实安装、升级回调。
        (self.data / "venv/bin").mkdir(parents=True)
        python = self.data / "venv/bin/python"
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
        result = subprocess.run(["bash", str(install / "cmd/install_callback")], env={**self.env, "wizard_ui_port": "19080"}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.data / "config/ui_port").read_text(), "19080\n")
        result = subprocess.run(["bash", str(install / "cmd/upgrade_callback")], env={**self.env, "wizard_ui_port": "18230"}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.data / "config/ui_port").read_text(), "19080\n")
        self.assertEqual((self.data / "config/ui_port").stat().st_mode & 0o777, 0o600)

    def test_wizard_rejects_ports_outside_normal_user_range(self):
        wizard = json.loads((fpk / "wizard/install").read_text())
        field = next(item for step in wizard for item in step["items"] if item.get("field") == "wizard_ui_port")
        pattern = next(rule["pattern"] for rule in field["rules"] if "pattern" in rule)
        for value in ("1024", "1099", "1100", "18230", "19080", "65535"):
            self.assertIsNotNone(re.fullmatch(pattern, value), value)
        for value in ("", "1023", "0", "65536", "99999", "18080.0", "port"):
            self.assertIsNone(re.fullmatch(pattern, value), value)


if __name__ == "__main__":
    unittest.main()
