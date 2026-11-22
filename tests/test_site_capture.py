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
            "https://member.ic.net.cn/login/../user.php",
            "https://member.ic.net.cn/login/%2e%2e/user.php",
            "https://member.ic.net.cn/user.php",
            "https://web.qq.com/",
        ]
        with patch.object(site_capture, "_cdp_json", return_value=[target("other", url) for url in sites]):
            self.assertEqual(site_capture.inspect_browser(), (site_capture.STATUSES["ok"] | {"member_portal_open": True}, None))
            with patch.object(site_capture, "async_playwright") as attach:
                self.assertIsNone(site_capture.capture_site("other", "verification"))
                attach.assert_not_called()
        with patch.object(site_capture, "_cdp_json", return_value=None):
            self.assertEqual(site_capture.inspect_browser(), (site_capture.STATUSES["closed"], None))
        for url in (LOGIN, "https://member.ic.net.cn/login", "https://member.ic.net.cn/login/verify/step"):
            self.assertEqual(site_capture.page_kind(url), "login")

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

    def browser_session(self, *, loader_after=None, navigates=False, false_document=False, encoded=None,
                        viewport_changes=None, navigate_after_input=False):
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
                viewport = {"pageX": 0, "pageY": 0, "clientWidth": 1600, "clientHeight": 900,
                            "offsetX": 0, "offsetY": 0, "scale": 1}
                return {"cssVisualViewport": viewport | (viewport_changes or {})}
            if command == "Page.captureScreenshot":
                if loader_after:
                    frame["loaderId"] = loader_after
                if navigates:
                    events["Page.navigatedWithinDocument"]({"frameId": "main-frame"})
                return {"data": encoded if encoded is not None else base64.b64encode(JPEG).decode("ascii")}
            if command.startswith("Input."):
                if navigate_after_input:
                    events["Page.frameStartedNavigating"]({"frameId": "main-frame"})
                return {}
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

    def capture(self, manager, *, kind="verification", frame=False):
        with patch.object(site_capture, "_cdp_json", side_effect=[
            [target("challenge", CAPTCHA)], {"webSocketDebuggerUrl": ENDPOINT},
        ]), patch.object(site_capture, "async_playwright", return_value=manager):
            capture = site_capture.capture_frame if frame else site_capture.capture_site
            return capture("challenge", kind)

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
            "Page.getLayoutMetrics",
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

    def frame(self, **options):
        _, _, _, manager, _ = self.browser_session(**options)
        frame = self.capture(manager, frame=True)
        self.assertIsInstance(frame, site_capture.PageFrame)
        self.assertEqual(frame.image, JPEG)
        return frame

    def relay(self, frame, action, *, url=CAPTCHA, **options):
        session, _, _, manager, _ = self.browser_session(**options)
        with patch.object(site_capture, "_cdp_json", side_effect=[
            [target("challenge", url)], {"webSocketDebuggerUrl": ENDPOINT},
        ]), patch.object(site_capture, "async_playwright", return_value=manager):
            done = site_capture.relay_input(frame, action)
        inputs = [call.args for call in session.send.await_args_list if call.args[0].startswith("Input.")]
        return done, inputs

    def test_relay_click_uses_clipped_frame_dimensions_without_page_scroll_offset(self):
        scroll = {"pageX": 20, "pageY": 100}
        frame = self.frame(viewport_changes=scroll)
        done, inputs = self.relay(frame, {"kind": "click", "x": 0.5, "y": 1}, viewport_changes=scroll)
        self.assertTrue(done)
        self.assertEqual([
            (method, params["type"], params["x"], params["y"], params["buttons"], params["modifiers"])
            for method, params in inputs
        ], [
            ("Input.dispatchMouseEvent", "mousePressed", 800, 899, 1, 0),
            ("Input.dispatchMouseEvent", "mouseReleased", 800, 899, 0, 0),
        ])

    def test_relay_drag_uses_only_submitted_coordinates_and_stops_on_navigation(self):
        frame = self.frame()
        action = {"kind": "drag", "points": [{"x": 0.25, "y": 0.5}, {"x": 0.5, "y": 0.5}, {"x": 0.75, "y": 0.5}]}
        done, inputs = self.relay(frame, action)
        self.assertTrue(done)
        self.assertEqual([(params["type"], params["x"], params["y"]) for _, params in inputs], [
            ("mousePressed", 400, 450), ("mouseMoved", 800, 450),
            ("mouseMoved", 1200, 450), ("mouseReleased", 1200, 450),
        ])
        done, inputs = self.relay(frame, action, navigate_after_input=True)
        self.assertFalse(done)
        self.assertEqual([params["type"] for _, params in inputs], ["mousePressed"])

    def test_relay_discards_stale_viewport_or_address_and_pinched_frame(self):
        frame = self.frame()
        click = {"kind": "click", "x": 0.5, "y": 0.25}
        self.assertEqual(self.relay(frame, click, viewport_changes={"pageY": 100}), (False, []))
        self.assertEqual(self.relay(frame, click, url="https://www.ic.net.cn/search/fixture.html"), (False, []))
        self.assertEqual(self.relay(frame, click, url="https://www.ic.net.cn/searchPnCode.php?session=other"), (False, []))
        pinch = self.frame(viewport_changes={"scale": 1.5})
        self.assertEqual(self.relay(pinch, click, viewport_changes={"scale": 1.5}), (False, []))

    def test_relay_text_and_key_use_only_page_input_commands(self):
        frame = self.frame()
        self.assertEqual(self.relay(frame, {"kind": "text", "text": "人工文本 12"}),
                         (True, [("Input.insertText", {"text": "人工文本 12"})]))
        done, inputs = self.relay(frame, {"kind": "key", "key": "ArrowRight"})
        self.assertTrue(done)
        self.assertEqual([(method, params["type"], params["key"], params["modifiers"]) for method, params in inputs], [
            ("Input.dispatchKeyEvent", "rawKeyDown", "ArrowRight", 0),
            ("Input.dispatchKeyEvent", "keyUp", "ArrowRight", 0),
        ])

    def test_relay_rejects_out_of_range_and_unrecognized_actions_before_discovery(self):
        frame = self.frame()
        with patch.object(site_capture, "_page_targets") as discover:
            for action in (
                {"kind": "click", "x": True, "y": 0}, {"kind": "click", "x": float("nan"), "y": 0},
                {"kind": "click", "x": 0.5, "y": 1.1}, {"kind": "drag", "points": [{"x": 0, "y": 1}] * 41},
                {"kind": "key", "key": "F12"}, {"kind": "key", "key": "Tab", "ctrlKey": True},
                {"kind": "text", "text": "含换行\n"}, {"kind": "text", "text": "x" * 257},
                {"kind": "clipboard", "text": "unknown"},
            ):
                with self.subTest(action=action):
                    self.assertFalse(site_capture.relay_input(frame, action))
            discover.assert_not_called()
