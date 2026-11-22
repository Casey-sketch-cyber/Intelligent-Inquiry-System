"""型号聚合、异步解析、非抢占操作与回复发件箱的离线验收。"""
import json
import time
import uuid
from unittest.mock import Mock, patch

from test_reliability import IsolatedTest, message
from inquiry import analysis, db, dispatch, jobs, llm, qq, workbench
from inquiry.history import HistorySnapshot
from inquiry.identity import SendUncertainError, SupplierIdentityError
from inquiry.runner import Runner, Task, render_message
from ui import server


class BatchTests(IsolatedTest):
    def test_paused_wave_keeps_remaining_count(self):
        runner = Runner([])
        task = Task(db.create_task("X", 1), "X", 1, "询价 X 1")
        task.candidates.extend([{"company": "A"}, {"company": "B"}])
        runner.cfg["first_batch"] = 2
        with patch.object(runner, "inquire", side_effect=[None, True]), patch.object(runner, "check_done", return_value=False):
            runner.send_wave(None, task, quota=1)
            self.assertEqual(task.waves, 1)
            self.assertEqual(task.wave_remaining, 2)
            task.blocked_until = 0
            runner.send_wave(None, task, quota=1)
        self.assertEqual(task.waves, 1)
        self.assertEqual(task.wave_remaining, 1)

    def test_twelve_parts_use_own_model_and_quantity(self):
        parts = workbench.validate_parts([{"part": f"chip-{i}", "qty": "5k"} for i in range(12)])
        runner = Runner(parts, message="请报 {part} × {qty} 的含税价")
        runner.make_tasks()
        self.assertEqual(len(runner.tasks), 12)
        for i, task in enumerate(runner.tasks):
            self.assertEqual(task.text, f"请报 CHIP-{i} × 5000 的含税价")
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(DISTINCT batch_id) FROM task").fetchone()[0], 1)

    def test_duplicate_and_invalid_rows_rejected_without_silent_skip(self):
        for data in ([{"part": "A", "qty": 1}, {"part": "a ", "qty": 2}],
                     [{"part": "A", "qty": "5oops"}], [{"part": "A", "qty": 0}],
                     [{"part": "A", "qty": 1.5}], [{"part": "", "qty": 5}]):
            with self.assertRaises(ValueError):
                workbench.validate_parts(data)

    def test_duplicate_model_cannot_create_queue_or_cli_tasks(self):
        template = "请报 {part} × {qty}"
        parts = [(" chip-1 /a", 100), ("CHIP-1/\tA", 200)]
        items = [{"part": part, "qty": qty} for part, qty in parts]
        body = {"request_id": uuid.uuid4().hex, "parts": items, "template": template, "dry_run": True}

        with self.assertRaisesRegex(ValueError, r"型号 CHIP-1/A 重复"):
            jobs.submit(body, {})
        with self.assertRaisesRegex(ValueError, r"型号 CHIP-1/A 重复"):
            Runner(parts, message=template).make_tasks()

        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inquiry_job").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task").fetchone()[0], 0)

    def test_part_suffix_and_punctuation_do_not_create_duplicates(self):
        parts = ["chip-1 /a", "chip-1 /b", "chip-1-a", "chip1/a", "chip-1-a+"]
        self.assertEqual(workbench.validate_parts([{"part": part, "qty": 5} for part in parts]), [
            ("CHIP-1/A", 5), ("CHIP-1/B", 5), ("CHIP-1-A", 5), ("CHIP1/A", 5), ("CHIP-1-A+", 5),
        ])

    def test_hardcoded_or_wrong_template_rejected(self):
        for tpl in ("询价 STM32 × 5000", "询价 {part}", "{part.__class__} {qty}", "{part} {qty:.0f}"):
            with self.assertRaises(ValueError):
                workbench.validate_template(tpl)
        workbench.validate_template("请报 {part} {qtyk}")

    def test_card_sending_is_explicit_opt_in(self):
        with patch("inquiry.runner.SETTINGS", {"inquiry": {"card_image": "名片.jpg", "send_card": False}}):
            self.assertIsNone(Runner([]).card_image)

    def test_existing_chat_can_accept_second_model(self):
        runner, first, sid, row = self.fixture()
        runner.chat_owner = {"恒佳达-张小姐": [(first, sid)]}
        runner.card_image = None
        other = Task(db.create_task("OTHER", 2), "OTHER", 2, "询价 OTHER 2")
        with patch.object(runner, "open_chat", return_value="恒佳达-张小姐"), patch.object(qq, "wait_layout_stable"), \
             patch.object(qq, "verify_supplier"), patch.object(qq, "send_text") as send, \
             patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), \
             patch.object(qq, "read_messages", side_effect=[[], [message("second", other.text, True)]]):
            self.assertTrue(runner.inquire(other, row))
        send.assert_called_once()
        self.assertEqual(len(runner.chat_owner["恒佳达-张小姐"]), 2)


class ModelTests(IsolatedTest):
    def test_models_group_case_and_space_but_keep_suffixes(self):
        first = db.create_task("STM32-A", 10)
        second = db.create_task(" stm32-a ", 20)
        db.create_task("STM32/A", 30)
        a = db.add_supplier(first, {"company": "C", "qqs": []})
        b = db.add_supplier(second, {"company": "C", "qqs": []})
        db.update_supplier(a, sent_at=1, status="quoted")
        db.update_supplier(b, sent_at=2, status="quoted")
        db.save_quote(a, {"unit_price": 10}, True)
        db.save_quote(b, {"unit_price": 20}, True)
        groups = workbench.model_groups(query="stm32-")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["runs"], 2)
        self.assertEqual(groups[0]["valid"], 1)
        self.assertEqual(groups[0]["supplier_ids"], [b])
        self.assertEqual(groups[0]["qty"], 20)

    def test_api_search_and_model_details(self):
        _, task, sid, _ = self.fixture()
        for status in ('failed', 'waiting_desktop', 'pending', 'sending', 'send_uncertain', 'identity_unverified'):
            other = db.add_supplier(task.id, {"company": "未发送-"+status, "qqs": []})
            db.update_supplier(other, status=status)
        client = server.app.test_client()
        r = client.get("/api/models?q=F405")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json[0]["task_ids"], [task.id])
        self.assertEqual(r.json[0]["total"], 1)
        d = client.get("/api/models/detail?part=STM32F405RGT6")
        self.assertEqual([s["id"] for s in d.json["suppliers"]], [sid])
        self.assertEqual(d.json["task"]["runs"], 1)
        self.assertEqual([s["id"] for s in client.get(f"/api/tasks/{task.id}").json["suppliers"]], [sid])
        self.assertEqual(client.get('/api/tasks').json[0]['total'], 1)
        self.assertEqual(len(db.suppliers(task.id)), 7)  # Display filtering never deletes failed attempts.


class OutboxTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        p = patch.object(qq, "verify_bound_history")
        p.start()
        self.addCleanup(p.stop)

    def enqueue(self):
        runner, task, sid, row = self.fixture()
        item = workbench.queue_reply(sid, "请报实价", str(uuid.uuid4()))
        return sid, item

    def test_same_request_is_idempotent(self):
        _, _, sid, _ = self.fixture()
        key = str(uuid.uuid4())
        a = workbench.queue_reply(sid, "请报实价", key)
        b = workbench.queue_reply(sid, "请报实价", key)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["content"], "请报实价")
        with self.assertRaises(ValueError):
            workbench.queue_reply(sid, "其他内容", key)
        with self.assertRaises(ValueError):
            workbench.queue_reply(sid, "请报实价", str(uuid.uuid4()))

    def test_unbound_supplier_cannot_send(self):
        _, _, sid, _ = self.fixture()
        db.update_supplier(sid, sent_at=None)
        with self.assertRaises(ValueError):
            workbench.queue_reply(sid, "你好", str(uuid.uuid4()))

    def test_bound_reply_sent_once_and_saved(self):
        sid, item = self.enqueue()
        with patch.object(qq, "open_recent", return_value=True), patch.object(qq, "verify_supplier"), patch.object(qq, "send_text") as send:
            self.assertTrue(dispatch.process_one(object(), {}))
            self.assertFalse(dispatch.process_one(object(), {}))
        send.assert_called_once_with(send.call_args[0][0], "请报实价", expect_chat=item["chat_name"], company=item["company"])
        self.assertEqual(workbench.outbox_for(sid)[0]["status"], "sent")
        self.assertEqual(db.messages(sid, "out")[-1], item["content"])

    def test_changed_binding_never_sends(self):
        sid, _ = self.enqueue()
        db.update_supplier(sid, chat_name="另一个人")
        with patch.object(qq, "send_text") as send:
            dispatch.process_one(object(), {})
        send.assert_not_called()
        self.assertEqual(workbench.outbox_for(sid)[0]["status"], "failed")

    def test_uncertain_send_is_not_retried(self):
        sid, _ = self.enqueue()
        with patch.object(qq, "open_recent", return_value=True), patch.object(qq, "verify_supplier"), \
             patch.object(qq, "send_text", side_effect=SendUncertainError("不确定")) as send:
            dispatch.process_one(object(), {})
            dispatch.process_one(object(), {})
        self.assertEqual(workbench.outbox_for(sid)[0]["status"], "uncertain")
        send.assert_called_once()

    def test_deferred_and_cancelled_reply_never_sends(self):
        sid, item = self.enqueue()
        with patch.object(qq, "open_recent", side_effect=qq.DesktopDeferred("后台不支持")), patch.object(qq, "send_text") as send:
            dispatch.process_one(object(), {})
            self.assertEqual(workbench.outbox_for(sid)[0]["status"], "deferred")
            workbench.cancel_reply(item["id"])
            dispatch.process_one(object(), {})
        send.assert_not_called()
        self.assertEqual(workbench.outbox_for(sid)[0]["status"], "cancelled")

    def test_crash_recovery_does_not_retry_sending(self):
        sid, item = self.enqueue()
        with db.connect() as c:
            c.execute("UPDATE outbox SET status='sending' WHERE id=?", (item["id"],))
        db.update_supplier(sid, status="sending")
        dispatch.recover_interrupted()
        self.assertEqual(workbench.outbox_for(sid)[0]["status"], "uncertain")
        self.assertEqual(db.supplier(sid)["status"], "send_uncertain")

    def test_global_rate_limit_survives_worker_restart(self):
        dispatch.record_attempt(1, "inquiry")
        self.assertGreater(dispatch.rate_delay({"min_interval": 0}), 19)
        with db.connect() as c:
            c.executemany("INSERT INTO send_attempt(created_at,kind) VALUES(?,'reply')", [(time.time() - 120,)] * 60)
        self.assertGreater(dispatch.rate_delay({"max_per_hour": 600}), 3400)

    def test_reply_api_requires_confirmation_and_reuses_request(self):
        _, _, sid, _ = self.fixture()
        client = server.app.test_client()
        payload = {"text": "请报价", "request_id": str(uuid.uuid4())}
        self.assertEqual(client.post(f"/api/suppliers/{sid}/reply", json=payload).status_code, 400)
        with patch.object(server, "start_reply_worker"):
            payload["confirm_send"] = True
            a = client.post(f"/api/suppliers/{sid}/reply", json=payload)
            b = client.post(f"/api/suppliers/{sid}/reply", json=payload)
        self.assertEqual(a.status_code, 202)
        self.assertEqual(a.json["id"], b.json["id"])
        self.assertEqual(a.json["content"], payload["text"])

    def test_foreign_webpage_cannot_trigger_send(self):
        response = server.app.test_client().post("/api/run", json={"parts": []}, headers={"Origin": "https://example.com"})
        self.assertEqual(response.status_code, 403)

    def test_stop_pauses_queued_replies(self):
        sid, _ = self.enqueue()
        with patch.object(server, "run_state", return_value={"running": True}):
            self.assertEqual(server.app.test_client().post("/api/stop").status_code, 200)
        self.assertEqual(workbench.outbox_for(sid)[0]["status"], "deferred")
        with patch.object(qq, "send_text") as send:
            dispatch.process_one(object(), {})
        send.assert_not_called()


class BackgroundTests(IsolatedTest):
    def test_reply_binding_requires_original_message_not_just_name(self):
        with patch.object(qq, "read_messages", return_value=[message("wrong", "询价 X", True)]), \
             patch.object(qq, "read_inquiry_history", side_effect=RuntimeError("未找到原询价")):
            with self.assertRaises(qq.DesktopDeferred):
                qq.verify_bound_history(object(), "同名联系人", "询价 X", "right", 1)

    def test_original_message_anchor_confirms_binding_without_scrolling(self):
        with patch.object(qq, "read_messages", return_value=[message("right", "询价 X", True)]), \
             patch.object(qq, "current_chat_name", return_value="联系人"), patch.object(qq, "read_inquiry_history") as scroll:
            qq.verify_bound_history(object(), "联系人", "询价 X", "right", 1)
        scroll.assert_not_called()

    def test_activate_never_changes_foreground(self):
        win = Mock(NativeWindowHandle=111)
        with patch.object(qq.win32gui, "GetForegroundWindow", return_value=222), \
             patch.object(qq.win32gui, "SetForegroundWindow") as switch, patch.object(qq.win32gui, "ShowWindow") as restore:
            self.assertFalse(qq.activate(win))
        switch.assert_not_called()
        restore.assert_not_called()

    def test_background_mode_forbids_global_mouse(self):
        with patch.object(qq, "FOREGROUND_ASSIST", False):
            with self.assertRaises(qq.DesktopDeferred):
                qq.human_click(Mock(), Mock())

    def test_background_text_uses_patterns_without_clipboard(self):
        editor, value, invoke, button = Mock(), Mock(), Mock(), Mock()
        editor.ClassName = "is-empty"
        value.IsReadOnly = False
        editor.GetPattern.return_value = value
        button.GetPattern.return_value = invoke
        with patch.object(qq, "verify_supplier"), patch.object(qq, "editor_of", return_value=editor), \
             patch.object(qq, "find", return_value=button), patch.object(qq, "editor_text", return_value="hello"), \
             patch.object(qq, "current_chat_name", return_value="chat"), patch.object(qq.time, "sleep"), \
             patch.object(qq.auto, "SetClipboardText") as clipboard:
            qq.send_text(object(), "hello", "chat", company="company")
        value.SetValue.assert_called_once_with("hello")
        invoke.Invoke.assert_called_once()
        clipboard.assert_not_called()

    def test_failed_reads_backoff_instead_of_repeated_clicking(self):
        runner, task, sid, _ = self.fixture()
        runner.chat_owner = {"恒佳达-张小姐": [(task, sid)]}
        with patch.object(qq, "recent_contacts", return_value=[]), patch.object(runner, "read_chat", return_value=False) as read:
            runner.poll_replies()
            runner.poll_replies()
        read.assert_called_once()

    def test_partial_background_read_merges_without_erasing(self):
        runner, task, sid, _ = self.fixture()
        runner.async_analysis = True
        db.replace_incoming(sid, ["旧回复"], [{"id": "old", "text": "旧回复"}])
        snapshot = HistorySnapshot([message("anchor", task.text, True), message("new", "新回复")], "anchor", False)
        with patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), patch.object(qq, "read_inquiry_history", return_value=snapshot):
            runner.read_chat("恒佳达-张小姐", task, sid)
        self.assertEqual(db.messages(sid, "in"), ["旧回复", "新回复"])
        self.assertIn("完整历史待", db.supplier(sid)["read_error"])


class AnalysisTests(IsolatedTest):
    def test_passive_observer_cannot_click_scroll_or_send(self):
        from inquiry import observer
        runner, task, sid, _ = self.fixture()
        messages = [message("anchor", task.text, True), message("r", "25含税")]
        with patch.object(qq, "main_window", return_value=object()), patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), \
             patch.object(qq, "read_messages", return_value=messages), patch.object(qq, "open_recent") as switch, \
             patch.object(qq, "scroll_region") as scroll, patch.object(qq, "send_text") as send:
            observer.observe_once()
        switch.assert_not_called()
        scroll.assert_not_called()
        send.assert_not_called()
        self.assertEqual(db.messages(sid, "in"), ["25含税"])

    def test_unknown_passive_history_does_not_attribute_old_messages(self):
        from inquiry import observer
        _, _, sid, _ = self.fixture()
        self.assertIsNone(observer.visible_snapshot(db.supplier(sid), [message("old", "其他询价", True), message("r", "25")]))

    def test_raw_reply_queued_without_blocking_on_model(self):
        runner, task, sid, _ = self.fixture()
        runner.async_analysis = True
        snapshot = HistorySnapshot([message("anchor", task.text, True), message("r", "含税25")], "anchor")
        with patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), patch.object(qq, "read_inquiry_history", return_value=snapshot), patch.object(llm, "parse_reply") as model:
            runner.read_chat("恒佳达-张小姐", task, sid)
        model.assert_not_called()
        self.assertEqual(workbench.suggestions_for(sid)["status"], "queued")

    def test_analysis_stores_quote_and_choices_in_one_call(self):
        _, _, sid, _ = self.fixture()
        db.replace_incoming(sid, ["25含税"])
        workbench.enqueue_analysis(sid)
        q = {**llm.normalize({"status": "quoted", "unit_price": 25}), "reply_suggestions": ["请问批次？", "交期多久？", "请确认数量。"]}
        with patch.object(llm, "parse_reply", return_value=q) as model:
            analysis.process_one()
            analysis.process_one()
        model.assert_called_once()
        self.assertEqual(len(workbench.suggestions_for(sid)["items"]), 3)
        self.assertEqual(db.supplier(sid)["status"], "quoted")

    def test_stale_suggestions_hidden_and_stale_results_discarded(self):
        _, _, sid, _ = self.fixture()
        db.replace_incoming(sid, ["25"])
        workbench.enqueue_analysis(sid)
        def delayed(*args, **kwargs):
            db.replace_incoming(sid, ["30"])
            workbench.enqueue_analysis(sid)
            return {**llm.normalize({"status": "quoted", "unit_price": 25}), "reply_suggestions": ["旧答案"]}
        with patch.object(llm, "parse_reply", side_effect=delayed):
            analysis.process_one()
        self.assertEqual(workbench.suggestions_for(sid)["items"], [])
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quote").fetchone()[0], 0)

    def test_ambiguous_batch_reply_saved_for_manual_assignment(self):
        runner, first, sid, row = self.fixture()
        second = Task(db.create_task("OTHER", 20), "OTHER", 20, "询价 OTHER 20")
        sid2 = db.add_supplier(second.id, row)
        db.update_supplier(sid2, sent_at=time.time()+1, chat_name="恒佳达-张小姐", status="sent", inquiry_message_id="a2")
        db.add_message(sid2, "out", second.text)
        runner.async_analysis = True
        snapshot = HistorySnapshot([message("a2", second.text, True), message("r", "含税25")], "a2")
        with patch.object(qq, "current_chat_name", return_value="恒佳达-张小姐"), patch.object(qq, "read_inquiry_history", return_value=snapshot):
            runner.read_chat("恒佳达-张小姐", second, sid2)
        self.assertEqual(db.messages(sid2, "in"), [])
        with db.connect() as c:
            mid = c.execute("SELECT id FROM inbox").fetchone()[0]
        workbench.assign_message(mid, sid2)
        workbench.assign_message(mid, sid2)
        self.assertEqual(db.messages(sid2, "in"), ["含税25"])
        self.assertEqual(workbench.suggestions_for(sid2)["status"], "queued")
