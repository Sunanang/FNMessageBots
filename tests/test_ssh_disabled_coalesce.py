"""SSH 关闭：Stopping/signal15/Stopped 短时合并为一次推送。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))

from monitor.ssh_journal_poller import SSH_DISABLED, SshJournalPoller


class SshDisabledCoalesceTests(unittest.TestCase):
    def test_rapid_disabled_lines_push_once(self):
        poller = SshJournalPoller.__new__(SshJournalPoller)
        poller.monitor_events = {SSH_DISABLED}
        poller.ignore_loopback = False
        poller.ignore_lan = False
        poller._service_hooks = []
        poller.poll_batch_summary_enabled = False
        poller.summary_batch_enqueue = None
        poller._last_disabled_push_at = 0.0
        poller._disabled_coalesce_seconds = 5.0
        poller.logger = MagicMock()
        calls = []
        poller.event_handlers = {SSH_DISABLED: lambda ed, e: calls.append(ed)}

        for i, msg in enumerate(
            ("Stopping ssh.service", "Received signal 15; terminating.", "Stopped ssh.service")
        ):
            poller._emit(
                SSH_DISABLED,
                {"from": "systemd", "unit": "ssh", "_source_cursor": f"c{i}"},
                msg,
                "2026-01-01 00:00:00",
            )
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
