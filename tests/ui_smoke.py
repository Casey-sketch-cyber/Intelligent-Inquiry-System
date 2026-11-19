"""离线浏览器验收：D 盘隔离数据库，禁用真实 QQ、子进程和模型。"""
import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry import db, llm, qq, runtime, workbench
from ui import server, service
from playwright.sync_api import sync_playwright


def main():
    output = runtime.DATA_DIR / "qa" / "20261023-reply-body"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime.TEMP_DIR, prefix="ui-smoke-") as folder:
        folder = Path(folder)
        settings = folder / "settings.toml"
        settings.write_text((runtime.ROOT / "settings.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
        with patch.object(db, "DB_PATH", folder / "test.db"), patch.object(server, "SETTINGS_FILE", settings), \
             patch.object(server, "spawn", Mock()), patch.object(server, "start_reply_worker", Mock()), \
             patch.object(server, "run_state", return_value={"running": False, "stopping": False}), \
             patch.object(server, "env_status", return_value={
                 "qq": {"ok": True, "text": "模拟会话"}, "browser": {"ok": True, "text": "已就绪"},
                 "deepseek": {"ok": True, "text": "模拟解析"}, "card": {"ok": False, "text": "未启用"}}), \
             patch.object(llm, "_client", side_effect=AssertionError("禁止真实模型调用")), \
             patch.object(qq, "send_text", side_effect=AssertionError("禁止真实发送")), \
             patch.object(server.analysis, "start", Mock()):
            t1 = db.create_task("STM32F405RGT6", 5000)
            t2 = db.create_task("STM32F405RGT6", 3000)
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
            httpd = service.bind_server(server.app, 0)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path=str(next(p for p in server.browser.EDGE_CANDIDATES if p.exists())),
                                                 headless=True, args=[f"--disk-cache-dir={folder / 'cache'}", f"--crash-dumps-dir={folder / 'crashes'}"])
                    page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                    errors = []
                    page.on("pageerror", lambda err: errors.append(str(err)))
                    base = f"http://127.0.0.1:{httpd.port}/"
                    page.goto(base + "#/tasks")
                    page.locator("#modelsTable tbody tr").first.wait_for()
                    assert page.locator("#modelsTable tbody tr").count() == 2
                    page.locator("#modelSearch").fill("STM32")
                    page.wait_for_timeout(400)
                    assert page.locator("#modelsTable tbody tr").count() == 1
                    page.screenshot(path=str(output / "models.png"), full_page=True)
                    page.locator("#modelsTable tbody tr").click()
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
                    page.locator("#sendReply").click()
                    page.wait_for_timeout(300)
                    assert len(workbench.outbox_for(sid)) == 1
                    assert workbench.outbox_for(sid)[0]["status"] == "queued"
                    assert workbench.outbox_for(sid)[0]["content"] == "这是一条模拟回复，不真实发送。"
                    page.locator("#closeConversation").click()
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
                    page.screenshot(path=str(output / "batch.png"), full_page=True)
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
                    page.screenshot(path=str(output / "settings.png"), full_page=True)
                    assert not errors, errors
                    browser.close()
                print("UI smoke passed: model grouping/search, conversation draft/choices, one queued reply, 12-part batch preview, card toggle.")
                print("No real QQ sends or model calls. Screenshots:", output)
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    main()
