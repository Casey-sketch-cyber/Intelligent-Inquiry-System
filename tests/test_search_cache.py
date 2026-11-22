"""在隔离库和浏览器替身中验证缓存、刷新和供应商实时核对。"""
import json
import uuid
from contextlib import nullcontext
from urllib.parse import unquote, urlsplit
from unittest.mock import Mock, patch

from inquiry import db, dispatch, icnet, isolation, jobs, qq, search_pacing
from inquiry import runner as runner_module
from inquiry.runner import Runner, Task
from test_reliability import IsolatedTest
from test_search_pacing import FakeClock
from test_sequential_inquiry import BrowserFixture, SearchPage


class SearchCacheTests(IsolatedTest):
    LINK = "https://wpa.qq.com/msgrd?uin=12345"

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        for module in (runner_module, dispatch, icnet, search_pacing):
            change = patch.object(module, "time", self.clock)
            change.start()
            self.addCleanup(change.stop)
        policy = patch.object(search_pacing.settings, "load_settings", return_value={})
        policy.start()
        self.addCleanup(policy.stop)
        self.page = Mock(url="about:blank")
        self.context = Mock(pages=[self.page])
        self.context.new_page.return_value = self.page
        self.page.goto.side_effect = lambda url, **_: setattr(self.page, "url", url)
        self.page.evaluate.side_effect = self.evaluate

    def evaluate(self, script):
        if script != icnet.EXTRACT_JS:
            return 12.5
        part = unquote(urlsplit(self.page.url).path.removeprefix("/search/").removesuffix(".html"))
        return [{"company": "测试供应商", "partNo": part, "qty": "10K", "qqs": [{"uin": "12345", "inthref": self.LINK}]}]

    def load(self, part="PART-A", *, refresh=False, hours=4):
        runner = Runner([], dry_run=True, refresh_search=refresh)
        runner.filter = {"search_cache_hours": hours}
        task = Task(db.create_task(part, 5000), part, 5000, "试验")
        self.assertTrue(runner.load_candidates(self.context, task))
        return task

    def metadata(self, task):
        with db.connect() as conn:
            return tuple(conn.execute("SELECT search_cache_at, search_cache_pages, ref_price FROM task WHERE id=?",
                                      (task.id,)).fetchone())

    def test_schema_six_upgrade_preserves_tasks_and_initializes_cache_metadata(self):
        tid = db.create_task("PREVIOUS", 23)
        with db.connect() as conn:
            conn.execute("DROP TABLE search_cache")
            conn.execute("ALTER TABLE task DROP COLUMN search_cache_at")
            conn.execute("ALTER TABLE task DROP COLUMN search_cache_pages")
            conn.execute("PRAGMA user_version=6")
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(tuple(conn.execute(
                "SELECT part_no, qty, search_cache_at, search_cache_pages FROM task WHERE id=?", (tid,)
            ).fetchone()), ("PREVIOUS", 23, None, 0))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0], 0)

    def test_warm_cache_avoids_navigation_and_expiry_reloads(self):
        fresh = self.load()
        fetched = self.clock.now
        cached = self.load("part-a")
        self.assertEqual(self.page.goto.call_count, 1)
        self.assertEqual(self.metadata(fresh), (None, 0, 12.5))
        self.assertEqual(self.metadata(cached), (fetched, 1, 12.5))
        self.assertNotIn("inthref", cached.candidates[0]["qqs"][0])
        self.clock.now += 4 * 3600
        expired = self.load()
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertEqual(self.metadata(expired), (None, 0, 12.5))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_attempt").fetchone()[0], 2)
            self.assertNotIn(self.LINK, conn.execute("SELECT rows_json FROM search_cache").fetchone()[0])

    def test_refresh_replaces_snapshot_and_zero_disables_reads_and_writes(self):
        self.load()
        first_at = self.clock.now
        self.load(hours=0)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT fetched_at FROM search_cache").fetchone()[0], first_at)
        self.assertEqual(self.metadata(self.load(refresh=True))[1], 0)
        second_at = self.clock.now
        self.assertGreater(second_at, first_at)
        self.assertEqual(self.metadata(self.load())[:2], (second_at, 1))
        self.assertEqual(self.page.goto.call_count, 3)

    def test_model_variants_and_page_numbers_do_not_share_cache(self):
        for part in ("PART", "PARTA", "PART-A", "PART/A", "PART-A-T"):
            with self.subTest(part=part):
                self.assertEqual(self.metadata(self.load(part))[1], 0)
                self.assertEqual(self.metadata(self.load(part.lower()))[1], 1)
        requested = [item.args[0] for item in self.page.goto.call_args_list]
        self.assertEqual(len(requested), 5)
        self.assertTrue(any("PART%2FA" in url for url in requested))

        runner = Runner([], dry_run=True)
        runner.filter = {"search_cache_hours": 4}
        task = Task(db.create_task("PART", 5000), "PART", 5000, "试验", pages_loaded=1)
        self.assertTrue(runner.load_candidates(self.context, task))
        self.assertEqual(len(self.page.goto.call_args_list), 6)
        self.assertTrue(self.page.url.endswith("?page=2"))

    def test_cache_hits_pause_for_manual_captcha_or_login(self):
        self.load()
        runner = Runner([], dry_run=True)
        runner.filter = {"search_cache_hours": 4}
        task = Task(db.create_task("PART-A", 5000), "PART-A", 5000, "试验")
        for url in ("https://www.ic.net.cn/searchPnCode.php", "https://member.ic.net.cn/login"):
            with self.subTest(url=url), patch.object(icnet.incidents, "record"):
                self.context.pages = [self.page, Mock(url=url)]
                with self.assertRaises(icnet.NotLoggedIn):
                    runner.load_candidates(self.context, task)
        self.page.goto.assert_called_once()
        self.assertEqual(task.pages_loaded, 0)

    def test_legacy_jobs_remain_idempotent_and_refresh_is_preserved(self):
        body = {"request_id": uuid.uuid4().hex, "parts": [{"part": "PART-A", "qty": 5000}], "dry_run": True}
        config = {"inquiry": {"template": "请报 {part}，数量 {qty}"}}
        stored = jobs.submit(body, config)
        with db.connect() as conn:
            payload = json.loads(conn.execute("SELECT payload FROM inquiry_job WHERE id=?", (stored["id"],)).fetchone()[0])
            payload["request"].pop("refresh_search")
            conn.execute("UPDATE inquiry_job SET payload=? WHERE id=?", (json.dumps(payload), stored["id"]))
        self.assertEqual(jobs.submit(body | {"refresh_search": False}, config)["task_ids"], stored["task_ids"])
        with self.assertRaises(ValueError):
            jobs.submit(body | {"refresh_search": True}, config)
        with self.assertRaises(ValueError):
            jobs.submit(body | {"refresh_search": "false"}, config)
        fresh = jobs.submit(body | {"request_id": uuid.uuid4().hex, "refresh_search": True}, config)
        with patch.object(runner_module.Runner, "run", autospec=True) as invoke:
            jobs.execute(fresh["id"])
        self.assertIs(invoke.call_args.args[0].refresh_search, True)

    def test_lookup_reverifies_cached_supplier_on_a_live_document(self):
        self.load()
        cached = self.load()
        row = dict(cached.candidates[0], index=99)
        context = BrowserFixture(self.clock)
        tabs = icnet.SearchTabs(context)
        with patch.object(SearchPage, "evaluate", return_value=[{**row, "index": 2}]):
            page, located = icnet.find_row(tabs, "PART-A", row)
        self.assertEqual(located["index"], 2)
        self.assertEqual(context.events[0][:3], ("search", "PART-A", 1))
        self.assertIsNotNone(page)

    def test_lookup_prefers_original_retained_page_after_paging(self):
        context = BrowserFixture(self.clock)
        tabs = icnet.SearchTabs(context)
        row = {"company": "示例供应商", "partNo": "MODEL0", "qty": "10K", "qqs": [{"uin": "12345"}]}

        def evaluate(page, script):
            if script == icnet.EXTRACT_JS:
                return [{**row, "index": 0}] if not urlsplit(page.url).query else []

        with patch.object(SearchPage, "evaluate", autospec=True, side_effect=evaluate):
            original, rows = icnet.search(tabs, "MODEL0", 1)
            icnet.search(tabs, "MODEL0", 2)
            self.assertIs(icnet.find_row(tabs, "MODEL0", rows[0])[0], original)
            self.assertEqual(len(context.events), 2)
            icnet.search(tabs, "MODEL1", 1)
            self.assertIs(icnet.find_row(tabs, "MODEL0", rows[0])[0], original)
            self.assertEqual(len(context.events), 3)
        self.assertEqual(context.peak_new_tabs, 2)
        tabs.close_all()
        self.assertEqual(context.pages, [context.other_page])

    def test_legacy_duplicate_tasks_skip_same_model_prefetch(self):
        runner = Runner([])
        runner.tasks = [Task(1, "PART-A", 100, "一"), Task(2, "part-a", 200, "二")]

        def finish(_, task):
            self.assertIsNone(runner.next_search)
            task.done = True

        with patch.object(runner, "check_done", side_effect=lambda task: task.done), \
             patch.object(runner, "load_candidates", side_effect=finish) as load:
            runner.send_in_order(Mock())
        self.assertEqual(load.call_count, 2)

    def run_batch(self, part, *, hours=4, max_pages=3, dry_run=False):
        context = BrowserFixture(self.clock)
        active_chat = None
        history = []

        def launch(page, row, **_):
            nonlocal active_chat
            self.assertEqual(page.part, part)
            active_chat = row["company"]
            return Mock(url="https://qidian.qq.com/")

        def send(_window, text, *, expect_chat, company):
            self.assertEqual(expect_chat, active_chat)
            history.append({"id": "simulated-message", "text": text, "self": True})
            context.events.append(("send", part, company, self.clock.now))

        config = {"inquiry": {"template": "请报 {part}，数量 {qty}", "send_card": False},
                  "filter": {"max_pages": max_pages, "search_cache_hours": hours}}
        request = {"request_id": uuid.uuid4().hex, "parts": [{"part": part, "qty": 5000}], "max": 1,
                   "dry_run": dry_run}
        job = jobs.submit(request, config)
        with patch.object(isolation, "ENABLED", False), \
             patch.object(runner_module, "sync_playwright", return_value=nullcontext(Mock())), \
             patch.object(runner_module.browser_mod, "connect", return_value=(None, context)), \
             patch.object(runner_module.report, "build", return_value="测试报告"), \
             patch.object(Runner, "wait_for_qq", return_value=True), \
             patch.object(qq, "require_assisted_foreground"), \
             patch.object(qq, "verify_supplier"), \
             patch.object(qq, "wait_layout_stable"), \
             patch.object(qq, "current_chat_name", side_effect=lambda _: active_chat), \
             patch.object(qq, "wait_chat_change", side_effect=lambda *args, **kw: active_chat), \
             patch.object(qq, "read_messages", side_effect=lambda _: list(history)), \
             patch.object(qq, "send_text", side_effect=send), \
             patch.object(icnet, "load_ok_origins", return_value={"qidian.qq.com"}), \
             patch.object(icnet, "launch_qq", side_effect=launch):
            jobs.execute(job["id"])

        self.assertEqual(jobs.get(job["id"])["status"], "done")
        task = Task(job["task_ids"][0], part, 5000, "")
        return [event[:3] for event in context.events], self.metadata(task), db.suppliers(task.id)

    def test_normal_batch_searches_live_with_positive_cache_as_with_disabled_cache(self):
        for part, hours in (("MODEL0", 4), ("MODEL1", 0)):
            with self.subTest(hours=hours):
                self.save_positive_cache(part)
                for page in (2, 3):
                    db.save_search_cache(part, page, [], None, self.clock.now)
                current = f"测试 {part} 供货商 0"
                events, metadata, suppliers = self.run_batch(part, hours=hours)
                self.assertEqual(events, [("search", part, 1), ("send", part, current)])
                self.assertEqual(metadata[:2], (None, 0))
                self.assertEqual([(entry["company"], entry["status"]) for entry in suppliers], [(current, "sent")])

    def save_positive_cache(self, part):
        row = {"company": "缓存中的过时供应商", "partNo": part, "page": 1, "qty": "10K", "batch": "25+",
               "date": "01-01", "qtyNum": 10_000, "qqs": [{"uin": "99999"}]}
        db.save_search_cache(part, 1, [row], 12.5, self.clock.now)

    def test_dry_run_batch_uses_positive_cache_without_navigation(self):
        part = "MODEL0"
        self.save_positive_cache(part)

        events, metadata, suppliers = self.run_batch(part, dry_run=True)

        self.assertEqual(events, [])
        self.assertEqual(metadata, (self.clock.now, 1, 12.5))
        self.assertEqual(suppliers, [])

    def test_normal_batch_skips_search_with_all_candidate_pages_known_empty(self):
        part = "MODEL0"
        oldest = self.clock.now - 60
        db.save_search_cache(part, 1, [], 12.5, oldest)
        db.save_search_cache(part, 2, [{"company": "无 QQ 的供应商", "partNo": part, "qqs": []}],
                             None, self.clock.now)

        events, metadata, suppliers = self.run_batch(part, max_pages=2)

        self.assertEqual(events, [])
        self.assertEqual(metadata, (oldest, 2, 12.5))
        self.assertEqual(suppliers, [])

    def test_normal_batch_searches_live_if_any_candidate_page_missing_or_expired(self):
        for part, expired_page in (("MODEL0", False), ("MODEL1", True)):
            with self.subTest(expired_page=expired_page):
                db.save_search_cache(part, 1, [], 12.5, self.clock.now)
                if expired_page:
                    db.save_search_cache(part, 2, [], None, self.clock.now - 4 * 3600)

                events, metadata, suppliers = self.run_batch(part, max_pages=2)

                current = f"测试 {part} 供货商 0"
                self.assertEqual(events, [("search", part, 1), ("send", part, current)])
                self.assertEqual(metadata[:2], (None, 0))
                self.assertEqual([entry["company"] for entry in suppliers], [current])
