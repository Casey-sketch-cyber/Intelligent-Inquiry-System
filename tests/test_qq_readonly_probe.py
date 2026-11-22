"""只用临时数据验证 QQ 只读诊断的正式库访问。"""
import contextlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

from inquiry import db
from test_reliability import IsolatedTest
from tools import qq_readonly_probe as probe


class QQReadOnlyProbeTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(probe, "ROOT", self.path))
        self.enterContext(patch.object(sys, "argv", ["qq_readonly_probe", "--session", "offline"]))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.qq_window = self.enterContext(patch.object(probe.qq, "main_window", return_value=None))
        self.folder = self.path / "data" / "qa" / "offline"
        self.supplier_id = self.fixture()[2]

    def test_diagnostic_counts_and_copies_committed_wal_data(self):
        with db.open_existing(db.DB_PATH) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO message(supplier_id, direction, content, seen_at) VALUES (?, 'out', 'WAL 测试', 1)",
                (self.supplier_id,),
            )
            writer.commit()
            self.assertGreater(db.DB_PATH.with_suffix(".db-wal").stat().st_size, 0)
            source_before = db.DB_PATH.read_bytes()
            probe.main()
            self.assertEqual(db.DB_PATH.read_bytes(), source_before)

        self.qq_window.assert_called_once()
        report_path, = self.folder.glob("probe-*.json")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        expected = {"outgoing": 2, "queued": 0, "send_attempt": 0}
        self.assertEqual(report["production_before"], expected)
        self.assertEqual(report["production_after"], expected)
        target = Path(report["isolated_database"])
        self.assertEqual(target, self.folder / "readonly.db")
        self.assertEqual(probe.send_counts(target), expected)
        with db.open_existing(target, read_only=True) as snapshot:
            self.assertEqual(snapshot.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_unpatched_sqlite_rejects_wal_before_contacting_qq(self):
        with patch.object(db.sqlite3, "sqlite_version_info", (3, 49, 1)):
            with self.assertRaisesRegex(db.sqlite3.OperationalError, "缺少 WAL 修复"):
                probe.main()
        self.qq_window.assert_not_called()
        self.assertFalse(self.folder.exists())

    def test_existing_isolated_wal_also_requires_patched_sqlite(self):
        with db.open_existing(db.DB_PATH) as production:
            self.assertEqual(production.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
        self.folder.mkdir(parents=True)
        isolated = self.folder / "readonly.db"
        db.snapshot(db.DB_PATH, isolated)
        with patch.object(db, "DB_PATH", isolated), db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        with patch.object(db.sqlite3, "sqlite_version_info", (3, 49, 1)):
            with self.assertRaisesRegex(db.sqlite3.OperationalError, "缺少 WAL 修复"):
                probe.main()
        self.qq_window.assert_not_called()
