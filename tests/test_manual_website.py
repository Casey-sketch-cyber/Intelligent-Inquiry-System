"""离线验证人工处理页面的绑定、保护和重放拒绝。"""
import base64
import unittest
from unittest.mock import Mock, patch

from lanhost import manual_website as manual


class ManualWebsiteTests(unittest.TestCase):
    event = "a" * 32
    click = {"kind": "click", "x": 0.25, "y": 0.5}

    def setUp(self):
        self.alerts = Mock()
        self.selection = ("EDGE-TAB", "verification")
        self.alerts.control_target.side_effect = lambda event: self.selection if event == self.event else None
        self.available = Mock(return_value=True)
        self.service = manual.ManualWebsite(self.alerts, self.available)
        self.now = 0
        self.picture = Mock(image=b"\xff\xd8test\xff\xd9")
        self.capture = self.enterContext(patch.object(manual.site_capture, "capture_frame", return_value=self.picture))
        self.send = self.enterContext(patch.object(manual.site_capture, "relay_input", return_value=True))
        self.enterContext(patch.object(manual.time, "monotonic", side_effect=lambda: self.now))

    def test_frame_is_reused_briefly_and_all_prior_tokens_are_consumed(self):
        first = self.service.frame(self.event)
        self.assertEqual(self.service.frame(self.event), first)
        self.assertEqual(base64.b64decode(first["image"]), self.picture.image)
        self.capture.assert_called_once_with(*self.selection)
        self.now = 3
        newer = self.service.frame(self.event)
        self.assertNotEqual(newer["frame_token"], first["frame_token"])
        self.service.relay({"event": self.event, "frame_token": first["frame_token"], "action": self.click})
        self.send.assert_called_once_with(self.picture, self.click)
        for frame in (first, newer):
            with self.assertRaises(manual.ManualPageConflict):
                self.service.relay({"event": self.event, "frame_token": frame["frame_token"], "action": self.click})
        self.send.assert_called_once()

    def test_expired_busy_and_no_longer_current_inputs_are_rejected(self):
        token = self.service.frame(self.event)["frame_token"]
        payload = {"event": self.event, "frame_token": token, "action": self.click}
        self.available.return_value = False
        with self.assertRaises(manual.ManualPageConflict):
            self.service.relay(payload)
        self.available.return_value = True
        self.now = manual.FRAME_TTL
        with self.assertRaises(manual.ManualPageConflict):
            self.service.relay(payload)
        current = self.service.frame(self.event)
        self.selection = ("ANOTHER", "login")
        with self.assertRaises(manual.ManualPageConflict):
            self.service.relay(payload | {"frame_token": current["frame_token"]})
        self.selection = None
        with self.assertRaises(manual.ManualPageConflict):
            self.service.frame(self.event)
        self.send.assert_not_called()

    def test_failed_capture_is_throttled_and_epoch_changes_fail_closed(self):
        self.capture.return_value = None
        for _ in range(2):
            with self.assertRaises(manual.ManualPageUnavailable):
                self.service.frame(self.event)
        self.capture.assert_called_once()
        self.now = 4

        def navigate(*_):
            self.selection = ("NEW-TAB", "login")
            return self.picture

        self.capture.side_effect = navigate
        with self.assertRaises(manual.ManualPageConflict):
            self.service.frame(self.event)
        self.send.assert_not_called()

    def test_only_explicit_supported_input_shapes_can_pass(self):
        p = {"x": 0, "y": 1}
        allowed = (self.click, {"kind": "drag", "points": [p, p]}, {"kind": "key", "key": "Tab"},
                   {"kind": "text", "text": "验证码 123"})
        for action in allowed:
            self.assertEqual(manual.input_action(action), action)
        wrong = (
            self.click | {"x": True}, self.click | {"x": float("nan")}, self.click | {"x": 1.1},
            self.click | {"target": "QQ"}, {"kind": "drag", "points": [p] * 41}, {"kind": "drag", "points": [p]},
            {"kind": "key", "key": "Control+A"}, {"kind": "key", "key": []}, {"kind": "text", "text": "\n"},
            {"kind": "text", "text": "x" * 257}, {"kind": "text", "text": "\ud800"}, {"kind": "navigate"},
        )
        for action in wrong:
            with self.subTest(action=repr(action)), self.assertRaises(ValueError):
                manual.input_action(action)
