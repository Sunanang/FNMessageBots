"""SSH 空闲监听恢复回归，不启动 SSH 或发送通知。"""
import json
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

from monitor.ssh_journal_poller import SSH_LOGIN_SUCCESS, SshJournalPoller


class SshJournalResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.poller = SshJournalPoller(self.temp.name, monitor_events=[SSH_LOGIN_SUCCESS])

    def align_empty(self):
        state = self.poller._load_state()
        with patch("monitor.ssh_journal_poller.time.time", return_value=1700000000.123456), \
             patch.object(self.poller, "_run_journalctl", return_value=(0, "", "")):
            self.poller._align_cursor(state)
        self.assertTrue(state["aligned"])
        self.assertEqual(state["since"], "@1700000000.123456")
        return state

    def test_empty_polls_keep_the_original_start_time(self):
        state = self.align_empty()
        with patch.object(self.poller, "_run_journalctl", return_value=(0, "", "")) as run:
            self.poller._poll_once(state)
            self.poller._poll_once(state)
        for call in run.call_args_list:
            args = call.args[0]
            self.assertEqual(args[args.index("--since") + 1], "@1700000000.123456")
            self.assertNotIn("-n", args)
        self.assertEqual(state["cursor"], "")

    def test_first_login_after_idle_is_emitted_then_uses_cursor(self):
        state = self.align_empty()
        handler = Mock()
        self.poller.add_handler(SSH_LOGIN_SUCCESS, handler)
        entry = json.dumps({
            "MESSAGE": "Accepted password for alice from 192.168.1.8 port 54321 ssh2",
            "__CURSOR": "first-login",
            "__REALTIME_TIMESTAMP": "1700000001123456",
        })
        with patch.object(self.poller, "_run_journalctl", side_effect=[
            (0, entry + "\n-- cursor: first-login\n", ""),
            (0, "-- cursor: first-login\n", ""),
        ]) as run:
            self.poller._poll_once(state)
            self.poller._poll_once(state)
        handler.assert_called_once()
        self.assertEqual(handler.call_args.args[0]["user"], "alice")
        self.assertEqual(handler.call_args.args[0]["IP"], "192.168.1.8")
        self.assertIn("--since", run.call_args_list[0].args[0])
        self.assertIn("--after-cursor", run.call_args_list[1].args[0])
        self.assertEqual(state["cursor"], "first-login")
        self.assertEqual(self.poller._load_state()["cursor"], "first-login")

    def test_read_failure_does_not_move_idle_start_time(self):
        state = self.align_empty()
        with patch.object(self.poller, "_run_journalctl", return_value=(124, "", "timeout")):
            self.poller._poll_once(state)
        self.assertEqual(state["since"], "@1700000000.123456")
        self.assertEqual(state["cursor"], "")

    def test_legacy_empty_state_gets_a_fixed_start_time_once(self):
        state = {"version": 1, "aligned": True, "cursor": ""}
        with patch("monitor.ssh_journal_poller.time.time", side_effect=[1700000000.0]), \
             patch.object(self.poller, "_run_journalctl", return_value=(0, "", "")):
            self.poller._poll_once(state)
            self.poller._poll_once(state)
        self.assertEqual(self.poller._load_state()["since"], "@1700000000.000000")

    def test_restart_replaces_idle_baseline_to_skip_downtime_history(self):
        original = self.align_empty()
        self.poller.running = True

        def stop_after_poll(state):
            self.assertNotEqual(state["since"], original["since"])
            self.assertEqual(state["since"], "@1700001000.000000")
            self.poller.running = False

        with patch("monitor.ssh_journal_poller.time.time", return_value=1700001000.0), \
             patch.object(self.poller, "_run_journalctl", return_value=(0, "", "")), \
             patch.object(self.poller, "_poll_once", side_effect=stop_after_poll):
            self.poller._run_loop()

    def test_restart_with_old_cursor_waits_for_successful_realignment(self):
        self.poller._save_state({"version": 1, "aligned": True, "cursor": "old-cursor"})
        self.poller.running = True

        def stop_after_poll(state):
            self.assertEqual(state["cursor"], "current-tail")
            self.poller.running = False

        with patch.object(self.poller, "_run_journalctl", side_effect=[
            (124, "", "timeout"), (0, "-- cursor: current-tail\n", "")
        ]) as run, patch.object(self.poller, "_poll_once", side_effect=stop_after_poll) as poll, \
             patch("monitor.ssh_journal_poller.time.sleep", side_effect=lambda _: poll.assert_not_called()):
            self.poller._run_loop()
        self.assertEqual(run.call_count, 2)
        poll.assert_called_once()

    def test_existing_cursor_still_skips_history(self):
        state = self.poller._load_state()
        with patch.object(self.poller, "_run_journalctl", return_value=(0, "-- cursor: existing\n", "")):
            self.poller._align_cursor(state)
        with patch.object(self.poller, "_run_journalctl", return_value=(0, "", "")) as run:
            self.poller._poll_once(state)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--after-cursor") + 1], "existing")
        self.assertNotIn("--since", args)

    def test_empty_journal_is_available_for_starting_listener(self):
        with patch("monitor.ssh_journal_poller.journalctl_available", return_value=True), \
             patch.object(self.poller, "_run_journalctl", return_value=(0, "", "")):
            self.assertTrue(self.poller.is_available())


if __name__ == "__main__":
    unittest.main()
