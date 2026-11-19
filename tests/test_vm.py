"""虚拟机边界、签名队列、只读发送锁和停机回归；不访问真实 QQ。"""
import base64
import json
import threading
import time
import uuid
from unittest.mock import Mock, patch

from test_reliability import IsolatedTest
from inquiry import isolation, qq, db
from ui import server
from vmhost import bridge as transport, host, manager, guest


class VmTest(IsolatedTest):
    def setUp(self):
        super().setUp()
        for name, value in (("ENABLED", False), ("ALLOW_SEND", False), ("DRAINING", threading.Event()), ("MANUAL", threading.Event())):
            p = patch.object(isolation, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.bridge = transport.Bridge(self.path / "bridge", "a" * 64)

    def heartbeat(self, **kwargs):
        transport.write_atomic(self.bridge.root / "heartbeat.json", {"at": time.time(), **kwargs}, self.bridge.secret)

    def request(self, method="POST", route="/api/run", expires=None):
        rid = uuid.uuid4().hex
        path = self.bridge.root / "requests" / (rid + ".json")
        transport.write_atomic(path, {"id": rid, "method": method, "path": route,
            "expires": expires or time.time() + 30, "body": "", "content_type": ""}, self.bridge.secret)
        return path

    def test_signature_rejects_modified_payload_and_wrong_key(self):
        raw = transport.encode({"action": "read"}, self.bridge.secret)
        for bad in (raw.replace(b"read", b"send"), b"[]", b'{"body":1,"mac":0}'):
            with self.assertRaises(transport.BridgeError):
                transport.decode(bad, self.bridge.secret)
        with self.assertRaises(transport.BridgeError):
            transport.decode(raw, "b" * 64)

    def test_old_heartbeat_is_offline(self):
        transport.write_atomic(self.bridge.root / "heartbeat.json", {"at": time.time()-20}, self.bridge.secret)
        self.assertFalse(self.bridge.status()["online"])
        with self.assertRaises(transport.BridgeError):
            self.bridge.call("POST", "/api/run")
        self.assertEqual(list((self.bridge.root / "requests").iterdir()), [])

    def test_expired_request_never_executes(self):
        path = self.request(expires=time.time()-1)
        handler = Mock()
        self.assertTrue(transport.serve_one(self.bridge, path, handler))
        handler.assert_not_called()
        result = transport.read_signed(self.bridge.root / "responses" / path.name, self.bridge.secret)
        self.assertEqual(result["status"], 408)

    def test_unknown_crash_result_is_not_replayed(self):
        path = self.request()
        handler = Mock(side_effect=RuntimeError("crash after side effect"))
        self.assertFalse(transport.serve_one(self.bridge, path, handler))
        self.assertFalse(transport.serve_one(self.bridge, path, handler))
        transport.recover_interrupted(self.bridge)
        handler.assert_called_once()
        result = transport.read_signed(self.bridge.root / "responses" / path.name, self.bridge.secret)
        self.assertEqual(result["status"], 409)
        self.assertFalse((self.bridge.root / "processing" / path.name).exists())

    def test_timeout_does_not_publish_second_write(self):
        self.heartbeat()
        with self.assertRaises(transport.BridgeError):
            self.bridge.call("POST", "/api/run", timeout=.15)
        paths = list((self.bridge.root / "requests").glob("*.json"))
        self.assertEqual(len(paths), 1)
        handler = Mock()
        transport.serve_one(self.bridge, paths[0], handler)
        handler.assert_not_called()

    def test_signed_roundtrip_through_real_guest_api(self):
        _, task, _, _ = self.fixture()
        self.heartbeat()
        stop = threading.Event()
        def consume():
            while not stop.wait(.005):
                for path in (self.bridge.root / "requests").glob("*.json"):
                    transport.serve_one(self.bridge, path, guest.request_handler)
        t = threading.Thread(target=consume)
        t.start()
        try:
            status, headers, body = self.bridge.call("GET", "/api/tasks", timeout=2)
            self.assertEqual(status, 200)
            self.assertIn(str(task.id), body.decode())
            self.assertIn("application/json", headers["Content-Type"])
        finally:
            stop.set()
            t.join(timeout=3)

    def test_host_rejects_cross_site_and_network_requests(self):
        client = host.app.test_client()
        with patch.object(manager, "start") as start:
            self.assertEqual(client.post("/api/vm/start").status_code, 403)
            self.assertEqual(client.post("/api/vm/start", headers={"Origin":"https://example.com"}).status_code, 403)
            self.assertEqual(client.get("/api/vm/status", environ_overrides={"REMOTE_ADDR":"192.168.1.2"}).status_code, 403)
            self.assertEqual(client.get("/api/vm/status", headers={"Host":"evil.example"}).status_code, 403)
        start.assert_not_called()

    def test_host_forwards_payload_without_executing_qq(self):
        client = host.app.test_client()
        mock = Mock()
        mock.call.return_value = (423, {"Content-Type": "application/json"}, b'{"error":"read only"}')
        with patch.object(host, "bridge", return_value=mock), patch.object(server, "spawn") as spawn:
            response = client.post("/api/run", json={"parts":[{"part":"X","qty":10}]},
                                   headers={"Origin":"http://localhost"})
        self.assertEqual(response.status_code, 423)
        self.assertEqual(mock.call.call_args.args[:2], ("POST","/api/run"))
        spawn.assert_not_called()

    def test_physical_host_cannot_enable_guest_desktop(self):
        cfg = self.path / "pair.json"
        cfg.write_text(json.dumps({"vm_uuid":"paired","secret":"a"*64,"bridge":r"\\VBOXSVR\TianxinBridge"}))
        with patch.object(isolation, "machine_identity", return_value=("Physical PC", "paired")):
            with self.assertRaises(RuntimeError):
                isolation.enable_guest(cfg)
        self.assertFalse(isolation.ENABLED)
        with patch.object(qq.win32gui, "SetForegroundWindow") as foreground:
            self.assertFalse(isolation.ensure_qq_foreground(object()))
        foreground.assert_not_called()

    def test_other_vm_cannot_enable_guest(self):
        cfg = self.path / "pair.json"
        cfg.write_text(json.dumps({"vm_uuid":"paired","secret":"a"*64,"bridge":r"\\VBOXSVR\TianxinBridge"}))
        with patch.object(isolation, "machine_identity", return_value=("VirtualBox","another")):
            with self.assertRaises(RuntimeError):
                isolation.enable_guest(cfg)
        self.assertFalse(isolation.ENABLED)

    def test_readonly_guest_blocks_all_send_entrypoints_before_drafting(self):
        isolation.ENABLED = True
        with patch.object(qq, "verify_supplier") as verify, patch.object(qq, "_focus_editor") as focus:
            for action in (
                lambda: qq.send_text(object(), "hello", expect_chat="supplier", company="supplier"),
                lambda: qq.send_image(object(), "image.jpg", expect_chat="supplier", company="supplier"),
                lambda: qq._paste_and_send(object(), "supplier", Mock(), "image", "supplier"),
                lambda: qq._click_send(object(), "supplier", "text", "supplier"),
            ):
                with self.assertRaises(qq.DesktopDeferred):
                    action()
        verify.assert_not_called()
        focus.assert_not_called()

    def test_guest_readonly_api_blocks_inquiry_reply_and_retry(self):
        isolation.ENABLED = True
        client = server.app.test_client()
        with patch.object(server, "spawn") as spawn:
            for path in ("/api/run","/api/suppliers/1/reply","/api/outbox/1/retry"):
                self.assertEqual(client.post(path, json={}).status_code, 423)
        spawn.assert_not_called()

    def test_guest_drain_rejects_new_jobs_even_when_sending_enabled(self):
        isolation.ENABLED = isolation.ALLOW_SEND = True
        isolation.DRAINING.set()
        self.assertEqual(server.app.test_client().put("/api/settings", json={}).status_code, 409)
        with self.assertRaises(ValueError):
            server.spawn(["--dry-run"], "dry-run", "X")
        with self.assertRaises(qq.DesktopDeferred):
            isolation.check_sending()

    def test_guest_subprocess_requires_pair_validation(self):
        isolation.ENABLED = True
        with patch.object(server, "run_state", return_value={"running":False}), \
             patch.object(server, "RUN_OUT", self.path / "worker.log"), \
             patch.object(server, "RUN", {"proc":None}), patch.object(server.subprocess, "Popen") as popen:
            server.spawn(["--collect","1"], "collect", "X")
        self.assertIn("--vm-guest", popen.call_args.args[0])

    def test_shutdown_never_powers_off_on_missing_ack(self):
        mock = Mock()
        mock.call.side_effect = transport.BridgeError("offline")
        with patch.object(manager, "read_config", return_value={"uuid":"vm"}), \
             patch.object(manager, "info", return_value={"state":"running"}), patch.object(manager, "command") as command:
            with self.assertRaises(transport.BridgeError):
                manager.shutdown(mock)
        command.assert_not_called()

    def test_shutdown_requires_quiescent_worker(self):
        mock = Mock()
        mock.call.return_value = (200,{},b"{}")
        mock.status.return_value = {"online":True,"shutdown_ready":False}
        with patch.object(manager, "read_config", return_value={"uuid":"vm"}), \
             patch.object(manager, "info", return_value={"state":"running"}), patch.object(manager, "command") as command:
            with self.assertRaises(RuntimeError):
                manager.shutdown(mock, timeout=.01)
        command.assert_not_called()

    def test_shutdown_uses_only_normal_acpi_after_ack(self):
        mock = Mock()
        mock.call.return_value = (200,{},b"{}")
        mock.status.return_value = {"online":True,"shutdown_ready":True}
        with patch.object(manager, "read_config", return_value={"uuid":"vm"}), \
             patch.object(manager, "info", return_value={"state":"running"}), patch.object(manager, "command") as command:
            self.assertEqual(manager.shutdown(mock)["state"], "stopping")
        command.assert_called_once_with("controlvm","vm","acpipowerbutton")

    def test_idle_collection_limits_one_chat_and_moves_to_next(self):
        runner, task, sid, _ = self.fixture()
        runner.chat_owner = {"a":[(task,sid)],"b":[(task,sid+1)]}
        contacts = [{"name":n,"time":"12:00","summary":"reply","unread":0} for n in ("a","b")]
        with patch.object(qq, "recent_contacts", return_value=contacts), patch.object(runner, "read_chat", return_value=True) as read:
            runner.poll_replies(max_reads=1)
            self.assertEqual(read.call_count, 1)
            runner.poll_replies(max_reads=1)
            self.assertEqual(read.call_count, 2)
            runner.poll_replies(max_reads=1)
            self.assertEqual(read.call_count, 2)
        self.assertEqual([call.args[0] for call in read.call_args_list], ["a","b"])

    def test_guest_report_download_is_forwardable(self):
        _, task, _, _ = self.fixture()
        path = self.path / "report.xlsx"
        path.write_bytes(b"report-content")
        with patch.object(server, "REPORT_DIR", self.path):
            status, headers, body = guest.request_handler("GET","/api/reports/report.xlsx",b"","")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"report-content")
        self.assertIn("Content-Disposition", headers)

    def test_drain_marks_queued_messages_deferred(self):
        self.fixture()
        status, _, _ = guest.request_handler("POST","/api/vm/prepare-shutdown",b"","")
        self.assertEqual(status, 200)
        self.assertTrue(isolation.DRAINING.is_set())
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM outbox WHERE status='queued'").fetchone()[0], 0)

    def test_manual_login_pauses_collector_and_new_jobs_until_resumed(self):
        isolation.ENABLED = True
        with patch.object(server, "run_state", return_value={"running":False}):
            self.assertEqual(guest.request_handler("POST","/api/vm/pause",b"","")[0], 200)
            self.assertTrue(isolation.MANUAL.is_set())
            with self.assertRaises(ValueError):
                server.spawn(["--dry-run"],"dry-run","X")
            with self.assertRaises(qq.DesktopDeferred):
                isolation.ensure_qq_foreground(object())
            self.assertEqual(guest.request_handler("POST","/api/vm/resume",b"","")[0], 200)
            self.assertFalse(isolation.MANUAL.is_set())

    def test_manual_login_cannot_cancel_inflight_stop(self):
        isolation.MANUAL.set()
        with patch.object(server, "run_state", return_value={"running":True}):
            self.assertEqual(guest.request_handler("POST","/api/vm/resume",b"","")[0], 409)
        self.assertTrue(isolation.MANUAL.is_set())

    def test_guest_uses_single_desktop_reader(self):
        isolation.ENABLED = True
        with patch.object(server.observer, "start") as observe, \
             patch.object(server, "run_state", return_value={"running":False}):
            server.start_background_services()
        observe.assert_not_called()
