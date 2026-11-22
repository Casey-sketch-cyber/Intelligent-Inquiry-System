"""双机页面浏览器验收：模拟连接/业务响应，不启动 QQ 或跨机操作。"""
import base64
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
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright, expect


def verify_manual_website_panel(page, connection, image_fixture):
    event = "4" * 32
    path = "/api/lan/website-alert"
    frames, actions = [], []
    capture_failure, input_status = False, 200
    screenshot = base64.b64encode(image_fixture.read_bytes()).decode()

    def frame(route):
        nonlocal capture_failure
        assert route.request.method == "GET"
        assert route.request.url.endswith(f"?event={event}")
        token = f"{len(frames) + 16:032x}"
        frames.append(token)
        if capture_failure:
            capture_failure = False
            route.fulfill(status=503, json={"error": "模拟截图暂不可用"})
        else:
            route.fulfill(json={"frame_token": token, "image": screenshot})

    def operate(route):
        assert route.request.method == "POST"
        payload = route.request.post_data_json
        assert payload["event"] == event and payload["frame_token"] == frames[-1]
        actions.append(payload)
        route.fulfill(status=input_status, json={"ok": input_status == 200})

    page.route(f"**{path}/frame?event=*", frame)
    page.route(f"**{path}/input", operate)
    connection["website_alert"] = {"id": event, "kind": "verification", "image_ready": False}
    page.evaluate("refresh()")
    opener = page.locator("#openWebsiteManual")
    expect(opener).to_be_hidden()
    connection["manual_website_support"] = True
    page.evaluate("refresh()")
    expect(opener).to_be_visible()
    panel = page.locator("#websiteManual")
    image = page.locator("#websiteManualImage")
    status = page.locator("#websiteManualStatus")
    field = page.locator("#websiteManualText")
    send = page.locator("#sendWebsiteManualText")
    opener.evaluate("button => button.click()")
    expect(panel).to_be_hidden()
    assert not frames
    opener.click()
    expect(panel).to_be_visible()
    expect(image).to_have_attribute("aria-disabled", "false")
    expect(field).to_have_attribute("type", "password")
    image.evaluate("""image => image.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true}))""")
    assert not actions

    image.click(position={"x": 100, "y": 60})
    expect(image).to_have_attribute("aria-disabled", "false")
    click = actions[-1]["action"]
    assert click["kind"] == "click"
    assert abs(click["x"] - 100 / 400) < 0.005 and abs(click["y"] - 60 / 190) < 0.005

    before = len(frames)
    box = image.bounding_box()
    page.mouse.move(box["x"] + 35, box["y"] + 50)
    page.mouse.down()
    page.mouse.move(box["x"] + 110, box["y"] + 56)
    page.wait_for_timeout(2700)
    assert len(frames) == before
    page.mouse.move(box["x"] + 135, box["y"] + 61)
    page.mouse.up()
    expect(image).to_have_attribute("aria-disabled", "false")
    drag = actions[-1]["action"]
    assert drag["kind"] == "drag" and len(drag["points"]) == 3
    assert [round(point["x"] * 400) for point in drag["points"]] == [35, 110, 135]

    image.press("ArrowDown")
    expect(image).to_have_attribute("aria-disabled", "false")
    assert actions[-1]["action"] == {"kind": "key", "key": "ArrowDown"}
    image.press("Control+A")
    assert len(actions) == 3

    field.fill("仅供离线测试的文字")
    send.evaluate("button => button.click()")
    assert len(actions) == 3
    capture_failure, input_status = True, 409
    send.click()
    expect(status).to_contain_text("画面暂时不可用")
    expect(field).to_have_value("仅供离线测试的文字")
    expect(send).to_be_disabled()
    expect(image).to_have_attribute("aria-disabled", "false", timeout=9000)
    expect(status).to_have_text("画面已更新，请核对后再操作。")
    assert len(actions) == 4
    assert actions[-1]["action"] == {"kind": "text", "text": "仅供离线测试的文字"}
    input_status = 200
    send.click()
    expect(field).to_have_value("")
    assert actions[-1]["frame_token"] != actions[-2]["frame_token"]

    page.locator("#closeWebsiteManual").click()
    expect(panel).to_be_hidden()
    assert image.get_attribute("src") is None
    opener.click()
    expect(image).to_have_attribute("aria-disabled", "false")
    field.fill("关闭时清空")
    connection["website_alert"] = {"id": "5" * 32, "kind": "login", "image_ready": False}
    page.evaluate("refresh()")
    expect(panel).to_be_hidden()
    expect(field).to_have_value("")
    assert image.get_attribute("src") is None
    connection["website_alert"] = None
    page.evaluate("refresh()")


def verify_website_webhook_settings(page):
    page.unroute("**/workbench")
    page.route("**/api/overview", lambda route: route.fulfill(json={
        "env": {key: {"ok": True, "text": "模拟"} for key in ("qq", "browser", "deepseek")},
        "run": {"running": False}, "runtime": {"dedicated_worker": True}, "collection": {},
    }))
    def regular_settings(route):
        assert route.request.method == "GET"
        route.fulfill(json={"settings": {"inquiry": {"template": "{part} {qty}"}}, "schema": []})

    page.route("**/api/settings", regular_settings)
    configured, writes = False, []

    def webhook_settings(route):
        nonlocal configured
        if route.request.method == "PUT":
            changes = route.request.post_data_json
            assert list(changes) == ["webhook_url"]
            writes.append(changes["webhook_url"])
            configured = bool(changes["webhook_url"])
        else:
            assert route.request.method == "GET"
        route.fulfill(json={"configured": configured})

    path = "/api/lan/website-alert/settings"
    page.route(f"**{path}", webhook_settings)
    page.locator("#workbench").evaluate("frame => frame.src='/workbench?fixture=settings#/settings'")
    frame = page.frame_locator("#workbench")
    field = frame.locator("#websiteWebhook")
    status = frame.locator("#webhookStatus")
    save = frame.locator("#saveWebsiteWebhook")
    remove = frame.locator("#removeWebsiteWebhook")
    expect(status).to_have_text("未配置")
    expect(field).to_have_attribute("type", "password")
    expect(field).to_have_value("")
    expect(save).to_be_disabled()
    expect(remove).to_be_hidden()
    fake_url = "https://example.invalid/fixture-webhook"
    field.fill(fake_url)
    save.click()
    expect(status).to_have_text("已配置")
    expect(field).to_have_value("")
    expect(remove).to_be_visible()
    with page.expect_response(lambda r: r.url.endswith(path) and r.request.method == "GET"):
        page.locator("#workbench").evaluate("frame => frame.src='/workbench?fixture=settings-reload#/settings'")
    expect(status).to_have_text("已配置")
    expect(field).to_have_value("")
    expect(save).to_be_disabled()
    remove.click()
    expect(status).to_have_text("未配置")
    expect(remove).to_be_hidden()
    assert writes == [fake_url, ""]


def main():
    output = runtime.DATA_DIR / "qa/lan-ui"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime.TEMP_DIR, prefix="lan-ui-") as directory:
        folder = Path(directory)
        image_fixture = folder / "website-alert.jpg"
        with Image.new("RGB", (400, 190), "#eff3fb") as image:
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((36, 24, 364, 166), radius=12, fill="#ffffff", outline="#d1daeb")
            draw.rectangle((62, 52, 222, 66), fill="#32516f")
            draw.rounded_rectangle((62, 86, 338, 132), radius=8, fill="#e7dcfb")
            image.save(image_fixture)
        connection = {"online": False, "paired": False, "text": "等待导入 QQ 工作机的连接文件"}
        local = {"name": "测试 QQ 工作机", "manual": True, "manual_ready": True,
                 "connection_file": r"D:\TianxinWorker\data\lan\办公电脑连接.json"}
        dispatch = Mock(return_value=(200, {"Content-Type": "application/json"}, b'{"ok":true}'))
        with patch.object(controller, "remote", side_effect=AssertionError("离线测试禁止连接真实工作机")), \
                patch.object(worker, "state", side_effect=lambda _: local.copy()), \
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
                    page.add_init_script("""
                      window.deviceAlerts = {notifications: [], closed: 0, sounds: 0, requests: 0};
                      window.Notification = class {
                        static permission = "default";
                        static async requestPermission() {
                          window.deviceAlerts.requests++;
                          this.permission = "granted";
                          return this.permission;
                        }
                        constructor(title, opts) { window.deviceAlerts.notifications.push({title, ...opts}); }
                        close() { window.deviceAlerts.closed++; }
                      };
                      window.AudioContext = class {
                        state = "suspended";
                        currentTime = 0;
                        destination = {};
                        async resume() { this.state = "running"; }
                        createOscillator() {
                          return {frequency: {}, connect(){}, start(){window.deviceAlerts.sounds++}, stop(){}};
                        }
                        createGain() { return {gain:{setValueAtTime(){}, exponentialRampToValueAtTime(){}}, connect(){}}; }
                      };
                    """)
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
                    image_requests = []

                    def screenshot(route):
                        image_requests.append(route.request.url.split("?", 1)[1])
                        route.fulfill(content_type="image/jpeg", path=image_fixture)

                    page.route("**/api/lan/website-alert/image?event=*", screenshot)
                    page.goto(f"http://127.0.0.1:{servers[0].port}/")
                    expect(page.locator("#workbench")).to_be_hidden()
                    expect(page.locator("#websiteAlert")).to_be_hidden()
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

                    # 只显示已配对状态里的正式事件，图片准备好后才读取。
                    event = {"id": "1" * 32, "kind": "verification", "image_ready": False}
                    connection["website_alert"] = event
                    page.evaluate("refresh()")
                    expect(page.locator("#websiteAlert")).to_be_visible()
                    expect(page.locator("#websiteAlertTitle")).to_have_text("IC交易网需要人工校验")
                    expect(page.locator("#websiteAlertInstructions")).to_contain_text("第二台电脑")
                    expect(page.locator("#websiteAlertImage")).to_be_hidden()
                    expect(page.locator("#websiteAlertAnnouncement")).to_contain_text("完成验证码")
                    assert image_requests == []
                    event["image_ready"] = True
                    page.evaluate("refresh()")
                    expect(page.locator("#websiteAlertImage")).to_be_visible()
                    assert page.locator("#websiteAlertImage").evaluate("img => img.naturalWidth") == 400
                    assert image_requests == [f"event={event['id']}"]
                    page.screenshot(path=str(output / "office-website-alert.png"), full_page=True)
                    assert page.evaluate("deviceAlerts.requests") == 0
                    page.get_by_role("button", name="启用桌面提醒和提示音").click()
                    expect(page.locator("#enableWebsiteAlerts")).to_be_hidden()
                    page.evaluate("refresh()")
                    assert page.evaluate("deviceAlerts.notifications.length") == 1
                    assert page.evaluate("deviceAlerts.sounds") == 1
                    assert image_requests == [f"event={event['id']}"]

                    connection["online"] = False
                    page.evaluate("refresh()")
                    expect(page.locator("#websiteAlert")).to_be_hidden()
                    assert page.locator("#websiteAlertImage").get_attribute("src") is None
                    assert page.evaluate("deviceAlerts.closed") == 1
                    connection["online"] = True
                    page.evaluate("refresh()")
                    expect(page.locator("#websiteAlertImage")).to_be_visible()
                    expect(page.locator("#websiteAlertAnnouncement")).to_have_text("")
                    assert page.evaluate("deviceAlerts.notifications.length") == 1
                    assert page.evaluate("deviceAlerts.sounds") == 1

                    connection["website_alert"] = {"id": "2" * 32, "kind": "login", "image_ready": False}
                    page.evaluate("refresh()")
                    expect(page.locator("#websiteAlertTitle")).to_have_text("IC交易网需要登录")
                    assert page.evaluate("deviceAlerts.notifications.length") == 2
                    assert page.evaluate("deviceAlerts.sounds") == 2
                    connection["website_alert"] = None
                    page.evaluate("refresh()")
                    expect(page.locator("#websiteAlert")).to_be_hidden()
                    assert page.evaluate("deviceAlerts.closed") == 2
                    count = len(image_requests)
                    connection["website_alert"] = {"id": "../not-an-event", "kind": "login", "image_ready": True}
                    page.evaluate("refresh()")
                    expect(page.locator("#websiteAlert")).to_be_hidden()
                    assert len(image_requests) == count
                    connection["website_alert"] = None
                    assert controls == ["resume", "pause"]

                    verify_manual_website_panel(page, connection, image_fixture)
                    verify_website_webhook_settings(page)

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
                               "已配对离线不显示安装引导", "慢检查不重叠/错误详情", "工作机本地登录入口",
                               "登录/验证码提醒和截图", "设备提醒去重", "断线/人工处理完成后清除提醒",
                               "群提醒地址不回显，保存和移除", "手动处理的画面锁定、坐标、键盘与重试边界"],
                    "physical_second_pc_tested": False, "qq_operations": 0}, ensure_ascii=False, indent=2), encoding="utf-8")
                print("LAN panels UI smoke passed; no QQ actions or physical second-PC operations.")
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    main()
