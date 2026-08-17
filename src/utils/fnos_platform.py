"""
飞牛开放 API：读取平台配置（系统版本等）。

后端调用：Unix Socket `/var/run/trim_open_gateway_apiscope.socket`
接口：`trim.system.getPlatformConfig`
认证：环境变量 `TRIM_API_TOKEN`（每次现读，不落盘）
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_GATEWAY_SOCKET = "/var/run/trim_open_gateway_apiscope.socket"
PLATFORM_CONFIG_REQ = "trim.system.getPlatformConfig"
DEFAULT_APP_NAME = "FnMessageBot"


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, unix_path: str, timeout: float = 3.0):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = unix_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix_path)
        self.sock = sock


def _gateway_socket_path() -> str:
    return (os.getenv("TRIM_OPEN_GATEWAY_SOCKET") or DEFAULT_GATEWAY_SOCKET).strip()


def _app_name() -> str:
    return (os.getenv("TRIM_APPNAME") or DEFAULT_APP_NAME).strip() or DEFAULT_APP_NAME


def fetch_platform_config(timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """
    调用 trim.system.getPlatformConfig。
    成功返回 data 字典（含 systemVersion / systemLanguage）；失败返回 None。
    """
    token = (os.getenv("TRIM_API_TOKEN") or "").strip()
    if not token:
        return None

    sock_path = _gateway_socket_path()
    if not sock_path or not os.path.exists(sock_path):
        return None

    body = {
        "reqId": str(uuid.uuid4()),
        "req": PLATFORM_CONFIG_REQ,
        "appName": _app_name(),
        "data": {},
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Length": str(len(payload)),
    }

    conn: Optional[_UnixHTTPConnection] = None
    try:
        conn = _UnixHTTPConnection(sock_path, timeout=timeout)
        conn.request("POST", "/api/v1/trimapp", body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        if resp.status != 200:
            logger.debug(
                "getPlatformConfig HTTP %s: %s",
                resp.status,
                (raw or "")[:200],
            )
            return None
        obj = json.loads(raw) if raw else {}
        if not isinstance(obj, dict):
            return None
        if int(obj.get("code", -1) or -1) != 0:
            logger.debug(
                "getPlatformConfig business code=%s msg=%s",
                obj.get("code"),
                obj.get("msg"),
            )
            return None
        data = obj.get("data")
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug("getPlatformConfig failed: %s", e)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def format_fnos_version_display(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("fnos"):
        return raw[:160]
    return f"FnOS {raw}"[:160]


def resolve_fnos_version_via_api() -> str:
    """
    优先开放 API；其次 TRIM_SYS_VERSION 环境变量。
    拿不到返回空字符串（由调用方决定是否走旧回退）。
    """
    data = fetch_platform_config()
    if data:
        ver = str(data.get("systemVersion") or "").strip()
        if ver:
            return format_fnos_version_display(ver)

    env_ver = (os.getenv("TRIM_SYS_VERSION") or "").strip()
    if env_ver:
        return format_fnos_version_display(env_ver)

    return ""
