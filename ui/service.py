"""本机界面服务的身份识别、单实例锁和端口分配。"""
import hashlib
import json
import msvcrt
import socket
import urllib.request
from pathlib import Path

from inquiry.runtime import DATA_DIR, ROOT

APP_ID = "tianxin-inquiry"
WORKSPACE_ID = hashlib.sha256(str(ROOT).casefold().encode("utf-8")).hexdigest()[:24]
STATE_FILE = DATA_DIR / "ui_server.json"
LOCK_FILE = DATA_DIR / "ui_server.lock"
DEFAULT_PORT = 8765


def acquire_lock(path: Path | None = None):
    path = path or LOCK_FILE
    handle = open(path, "a+")
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def probe(port: int, instance_id: str | None = None) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as response:
            result = json.loads(response.read(4096))
        return (result.get("app_id") == APP_ID and result.get("workspace_id") == WORKSPACE_ID
                and (instance_id is None or result.get("instance_id") == instance_id))
    except (OSError, ValueError, AttributeError):
        return False


def existing_url(state_file: Path | None = None) -> str | None:
    state_file = state_file or STATE_FILE
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        port = int(state["port"])
        if (state["workspace_id"] == WORKSPACE_ID and 1 <= port <= 65535
                and probe(port, state["instance_id"])):
            return f"http://127.0.0.1:{port}/"
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def bind_server(app, preferred: int = DEFAULT_PORT, fallback: bool = True):
    """先实际绑定端口，再打开窗口；冲突时使用后续端口，最后让系统分配。"""
    from werkzeug.serving import make_server

    ports = [*range(preferred, min(preferred + 20, 65536)), 0] if fallback and preferred else [preferred]
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", port))
            sock.listen(128)
        except OSError:
            sock.close()
            continue
        try:
            return make_server("127.0.0.1", sock.getsockname()[1], app, threaded=True, fd=sock.fileno())
        finally:
            sock.close()
    raise RuntimeError(f"界面端口 {preferred} 无法绑定或已被占用；没有打开其他程序的页面")


def write_state(port: int, instance_id: str, state_file: Path | None = None):
    state_file = state_file or STATE_FILE
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": port, "workspace_id": WORKSPACE_ID, "instance_id": instance_id}), encoding="utf-8")
    tmp.replace(state_file)
