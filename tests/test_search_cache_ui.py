"""离线验证搜索缓存界面读取和刷新请求。"""
import uuid
from unittest.mock import patch

from inquiry import db, isolation
from test_reliability import IsolatedTest
from ui import server


class SearchCacheUiTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        config = self.path / "settings.toml"
        config.write_text('[inquiry]\ntemplate = "请报价 {part}，数量 {qty}"\n', encoding="utf-8")
        self.enterContext(patch.object(server, "SETTINGS_FILE", config))
        self.client = server.app.test_client()

    def test_model_detail_keeps_cache_source_for_each_task(self):
        older = db.create_task("MODEL-C", 2000)
        latest = db.create_task("MODEL-C", 5000)
        stamp = 1_795_000_000.0
        db.set_search_cache_usage(latest, stamp, 2)

        response = self.client.get("/api/models/detail?part=MODEL-C")
        self.assertEqual(response.status_code, 200)
        detail = response.get_json()
        self.assertEqual(detail["task"]["id"], latest)
        self.assertEqual(detail["task"]["search_cache_pages"], 2)
        self.assertEqual(detail["task"]["search_cache_at"], stamp)
        self.assertEqual(
            [(task["id"], task["search_cache_pages"], task["search_cache_at"]) for task in detail["history"]],
            [(latest, 2, stamp), (older, 0, None)],
        )

    def test_settings_default_and_zero_to_disable_cache(self):
        settings = self.client.get("/api/settings").get_json()
        self.assertEqual(settings["settings"]["filter"]["search_cache_hours"], 4)
        self.assertTrue(any(row[:3] == ["filter", "search_cache_hours", "float"] for row in settings["schema"]))
        for value in ("-1", "nan"):
            with self.subTest(value=value):
                response = self.client.put("/api/settings", json={"filter": {"search_cache_hours": value}})
                self.assertEqual(response.status_code, 400)

        disabled = self.client.put("/api/settings", json={"filter": {"search_cache_hours": "0"}})
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(server.load_settings()["filter"]["search_cache_hours"], 0)
        self.assertEqual(self.client.get("/api/settings").get_json()["settings"]["filter"]["search_cache_hours"], 0)

    def test_local_preview_run_requires_boolean_refresh_and_passes_flag_to_cli(self):
        request = {"request_id": uuid.uuid4().hex, "parts": [{"part": "MODEL-C", "qty": 5000}], "dry_run": True}
        with patch.object(isolation, "ENABLED", False), \
             patch.object(server, "run_state", return_value={"running": False}), \
             patch.object(server, "spawn") as spawn:
            invalid = self.client.post("/api/run", json={**request, "refresh_search": "true"})
            self.assertEqual(invalid.status_code, 400)
            spawn.assert_not_called()
            response = self.client.post("/api/run", json={**request, "refresh_search": True})
            self.assertEqual(response.status_code, 200)
            cli_args, mode, _ = spawn.call_args.args
            self.assertIn("--refresh-search", cli_args)
            self.assertIn("--dry-run", cli_args)
            self.assertEqual(mode, "dry-run")
            default = self.client.post("/api/run", json=request)
            self.assertEqual(default.status_code, 200)
            self.assertNotIn("--refresh-search", spawn.call_args.args[0])
