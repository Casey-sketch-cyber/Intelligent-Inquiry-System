"""双机边界与真实 TLS 本机回环测试；不操作 QQ、不发送消息。"""
import base64
import json
import socket
import threading
import time
import uuid
import zipfile
from unittest.mock import Mock, patch
from email.utils import formatdate

from test_reliability import IsolatedTest
from inquiry import isolation, qq, db
from ui import server
from lanhost import config, controller, worker, package
from lanhost import alert_webhook, website_alerts
from lanhost.transport import Journal, Remote, make_server, check_request
from vmhost.bridge import BridgeError


class LanTest(IsolatedTest):
    def setUp(self):
        super().setUp()
        for name, value in (("ENABLED", False), ("MODE", "local"), ("ALLOW_SEND", False),
                            ("MANUAL", threading.Event()), ("DRAINING", threading.Event())):
            p = patch.object(isolation, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("LAN_DIR", self.path / "lan"), ("CONTROLLER", self.path / "lan/office.json"),
                            ("CONNECTION", self.path / "lan/connection.json"), ("WORKER", self.path / "worker.json")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        config.LAN_DIR.mkdir()
        self.office = {"controller_id": uuid.uuid4().hex, "office_device": "a" * 64}
        self.cfg = {"worker_id": uuid.uuid4().hex, "controller_id": self.office["controller_id"],
                    "secret": "b" * 64, "name": "测试工作机", "version": 1, "role": "worker",
                    "office_device": "a" * 64, "worker_device": "c" * 64, "runtime": str(config.ROOT),
                    "allow_send": False}
        pem, fingerprint = config.certificate(config.LAN_DIR, self.cfg["worker_id"])
        self.cfg.update(certificate=pem, fingerprint=fingerprint)

    def start_rpc(self, handler, clock_skew=0, legacy_head=False):
        journal = Journal(self.path / (uuid.uuid4().hex + ".db"))
        httpd = make_server(self.cfg, handler, journal, config.LAN_DIR / "server.pem",
                            config.LAN_DIR / "server-key.pem", host="127.0.0.1", preferred=0)
        self.httpd = httpd
        if clock_skew:
            httpd.RequestHandlerClass.date_time_string = lambda _: formatdate(time.time() + clock_skew, usegmt=True)
        if legacy_head:
            httpd.RequestHandlerClass.do_HEAD = lambda request: request.send_error(501)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        def close():
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=2)
        self.addCleanup(close)
        descriptor = config.descriptor(self.cfg, httpd.server_port, ["127.0.0.1"])
        return Remote(descriptor), descriptor, journal

    def value(self, **changes):
        value = {"id": uuid.uuid4().hex, "method": "POST", "path": "/api/run", "expires": time.time() + 40,
                 "body": base64.b64encode(b'{}').decode(), "content_type": "application/json",
                 "controller_id": self.cfg["controller_id"]}
        value.update(changes)
        return value

    def test_real_tls_roundtrip_and_wrong_certificate_sends_no_application_request(self):
        handler = Mock(return_value=(200, {"Content-Type": "application/json"}, b'{"ok":true}'))
        remote, descriptor, _ = self.start_rpc(handler)
        self.assertEqual(remote.call("GET", "/api/tasks")[0], 200)
        self.assertEqual(handler.call_count, 1)
        bad = Remote(descriptor | {"fingerprint": "f" * 64})
        with self.assertRaises(BridgeError):
            bad.call("POST", "/api/run", b'{}')
        self.assertEqual(handler.call_count, 1)

    def test_unauthenticated_request_never_reaches_business_handler(self):
        handler = Mock(return_value=(200, {}, b'{}'))
        _, descriptor, _ = self.start_rpc(handler)
        with self.assertRaises(BridgeError):
            Remote(descriptor | {"secret": "d" * 64}).call("POST", "/api/run", b'{}')
        handler.assert_not_called()

    def test_website_screenshot_is_bound_to_current_event_and_skips_rpc_journal(self):
        event = uuid.uuid4().hex
        screenshot = b"\xff\xd8\xfffixture\xff\xd9"
        alerts = website_alerts.WebsiteAlerts(config.LAN_DIR / "website-alert.jpg")
        with patch.object(website_alerts.site_capture, "inspect_browser", return_value=({"code": "login"}, "EDGE-TAB")), \
             patch.object(website_alerts.site_capture, "capture_site", return_value=screenshot), \
             patch.object(website_alerts.uuid, "uuid4", return_value=Mock(hex=event)), \
             patch.object(alert_webhook, "push"):
            alerts.poll()
        with patch.object(worker, "WEBSITE_ALERTS", alerts):
            remote, descriptor, journal = self.start_rpc(worker.handler(self.cfg))
            config.save_json(config.CONNECTION, descriptor)
            self.enterContext(patch.object(controller, "remote", return_value=remote))
            client = controller.app.test_client()
            status = client.get("/api/lan/status").json
            self.assertEqual(status["website_alert"], {"id": event, "kind": "login", "image_ready": True})
            path = f"/api/lan/website-alert/image?event={event}"
            image = client.get(path)
            self.assertEqual(image.data, screenshot)
            self.assertEqual(image.content_type, "image/jpeg")
            self.assertEqual(image.headers.get("Cache-Control"), "no-store")
            self.assertEqual(image.headers.get("Cross-Origin-Resource-Policy"), "same-origin")
            self.assertEqual(client.get(path, headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
            self.assertEqual(client.get(path + "&event=" + event).status_code, 404)
            with self.assertRaises(BridgeError):
                Remote(descriptor | {"secret": "d" * 64}).call("GET", f"/api/worker/website-alert/image?event={event}")
            with journal.connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM request").fetchone()[0], 0)
            alerts.clear()
            self.assertIsNone(client.get("/api/lan/status").json["website_alert"])
            self.assertEqual(client.get(path).status_code, 404)

    def test_website_webhook_settings_are_write_only_and_require_same_origin(self):
        secret = "12345678-1234-1234-1234-123456789abc"
        url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={secret}"
        path = "/api/lan/website-alert/settings"
        with patch.object(alert_webhook, "PATH", self.path / "webhook.json"), \
             patch.object(alert_webhook, "push") as notify:
            remote, descriptor, journal = self.start_rpc(worker.handler(self.cfg))
            config.save_json(config.CONNECTION, descriptor)
            self.enterContext(patch.object(controller, "remote", return_value=remote))
            client = controller.app.test_client()
            origin = {"Origin": "http://localhost"}
            self.assertEqual(client.get(path).json, {"configured": False})
            self.assertEqual(client.put(path, json={"webhook_url": url}).status_code, 403)
            self.assertEqual(client.put(path, json={"webhook_url": url}, headers=origin).json, {"configured": True})
            self.assertNotIn(secret, client.get(path).get_data(as_text=True))
            invalid = client.put(path, json={"webhook_url": "https://example.test/" + secret}, headers=origin)
            self.assertEqual(invalid.status_code, 400)
            self.assertNotIn(secret, invalid.get_data(as_text=True))
            self.assertEqual(client.put(path, json={"webhook_url": ""}, headers=origin).json, {"configured": False})
            with journal.connect() as conn:
                self.assertNotIn(secret, str(conn.execute("SELECT body FROM request").fetchall()))
            notify.assert_not_called()

    def test_duplicate_request_executes_once_and_changed_payload_is_rejected(self):
        journal = Journal(self.path / "journal.db")
        handler = Mock(return_value=(202, {"Content-Type": "application/json"}, b'{"id":1}'))
        value = self.value()
        self.assertEqual(journal.execute(value, handler), journal.execute(value, handler))
        handler.assert_called_once()
        with self.assertRaises(BridgeError):
            journal.execute(value | {"path": "/api/collect"}, handler)

    def test_crash_after_side_effect_is_not_replayed_after_recovery(self):
        journal = Journal(self.path / "journal.db")
        handler = Mock(side_effect=RuntimeError("process interrupted after side effect"))
        value = self.value()
        self.assertEqual(journal.execute(value, handler)[0], 409)
        journal.recover()
        self.assertEqual(journal.execute(value, handler)[0], 409)
        handler.assert_called_once()

    def test_old_processing_entry_is_unknown_and_expired_requests_do_not_execute(self):
        journal = Journal(self.path / "journal.db")
        value = self.value()
        with journal.connect() as c:
            c.execute("INSERT INTO request VALUES(?,?,'processing',?,NULL,NULL,NULL)", (value["id"], "digest", time.time()))
        journal.recover()
        with journal.connect() as c:
            self.assertEqual(c.execute("SELECT state FROM request").fetchone()[0], "unknown")
        handler = Mock()
        with self.assertRaises(BridgeError):
            journal.execute(self.value(expires=time.time() - 1), handler)
        handler.assert_not_called()

    def test_invalid_routes_are_rejected(self):
        for route in ("http://elsewhere/api/run", "/api/../secret", "/api/%2e%2e/secret", "/api/run\r\nx:y", "/api/abc#fragment"):
            with self.subTest(route=route), self.assertRaises(BridgeError):
                check_request(self.value(path=route))

    def test_timeout_after_transmission_does_not_try_second_address(self):
        calls = []
        completed = threading.Event()
        def delayed(*args):
            calls.append(args)
            time.sleep(.35)
            completed.set()
            return 200, {}, b'{}'
        remote, descriptor, _ = self.start_rpc(delayed)
        remote.cfg["addresses"] = ["127.0.0.1", "127.0.0.1"]
        with self.assertRaises(BridgeError):
            remote.call("POST", "/api/collect", b'{}', timeout=.15)
        self.assertTrue(completed.wait(2))
        self.assertEqual(len(calls), 1)

    def test_multiple_network_interfaces_share_one_connection_time_budget(self):
        descriptor = config.descriptor(self.cfg, 8778, ["192.168.1.50", "192.168.2.50"])
        connection = Mock()
        connection.connect.side_effect = OSError("unreachable")
        with patch("lanhost.transport.time.monotonic", side_effect=[0, 0, 6]), \
                patch("lanhost.transport.http.client.HTTPSConnection", return_value=connection) as connect:
            with self.assertRaises(BridgeError):
                Remote(descriptor).call("GET", "/api/worker/status", timeout=5)
        self.assertEqual(connect.call_count, 1)
        connection.close.assert_called_once()

    def test_status_failure_preserves_pairing_and_reports_connection_reason(self):
        descriptor = config.descriptor(self.cfg, 8778, ["192.168.1.50"])
        with patch.object(Remote, "call", side_effect=BridgeError("连接检查已超时，未向工作机发送请求")):
            offline = Remote(descriptor).status()
        self.assertFalse(offline["online"])
        self.assertIn("超时", offline["error"])
        config.save_json(config.CONNECTION, descriptor)
        remote = Mock()
        remote.status.return_value = offline
        with patch.object(controller, "remote", return_value=remote):
            response = controller.app.test_client().get("/api/lan/status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["paired"])
        self.assertFalse(response.json["online"])
        self.assertEqual(config.read_json(config.CONNECTION), descriptor)
        remote.call.assert_not_called()

    def test_short_network_budget_does_not_expire_signature_with_small_clock_difference(self):
        handler = Mock(return_value=(200, {}, b'{}'))
        remote, _, _ = self.start_rpc(handler)
        validate = check_request
        request_times = []

        def worker_clock(value, *, now=None):
            # 客户端在发出前也会调用验证；只在 RPC 服务线程模拟工作机时钟。
            if threading.current_thread() is threading.main_thread():
                return validate(value, now=now)
            now = time.time()
            request_times.append(value["expires"] - now)
            with patch("lanhost.transport.time.time", return_value=now + 5.5):
                return validate(value)

        with patch("lanhost.transport.check_request", side_effect=worker_clock):
            self.assertEqual(remote.call("GET", "/api/worker/status", timeout=5)[0], 200)
        self.assertTrue(request_times)
        self.assertGreater(request_times[0], 20)
        handler.assert_called_once()

    def test_paired_clock_handles_large_positive_and_negative_skews_with_legacy_worker(self):
        validate = check_request
        for skew in (75, -90, 7200):
            with self.subTest(skew=skew):
                handler = Mock(return_value=(200, {}, b'{}'))
                remote, _, _ = self.start_rpc(handler, clock_skew=skew, legacy_head=True)
                def check_on_worker(value, *, now=None):
                    if threading.current_thread() is threading.main_thread():
                        return validate(value, now=now)
                    return validate(value, now=time.time() + skew)
                with patch("lanhost.transport.check_request", side_effect=check_on_worker):
                    self.assertEqual(remote.call("GET", "/api/worker/status", timeout=5)[0], 200)
                handler.assert_called_once()

    def test_invalid_peer_clock_never_sends_business_request(self):
        handler = Mock(return_value=(200, {}, b'{}'))
        remote, _, _ = self.start_rpc(handler)
        with patch.object(self.httpd.RequestHandlerClass, "date_time_string", return_value="invalid"):
            with self.assertRaisesRegex(BridgeError, "时间校准未通过"):
                remote.call("POST", "/api/run", b'{}')
        handler.assert_not_called()

    def test_clock_is_shared_by_new_remote_instances_and_refreshed_when_expired(self):
        handler = Mock(return_value=(200, {}, b'{}'))
        remote, descriptor, _ = self.start_rpc(handler)
        original = self.httpd.RequestHandlerClass.do_HEAD
        heads = []
        def head(request):
            heads.append(True)
            return original(request)
        with patch.object(self.httpd.RequestHandlerClass, "do_HEAD", head):
            remote.call("GET", "/api/worker/status")
            Remote(descriptor).call("GET", "/api/worker/status")
            self.assertEqual(len(heads), 1)
            with patch("lanhost.transport.CLOCK_CACHE_SECONDS", 0):
                Remote(descriptor).call("GET", "/api/worker/status")
            self.assertEqual(len(heads), 2)
        self.assertEqual(handler.call_count, 3)

    def test_clock_jump_rejects_once_without_replaying_write_then_refreshes_for_next_status(self):
        handler = Mock(return_value=(200, {}, b'{}'))
        remote, _, _ = self.start_rpc(handler)
        real_validate, skew = check_request, [0]
        def on_worker(value, *, now=None):
            if threading.current_thread() is threading.main_thread():
                return real_validate(value, now=now)
            return real_validate(value, now=time.time() + skew[0])
        def date(_):
            return formatdate(time.time() + skew[0], usegmt=True)
        with patch("lanhost.transport.check_request", side_effect=on_worker), \
             patch.object(self.httpd.RequestHandlerClass, "date_time_string", date):
            remote.call("GET", "/api/worker/status")
            skew[0] = 200
            with self.assertRaises(BridgeError):
                remote.call("POST", "/api/run", b'{}')
            self.assertEqual(handler.call_count, 1)
            remote.call("GET", "/api/worker/status")
        self.assertEqual(handler.call_count, 2)

    def test_office_device_cannot_enroll_as_worker_or_enable_desktop(self):
        with patch.object(config, "device_identity", return_value=self.office["office_device"]):
            with self.assertRaises(RuntimeError):
                config.prepare_worker(self.office)
            self.assertFalse(config.WORKER.exists())
        config.save_json(config.WORKER, self.cfg)
        with patch.object(config, "device_identity", return_value=self.office["office_device"]):
            with self.assertRaises(RuntimeError):
                isolation.enable_worker()
        self.assertFalse(isolation.ENABLED)
        with patch.object(qq.win32gui, "SetForegroundWindow") as foreground:
            self.assertFalse(isolation.ensure_qq_foreground(object()))
            foreground.assert_not_called()

    def test_only_paired_worker_can_enable_and_sending_configuration_cannot_be_flipped(self):
        config.save_json(config.WORKER, self.cfg)
        with patch.object(config, "device_identity", return_value=self.cfg["worker_device"]):
            isolation.enable_worker()
            self.assertEqual(isolation.MODE, "worker")
            self.assertFalse(isolation.ALLOW_SEND)
            config.save_json(config.WORKER, self.cfg | {"allow_send": True})
            with self.assertRaises(RuntimeError):
                config.worker_config()

    def test_connection_file_binds_controller_worker_certificate_and_lan_address(self):
        value = config.descriptor(self.cfg, 8778, ["192.168.1.50"])
        self.assertEqual(config.validate_connection(value, self.office)["worker_id"], self.cfg["worker_id"])
        for change in ({"controller_id": uuid.uuid4().hex}, {"worker_device": self.office["office_device"]},
                       {"addresses": ["8.8.8.8"]}, {"addresses": ["127.0.0.1"]}, {"fingerprint": "0" * 64},
                       {"allow_send": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                config.validate_connection(value | change, self.office)

    def test_worker_api_blocks_real_inquiry_reply_and_retry_over_tls(self):
        isolation.ENABLED, isolation.MODE = True, "worker"
        _, _, sid, _ = self.fixture()
        remote, _, _ = self.start_rpc(worker.handler(self.cfg))
        with patch.object(server, "spawn") as spawn:
            for path, body in (("/api/run", {"parts": [{"part": "TEST", "qty": 100}]}),
                               (f"/api/suppliers/{sid}/reply", {"confirm_send": True, "text": "never sent"}),
                               ("/api/outbox/1/retry", {})):
                self.assertEqual(remote.call("POST", path, json.dumps(body).encode(), "application/json")[0], 423)
        spawn.assert_not_called()
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM send_attempt").fetchone()[0], 0)

    def test_controller_rejects_cross_site_requests_and_forwards_without_local_spawn(self):
        client = controller.app.test_client()
        remote = Mock()
        remote.call.return_value = (423, {"Content-Type": "application/json"}, b'{"error":"locked"}')
        with patch.object(controller, "remote", return_value=remote), patch.object(server, "spawn") as spawn:
            self.assertEqual(client.post("/api/run", json={}).status_code, 403)
            self.assertEqual(client.get("/api/lan/status", environ_overrides={"REMOTE_ADDR": "192.168.1.2"}).status_code, 403)
            self.assertEqual(client.get("/api/lan/status", headers={"Host": "evil.example"}).status_code, 403)
            response = client.post("/api/run", json={}, headers={"Origin": "http://localhost"})
            self.assertEqual(response.status_code, 423)
            remote.call.assert_called_once()
            spawn.assert_not_called()

    def test_pause_resume_and_local_status(self):
        isolation.ENABLED, isolation.MODE = True, "worker"
        dispatch = worker.handler(self.cfg)
        with patch.object(server, "run_state", return_value={"running": False}):
            self.assertEqual(dispatch("POST", "/api/worker/pause", b"", "")[0], 200)
            self.assertTrue(isolation.MANUAL.is_set())
            self.assertEqual(dispatch("POST", "/api/worker/resume", b"", "")[0], 200)
            self.assertFalse(isolation.MANUAL.is_set())
            with patch.object(config, "local_addresses", return_value=["192.168.1.50"]):
                app = worker.local_app(self.cfg, self.path / "connection.json", 8778)
                response = app.test_client().get("/api/status")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["port"], 8778)
                self.assertNotIn("secret", response.json)

    def test_archive_contains_application_without_customer_data(self):
        dest = self.path / "source.zip"
        package.source_archive(dest)
        with zipfile.ZipFile(dest) as archive:
            names = set(archive.namelist())
        self.assertIn("lanhost/worker.py", names)
        self.assertIn("lanhost/controller.py", names)
        self.assertNotIn(".env", names)
        self.assertNotIn("settings.toml", names)
        self.assertFalse(any(n.startswith("data/") or n.startswith(".cache/") for n in names))
