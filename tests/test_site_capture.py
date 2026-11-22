"""离线验证办公端截图只读取专用 Edge 中的受支持页面。"""
import base64
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from inquiry import browser, site_capture


CAPTCHA = "https://www.ic.net.cn/searchPnCode.php?l=ins"
LOGIN = "https://member.ic.net.cn/login.php"
ENDPOINT = f"ws://127.0.0.1:{browser.CDP_PORT}/devtools/browser/sample-browser-id"
JPEG = b"\xff\xd8\xfffixture\xff\xd9"


def target(target_id, url, kind="page"):
    return {"id": target_id, "type": kind, "url": url}


class SiteCaptureTests(unittest.TestCase):
    def test_inspection_reports_login_before_verification_and_excludes_non_pages(self):
        with patch.object(site_capture, "_cdp_json", return_value=[
            target("iframe", LOGIN, "iframe"), target("verify", CAPTCHA), target("login", LOGIN),
        ]) as request:
            self.assertEqual(site_capture.inspect_browser(), (site_capture.STATUSES["login"], "login"))
        request.assert_called_once_with("/json/list")
        with patch.object(site_capture, "_cdp_json", return_value=[target("verify", CAPTCHA)]):
            self.assertEqual(site_capture.inspect_browser(), (site_capture.STATUSES["verification"], "verify"))

    def test_only_exact_https_help_pages_are_eligible(self):
        sites = [
            "https://www.ic.net.cn.evil.example/searchPnCode.php",
            "https://evil.example/?source=https://www.ic.net.cn/searchPnCode.php",
            "https://www.ic.net.cn@evil.example/searchPnCode.php",
            "https://secret@www.ic.net.cn/searchPnCode.php",
            "https://www.ic.net.cn:443/searchPnCode.php",
            "http://www.ic.net.cn/searchPnCode.php",
            "https://www.ic.net.cn/searchPnCode.php/other",
            "https://member.ic.net.cn/login-other",
            "https://member.ic.net.cn/user.php",
            "https://web.qq.com/",
        ]
        with patch.object(site_capture, "_cdp_json", return_value=[target("other", url) for url in sites]):
            self.assertEqual(site_capture.inspect_browser(), (site_capture.STATUSES["ok"], None))
            with patch.object(site_capture, "async_playwright") as attach:
                self.assertIsNone(site_capture.capture_site("other", "verification"))
                attach.assert_not_called()
        with patch.object(site_capture, "_cdp_json", return_value=None):
            self.assertEqual(site_capture.inspect_browser(), (site_capture.STATUSES["closed"], None))

    def test_cdp_discovery_does_not_follow_redirects_or_external_websocket(self):
        with patch.object(site_capture.http.client, "HTTPConnection") as connect:
            connection = connect.return_value
            connection.getresponse.return_value.status = 302
            self.assertIsNone(site_capture._cdp_json("/json/list"))
            connect.assert_called_once_with("127.0.0.1", browser.CDP_PORT, timeout=2)
            connection.request.assert_called_once_with("GET", "/json/list")
            connection.close.assert_called_once()
        with patch.object(site_capture, "_cdp_json", return_value={
            "webSocketDebuggerUrl": ENDPOINT.replace("127.0.0.1", "other.example"),
        }):
            self.assertIsNone(site_capture._socket_url())

    def browser_session(self, *, loader_after=None, navigates=False, false_document=False, encoded=None):
        events = {}
        frame = {"id": "main-frame", "loaderId": "loader", "url": CAPTCHA, "securityOrigin": "https://www.ic.net.cn"}
        if false_document:
            frame["unreachableUrl"] = CAPTCHA
        session = Mock()
        session.on.side_effect = lambda key, callback: events.update({key: callback})
        session.detach = AsyncMock()

        def response(command, *_):
            if command == "Target.getTargetInfo":
                return {"targetInfo": {"targetId": "challenge", "url": CAPTCHA, "type": "page"}}
            if command == "Page.getFrameTree":
                return {"frameTree": {"frame": frame.copy()}}
            if command == "Page.getLayoutMetrics":
                return {"cssVisualViewport": {"pageX": 0, "pageY": 0, "clientWidth": 1600, "clientHeight": 900}}
            if command == "Page.captureScreenshot":
                if loader_after:
                    frame["loaderId"] = loader_after
                if navigates:
                    events["Page.navigatedWithinDocument"]({"frameId": "main-frame"})
                return {"data": encoded if encoded is not None else base64.b64encode(JPEG).decode("ascii")}
            self.assertEqual(command, "Page.enable")
            return {}

        session.send = AsyncMock(side_effect=response)
        page = Mock(url=CAPTCHA)
        other_page = Mock(url="https://web.qq.com/")
        context = Mock(pages=[other_page, page], new_cdp_session=AsyncMock(return_value=session))
        manager = MagicMock()
        connect = manager.__aenter__.return_value.chromium.connect_over_cdp = AsyncMock(
            return_value=Mock(contexts=[context]))
        return session, page, context, manager, connect

    def capture(self, manager, *, kind="verification"):
        with patch.object(site_capture, "_cdp_json", side_effect=[
            [target("challenge", CAPTCHA)], {"webSocketDebuggerUrl": ENDPOINT},
        ]), patch.object(site_capture, "async_playwright", return_value=manager):
            return site_capture.capture_site("challenge", kind)

    def test_capture_uses_bound_page_session_without_touching_other_tabs(self):
        session, page, context, manager, connect = self.browser_session()
        self.assertEqual(self.capture(manager), JPEG)
        connect.assert_awaited_once()
        self.assertEqual(connect.call_args.args, (ENDPOINT,))
        self.assertTrue(connect.call_args.kwargs["no_defaults"])
        context.new_cdp_session.assert_awaited_once_with(page)
        session.detach.assert_awaited_once()
        commands = [(call.args[0], call.args[1:]) for call in session.send.await_args_list]
        capture_args = next(args[0] for command, args in commands if command == "Page.captureScreenshot")
        self.assertEqual(capture_args["format"], "jpeg")
        self.assertFalse(capture_args["captureBeyondViewport"])
        self.assertEqual(capture_args["clip"]["scale"], 1280 / 1600)
        self.assertEqual([command for command, _ in commands], [
            "Target.getTargetInfo", "Page.enable", "Target.getTargetInfo", "Page.getFrameTree",
            "Page.getLayoutMetrics", "Page.captureScreenshot", "Target.getTargetInfo", "Page.getFrameTree",
        ])

    def test_document_navigation_races_discard_images(self):
        for configuration in ({"loader_after": "replacement"}, {"navigates": True}):
            with self.subTest(configuration=configuration):
                session, _, _, manager, _ = self.browser_session(**configuration)
                self.assertIsNone(self.capture(manager))
                self.assertTrue(any(call.args[0] == "Page.captureScreenshot" for call in session.send.await_args_list))
                session.detach.assert_awaited_once()

    def test_unreachable_main_document_or_wrong_kind_does_not_capture(self):
        session, _, _, manager, connect = self.browser_session(false_document=True)
        self.assertIsNone(self.capture(manager, kind="login"))
        connect.assert_not_awaited()
        self.assertIsNone(self.capture(manager))
        self.assertFalse(any(call.args[0] == "Page.captureScreenshot" for call in session.send.await_args_list))
        session.detach.assert_awaited_once()

    def test_invalid_or_oversized_cdp_images_are_discarded(self):
        for data in ("not-base64!", base64.b64encode(b"not jpeg").decode("ascii"),
                     base64.b64encode(b"\x00" * (site_capture.MAX_SCREENSHOT_BYTES + 1)).decode("ascii")):
            with self.subTest(encoded_length=len(data)):
                _, _, _, manager, _ = self.browser_session(encoded=data)
                self.assertIsNone(self.capture(manager))
