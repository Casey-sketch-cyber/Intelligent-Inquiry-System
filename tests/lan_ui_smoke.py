"""双机页面浏览器验收：模拟连接/业务响应，不启动 QQ 或跨机操作。"""
import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry import runtime, browser
from ui import service
from lanhost import controller, worker
from playwright.sync_api import sync_playwright, expect


def main():
    output = runtime.DATA_DIR / "qa/lan-connection-20261023"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime.TEMP_DIR, prefix="lan-ui-") as directory:
        folder = Path(directory)
        connection = {"online": False, "paired": False, "text": "等待导入 QQ 工作机的连接文件"}
        local = {"name": "测试 QQ 工作机", "manual": True, "manual_ready": True,
                 "connection_file": r"D:\TianxinWorker\data\lan\办公电脑连接.json"}
        dispatch = Mock(return_value=(200, {"Content-Type": "application/json"}, b'{"ok":true}'))
        with patch.object(worker, "state", side_effect=lambda _: local.copy()), \
                patch.object(worker, "handler", return_value=dispatch):
            servers = [service.bind_server(controller.app, 0),
                       service.bind_server(worker.local_app({}, folder / "connection.json", 8778), 0)]
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    b = pw.chromium.launch(executable_path=str(next(p for p in browser.EDGE_CANDIDATES if p.exists())),
                        headless=True, args=[f"--disk-cache-dir={folder / 'cache'}", f"--crash-dumps-dir={folder / 'crashes'}"])
                    page = b.new_page(viewport={"width": 1440, "height": 1050})
                    errors = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.route("**/api/lan/status", lambda route: route.fulfill(json=connection))
                    page.route("**/workbench", lambda route: route.fulfill(content_type="text/html", body="<h1>模拟型号工作台</h1><input id='draft'>"))
                    imports, controls = [], []

                    def import_file(route):
                        imports.append(route.request.post_data_json)
                        connection.update(online=True, paired=True, name="测试 QQ 工作机", manual=True, manual_ready=True)
                        route.fulfill(json={"ok": True})

                    page.route("**/api/lan/import", import_file)

                    def control(route):
                        name = route.request.url.rsplit("/", 1)[-1]
                        controls.append(name)
                        connection.update(manual=name == "pause", manual_ready=name == "pause")
                        route.fulfill(json={"ok": True})

                    for name in ("pause", "resume"):
                        page.route("**/api/lan/" + name, control)
                    page.goto(f"http://127.0.0.1:{servers[0].port}/")
                    expect(page.locator("#workbench")).to_be_hidden()
                    expect(page.locator("#resume")).to_be_disabled()
                    page.locator("#import").click()
                    expect(page.locator("#feedback")).to_contain_text("请选择有效")
                    page.screenshot(path=str(output / "office-awaiting.png"), full_page=True)
                    upload = folder / "fixture.json"
                    upload.write_text('{"fixture":true}', encoding="utf-8")
                    page.locator("#connection").set_input_files(str(upload))
                    page.locator("#import").click()
                    expect(page.locator("#workbench")).to_be_visible()
                    expect(page.locator("#note")).to_contain_text("自动操作已暂停")
                    expect(page.locator("#resume")).to_be_enabled()
                    assert imports == [{"fixture": True}]
                    page.locator("#resume").click()
                    expect(page.locator("#note")).to_contain_text("新建询价")
                    page.locator("#pause").click()
                    expect(page.locator("#note")).to_contain_text("自动操作已暂停")
                    assert controls == ["resume", "pause"]
                    page.screenshot(path=str(output / "office-connected.png"), full_page=True)
                    connection.update(manual_ready=False)
                    page.evaluate("refresh()")
                    expect(page.locator("#resume")).to_be_disabled()
                    expect(page.locator("#note")).to_contain_text("正在停止")
                    connection.update(trial_running=True,manual=False)
                    page.evaluate("refresh()")
                    expect(page.locator("#sendMode")).to_contain_text("授权真实测试中")
                    expect(page.locator("#note")).to_contain_text("第二台正在执行")
                    expect(page.locator("#resume")).to_be_disabled()
                    connection["trial_running"] = False
                    connection["allow_send"] = True
                    page.evaluate("refresh()")
                    frame = page.frame_locator("#workbench")
                    frame.locator("#draft").fill("尚未提交的草稿")
                    page.frames[1].evaluate("location.hash='#/models/TEST'")
                    current_url = page.frames[1].url
                    connection["online"] = False
                    connection["error"] = "工作机状态检查超时"
                    page.evaluate("refresh()")
                    expect(page.locator("#workbench")).to_be_visible()
                    expect(page.locator("#welcome")).to_be_hidden()
                    expect(page.locator("#connectionProblem")).to_be_visible()
                    expect(page.locator("#connectionDetail")).to_contain_text("超时")
                    expect(page.locator("#sendMode")).to_contain_text("连接待恢复")
                    expect(page.locator("#collectReplies")).to_be_disabled()
                    expect(page.locator("#pause")).to_be_disabled()
                    assert page.locator("#workbench").evaluate("node=>node.inert")
                    assert page.frames[1].url == current_url
                    page.screenshot(path=str(output / "office-reconnecting.png"), full_page=True)
                    connection["online"] = True
                    page.locator("#retryConnection").click()
                    expect(page.locator("#connectionProblem")).to_be_hidden()
                    expect(frame.locator("#draft")).to_have_value("尚未提交的草稿")
                    assert page.frames[1].url == current_url
                    assert not page.locator("#workbench").evaluate("node=>node.inert")
                    assert imports == [{"fixture": True}]
                    assert controls == ["resume", "pause"]

                    # 已配对但启动离线，只显示连接诊断，不误导用户重新安装。
                    connection["online"] = False
                    page.reload()
                    expect(page.locator("#connectionProblem")).to_be_visible()
                    expect(page.locator("#welcome")).to_be_hidden()
                    page.locator("#changeConnection").click()
                    expect(page.locator("#welcomeTitle")).to_contain_text("更新")
                    expect(page.locator("#setupInstructions")).to_be_hidden()
                    expect(page.locator("#deployment")).to_be_hidden()
                    connection["online"] = True
                    page.evaluate("refresh()")
                    expect(page.locator("#welcome")).to_be_hidden()
                    expect(page.locator("#workbench")).to_be_visible()

                    # 一次状态检查未结束时，周期和手动重试不能启动重叠请求。
                    held = []
                    page.route("**/api/lan/status", lambda route: held.append(route))
                    page.evaluate("void refresh(); void refresh(); void refresh()")
                    page.wait_for_timeout(150)
                    assert len(held) == 1, len(held)
                    held.pop().fulfill(status=503, json={"error": "本机连接检查失败"})
                    expect(page.locator("#connectionDetail")).to_contain_text("本机连接检查失败")
                    expect(page.locator("#pause")).to_be_disabled()
                    expect(page.locator("#workbench")).to_be_visible()
                    expect(page.locator("#welcome")).to_be_hidden()
                    page.unroute("**/api/lan/status")
                    page.route("**/api/lan/status", lambda route: route.fulfill(json=connection))
                    page.evaluate("refresh()")
                    expect(page.locator("#connectionProblem")).to_be_hidden()

                    # 工作机页面使用真实本机 Flask 路由；浏览器打开动作被模拟，不接触 QQ。
                    page.goto(f"http://127.0.0.1:{servers[1].port}/")
                    expect(page.locator("#status")).to_contain_text("已暂停")
                    page.locator("#browser").click()
                    expect(page.locator("#feedback")).to_contain_text("已在本工作机打开")
                    dispatch.assert_called_once_with("POST", "/api/browser/open", b"", "")
                    page.screenshot(path=str(output / "worker-panel.png"), full_page=True)
                    local.update(manual_ready=False)
                    page.evaluate("refresh()")
                    expect(page.locator("#status")).to_contain_text("正在停止")
                    expect(page.locator("#browser")).to_be_disabled()
                    # UI 外直接提交也必须等任务释放，不会启动浏览器。
                    reply = page.request.post(f"http://127.0.0.1:{servers[1].port}/api/browser",
                        headers={"Origin": f"http://127.0.0.1:{servers[1].port}"})
                    assert reply.status == 409
                    assert dispatch.call_count == 1
                    assert not errors, errors
                    b.close()
                (output / "ui-result.json").write_text(json.dumps({
                    "passed": True,
                    "checks": ["未配对/无效文件", "导入连接", "工作台只读展示", "暂停/恢复单次提交",
                               "停止中禁止人工登录", "断线保留页面/草稿且禁止操作", "恢复不重载/重放请求",
                               "已配对离线不显示安装引导", "慢检查不重叠/错误详情", "工作机本地登录入口"],
                    "physical_second_pc_tested": False, "qq_operations": 0}, ensure_ascii=False, indent=2), encoding="utf-8")
                print("LAN panels UI smoke passed; no QQ actions or physical second-PC operations.")
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    main()
