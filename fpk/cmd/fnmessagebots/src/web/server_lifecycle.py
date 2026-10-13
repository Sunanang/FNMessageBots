"""独立维护 Web 入口，避免临时监听失败使后台永久不可用。"""

from __future__ import annotations

import logging
import socketserver
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, final

from flask import Flask
from werkzeug import serving

from web.access_guard import GatewayPrefixMiddleware, StripGatewayHeadersMiddleware

if TYPE_CHECKING:
    from _typeshed.wsgi import WSGIApplication


logger = logging.getLogger(__name__)


@final
class _LocalWSGIServer(serving.ThreadedWSGIServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind 会调用 getfqdn；监听无需等待 NAS 的反向 DNS。
        try:
            socketserver.TCPServer.server_bind(self)
        except OSError as error:
            # Werkzeug 会将 OSError 转成 SystemExit(1)，丢失文件日志中的失败原因。
            raise RuntimeError(str(error)) from error
        self.server_name = self.host
        self.server_port = self.port


@dataclass
class _Listener:
    host: str
    port: int
    middleware: Callable[[WSGIApplication], WSGIApplication]
    server: Optional[serving.BaseWSGIServer] = None
    thread: Optional[threading.Thread] = None
    failure: str = ""
    last_warning: float = 0.0


@final
class UIServerSupervisor:
    """各入口分别重试；已正常工作的入口和推送线程不受影响。"""

    def __init__(
        self,
        app_factory: Callable[[], Flask],
        host: str,
        port: str,
        socket_path: str = "",
        gateway_prefix: str = "",
        retry_interval: float = 5.0,
    ):
        self._app_factory = app_factory
        self._app: Optional[Flask] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.retry_interval = retry_interval
        self._port = port
        self._app_failure = ""
        self._app_warning_time = 0.0
        self._listeners: dict[str, _Listener] = {
            "TCP 配置入口": _Listener(host, 0, StripGatewayHeadersMiddleware),
        }
        if socket_path:
            self._listeners["统一网关 Socket"] = _Listener(
                f"unix://{socket_path}", 0,
                lambda app: GatewayPrefixMiddleware(app, gateway_prefix),
            )

    def start(self) -> UIServerSupervisor:
        self._check_listeners()
        thread = threading.Thread(
            target=self._watch, name="FnMessageBots-UI-Supervisor", daemon=True,
        )
        try:
            thread.start()
        except Exception:
            self.stop()
            raise
        self._thread = thread
        return self

    def _watch(self) -> None:
        while not self._stop.wait(self.retry_interval):
            self._check_listeners()

    def _check_listeners(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if self._app is None:
                try:
                    self._app = self._app_factory()
                    self._app_failure = ""
                except (Exception, SystemExit) as error:
                    failure = f"{type(error).__name__}: {error}"
                    now = time.monotonic()
                    if failure != self._app_failure or now - self._app_warning_time >= 60:
                        logger.warning("配置 UI 初始化失败（%s），%s 秒后重试", failure, self.retry_interval)
                        self._app_warning_time = now
                    self._app_failure = failure
                    return
            for name, listener in self._listeners.items():
                if self._stop.is_set():
                    return
                if listener.thread is not None and listener.thread.is_alive():
                    continue
                # serve_forever 退出时已关闭旧 Socket，随后才重新绑定。
                listener.thread = None
                listener.server = None
                server = None
                try:
                    if name == "TCP 配置入口":
                        listener.port = int(self._port)
                    server = _LocalWSGIServer(
                        listener.host, listener.port,
                        listener.middleware(self._app.wsgi_app),
                    )
                    thread = threading.Thread(
                        target=self._serve, args=(server, name), name=f"FnMessageBots-{name}", daemon=True,
                    )
                    thread.start()
                    listener.server = server
                    listener.thread = thread
                    listener.failure = ""
                    address = listener.host if listener.host.startswith("unix://") else f"http://{listener.host}:{server.port}"
                    logger.info("%s 已监听：%s", name, address)
                except (Exception, SystemExit) as error:
                    if server is not None:
                        server.server_close()
                    failure = f"{type(error).__name__}: {error}"
                    now = time.monotonic()
                    if failure != listener.failure or now - listener.last_warning >= 60:
                        logger.warning(
                            "%s 监听失败（地址=%s，端口=%s，%s），%s 秒后重试；请检查端口占用、监听地址及 Socket 权限",
                            name, listener.host, self._port if name == "TCP 配置入口" else 0,
                            failure, self.retry_interval,
                        )
                        listener.last_warning = now
                    listener.failure = failure

    def _serve(self, server: serving.BaseWSGIServer, name: str) -> None:
        try:
            server.serve_forever()
        except (Exception, SystemExit):
            if not self._stop.is_set():
                logger.exception("%s 服务线程异常退出", name)
        finally:
            server.server_close()
            if not self._stop.is_set():
                logger.warning("%s 服务线程已退出，%s 秒内重新监听", name, self.retry_interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            for listener in self._listeners.values():
                if listener.server is not None:
                    if listener.thread is not None and listener.thread.is_alive():
                        listener.server.shutdown()
                        listener.thread.join(timeout=5)
                    listener.server.server_close()
                    listener.server = None
                    listener.thread = None
