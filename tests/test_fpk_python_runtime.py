"""2.9.0 的 Python 运行时回归：商店运行时优先、离线依赖轮子齐备、不联网。"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FPK = ROOT / "fpk"
WHEELS = ROOT / "vendor/wheels"


def manifest_values(text):
    values = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def requirement_names():
    names = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        if ";" in line:
            marker = line.split(";", 1)[1]
            # 只给旧 Python 用的依赖（如 dataclasses; python_version < '3.7'）不需要轮子
            if "python_version <" in marker:
                continue
        line = line.split(";")[0].strip()
        name = re.split(r"[<>=!~\[]", line)[0].strip()
        if name:
            names.append(name.lower().replace("-", "_"))
    return names


@unittest.skipUnless((FPK / "manifest").is_file(), "需要 FPK 项目脚本")
class FpkPythonRuntimeTests(unittest.TestCase):
    def source_env(self, script, env):
        return subprocess.run(
            ["bash", "-c", 'source "$1"; shift; eval "$@"', "test", str(FPK / "cmd/python_env"), script],
            env={**os.environ, **env}, capture_output=True, text=True, timeout=10)

    def test_manifest_uses_store_python_and_official_links(self):
        values = manifest_values((FPK / "manifest").read_text(encoding="utf-8"))
        self.assertEqual(values.get("install_dep_apps"), "python312")
        self.assertEqual(values.get("maintainer"), "Lando")
        self.assertEqual(values.get("maintainer_url"), "https://www.lndo.top/")
        self.assertEqual(values.get("distributor"), "Lando")
        self.assertEqual(values.get("distributor_url"), "https://www.fnmb.lndo.top/")

    def test_store_python_is_preferred_and_system_python_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix = Path(temp) / "store"
            (prefix / "bin").mkdir(parents=True)
            store = prefix / "bin/python3"
            store.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            store.chmod(0o755)
            result = self.source_env("fnmb_python_cmd", {"FNMB_STORE_PYTHON_PREFIX": str(prefix)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(store))

            result = self.source_env("fnmb_python_cmd", {"FNMB_STORE_PYTHON_PREFIX": str(Path(temp) / "gone")})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip().endswith("python3"), result.stdout)

    def test_dependencies_prefer_bundled_wheels_over_the_network(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "app"
            (app / "wheels").mkdir(parents=True)
            (app / "wheels/demo-1.0-py3-none-any.whl").write_bytes(b"x")
            log = Path(temp) / "python-args.txt"
            python = Path(temp) / "python3"
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "' + str(log) + '"\nexit 0\n', encoding="utf-8")
            python.chmod(0o755)
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; FNMB_APP_CODE_DIR="$2"; fnmb_install_requirements "$3" "$4" "$5"',
                 "test", str(FPK / "cmd/python_env"), str(app), str(python),
                 str(ROOT / "requirements.txt"), str(Path(temp) / "target")],
                env={**os.environ, "PATH": "/usr/bin:/bin", "FNMB_STORE_PYTHON_PREFIX": str(Path(temp) / "none")},
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8")
            self.assertIn("-m pip install", calls)
            self.assertIn("--no-index", calls)
            self.assertIn("--find-links", calls)

    def test_bundled_wheels_cover_every_requirement_for_both_architectures(self):
        wheels = sorted(p.name for p in WHEELS.glob("*.whl"))
        self.assertTrue(wheels, "vendor/wheels 为空，安装包会退回联网装依赖")
        normalised = [name.split("-")[0].lower().replace("-", "_") for name in wheels]
        for requirement in requirement_names():
            self.assertIn(requirement, normalised, f"缺少 {requirement} 的轮子")
        for arch in ("x86_64", "aarch64"):
            self.assertTrue(any(arch in name and name.startswith("psutil") for name in wheels),
                            f"缺少 {arch} 的 psutil 轮子")

    def test_install_scripts_use_shared_runtime_helper(self):
        for name in ("install_callback", "upgrade_callback", "main"):
            text = (FPK / "cmd" / name).read_text(encoding="utf-8")
            self.assertIn('source "$(dirname "${BASH_SOURCE[0]}")/python_env"', text, name)
            self.assertIn("fnmb_prepend_python_path", text, name)
        for name in ("install_callback", "upgrade_callback"):
            text = (FPK / "cmd" / name).read_text(encoding="utf-8")
            self.assertIn("fnmb_install_requirements", text, name)

    def test_runtime_launcher_prefers_recorded_interpreter_and_target_modules(self):
        text = (FPK / "cmd/main").read_text(encoding="utf-8")
        self.assertIn(".fnmb_python", text)
        self.assertIn("python_modules", text)
        self.assertIn("fnmb_venv_site_packages", text)


if __name__ == "__main__":
    unittest.main()
