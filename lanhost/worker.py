"""专用 QQ 工作机：只开放配对 TLS RPC，复用业务 API 与串行回复采集。"""
import argparse
import ctypes
import json
import logging
import threading
import time
import re
import os
from urllib.parse import urlsplit, parse_qs
from pathlib import Path

from inquiry.runtime import ROOT, DATA_DIR
from lanhost import config
from lanhost.transport import Journal, WEBSITE_ALERT_IMAGE_PATH, WEBSITE_FRAME_PATH, WEBSITE_INPUT_PATH, make_server


WEBSITE_ALERTS = None
MANUAL_WEBSITE = None


def state(cfg):
    from inquiry import isolation, collection
    from ui import server
    from lanhost import live_trial
    trial = live_trial.active_status()
    busy = server.run_state()["running"] or trial["trial_running"]
    return {"worker_id": cfg["worker_id"], "name": cfg["name"], "online": True,
            "allow_send": isolation.ALLOW_SEND, "manual": isolation.MANUAL.is_set(),
            "manual_ready": isolation.MANUAL.is_set() and not busy,
            "running": busy, "text": "授权实发送测试进行中" if trial["trial_running"] else ("常规实发已开放" if isolation.ALLOW_SEND else "常规发送已锁定"),
            "collection": collection.status(),
            "website_alert": WEBSITE_ALERTS.status() if WEBSITE_ALERTS else None,
            "manual_website_support": MANUAL_WEBSITE is not None,
            "normal_capability": 1, "support_capability": 1, **trial}


def _manual_website_request(method, event, body):
    from lanhost.manual_website import ManualPageConflict, ManualPageUnavailable

    status = 200
    try:
        if MANUAL_WEBSITE is None:
            raise ManualPageUnavailable("当前工作机不支持远程人工处理")
        if method == "GET":
            payload = MANUAL_WEBSITE.frame(event)
        else:
            if not 0 < len(body) <= 8192:
                raise ValueError()
            MANUAL_WEBSITE.relay(json.loads(body))
            payload = {"ok": True}
    except ManualPageConflict as error:
        status, payload = 409, {"error": str(error)}
    except ManualPageUnavailable as error:
        status, payload = 503, {"error": str(error)}
    except ValueError:
        status, payload = 400, {"error": "人工输入格式无效"}
    return status, {"Content-Type": "application/json"}, json.dumps(payload, ensure_ascii=False).encode()


def handler(cfg):
    from vmhost.guest import request_handler

    def dispatch(method, path, body, content_type):
        from lanhost import live_trial
        route = urlsplit(path).path
        if route.startswith("/api/worker/support/"):
            from lanhost import support
            try:
                result = support.dispatch(cfg, method, route.removeprefix("/api/worker/support/"), body, parse_qs(urlsplit(path).query))
                return 200, {"Content-Type": "application/json"}, json.dumps(result, ensure_ascii=False).encode()
            except (ValueError, OSError, KeyError, TypeError, SyntaxError) as exc:
                return 409, {"Content-Type": "application/json"}, json.dumps({"error": str(exc)[:400]}).encode()
        if method == "GET" and path == "/api/worker/status":
            return 200, {"Content-Type": "application/json"}, json.dumps(state(cfg), ensure_ascii=False).encode()
        if method == "GET" and (match := WEBSITE_ALERT_IMAGE_PATH.fullmatch(path)):
            data = WEBSITE_ALERTS.image(match[1]) if WEBSITE_ALERTS else None
            return (200, {"Content-Type": "image/jpeg"}, data) if data is not None else (404, {}, b"")
        if method == "GET" and (match := WEBSITE_FRAME_PATH.fullmatch(path)):
            return _manual_website_request(method, match[1], b"")
        if method == "POST" and path == WEBSITE_INPUT_PATH:
            return _manual_website_request(method, None, body)
        if path == "/api/worker/website-alert/settings" and method in ("GET", "PUT"):
            from lanhost import alert_webhook
            try:
                if method == "PUT":
                    if len(body) > 2048:
                        raise ValueError()
                    payload = json.loads(body)
                    alert_webhook.save(payload.get("webhook_url") if isinstance(payload, dict) else None)
                return 200, {"Content-Type": "application/json"}, json.dumps({"configured": bool(alert_webhook.load())}).encode()
            except ValueError:
                return 400, {"Content-Type": "application/json"}, '{"error":"请输入有效的企业微信群机器人 Webhook 地址"}'.encode()
            except OSError:
                return 503, {"Content-Type": "application/json"}, '{"error":"工作机无法保存提醒设置"}'.encode()
        if method == "POST" and route == "/api/worker/sending":
            try:
                config.set_sending(cfg, json.loads(body).get("enabled"))
                return 200, {"Content-Type": "application/json"}, json.dumps(state(cfg), ensure_ascii=False).encode()
            except (ValueError, TypeError) as exc:
                return 409, {"Content-Type": "application/json"}, json.dumps({"error":str(exc)}, ensure_ascii=False).encode()
        if method == "POST" and path in ("/api/worker/pause", "/api/worker/resume"):
            if path.endswith("/resume") and live_trial.active_status()["trial_running"]:
                return 409, {"Content-Type": "application/json"}, '{"error":"授权测试仍在执行，请先暂停并等待空闲"}'.encode()
            if path.endswith("/pause") and live_trial.active_status()["trial_running"]:
                from ui import server
                server.STOP_FILE.write_text("stop", encoding="utf-8")
            target = "/api/vm/" + path.rsplit("/", 1)[-1]
            return request_handler(method, target, body, content_type)
        if method == "POST" and route == "/api/worker/live-trial":
            try:
                result = live_trial.start_remote(ROOT, cfg, json.loads(body))
                return 202, {"Content-Type": "application/json"}, json.dumps(result).encode()
            except (ValueError, OSError) as exc:
                return 409, {"Content-Type": "application/json"}, json.dumps({"error": str(exc)}).encode()
        if method == "GET" and route in ("/api/worker/trial-result", "/api/worker/trial-audit", "/api/worker/trial-log"):
            request_id = parse_qs(urlsplit(path).query).get("request_id", [""])[0]
            if not re.fullmatch(r"[0-9a-f]{32}", request_id):
                return 400, {}, b'{}'
            if route.endswith("log"):
                log = live_trial.safe_path(ROOT / "data/logs" / ("live-trial-" + request_id + ".log"), ROOT)
                if not log.exists():
                    return 404, {}, b'{}'
                return 200, {"Content-Type": "application/json"}, json.dumps({"lines": log.read_bytes()[-131072:].decode("utf-8", errors="replace").splitlines()}, ensure_ascii=False).encode()
            folder = live_trial.safe_path(ROOT / "data/lan/live-trials" / request_id, ROOT)
            file = live_trial.safe_path(folder / ("result.json" if route.endswith("result") else "audit.jsonl"), ROOT)
            if not file.exists():
                return 404, {}, b'{}'
            value = json.loads(file.read_text(encoding="utf-8")) if route.endswith("result") else [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()[-2000:]]
            return 200, {"Content-Type": "application/json"}, json.dumps(value, ensure_ascii=False).encode()
        if path.startswith("/api/worker/") or path.startswith("/api/vm/"):
            return 404, {}, b'{}'
        return request_handler(method, path, body, content_type)
    return dispatch


def local_app(cfg, export, rpc_port):
    from flask import Flask, jsonify, request, send_from_directory
    from lanhost.controller import protect_local
    app = Flask("tianxin-worker-local")
    app.before_request(protect_local)

    @app.get("/")
    def home():
        return send_from_directory(Path(__file__).parent, "worker.html")

    @app.get("/api/status")
    def status():
        return jsonify(state(cfg) | {"addresses": config.local_addresses(), "port": rpc_port,
                                    "connection_file": str(export), "service_pid": os.getpid()})

    @app.post("/api/pause")
    @app.post("/api/resume")
    def pause_resume():
        status, headers, body = handler(cfg)("POST", "/api/worker/" + request.path.rsplit("/", 1)[-1], b"", "")
        return body, status, headers

    @app.post("/api/browser")
    def open_login_browser():
        if not state(cfg)["manual_ready"]:
            return jsonify(error="请先暂停自动操作，等待任务停止后再登录网站"), 409
        status, headers, body = handler(cfg)("POST", "/api/browser/open", b"", "")
        return body, status, headers

    @app.post("/api/folder")
    def folder():
        import os
        os.startfile(str(config.LAN_DIR))
        return jsonify(ok=True)

    @app.post("/api/export")
    def refresh_connection():
        value = config.descriptor(cfg, rpc_port)
        if not value["addresses"]:
            return jsonify(error="未发现局域网地址，请先连接与办公电脑相同的网络"), 409
        config.save_json(export, value)
        return jsonify(ok=True)

    @app.post("/api/live-trial")
    def start_live_trial():
        status, headers, body = handler(cfg)("POST", "/api/worker/live-trial", request.get_data(), "application/json")
        return body, status, headers

    return app


def main():
    global WEBSITE_ALERTS, MANUAL_WEBSITE
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", type=Path, help="首次配对用的办公电脑部署资料")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.setup:
        config.prepare_worker(config.read_json(args.setup))
    from inquiry import isolation
    cfg = isolation.enable_worker()
    from ui import server, service
    lock = service.acquire_lock()
    if lock is None:
        import urllib.request
        from urllib.parse import urlsplit
        saved = config.read_json(config.LAN_DIR / "worker-ui.json")
        parsed = urlsplit(saved["url"])
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/" or not parsed.port:
            raise RuntimeError("已有工作机窗口地址无效")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(saved["url"] + "api/status", timeout=2) as r:
            actual = json.load(r)
        if actual.get("worker_id") != cfg["worker_id"]:
            raise RuntimeError("已有工作机窗口身份不符")
        if not args.no_browser:
            server.open_window(saved["url"])
        return
    isolation.MANUAL.set()  # 每次启动先等待用户登录/确认；不自动操作 QQ。
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    journal = Journal(config.LAN_DIR / "requests.db")
    journal.recover()
    httpd = make_server(cfg, handler(cfg), journal, config.LAN_DIR / "server.pem", config.LAN_DIR / "server-key.pem")
    export = config.LAN_DIR / "办公电脑连接.json"
    config.save_json(export, config.descriptor(cfg, httpd.server_port))
    local = service.bind_server(local_app(cfg, export, httpd.server_port), 8779)
    local_url = f"http://127.0.0.1:{local.port}/"
    config.save_json(config.LAN_DIR / "worker-ui.json", {"url": local_url, "port": httpd.server_port})
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
    server.start_background_services()
    from inquiry import jobs
    from lanhost import live_trial
    from lanhost.manual_website import ManualWebsite
    from lanhost.website_alerts import WebsiteAlerts
    jobs.start_pump()
    WEBSITE_ALERTS = WebsiteAlerts(config.LAN_DIR / "website-alert.jpg")
    MANUAL_WEBSITE = ManualWebsite(WEBSITE_ALERTS, available=lambda: not (
        isolation.DRAINING.is_set() or server.run_state()["running"] or live_trial.active_status()["trial_running"]
    ))
    alert_monitor = WEBSITE_ALERTS.start(isolation.DRAINING)
    threading.Thread(target=local.serve_forever, daemon=True, name="worker-local-panel").start()
    try:
        if not args.no_browser:
            server.open_window(local_url)
        print("工作机已启动。连接文件：" + str(export), flush=True)
        httpd.serve_forever()
    finally:
        isolation.DRAINING.set()
        alert_monitor.join(timeout=2)
        WEBSITE_ALERTS.clear()
        local.shutdown()
        local.server_close()
        httpd.server_close()
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        lock.close()


if __name__ == "__main__":
    main()
