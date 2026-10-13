"""Web 入口恢复与真实 HTTP 健康检查；不发送通知或访问 NAS。"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, build_opener

from flask import Flask

root = Path(__file__).resolve().parents[1]
source = root / "src"
if not source.is_dir():
    source = root / "cmd/fnmessagebots/src"
sys.path.insert(0, str(source))

from web.server_lifecycle import UIServerSupervisor


class FakeServer:
    def __init__(self, error=None):
        self.port = 18080
        self.entered = threading.Event()
        self.release = threading.Event()
        self.server_close = Mock()
        self.error = error

    def serve_forever(self):
        self.entered.set()
        if self.error:
            raise self.error
        self.release.wait(timeout=10)

    def shutdown(self):
        self.release.set()


def make_app(body=None):
    app = Flask(__name__)
    app.add_url_rule("/healthz", view_func=lambda: body if body is not None else {"ok": True, "service": "fn-message-bots"})
    return app


class UIRecoveryTests(unittest.TestCase):
    def supervisor(self, **kwargs):
        supervisor = UIServerSupervisor(lambda: make_app(), "127.0.0.1", "18080", **kwargs)
        self.addCleanup(supervisor.stop)
        return supervisor

    def test_tcp_failure_retries_without_restarting_working_gateway(self):
        supervisor = self.supervisor(socket_path="/unused/app.sock")
        gateway, tcp = FakeServer(), FakeServer()
        with patch("web.server_lifecycle._LocalWSGIServer", side_effect=[SystemExit(1), gateway, tcp]) as factory, \
             self.assertLogs("web.server_lifecycle", level="INFO") as logs:
            supervisor._check_listeners()
            self.assertTrue(gateway.entered.wait(timeout=2))
            supervisor._check_listeners()
            self.assertTrue(tcp.entered.wait(timeout=2))
            supervisor._check_listeners()
        self.assertEqual(factory.call_count, 3)
        self.assertIn("TCP 配置入口 监听失败", "\n".join(logs.output))
        self.assertIn("TCP 配置入口 已监听", "\n".join(logs.output))
        gateway.server_close.assert_not_called()

    def test_gateway_failure_retries_without_restarting_working_tcp(self):
        supervisor = self.supervisor(socket_path="/unused/app.sock")
        tcp, gateway = FakeServer(), FakeServer()
        with patch("web.server_lifecycle._LocalWSGIServer", side_effect=[tcp, OSError("socket directory not ready"), gateway]) as factory:
            supervisor._check_listeners()
            supervisor._check_listeners()
        self.assertEqual(factory.call_count, 3)
        self.assertTrue(tcp.entered.wait(timeout=2))
        self.assertTrue(gateway.entered.wait(timeout=2))
        tcp.server_close.assert_not_called()

    def test_all_entries_failed_then_recover_without_recreating_app(self):
        supervisor = self.supervisor(socket_path="/unused/app.sock")
        factory = Mock(return_value=make_app())
        supervisor._app_factory = factory
        with patch("web.server_lifecycle._LocalWSGIServer", side_effect=[SystemExit(1), SystemExit(1), FakeServer(), FakeServer()]):
            supervisor._check_listeners()
            supervisor._check_listeners()
        factory.assert_called_once()
        self.assertTrue(all(entry.thread.is_alive() for entry in supervisor._listeners.values()))

    def test_watch_retries_initialization_failure_and_stops_cleanly(self):
        supervisor = self.supervisor(retry_interval=0.01)
        supervisor._app_factory = Mock(side_effect=[OSError("config mount not ready"), make_app()])
        server = FakeServer()
        with patch("web.server_lifecycle._LocalWSGIServer", return_value=server) as factory:
            supervisor.start()
            self.assertTrue(server.entered.wait(timeout=2))
            supervisor.stop()
            supervisor._check_listeners()
        factory.assert_called_once()
        self.assertFalse(supervisor._thread.is_alive())
        self.assertTrue(server.release.is_set())
        self.assertIsNone(supervisor._listeners["TCP 配置入口"].thread)

    def test_serve_exception_closes_socket_then_rebinds(self):
        supervisor = self.supervisor()
        failed, recovered = FakeServer(RuntimeError("serve loop failed")), FakeServer()
        with patch("web.server_lifecycle._LocalWSGIServer", side_effect=[failed, recovered]):
            supervisor._check_listeners()
            old_thread = supervisor._listeners["TCP 配置入口"].thread
            old_thread.join(timeout=2)
            failed.server_close.assert_called_once()
            supervisor._check_listeners()
            self.assertTrue(recovered.entered.wait(timeout=2))

    def test_invalid_tcp_port_does_not_prevent_gateway(self):
        supervisor = self.supervisor(socket_path="/unused/app.sock")
        supervisor._port = "invalid"
        server = FakeServer()
        with patch("web.server_lifecycle._LocalWSGIServer", return_value=server) as factory:
            supervisor._check_listeners()
        self.assertTrue(server.entered.wait(timeout=2))
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args[0], "unix:///unused/app.sock")

    def test_repeated_identical_failure_is_logged_at_most_once_per_minute(self):
        supervisor = self.supervisor()
        with patch("web.server_lifecycle._LocalWSGIServer", side_effect=SystemExit(1)), \
             patch("web.server_lifecycle.time.monotonic", side_effect=[100, 105, 161]), \
             self.assertLogs("web.server_lifecycle", level="WARNING") as logs:
            for _ in range(3):
                supervisor._check_listeners()
        self.assertEqual(len(logs.output), 2)


class RealHTTPRecoveryTests(unittest.TestCase):
    def wait_until(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail("Web service did not recover within three seconds")

    def test_occupied_port_recovers_after_release_and_after_serve_loop_exits(self):
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        supervisor = UIServerSupervisor(make_app, "127.0.0.1", str(port), retry_interval=0.02)
        self.addCleanup(supervisor.stop)
        supervisor.start()
        entry = supervisor._listeners["TCP 配置入口"]
        self.assertIsNone(entry.server)
        occupied.close()
        self.wait_until(lambda: entry.server is not None)
        opener = build_opener(ProxyHandler({}))
        url = f"http://127.0.0.1:{port}/healthz"
        with opener.open(url, timeout=2) as response:
            self.assertEqual(json.load(response), {"ok": True, "service": "fn-message-bots"})
        first = entry.server
        first.shutdown()
        self.wait_until(lambda: entry.server is not None and entry.server is not first)
        with opener.open(url, timeout=2) as response:
            self.assertEqual(response.status, 200)

    def healthcheck(self, port, host="0.0.0.0"):
        # 隔离进程探测（macOS sandbox 无 pgrep 权限），实际运行脚本中的 HTTP 检查。
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "pgrep").write_text("#!/bin/sh\nexit 0\n")
            (folder / "pgrep").chmod(0o755)
            (folder / "python").symlink_to(sys.executable)
            return subprocess.run(["bash", str(root / "healthcheck.sh")], capture_output=True, text=True, timeout=5, env={
                **os.environ, "PATH": str(folder) + os.pathsep + os.environ.get("PATH", ""),
                "UI_HOST": host, "UI_PORT": str(port),
                "HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": "",
            })

    def start_http(self, body=None, host="127.0.0.1"):
        supervisor = UIServerSupervisor(lambda: make_app(body), host, "0")
        self.addCleanup(supervisor.stop)
        supervisor.start()
        return supervisor._listeners["TCP 配置入口"].server.port

    def test_listener_does_not_require_reverse_dns(self):
        with patch("socket.getfqdn", side_effect=AssertionError("DNS must not gate Web startup")):
            port = self.start_http()
            with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                self.assertEqual(json.load(response), {"ok": True, "service": "fn-message-bots"})

    def test_real_unix_socket_keeps_gateway_prefix_handling(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "app.sock")
            supervisor = UIServerSupervisor(make_app, "127.0.0.1", "0", path, "/app/FnMessageBot").start()
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(2)
                    client.connect(path)
                    client.sendall(b"GET /app/FnMessageBot/healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
                    with client.makefile("rb") as stream:
                        response = stream.read()
                headers, body = response.split(b"\r\n\r\n", 1)
                self.assertIn(b"200 OK", headers)
                self.assertEqual(json.loads(body), {"ok": True, "service": "fn-message-bots"})
            finally:
                supervisor.stop()

    @unittest.skipUnless((root / "healthcheck.sh").is_file(), "Docker 专用健康检查不属于 FPK")
    def test_healthcheck_requires_project_response_and_bypasses_proxy(self):
        result = self.healthcheck(self.start_http())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for body in ({"ok": True, "service": "another-app"}, "not JSON"):
            with self.subTest(body=body):
                result = self.healthcheck(self.start_http(body))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    @unittest.skipUnless((root / "healthcheck.sh").is_file(), "Docker 专用健康检查不属于 FPK")
    def test_healthcheck_fails_when_process_exists_but_no_http_listener(self):
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
        result = self.healthcheck(port)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("配置 UI 无法正常响应", result.stdout)

    def test_ipv6_loopback_and_docker_healthcheck(self):
        with socket.socket(socket.AF_INET6) as probe:
            try:
                probe.bind(("::1", 0))
            except OSError as error:
                self.skipTest(f"IPv6 loopback unavailable: {error}")
        port = self.start_http(host="::1")
        with build_opener(ProxyHandler({})).open(f"http://[::1]:{port}/healthz", timeout=2) as response:
            self.assertEqual(json.load(response), {"ok": True, "service": "fn-message-bots"})
        if (root / "healthcheck.sh").is_file():
            result = self.healthcheck(port, host="::")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ApplicationWiringTests(unittest.TestCase):
    def test_monitoring_continues_and_shutdown_stops_ui_supervisor(self):
        from main import Application
        application = Application()
        application.initialize = Mock(return_value=True)
        application.notifier = Mock()
        application.notifier.get_stats.return_value = {}
        application.log_poller = Mock()
        application._mark_app_start_notice = Mock(return_value=True)
        supervisor = Mock()

        def finish(_):
            application.running = False

        with patch("main.start_ui_server_in_background", return_value=supervisor) as start, \
             patch("main.start_nas_patrol_thread"), patch("main.start_wan_ip_monitor_thread"), \
             patch("main.signal.signal"), patch("main.time.sleep", side_effect=finish), \
             patch("web.ui_app.set_runtime_monitor_app"):
            application.run()
        start.assert_called_once_with(on_config_saved=application.reload_config)
        application.log_poller.start.assert_called_once()
        self.assertEqual(application.notifier.send_system_notification.call_args_list[0].args[0], "APP_START")
        supervisor.stop.assert_called_once()
        self.assertIsNone(application._ui_server)


if __name__ == "__main__":
    unittest.main()
