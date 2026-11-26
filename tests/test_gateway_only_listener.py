"""FPK 只走飞牛网关 Socket 时不应开放 TCP 端口；Docker（只有 TCP）行为保持不变。"""
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

root = Path(__file__).resolve().parents[1]
source = root / "src"
sys.path.insert(0, str(source))

from web.server_lifecycle import UIServerSupervisor  # noqa: E402
from web import ui_app  # noqa: E402

TCP_NAME = "TCP 配置入口"
SOCKET_NAME = "统一网关 Socket"


def make_app():
    app = Flask(__name__)
    app.add_url_rule("/healthz", view_func=lambda: {"ok": True, "service": "fn-message-bots"})
    return app


class GatewayOnlyListenerTests(unittest.TestCase):
    def test_gateway_only_has_no_tcp_listener_and_serves_unix_socket(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "app.sock")
            supervisor = UIServerSupervisor(make_app, "127.0.0.1", "", path, "/app/FnMessageBot")
            self.addCleanup(supervisor.stop)
            self.assertEqual(list(supervisor._listeners), [SOCKET_NAME])
            supervisor.start()
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(2)
                client.connect(path)
                client.sendall(b"GET /app/FnMessageBot/healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
                with client.makefile("rb") as stream:
                    response = stream.read()
            headers, body = response.split(b"\r\n\r\n", 1)
            self.assertIn(b"200 OK", headers)
            self.assertEqual(json.loads(body), {"ok": True, "service": "fn-message-bots"})

    def test_tcp_only_keeps_docker_listener(self):
        supervisor = UIServerSupervisor(make_app, "127.0.0.1", "0")
        self.addCleanup(supervisor.stop)
        self.assertEqual(list(supervisor._listeners), [TCP_NAME])

    def test_both_entries_when_socket_and_port_given(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "app.sock")
            supervisor = UIServerSupervisor(make_app, "127.0.0.1", "0", path, "/app/FnMessageBot")
            self.addCleanup(supervisor.stop)
            self.assertEqual(list(supervisor._listeners), [TCP_NAME, SOCKET_NAME])

    def test_refuses_to_start_without_any_entry(self):
        with self.assertRaises(ValueError):
            UIServerSupervisor(make_app, "127.0.0.1", "")


class StartUiServerEnvTests(unittest.TestCase):
    def test_gateway_env_drops_tcp_port(self):
        with patch.dict("os.environ", {"FNMB_GATEWAY_SOCKET": "/tmp/x.sock", "FNMB_GATEWAY_PREFIX": "/app/FnMessageBot",
                                       "UI_PORT": "19080", "UI_HOST": "127.0.0.1"}, clear=True), \
             patch.object(ui_app, "create_app", return_value=make_app()), \
             patch.object(ui_app, "UIServerSupervisor") as supervisor:
            ui_app.start_ui_server_in_background()
            _, host, port, path, prefix = supervisor.call_args.args
            self.assertEqual((host, port, path, prefix), ("127.0.0.1", "", "/tmp/x.sock", "/app/FnMessageBot"))

    def test_docker_env_defaults_to_18080_tcp(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(ui_app, "create_app", return_value=make_app()), \
             patch.object(ui_app, "UIServerSupervisor") as supervisor:
            ui_app.start_ui_server_in_background()
            _, host, port, path, prefix = supervisor.call_args.args
            self.assertEqual((host, port, path, prefix), ("0.0.0.0", "18080", "", ""))


if __name__ == "__main__":
    unittest.main()
