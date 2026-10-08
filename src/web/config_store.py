"""
Web 配置存储辅助：配置文件读写、URL 列表拼拆、标题前缀解析、推送渠道启用状态。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import TITLE_PREFIX_DEFAULT
from utils.value_parser import as_bool

# 渠道类型 → config.json 扁平字段（仅写入「已开启」的渠道，供通知器使用）
CHANNEL_TYPE_KEYS: Tuple[Tuple[str, str], ...] = (
    ("wechat", "wechat_webhook_url"),
    ("dingtalk", "dingtalk_webhook_url"),
    ("feishu", "feishu_webhook_url"),
    ("bark", "bark_url"),
    ("pushplus", "pushplus_params"),
    ("magic_push", "magic_push_params"),
    ("smtp", "smtp_params"),
    ("wecom_app", "wecom_app_params"),
    ("webhook", "webhook_params"),
    ("meow", "meow_params"),
)


def title_prefix_from_dict(d: dict, key: str = "title_prefix") -> str:
    """无 title_prefix 时用默认；显式空/空白则返回空。"""
    if key not in d:
        return TITLE_PREFIX_DEFAULT
    v = d[key]
    if v is None:
        return TITLE_PREFIX_DEFAULT
    return v.strip() if isinstance(v, str) else str(v).strip()


def config_load_error(config_file: Path) -> str:
    """若 config.json 不可读或 JSON 非法则返回错误说明，否则返回空串。"""
    if not config_file.exists():
        return ""
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            json.load(f)
    except json.JSONDecodeError as e:
        return f"config.json 不是合法 JSON：{e}"
    except OSError as e:
        return f"无法读取配置文件：{e}"


def load_raw_config(config_file: Path) -> dict:
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            # 配置损坏时回退空配置，避免 UI 崩溃
            return {}
    return {}


def save_raw_config(config_file: Path, data: dict) -> None:
    """原子写入，并保持 0600（含密码哈希与各渠道密钥）。"""
    config_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=str(config_file.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, config_file)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def split_urls(raw: str):
    if not raw:
        return []
    return [u.strip() for u in str(raw).split("|") if u.strip()]


def join_urls(urls):
    clean = [u.strip() for u in urls if u and u.strip()]
    return "|".join(clean)


def channels_from_raw(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    供配置页展示：优先读 push_channels（含 enabled）；
    旧配置仅有扁平字段时，全部视为开启。
    """
    stored = raw.get("push_channels")
    if isinstance(stored, list) and stored:
        out: List[Dict[str, Any]] = []
        for item in stored:
            if not isinstance(item, dict):
                continue
            ch_type = str(item.get("type") or "").strip()
            url = (item.get("url") or "").strip()
            if not ch_type or not url:
                continue
            if url.startswith("${") and url.endswith("}"):
                continue
            out.append(
                {
                    "type": ch_type,
                    "url": url,
                    "enabled": as_bool(item.get("enabled", True), True),
                }
            )
        if out:
            return out

    out = []
    for ch_type, key in CHANNEL_TYPE_KEYS:
        for url in split_urls(raw.get(key, "") or ""):
            if url.startswith("${") and url.endswith("}"):
                continue
            out.append({"type": ch_type, "url": url, "enabled": True})
    return out


def normalize_push_channels(channels: Any) -> List[Dict[str, Any]]:
    """规范化前端提交的渠道列表。"""
    if not isinstance(channels, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in channels:
        if not isinstance(item, dict):
            continue
        ch_type = str(item.get("type") or "").strip()
        url = (item.get("url") or "").strip()
        if not ch_type or not url:
            continue
        out.append(
            {
                "type": ch_type,
                "url": url,
                "enabled": as_bool(item.get("enabled", True), True),
            }
        )
    return out


SECRET_MASK = "******"
_URL_SECRET_QUERY_KEYS = frozenset({"key", "access_token", "token", "secret", "sign"})
_URL_SECRET_SEGMENT = re.compile(r"^[A-Za-z0-9_\-]{16,}$")
_SECRET_HEADER_NAME = re.compile(r"auth|token|key|secret|sign|cookie|password", re.I)


def mask_secret(value: str) -> str:
    v = str(value or "")
    if not v:
        return v
    return (v[:4] + SECRET_MASK) if len(v) > 8 else SECRET_MASK


def _mask_headers(raw: str) -> str:
    """通用 Webhook 请求头：名称像凭据的（Authorization / X-Token 等）隐藏值。"""
    out = []
    for line in str(raw or "").splitlines():
        name, sep, value = line.partition(":")
        if sep and _SECRET_HEADER_NAME.search(name) and value.strip():
            out.append(f"{name}: {mask_secret(value.strip())}")
        else:
            out.append(line)
    return "\n".join(out)


def _mask_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return mask_secret(url)
    segments = [mask_secret(s) if _URL_SECRET_SEGMENT.match(s) else s for s in parts.path.split("/")]
    query = [
        (k, mask_secret(v) if k.lower() in _URL_SECRET_QUERY_KEYS else v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            "/".join(segments),
            urllib.parse.urlencode(query, safe="*"),
            parts.fragment,
        )
    )


# JSON 配置渠道：字段 → 打码函数（还原时比较「打码(已存值) == 提交值」）
_JSON_SECRET_FIELDS: Dict[str, Dict[str, Callable[[str], str]]] = {
    "pushplus": {"token": mask_secret},
    "magic_push": {"token": mask_secret},
    "smtp": {"password": mask_secret},
    "wecom_app": {"corp_secret": mask_secret},
    "webhook": {"url": _mask_url, "headers": _mask_headers},
}


def _mask_one(ch_type: str, url: str) -> str:
    fields = _JSON_SECRET_FIELDS.get(ch_type)
    if fields is None:
        return _mask_url(url)
    try:
        obj = json.loads(url)
    except json.JSONDecodeError:
        return mask_secret(url)
    if not isinstance(obj, dict):
        return mask_secret(url)
    for f, masker in fields.items():
        if isinstance(obj.get(f), str) and obj[f]:
            masked = masker(obj[f])
            # 未识别到密钥时保留原值（_mask_url 会重新编码查询串，可能破坏 {title} 等占位符）
            if SECRET_MASK in masked:
                obj[f] = masked
    return json.dumps(obj, ensure_ascii=False).replace("|", "\\u007c")


def mask_channel_url(ch_type: str, url: str) -> str:
    """多个地址以 | 分隔时逐个打码；未识别到密钥的部分原样返回。"""
    out = []
    for u in str(url or "").split("|"):
        m = _mask_one(ch_type, u)
        out.append(m if SECRET_MASK in m else u)
    return "|".join(out)


def mask_channels(channels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """配置页展示用：隐藏 Webhook key、Token、SMTP 密码等。"""
    return [{**ch, "url": mask_channel_url(str(ch.get("type") or ""), ch.get("url") or "")} for ch in channels]


def _restore_json_secret(ch_type: str, url: str, stored_urls: List[str]) -> Optional[str]:
    try:
        obj = json.loads(url)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    for f, masker in _JSON_SECRET_FIELDS[ch_type].items():
        submitted = str(obj.get(f) or "")
        if SECRET_MASK not in submitted:
            continue
        found = None
        for s in stored_urls:
            try:
                so = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(so, dict) and isinstance(so.get(f), str) and so[f] and masker(so[f]) == submitted:
                found = so[f]
                break
        if found is None:
            return None
        obj[f] = found
    return json.dumps(obj, ensure_ascii=False).replace("|", "\\u007c")


def restore_masked_channels(
    submitted: List[Dict[str, Any]], stored: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], str]:
    """把前端回传的打码值还原为已保存的真实值；无法匹配时返回错误说明。"""
    stored_by_type: Dict[str, List[str]] = {}
    for ch in stored:
        for u in str(ch.get("url") or "").split("|"):
            if u.strip():
                stored_by_type.setdefault(str(ch.get("type") or ""), []).append(u.strip())

    out: List[Dict[str, Any]] = []
    for ch in submitted:
        ch_type = str(ch.get("type") or "")
        url = str(ch.get("url") or "")
        if SECRET_MASK not in url:
            out.append(ch)
            continue
        candidates = stored_by_type.get(ch_type, [])
        restored_parts = []
        for part in url.split("|"):
            if SECRET_MASK not in part:
                restored_parts.append(part)
                continue
            if ch_type in _JSON_SECRET_FIELDS:
                real = _restore_json_secret(ch_type, part, candidates)
            else:
                real = next((s for s in candidates if _mask_url(s) == part), None)
            if real is None:
                return [], "存在已打码的密钥但无法匹配原值，请重新填写完整的推送地址或密钥后再保存。"
            restored_parts.append(real)
        out.append({**ch, "url": "|".join(restored_parts)})
    return out, ""


def sync_channel_flat_keys(channels: List[Dict[str, Any]]) -> Dict[str, str]:
    """仅把已开启渠道写入扁平字段，关闭的渠道只保留在 push_channels。"""
    buckets = {key: [] for _, key in CHANNEL_TYPE_KEYS}
    type_to_key = dict(CHANNEL_TYPE_KEYS)
    for ch in channels:
        if not as_bool(ch.get("enabled", True), True):
            continue
        key = type_to_key.get(str(ch.get("type") or ""))
        url = (ch.get("url") or "").strip()
        if key and url:
            buckets[key].append(url)
    return {key: join_urls(urls) for key, urls in buckets.items()}
