"""
Web 鉴权服务：密码哈希、校验、配置读取辅助函数。
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Callable, Optional, Tuple
from utils.value_parser import as_bool

PBKDF2_ITERATIONS = 100000


def hash_password(password: str, salt: bytes) -> str:
    """PBKDF2-HMAC-SHA256，返回 hex。"""
    h = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return h.hex()


def verify_password(password: str, salt_hex: str, stored_hash: str) -> bool:
    """验证密码是否与存储的 hash 一致。"""
    try:
        salt = bytes.fromhex(salt_hex)
        got = hash_password(password, salt)
        return secrets.compare_digest(got, stored_hash)
    except Exception:
        return False


def get_password_config(raw: dict) -> Tuple[Optional[str], Optional[str]]:
    """返回 (salt_hex, hash_hex)，未设置则 (None, None)。"""
    salt = (raw.get("web_password_salt") or "").strip()
    h = (raw.get("web_password_hash") or "").strip()
    if salt and h:
        return (salt, h)
    return (None, None)


def has_password_set(load_raw_config: Callable[[], dict]) -> bool:
    """当前配置是否已设置访问密码。"""
    raw = load_raw_config()
    salt, h = get_password_config(raw)
    return salt is not None and h is not None


def is_password_verification_enabled(load_raw_config: Callable[[], dict]) -> bool:
    """是否开启密码验证（默认 True）。"""
    raw = load_raw_config()
    return as_bool(raw.get("web_password_enabled", True), True)
