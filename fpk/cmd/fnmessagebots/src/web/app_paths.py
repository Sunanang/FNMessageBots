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
# APP_HOME 在 FPK 中是可写的数据目录；图片随代码安装，不应从数据目录查找。
ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets"
ICON_FILE = ASSETS_DIR / "icons" / "app-icon.png"
SUPPORT_QR_DIR = ASSETS_DIR / "icons"
SUPPORT_QR_FILENAMES = frozenset({"wechat_pay.jpg", "ali_pay.jpg"})

