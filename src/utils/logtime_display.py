"""日志库 logtime 的可选秒级补偿；标准 Unix 时间戳无需额外偏移。"""

import os


def get_logtime_display_offset_seconds() -> int:
    raw = os.environ.get("LOGTIME_DISPLAY_OFFSET_SECONDS", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0
