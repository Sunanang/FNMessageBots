"""SSH 开启超时：事件勾选 + 分钟数。"""
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))

from config import Config


class SshOpenTimeoutConfigTests(unittest.TestCase):
    def test_enabled_requires_event_and_minutes(self):
        c = Config.__new__(Config)
        c.monitor_events = ["SSH_OPEN_TIMEOUT"]
        c.ssh_open_remind_minutes = 30
        c.ssh_open_timeout_by_event = True
        self.assertTrue(c.ssh_open_timeout_enabled())

        c.monitor_events = ["SSH_LOGIN_SUCCESS"]
        self.assertFalse(c.ssh_open_timeout_enabled())

        c.monitor_events = ["SSH_OPEN_TIMEOUT"]
        c.ssh_open_remind_minutes = 0
        self.assertFalse(c.ssh_open_timeout_enabled())

    def test_migrate_minutes_on_adds_event(self):
        c = Config.__new__(Config)
        c.monitor_events = ["SSH_LOGIN_SUCCESS"]
        c.ssh_open_remind_minutes = 45
        c.ssh_open_timeout_by_event = False
        c._migrate_ssh_open_timeout_event()
        self.assertIn("SSH_OPEN_TIMEOUT", c.monitor_events)
        self.assertEqual(c.ssh_open_remind_minutes, 45)
        self.assertTrue(c.ssh_open_timeout_by_event)

    def test_migrate_minutes_zero_stays_off(self):
        c = Config.__new__(Config)
        c.monitor_events = ["SSH_LOGIN_SUCCESS", "SSH_OPEN_TIMEOUT"]
        c.ssh_open_remind_minutes = 0
        c.ssh_open_timeout_by_event = False
        c._migrate_ssh_open_timeout_event()
        self.assertNotIn("SSH_OPEN_TIMEOUT", c.monitor_events)
        self.assertEqual(c.ssh_open_remind_minutes, 30)
        self.assertTrue(c.ssh_open_timeout_by_event)

    def test_migrate_skips_when_flag_set(self):
        c = Config.__new__(Config)
        c.monitor_events = ["SSH_LOGIN_SUCCESS"]
        c.ssh_open_remind_minutes = 30
        c.ssh_open_timeout_by_event = True
        c._migrate_ssh_open_timeout_event()
        self.assertNotIn("SSH_OPEN_TIMEOUT", c.monitor_events)


if __name__ == "__main__":
    unittest.main()
