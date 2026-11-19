"""续问三家、异步激活等待、远程启动幂等与升级备份。全部阻止真实 QQ 操作。"""
import json
import threading
import time
from unittest.mock import Mock, MagicMock, patch

from test_reliability import IsolatedTest
from inquiry import db, isolation, qq
from inquiry.runner import Runner
from lanhost import live_trial, upgrade_trial
from lanhost.reset_history import task_fingerprint


class ForegroundTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        p = patch.object(isolation, "fit_worker_window")
        p.start(); self.addCleanup(p.stop)  # Monitor fitting is tested separately; hwnd=123 here is synthetic.
        for field, value in (("ENABLED", True), ("MODE", "worker"), ("MANUAL", threading.Event()), ("DRAINING", threading.Event())):
            p = patch.object(isolation, field, value)
            p.start(); self.addCleanup(p.stop)

    def test_async_activation_waits_for_actual_foreground(self):
        win = Mock(NativeWindowHandle=123)
        with patch.object(qq, "exe_of", return_value=r"D:\QQ\QQ.exe"), \
             patch.object(qq.win32process, "GetWindowThreadProcessId", return_value=(1, 2)), \
             patch.object(qq.win32gui, "IsIconic", return_value=False), \
             patch.object(qq.win32gui, "GetForegroundWindow", side_effect=[999, 999, 123]), \
             patch.object(qq.win32gui, "SetForegroundWindow") as activate, \
             patch.object(qq.win32gui, "SendMessageTimeout") as sync:
            self.assertTrue(isolation.ensure_qq_foreground(win))
            activate.assert_called_once_with(123)
            sync.assert_called_once()

    def test_office_never_attempts_activation(self):
        isolation.ENABLED = False
        with patch.object(qq.win32gui, "SetForegroundWindow") as activate:
            self.assertFalse(isolation.ensure_qq_foreground(object()))
            activate.assert_not_called()

    def test_foreground_rejection_stops_without_typing(self):
        win = Mock(NativeWindowHandle=123)
        with patch.object(qq, "exe_of", return_value=r"D:\QQ\QQ.exe"), \
             patch.object(qq.win32process, "GetWindowThreadProcessId", return_value=(1, 2)), \
             patch.object(qq.win32gui, "IsIconic", return_value=False), \
             patch.object(qq.win32gui, "GetForegroundWindow", return_value=999), \
             patch.object(qq.win32gui, "SetForegroundWindow"), \
             patch.object(qq.win32gui, "SendMessageTimeout"), \
             patch.object(isolation, "activate_worker_window", return_value=False), \
             patch.object(isolation.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(qq.DesktopDeferred, "第二台"):
                isolation.ensure_qq_foreground(win)


class TrialV2Tests(IsolatedTest):
    def setUp(self):
        super().setUp()
        import uuid
        self.cfg = {"secret": "a" * 64, "worker_id": "worker-fixture", "allow_send": False}
        self.body = {"kind": "tianxin-live-trial-v1", "worker_id": self.cfg["worker_id"], "request_id": uuid.uuid4().hex,
                     "created_at": 1000, "expires_at": 2000, "parts": [{"part": p, "qty": 5000} for p in live_trial.PARTS],
                     "template": live_trial.TEMPLATE, "max_per_model": 1, "max_total": 3, "task_fingerprint": "c" * 64}

    def budget(self):
        folder = self.path / "attempts"
        folder.mkdir()
        return live_trial.TrialBudget(folder, self.body, time.monotonic() + 60)

    def v2(self):
        return dict(self.body, kind="tianxin-live-trial-v2", max_per_model=3, max_total=9)

    def test_v2_cap_signature_and_duplicate_uin(self):
        body = self.v2()
        signed = body | {"signature": live_trial.sign(body, self.cfg["secret"])}
        self.assertEqual(live_trial.validate_permit(signed, self.cfg, now=1500)["max_total"], 9)
        budget = self.budget()
        budget.permit = body
        sender = Mock()
        for part in live_trial.PARTS:
            for i in range(3):
                budget.begin(part, f"company-{i}", str(i))
                budget.send(sender, object(), live_trial.TEMPLATE.format(part=part, qty=5000), expect_chat="chat", company=f"company-{i}")
            with self.assertRaises(ValueError):
                budget.begin(part, "alias-same-qq", "0")
        self.assertEqual(sender.call_count, 9)
        budget.begin(live_trial.PARTS[-1], "fourth", "4")
        with self.assertRaises(Exception):
            budget.send(sender, object(), live_trial.TEMPLATE.format(part=live_trial.PARTS[-1], qty=5000), expect_chat="chat", company="fourth")
        self.assertEqual(sender.call_count, 9)

    def test_existing_one_each_only_sends_two_more_and_keeps_three_tasks(self):
        root = self.path / "worker"
        (root / "data").mkdir(parents=True)
        for part in live_trial.PARTS:
            tid = db.create_task(part, 5000)
            sid = db.add_supplier(tid, {"company": "prior", "qqs": [{"uin": "old"}]})
            db.update_supplier(sid, sent_at=1, status="sent")
        with db.connect() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM task")]
        body = dict(self.v2(), task_fingerprint=task_fingerprint(rows))
        attempted = []
        def load(runner, context, task):
            task.candidates.append({"company": "alias-of-prior", "qqs": [{"uin": "old"}]})
            task.candidates.extend({"company": f"new-{i}", "qqs": [{"uin": str(i)}]} for i in range(5))
        def inquire(runner, task, row):
            attempted.append((task.part_no, row["company"]))
            sid = db.add_supplier(task.id, row)
            qq.send_text(object(), task.text, expect_chat="chat", company=row["company"])
            db.update_supplier(sid, sent_at=2, status="sent")
            db.add_message(sid, "out", task.text)
            return True
        with patch.object(live_trial, "worker_ui", return_value=(1, 2, Mock(return_value={"manual": True, "manual_ready": True, "running": False}))), \
             patch.object(isolation, "enable_worker"), patch.object(isolation, "ALLOW_SEND", False), patch.object(isolation, "MANUAL", threading.Event()), \
             patch.object(isolation, "ensure_qq_foreground", return_value=True), patch.object(live_trial, "desktop_snapshot", return_value={}), \
             patch.object(qq, "ready_main_window", return_value=object()), patch.object(qq, "send_text", Mock()) as send, \
             patch("playwright.sync_api.sync_playwright", MagicMock()), patch("inquiry.browser.connect", return_value=(None, object())), \
             patch("inquiry.report.build", return_value=root / "report.xlsx"), \
             patch.object(Runner, "load_candidates", load), patch.object(Runner, "inquire", inquire):
            result = live_trial.run_trial(root, self.cfg, body)
            self.assertEqual(send.call_count, 6)
        self.assertEqual(len(attempted), 6)
        self.assertTrue(all(company != "alias-of-prior" for _, company in attempted))
        self.assertEqual(len(result["tasks"]), 3)
        self.assertEqual(result["state"], "finished")
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM task").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM supplier WHERE sent_at IS NOT NULL").fetchone()[0], 9)

    def test_remote_start_claims_before_spawn_and_duplicate_is_not_replayed(self):
        root = self.path / "worker"
        (root / "data/logs").mkdir(parents=True)
        body = self.v2()
        body.update(created_at=time.time(), expires_at=time.time() + 3600)
        value = body | {"signature": live_trial.sign(body, self.cfg["secret"])}
        process = Mock()
        process.poll.return_value = 0
        with patch.object(live_trial, "ACTIVE", {"proc": None, "request_id": None, "starting": False}), \
             patch.object(isolation, "MANUAL", threading.Event()), patch("ui.server.run_state", return_value={"running": False}), \
             patch.object(live_trial, "grant_foreground", return_value=False), \
             patch.object(live_trial.subprocess, "Popen", return_value=process) as spawn:
            isolation.MANUAL.set()
            self.assertTrue(live_trial.start_remote(root, self.cfg, value)["accepted"])
            with self.assertRaises(FileExistsError):
                live_trial.start_remote(root, self.cfg, value)
            spawn.assert_called_once()


class UpgradeTests(IsolatedTest):
    def test_unknown_source_version_refuses_before_backup(self):
        root, package = self.path / "worker", self.path / "package"
        root.mkdir(); package.mkdir()
        manifest = {"files": {}}
        for name in upgrade_trial.TARGETS:
            src, dst = package / "payload" / name, root / name
            src.parent.mkdir(parents=True, exist_ok=True); dst.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("new", encoding="utf-8"); dst.write_text("old", encoding="utf-8")
            manifest["files"][name] = {"old": upgrade_trial.digest(dst), "new": upgrade_trial.digest(src)}
        (root / "inquiry/qq.py").write_text("user edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source changed"):
            upgrade_trial.validate_files(root, package, manifest)
        self.assertFalse((root / "data/backups").exists())


class PollingTests(IsolatedTest):
    def test_unchanged_visible_summary_still_has_periodic_id_check_without_rapid_reopen(self):
        runner, task, sid, _ = self.fixture()
        runner.chat_owner = {"chat": [(task, sid)]}
        now = [10000]
        with patch("inquiry.runner.time.time", side_effect=lambda: now[0]), \
             patch.object(qq, "recent_contacts", return_value=[{"name": "chat", "time": "12:00", "summary": "好", "unread": 0}]), \
             patch.object(runner, "read_chat", return_value=True) as read:
            runner.poll_replies(max_reads=1)
            now[0] += 8
            runner.poll_replies(max_reads=1)
            self.assertEqual(read.call_count, 1)
            now[0] += 181
            runner.poll_replies(max_reads=1)
            self.assertEqual(read.call_count, 2)
            now[0] += 8
            runner.poll_replies(max_reads=1)
            self.assertEqual(read.call_count, 2)


class ReadAuditTests(IsolatedTest):
    def test_other_chat_id_cannot_hide_missing_reply_and_own_inbox_is_counted(self):
        from types import SimpleNamespace
        _, _, sid, _ = self.fixture()
        name = db.supplier(sid)["chat_name"]
        tid = db.create_task("OTHER", 5000)
        other = db.add_supplier(tid, {"company": "other", "qqs": []})
        db.update_supplier(other, chat_name="different-chat")
        db.replace_incoming(other, ["same id elsewhere"], [{"id": "reply-id", "text": "same id elsewhere"}])
        snapshot = SimpleNamespace(messages=[{"id": "reply-id", "self": False, "text": "reply"}])
        live_trial.audit_read_result(self.path, name, sid, set(), snapshot, True)
        lines = (self.path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[-1])["unsaved_visible_ids"], ["reply-id"])
        with db.connect() as c:
            c.execute("INSERT INTO inbox(chat_name,source_id,content,created_at) VALUES(?,?,?,?)", (name, "reply-id", "reply", 1))
        live_trial.audit_read_result(self.path, name, sid, set(), snapshot, True)
        lines = (self.path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[-1])["unsaved_visible_ids"], [])
