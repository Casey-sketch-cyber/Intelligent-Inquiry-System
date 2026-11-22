"""企业微信提醒只推送固定文字，网络调用在离线测试中完全替换。"""

import json
import tempfile
import unittest
from http.client import HTTPException
from pathlib import Path
from unittest.mock import patch

from inquiry.runtime import TEMP_DIR
from lanhost import alert_webhook

KEY = "00000000-0000-0000-0000-000000000000"
URL = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={KEY}"


class AlertWebhookTests(unittest.TestCase):
    def setUp(self):
        fixture = self.enterContext(tempfile.TemporaryDirectory(dir=TEMP_DIR))
        self.path = Path(fixture) / "website-alert-webhook.json"
        self.enterContext(patch.object(alert_webhook, "PATH", self.path))
        self.connection = self.enterContext(patch.object(alert_webhook, "HTTPSConnection"))

    def test_default_empty_and_disabling_leave_no_network_activity(self):
        self.assertEqual(alert_webhook.load(), "")
        alert_webhook.push("login")
        alert_webhook.save(URL)
        self.assertEqual(alert_webhook.load(), URL)
        alert_webhook.save("")
        self.assertEqual(alert_webhook.load(), "")
        self.assertNotIn(KEY, self.path.read_text(encoding="utf-8"))
        alert_webhook.push("verification")
        self.connection.assert_not_called()

    def test_validation_is_exact_and_preserves_previous_secret(self):
        alert_webhook.save(URL)
        invalid = [
            URL.replace("https:", "http:"),
            URL.replace("qyapi.weixin.qq.com", "qyapi.weixin.qq.com.other.invalid"),
            URL.replace("qyapi.weixin.qq.com", "qyapi.weixin.qq.com:443"),
            URL.replace("qyapi.weixin.qq.com", "my-user@qyapi.weixin.qq.com"),
            URL.replace("/cgi-bin/webhook/send", "/cgi-bin/webhook%2fsend"),
            URL.replace("?key=", "?Key="),
            URL + f"&key={KEY}",
            URL + "#fragment",
            URL + "\n",
            URL.replace(KEY, "invalid-token"),
            None,
            True,
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    alert_webhook.save(value)
                self.assertNotIn(KEY, str(raised.exception))
                self.assertNotIn("invalid-token", str(raised.exception))
                self.assertEqual(alert_webhook.load(), URL)
        self.connection.assert_not_called()

    def test_posts_typed_text_once_even_if_provider_redirects(self):
        alert_webhook.save(URL)
        transport = self.connection.return_value
        transport.getresponse.return_value.status = 302
        transport.getresponse.return_value.headers = {"Location": "https://other.invalid/path"}
        for kind, expected_text in alert_webhook.MESSAGES.items():
            with self.subTest(kind=kind):
                self.connection.reset_mock()
                alert_webhook.push(kind)
                self.connection.assert_called_once_with(alert_webhook.HOST, timeout=3)
                transport.request.assert_called_once()
                method, target = transport.request.call_args.args
                self.assertEqual((method, target), ("POST", f"/cgi-bin/webhook/send?key={KEY}"))
                options = transport.request.call_args.kwargs
                self.assertEqual(json.loads(options["body"]), {"msgtype": "text", "text": {"content": expected_text}})
                self.assertEqual(options["headers"], {"Content-Type": "application/json; charset=utf-8"})
                self.assertNotIn(KEY.encode(), options["body"])
                transport.close.assert_called_once()

    def test_does_not_send_for_any_other_kind_or_invalid_saved_value(self):
        alert_webhook.save(URL)
        alert_webhook.push("contains private text instead of status")
        self.path.write_text('{"url":"https://other.invalid/"}', encoding="utf-8")
        self.assertEqual(alert_webhook.load(), "")
        alert_webhook.push("login")
        self.path.write_text("invalid json", encoding="utf-8")
        self.assertEqual(alert_webhook.load(), "")
        alert_webhook.push("verification")
        self.connection.assert_not_called()

    def test_connection_failure_is_best_effort(self):
        alert_webhook.save(URL)
        transport = self.connection.return_value
        for error in (TimeoutError(URL), HTTPException(URL)):
            with self.subTest(error=type(error).__name__):
                transport.request.side_effect = error
                alert_webhook.push("login")
        self.assertEqual(transport.request.call_count, 2)
        self.assertEqual(transport.close.call_count, 2)
