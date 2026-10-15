"""发布回归：所有 registry/GitHub 命令均替身，不发布真实产物。"""
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_fpk", ROOT / "scripts/verify-fpk.py")
verify_fpk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_fpk)


class FpkBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        files = {
            "src/main.py": "print('current source')\n",
            "requirements.txt": "Flask>=3.0.0\n",
            "assets/icons/app-icon.png": "current icon",
            "fpk/manifest": "appname = FnMessageBot\nversion = 2.6.0\nplatform = all\n",
            "fpk/cmd/main": "#!/bin/sh\nexit 0\n",
            "fpk/cmd/fnmessagebots/config/config.json": "{}",
            "fpk/cmd/fnmessagebots/requirements.txt": "obsolete requirements",
            "fpk/cmd/fnmessagebots/assets/icons/app-icon.png": "obsolete icon",
            "fpk/cmd/fnmessagebots/src/local-debug.py": "preserve local work",
            "fpk/app/ui/config": '{"gatewayPrefix":"/app/FnMessageBot"}',
        }
        for name, text in files.items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        (self.repo / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/build-fpk.sh", self.repo / "scripts/build-fpk.sh")
        self.tool = self.base / "fnpack"
        self.tool.write_text(f"#!{sys.executable}\n" + '''
import io, os, sys, tarfile
from pathlib import Path
payload = Path(sys.argv[sys.argv.index('--directory') + 1])
Path(os.environ['CAPTURE']).write_text(str(payload))
if os.environ.get('FAIL_FNPACK'):
    sys.exit(42)
app = io.BytesIO()
with tarfile.open(fileobj=app, mode='w:gz') as archive:
    archive.add(payload / 'app', arcname='.')
with tarfile.open('FnMessageBot.fpk', mode='w:gz') as archive:
    for path in payload.rglob('*'):
        if path.is_file() and not path.relative_to(payload).as_posix().startswith('app/'):
            archive.add(path, arcname=path.relative_to(payload).as_posix())
    data = app.getvalue()
    member = tarfile.TarInfo('app.tgz')
    member.size = len(data)
    archive.addfile(member, io.BytesIO(data))
''')
        self.tool.chmod(0o755)
        self.capture = self.base / "payload-path"
        self.env = {**os.environ, "FNPACK": str(self.tool), "CAPTURE": str(self.capture)}

    def build(self, **env):
        return subprocess.run(["bash", str(self.repo / "scripts/build-fpk.sh")], env={**self.env, **env},
                              capture_output=True, text=True, timeout=10)

    def test_build_uses_current_source_assets_and_dependencies_without_touching_checkout(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        package = self.repo / "dist/FnMessageBot-2.6.0.fpk"
        metadata = verify_fpk.verify_package(package, self.repo)
        self.assertEqual(metadata["platform"], "all")
        self.assertEqual((self.repo / "fpk/cmd/fnmessagebots/src/local-debug.py").read_text(), "preserve local work")
        self.assertFalse(Path(self.capture.read_text()).parent.exists())
        with tarfile.open(package) as archive:
            self.assertNotIn("cmd/fnmessagebots/src/local-debug.py", archive.getnames())

    def test_fnpack_failure_cleans_staging_and_keeps_local_source(self):
        result = self.build(FAIL_FNPACK="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(Path(self.capture.read_text()).parent.exists())
        self.assertTrue((self.repo / "fpk/cmd/fnmessagebots/src/local-debug.py").is_file())
        self.assertFalse((self.repo / "dist").exists())

    def test_verifier_rejects_stale_source_or_wrong_checksum(self):
        self.assertEqual(self.build().returncode, 0)
        package = self.repo / "dist/FnMessageBot-2.6.0.fpk"
        (self.repo / "src/main.py").write_text("newer source")
        with self.assertRaisesRegex(ValueError, "src/main.py"):
            verify_fpk.verify_package(package, self.repo)
        Path(str(package) + ".sha256").write_text("0" * 64 + "  " + package.name)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            verify_fpk.verify_package(package, self.repo)


@unittest.skipUnless(shutil.which("jq"), "Docker tag publication requires jq")
class DockerPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.log = self.base / "docker.jsonl"
        docker = self.base / "docker"
        docker.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
args = sys.argv[1:]
with open(os.environ['DOCKER_COMMANDS'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
mode = os.environ.get('FAIL_MODE', '')
if 'inspect' in args:
    ref = args[args.index('inspect') + 1]
    arm = ref.endswith('-arm64') or ref.endswith('b' * 64)
    if '--raw' in args:
        arches = ['amd64'] if mode == 'bad-index' else ['amd64', 'arm64']
        print(json.dumps({'manifests': [{'platform': {'os': 'linux', 'architecture': a}} for a in arches]}))
    elif args[-1] == '{{.Manifest.Digest}}':
        if mode == 'unavailable' and arm:
            sys.exit(1)
        print('invalid' if mode == 'bad-digest' else 'sha256:' + ('b' if arm else 'a') * 64)
    else:
        arch = 'amd64' if arm and mode == 'wrong-arch' else ('arm64' if arm else 'amd64')
        revision = '0' * 40 if arm and mode == 'wrong-commit' else os.environ['REVISION']
        print(json.dumps({'os': 'linux', 'architecture': arch, 'config': {'Labels': {'org.opencontainers.image.revision': revision}}}))
''')
        docker.chmod(0o755)

    def publish(self, mode=""):
        return subprocess.run(["bash", str(ROOT / "scripts/publish-docker-tags.sh"), "sunanang/fn-message-bots", "a" * 40],
                              capture_output=True, text=True, timeout=5, env={
                                  **os.environ, "PATH": str(self.base) + os.pathsep + os.environ["PATH"],
                                  "DOCKER_COMMANDS": str(self.log), "REVISION": "a" * 40, "FAIL_MODE": mode,
                              })

    def mutations(self):
        return [json.loads(line) for line in self.log.read_text().splitlines() if '"create"' in line]

    def test_publishes_both_arch_tags_and_combined_index_from_verified_digests(self):
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mutations = self.mutations()
        self.assertEqual([args[args.index("--tag") + 1] for args in mutations], [
            "sunanang/fn-message-bots:latest-amd64", "sunanang/fn-message-bots:latest-arm64", "sunanang/fn-message-bots:latest",
        ])
        self.assertEqual(mutations[-1][-2:], ["sunanang/fn-message-bots@sha256:" + "a" * 64, "sunanang/fn-message-bots@sha256:" + "b" * 64])

    def test_bad_or_unavailable_image_never_replaces_latest(self):
        for mode in ("wrong-arch", "wrong-commit", "unavailable", "bad-digest"):
            with self.subTest(mode=mode):
                self.log.write_text("")
                self.assertNotEqual(self.publish(mode).returncode, 0)
                self.assertEqual(self.mutations(), [])

    def test_verification_detects_single_arch_latest(self):
        self.assertNotEqual(self.publish("bad-index").returncode, 0)


@unittest.skipUnless(shutil.which("sha256sum"), "FPK publication requires sha256sum")
class FpkPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "scripts").mkdir()
        (self.base / "dist").mkdir()
        shutil.copy2(ROOT / "scripts/publish-fpk.sh", self.base / "scripts/publish-fpk.sh")
        self.package = self.base / "dist/FnMessageBot-2.6.0.fpk"
        self.package.write_bytes(b"verified FPK")
        Path(str(self.package) + ".sha256").write_text(hashlib.sha256(self.package.read_bytes()).hexdigest() + "  " + self.package.name + "\n")
        self.log = self.base / "gh.jsonl"
        gh = self.base / "gh"
        gh.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
with open(os.environ['GH_COMMANDS'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['release', 'view'] and not os.environ.get('EXISTING_RELEASE'):
    sys.exit(1)
''')
        gh.chmod(0o755)

    def publish(self, existing=False):
        return subprocess.run(["bash", str(self.base / "scripts/publish-fpk.sh")], capture_output=True, text=True, timeout=5, env={
            **os.environ, "PATH": str(self.base) + os.pathsep + os.environ["PATH"], "GH_COMMANDS": str(self.log),
            "GITHUB_SHA": "a" * 40, "GH_REPO": "Sunanang/FNMessageBots", "EXISTING_RELEASE": "1" if existing else "",
        })

    def test_first_release_and_rerun_have_matching_stable_alias_and_checksum(self):
        for existing in (False, True):
            result = self.publish(existing)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            alias = self.base / "dist/FnMessageBot-latest.fpk"
            self.assertEqual(alias.read_bytes(), self.package.read_bytes())
            self.assertEqual(Path(str(alias) + ".sha256").read_text().split(), [hashlib.sha256(alias.read_bytes()).hexdigest(), alias.name])
        commands = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(any(args[:2] == ["release", "create"] for args in commands))
        self.assertTrue(any(args[:3] == ["api", "--method", "PATCH"] for args in commands))

    def test_invalid_checksum_never_calls_github(self):
        self.package.write_bytes(b"corrupted FPK")
        self.assertNotEqual(self.publish().returncode, 0)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
