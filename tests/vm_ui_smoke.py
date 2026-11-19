"""宿主控制台浏览器验收，模拟 VM 状态，不启动 VM 或操作真实 QQ。"""
import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry import runtime, browser
from ui import service
from vmhost import host, manager
from playwright.sync_api import sync_playwright, expect


def main():
    output = runtime.DATA_DIR / "qa" / "vm-20261020"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime.TEMP_DIR, prefix="vm-ui-") as directory:
        folder = Path(directory)
        with patch.object(manager, "start", side_effect=RuntimeError("尚未安装 Windows，请先完成镜像下载")) as start:
            httpd = service.bind_server(host.app, 0)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as pw:
                    b = pw.chromium.launch(executable_path=str(next(p for p in browser.EDGE_CANDIDATES if p.exists())),
                        headless=True, args=[f"--disk-cache-dir={folder / 'cache'}", f"--crash-dumps-dir={folder / 'crashes'}"])
                    page = b.new_page(viewport={"width":1440,"height":1000})
                    errors = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    vm = {"state":"poweroff","text":"虚拟机已建好，等待 Windows ISO；尚未安装系统。"}
                    connection = {"online":False}
                    page.route("**/api/vm/status", lambda route: route.fulfill(json={"vm":vm,"connection":connection}))
                    base = f"http://127.0.0.1:{httpd.port}"
                    page.goto(base)
                    expect(page.locator("#status")).to_contain_text("已关机")
                    expect(page.locator("#workbench")).to_be_hidden()
                    page.locator("[data-action=start]").click()
                    expect(page.locator("#feedback")).to_contain_text("尚未安装 Windows")
                    page.screenshot(path=str(output / "awaiting-windows.png"), full_page=True)
                    assert start.call_count == 1
                    # Simulate service connection; iframe can load workbench but no guest API is invoked.
                    page.route("**/workbench", lambda route: route.fulfill(content_type="text/html", body="<h1>模拟询价工作台</h1>"))
                    connection.update(online=True, allow_send=False, shutdown_ready=False)
                    vm.update(state="running")
                    page.evaluate("refresh()")
                    expect(page.locator("#workbench")).to_be_visible()
                    expect(page.locator("#note")).to_contain_text("真实询价和回复发送均已锁定")
                    page.screenshot(path=str(output / "readonly-connected.png"), full_page=True)
                    connection["online"] = False
                    page.evaluate("refresh()")
                    expect(page.locator("#workbench")).to_be_hidden()
                    assert not errors, errors
                    b.close()
                (output / "ui-result.json").write_text(json.dumps({
                    "passed":True,"checks":["关机/等待镜像","错误持久显示","只读连接","断线隐藏工作台"],
                    "real_vm_started":False,"qq_operations":0}, ensure_ascii=False, indent=2), encoding="utf-8")
                print("VM panel UI smoke passed; no real VM start or QQ actions.")
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    main()
