"""Telegram bot tokens must not reach config views, logs or returned errors."""
import logging
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notifier.connection_pool import ConnectionPool
from web.config_store import load_raw_config, mask_channels, restore_masked_channels, save_raw_config


TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


class TelegramTokenRedactionTests(unittest.TestCase):
    def test_config_round_trip_keeps_secret_only_in_stored_value(self):
        stored = [{"type": "webhook", "url": json.dumps({"url": URL, "method": "POST"}), "enabled": True}]
        displayed = mask_channels(stored)
        self.assertNotIn(TOKEN, str(displayed))
        self.assertIn("bot******", displayed[0]["url"])
        restored, error = restore_masked_channels(displayed, stored)
        self.assertEqual(error, "")
        self.assertEqual(restored, stored)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            save_raw_config(config, {"push_channels": restored})
            self.assertEqual(load_raw_config(config)["push_channels"], stored)

    def test_request_failures_never_expose_token(self):
        pool = ConnectionPool(max_retries=0)
        failures = [
            requests.exceptions.Timeout(URL),
            requests.exceptions.ConnectionError(URL),
            RuntimeError(URL),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch.object(pool.session, "request", side_effect=failure):
                    with self.assertLogs("notifier.connection_pool", logging.ERROR) as logs:
                        result = pool.post(URL, {"text": "test"})
                self.assertNotIn(TOKEN, " ".join(logs.output) + str(result))
        pool.close()

    def test_proxy_url_with_port_is_also_redacted(self):
        proxy_url = f"https://telegram-proxy.example:8443/bot{TOKEN}/sendMessage"
        self.assertNotIn(TOKEN, ConnectionPool._safe_url(proxy_url))

    def test_http_error_and_get_failure_redact_token(self):
        pool = ConnectionPool(max_retries=0)
        response = Mock(status_code=401, content=b'error')
        response.text = URL
        response.json.side_effect = ValueError("not json")
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(URL, response=response)
        with patch.object(pool.session, "request", return_value=response):
            with self.assertLogs("notifier.connection_pool", logging.ERROR) as logs:
                result = pool.post(URL, {"text": "test"})
        self.assertNotIn(TOKEN, " ".join(logs.output) + str(result))
        with patch.object(pool.session, "get", side_effect=requests.exceptions.ConnectionError(URL)):
            with self.assertLogs("notifier.connection_pool", logging.ERROR) as logs:
                result = pool.get(URL)
        self.assertNotIn(TOKEN, " ".join(logs.output) + str(result))
        pool.close()

    def test_response_echo_does_not_return_token(self):
        pool = ConnectionPool(max_retries=0)
        response = Mock(status_code=200, content=b'json')
        response.json.return_value = {"echo": {"url": URL}}
        response.raise_for_status.return_value = None
        with patch.object(pool.session, "request", return_value=response):
            result = pool.post(URL, {"text": "test"})
        self.assertTrue(result["success"])
        self.assertNotIn(TOKEN, str(result))
        pool.close()
