"""离线验证搜索导航、SQLite 滚动额度和取消行为。"""

import importlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from unittest.mock import Mock, patch

from inquiry import db, icnet, qq, search_pacing
from inquiry.runner import Runner, Task
from test_reliability import IsolatedTest


class FakeClock:
    def __init__(self, now=10_000):
        self.now = now

    def time(self):
        return self.now

    monotonic = time

    def sleep(self, seconds):
        self.now += seconds


class SearchPacingTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.policy = {"search_min_interval": 30, "search_max_per_10min": 10}
        for target, replacement in (("inquiry.search_pacing.time", self.clock), ("inquiry.icnet.time", self.clock)):
            p = patch(target, replacement)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(search_pacing.settings, "load_settings", return_value={"pacing": self.policy})
        p.start()
        self.addCleanup(p.stop)
        self.page = Mock(url="about:blank")
        self.context = Mock(pages=[self.page])
        self.context.new_page.return_value = self.page
        self.visits = []

        def goto(url, **_):
            # 发起浏览器导航前，另一条 SQLite 连接已经能读到记录。
            entries = self.attempts()
            self.assertEqual(len(entries), len(self.visits) + 1)
            self.assertEqual(entries[-1]["created_at"], self.clock.now)
            self.visits.append((self.clock.now, url))
            self.page.url = url

        self.page.goto.side_effect = goto
        self.page.evaluate.return_value = []

    def attempts(self):
        with db.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM search_attempt ORDER BY id")]

    def test_all_models_share_interval_and_rolling_window(self):
        for i in range(12):
            icnet.search(self.context, f"EXAMPLE-{i}")
        times = [at for at, _ in self.visits]
        self.assertEqual([at - times[0] for at in times], [*range(0, 300, 30), 600, 630])
        self.assertTrue(all(left + 30 <= right for left, right in zip(times, times[1:])))
        for right in times:
            self.assertLessEqual(sum(right - 600 < at <= right for at in times), 10)

    def test_find_row_pages_share_same_limit_and_existing_hit_does_not_navigate(self):
        row = {"company": "示例供货商", "qqs": [{"uin": "12345"}], "page": 2}
        icnet.search(self.context, "EXAMPLE")
        self.page.evaluate.side_effect = lambda _: [{**row, "index": 5}] if urlsplit(self.page.url).query == "page=3" else []
        _, match = icnet.find_row(self.context, "EXAMPLE", row)
        self.assertEqual(match["page"], 3)
        self.assertEqual([item["page_no"] for item in self.attempts()], [1, 2, 1, 3])
        self.assertEqual([item["source"] for item in self.attempts()], ["candidates", *(["supplier_lookup"] * 3)])
        self.assertEqual([at for at, _ in self.visits], [10_000, 10_030, 10_060, 10_090])
        self.assertIsNotNone(icnet.find_row(self.context, "EXAMPLE", row))
        self.assertEqual(len(self.visits), 4)

    def test_upgrades_existing_database_and_keeps_quota_when_limiter_restarts(self):
        task_id = db.create_task("EXISTING", 10)
        with db.connect() as conn:
            conn.execute("DROP TABLE search_attempt")
            conn.execute("PRAGMA user_version=3")
        self.policy["search_max_per_10min"] = 1
        icnet.search(self.context, "EXAMPLE")
        # Reload 模块模拟进程启动后全新的限速器，额度从同一个 SQLite 文件读取。
        importlib.reload(search_pacing)
        with patch.object(search_pacing, "time", self.clock):
            icnet.search(self.context, "AFTER-RESTART")
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(conn.execute("SELECT part_no FROM task WHERE id=?", (task_id,)).fetchone()[0], "EXISTING")
        self.assertEqual([at for at, _ in self.visits], [10_000, 10_600])

    def test_concurrent_process_connections_cannot_claim_same_slot(self):
        self.attempts()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda part: search_pacing._reserve(part, 1, "candidates", 30, 10), ["A", "B"]))
        self.assertEqual(sorted(outcomes), [0, 30])
        self.assertEqual(len(self.attempts()), 1)

    def test_pause_or_captcha_while_waiting_blocks_navigation(self):
        icnet.search(self.context, "EXAMPLE")
        deadline = self.clock.now + 1
        runner = Runner([])
        task = Task(1, "NEXT", 5, "询价")
        with patch.object(runner, "stop_requested", side_effect=lambda: self.clock.now >= deadline):
            self.assertFalse(runner.load_candidates(self.context, task))
        self.assertEqual(task.pages_loaded, 0)

        def change_page(seconds):
            self.clock.sleep(seconds)
            self.context.pages = [self.page, Mock(url="https://www.ic.net.cn/searchPnCode.php?l=ins")]

        with patch.object(search_pacing, "time", Mock(time=self.clock.time, monotonic=self.clock.monotonic, sleep=change_page)):
            with self.assertRaises(icnet.NotLoggedIn):
                icnet.search(self.context, "ANOTHER")
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(len(self.visits), 1)

    def test_failed_navigation_still_counts_and_diagnostic_does_not_wait(self):
        self.page.goto.side_effect = RuntimeError("导航超时")
        with self.assertRaisesRegex(RuntimeError, "导航超时"):
            icnet.search(self.context, "EXAMPLE")
        with self.assertRaises(search_pacing.SearchRateLimited):
            icnet.search(self.context, "DIAGNOSTIC", source="diagnostic", wait=False)
        self.assertEqual(self.clock.now, 10_000)
        self.assertEqual(self.page.goto.call_count, 1)
        self.assertEqual(len(self.attempts()), 1)

    def test_pause_or_verification_during_supplier_lookup_keeps_candidate_retryable(self):
        runner = Runner([])
        row = {"company": "示例供货商", "qqs": [{"uin": "12345"}], "partNo": "EXAMPLE", "qty": "10"}
        self.page.evaluate.side_effect = lambda script: [row] if script == icnet.EXTRACT_JS else None
        for error in (icnet.SearchCancelled("已停止"), icnet.NotLoggedIn("等待人工校验")):
            with self.subTest(error=type(error).__name__):
                task = Task(db.create_task("EXAMPLE", 5), "EXAMPLE", 5, "询价")
                with patch.object(runner, "wait_rate_limit", return_value=True), \
                     patch.object(runner, "open_chat", side_effect=error), \
                     patch.object(qq, "send_text") as send:
                    if isinstance(error, icnet.SearchCancelled):
                        self.assertFalse(runner.inquire(task, row))
                    else:
                        with self.assertRaises(icnet.NotLoggedIn):
                            runner.inquire(task, row)
                send.assert_not_called()
                self.assertEqual(db.suppliers(task.id)[0]["status"], "waiting_search")
                self.assertTrue(runner.load_candidates(self.context, task))
                self.assertEqual(task.candidates[0]["company"], row["company"])
