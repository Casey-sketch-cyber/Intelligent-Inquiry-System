"""维护程序应通过统一的引擎检查再访问业务库。"""

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from inquiry import db, runtime
from lanhost import reset_history, upgrade_trial


class MaintenanceJournalGuardTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=runtime.TEMP_DIR)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.database = self.data / "inquiry.db"

    def test_maintenance_open_keeps_existing_delete_mode_and_enforces_read_only(self):
        with closing(db.sqlite3.connect(self.database)) as conn, conn:
            conn.execute("CREATE TABLE example (value TEXT)")
            conn.execute("INSERT INTO example VALUES ('keep')")
        with patch.object(db, "_has_wal_fix", return_value=False), db.open_existing(self.database, read_only=True) as reader:
            self.assertEqual(reader.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(reader.execute("SELECT value FROM example").fetchone()[0], "keep")
            with self.assertRaises(db.sqlite3.OperationalError):
                reader.execute("DELETE FROM example")
        with db.open_existing(self.database, read_only=True) as reader:
            self.assertEqual(reader.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_writable_maintenance_open_does_not_create_missing_business_db(self):
        with self.assertRaises(db.sqlite3.OperationalError):
            with db.open_existing(self.database):
                self.fail("维护工具打开了不存在的业务库")
        self.assertFalse(self.database.exists())

    def test_older_driver_rejects_wal_for_maintenance_and_preserves_previous_backup(self):
        with closing(db.sqlite3.connect(self.database)) as conn, conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            conn.execute("CREATE TABLE example (value TEXT)")
            conn.execute("INSERT INTO example VALUES ('keep')")
        target = self.root / "migration.db"
        target.write_bytes(b"previous backup")
        with patch.object(db, "_has_wal_fix", return_value=False):
            for read_only in (True, False):
                with self.subTest(read_only=read_only), self.assertRaisesRegex(db.sqlite3.OperationalError, "缺少 WAL 修复"):
                    with db.open_existing(self.database, read_only=read_only):
                        self.fail("维护工具取得了存在缺陷的 WAL 连接")
            with self.assertRaisesRegex(db.sqlite3.OperationalError, "缺少 WAL 修复"):
                db.snapshot(self.database, target)
        self.assertEqual(target.read_bytes(), b"previous backup")
        with db.open_existing(self.database, read_only=True) as conn:
            self.assertEqual(conn.execute("SELECT value FROM example").fetchone()[0], "keep")

    def test_history_reset_checks_wal_before_creating_a_backup_or_deleting_rows(self):
        with patch.object(db, "DB_PATH", self.database):
            task_id = db.create_task("EXAMPLE-001", 5000)
        with patch.object(db, "_has_wal_fix", return_value=False):
            with self.assertRaisesRegex(db.sqlite3.OperationalError, "缺少 WAL 修复"):
                reset_history.clear_history(self.root)
        self.assertFalse((self.data / "backups").exists())
        with db.open_existing(self.database, read_only=True) as conn:
            self.assertEqual(conn.execute("SELECT id FROM task").fetchall(), [(task_id,)])

    def test_upgrade_rejects_older_wal_driver_before_creating_backup(self):
        with patch.object(db, "DB_PATH", self.database):
            db.create_task("EXAMPLE-002", 5000)
        with patch.object(upgrade_trial, "validate_files"), patch.object(db, "_has_wal_fix", return_value=False):
            with self.assertRaisesRegex(db.sqlite3.OperationalError, "缺少 WAL 修复"):
                upgrade_trial.install(self.root, self.root, {})
        self.assertFalse((self.data / "backups").exists())
