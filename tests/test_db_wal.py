"""用 D 盘临时库验证 WAL 下的并发读写与锁等待。"""
import json
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from inquiry import db, jobs, runtime


class SQLiteWalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="inquiry-wal-", dir=runtime.TEMP_DIR)
        self.addCleanup(directory.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(directory.name) / "test.db"))

    def test_wal_and_busy_timeout_are_set_per_connection(self):
        for timeout, expected_ms in ((None, 30_000), (0.125, 125), (None, 30_000)):
            with self.subTest(timeout=timeout):
                connection = db.connect() if timeout is None else db.connect(timeout=timeout)
                with connection as conn:
                    self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                    self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], expected_ms)

    def test_unpatched_sqlite_uses_rollback_and_refuses_existing_wal(self):
        with patch.object(db.sqlite3, "sqlite_version_info", (3, 49, 1)):
            db.create_task("ORIGINAL", 100)
            with db.connect() as conn:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        with patch.object(db.sqlite3, "sqlite_version_info", (3, 49, 1)):
            with self.assertRaisesRegex(db.sqlite3.OperationalError, "缺少 WAL 修复"):
                db.create_task("BLOCKED", 100)
        with db.connect() as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT part_no FROM task")], ["ORIGINAL"])

    def test_concurrent_first_use_migrates_existing_rollback_journal_database(self):
        with closing(db.sqlite3.connect(db.DB_PATH)) as legacy, legacy:
            self.assertEqual(legacy.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            legacy.executescript(db.SCHEMA)
            legacy.execute("INSERT INTO task(part_no, qty, created_at) VALUES ('LEGACY', 5000, 0)")
        ready = threading.Barrier(3)

        def create_task(index):
            ready.wait(timeout=10)
            return db.create_task(f"MODEL{index}", 5000)

        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(len(set(pool.map(create_task, range(3)))), 3)
        with db.connect() as conn:
            tasks = {row[0] for row in conn.execute("SELECT part_no FROM task")}
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(tasks, {"LEGACY", "MODEL0", "MODEL1", "MODEL2"})

    def test_wal_activation_waits_for_legacy_reader(self):
        with closing(db.sqlite3.connect(db.DB_PATH)) as legacy:
            legacy.execute("CREATE TABLE legacy_probe (id INT)")
            legacy.execute("BEGIN")
            legacy.execute("SELECT * FROM legacy_probe").fetchall()

            def read_journal_mode():
                with db.connect(timeout=2) as conn:
                    return conn.execute("PRAGMA journal_mode").fetchone()[0]

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(read_journal_mode)
                try:
                    with self.assertRaises(TimeoutError):
                        future.result(timeout=0.1)
                finally:
                    legacy.rollback()
                self.assertEqual(future.result(), "wal")

    def test_open_reader_keeps_snapshot_while_writer_commits(self):
        db.create_task("FIRST", 100)
        with db.connect() as reader:
            reader.execute("BEGIN")
            self.assertEqual(reader.execute("SELECT part_no FROM task").fetchone()[0], "FIRST")
            with db.connect(timeout=0.5) as writer:
                writer.execute("INSERT INTO task(part_no, qty, created_at) VALUES ('SECOND', 100, 0)")
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM task").fetchone()[0], 1)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task").fetchone()[0], 2)

    def test_analysis_collection_and_api_writes_commit_600_transactions(self):
        supplier_ids = range(1, 201)
        task_id = db.create_task("FIXTURE", 5000)
        with db.connect() as conn:
            conn.executemany(
                "INSERT INTO supplier(id, task_id, company) VALUES (?,?,?)",
                [(sid, task_id, f"测试供应商 {sid}") for sid in supplier_ids],
            )
        ready = threading.Barrier(3)

        def save_analysis(sid):
            db.save_quote(sid, {"unit_price": sid}, valid=True)

        def save_collection(sid):
            text = f"测试报价 {sid}"
            db.replace_incoming(sid, [text], [{"id": str(sid), "time": "10:00"}])

        def submit_api_job(sid):
            jobs.submit({
                "request_id": uuid.UUID(int=sid).hex,
                "parts": [{"part": f"MODEL{sid}", "qty": 5000}],
                "template": "请报价 {part}，数量 {qty}",
                "dry_run": True,
            }, {})

        def write_batch(write):
            for sid in supplier_ids:
                # 让三种写入在每组操作开始时重新并发，避免某个线程提前完成全部写入。
                if sid % 10 == 1:
                    ready.wait(timeout=10)
                write(sid)

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(write_batch, write) for write in (save_analysis, save_collection, submit_api_job)]
            for future in futures:
                future.result()

        with db.connect() as conn:
            quotes = {row["supplier_id"]: json.loads(row["data_json"]) for row in conn.execute("SELECT * FROM quote")}
            messages = {
                row["supplier_id"]: (row["source_id"], row["content"])
                for row in conn.execute("SELECT * FROM message WHERE direction='in'")
            }
            queued = {
                (row["id"], row["part_no"], row["qty"])
                for row in conn.execute("SELECT j.id, t.part_no, t.qty FROM inquiry_job j JOIN task t ON t.batch_id=j.id")
            }
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM message").fetchone()[0], 200)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inquiry_job").fetchone()[0], 200)
        self.assertEqual(quotes, {sid: {"unit_price": sid} for sid in supplier_ids})
        self.assertEqual(messages, {sid: (str(sid), f"测试报价 {sid}") for sid in supplier_ids})
        self.assertEqual(queued, {(uuid.UUID(int=sid).hex, f"MODEL{sid}", 5000) for sid in supplier_ids})
