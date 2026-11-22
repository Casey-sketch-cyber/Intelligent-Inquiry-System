"""离线浏览器验收：D 盘隔离数据库，禁用真实 QQ、子进程和模型。"""
import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry import db, incidents, llm, qq, runtime, workbench
from ui import server, service
from playwright.sync_api import expect, sync_playwright


def verify_selfcheck_status(browser, base, output):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    pending_job, login_job = "a" * 32, "b" * 32
    overview = {
        "today": "2026-11-22",
        "run": {"running": False, "stopping": False},
        "env": {key: {"ok": True, "text": "模拟"} for key in ("qq", "browser", "deepseek", "card")},
        "runtime": {"dedicated_worker": True, "allow_send": True},
        "stats": {"sent_today": 0, "replied_today": 0, "valid_today": 0},
        "tasks": [{"id": 42, "part_no": "TEST-QQ", "qty": 5000, "status": "waiting_selfcheck",
                   "status_cn": "等待 QQ 自检", "created_at": 1_795_000_000, "target": 5,
                   "valid": 0, "sent": 0, "replied": 0}],
        "jobs": [{"id": pending_job, "task_ids": [42], "status": "waiting_selfcheck", "dry_run": False},
                 {"id": login_job, "task_ids": [43], "status": "waiting_member_login", "dry_run": True}],
    }
    errors, submission, cancelled = [], [], []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.route("**/api/overview", lambda route: route.fulfill(json=overview))
    page.route("**/api/settings", lambda route: route.fulfill(json={
        "settings": {"inquiry": {"template": "{part} {qty}"}}, "schema": [],
    }))

    def reject_run(route):
        submission.append(route.request.post_data_json)
        route.fulfill(status=400, json={"error": "离线测试不接受新任务"})

    def cancel(route):
        cancelled.append(route.request.url)
        overview["jobs"][0]["status"] = "stopped"
        route.fulfill(json={"ok": True})

    page.route("**/api/run", reject_run)
    page.route("**/api/jobs/*/cancel", cancel)
    try:
        page.goto(base + "#/")
        expect(page.get_by_role("heading", name="执行批次")).to_be_visible()
        notice = page.locator("#selfcheckStatus")
        expect(notice).to_be_hidden()
        overview["selfcheck"] = None
        page.evaluate("refreshOverview()")
        expect(notice).to_have_text("等待 QQ 控件自检")
        overview["selfcheck"] = {
            "ok": False, "missing": ["editor", "send_button"], "qq_version": "9.9.1.0",
            "edge_version": "140.0.0.0", "text": "QQ 控件未就绪", "checked_at": 1_795_000_000,
        }
        page.evaluate("refreshOverview()")
        expect(notice).to_contain_text("QQ 版本 9.9.1.0 · Edge 版本 140.0.0.0")
        expect(notice).to_contain_text("缺少 输入框、发送按钮")
        expect(page.locator(".task-row .chip.amber")).to_have_text("等待 QQ 自检")
        expect(page.get_by_text("任务 #43 · 等待网站登录")).to_be_visible()
        expect(page.locator(f'[data-cancel-job="{login_job}"]')).to_be_visible()
        page.screenshot(path=str(output / "qq-selfcheck.png"), full_page=True, animations="disabled")
        page.locator(f'[data-cancel-job="{pending_job}"]').click()
        expect(page.get_by_text("任务 #42 · 已停止")).to_be_visible()
        assert len(cancelled) == 1 and cancelled[0].endswith(f"/api/jobs/{pending_job}/cancel")
        page.get_by_role("link", name="新建询价").first.click()
        expect(page.locator("#checklist")).to_contain_text("缺少 输入框、发送按钮")
        expect(page.locator("#dryRun")).not_to_be_checked()
        expect(page.locator("#btnStart")).to_be_disabled()
        page.locator("#dryRun").locator("..").click()
        expect(page.locator("#btnStart")).to_be_enabled()
        overview["selfcheck"] = None
        page.evaluate("refreshOverview()")
        expect(page.locator("#btnStart")).to_be_enabled()
        page.locator("#dryRun").locator("..").click()
        expect(page.locator("#btnStart")).to_be_disabled()
        overview["selfcheck"] = {"ok": True, "missing": [], "text": "QQ 自检通过"}
        page.evaluate("refreshOverview()")
        expect(notice).to_contain_text("QQ 自检通过")
        expect(page.locator("#btnStart")).to_be_enabled()
        assert not submission and not errors, (submission, errors)
    finally:
        page.close()


def main():
    output = runtime.DATA_DIR / "qa" / "20261119-runtime-incidents"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime.TEMP_DIR, prefix="ui-smoke-") as folder:
        folder = Path(folder)
        settings = folder / "settings.toml"
        settings.write_text((runtime.ROOT / "settings.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
        with patch.object(db, "DB_PATH", folder / "test.db"), patch.object(server, "SETTINGS_FILE", settings), \
             patch.object(server, "STOP_FILE", folder / "stop.flag"), patch.object(server, "LOG_DIR", folder / "logs"), \
             patch.object(server, "spawn", Mock()) as spawn, patch.object(server, "start_reply_worker", Mock()), \
             patch.object(server, "run_state", return_value={"running": False, "stopping": False}), \
             patch.object(server, "env_status", return_value={
                 "qq": {"ok": True, "text": "模拟会话"}, "browser": {"ok": True, "text": "已就绪"},
                 "deepseek": {"ok": True, "text": "模拟解析"}, "card": {"ok": False, "text": "未启用"}}), \
             patch.object(llm, "_client", side_effect=AssertionError("禁止真实模型调用")), \
             patch.object(qq, "send_text", side_effect=AssertionError("禁止真实发送")), \
             patch.object(incidents, "_versions", return_value={"qq_version": "9.9.1.0", "edge_version": "140.0.0.0"}), \
             patch.object(server.analysis, "start", Mock()):
            t1 = db.create_task("STM32F405RGT6", 5000)
            t2 = db.create_task("STM32F405RGT6", 3000)
            db.set_search_cache_usage(t2, 1_795_000_000.0, 2)
            db.create_task("GD32F405RGT6", 2000)
            sid = db.add_supplier(t2, {"company": "示例电子有限公司", "qqs": [{"uin": "fixture-contact"}], "qty": "9600", "batch": "26+"})
            db.update_supplier(sid, status="asking", sent_at=100, chat_name="示例电子/采购对接", last_reply_at=200)
            db.add_message(sid, "out", "您好，料号：STM32F405RGT6，数量：3000pcs，请报含税单价。")
            db.replace_incoming(sid, ["接受多少呢？"], [{"id": "reply1", "text": "接受多少呢？"}])
            workbench.enqueue_analysis(sid)
            with db.connect() as c:
                c.execute("UPDATE analysis_job SET status='done',suggestions=? WHERE supplier_id=?",
                          (json.dumps(["暂无目标价，请报实价支持谈单。", "请按 3000pcs 报含税单价，谢谢。", "请先确认现货数量和批次。"], ensure_ascii=False), sid))
            workbench.save_unassigned("示例电子/采购对接", {"id": "ambiguous", "text": "整包出", "time": "12:00"}, "多个型号")
            incidents.record("captcha", "search", severity="warning")
            httpd = service.bind_server(server.app, 0)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path=str(next(p for p in server.browser.EDGE_CANDIDATES if p.exists())),
                                                 headless=True, args=[f"--disk-cache-dir={folder / 'cache'}", f"--crash-dumps-dir={folder / 'crashes'}"])
                    page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                    errors = []
                    requests = []
                    page.on("pageerror", lambda err: errors.append(str(err)))
                    page.on("request", lambda request: requests.append(request))
                    base = f"http://127.0.0.1:{httpd.port}/"
                    page.goto(base + "#/tasks")
                    page.locator("#modelsTable tbody tr").first.wait_for()
                    assert page.locator("#modelsTable tbody tr").count() == 2
                    page.locator("#modelSearch").fill("STM32")
                    page.wait_for_timeout(400)
                    assert page.locator("#modelsTable tbody tr").count() == 1
                    page.screenshot(path=str(output / "models.png"), full_page=True)
                    page.locator("#modelsTable tbody tr").click()
                    cache_summary = page.locator("#searchCacheSummary")
                    cache_summary.wait_for()
                    assert "使用 2 页搜索缓存 · 最早采集于" in cache_summary.inner_text()
                    assert "搜索缓存" not in page.locator(f'a[href="#/tasks/{t1}"]').inner_text()
                    page.locator("[data-conversation]").click()
                    page.locator("[data-suggestion='0']").click()
                    assert "暂无目标价" in page.locator("#replyText").input_value()
                    page.locator("#replyText").fill("这是一条模拟回复，不真实发送。")
                    assert "不自动添加型号" in page.locator("body").inner_text()
                    page.wait_for_timeout(4300)
                    assert "模拟回复" in page.locator("#replyText").input_value()
                    page.screenshot(path=str(output / "conversation.png"), full_page=True)
                    page.emulate_media(color_scheme="dark")
                    page.screenshot(path=str(output / "conversation-dark.png"), full_page=True)
                    page.emulate_media(color_scheme="light")
                    with page.expect_response(lambda res: res.url.endswith(f"/api/suppliers/{sid}/reply")
                                              and res.request.method == "POST") as reply:
                        page.locator("#sendReply").click()
                    assert reply.value.ok, reply.value.text()
                    assert reply.value.request.post_data_json["suggestion"] == {"digest": workbench.suggestions_for(sid)["digest"], "index": 0}
                    assert len(workbench.outbox_for(sid)) == 1
                    assert workbench.outbox_for(sid)[0]["status"] == "queued"
                    assert workbench.outbox_for(sid)[0]["content"] == "这是一条模拟回复，不真实发送。"
                    reject_path = f"/api/suppliers/{sid}/suggestion-rejection"
                    assert not any(r.url.endswith(reject_path) for r in requests)
                    page.locator("#replyText").fill("保留未发送的手写草稿")
                    with page.expect_response(lambda res: res.url.endswith(reject_path)
                                              and res.request.method == "POST") as reject:
                        page.get_by_role("button", name="不采用建议").click()
                    assert reject.value.ok, reject.value.text()
                    assert reject.value.request.post_data_json == {"digest": workbench.suggestions_for(sid)["digest"]}
                    page.get_by_text("本轮建议已标记为不采用", exact=True).wait_for()
                    assert page.locator("#replyText").input_value() == "保留未发送的手写草稿"
                    assert len(workbench.outbox_for(sid)) == 1
                    page.locator("#closeConversation").click()

                    with page.expect_response(lambda res: res.url.endswith("/api/incidents?days=14")) as issue_response:
                        page.get_by_role("link", name="运行问题", exact=True).click()
                    assert issue_response.value.ok
                    page.get_by_role("heading", name="按类型统计", exact=True).wait_for()
                    expected = ["网站验证码", "1", "编辑建议后提交", "1", "明确不采用建议", "1"]
                    assert page.locator("#incidentTypes tbody td").all_text_contents() == expected
                    daily_captcha = page.locator("#incidentDates tbody tr").filter(has_text="网站验证码")
                    assert daily_captcha.locator("td").all_text_contents()[1:] == ["网站验证码", "1"]
                    assert len(page.locator("#incidentDates tbody tr").all_text_contents()) == 3
                    page.screenshot(path=str(output / "incidents.png"), full_page=True, animations="disabled")

                    page.goto(base + "#/new")
                    page.locator("#btnPaste").click()
                    page.locator("#batchQty").fill("5k")
                    page.locator("#pasteText").fill("\n".join(f"TEST-{i}" for i in range(12)))
                    page.locator("#btnParse").click()
                    assert page.locator("#partsBody tr").count() == 12
                    assert page.locator("#msgPreview .preview").count() == 12
                    assert "TEST-11" in page.locator("#msgPreview").inner_text()
                    assert "5000pcs" in page.locator("#msgPreview").inner_text()
                    assert page.locator("#dryRun").is_checked()
                    refresh = page.get_by_role("button", name="刷新搜索", exact=True)
                    assert refresh.get_attribute("aria-pressed") == "false"
                    refresh.click()
                    assert refresh.get_attribute("aria-pressed") == "true"
                    assert page.locator("#searchRefreshPreview").is_visible()
                    assert not any(r.url.endswith("/api/run") and r.method == "POST" for r in requests)
                    page.screenshot(path=str(output / "batch.png"), full_page=True)
                    with page.expect_response(lambda res: res.url.endswith("/api/run")
                                              and res.request.method == "POST") as refreshed_run:
                        page.locator("#btnStart").click()
                    assert refreshed_run.value.ok, refreshed_run.value.text()
                    assert refreshed_run.value.request.post_data_json["refresh_search"] is True
                    assert refreshed_run.value.request.post_data_json["dry_run"] is True
                    assert "--refresh-search" in spawn.call_args.args[0]
                    page.wait_for_url("**/#/log")
                    page.goto(base + "#/settings")
                    toggle = page.locator('[data-k="inquiry.send_card"]')
                    toggle.wait_for(state="attached")
                    toggle.locator("..").click()
                    page.locator("#btnSave").click()
                    page.wait_for_timeout(250)
                    assert server.load_settings()["inquiry"]["send_card"] is True
                    toggle.locator("..").click()
                    page.locator("#btnSave").click()
                    page.wait_for_timeout(250)
                    assert server.load_settings()["inquiry"]["send_card"] is False
                    assert not page.locator('[data-k="desktop.foreground_assist"]').is_checked()
                    cache_hours = page.locator('[data-k="filter.search_cache_hours"]')
                    assert cache_hours.input_value() == "4"
                    cache_hours.fill("0")
                    with page.expect_response(lambda res: res.url.endswith("/api/settings")
                                              and res.request.method == "PUT") as saved_cache_settings:
                        page.locator("#btnSave").click()
                    assert saved_cache_settings.value.ok
                    assert server.load_settings()["filter"]["search_cache_hours"] == 0
                    page.screenshot(path=str(output / "settings.png"), full_page=True)
                    assert not errors, errors
                    verify_selfcheck_status(browser, base, output)
                    browser.close()
                print("UI smoke passed: model grouping/search, reply suggestions, incidents, batch previews, settings, and QQ selfcheck.")
                print("No real QQ sends or model calls. Screenshots:", output)
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    main()
