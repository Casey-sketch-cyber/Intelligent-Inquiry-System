"""离线检查工作机批次和回复的自检入口。"""
import threading
import uuid
from unittest.mock import Mock, patch

from inquiry import db, dispatch, icnet, isolation, jobs, qq, selfcheck, site_capture, workbench
from inquiry import runner as runner_module
from test_reliability import IsolatedTest
from ui import server


class WorkerSelfcheckTests(IsolatedTest):
    blocked = {"ok": False, "missing": ["editor"], "text": "QQ 自检缺少输入框"}
    ready = {"ok": True, "missing": [], "text": "QQ 自检通过"}

    def setUp(self):
        super().setUp()
        self.enterContext(patch.multiple(isolation, ENABLED=True, MODE="worker", ALLOW_SEND=True,
                                         MANUAL=threading.Event(), DRAINING=threading.Event()))
        self.enterContext(patch.object(server, "run_state", return_value={"running": False}))
        self.browser = self.enterContext(patch.object(server, "browser_status", return_value={"code": "ok"}))
        self.spawn = self.enterContext(patch.object(server, "spawn"))

    def submit(self, *, dry_run=False):
        return jobs.submit({"request_id": uuid.uuid4().hex, "dry_run": dry_run,
                            "parts": [{"part": "PART01", "qty": 5}], "template": "{part} 数量 {qty}"}, {})

    def task_state(self, job):
        with db.connect() as conn:
            return conn.execute("SELECT status FROM task WHERE id=?", (job["task_ids"][0],)).fetchone()[0]

    def test_dispatch_holds_missing_control_then_requeues_same_batch_when_ready(self):
        job = self.submit()
        with patch.object(selfcheck, "snapshot", side_effect=[self.blocked, self.ready]):
            jobs.dispatch_one()
            self.spawn.assert_not_called()
            self.assertEqual(jobs.get(job["id"])["status"], "waiting_selfcheck")
            self.assertEqual(self.task_state(job), "waiting_selfcheck")
            self.assertIn("输入框", jobs.get(job["id"])["error"])
            jobs.dispatch_one()
        self.assertEqual(self.spawn.call_args.args[0], ["--job", job["id"]])
        self.assertEqual(self.task_state(job), "queued")
        self.assertEqual(jobs.get(job["id"])["task_ids"], job["task_ids"])

    def test_dry_run_dispatches_while_older_real_batch_waits_for_qq(self):
        real, dry = self.submit(), self.submit(dry_run=True)
        with patch.object(selfcheck, "snapshot", return_value=self.blocked) as probe:
            jobs.dispatch_one()
        self.assertEqual(jobs.get(real["id"])["status"], "waiting_selfcheck")
        self.assertEqual(self.spawn.call_args.args[:2], (["--job", dry["id"]], "dry-run"))
        probe.assert_called_once()
        jobs.cancel(real["id"])
        self.assertEqual(self.task_state(real), "stopped")

    def test_worker_rechecks_before_browser_search_and_retains_waiting_tasks(self):
        job = self.submit()
        with patch.object(selfcheck, "require_ready", side_effect=selfcheck.SelfcheckDeferred(self.blocked["text"])), \
             patch.object(runner_module, "sync_playwright") as playwright, patch.object(qq, "send_text") as send:
            jobs.execute(job["id"])
        playwright.assert_not_called()
        send.assert_not_called()
        self.assertEqual(jobs.get(job["id"])["status"], "waiting_selfcheck")
        self.assertEqual(self.task_state(job), "waiting_selfcheck")
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM send_attempt").fetchone()[0], 0)

    def test_pause_during_selfcheck_prevents_worker_from_spawning_the_batch(self):
        job = self.submit()

        def pause_then_finish_probe():
            isolation.MANUAL.set()
            return self.ready

        with patch.object(selfcheck, "snapshot", side_effect=pause_then_finish_probe):
            jobs.dispatch_one()
        self.spawn.assert_not_called()
        self.assertEqual(jobs.get(job["id"])["status"], "queued")

    def test_failed_selfcheck_defers_outbox_before_opening_a_conversation(self):
        _, _, sid, _ = self.fixture()
        item = workbench.queue_reply(sid, "替身回复", str(uuid.uuid4()))
        with patch.object(selfcheck, "require_ready", side_effect=selfcheck.SelfcheckDeferred(self.blocked["text"])), \
             patch.object(qq, "open_recent") as open_recent, patch.object(qq, "send_text") as send:
            self.assertTrue(dispatch.process_one(object(), {}))
        open_recent.assert_not_called()
        send.assert_not_called()
        with db.connect() as conn:
            record = conn.execute("SELECT status,error FROM outbox WHERE id=?", (item["id"],)).fetchone()
            self.assertEqual(tuple(record), ("deferred", self.blocked["text"]))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM send_attempt").fetchone()[0], 0)

    def test_unknown_member_page_stays_waiting_until_redirected_page_is_closed(self):
        job = self.submit(dry_run=True)
        with patch.object(runner_module.Runner, "run", side_effect=icnet.MemberPortalRedirect(icnet.MEMBER_PORTAL_WAIT_TEXT)):
            jobs.execute(job["id"])
        self.assertEqual(self.task_state(job), "waiting_member_login")
        self.browser.return_value = {"code": "ok", "member_portal_open": True}
        with patch.object(selfcheck, "snapshot") as probe:
            jobs.dispatch_one()
            jobs.dispatch_one()
            self.spawn.assert_not_called()
            self.browser.return_value = {"code": "ok"}
            jobs.dispatch_one()
        probe.assert_not_called()
        self.assertEqual(self.spawn.call_args.args[0], ["--job", job["id"]])
        self.assertEqual(self.task_state(job), "queued")

    def test_member_destination_can_pause_search_without_allowing_remote_input(self):
        url = "https://member.ic.net.cn/login.html"
        page = Mock(url="about:blank")
        page.goto.side_effect = lambda *a, **kw: setattr(page, "url", url)
        context = Mock(pages=[page])
        context.new_page.return_value = page
        with patch.object(icnet.search_pacing, "wait_for_search"), patch.object(icnet.incidents, "record"):
            with self.assertRaises(icnet.MemberPortalRedirect):
                icnet.search(context, "PART01")
        self.assertIsNone(site_capture.page_kind(url))
        with patch.object(site_capture, "_page_targets", return_value=[{"id": "TAB", "url": url}]):
            status, target = site_capture.inspect_browser()
        self.assertTrue(status["member_portal_open"])
        self.assertIsNone(target)
