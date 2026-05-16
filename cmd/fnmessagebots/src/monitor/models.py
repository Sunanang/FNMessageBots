"""
数据模型定义（当前仅数据库轮询使用）
"""

from dataclasses import dataclass, asdict
from typing import Dict, Any


@dataclass
class JournalEntry:
    """日志条目（由 db_log_poller 从 log 表行构造，供 event_processor 使用）"""

    cursor: str
    timestamp: str
    hostname: str
    syslog_identifier: str
    message: str
    priority: int
    pid: int
    raw_data: str
    original_line: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)
