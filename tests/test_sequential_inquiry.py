"""用浏览器和 QQ 替身验证批次顺序、限速和暂停恢复。"""

import threading
import uuid
from contextlib import nullcontext
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import Mock, patch

from inquiry import browser, db, dispatch, icnet, incidents, isolation, jobs, qq, search_pacing
from inquiry import runner as runner_module
from test_reliability import IsolatedTest
from test_search_pacing import FakeClock
from ui import server


class SearchPage:
    def __init__(self, context, url="about:blank"):
        self.context = context
        self.url = url
        self.part = None

    def goto(self, url, **kwargs):
        parsed = urlsplit(url)
        assert parsed.hostname == "www.ic.net.cn" and parsed.path.startswith("/search/")
        self.url = url
        self.part = unquote(parsed.path.removeprefix("/search/").removesuffix(".html"))
        page_no = int(parse_qs(parsed.query).get("page", [1])[0])
        self.context.events.append(("search", self.part, page_no, self.context.clock.now))
        tab_count = sum(page.part is not None for page in self.context.pages)
        self.context.peak_search_tabs = max(self.context.peak_search_tabs, tab_count)
        if self.part == self.context.verification_part:
            self.part = None
            self.url = "https://www.ic.net.cn/searchPnCode.php"

    def wait_for_selector(self, selector, **kwargs):
        assert selector == "#resultList > li"

    def evaluate(self, script):
        if script != icnet.EXTRACT_JS:
            return None
        part_index = int(self.part.removeprefix("MODEL"))
        return [
            {"partNo": self.part, "qty": "10000", "batch": "25+", "date": "01-01", "index": i,
             "company": f"测试 {self.part} 供货商 {i}", "qqs": [{"uin": str(10000 + part_index * 10 + i)}]}
            for i in range(3)
        ]

    def is_closed(self):
        return self not in self.context.pages

    def close(self):
        self.context.pages.remove(self)


class BrowserFixture:
    def __init__(self, clock):
        self.clock = clock
        self.events = []
        self.other_page = SearchPage(self, "https://www.ic.net.cn/")
        self.pages = [self.other_page]
        self.peak_search_tabs = 0
        self.peak_new_tabs = 0
        self.verification_part = None

    def new_page(self):
        page = SearchPage(self)
        self.pages.append(page)
        self.peak_new_tabs = max(self.peak_new_tabs, len(self.pages) - 1)
        return page


class BatchClock(FakeClock):
    def sleep(self, seconds):
        super().sleep(seconds)
        if self.now > 20_000:
            raise AssertionError("批次未在模拟时间内结束")


class SequentialInquiryTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.clock = BatchClock()
        self.context = BrowserFixture(self.clock)
        self.active_chat = None
        self.messages = {}
        self.settings = {
            "inquiry": {"template": "请报 {part} {qty}", "send_card": False},
            "filter": {"max_pages": 1},
            "pacing": {"min_interval": 20, "max_interval": 20, "max_per_hour": 60,
                       "search_min_interval": 30, "search_max_per_10min": 10},
        }
        self.patch_for_test(runner_module, "sync_playwright", return_value=nullcontext(Mock()))
        self.patch_for_test(runner_module.signal, "signal")
        self.patch_for_test(runner_module.report, "build", return_value="测试报告")
        self.patch_for_test(browser, "connect", return_value=(None, self.context))
        for module in (runner_module, dispatch, icnet, search_pacing):
            self.patch_for_test(module, "time", self.clock)
        self.patch_for_test(search_pacing.settings, "load_settings", return_value=self.settings)
        self.patch_for_test(incidents, "_versions", return_value={})
        for name, value in {"ENABLED": False, "ALLOW_SEND": False, "MODE": "local",
                            "MANUAL": threading.Event(), "DRAINING": threading.Event()}.items():
            self.patch_for_test(isolation, name, value)
        self.patch_for_test(qq, "ready_main_window", return_value=object())
        self.patch_for_test(qq, "require_assisted_foreground")
        self.patch_for_test(qq, "wait_layout_stable")
        self.patch_for_test(qq, "verify_supplier")
        self.patch_for_test(qq, "current_chat_name", side_effect=lambda _: self.active_chat)
        self.patch_for_test(qq, "wait_chat_change", side_effect=lambda *args, **kwargs: self.active_chat)
        self.patch_for_test(qq, "read_messages", side_effect=lambda _: self.messages.get(self.active_chat, []))
        self.send_text = self.patch_for_test(qq, "send_text", side_effect=self.send)
        self.patch_for_test(qq, "send_image", side_effect=AssertionError("测试不可发出 QQ 名片"))
        self.patch_for_test(icnet, "load_ok_origins", return_value={"qidian.qq.com"})
        self.patch_for_test(icnet, "launch_qq", side_effect=self.open_chat)

    def patch_for_test(self, obj, name, *args, **kwargs):
        change = patch.object(obj, name, *args, **kwargs)
        self.addCleanup(change.stop)
        return change.start()

    def open_chat(self, page, row, **kwargs):
        # 只模拟协议唤起，其前面的页面及供应商重新定位使用实际代码。
        assert row["partNo"] == page.part
        self.active_chat = row["company"]
        return Mock(url="https://qidian.qq.com/")

    def send(self, window, text, *, expect_chat, company):
        assert expect_chat == company == self.active_chat
        part = text.split()[1]
        self.context.events.append(("send", part, company, self.clock.now))
        self.messages[expect_chat] = [{"self": True, "id": company, "text": text}]

    def submit(self, count, *, max_suppliers=3, dry_run=False):
        parts = [{"part": f"MODEL{i}", "qty": 5000} for i in range(count)]
        return jobs.submit({"request_id": uuid.uuid4().hex, "parts": parts, "max": max_suppliers,
                            "dry_run": dry_run}, self.settings)["id"]

    def searches(self):
        return [event for event in self.context.events if event[0] == "search"]

    def task_states(self, job_id):
        with db.connect() as conn:
            return [r[0] for r in conn.execute("SELECT status FROM task WHERE batch_id=? ORDER BY id", (job_id,))]

    def test_three_models_send_in_order_and_search_once_each(self):
        jid = self.submit(3)
        jobs.execute(jid)
        events = self.context.events
        sends = [event for event in events if event[0] == "send"]
        self.assertEqual(events[0][:2], ("search", "MODEL0"))
        self.assertEqual(events[1][:2], ("send", "MODEL0"))
        self.assertEqual([e[1] for e in sends], [f"MODEL{i}" for i in range(3) for _ in range(3)])
        self.assertEqual(len({e[2] for e in sends}), 9)
        self.assertEqual([(e[1], e[2]) for e in self.searches()], [(f"MODEL{i}", 1) for i in range(3)])
        for left, right in zip(sends, sends[1:]):
            self.assertGreaterEqual(right[3] - left[3], 20)
        # 首次搜索之后第 30 秒有搜索额度，第 3 次发送仍在等待。
        self.assertLess(sends[0][3], self.searches()[1][3])
        self.assertLess(self.searches()[1][3], sends[2][3])
        self.assertEqual(jobs.get(jid)["status"], "done")
        self.assertEqual(self.task_states(jid), ["done"] * 3)

    def test_thirty_models_keep_at_most_two_search_tabs(self):
        jid = self.submit(30, max_suppliers=1)
        jobs.execute(jid)
        self.assertEqual([e[1] for e in self.searches()], [f"MODEL{i}" for i in range(30)])
        self.assertLessEqual(self.context.peak_search_tabs, 2)
        self.assertLessEqual(self.context.peak_new_tabs, 2)
        self.assertEqual(self.context.pages, [self.context.other_page])
        self.assertEqual(jobs.get(jid)["status"], "done")

    def test_paused_dry_run_continues_without_resending_or_researching_first_model(self):
        jid = self.submit(2, dry_run=True)
        pause_at = self.clock.now + 2
        original_sleep = self.clock.sleep

        def pause_during_second_search_wait(seconds):
            original_sleep(seconds)
            if self.clock.now >= pause_at:
                server.STOP_FILE.touch()

        with patch.object(self.clock, "sleep", side_effect=pause_during_second_search_wait):
            jobs.execute(jid)
        self.assertEqual(jobs.get(jid)["status"], "stopped")
        self.assertEqual(self.task_states(jid), ["dry-run", "stopped"])
        self.assertEqual([e[1] for e in self.searches()], ["MODEL0"])
        self.assertEqual(self.context.pages, [self.context.other_page])

        client = server.app.test_client()
        live_id = self.submit(1)
        jobs.cancel(live_id)
        with patch.object(isolation, "ENABLED", True), patch.object(isolation, "MODE", "worker"):
            self.assertEqual(client.post(f"/api/jobs/{live_id}/resume").status_code, 423)
            self.assertEqual(client.post(f"/api/jobs/{jid}/resume").status_code, 200)
        jobs.execute(jid)
        self.assertEqual(self.task_states(jid), ["dry-run", "dry-run"])
        self.assertEqual([e[1] for e in self.searches()], ["MODEL0", "MODEL1"])
        self.assertEqual(jobs.get(jid)["status"], "done")
        self.send_text.assert_not_called()

    def test_captcha_during_prefetch_stops_sends_and_keeps_verification_page(self):
        self.context.verification_part = "MODEL1"
        jid = self.submit(3)
        jobs.execute(jid)
        self.assertEqual(jobs.get(jid)["status"], "waiting_verification")
        self.assertEqual([event[1] for event in self.searches()], ["MODEL0", "MODEL1"])
        sends = [event for event in self.context.events if event[0] == "send"]
        self.assertEqual([event[1] for event in sends], ["MODEL0", "MODEL0"])
        self.assertTrue(all(event[3] < self.searches()[1][3] for event in sends))
        self.assertIn(self.context.other_page, self.context.pages)
        self.assertTrue(any(urlsplit(page.url).path == "/searchPnCode.php" for page in self.context.pages))
