"""忽略局域网 SSH 来源判定单测。"""
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))

from monitor.ssh_journal_poller import (
    SSH_AUTH_FAILED,
    SSH_DISCONNECTED,
    SSH_LOGIN_SUCCESS,
    is_lan_ip,
    is_loopback_ip,
    should_ignore_ssh_by_source,
)


class SshIgnoreLanTests(unittest.TestCase):
    def test_lan_private_ranges(self):
        self.assertTrue(is_lan_ip("192.168.1.8"))
        self.assertTrue(is_lan_ip("192.168.1.254"))
        self.assertTrue(is_lan_ip("10.0.0.5"))
        self.assertTrue(is_lan_ip("172.16.3.1"))
        self.assertFalse(is_lan_ip("8.8.8.8"))
        self.assertFalse(is_lan_ip("127.0.0.1"))  # 回环不算局域网开关范围
        self.assertFalse(is_lan_ip("not-an-ip"))

    def test_loopback_separate(self):
        self.assertTrue(is_loopback_ip("127.0.0.1"))
        self.assertTrue(is_loopback_ip("::1"))
        self.assertFalse(is_loopback_ip("192.168.1.1"))

    def test_ignore_lan_only_quiet_events(self):
        ed = {"IP": "192.168.1.10"}
        self.assertTrue(
            should_ignore_ssh_by_source(
                SSH_LOGIN_SUCCESS, ed, ignore_loopback=False, ignore_lan=True
            )
        )
        self.assertTrue(
            should_ignore_ssh_by_source(
                SSH_DISCONNECTED, ed, ignore_loopback=False, ignore_lan=True
            )
        )
        self.assertFalse(
            should_ignore_ssh_by_source(
                SSH_AUTH_FAILED, ed, ignore_loopback=False, ignore_lan=True
            )
        )

    def test_public_ip_not_ignored_by_lan_switch(self):
        ed = {"IP": "8.8.8.8"}
        self.assertFalse(
            should_ignore_ssh_by_source(
                SSH_LOGIN_SUCCESS, ed, ignore_loopback=True, ignore_lan=True
            )
        )

    def test_loopback_needs_loopback_switch(self):
        ed = {"IP": "127.0.0.1"}
        self.assertFalse(
            should_ignore_ssh_by_source(
                SSH_LOGIN_SUCCESS, ed, ignore_loopback=False, ignore_lan=True
            )
        )
        self.assertTrue(
            should_ignore_ssh_by_source(
                SSH_LOGIN_SUCCESS, ed, ignore_loopback=True, ignore_lan=False
            )
        )


if __name__ == "__main__":
    unittest.main()
