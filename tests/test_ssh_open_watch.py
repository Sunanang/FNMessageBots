"""SSH 开启超时提醒单元测试（按间隔反复提醒直至关闭）。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))

from monitor.ssh_journal_poller import SSH_DISABLED, SSH_ENABLED
from monitor.ssh_open_watch import SshOpenWatch, _ssh_service_active


class SshOpenWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.minutes = {"v": 1}
        self.calls = []

        def on_timeout(ed, entry):
            self.calls.append((ed, entry))

        self.watch = SshOpenWatch(
            self.temp.name,
            get_remind_minutes=lambda: int(self.minutes["v"]),
            on_timeout=on_timeout,
        )

    def tearDown(self):
        self.watch.stop()
        self.temp.cleanup()

    def test_timeout_fires_after_deadline(self):
        self.minutes["v"] = 30
        self.watch.on_enabled()
        with self.watch._lock:
            opened = time.time() - 31 * 60
            self.watch._opened_at = opened
            self.watch._next_due_at = opened + 30 * 60
            self.watch._save_state_locked()
            self.watch._reschedule_locked(reason="test")
        deadline = time.time() + 2.0
        while time.time() < deadline and not self.calls:
            time.sleep(0.05)
        self.assertEqual(len(self.calls), 1)
        ed, entry = self.calls[0]
        self.assertEqual(ed.get("remind_minutes"), 30)
        self.assertGreaterEqual(int(ed.get("elapsed_minutes") or 0), 30)
        self.assertTrue(entry.message)
        with self.watch._lock:
            self.assertIsNotNone(self.watch._next_due_at)
            self.assertGreater(float(self.watch._next_due_at), time.time() - 1)

    def test_repeats_until_disabled(self):
        """同一开启会话应按间隔再次提醒。"""
        import monitor.ssh_open_watch as sow

        self.minutes["v"] = 30
        with patch.object(sow, "_ssh_service_active", return_value=True):
            self.watch.on_enabled()
            with self.watch._lock:
                opened = time.time() - 61 * 60
                self.watch._opened_at = opened
                due1 = time.time() - 1
                self.watch._next_due_at = due1
                self.watch._save_state_locked()
            self.watch._fire_timeout(opened_at=opened, due_at=due1, minutes=30)
            self.assertEqual(len(self.calls), 1)
            with self.watch._lock:
                due2 = time.time() - 0.5
                self.watch._next_due_at = due2
                self.watch._save_state_locked()
            self.watch._fire_timeout(opened_at=opened, due_at=due2, minutes=30)
            self.assertEqual(len(self.calls), 2)
            self.assertNotEqual(
                self.calls[0][0].get("_source_cursor"),
                self.calls[1][0].get("_source_cursor"),
            )
            with self.watch._lock:
                self.assertGreater(float(self.watch._next_due_at), time.time())

    def test_disabled_cancels_pending(self):
        self.minutes["v"] = 60
        self.watch.on_service_event(SSH_ENABLED, {})
        self.watch.on_service_event(SSH_DISABLED, {})
        time.sleep(0.2)
        self.assertEqual(self.calls, [])
        with self.watch._lock:
            self.assertIsNone(self.watch._opened_at)
            self.assertIsNone(self.watch._next_due_at)

    def test_zero_minutes_disables(self):
        self.minutes["v"] = 0
        self.watch.on_enabled()
        time.sleep(0.2)
        self.assertEqual(self.calls, [])
        with self.watch._lock:
            self.assertIsNone(self.watch._timer)

    def test_disabled_during_fire_skips_push(self):
        """模拟 timer 已判定超时、推送前 SSH 已关闭：不应推送。"""
        self.minutes["v"] = 30
        self.watch.on_enabled()
        opened = None
        due = None
        with self.watch._lock:
            opened = float(self.watch._opened_at)
            self.watch._opened_at = opened - 31 * 60
            due = time.time() - 1
            self.watch._next_due_at = due
            self.watch._save_state_locked()

        self.watch.on_disabled()
        self.watch._fire_timeout(opened_at=opened - 31 * 60, due_at=due, minutes=30)
        self.assertEqual(self.calls, [])

    def test_stop_during_callback_does_not_rearm_timer(self):
        with patch("monitor.ssh_open_watch.threading.Timer") as timer, patch(
            "monitor.ssh_open_watch._ssh_service_active", return_value=True
        ):
            self.watch.on_enabled()
            opened, due = self.watch._opened_at, self.watch._next_due_at
            self.watch._on_timeout = lambda *_: self.watch.stop()
            timer.reset_mock()
            self.watch._fire_timeout(opened_at=opened, due_at=due, minutes=1)
            timer.assert_not_called()
            self.assertIsNone(self.watch._timer)
            self.watch._fire_timeout(opened_at=opened, due_at=due, minutes=1)
            self.assertEqual(self.calls, [])

    def test_stopped_watch_ignores_already_queued_timeout(self):
        with patch("monitor.ssh_open_watch.threading.Timer"), patch(
            "monitor.ssh_open_watch._ssh_service_active", return_value=True
        ):
            self.watch.on_enabled()
            opened, due = self.watch._opened_at, self.watch._next_due_at
            self.watch.stop()
            self.watch._fire_timeout(opened_at=opened, due_at=due, minutes=1)
            self.assertEqual(self.calls, [])

    def test_systemctl_failure_is_unknown_not_confirmed_inactive(self):
        result = Mock(returncode=1, stdout="", stderr="System has not been booted with systemd")
        with patch("monitor.ssh_open_watch.subprocess.run", return_value=result):
            self.assertIsNone(_ssh_service_active())

    def test_inactive_unit_is_confirmed_and_active_alias_wins(self):
        inactive = Mock(returncode=3, stdout="inactive\n", stderr="")
        active = Mock(returncode=0, stdout="active\n", stderr="")
        with patch("monitor.ssh_open_watch.subprocess.run", return_value=inactive):
            self.assertFalse(_ssh_service_active())
        with patch("monitor.ssh_open_watch.subprocess.run", side_effect=[inactive, active]):
            self.assertTrue(_ssh_service_active())


if __name__ == "__main__":
    unittest.main()
