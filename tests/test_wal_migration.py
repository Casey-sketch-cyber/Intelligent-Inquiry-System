"""迁移和历史重置备份应包含尚未写回主文件的 WAL 记录。"""

import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from inquiry import db
from lanhost import package as lan_package
from lanhost import reset_history
from test_reliability import IsolatedTest
from vmhost import package as vm_package


class WalMigrationTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.root = self.path / "worker"
        self.data = self.root / "data"
        self.data.mkdir(parents=True)
        self.source = self.data / "inquiry.db"
        db_path = patch.object(db, "DB_PATH", self.source)
        db_path.start()
        self.addCleanup(db_path.stop)
        db.create_task("CHECKPOINTED", 5000)

        self.writer = sqlite3.connect(self.source)
        self.addCleanup(self.writer.close)
        self.assertEqual(self.writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.assertEqual(self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
        main_file = self.source.read_bytes()
        with self.writer:
            self.writer.execute("INSERT INTO task(part_no,qty,created_at) VALUES('WAL-ONLY',5000,1)")
            self.writer.execute("INSERT INTO search_attempt(created_at,part_no,page_no,source) VALUES(1,'WAL-ONLY',1,'test')")
        self.assertGreater((self.data / "inquiry.db-wal").stat().st_size, 0)
        self.assertEqual(self.source.read_bytes(), main_file)
        (self.root / "settings.toml").write_text("[inquiry]\nsend_card = false\n", encoding="utf-8")

    def assert_standalone(self, path, models):
        self.assertEqual(list(path.parent.glob(path.name + "-*")), [])
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as copy:
            self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual([row[0] for row in copy.execute("SELECT part_no FROM task ORDER BY id")], models)
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM search_attempt").fetchone()[0], 1)
        self.assertEqual(list(path.parent.glob(path.name + "-*")), [])

    def test_lan_migration_zip_has_one_complete_business_database(self):
        for name in ("install.ps1", "install_helpers.ps1", "allow_connection.ps1"):
            path = self.root / "lanhost" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("# 测试替身\n", encoding="utf-8")
        with patch.multiple(lan_package, ROOT=self.root, DATA_DIR=self.data), \
             patch.object(lan_package, "source_archive", side_effect=lambda path: path.write_bytes(b"fixture")), \
             patch.object(lan_package, "copy_materials"), \
             patch.object(lan_package.config, "controller", return_value={"controller_id": "fixture", "office_device": "fixture"}):
            result = lan_package.build(with_data=True)

        extracted = self.path / "zip-database.db"
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertEqual([name for name in archive.namelist() if name.startswith("seed/inquiry.db")],
                             ["seed/inquiry.db"])
            extracted.write_bytes(archive.read("seed/inquiry.db"))
        self.assert_standalone(extracted, ["CHECKPOINTED", "WAL-ONLY"])

    def test_vm_package_can_replace_seed_with_latest_wal_records(self):
        (self.root / "requirements.lock.txt").write_text("", encoding="utf-8")
        app_sources = self.root / "vmhost"
        app_sources.mkdir()
        for name in ("install_guest.ps1", "InstallGuest.cmd"):
            (app_sources / name).write_text("", encoding="utf-8")
        setup = self.data / "vm" / "setup"
        with patch.multiple(vm_package, ROOT=self.root, DATA_DIR=self.data, SETUP=setup):
            result = vm_package.build(with_data=True)
            seed = Path(result["path"]) / "seed" / "inquiry.db"
            self.assert_standalone(seed, ["CHECKPOINTED", "WAL-ONLY"])
            with self.writer:
                self.writer.execute("INSERT INTO task(part_no,qty,created_at) VALUES('APPENDED',5000,2)")
            vm_package.build(with_data=True)
        self.assert_standalone(seed, ["CHECKPOINTED", "WAL-ONLY", "APPENDED"])

    def test_reset_backup_contains_live_wal_and_active_database_keeps_wal_mode(self):
        result = reset_history.clear_history(self.root)
        self.assertEqual(result["state"], "cleared")
        self.assert_standalone(Path(result["backup"]) / "inquiry.db", ["CHECKPOINTED", "WAL-ONLY"])
        self.assertEqual(self.writer.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.writer.execute("SELECT COUNT(*) FROM task").fetchone()[0], 0)
        self.assertEqual(self.writer.execute("SELECT COUNT(*) FROM search_attempt").fetchone()[0], 1)
