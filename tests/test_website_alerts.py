"""验证提醒生命周期和本地截图的范围，不连接真实网站或企业微信。"""
from threading import Event
from unittest.mock import Mock, patch

from inquiry import site_capture
from lanhost import website_alerts
from test_reliability import IsolatedTest


class WebsiteAlertsTests(IsolatedTest):
    JPEG = b"\xff\xd8\xfffixture\xff\xd9"

    def setUp(self):
        super().setUp()
        self.file = self.path / "website-alert.jpg"
        self.alerts = website_alerts.WebsiteAlerts(self.file)

    def test_repeated_poll_keeps_one_alert_and_clears_image_after_recovery(self):
        blocked = {"code": "verification"}, "EDGE-TAB"
        with patch.object(site_capture, "inspect_browser", side_effect=[blocked, blocked, ({"code": "ok"}, None), blocked]), \
             patch.object(site_capture, "capture_site", return_value=self.JPEG) as capture, \
             patch.object(website_alerts.threading, "Thread") as notify:
            self.alerts.poll()
            first = self.alerts.status()
            self.assertEqual(first["kind"], "verification")
            self.assertTrue(first["image_ready"])
            self.assertNotIn("target", first)
            self.assertEqual(self.alerts.image(first["id"]), self.JPEG)
            self.assertIsNone(self.alerts.image("other"))
            self.alerts.poll()
            self.assertEqual(self.alerts.status(), first)
            capture.assert_called_once_with("EDGE-TAB", "verification")
            self.assertEqual(notify.call_args.kwargs["args"], ("verification",))
            notify.return_value.start.assert_called_once()
            self.alerts.poll()
            self.assertIsNone(self.alerts.status())
            self.assertIsNone(self.alerts.image(first["id"]))
            self.assertFalse(self.file.exists())
            self.alerts.poll()
            self.assertNotEqual(self.alerts.status()["id"], first["id"])
            self.assertEqual(capture.call_count, 2)
            self.assertEqual(notify.call_count, 2)

    def test_text_survives_missing_target_and_failed_capture(self):
        login = {"code": "login"}
        with patch.object(site_capture, "inspect_browser", side_effect=[(login, None), (login, "LOGIN-TAB"), (login, "LOGIN-TAB")]), \
             patch.object(site_capture, "capture_site", side_effect=[None, self.JPEG]) as capture, \
             patch.object(website_alerts.threading, "Thread") as notify:
            self.alerts.poll()
            text_only = self.alerts.status()
            self.assertEqual(text_only["kind"], "login")
            self.assertFalse(text_only["image_ready"])
            capture.assert_not_called()
            self.alerts.poll()
            self.assertFalse(self.alerts.status()["image_ready"])
            self.alerts.poll()
            self.assertTrue(self.alerts.status()["image_ready"])
            self.assertEqual(self.alerts.status()["id"], text_only["id"])
            notify.assert_called_once()

    def test_inflight_capture_may_not_publish_after_shutdown(self):
        stop = Event()
        capture = Mock(side_effect=lambda *_: (stop.set(), self.JPEG)[1])
        with patch.object(site_capture, "inspect_browser", return_value=({"code": "verification"}, "EDGE-TAB")), \
             patch.object(site_capture, "capture_site", capture), patch.object(website_alerts.threading, "Thread"):
            self.alerts.poll(stop.is_set)
        self.assertFalse(self.alerts.status()["image_ready"])
        self.assertFalse(self.file.exists())
