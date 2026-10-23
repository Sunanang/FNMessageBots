"""
Web 鉴权服务：密码哈希、校验、配置读取辅助函数。
"""

from __future__ import annotations

import hashlib
import secrets
import unicodedata
from typing import Callable, Optional, Tuple

PBKDF2_ITERATIONS = 100000
PASSWORD_MIN_LENGTH = 8
# 手机输入法会夹进零宽字符；这些字符不改变用户看到的密码。
_INVISIBLE = dict.fromkeys((0x00AD, 0x034F, 0x180E, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))
# iOS 智能标点会把直引号、连字符和省略号换成排版字符。
_SMART_PUNCTUATION = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
    "\u2026": "...",
})


def normalize_password(password: str) -> str:
    """去掉手机键盘对密码的常见改写，桌面端原样输入保持不变。"""
    if not isinstance(password, str):
        return ""
    text = unicodedata.normalize("NFKC", password)
    return text.translate(_INVISIBLE).translate(_SMART_PUNCTUATION).strip()


def _password_candidates(password: str) -> list[str]:
    """兼容输入法标点与旧密码原文，始终保留大小写。"""
    raw = password.strip() if isinstance(password, str) else ""
    normalized = normalize_password(password)
    candidates: list[str] = []
    for item in (normalized, raw):
        if item and item not in candidates:
            candidates.append(item)
    return candidates


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


def password_matches(password: str, salt_hex: str, stored_hash: str) -> bool:
    """接受原密码，以及手机键盘改写后仍能还原的输入。"""
    return any(verify_password(candidate, salt_hex, stored_hash) for candidate in _password_candidates(password))


def get_password_config(raw: dict) -> Tuple[Optional[str], Optional[str]]:
    """返回 (salt_hex, hash_hex)，未设置则 (None, None)。"""
    salt = raw.get("web_password_salt")
    h = raw.get("web_password_hash")
    salt = salt.strip() if isinstance(salt, str) else ""
    h = h.strip() if isinstance(h, str) else ""
    if salt and h:
        return (salt, h)
    return (None, None)


def has_password_set(load_raw_config: Callable[[], dict]) -> bool:
    """当前配置是否已设置访问密码。"""
    raw = load_raw_config()
    salt, h = get_password_config(raw)
    return salt is not None and h is not None


def apply_new_password(raw: dict, password: str) -> dict:
    """生成新盐值与哈希写入 raw（原地修改并返回），明文不落盘。"""
    salt = secrets.token_hex(16)
    raw["web_password_salt"] = salt
    raw["web_password_hash"] = hash_password(normalize_password(password), bytes.fromhex(salt))
    return raw
