"""SSH 服务开关（Started/Stopped ssh.service）解析单测。"""
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))

from monitor.ssh_journal_poller import (
    SSH_DISABLED,
    SSH_DISCONNECTED,
    SSH_ENABLED,
    SSH_LOGIN_SUCCESS,
    parse_ssh_journal_message,
)


class SshServiceSwitchParseTests(unittest.TestCase):
    def test_started_ssh_service(self):
        parsed = parse_ssh_journal_message("Started ssh.service")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        et, data = parsed
        self.assertEqual(et, SSH_ENABLED)
        self.assertEqual(data.get("from"), "systemd")
        self.assertEqual(data.get("unit"), "ssh")

    def test_stopped_ssh_service(self):
        parsed = parse_ssh_journal_message("Stopped ssh.service")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        et, data = parsed
        self.assertEqual(et, SSH_DISABLED)
        self.assertEqual(data.get("unit"), "ssh")

    def test_started_sshd_service_variant(self):
        parsed = parse_ssh_journal_message("Started sshd.service - OpenBSD Secure Shell server.")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        et, data = parsed
        self.assertEqual(et, SSH_ENABLED)
        self.assertEqual(data.get("unit"), "sshd")

    def test_ignores_starting_only(self):
        # Starting 仍忽略；Stopping / signal 15 视为关闭（部分环境无最终 Stopped 行）
        self.assertIsNone(parse_ssh_journal_message("Starting ssh.service"))

    def test_stopping_and_signal15_as_disabled(self):
        for msg in (
            "Stopping ssh.service",
            "Stopped OpenBSD Secure Shell server.",
            "Received signal 15; terminating.",
        ):
            parsed = parse_ssh_journal_message(msg)
            self.assertIsNotNone(parsed, msg)
            assert parsed is not None
            et, _ = parsed
            self.assertEqual(et, SSH_DISABLED, msg)

    def test_message_id_stopped(self):
        from monitor.ssh_journal_poller import parse_ssh_journal_entry

        parsed = parse_ssh_journal_entry(
            {
                "MESSAGE": "",
                "MESSAGE_ID": "9d1aaa27d60140bd96365438aad20286",
                "UNIT": "ssh.service",
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        et, data = parsed
        self.assertEqual(et, SSH_DISABLED)
        self.assertEqual(data.get("via"), "message_id")

    def test_login_still_parsed(self):
        parsed = parse_ssh_journal_message(
            "Accepted publickey for alice from 192.168.1.8 port 22 ssh2"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        et, data = parsed
        self.assertEqual(et, SSH_LOGIN_SUCCESS)
        self.assertEqual(data.get("user"), "alice")
        self.assertEqual(data.get("IP"), "192.168.1.8")

    def test_disconnect_still_parsed(self):
        parsed = parse_ssh_journal_message(
            "Disconnected from user bob 10.0.0.2 port 51234"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        et, _ = parsed
        self.assertEqual(et, SSH_DISCONNECTED)


if __name__ == "__main__":
    unittest.main()
