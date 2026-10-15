"""开发分支与发布来源保护；使用临时 Git 仓库，不访问远端。"""
import copy
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("require_dev_merge", ROOT / "scripts/require-dev-merge.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class ReleaseSourceTests(unittest.TestCase):
    def setUp(self):
        self.repository = "Sunanang/FNMessageBots"
        self.revision = "a" * 40
        self.pr = {
            "number": 1,
            "merged_at": "2026-10-15T09:00:00Z",
            "merge_commit_sha": self.revision,
            "head": {"ref": "dev", "repo": {"full_name": self.repository}},
            "base": {"ref": "main", "repo": {"full_name": self.repository}},
        }

    def test_accepts_same_repository_dev_merge_in_later_api_page(self):
        invalid = copy.deepcopy(self.pr)
        invalid["head"]["ref"] = "feature"
        result = policy.matching_dev_merge([[invalid], [self.pr]], self.repository, self.revision)
        self.assertIs(result, self.pr)

    def test_direct_main_commit_without_pull_request_cannot_publish(self):
        with self.assertRaisesRegex(ValueError, "dev → main"):
            policy.matching_dev_merge([], self.repository, self.revision)

    def test_unmerged_pr_and_old_merge_commit_cannot_authorize_release(self):
        for field, value in (("merged_at", None), ("merge_commit_sha", "b" * 40)):
            with self.subTest(field=field):
                invalid = copy.deepcopy(self.pr)
                invalid[field] = value
                with self.assertRaises(ValueError):
                    policy.matching_dev_merge([invalid], self.repository, self.revision)

    def test_wrong_branch_base_and_fork_named_dev_cannot_publish(self):
        for side, field, value in (
            ("head", "ref", "feature"), ("base", "ref", "dev"),
            ("head", "repo", {"full_name": "someone/FNMessageBots"}),
            ("base", "repo", {"full_name": "someone/FNMessageBots"}), ("head", "repo", None),
        ):
            with self.subTest(side=side, field=field, value=value):
                invalid = copy.deepcopy(self.pr)
                invalid[side][field] = value
                with self.assertRaises(ValueError):
                    policy.matching_dev_merge([invalid], self.repository, self.revision)

    def test_cli_returns_failure_for_direct_push_and_success_for_valid_merge(self):
        for items, expected in (([], 1), ([[self.pr]], 0)):
            result = subprocess.run(
                [shutil.which("python3"), str(ROOT / "scripts/require-dev-merge.py"), self.repository, self.revision],
                input=json.dumps(items), capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)


class GitHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        shutil.copytree(ROOT / ".githooks", self.repo / ".githooks")
        self.git("init", "-b", "main")
        self.git("config", "core.hooksPath", ".githooks")

    def git(self, *args, check=True):
        return subprocess.run(
            ["git", "-c", "user.name=Branch policy test", "-c", "user.email=branch-policy@example.invalid", *args],
            cwd=self.repo, capture_output=True, text=True, check=check, timeout=5,
        )

    def test_real_commit_is_blocked_on_main_but_allowed_on_dev(self):
        (self.repo / "example.txt").write_text("development change")
        self.git("add", "example.txt")
        result = self.git("commit", "-m", "must be blocked", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dev", result.stderr)
        self.git("switch", "-c", "dev")
        self.git("commit", "-m", "allowed development")
        self.assertEqual(self.git("branch", "--show-current").stdout.strip(), "dev")

    def test_push_cannot_target_main_even_from_dev_or_for_deletion(self):
        revision = "a" * 40
        cases = (
            (f"refs/heads/dev {revision} refs/heads/dev {'0' * 40}\n", 0),
            (f"refs/heads/main {revision} refs/heads/main {revision}\n", 1),
            (f"refs/heads/dev {revision} refs/heads/main {revision}\n", 1),
            (f"(delete) {'0' * 40} refs/heads/main {revision}\n", 1),
            (f"refs/tags/latest {revision} refs/tags/latest {revision}\n", 0),
        )
        for updates, expected in cases:
            with self.subTest(updates=updates):
                result = subprocess.run(
                    [str(self.repo / ".githooks/pre-push")], cwd=self.repo,
                    input=updates, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
