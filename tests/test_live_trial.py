"""实发送测试授权、次数认领与异常恢复；所有 QQ 调用均用替身。"""
import json
import threading
import time
import uuid
from contextlib import ExitStack
from unittest.mock import Mock, MagicMock, patch

from test_reliability import IsolatedTest
from inquiry import db, isolation, qq
from inquiry.identity import SendUncertainError
from inquiry.runner import Runner
from lanhost import live_trial


class LiveTrialTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.cfg = {"secret": "a" * 64, "worker_id": "worker-fixture", "allow_send": False}
        self.body = {"kind": "tianxin-live-trial-v1", "worker_id": self.cfg["worker_id"],
                     "request_id": uuid.uuid4().hex, "created_at": 1000, "expires_at": 2000,
                     "parts": [{"part": p, "qty": 5000} for p in live_trial.PARTS],
                     "template": live_trial.TEMPLATE, "max_per_model": 1, "max_total": 3,
                     "task_fingerprint": "c" * 64}

    def signed(self, **changes):
        body = dict(self.body, **changes)
        return body | {"signature": live_trial.sign(body, self.cfg["secret"])}

    def test_signature_worker_expiry_and_scope_are_enforced(self):
        self.assertEqual(live_trial.validate_permit(self.signed(), self.cfg, now=1500), self.body)
        cases = [self.signed(worker_id="other"), self.signed(max_total=4), self.signed(max_per_model=True),
                 self.signed(parts=[{"part": "OTHER", "qty": 5000}]), self.signed(template="other"),
                 self.signed(created_at=1700), self.signed(expires_at=1400)]
        tampered = self.signed()
        tampered["max_total"] = 30
        cases.append(tampered)
        for value in cases:
            with self.subTest(value=value.get("max_total")), self.assertRaises(ValueError):
                live_trial.validate_permit(value, self.cfg, now=1500)
        with self.assertRaises(ValueError):
            live_trial.validate_permit(self.signed(), self.cfg | {"allow_send": True}, now=1500)

    def test_claim_cannot_be_reused_even_if_no_send_was_confirmed(self):
        root = self.path / "worker"
        root.mkdir()
        folder = live_trial.claim_trial(root, self.body)
        self.assertTrue((folder / "claim.json").is_file())
        with self.assertRaisesRegex(ValueError, "already claimed"):
            live_trial.claim_trial(root, self.body)

    def budget(self):
        folder = self.path / "attempts"
        folder.mkdir()
        return live_trial.TrialBudget(folder, self.body, time.monotonic() + 60)

    def test_wrong_text_company_or_missing_binding_never_reaches_qq(self):
        budget = self.budget()
        part = live_trial.PARTS[0]
        budget.begin(part, "company")
        sender = Mock()
        for text, company, chat in (("wrong", "company", "chat"),
                                    (live_trial.TEMPLATE.format(part=part, qty=5000), "other", "chat"),
                                    (live_trial.TEMPLATE.format(part=part, qty=5000), "company", None)):
            with self.assertRaises(ValueError):
                budget.send(sender, object(), text, expect_chat=chat, company=company)
        sender.assert_not_called()
        self.assertFalse(budget.attempts)

    def test_unknown_send_is_recorded_before_call_and_never_retried(self):
        budget = self.budget()
        part = live_trial.PARTS[0]
        budget.begin(part, "company")
        text = live_trial.TEMPLATE.format(part=part, qty=5000)
        def unknown(*args, **kwargs):
            saved = json.loads((budget.folder / "send-attempts.json").read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["state"], "claimed_result_unknown")
            raise SendUncertainError("connection lost after action")
        sender = Mock(side_effect=unknown)
        for _ in range(2):
            with self.assertRaises(SendUncertainError):
                budget.send(sender, object(), text, expect_chat="chat", company="company")
        sender.assert_called_once()
        with self.assertRaises(ValueError):
            budget.begin(part, "second company")

    def test_exactly_three_permitted_text_calls_and_no_fourth(self):
        budget = self.budget()
        sender = Mock()
        for part in live_trial.PARTS:
            budget.begin(part, "company")
            budget.send(sender, object(), live_trial.TEMPLATE.format(part=part, qty=5000),
                        expect_chat="chat", company="company")
        self.assertEqual(sender.call_count, 3)
        with self.assertRaises(ValueError):
            budget.begin("OTHER", "company")
        with self.assertRaises(SendUncertainError):
            budget.send(sender, object(), "extra", expect_chat="chat", company="company")
        self.assertEqual(sender.call_count, 3)

    def run_offline(self, fail=False):
        root = self.path / "worker"
        (root / "data").mkdir(parents=True)
        attempted = []
        def load(runner, context, task):
            task.candidates.extend([{"company": task.part_no + "-A", "qqs": [{"uin": "123"}]},
                                    {"company": task.part_no + "-B", "qqs": [{"uin": "456"}]}])
        def inquire(runner, task, row):
            attempted.append((task.part_no, row["company"]))
            if fail:
                raise RuntimeError("fixture interrupted")
            if task.part_no == live_trial.PARTS[1]:
                return False  # 首家失败也不换另一家、不自动重试。
            self.assertTrue(isolation.ALLOW_SEND)
            qq.send_text(object(), task.text, expect_chat="chat", company=row["company"])
            return True
        with ExitStack() as stack:
            for target, value in (("lanhost.live_trial.worker_ui", Mock(return_value=(1, 2, Mock(return_value={"manual_ready": True, "running": False})))),
                                  ("inquiry.isolation.enable_worker", Mock(return_value=self.cfg)),
                                  ("inquiry.isolation.ALLOW_SEND", False), ("inquiry.isolation.MANUAL", threading.Event()),
                                  ("inquiry.qq.ready_main_window", Mock(return_value=object())),
                                  ("inquiry.qq.send_text", Mock()), ("inquiry.qq.send_image", Mock()),
                                  ("inquiry.browser.connect", Mock(return_value=(None, object()))),
                                  ("playwright.sync_api.sync_playwright", MagicMock()),
                                  ("inquiry.report.build", Mock(return_value=root / "report.xlsx"))):
                stack.enter_context(patch(target, value))
            stack.enter_context(patch.object(Runner, "load_candidates", load))
            stack.enter_context(patch.object(Runner, "inquire", inquire))
            original_text, original_image = qq.send_text, qq.send_image
            if fail:
                with self.assertRaisesRegex(RuntimeError, "fixture interrupted"):
                    live_trial.run_trial(root, self.cfg, self.body)
                result = json.loads(next((root / "data/lan/live-trials").glob("*/result.json")).read_text(encoding="utf-8"))
            else:
                result = live_trial.run_trial(root, self.cfg, self.body)
                self.assertEqual(len(attempted), 3)
                self.assertEqual(original_text.call_count, 2)
                original_image.assert_not_called()
            self.assertIs(qq.send_text, original_text)
            self.assertIs(qq.send_image, original_image)
            self.assertFalse(isolation.ALLOW_SEND)
            self.assertTrue(isolation.MANUAL.is_set())
            self.assertEqual(len(result["tasks"]), 3)
            self.assertEqual(self.cfg["allow_send"], False)
            return result

    def test_batch_tries_one_supplier_each_without_retry_and_restores_lock(self):
        result = self.run_offline()
        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["send_calls_claimed"], 2)

    def test_exception_records_result_and_restores_readonly(self):
        result = self.run_offline(fail=True)
        self.assertEqual(result["state"], "stopped_with_error")
        self.assertEqual(result["send_calls_claimed"], 0)
