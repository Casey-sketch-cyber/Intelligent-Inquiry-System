"""工作机停服清理：验证可恢复备份、事务回滚和既有频率记录不被清空。"""
import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

from test_reliability import IsolatedTest
from inquiry import db
from lanhost import reset_history
from ui import service


class HistoryResetTests(IsolatedTest):
    def seed(self):
        self.root = self.path / "worker"
        self.data = self.root / "data"
        self.data.mkdir(parents=True)
        self.database = self.data / "inquiry.db"
        self.patch_db = patch.object(db, "DB_PATH", self.database)
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)
        _, task, sid, _ = self.fixture()
        db.add_message(sid, "in", "现货，含税 10 元")
        with db.connect() as c:
            c.execute("INSERT INTO quote VALUES(?,?,?,?)", (sid, '{"unit_price":10}', 1, 1))
            c.execute("INSERT INTO analysis_job(supplier_id,digest,status) VALUES(?,?,'done')", (sid, "digest"))
            c.execute("INSERT INTO outbox(request_id,supplier_id,company,chat_name,qq_uin,content,status,created_at,updated_at) "
                      "VALUES('old',?,'company','chat','123','draft','canceled',1,1)", (sid,))
            c.execute("INSERT INTO inbox(chat_name,source_id,content,created_at,assigned_to) VALUES('chat','mid','reply',1,?)", (sid,))
            c.execute("INSERT INTO send_attempt(created_at,supplier_id,kind) VALUES(1,?,'inquiry')", (sid,))
        reports = self.data / "reports"
        reports.mkdir()
        (reports / "old.xlsx").write_bytes(b"report-fixture")
        (self.root / "settings.toml").write_text("send_card = false\n", encoding="utf-8")
        (self.data / "worker.json").write_text('{"keep":"pairing"}', encoding="utf-8")
        return task.id

    def counts(self, path=None):
        with closing(sqlite3.connect(path or self.database)) as c:
            return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in (*reset_history.TABLES, "send_attempt")}

    def test_backup_restores_all_history_and_pacing_and_config_are_preserved(self):
        self.seed()
        before = self.counts()
        result = reset_history.clear_history(self.root)
        backup = self.data / "backups" / result["backup"].split("\\")[-1]
        self.assertEqual(self.counts(backup / "inquiry.db"), before)
        self.assertEqual((backup / "reports/old.xlsx").read_bytes(), b"report-fixture")
        self.assertFalse(list((self.data / "reports").iterdir()))
        self.assertEqual(result["state"], "cleared")
        self.assertTrue(all(self.counts()[t] == 0 for t in reset_history.TABLES))
        self.assertEqual(self.counts()["send_attempt"], 1)
        self.assertEqual(json.loads((self.data / "worker.json").read_text()), {"keep": "pairing"})
        self.assertEqual((self.root / "settings.toml").read_text(), "send_card = false\n")
        self.assertFalse(result["allow_send"])

    def test_active_worker_or_runner_refuses_cleanup_before_backup(self):
        self.seed()
        before = self.counts()
        for name in ("ui_server.lock", "runner.lock"):
            with self.subTest(name=name), service.acquire_lock(self.data / name):
                with self.assertRaisesRegex(RuntimeError, "still active"):
                    reset_history.clear_history(self.root)
                self.assertEqual(self.counts(), before)
                self.assertFalse((self.data / "backups").exists())

    def test_report_backup_failure_keeps_business_data(self):
        self.seed()
        before = self.counts()
        with patch.object(reset_history.shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                reset_history.clear_history(self.root)
        self.assertEqual(self.counts(), before)
        self.assertTrue((self.data / "reports/old.xlsx").is_file())

    def test_delete_failure_rolls_back_all_child_tables_and_keeps_report(self):
        self.seed()
        before = self.counts()
        with db.connect() as c:
            c.execute("CREATE TRIGGER refuse_task_delete BEFORE DELETE ON task BEGIN SELECT RAISE(ABORT,'blocked'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            reset_history.clear_history(self.root)
        self.assertEqual(self.counts(), before)
        self.assertTrue((self.data / "reports/old.xlsx").is_file())
        backup = next((self.data / "backups").iterdir())
        self.assertEqual(self.counts(backup / "inquiry.db"), before)

    def test_missing_database_and_outside_d_paths_are_refused(self):
        with self.assertRaises(RuntimeError):
            reset_history.safe_path("C:/worker/data", "C:/worker")
        with self.assertRaises(RuntimeError):
            reset_history.safe_path(self.path.parent, self.path)
        root = self.path / "empty"
        root.mkdir()
        with self.assertRaisesRegex(RuntimeError, "not found"):
            reset_history.clear_history(root)
        self.assertFalse((root / "data/inquiry.db").exists())

    def test_snapshot_change_refuses_deletion_and_reused_ids_have_different_fingerprint(self):
        self.seed()
        before = self.counts()
        with self.assertRaisesRegex(RuntimeError, "Task list changed"):
            reset_history.clear_history(self.root, "0" * 64)
        self.assertEqual(self.counts(), before)
        self.assertFalse((self.data / "backups").exists())
        first = dict(id=1, part_no="A", qty=5000, created_at=100.0)
        later = dict(first, created_at=200.0)
        self.assertNotEqual(reset_history.task_fingerprint([first]), reset_history.task_fingerprint([later]))
