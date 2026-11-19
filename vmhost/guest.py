"""来宾服务：通过唯一共享目录接收工作台请求，不开放网络服务端口。"""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry import db, isolation, qq
from inquiry.runtime import DATA_DIR
from vmhost.bridge import Bridge, recover_interrupted, serve_one, write_atomic


def request_handler(method, path, body, content_type):
    from ui import server
    if path.split("?")[0] == "/api/vm/pause" and method == "POST":
        isolation.MANUAL.set()
        if server.run_state()["running"]:
            server.STOP_FILE.write_text("stop", encoding="utf-8")
        return 200, {"Content-Type": "application/json"}, b'{"ok":true}'
    if path.split("?")[0] == "/api/vm/resume" and method == "POST":
        if isolation.DRAINING.is_set() or server.run_state()["running"]:
            return 409, {"Content-Type": "application/json"}, '{"error":"仍有任务在停止，请稍后恢复"}'.encode()
        server.STOP_FILE.unlink(missing_ok=True)
        isolation.MANUAL.clear()
        return 200, {"Content-Type": "application/json"}, b'{"ok":true}'
    if path.split("?")[0] == "/api/vm/prepare-shutdown" and method == "POST":
        isolation.DRAINING.set()
        if server.run_state()["running"]:
            server.STOP_FILE.write_text("stop", encoding="utf-8")
        with db.connect() as c:
            c.execute("UPDATE outbox SET status='deferred',error='准备关闭运行环境，未发送',updated_at=? WHERE status='queued'", (time.time(),))
        return 200, {"Content-Type": "application/json"}, b'{"ok":true}'
    with server.app.test_client() as client:
        response = client.open(path, method=method, data=body, content_type=content_type or None)
        try:
            headers = {k: v for k, v in response.headers.items() if k.lower() in ("content-type", "content-disposition")}
            response.direct_passthrough = False
            return response.status_code, headers, response.get_data()
        finally:
            response.close()


def collector():
    """Compatibility entry: only explicit one-round requests may touch QQ."""
    from inquiry import collection
    collection.start_pump()


def main():
    cfg = isolation.enable_guest()
    from ui import server, service
    lock = service.acquire_lock()
    if lock is None:
        raise RuntimeError("来宾中已有询价服务，未重复启动")
    # 仅在配对来宾进程中阻止空闲睡眠；不会改宿主电源计划，也不解除锁屏。
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
    bridge = Bridge(cfg["bridge"], cfg["secret"])
    recover_interrupted(bridge)
    server.start_background_services()

    def heartbeat():
        while True:
            try:
                write_atomic(bridge.root / "heartbeat.json", {
                    "at": time.time(), "vm_uuid": cfg["vm_uuid"], "allow_send": isolation.ALLOW_SEND,
                    "shutdown_ready": isolation.DRAINING.is_set() and not server.run_state()["running"],
                    "manual": isolation.MANUAL.is_set(),
                    "manual_ready": isolation.MANUAL.is_set() and not server.run_state()["running"],
                    "text": "只读验收模式" if not isolation.ALLOW_SEND else "运行环境已连接",
                }, bridge.secret)
            except OSError:
                pass
            time.sleep(2)

    threading.Thread(target=heartbeat, daemon=True, name="vm-heartbeat").start()
    try:
        while True:
            for path in sorted((bridge.root / "requests").glob("*.json")):
                serve_one(bridge, path, request_handler)
            # 清理过期的通信临时文件，业务数据始终在来宾数据库中。
            for name in ("requests", "responses"):
                for path in (bridge.root / name).glob("*.json"):
                    try:
                        if time.time() - path.stat().st_mtime > 600:
                            path.unlink()
                    except OSError:
                        pass
            time.sleep(0.1)
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        lock.close()


if __name__ == "__main__":
    main()
