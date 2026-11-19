"""宿主工作台：只控制本项目虚拟机，不导入或启动 QQ 自动化执行器。"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry.runtime import ROOT, DATA_DIR
from inquiry import browser
from flask import Flask, jsonify, request, Response, send_from_directory
from ui import service
from vmhost import manager
from vmhost.bridge import Bridge, BridgeError

app = Flask(__name__, static_folder=str(ROOT / "ui" / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
INSTANCE = uuid.uuid4().hex


def bridge():
    cfg = manager.read_config()
    return Bridge(cfg["bridge"], cfg["secret"])


@app.before_request
def local_only():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify(error="只允许本机访问"), 403
    if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
        return jsonify(error="访问地址不受信任"), 403
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("Origin") != request.host_url.rstrip("/"):
            return jsonify(error="请从本机工作台操作"), 403


@app.errorhandler(BridgeError)
@app.errorhandler(RuntimeError)
@app.errorhandler(ValueError)
@app.errorhandler(OSError)
def failed(exc):
    # 不回传密钥、宿主环境变量或完整 traceback。
    return jsonify(error=str(exc)[:500]), 503


@app.get("/api/health")
def health():
    return jsonify(app_id="tianxin-vm-host", instance_id=INSTANCE, workspace_id=service.WORKSPACE_ID)


@app.get("/")
def home():
    return send_from_directory(Path(__file__).parent, "panel.html")


@app.get("/workbench")
def workbench():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/vm/status")
def status():
    try:
        vm = manager.info()
        conn = bridge().status() if manager.CONFIG.exists() else {"online": False}
        return jsonify(vm=vm, connection=conn)
    except (RuntimeError, OSError, ValueError) as exc:
        return jsonify(vm={"state": "unavailable", "text": str(exc)[:300]}, connection={"online": False})


@app.post("/api/vm/start")
def start():
    result = manager.start()
    conn = bridge()
    if conn.status().get("manual"):
        code, _, body = conn.call("POST", "/api/vm/resume")
        if code != 200:
            raise RuntimeError(json.loads(body).get("error", "暂时无法恢复"))
    return jsonify(result)


@app.post("/api/vm/show")
def show():
    conn = bridge()
    if conn.status().get("online"):
        code, _, _ = conn.call("POST", "/api/vm/pause")
        if code != 200:
            raise RuntimeError("暂停未确认，请稍后重试")
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if conn.status().get("manual_ready"):
                break
            time.sleep(.2)
        else:
            raise RuntimeError("仍在停止当前任务，暂未打开登录窗口。稍后再点查看登录窗口")
    return jsonify(manager.start(show=True))


@app.post("/api/vm/shutdown")
def shutdown():
    return jsonify(manager.shutdown(bridge()))


@app.post("/api/vm/mount-tools")
def mount_tools():
    return jsonify(manager.mount_tools())


@app.route("/api/<path:path>", methods=["GET", "POST", "PUT"])
def forward(path):
    if path.startswith("vm/"):
        return jsonify(error="未知的运行环境操作"), 404
    target = "/api/" + path
    if request.query_string:
        target += "?" + request.query_string.decode("ascii")
    status, headers, body = bridge().call(request.method, target, request.get_data(),
                                        request.content_type or "")
    response = Response(body, status=status)
    for key, value in headers.items():
        if key.lower() in ("content-type", "content-disposition"):
            response.headers[key] = value
    response.headers["Cache-Control"] = "no-store"
    return response


def open_panel(url):
    edge = next((p for p in browser.EDGE_CANDIDATES if p.exists()), None)
    if edge:
        subprocess.Popen([str(edge), *browser.edge_args(DATA_DIR / "vm" / "ui-profile", "vm-ui"),
                          "--app=" + url, "--window-size=1440,920"], cwd=ROOT, env=os.environ.copy())


def main():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    manager.VM_ROOT.mkdir(parents=True, exist_ok=True)
    lock = service.acquire_lock(manager.VM_ROOT / "host.lock")
    state = manager.VM_ROOT / "host.json"
    if lock is None:
        import urllib.request
        from urllib.parse import urlsplit
        saved = json.loads(state.read_text(encoding="utf-8"))
        parsed = urlsplit(saved["url"])
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/" or not parsed.port:
            raise RuntimeError("已有工作台地址无效")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(saved["url"] + "api/health", timeout=2) as r:
            actual = json.load(r)
        if (actual.get("app_id") != "tianxin-vm-host" or actual.get("instance_id") != saved["instance_id"]
                or actual.get("workspace_id") != service.WORKSPACE_ID):
            raise RuntimeError("现有运行环境窗口身份不符")
        if not args.no_browser:
            open_panel(saved["url"])
        return
    httpd = service.bind_server(app, 8776)
    url = f"http://127.0.0.1:{httpd.port}/"
    state.write_text(json.dumps({"url": url, "instance_id": INSTANCE}), encoding="utf-8")
    print(url, flush=True)
    try:
        if not args.no_browser:
            open_panel(url)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        state.unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    main()
