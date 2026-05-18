"""日志库 logtime 字段显示用的秒级偏移（与存库时区/偏差对齐）。"""

import os


def get_logtime_display_offset_seconds() -> int:
    raw = os.environ.get("LOGTIME_DISPLAY_OFFSET_SECONDS", "28800")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 28800
