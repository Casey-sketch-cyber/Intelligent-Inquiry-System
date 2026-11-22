"""验证供应商链接迁移、保密及发送前的真实核对流程。"""
import json
from threading import Event
from unittest.mock import Mock, patch

from inquiry import db, icnet, isolation, qq, workbench
from inquiry.runner import Runner, Task
from test_reliability import IsolatedTest
from ui import server


class SupplierQQLinkTests(IsolatedTest):
    LINK = "https://wpa.qq.com/msgrd?v=3&uin=12345&site=qq&menu=yes"

    def setUp(self):
        super().setUp()
        self.runner = Runner([])
        self.runner.cfg.pop("direct_launch", None)
        self.runner.context = object()
        self.runner.win = object()
        self.task = Task(db.create_task("MODEL", 100), "MODEL", 100, "询价 MODEL")
        self.row = {"company": "示例芯微电子", "qqs": [
            {"uin": "12345", "inthref": self.LINK, "href": "#contact", "title": "示例芯微"},
            {"uin": "98765", "inthref": "https://wpa.qq.com/msgrd?uin=98765"},
        ]}

    def worker(self):
        return patch.multiple(isolation, ENABLED=True, MODE="worker", MANUAL=Event(), DRAINING=Event())

    def test_raw_link_belongs_to_first_contact_and_stays_private(self):
        sid = db.add_supplier(self.task.id, self.row)
        self.assertEqual(db.saved_qq_link(self.task.id, self.row["company"], "12345"),
                         {"uin": "12345", "inthref": self.LINK, "href": "#contact"})
        self.assertIsNone(db.saved_qq_link(self.task.id, self.row["company"], "98765"))
        self.assertIsNone(db.saved_qq_link(self.task.id, "另一家公司", "12345"))
        self.assertIsNone(db.add_supplier(self.task.id, {**self.row, "qqs": list(reversed(self.row["qqs"]))}))
        for record in (workbench.conversation(sid), db.task_quotes(self.task.id)[0]):
            self.assertNotIn("qq_launch_json", record)
            self.assertNotIn(self.LINK, json.dumps(record))
        self.assertEqual(db.task_quotes(self.task.id)[0]["row"]["qqs"][0]["title"], "示例芯微")

    def test_previous_database_keeps_supplier_rows(self):
        old = {"company": "旧供应商", "qqs": [{"uin": "12345", "title": "旧联系人"}]}
        sid = db.add_supplier(self.task.id, old)
        with db.connect() as conn:
            conn.execute("ALTER TABLE supplier DROP COLUMN qq_launch_json")
            conn.execute("PRAGMA user_version=5")
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertEqual(db.task_quotes(self.task.id)[0]["row"], old)
        self.assertEqual(db.supplier(sid)["qq_uin"], "12345")
        self.assertIsNone(db.saved_qq_link(self.task.id, old["company"], "12345"))

    def test_default_off_uses_existing_lookup(self):
        db.add_supplier(self.task.id, self.row)
        with self.worker(), patch.object(icnet, "find_visible_row") as visible, \
             patch.object(icnet, "find_row", return_value=(object(), self.row)) as locate, \
             patch.object(icnet, "launch_saved_qq") as direct, \
             patch.object(self.runner, "_await_qq_launch", return_value="示例芯微"):
            self.assertEqual(self.runner.open_chat(self.task, self.row), "示例芯微")
        visible.assert_not_called()
        direct.assert_not_called()
        locate.assert_called_once()

    def test_workbench_settings_save_preserves_file_only_switch(self):
        path = self.path / "settings.toml"
        config = '[inquiry]\ntemplate = "请报 {part}，数量 {qty}"\n'
        path.write_text(config + "direct_launch = true\n", encoding="utf-8")
        with patch.object(server, "SETTINGS_FILE", path), server.app.test_client() as client:
            update = client.put("/api/settings", json={"inquiry": {"send_card": False, "direct_launch": False}})
            self.assertEqual(update.status_code, 200)
            self.assertIs(server.load_settings()["inquiry"]["direct_launch"], True)
            self.assertNotIn("direct_launch", [entry[1] for entry in client.get("/api/settings").json["schema"]])
            path.write_text(config + 'direct_launch = "false"\n', encoding="utf-8")
            self.assertEqual(client.put("/api/settings", json={"inquiry": {"send_card": False}}).status_code, 200)
            self.assertNotIn("direct_launch", server.load_settings()["inquiry"])

    def test_visible_supplier_is_preferred_when_enabled(self):
        self.runner.cfg["direct_launch"] = True
        db.add_supplier(self.task.id, self.row)
        with self.worker(), patch.object(icnet, "find_visible_row", return_value=(object(), self.row)), \
             patch.object(icnet, "find_row") as locate, patch.object(icnet, "launch_saved_qq") as direct, \
             patch.object(self.runner, "_await_qq_launch", return_value="示例芯微"):
            self.assertEqual(self.runner.open_chat(self.task, self.row), "示例芯微")
        direct.assert_not_called()
        locate.assert_not_called()

    def test_direct_link_requires_same_supplier_before_sending(self):
        self.runner.cfg["direct_launch"] = True
        jump = Mock()
        with self.worker(), patch.object(self.runner, "wait_rate_limit", return_value=True), \
             patch.object(icnet, "find_visible_row", return_value=None), patch.object(icnet, "find_row") as locate, \
             patch.object(icnet, "launch_saved_qq", return_value=jump) as direct, \
             patch.object(icnet, "settle_origin", return_value="https://wpa.qq.com"), \
             patch.object(icnet, "save_ok_origins"), patch("lanhost.support.confirm_qq_dialog"), \
             patch.object(qq, "current_chat_name", side_effect=["无关旧会话", "其他公司", "其他公司"]), \
             patch.object(qq, "wait_chat_change", return_value="其他公司") as wait, \
             patch.object(qq, "profile_company", return_value="其他公司电子"), \
             patch.object(qq, "send_text") as send, patch.object(qq, "send_image") as image:
            self.assertFalse(self.runner.inquire(self.task, self.row))
        direct.assert_called_once()
        wait.assert_called_once()
        locate.assert_not_called()
        send.assert_not_called()
        image.assert_not_called()
        supplier = db.suppliers(self.task.id)[0]
        self.assertEqual(supplier["status"], "identity_unverified")
        self.assertIsNone(supplier["sent_at"])

    def test_unchanged_chat_after_failed_direct_launch_falls_back_once(self):
        self.runner.cfg["direct_launch"] = True
        db.add_supplier(self.task.id, self.row)
        legacy_tab = Mock()
        with self.worker(), patch.object(icnet, "find_visible_row", return_value=None), \
             patch.object(icnet, "find_row", return_value=(object(), self.row)) as locate, \
             patch.object(icnet, "launch_saved_qq", side_effect=icnet.QQLaunchFailed(self.LINK)) as direct, \
             patch.object(icnet, "launch_qq", return_value=legacy_tab) as legacy, \
             patch.object(icnet, "settle_origin", return_value="https://admin.qidian.qq.com"), \
             patch.object(icnet, "save_ok_origins"), patch("lanhost.support.confirm_qq_dialog"), \
             patch.object(qq, "current_chat_name", return_value="旧会话"), \
             patch.object(qq, "wait_chat_change", return_value="示例芯微"):
            self.assertEqual(self.runner.open_chat(self.task, self.row), "示例芯微")
        direct.assert_called_once()
        locate.assert_called_once()
        legacy.assert_called_once()
        self.assertNotIn(self.LINK, str(self.log.call_args_list))
