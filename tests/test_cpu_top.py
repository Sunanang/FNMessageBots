"""CPU Top3 采样与正文渲染单元测试（不依赖真实进程采样）。"""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

root = Path(__file__).resolve().parents[1]
source = root / "src"
sys.path.insert(0, str(root))
sys.path.insert(0, str(source))

from utils import cpu_top


class CpuTopHelperTests(unittest.TestCase):
    def test_aggregate_by_name_sums_same_app(self):
        rows = [
            {"name": "ffmpeg", "cpu_percent": 20.0, "pid": 1},
            {"name": "ffmpeg", "cpu_percent": 15.0, "pid": 2},
            {"name": "nginx", "cpu_percent": 10.0, "pid": 3},
            {"name": "idle", "cpu_percent": 1.0, "pid": 4},
        ]
        top = cpu_top._aggregate_by_name(rows, limit=3)
        self.assertEqual(len(top), 3)
        self.assertEqual(top[0]["name"], "ffmpeg")
        self.assertAlmostEqual(top[0]["cpu_percent"], 35.0)
        self.assertEqual(top[1]["name"], "nginx")

    def test_format_top_cpu_lines(self):
        text = cpu_top.format_top_cpu_lines(
            [
                {"name": "trim.media", "cpu_percent": 42.14, "pid": 9},
                {"name": "docker", "cpu_percent": 18.3, "pid": 8},
                {"name": "postgres", "cpu_percent": 9.6, "pid": 7},
            ]
        )
        self.assertIn("当前占用 Top3:", text)
        self.assertIn("1. trim.media  42.1%", text)
        self.assertIn("3. postgres  9.6%", text)

    def test_format_top_cpu_short(self):
        short = cpu_top.format_top_cpu_short(
            [
                {"name": "a", "cpu_percent": 12.4, "pid": 1},
                {"name": "b", "cpu_percent": 8.1, "pid": 2},
            ]
        )
        self.assertEqual(short, "a 12%/b 8%")

    def test_format_empty(self):
        self.assertEqual(cpu_top.format_top_cpu_lines([]), "")
        self.assertEqual(cpu_top.format_top_cpu_short([]), "")

    def test_sample_prefers_open_api(self):
        api_rows = [{"name": "from-api", "cpu_percent": 55.0, "pid": 11}]
        with patch.object(cpu_top, "_sample_via_open_api", return_value=api_rows), patch.object(
            cpu_top, "_sample_via_psutil"
        ) as ps:
            out = cpu_top.sample_top_cpu_processes(limit=3, timeout=1.0)
            self.assertEqual(out[0]["name"], "from-api")
            ps.assert_not_called()

    def test_open_api_skipped_without_explicit_req(self):
        with patch.dict("os.environ", {}, clear=False):
            # 确保未设置 TRIM_PROC_LIST_REQ
            import os
            os.environ.pop("TRIM_PROC_LIST_REQ", None)
            with patch("utils.fnos_platform.call_trimapp") as call_api:
                out = cpu_top._sample_via_open_api(limit=3, deadline=time.monotonic() + 1.0)
                self.assertEqual(out, [])
                call_api.assert_not_called()

    def test_sample_falls_back_to_psutil(self):
        ps_rows = [{"name": "from-ps", "cpu_percent": 33.0, "pid": 22}]
        with patch.object(cpu_top, "_sample_via_open_api", return_value=[]), patch.object(
            cpu_top, "_sample_via_psutil", return_value=ps_rows
        ):
            out = cpu_top.sample_top_cpu_processes(limit=3, timeout=1.0)
            self.assertEqual(out[0]["name"], "from-ps")

    def test_sample_swallows_errors(self):
        with patch.object(cpu_top, "_sample_via_open_api", side_effect=RuntimeError("boom")), patch.object(
            cpu_top, "_sample_via_psutil", side_effect=RuntimeError("boom2")
        ):
            self.assertEqual(cpu_top.sample_top_cpu_processes(), [])

    def test_extract_proc_rows_flexible_keys(self):
        data = {
            "list": [
                {"Name": "foo", "cpuPercent": 12, "PID": 1},
                {"comm": "bar", "busy": "8.5", "pid": 2},
            ]
        }
        rows = cpu_top._extract_proc_rows(data)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "foo")
        self.assertAlmostEqual(rows[1]["cpu_percent"], 8.5)

    def test_short_app_name_from_python_cmdline(self):
        name = cpu_top._short_app_name_from_proc(
            "python3", ["/usr/bin/python3", "/vol1/app/main.py", "--daemon"]
        )
        self.assertEqual(name, "main.py")


class CpuAlarmContentTests(unittest.TestCase):
    def _build_content(self, event_data):
        """镜像 notifier 正文拼装逻辑，避免拉全量 MultiPlatformNotifier 依赖。"""
        data = event_data.get("data", {})
        threshold = data.get("THRESHOLD", 0)
        content = f"📊 使用率阈值: {threshold}%\n"
        tops = event_data.get("top_cpu_processes") or data.get("top_cpu_processes") or []
        top_text = cpu_top.format_top_cpu_lines(tops if isinstance(tops, list) else [])
        if top_text:
            content += top_text + "\n"
        return content

    def test_alarm_content_includes_top3(self):
        content = self._build_content(
            {
                "data": {"THRESHOLD": 90},
                "top_cpu_processes": [
                    {"name": "trim.media", "cpu_percent": 40.0, "pid": 1},
                    {"name": "docker", "cpu_percent": 20.0, "pid": 2},
                    {"name": "postgres", "cpu_percent": 10.0, "pid": 3},
                ],
            }
        )
        self.assertIn("使用率阈值: 90%", content)
        self.assertIn("当前占用 Top3:", content)
        self.assertIn("1. trim.media  40.0%", content)

    def test_alarm_content_without_top_still_works(self):
        content = self._build_content({"data": {"THRESHOLD": 80}})
        self.assertIn("使用率阈值: 80%", content)
        self.assertNotIn("Top3", content)


class CpuAlarmHandlerTests(unittest.TestCase):
    def _make_processor(self, notifier):
        from monitor.event_processor import EventProcessor

        config = SimpleNamespace(log_retention_days=7, log_dir="/tmp")
        with patch("monitor.event_processor.LogStorage"):
            return EventProcessor(notifier, config)

    def _entry(self, cursor="1"):
        from monitor.models import JournalEntry

        return JournalEntry(
            cursor=cursor,
            timestamp="2026-10-30 12:00:00",
            hostname="nas",
            syslog_identifier="resmon",
            message="cpu",
            priority=4,
            pid=1,
            raw_data="{}",
        )

    def test_handler_enriches_event_data(self):
        notifier = MagicMock()
        tops = [{"name": "ffmpeg", "cpu_percent": 50.0, "pid": 9}]
        entry = self._entry()

        with patch(
            "utils.cpu_top.sample_top_cpu_processes", return_value=tops
        ), patch(
            "src.utils.cpu_top.sample_top_cpu_processes", return_value=tops
        ):
            processor = self._make_processor(notifier)
            with patch.object(processor, "_store_notification_log"):
                processor._handle_cpu_usage_alarm({"data": {"THRESHOLD": 90}}, entry)

        notifier.send_notification.assert_called_once()
        call_kwargs = notifier.send_notification.call_args.kwargs
        self.assertEqual(call_kwargs.get("event_type"), "CPU_USAGE_ALARM")
        self.assertEqual(call_kwargs.get("event_data", {}).get("top_cpu_processes"), tops)

    def test_batch_summary_enriches_cpu_alarm_once(self):
        notifier = MagicMock()
        tops = [{"name": "ffmpeg", "cpu_percent": 50.0, "pid": 9}]
        sample = MagicMock(return_value=tops)

        with patch("utils.cpu_top.sample_top_cpu_processes", sample), patch(
            "src.utils.cpu_top.sample_top_cpu_processes", sample
        ):
            processor = self._make_processor(notifier)
            with patch.object(processor, "_store_notification_log"):
                processor.process_batch_events(
                    [
                        {
                            "event_type": "CPU_USAGE_ALARM",
                            "event_data": {"data": {"THRESHOLD": 90}},
                            "entry": self._entry("11"),
                            "source": "db",
                            "row_id": "11",
                        },
                        {
                            "event_type": "CPU_USAGE_ALARM",
                            "event_data": {"data": {"THRESHOLD": 95}},
                            "entry": self._entry("12"),
                            "source": "db",
                            "row_id": "12",
                        },
                    ]
                )

        sample.assert_called_once()
        notifier.send_notification.assert_called_once()
        call_kwargs = notifier.send_notification.call_args.kwargs
        self.assertEqual(call_kwargs.get("event_type"), "POLL_BATCH_SUMMARY")
        summary = call_kwargs.get("event_data") or {}
        grouped = summary.get("grouped_events", {}).get("CPU_USAGE_ALARM") or []
        self.assertEqual(len(grouped), 2)
        for item in grouped:
            self.assertEqual(item["event_data"].get("top_cpu_processes"), tops)


if __name__ == "__main__":
    unittest.main()
