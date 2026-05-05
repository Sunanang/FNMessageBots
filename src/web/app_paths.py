"""
Web 应用路径与资源常量。
"""

from __future__ import annotations

import os
from pathlib import Path


def get_base_dir() -> Path:
    """Docker 下用 /app，本地调试用项目根目录（含 config 的目录）。"""
    app_home = os.getenv("APP_HOME")
    if app_home:
        return Path(app_home)
    candidate = Path(__file__).resolve().parent.parent.parent
    if (candidate / "config").exists():
        return candidate
    return Path("/app")


BASE_DIR = get_base_dir()
CONFIG_FILE = BASE_DIR / "config" / "config.json"
ICON_FILE = BASE_DIR / "assets" / "icons" / "app-icon.png"
GITHUB_ICON_FILE = BASE_DIR / "assets" / "icons" / "github.svg"
SUPPORT_QR_DIR = BASE_DIR / "assets" / "icons"
SUPPORT_QR_FILENAMES = frozenset({"wechat_pay.jpg", "ali_pay.jpg"})

