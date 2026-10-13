"""
Web 访问控制辅助：统一网关身份、首次设密初始化码、登录失败限流。
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

# 仅统一网关（Unix Socket）入口会写入该标记；TCP 入口会剥离 X-Trim-* 头，避免伪造
GATEWAY_ENVIRON_KEY = "fnmb.via_gateway"

SETUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SETUP_CODE_LENGTH = 10

LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 600
LOGIN_LOCK_SECONDS = 300


class GatewayPrefixMiddleware:
    """统一网关入口：转发时保留 /app/<appname> 前缀，这里拆成 SCRIPT_NAME 并标记可信来源。"""

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""

    def __call__(self, environ, start_response):
        environ[GATEWAY_ENVIRON_KEY] = True
        path = environ.get("PATH_INFO") or "/"
        if self.prefix:
            if path == self.prefix:
                start_response("301 Moved Permanently", [("Location", self.prefix + "/")])
                return [b""]
            if path.startswith(self.prefix + "/"):
                environ["SCRIPT_NAME"] = self.prefix
                environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.app(environ, start_response)


class StripGatewayHeadersMiddleware:
    """TCP 入口：丢弃客户端自带的 X-Trim-* 头，只有网关 Socket 的身份头可信。"""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        for key in [k for k in environ if k.startswith("HTTP_X_TRIM_")]:
            del environ[key]
        environ[GATEWAY_ENVIRON_KEY] = False
        return self.app(environ, start_response)


def gateway_identity(environ) -> Optional[Dict[str, object]]:
    """经统一网关进入时返回 {username, is_admin}；否则 None。"""
    if not environ.get(GATEWAY_ENVIRON_KEY):
        return None
    uid = (environ.get("HTTP_X_TRIM_USERID") or "").strip()
    if not uid:
        return None
    return {
        "uid": uid,
        "username": (environ.get("HTTP_X_TRIM_USERNAME") or "").strip(),
        "is_admin": (environ.get("HTTP_X_TRIM_ISADMIN") or "").strip().lower() == "true",
    }


def is_loopback_request(environ) -> bool:
    if environ.get(GATEWAY_ENVIRON_KEY):
        return False
    addr = (environ.get("REMOTE_ADDR") or "").strip()
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def client_address(environ) -> str:
    """仅信任显式配置的反代；从转发链右端剥离可信代理地址。"""
    peer = (environ.get("REMOTE_ADDR") or "unknown").strip()
    try:
        trusted = [ipaddress.ip_network(v.strip(), strict=False)
                   for v in os.getenv("FNMB_TRUSTED_PROXIES", "").split(",") if v.strip()]
        peer_ip = ipaddress.ip_address(peer)
        if not any(peer_ip in network for network in trusted):
            return peer
        forwarded = [ipaddress.ip_address(v.strip()) for v in
                     (environ.get("HTTP_X_FORWARDED_FOR") or "").split(",") if v.strip()]
        for address in reversed(forwarded):
            if not any(address in network for network in trusted):
                return str(address)
        return str(forwarded[0]) if forwarded else peer
    except ValueError:
        # 无效地址/配置不能扩大信任范围。
        return peer
class SetupCodeStore:
    """未设密码时生成一次性初始化码：写入配置目录（0600）并打印到运行日志。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def ensure(self) -> str:
        with self._lock:
            code = self.read()
            if code:
                return code
            code = "".join(secrets.choice(SETUP_CODE_ALPHABET) for _ in range(SETUP_CODE_LENGTH))
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(code)
            except OSError as e:
                print(f"初始化码写入失败（{e}），仅输出到日志")
            print(
                "=" * 60
                + f"\n尚未设置 Web 访问密码。首次设置密码需填写初始化码：{code}"
                + f"\n（也可在 {self.path} 查看；设置密码后自动失效）\n"
                + "=" * 60,
                flush=True,
            )
            return code

    def verify(self, value: str) -> bool:
        code = self.read()
        if not code or not value:
            return False
        return secrets.compare_digest(code.upper(), value.strip().upper())

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


class LoginRateLimiter:
    """按来源 IP 统计密码/初始化码失败次数，超限后临时锁定。"""

    def __init__(self):
        self._fails: Dict[str, List[float]] = {}
        self._locked_until: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    def _cleanup(self, now: float) -> None:
        if 0 <= now - self._last_cleanup < 60:
            return
        self._locked_until = {key: until for key, until in self._locked_until.items() if until > now}
        self._fails = {key: recent for key, times in self._fails.items()
                       if (recent := [t for t in times if now - t < LOGIN_FAILURE_WINDOW_SECONDS])}
        self._last_cleanup = now

    def retry_after(self, key: str) -> int:
        now = time.time()
        with self._lock:
            self._cleanup(now)
            until = self._locked_until.get(key, 0.0)
            if until > now:
                return int(until - now) + 1
            self._locked_until.pop(key, None)
            return 0

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            self._cleanup(now)
            fails = [t for t in self._fails.get(key, []) if now - t < LOGIN_FAILURE_WINDOW_SECONDS]
            fails.append(now)
            if len(fails) >= LOGIN_MAX_FAILURES:
                self._locked_until[key] = now + LOGIN_LOCK_SECONDS
                fails = []
            self._fails[key] = fails

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._locked_until.pop(key, None)
