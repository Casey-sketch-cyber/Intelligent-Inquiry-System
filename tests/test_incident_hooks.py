"""以离线替身验证运行问题记录接在真实业务分支上。"""
import threading
import uuid
from unittest.mock import Mock, patch

from inquiry import db, icnet, incidents, isolation, jobs, qq
from inquiry.history import HistoryReadError
from inquiry.identity import SendUncertainError, SupplierIdentityError
from inquiry.runner import Runner, Task
from test_reliability import IsolatedTest
from ui import server


class IncidentHooksTest(IsolatedTest):
    def setUp(self):
        super().setUp()
        recorder = patch.object(incidents, "record")
        self.record = recorder.start()
        self.addCleanup(recorder.stop)

    def test_search_captures_redirect_and_existing_challenge(self):
        page = Mock()
        context = Mock(pages=[page])
        for kind, url in (
            ("captcha", "https://www.ic.net.cn/searchPnCode.php"),
            ("login_required", "https://member.ic.net.cn/login.php"),
        ):
            with self.subTest(kind=kind):
                page.url = "about:blank"
                page.goto.side_effect = lambda *args, **kwargs: setattr(page, "url", url)
                self.record.reset_mock()
                with patch.object(icnet, "search_page", return_value=page), \
                     patch.object(icnet.search_pacing, "wait_for_search"), self.assertRaises(icnet.NotLoggedIn):
                    icnet.search(context, "MODEL")
                self.record.assert_called_once_with(kind, "search", severity="warning")
        page.url = "https://www.ic.net.cn/searchPnCode.php"
        page.goto.reset_mock()
        self.record.reset_mock()
        with self.assertRaises(icnet.NotLoggedIn):
            icnet.search(context, "MODEL")
        page.goto.assert_not_called()
        self.record.assert_called_once_with("captcha", "search", severity="warning")

    def test_queue_records_state_transition_once(self):
        job = jobs.submit({"request_id": uuid.uuid4().hex, "parts": [{"part": "MODEL", "qty": 5000}],
                           "template": "请报价 {part} {qty}"}, {})
        with patch.object(isolation, "MANUAL", threading.Event()), \
             patch.object(isolation, "DRAINING", threading.Event()), \
             patch.object(server, "run_state", return_value={"running": False}), \
             patch.object(server, "browser_status", return_value={"code": "verification"}), \
             patch.object(server, "spawn") as spawn:
            jobs.dispatch_one()
            jobs.dispatch_one()
        self.assertEqual(jobs.get(job["id"])["status"], "waiting_verification")
        self.record.assert_called_once_with("captcha", "queue", severity="warning")
        spawn.assert_not_called()

    def test_qq_launch_records_timeout_and_refusal(self):
        runner = Runner([])
        runner.win = object()
        row = {"company": "测试电子有限公司", "qqs": [{"uin": "12345"}]}
        for kind, severity, error, part in (
            ("qq_launch_failed", "error", None, "MODEL1"),
            ("temp_session_refused", "warning", "对方不支持临时会话", "MODEL2"),
        ):
            with self.subTest(kind=kind):
                task = Task(db.create_task(part, 5000), part, 5000, "请报价")
                self.record.reset_mock()
                with patch.object(isolation, "ENABLED", False), \
                     patch.object(qq, "require_assisted_foreground"), \
                     patch.object(qq, "current_chat_name", return_value=None), \
                     patch.object(icnet, "find_row", return_value=(Mock(), row)), \
                     patch.object(icnet, "launch_qq", return_value=Mock()), \
                     patch.object(icnet, "settle_origin", return_value="qq.com"), \
                     patch.object(icnet, "qq_launch_error", return_value=error), \
                     patch.object(qq, "wait_chat_change", side_effect=lambda *a, **kw: kw["launch_error"]()), \
                     patch.object(qq, "send_text") as send:
                    self.assertFalse(runner.inquire(task, row))
                send.assert_not_called()
                self.record.assert_called_once()
                self.assertEqual(self.record.call_args.args, (kind, "qq_launch"))
                self.assertEqual(self.record.call_args.kwargs["severity"], severity)
                self.assertEqual(self.record.call_args.kwargs["supplier_id"], db.suppliers(task.id)[0]["id"])

    def test_inquiry_records_identity_refusal_and_uncertain_send(self):
        for kind, error, part in (
            ("identity_unverified", SupplierIdentityError("身份不符"), "MODEL1"),
            ("send_uncertain", SendUncertainError("请求发出，但结果未确认"), "MODEL2"),
        ):
            with self.subTest(kind=kind):
                runner = Runner([])
                task = Task(db.create_task(part, 5000), part, 5000, "请报价")
                row = {"company": "测试电子有限公司", "qqs": [{"uin": "12345"}]}
                self.record.reset_mock()
                with patch.object(runner, "open_chat", side_effect=error):
                    self.assertFalse(runner.inquire(task, row))
                self.record.assert_called_once()
                self.assertEqual(self.record.call_args.args, (kind, "inquiry_send"))
                self.assertEqual(self.record.call_args.kwargs["error"], error)

    def test_missing_input_control_is_recorded(self):
        runner = Runner([])
        task = Task(db.create_task("MODEL", 5000), "MODEL", 5000, "请报价")
        row = {"company": "测试电子有限公司", "qqs": [{"uin": "12345"}]}
        with patch.object(runner, "open_chat", return_value="测试会话"), \
             patch.object(runner, "wait_rate_limit", return_value=True), \
             patch.object(isolation, "check_sending"), patch.object(qq, "verify_supplier"), \
             patch.object(qq, "wait_layout_stable"), patch.object(qq, "read_messages", return_value=[]), \
             patch.object(qq, "editor_of", return_value=None):
            self.assertIsNone(runner.inquire(task, row))
            self.assertIsNone(runner.inquire(task, row))
        self.record.assert_called_once_with("uia_control_missing", "qq_controls", severity="warning",
                                           task_id=task.id, supplier_id=db.suppliers(task.id)[0]["id"],
                                           extra={"control": "editor"})

    def test_missing_scroll_control_is_recorded(self):
        with patch.object(qq, "find", return_value=None), self.assertRaises(HistoryReadError):
            qq.scroll_region(object(), "chat-msg-area__vlist", "up")
        self.record.assert_called_once_with("uia_control_missing", "qq_controls", severity="warning",
                                           extra={"control": "message_list"})

    def test_unchanged_read_failure_is_not_recorded_again(self):
        runner, task, sid, _ = self.fixture()
        error = HistoryReadError("锚点丢失")
        with patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), \
             patch.object(qq, "read_inquiry_history", side_effect=error):
            self.assertFalse(runner.read_chat("恒佳达-张小姐", task, sid))
            self.assertFalse(runner.read_chat("恒佳达-张小姐", task, sid))
        self.record.assert_called_once_with("read_incomplete", "collection", severity="warning",
                                           task_id=task.id, supplier_id=sid, error=error)
