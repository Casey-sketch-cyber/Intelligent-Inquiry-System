"""离线回归：隔离数据库、模拟 QQ/模型；禁止实际鼠标、键盘和模型调用。"""
import hashlib
import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from inquiry import browser, db, llm, qq, report, runtime
from inquiry.history import HistoryReadError, HistorySnapshot, prepend_page, read_history
from inquiry.identity import SendUncertainError, SupplierIdentityError, name_matches
from inquiry.runner import Runner, Task
from ui import server, service


def message(mid, text, mine=False, stamp=""):
    return {"id": mid, "text": text, "self": mine, "time": stamp}


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="inquiry-test-", dir=runtime.TEMP_DIR)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        for target, value in [
            ("inquiry.db.DB_PATH", self.path / "test.db"),
            ("inquiry.runner.STOP_FILE", self.path / "stop.flag"),
            ("inquiry.report.REPORT_DIR", self.path / "reports"),
            ("ui.server.LOCK_FILE", self.path / "runner.lock"),
            ("ui.server.STOP_FILE", self.path / "stop.flag"),
        ]:
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        for target in ("inquiry.qq.auto.SendKeys", "inquiry.qq.auto.Click", "inquiry.qq.auto.MoveTo", "inquiry.llm._client"):
            p = patch(target, side_effect=AssertionError("离线测试禁止真实操作：" + target))
            p.start()
            self.addCleanup(p.stop)
        p = patch("inquiry.runner.log")
        self.log = p.start()
        self.addCleanup(p.stop)
        p = patch("inquiry.analysis.start")
        p.start()
        self.addCleanup(p.stop)
        p = patch("inquiry.observer.start")
        p.start()
        self.addCleanup(p.stop)
        p = patch("ui.server.start_reply_pump")
        p.start()
        self.addCleanup(p.stop)

    def fixture(self):
        tid = db.create_task("STM32F405RGT6", 5000)
        text = "您好，料号：STM32F405RGT6，数量：5k，请报含税单价"
        task = Task(tid, "STM32F405RGT6", 5000, text)
        row = {"company": "深圳恒佳达电子有限公司", "qqs": [{"uin": "12345"}]}
        sid = db.add_supplier(tid, row)
        db.update_supplier(sid, chat_name="恒佳达-张小姐", sent_at=time.time(), status="sent", inquiry_message_id="anchor")
        db.add_message(sid, "out", text)
        runner = Runner([])
        runner.win = object()
        return runner, task, sid, row


class IdentityTests(IsolatedTest):
    def test_nickname_requires_complete_core(self):
        self.assertTrue(name_matches("深圳恒佳达电子有限公司", "恒佳达-张小姐"))
        self.assertTrue(name_matches("深圳市润金微科技有限公司", "Kyrie/润金微科技"))
        self.assertFalse(name_matches("深圳恒佳电子有限公司", "恒佳达-张小姐"))
        self.assertFalse(name_matches("深圳恒佳达电子有限公司", "张小姐"))

    def test_conflicting_profile_overrides_matching_nickname(self):
        self.assertFalse(name_matches("深圳恒佳达电子有限公司", "恒佳达-张小姐", "深圳其他电子有限公司"))
        self.assertTrue(name_matches("深圳恒佳达电子有限公司", "张小姐", "深圳市恒佳达电子有限公司"))

    def test_generic_words_inside_company_core_are_not_removed(self):
        self.assertFalse(name_matches("深圳芯电子城有限公司", "销售", "深圳芯科技城有限公司"))
        self.assertTrue(name_matches("深圳恒佳达电子有限公司", "恒佳达电子-张小姐"))

    def test_unverified_supplier_never_sends(self):
        runner = Runner([])
        runner.win = object()
        runner.card_image = None
        task = Task(db.create_task("X", 100), "X", 100, "询价 X")
        row = {"company": "深圳恒佳达电子有限公司", "qqs": [{"uin": "12345"}]}
        with patch.object(runner, "open_chat", return_value="陌生会话"), \
             patch.object(qq, "wait_layout_stable"), patch.object(qq, "current_chat_name", return_value="陌生会话"), \
             patch.object(qq, "profile_company", return_value="其他公司"), \
             patch.object(qq, "send_text") as text, patch.object(qq, "send_image") as image:
            self.assertFalse(runner.inquire(task, row))
        text.assert_not_called()
        image.assert_not_called()
        record = dict(db.suppliers(task.id)[0])
        self.assertEqual(record["status"], "identity_unverified")
        self.assertIsNone(record["sent_at"])

    def test_profile_change_before_click_is_blocked(self):
        with patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), \
             patch.object(qq, "profile_company", return_value="其他公司"), patch.object(qq, "wait_layout_stable"), \
             patch.object(qq, "human_click") as click:
            with self.assertRaises(SupplierIdentityError):
                qq._click_send(object(), "恒佳达-张小姐", "文字", "深圳恒佳达电子有限公司")
        click.assert_not_called()

    def test_chat_changes_during_verification(self):
        with patch.object(qq, "current_chat_name", side_effect=["恒佳达", "其他会话"]), \
             patch.object(qq, "profile_company", return_value="深圳恒佳达电子有限公司"):
            with self.assertRaises(SupplierIdentityError):
                qq.verify_supplier(object(), "深圳恒佳达电子有限公司", "恒佳达")

    def test_company_card_can_finish_loading_after_chat_title(self):
        with patch.object(qq, "current_chat_name", return_value="示例芯微 张先生"), \
             patch.object(qq, "profile_company", side_effect=[None, None, "示例芯微电子"]) as profile, \
             patch.object(qq.time, "sleep"):
            qq.verify_supplier(object(), "深圳市示例芯微电子有限公司", "示例芯微 张先生")
        self.assertEqual(profile.call_count, 3)

    def test_missing_card_still_fails_and_conflicting_card_is_not_waited_out(self):
        with patch.object(qq, "current_chat_name", return_value="示例芯微 张先生"), \
             patch.object(qq, "profile_company", return_value=None), \
             patch.object(qq, "send_text") as send:
            with self.assertRaises(SupplierIdentityError):
                qq.verify_supplier(object(), "深圳市示例芯微电子有限公司", "示例芯微 张先生", timeout=0)
        send.assert_not_called()
        with patch.object(qq, "current_chat_name", return_value="示例芯微 张先生"), \
             patch.object(qq, "profile_company", side_effect=["其他公司", "示例芯微电子"]) as profile:
            with self.assertRaises(SupplierIdentityError):
                qq.verify_supplier(object(), "深圳市示例芯微电子有限公司", "示例芯微 张先生")
        self.assertEqual(profile.call_count, 1)

    def test_pause_or_chat_change_interrupts_card_loading(self):
        with patch.object(qq, "current_chat_name", return_value="示例芯微 张先生"), \
             patch.object(qq, "profile_company", return_value=None) as profile, \
             patch.object(qq.time, "sleep"):
            with self.assertRaises(qq.DesktopDeferred):
                qq.verify_supplier(object(), "深圳市示例芯微电子有限公司", "示例芯微 张先生",
                                   cancelled=Mock(side_effect=[False, True]))
        self.assertEqual(profile.call_count, 1)
        with patch.object(qq, "current_chat_name", side_effect=["示例芯微 张先生", "示例芯微 张先生", "其他人"]), \
             patch.object(qq, "profile_company", return_value=None) as profile, \
             patch.object(qq.time, "sleep"):
            with self.assertRaises(SupplierIdentityError):
                qq.verify_supplier(object(), "深圳市示例芯微电子有限公司", "示例芯微 张先生")
        self.assertEqual(profile.call_count, 1)

    def test_uncertain_send_is_not_labeled_unsent_or_retried(self):
        runner = Runner([])
        runner.win = object()
        task = Task(db.create_task("X", 10), "X", 10, "询价 X 10")
        row = {"company": "深圳恒佳达电子有限公司", "qqs": [{"uin": "12345"}]}
        with patch.object(runner, "open_chat", return_value="恒佳达"), \
             patch.object(qq, "verify_supplier"), patch.object(qq, "wait_layout_stable"), \
             patch.object(qq, "read_messages", return_value=[]), \
             patch.object(qq, "send_text", side_effect=SendUncertainError("结果不确定")):
            self.assertFalse(runner.inquire(task, row))
        record = dict(db.suppliers(task.id)[0])
        self.assertEqual(record["status"], "send_uncertain")
        self.assertIsNotNone(record["attempted_at"])
        self.assertIn(row["company"], db.recently_asked("X", 24))

    def test_new_send_does_not_reuse_old_same_text_anchor(self):
        runner = Runner([])
        runner.win, runner.card_image = object(), None
        task = Task(db.create_task("X", 10), "X", 10, "询价 X 10")
        row = {"company": "深圳恒佳达电子有限公司", "qqs": [{"uin": "12345"}]}
        old = message("old", task.text, True, "旧时间")
        new = message("new", task.text, True)
        with patch.object(runner, "open_chat", return_value="恒佳达"), \
             patch.object(qq, "verify_supplier"), patch.object(qq, "wait_layout_stable"), \
             patch.object(qq, "current_chat_name", return_value="恒佳达"), \
             patch.object(qq, "parse_qq_time", return_value=time.time() - 48 * 3600), \
             patch.object(qq, "read_messages", side_effect=[[old], [old, new]]), \
             patch.object(qq, "send_text") as send:
            self.assertTrue(runner.inquire(task, row))
        send.assert_called_once()
        self.assertEqual(dict(db.suppliers(task.id)[0])["inquiry_message_id"], "new")


class HistoryTests(IsolatedTest):
    def run_history(self, pages, start=None, anchor="a", cancelled=lambda: False):
        index = len(pages) - 1 if start is None else start
        def read():
            return pages[index]
        def scroll(direction):
            nonlocal index
            index = min(len(pages) - 1, index + 1) if direction == "down" else max(0, index - 1)
        return read_history(read, scroll, cancelled, "询价 X 100", anchor, 1000, lambda t: float(t) if t else None)

    def test_multiple_pages_preserve_repeated_text_and_reach_latest(self):
        a, b, c, d = message("a", "询价 X 100", True), message("b", "稍等"), message("c", "稍等"), message("d", "25元含税")
        result = self.run_history([[a, b], [b, c], [c, d]], start=0)
        self.assertEqual([m["id"] for m in result.messages], ["a", "b", "c", "d"])

    def test_missing_anchor_is_failure_not_old_quote(self):
        with self.assertRaises(HistoryReadError):
            self.run_history([[message("old", "旧报价 1元")]])

    def test_no_overlap_is_failure(self):
        with self.assertRaises(HistoryReadError):
            self.run_history([[message("a", "询价 X 100", True)], [message("r", "25元")]])

    def test_similar_model_is_not_anchor(self):
        with self.assertRaises(HistoryReadError):
            self.run_history([[message("a", "询价 X 1000", True), message("r", "25")]])

    def test_legacy_requires_matching_time(self):
        page = [message("a", "询价 X 100", True, "1000"), message("r", "25")]
        self.assertEqual(self.run_history([page], anchor=None).anchor_id, "a")
        page[0]["time"] = "500"
        with self.assertRaises(HistoryReadError):
            self.run_history([page], anchor=None)

    def test_ambiguous_legacy_inquiry_is_rejected(self):
        with self.assertRaises(HistoryReadError):
            self.run_history([[message("a", "询价 X 100", True, "1000"), message("b", "询价 X 100", True, "1020")]], anchor=None)

    def test_cancelled_history_exits(self):
        with self.assertRaises(HistoryReadError):
            self.run_history([[message("a", "询价 X 100", True)]], cancelled=lambda: True)

    def test_contact_outside_view_is_found_by_scrolling(self):
        pages = [[{"name": "甲"}], [{"name": "乙"}], [{"name": "目标"}]]
        index = 0
        def scroll(win, cls, direction):
            nonlocal index
            index = min(2, index + 1) if direction == "down" else max(0, index - 1)
        with patch.object(qq, "recent_contacts", side_effect=lambda w: pages[index]), \
             patch.object(qq, "_click_recent", side_effect=lambda w, n: any(c["name"] == n for c in pages[index])), \
             patch.object(qq, "scroll_region", side_effect=scroll):
            self.assertTrue(qq.open_recent(object(), "目标"))

    def test_snapshot_failure_preserves_database(self):
        runner, task, sid, _ = self.fixture()
        db.replace_incoming(sid, ["旧的已确认回复"])
        with patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), \
             patch.object(qq, "read_inquiry_history", side_effect=HistoryReadError("锚点丢失")), \
             patch.object(llm, "parse_reply") as parse:
            self.assertFalse(runner.read_chat("恒佳达-张小姐", task, sid))
        parse.assert_not_called()
        self.assertEqual(db.messages(sid, "in"), ["旧的已确认回复"])
        self.assertEqual(db.supplier(sid)["read_error"], "锚点丢失")

    def test_partial_nonempty_snapshot_cannot_erase_previous_replies(self):
        runner, task, sid, _ = self.fixture()
        db.replace_incoming(sid, ["25元", "含税现货"], [message("r1", "25元"), message("r2", "含税现货")])
        snapshot = HistorySnapshot([message("anchor", task.text, True), message("r1", "25元")], "anchor")
        with patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), \
             patch.object(qq, "read_inquiry_history", return_value=snapshot), patch.object(llm, "parse_reply") as parse:
            self.assertFalse(runner.read_chat("恒佳达-张小姐", task, sid))
        self.assertEqual(db.messages(sid, "in"), ["25元", "含税现货"])
        parse.assert_not_called()

    def test_parse_failure_saves_raw_and_retries(self):
        runner, task, sid, _ = self.fixture()
        snapshot = HistorySnapshot([message("anchor", task.text, True), message("r", "25元含税")], "anchor")
        quote = llm.normalize({"status": "quoted", "unit_price": 25, "tax_included": True})
        with patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), \
             patch.object(qq, "read_inquiry_history", return_value=snapshot), \
             patch.object(llm, "parse_reply", side_effect=[RuntimeError("模型离线"), quote]) as parse:
            self.assertFalse(runner.read_chat("恒佳达-张小姐", task, sid))
            self.assertEqual(db.messages(sid, "in"), ["25元含税"])
            self.assertEqual(db.supplier(sid)["status"], "parse_pending")
            self.assertTrue(runner.read_chat("恒佳达-张小姐", task, sid))
            self.assertTrue(runner.read_chat("恒佳达-张小姐", task, sid))
        self.assertEqual(parse.call_count, 2)
        self.assertIsNone(db.supplier(sid)["read_error"])
        self.assertEqual(runner.valid_count(task), 1)

    def test_later_inquiry_stops_attribution(self):
        runner, task, sid, _ = self.fixture()
        other_tid = db.create_task("Y", 5)
        other_sid = db.add_supplier(other_tid, {"company": "另一条记录", "qqs": []})
        db.update_supplier(other_sid, chat_name="恒佳达-张小姐")
        db.add_message(other_sid, "out", "询价 Y 5")
        snapshot = HistorySnapshot([message("anchor", task.text, True), message("r1", "25"),
                                    message("later", "询价 Y 5", True), message("r2", "99")], "anchor")
        with patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), \
             patch.object(qq, "read_inquiry_history", return_value=snapshot), \
             patch.object(llm, "parse_reply", return_value=llm.normalize({"status": "quoted", "unit_price": 25})):
            self.assertTrue(runner.read_chat("恒佳达-张小姐", task, sid))
        self.assertEqual(db.messages(sid, "in"), ["25"])

    def test_poll_does_not_consume_new_signature_during_parse(self):
        runner, task, sid, _ = self.fixture()
        runner.chat_owner = {"恒佳达-张小姐": [(task, sid)]}
        before = {"name": "恒佳达-张小姐", "time": "12:00", "summary": "稍等", "unread": 1}
        with patch.object(qq, "recent_contacts", return_value=[before]) as contacts, \
             patch.object(runner, "read_chat", return_value=True):
            runner.poll_replies()
        self.assertEqual(runner.last_sig[sid], ("12:00", "稍等", 1))
        self.assertEqual(contacts.call_count, 1)

    def test_poll_includes_hidden_contacts_and_failed_reads_retry(self):
        runner, task, sid, _ = self.fixture()
        runner.chat_owner = {"恒佳达-张小姐": [(task, sid)]}
        with patch.object(qq, "recent_contacts", return_value=[]), patch.object(runner, "read_chat", return_value=False) as read:
            runner.poll_replies()
            runner.last_attempt_at.clear()
            runner.poll_replies()
        self.assertEqual(read.call_count, 2)
        self.assertNotIn(sid, runner.last_read_at)

    def test_unchanged_visible_contact_is_not_reopened_every_minute(self):
        runner, task, sid, _ = self.fixture()
        runner.chat_owner = {"恒佳达-张小姐": [(task, sid)]}
        runner.last_sig[sid] = ("12:00", "25", 0)
        runner.last_read_at[sid] = time.time() - 61
        with patch.object(qq, "recent_contacts", return_value=[{"name": "恒佳达-张小姐", "time": "12:00", "summary": "25", "unread": 0}]), \
             patch.object(runner, "read_chat", return_value=True) as read:
            runner.poll_replies()
        read.assert_not_called()


class LifecycleTests(IsolatedTest):
    def test_rate_limit_honors_stop(self):
        runner = Runner([])
        runner.pacing = {"max_per_hour": 1}
        runner.sent_times.append(time.time())
        def stop(seconds):
            runner._stop = True
        with patch.object(runner, "idle", side_effect=stop) as idle:
            self.assertFalse(runner.wait_rate_limit())
        idle.assert_called_once()

    def test_collect_stop_flag_is_observed(self):
        runner, task, sid, _ = self.fixture()
        runner.collect = True
        (self.path / "stop.flag").write_text("stop", encoding="utf-8")
        with patch.object(runner, "read_chat") as read:
            self.assertFalse(runner.sweep(task))
        read.assert_not_called()

    def test_incomplete_sweep_is_not_marked_done(self):
        runner, task, sid, _ = self.fixture()
        task.started_at = time.time() - 10000
        with patch.object(runner, "sweep", return_value=False):
            self.assertTrue(runner.check_done(task))
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM task WHERE id=?", (task.id,)).fetchone()[0], "needs_review")

    def test_price_does_not_invent_tax_status(self):
        self.assertIsNone(report.tax_price({"unit_price": 25, "currency": "CNY", "tax_included": None}))
        self.assertEqual(report.tax_price({"unit_price": 25, "currency": "CNY", "tax_included": False, "tax_rate": 9}), 27.25)
        self.assertEqual(report.tax_price({"unit_price": 25, "currency": "CNY", "tax_included": False}), 28.25)
        self.assertIsNone(report.tax_price({"unit_price": 25, "currency": "USD", "tax_included": True}))

    def test_invalid_prices_are_not_quotes(self):
        for price in (0, -1, "-5", float("inf"), float("nan")):
            self.assertEqual(llm.normalize({"status": "quoted", "unit_price": price})["status"], "pending")

    def test_schema_migration_preserves_old_data(self):
        import sqlite3
        with sqlite3.connect(db.DB_PATH) as c:
            c.executescript(db.SCHEMA)
            c.execute("INSERT INTO task(part_no,qty,created_at) VALUES ('OLD',1,100)")
        c.close()
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT part_no FROM task").fetchone()[0], "OLD")
            self.assertIn("inquiry_message_id", {r["name"] for r in c.execute("PRAGMA table_info(supplier)")})


class ServiceTests(IsolatedTest):
    def test_complete_startup_reuses_only_verified_service_and_cleans_state(self):
        state, lock = self.path / "service.json", self.path / "service.lock"
        real_bind = service.bind_server
        seen = []
        def bind(app, preferred, fallback=True):
            httpd = real_bind(app, preferred, fallback)
            original_serve = httpd.serve_forever
            def serve():
                thread = threading.Thread(target=original_serve, daemon=True)
                thread.start()
                try:
                    self.assertTrue(service.probe(httpd.port, server.INSTANCE_ID))
                    self.assertEqual(service.existing_url(), f"http://127.0.0.1:{httpd.port}/")
                    seen.append(httpd.port)
                    server.main()  # 第二次启动应识别已有实例，不能再绑定。
                finally:
                    httpd.shutdown()
                    thread.join(timeout=2)
            httpd.serve_forever = serve
            return httpd
        with patch.object(service, "STATE_FILE", state), patch.object(service, "LOCK_FILE", lock), \
             patch.object(service, "bind_server", side_effect=bind) as binder, \
             patch("sys.argv", ["ui/server.py", "--no-browser"]), \
             patch("ctypes.windll.kernel32.SetConsoleTitleW"), patch.object(server, "open_window") as window, \
             contextlib.redirect_stdout(io.StringIO()):
            server.main()
        binder.assert_called_once()
        window.assert_not_called()
        self.assertEqual(len(seen), 1)
        self.assertFalse(state.exists())

    def test_explicit_occupied_port_fails_without_fallback(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with self.assertRaises(RuntimeError):
                service.bind_server(server.app, occupied.getsockname()[1], fallback=False)

    def test_occupied_port_falls_back_and_identifies_this_workspace(self):
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        self.addCleanup(occupied.close)
        port = occupied.getsockname()[1]
        httpd = service.bind_server(server.app, port)
        self.addCleanup(httpd.server_close)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.assertNotEqual(httpd.port, port)
        self.assertTrue(service.probe(httpd.port, server.INSTANCE_ID))
        self.assertFalse(service.probe(httpd.port, "wrong-instance"))
        state = self.path / "service.json"
        service.write_state(httpd.port, server.INSTANCE_ID, state)
        self.assertEqual(service.existing_url(state), f"http://127.0.0.1:{httpd.port}/")

    def test_unrelated_service_is_not_reused(self):
        from flask import Flask, jsonify
        unrelated = Flask("unrelated")
        unrelated.add_url_rule("/api/health", view_func=lambda: jsonify(app_id="other", workspace_id=service.WORKSPACE_ID))
        httpd = service.bind_server(unrelated, 0)
        self.addCleanup(httpd.server_close)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.assertFalse(service.probe(httpd.port))

    def test_same_program_other_workspace_is_not_reused(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"app_id": service.APP_ID, "workspace_id": "different"}).encode()
        with patch("ui.service.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertFalse(service.probe(8765))

    def test_lock_prevents_two_startups(self):
        path = self.path / "ui.lock"
        first = service.acquire_lock(path)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(service.acquire_lock(path))
        finally:
            first.close()
        second = service.acquire_lock(path)
        self.assertIsNotNone(second)
        second.close()

    def test_api_exposes_read_failure_and_identity_state(self):
        _, task, sid, _ = self.fixture()
        db.update_supplier(sid, status="identity_unverified", read_error="消息未完整读取")
        response = server.app.test_client().get(f"/api/tasks/{task.id}")
        self.assertEqual(response.status_code, 200)
        item = response.get_json()["suppliers"][0]
        self.assertIn("未发送", item["status_cn"])
        self.assertEqual(item["read_error"], "消息未完整读取")

    def test_zero_rate_limit_setting_rejected_without_write(self):
        with patch.object(server, "save_settings") as save:
            response = server.app.test_client().put("/api/settings", json={"pacing": {"max_per_hour": 0}})
        self.assertEqual(response.status_code, 400)
        save.assert_not_called()

    def test_all_runtime_storage_is_on_d(self):
        for key in ("TEMP", "TMP", "TMPDIR", "PIP_CACHE_DIR", "PYTHONPYCACHEPREFIX", "PLAYWRIGHT_BROWSERS_PATH", "XDG_CACHE_HOME"):
            self.assertEqual(Path(os.environ[key]).resolve().drive.upper(), "D:")
        self.assertEqual(Path(tempfile.gettempdir()).drive.upper(), "D:")
        with self.assertRaises(RuntimeError):
            runtime.require_d_path(Path("C:/not-allowed/output"))

    def test_browser_profile_and_downloads_are_on_d(self):
        profile = self.path / "edge"
        args = browser.edge_args(profile, "test-edge")
        self.assertIn(f"--user-data-dir={profile}", args)
        prefs = json.loads((profile / "Default/Preferences").read_text(encoding="utf-8"))
        self.assertEqual(Path(prefs["download"]["default_directory"]).drive.upper(), "D:")


if __name__ == "__main__":
    unittest.main()
