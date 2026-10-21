"""在真实进程时区下检查勿扰边界、数据库统计和消息时间；不连接 NAS 或推送。"""
import logging
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path.cwd() / "src"))
from notifier import multi_platform_notifier, unified_notifier
from utils.cron_util import next_cron_timestamp


class Clock(datetime):
    epoch = 0.0

    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(cls.epoch, tz)


def check_timezone(zone_name):
    zone = ZoneInfo(zone_name)
    end = datetime(2026, 10, 12, 7, tzinfo=zone)
    # 从 Unix 时间转回实际运行时区，验证 libc 与时区数据库都可用。
    assert datetime.fromtimestamp(end.timestamp()) == end.replace(tzinfo=None), "运行时区与预期不符"
    notifier = object.__new__(unified_notifier.UnifiedNotifier)
    notifier.config = SimpleNamespace(dnd_enabled=True, dnd_start_time="22:00", dnd_end_time="07:00",
                                      monitor_events=["LoginSucc"])
    notifier.logger = logging.getLogger("timezone-check")
    formatter = object.__new__(multi_platform_notifier.MultiPlatformNotifier)
    notifier.multi_platform_notifier = formatter
    with patch.object(unified_notifier, "datetime", Clock), patch.object(multi_platform_notifier, "datetime", Clock):
        for start_hour, end_hour, cross_day in [(22, 7, True), (13, 14, False)]:
            notifier.config.dnd_start_time = f"{start_hour:02}:00"
            notifier.config.dnd_end_time = f"{end_hour:02}:00"
            end = end.replace(hour=end_hour)
            start = (end - timedelta(days=int(cross_day))).replace(hour=start_hour)
            for when, quiet in [(start - timedelta(seconds=1), False), (start, True),
                                (end - timedelta(seconds=1), True), (end, False)]:
                Clock.epoch = when.timestamp()
                assert notifier._in_dnd_window() == quiet, (zone_name, when, quiet)
                if quiet:
                    assert notifier._calc_latest_dnd_period() == (None, None)
            Clock.epoch = end.timestamp()
            start_dt, end_dt = notifier._calc_latest_dnd_period()
            assert (start_dt, end_dt) == (start.replace(tzinfo=None), end.replace(tzinfo=None))
            assert start_dt.timestamp() == start.timestamp()
            assert end_dt.timestamp() == end.timestamp()
            assert next_cron_timestamp(f"0 {end_hour} * * *", end.timestamp() - 60) == end.timestamp()

            with tempfile.TemporaryDirectory() as directory:
                notifier.config.logger_db_path = str(Path(directory) / "logger.db3")
                with sqlite3.connect(notifier.config.logger_db_path) as conn:
                    conn.execute("CREATE TABLE log (id INTEGER PRIMARY KEY, logtime INTEGER, eventId TEXT, "
                                 "uname TEXT, serviceId TEXT, category TEXT, parameter TEXT)")
                    for offset in [0, 28800]:
                        conn.execute("DELETE FROM log")
                        for epoch in [start.timestamp() - 1, start.timestamp(), end.timestamp() - 1, end.timestamp()]:
                            conn.execute("INSERT INTO log (logtime, eventId) VALUES (?, 'LoginSucc')", (int(epoch) - offset,))
                        conn.commit()
                        with patch.object(unified_notifier, "get_logtime_display_offset_seconds", return_value=offset):
                            counts = notifier._query_dnd_events_summary(start_dt, end_dt)
                        assert counts == {"LoginSucc": 2}, (zone_name, offset, counts)
            summary = notifier._build_dnd_summary_from_db(start_dt, end_dt, counts)
            body = formatter._build_system_content("DND_SUMMARY", {}, summary)
            assert f"{start_hour:02}:00 - {end_hour:02}:00" in body
            assert body.endswith(end.strftime("%Y-%m-%d %H:%M:%S")), body
    print(f"Timezone {zone_name}: DND boundaries, query range, summary timestamp and Cron passed")


if __name__ == "__main__":
    check_timezone(sys.argv[1])
