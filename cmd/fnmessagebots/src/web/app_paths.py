"""
Web 应用路径与资源常量。
"""

from __future__ import annotations

import os
from pathlib import Path


def _code_base_dir() -> Path:
    """代码目录（包含 src/config/assets），用于读取静态资源。"""
    candidate = Path(__file__).resolve().parent.parent.parent
    if (candidate / "assets").exists():
        return candidate
    return Path("/app")


def _config_base_dir() -> Path:
    """配置目录优先使用 APP_HOME，确保配置可持久化。"""
    app_home = os.getenv("APP_HOME")
    if app_home:
        return Path(app_home)
    return _code_base_dir()


BASE_DIR = _config_base_dir()
ASSETS_DIR = _code_base_dir() / "assets"


def get_base_dir() -> Path:
    """兼容 FnMessageBots 旧调用；FPK 下返回配置/数据基准目录。"""
    return BASE_DIR


CONFIG_FILE = BASE_DIR / "config" / "config.json"
# 随应用数据目录存在；卸载并勾选删除数据后文件消失，重装会生成新值，用于让浏览器清除 localStorage 中的主题偏好
DATA_DIR = BASE_DIR / "data"
CLIENT_NONCE_FILE = DATA_DIR / ".fnmb_client_nonce"
ICON_FILE = ASSETS_DIR / "icons" / "app-icon.png"
GITHUB_ICON_FILE = ASSETS_DIR / "icons" / "github.svg"
SUPPORT_QR_DIR = ASSETS_DIR / "icons"
SUPPORT_QR_FILENAMES = frozenset({"wechat_pay.jpg", "ali_pay.jpg"})
