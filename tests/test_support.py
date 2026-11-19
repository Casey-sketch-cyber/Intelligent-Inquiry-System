"""Paired diagnostics, application-only updates and prompt controls; no real UI input."""
import base64
import hashlib
import json
import threading
from unittest.mock import Mock, patch

from test_reliability import IsolatedTest
from inquiry import db, isolation, qq, workbench
from lanhost import support, live_trial
from ui import server


class SupportTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.root = self.path / "worker"
        (self.root / "inquiry").mkdir(parents=True)
        for key, value in (("ENABLED", True), ("MODE", "worker"), ("MANUAL", threading.Event())):
            p = patch.object(isolation, key, value); p.start(); self.addCleanup(p.stop)
        isolation.MANUAL.set()
        p = patch.object(live_trial, "active_status", return_value={"trial_running": False})
        p.start(); self.addCleanup(p.stop)
        p = patch.object(server, "run_state", return_value={"running": False})
        p.start(); self.addCleanup(p.stop)

    def request(self, name="inquiry/qq.py", content=b"x = 1\n"):
        return {"worker_id": "fixture", "request_id": "a"*32, "files": {
            name: {"old": None, "new": hashlib.sha256(content).hexdigest(), "content": base64.b64encode(content).decode()}}}

    def test_staging_never_updates_running_source_and_duplicate_rejected(self):
        value = self.request()
        folder = support.stage_update(self.root, {"worker_id": "fixture"}, value)
        self.assertFalse((self.root / "inquiry/qq.py").exists())
        self.assertEqual((folder / "payload/inquiry/qq.py").read_bytes(), b"x = 1\n")
        with self.assertRaisesRegex(ValueError, "pending"): support.stage_update(self.root, {"worker_id": "fixture"}, value)

    def test_rejects_non_source_paths_and_foreign_worker(self):
        for name in (".env", "data/worker.json", "../inquiry/qq.py", "inquiry/../qq.py", "C:/foo.py", "inquiry\\qq.py", "inquiry/test.exe"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                support.stage_update(self.root, {"worker_id": "fixture"}, self.request(name))
        with self.assertRaises(ValueError): support.stage_update(self.root, {"worker_id": "different"}, self.request())

    def test_checks_old_hash_content_and_python_syntax_before_staging(self):
        (self.root / "inquiry/qq.py").write_text("previous", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"): support.stage_update(self.root, {"worker_id": "fixture"}, self.request())
        (self.root / "inquiry/qq.py").unlink()
        bad = self.request(); bad["files"]["inquiry/qq.py"]["new"] = "b"*64
        with self.assertRaises(ValueError): support.stage_update(self.root, {"worker_id": "fixture"}, bad)
        with self.assertRaises(SyntaxError): support.stage_update(self.root, {"worker_id": "fixture"}, self.request(content=b"def : broken"))
        self.assertFalse((self.root / "data/lan/support-jobs").exists())

    def test_busy_worker_and_office_cannot_stage(self):
        isolation.MANUAL.clear()
        with self.assertRaisesRegex(ValueError, "Pause"): support.stage_update(self.root, {"worker_id": "fixture"}, self.request())
        isolation.MANUAL.set(); isolation.MODE = "local"
        with self.assertRaisesRegex(ValueError, "paired"): support.stage_update(self.root, {"worker_id": "fixture"}, self.request())

    def test_protocol_control_rejects_qq_send_button_and_changed_context(self):
        with patch.object(support, "checked_window", return_value={"exe": "qq.exe"}):
            with self.assertRaises(ValueError): support.protocol_action({"hwnd": 123, "path": [1], "name": "发送"})
        with patch.object(support, "checked_window", return_value={"exe": "msedge.exe"}), \
             patch.object(support, "tree", return_value={"controls": [{"path": [1], "name": "打开", "type": "ButtonControl"}]}):
            with self.assertRaisesRegex(ValueError, "origin"): support.protocol_action({"hwnd": 123, "path": [1], "name": "打开"})

    def test_only_observed_qq_origin_button_can_invoke(self):
        root, button = Mock(), Mock()
        root.GetChildren.return_value = [button]
        button.Name = "打开 QQ"; button.ControlTypeName = "ButtonControl"
        control = {"path": [0], "name": "打开 QQ", "type": "ButtonControl"}
        observation = {"controls": [control, {"path": [1], "name": "wpa.qq.com 请求打开 QQ", "type": "TextControl"}]}
        with patch.object(support, "checked_window", return_value={"exe": "msedge.exe"}), \
             patch.object(support, "tree", return_value=observation), \
             patch.object(qq.auto, "ControlFromHandle", return_value=root), \
             patch.object(qq.auto, "UIAutomationInitializerInThread"):
            self.assertEqual(support.protocol_action({"hwnd": 123, "path": [0], "name": "打开 QQ"})["action"], "open_qq")
        button.GetPattern.return_value.Invoke.assert_called_once()

    def test_web_page_cannot_spoof_native_qq_permission(self):
        observation = {"controls": [{"path": [0], "name": "", "type": "DocumentControl"},
                                    {"path": [0, 1], "name": "wpa.qq.com 请求打开 QQ", "type": "TextControl"},
                                    {"path": [0, 2], "name": "打开", "type": "ButtonControl"}]}
        with patch.object(support, "checked_window", return_value={"exe": "msedge.exe"}), \
             patch.object(support, "tree", return_value=observation):
            with self.assertRaisesRegex(ValueError, "origin"):
                support.protocol_action({"hwnd": 123, "path": [0, 2], "name": "打开"})

    def test_pause_interrupts_chat_wait_before_read_or_input(self):
        with patch.object(qq, "current_chat_name") as read:
            with self.assertRaisesRegex(qq.DesktopDeferred, "暂停"):
                qq.wait_chat_change(object(), None, timeout=90, cancelled=lambda: True)
        read.assert_not_called()

    def test_protocol_probe_accepts_only_unsent_authorized_trial_supplier(self):
        task = db.create_task("LPC2387FBD100K",5000)
        sid = db.add_supplier(task,{"company":"fixture","qqs":[{"uin":"1234567890"}]})
        self.assertEqual(support.known_trial_supplier(sid)["id"],sid)
        db.update_supplier(sid,status="send_uncertain",attempted_at=1)
        with self.assertRaises(ValueError): support.known_trial_supplier(sid)
        other = db.create_task("UNAUTHORIZED",5000)
        sid2 = db.add_supplier(other,{"company":"fixture","qqs":[{"uin":"1234567890"}]})
        with self.assertRaises(ValueError): support.known_trial_supplier(sid2)

    def test_current_qq_verified_company_card_and_conflict(self):
        old,new=Mock(),Mock();old.Exists.return_value=False;new.Exists.return_value=True
        win=Mock();win.DocumentControl.side_effect=[old,new]
        label=Mock(Name="@深圳市华美锐科技有限公司",ControlType=qq.auto.ControlType.TextControl,ClassName="")
        verified=Mock(Name="企业认证",ControlType=qq.auto.ControlType.HyperlinkControl,ClassName="company-verify")
        with patch.object(qq.auto,"WalkControl",return_value=[(label,1),(verified,1)]):
            self.assertEqual(qq.profile_company(win),"深圳市华美锐科技有限公司")
        from inquiry.identity import identity_error
        self.assertIsNone(identity_error("深圳市华美锐科技有限公司","华美锐科技","深圳市华美锐科技有限公司"))
        self.assertIsNotNone(identity_error("深圳市其他科技有限公司","华美锐科技","深圳市华美锐科技有限公司"))

    def test_first_launch_permission_is_handled_without_repeated_selection(self):
        controls=[{"type":"TextControl","name":"admin.qidian.qq.com 想要打开 QQ"},
                  {"type":"CheckBoxControl","name":"始终允许 admin.qidian.qq.com","path":[1]},
                  {"type":"ButtonControl","name":"打开","path":[2]}]
        with patch.object(support,"windows",return_value=[{"hwnd":123,"exe":"msedge.exe","iconic":False}]), \
             patch.object(support,"tree",return_value={"controls":controls}),patch.object(support,"protocol_action") as action:
            self.assertTrue(support.confirm_qq_dialog(wait=1))
        self.assertEqual(action.call_count,2)

    def test_verify_sent_supplier_checks_company_and_original_anchor_without_sending(self):
        _, _, sid, _ = self.fixture()
        (self.path / "data").mkdir()
        with patch.object(support, "ROOT", self.path), \
             patch.object(qq, "ready_main_window", return_value=object()), \
             patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), \
             patch.object(qq, "verify_supplier") as verify, \
             patch.object(qq, "verify_bound_history") as history, \
             patch.object(qq, "profile_company", return_value="深圳恒佳达电子有限公司"), \
             patch.object(qq, "send_text") as send:
            result = support.dispatch({}, "POST", "verify-supplier", json.dumps({"supplier_id": sid}), {})
        self.assertTrue(result["verified"])
        self.assertTrue(result["inquiry_anchor_verified"])
        self.assertFalse(result["sending"])
        verify.assert_called_once()
        self.assertEqual(history.call_args.args[3], "anchor")
        send.assert_not_called()

    def test_reply_verification_reads_only_a_sent_reply_from_the_bound_supplier(self):
        _, _, sid, _ = self.fixture()
        from uuid import uuid4
        item = workbench.queue_reply(sid, "请到货通知", str(uuid4()))
        (self.path / "data").mkdir()
        with patch.object(support, "ROOT", self.path), \
             patch.object(qq, "ready_main_window", return_value=object()), \
             patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), \
             patch.object(qq, "verify_supplier"), patch.object(qq, "verify_bound_history"), \
             patch.object(qq, "profile_company", return_value="深圳恒佳达电子有限公司"), \
             patch.object(qq, "read_messages", return_value=[{"id": "qq-reply-1", "self": True, "text": item["content"]}]) as read, \
             patch.object(qq, "send_text") as send:
            body = json.dumps({"supplier_id": sid, "outbox_id": item["id"]})
            with self.assertRaisesRegex(ValueError, "sent reply"):
                support.dispatch({}, "POST", "verify-supplier", body, {})
            read.assert_not_called()
            with db.connect() as conn:
                conn.execute("UPDATE outbox SET status='sent' WHERE id=?", (item["id"],))
            result = support.dispatch({}, "POST", "verify-supplier", body, {})
        self.assertTrue(result["reply_confirmed"])
        self.assertEqual(result["reply_message_ids"], ["qq-reply-1"])
        send.assert_not_called()
