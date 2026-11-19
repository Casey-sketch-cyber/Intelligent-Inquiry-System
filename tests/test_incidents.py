import json
import time
import uuid
from unittest.mock import patch

from inquiry import analysis, db, incidents, llm, workbench
from test_reliability import IsolatedTest
from ui import server


class IncidentTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        versions = patch.object(incidents, "_versions", return_value={"qq_version": "9.9.0.1", "edge_version": "140.0.0.2"})
        versions.start()
        self.addCleanup(versions.stop)

    def events(self):
        with db.connect() as conn:
            return [json.loads(r["payload"]) for r in conn.execute("SELECT payload FROM incident ORDER BY id")]

    def test_privacy_and_search_window_in_both_sinks(self):
        _, task, sid, _ = self.fixture()
        company, number, message = "德莫测试企业", "79241586302", "不可写入诊断文件的供应商回复"
        db.update_supplier(sid, company=company, qq_uin=number)
        db.add_message(sid, "in", message)
        now = time.time()
        with db.connect() as conn:
            for age in (60, 120, 660, 3660):
                conn.execute("INSERT INTO search_attempt(created_at,part_no,page_no,source) VALUES(?,?,1,'candidates')",
                             (now - age, task.part_no))
            for age in (45, 630, 7200):
                conn.execute("INSERT INTO send_attempt(created_at,supplier_id,kind) VALUES(?,?,'inquiry')", (now - age, sid))
        extra = {"company": company, "qq_uin": number, "message": message, "control": message}
        incidents.record("captcha", "search", severity="warning", supplier_id=sid, error=RuntimeError(message), extra=extra)
        incidents.record("identity_unverified", "reply_send", severity="warning", supplier_id=sid)

        first, second = self.events()
        self.assertEqual(first["searches"], {"10m": 2, "1h": 3})
        self.assertEqual(first["sends"], {"10m": 1, "1h": 2})
        self.assertEqual(first["qq_version"], "9.9.0.1")
        self.assertEqual(first["edge_version"], "140.0.0.2")
        for field in ("company_hash", "qq_uin_hash"):
            self.assertRegex(first[field], "^[0-9a-f]{64}$")
            self.assertEqual(first[field], second[field])
        directory = self.path / "incidents"
        lines = (directory / f"{first['date'].replace('-', '')}.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line) for line in lines], [first, second])
        self.assertEqual(len((directory / ".salt").read_bytes()), 32)
        for text in (json.dumps([first, second], ensure_ascii=False), "\n".join(lines)):
            for private in (company, number, message):
                self.assertNotIn(private, text)

    def test_analysis_distinguishes_timeout_and_invalid_model_output(self):
        _, _, sid, _ = self.fixture()
        db.add_message(sid, "in", "含税价格请仅保存在会话里")
        for error, expected in ((TimeoutError("time limit"), "llm_timeout"),
                                (ValueError("invalid response"), "llm_parse_failed")):
            workbench.enqueue_analysis(sid, refresh=True)
            with patch.object(llm, "parse_reply", side_effect=error):
                self.assertTrue(analysis.process_one())
            self.assertEqual(self.events()[-1]["kind"], expected)

    def test_stats_route_exposes_only_counts(self):
        with db.connect() as conn:
            conn.execute("INSERT INTO incident(created_at,occurred_date,kind,stage,severity,payload) VALUES(0,'2000-01-01','captcha','search','warning','{}')")
        incidents.record("captcha", "search", severity="warning")
        incidents.record("captcha", "queue", severity="warning")
        client = server.app.test_client()
        result = client.get("/api/incidents?days=14").get_json()
        self.assertEqual(result["by_type"], [{"count": 2, "kind": "captcha", "label": "网站验证码"}])
        self.assertEqual(len(result["by_date"]), 1)
        self.assertEqual(set(result["by_date"][0]), {"date", "count", "kind", "label"})
        self.assertEqual(client.get("/api/incidents?days=0").status_code, 400)

    def test_suggestion_edit_and_rejection_do_not_log_content_or_repeat(self):
        _, _, sid, _ = self.fixture()
        db.add_message(sid, "in", "供应商回复原文")
        workbench.enqueue_analysis(sid)
        suggestion = "建议客户稍后联系"
        with db.connect() as conn:
            conn.execute("UPDATE analysis_job SET status='done',suggestions=? WHERE supplier_id=?", (json.dumps([suggestion]), sid))
        client = server.app.test_client()
        current = workbench.suggestions_for(sid)
        data = {"confirm_send": True, "request_id": str(uuid.uuid4()), "text": "人工修改的回复",
                "suggestion": {"digest": current["digest"], "index": 0}}
        with patch.object(server, "start_reply_worker"):
            for _ in range(2):
                result = client.post(f"/api/suppliers/{sid}/reply", json=data)
                self.assertEqual(result.status_code, 202)
                self.assertEqual(result.get_json()["content"], data["text"])
        for _ in range(2):
            result = client.post(f"/api/suppliers/{sid}/suggestion-rejection", json={"digest": current["digest"]})
            self.assertEqual(result.status_code, 200)
        self.assertEqual([e["kind"] for e in self.events()], ["suggestion_edited", "suggestion_rejected"])
        lines = next((self.path / "incidents").glob("*.jsonl")).read_text(encoding="utf-8")
        for private in (suggestion, data["text"], current["digest"]):
            self.assertNotIn(private, lines)
        with db.connect() as conn:
            conn.execute("UPDATE analysis_job SET suggestions=? WHERE supplier_id=?", (json.dumps(["新的建议"]), sid))
        self.assertNotEqual(current["digest"], workbench.suggestions_for(sid)["digest"])
        self.assertEqual(client.post(f"/api/suppliers/{sid}/suggestion-rejection", json={"digest": current["digest"]}).status_code, 400)
