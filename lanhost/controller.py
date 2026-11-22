"""办公电脑的本机界面代理。不会导入 QQ 执行器或启动本地询价进程。"""
import argparse
import json
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path

from inquiry.runtime import ROOT, DATA_DIR
from flask import Flask, Response, jsonify, request, send_from_directory
from inquiry import browser
from ui import service
from lanhost import config
from lanhost.transport import Remote
from vmhost.bridge import BridgeError

INSTANCE = uuid.uuid4().hex
app = Flask(__name__, static_folder=str(ROOT / "ui/static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024


def protect_local():
    if request.remote_addr not in ("127.0.0.1", "::1") or request.host.split(":")[0] not in ("localhost", "127.0.0.1"):
        return jsonify(error="工作台只允许本机访问"), 403
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("Origin") != request.host_url.rstrip("/"):
        return jsonify(error="请从本机工作台操作"), 403


app.before_request(protect_local)


@app.after_request
def no_stale_ui(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def remote():
    if not config.CONNECTION.exists():
        raise BridgeError("请先导入第二台电脑生成的连接文件")
    return Remote(config.validate_connection(config.read_json(config.CONNECTION)))


@app.errorhandler(BridgeError)
@app.errorhandler(RuntimeError)
@app.errorhandler(OSError)
@app.errorhandler(ValueError)
def failed(exc):
    return jsonify(error=str(exc)[:350]), 503


@app.get("/api/health")
def health():
    return jsonify(app_id="tianxin-lan-controller", instance_id=INSTANCE, workspace_id=service.WORKSPACE_ID)


@app.get("/")
def home():
    return send_from_directory(Path(__file__).parent, "panel.html")


@app.get("/workbench")
def workbench():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/lan/status")
def status():
    if not config.CONNECTION.exists():
        return jsonify(online=False, paired=False, text="等待导入 QQ 工作机的连接文件")
    result = remote().status()
    return jsonify(result | {"paired": True})


@app.get("/api/lan/website-alert/image")
def website_alert_image():
    if request.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
        return Response(status=403)
    events = request.args.getlist("event")
    if len(events) != 1 or not re.fullmatch(r"[0-9a-f]{32}", events[0]) or len(request.args) != 1:
        return Response(status=404)
    code, headers, body = remote().call("GET", f"/api/worker/website-alert/image?event={events[0]}")
    if code != 200:
        return Response(status=code)
    if next((value for key, value in headers.items() if key.lower() == "content-type"), "") != "image/jpeg":
        return Response(status=502)
    return Response(body, mimetype="image/jpeg", headers={
        "Cross-Origin-Resource-Policy": "same-origin", "X-Content-Type-Options": "nosniff",
    })


@app.route("/api/lan/website-alert/settings", methods=["GET", "PUT"])
def website_alert_settings():
    body = request.get_data()
    if len(body) > 2048:
        return jsonify(error="提醒设置内容过大"), 400
    code, _, result = remote().call("GET" if request.method == "HEAD" else request.method,
                                    "/api/worker/website-alert/settings", body, request.content_type or "")
    return Response(result, status=code, mimetype="application/json")


@app.post("/api/lan/import")
def import_connection():
    if len(request.get_data()) > 65536:
        raise ValueError("连接文件过大")
    cfg = config.validate_connection(request.get_json(force=True))
    code, _, body = Remote(cfg).call("GET", "/api/worker/status", timeout=8)
    if code != 200 or json.loads(body).get("worker_id") != cfg["worker_id"]:
        raise ValueError("工作机身份或连接未验证，未保存连接文件")
    config.save_json(config.CONNECTION, cfg)
    return jsonify(ok=True, name=cfg["name"])


@app.post("/api/lan/pause")
@app.post("/api/lan/resume")
@app.post("/api/lan/sending")
def control():
    code, headers, body = remote().call("POST", "/api/worker/" + request.path.rsplit("/", 1)[-1], request.get_data(), request.content_type or "")
    return Response(body, status=code, headers=headers)


@app.post("/api/lan/package")
def package_worker():
    from lanhost.package import build
    return jsonify(build("worker", (request.get_json(silent=True) or {}).get("with_data") is True))


@app.route("/api/<path:path>", methods=["GET", "POST", "PUT"])
def forward(path):
    if path.startswith(("lan/", "vm/", "worker/")):
        return jsonify(error="未知的工作机操作"), 404
    route = "/api/" + path
    if request.query_string:
        route += "?" + request.query_string.decode("ascii")
    code, headers, body = remote().call(request.method, route, request.get_data(), request.content_type or "")
    response = Response(body, status=code)
    for key, value in headers.items():
        if key.lower() in ("content-type", "content-disposition"):
            response.headers[key] = value
    response.headers["Cache-Control"] = "no-store"
    return response


def open_panel(url):
    edge = next((p for p in browser.EDGE_CANDIDATES if p.exists()), None)
    if edge:
        subprocess.Popen([str(edge), *browser.edge_args(DATA_DIR / "lan/ui-profile", "lan-ui"),
                          "--app=" + url, "--window-size=1440,920"], cwd=ROOT, env=os.environ.copy())


def main():
    import urllib.request
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    config.controller()
    lock = service.acquire_lock(config.LAN_DIR / "controller.lock")
    state_file = config.LAN_DIR / "controller-ui.json"
    if lock is None:
        from urllib.parse import urlsplit
        saved = config.read_json(state_file)
        parsed = urlsplit(saved["url"])
        if parsed.hostname != "127.0.0.1" or parsed.scheme != "http" or parsed.path != "/" or not parsed.port:
            raise RuntimeError("已有工作台地址无效")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(saved["url"] + "api/health", timeout=2) as r:
            actual = json.load(r)
        if (actual.get("app_id") != "tianxin-lan-controller" or actual.get("instance_id") != saved["instance_id"]
                or actual.get("workspace_id") != service.WORKSPACE_ID):
            raise RuntimeError("已有工作台身份不符")
        if not args.no_browser:
            open_panel(saved["url"])
        return
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    httpd = service.bind_server(app, 8777)
    url = f"http://127.0.0.1:{httpd.port}/"
    config.save_json(state_file, {"url": url, "instance_id": INSTANCE})
    try:
        if not args.no_browser:
            open_panel(url)
        print(url, flush=True)
        httpd.serve_forever()
    finally:
        httpd.server_close()
        state_file.unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    main()
