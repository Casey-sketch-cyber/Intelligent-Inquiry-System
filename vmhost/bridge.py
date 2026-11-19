"""通过指定共享目录传递有签名、有期限的请求；不重放未确认的写操作。"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

MAX_BODY = 12 * 1024 * 1024
MAX_FILE = 18 * 1024 * 1024


class BridgeError(RuntimeError):
    pass


def encode(value, secret):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return json.dumps({"body": body, "mac": mac}, ensure_ascii=False).encode()


def decode(raw, secret):
    if len(raw) > MAX_FILE:
        raise BridgeError("通信文件超过大小限制")
    try:
        outer = json.loads(raw)
        if not isinstance(outer, dict) or not isinstance(outer.get("body"), str) or not isinstance(outer.get("mac"), str):
            raise BridgeError("通信文件格式无效")
        expected = hmac.new(secret.encode(), outer["body"].encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, outer["mac"]):
            raise BridgeError("运行环境身份核验失败")
        value = json.loads(outer["body"])
        if not isinstance(value, dict):
            raise BridgeError("通信内容格式无效")
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise BridgeError("通信文件无效") from exc


def write_atomic(path, value, secret):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_bytes(encode(value, secret))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_signed(path, secret):
    with Path(path).open("rb") as stream:
        return decode(stream.read(MAX_FILE + 1), secret)


class Bridge:
    def __init__(self, root, secret):
        self.root, self.secret = Path(root), secret
        if len(secret) < 48:
            raise BridgeError("配对密钥无效")
        for name in ("requests", "processing", "responses"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def status(self):
        try:
            state = read_signed(self.root / "heartbeat.json", self.secret)
            state["online"] = 0 <= time.time() - float(state["at"]) < 15
            return state
        except (OSError, BridgeError, KeyError, TypeError, ValueError):
            return {"online": False, "text": "等待虚拟机里的询价服务"}

    def call(self, method, path, body=b"", content_type="", timeout=40):
        if method not in ("GET", "POST", "PUT") or not path.startswith("/api/") or "\r" in path or "\n" in path:
            raise BridgeError("请求不在允许范围内")
        if len(body) > MAX_BODY:
            raise BridgeError("请求内容过大")
        if not self.status()["online"]:
            raise BridgeError("虚拟机询价服务未连接，请先启动或完成登录")
        rid = uuid.uuid4().hex
        target = self.root / "requests" / (rid + ".json")
        response = self.root / "responses" / (rid + ".json")
        write_atomic(target, {
            "id": rid, "method": method, "path": path, "expires": time.time() + timeout,
            "body": base64.b64encode(body).decode(), "content_type": content_type,
        }, self.secret)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if response.exists():
                result = read_signed(response, self.secret)
                if result.get("id") != rid:
                    raise BridgeError("响应与请求不匹配")
                response.unlink(missing_ok=True)
                return result["status"], result.get("headers", {}), base64.b64decode(result["body"], validate=True)
            time.sleep(0.1)
        # 请求一经发布，不自行重发，也不删除可能正在处理的请求。
        raise BridgeError("运行环境未及时确认结果。请先查看任务或发件箱，不要重复提交；系统没有自动重发")


def serve_one(bridge, path, handler):
    """先原子认领，再执行一次。崩溃后 processing 保留，不重新执行。"""
    path = Path(path)
    if len(path.stem) != 32 or any(c not in "0123456789abcdef" for c in path.stem):
        return False
    claimed = bridge.root / "processing" / path.name
    try:
        # 只有一名来宾服务可运行。遗留 processing 代表未知结果，绝不覆盖。
        if claimed.exists():
            return False
        path.rename(claimed)
    except OSError:
        return False
    try:
        request = read_signed(claimed, bridge.secret)
        if request.get("id") != path.stem:
            raise BridgeError("请求编号无效")
        route = request.get("path", "")
        if (request.get("method") not in ("GET", "POST", "PUT") or not isinstance(route, str)
                or not route.startswith("/api/") or "\r" in route or "\n" in route):
            raise BridgeError("请求不在允许范围内")
        if not time.time() <= float(request["expires"]) <= time.time() + 120:
            status, headers, body = 408, {}, '{"error":"请求已过期，未执行"}'.encode()
        else:
            body = base64.b64decode(request["body"], validate=True)
            if len(body) > MAX_BODY:
                raise BridgeError("请求内容过大")
            status, headers, body = handler(request["method"], request["path"], body, request.get("content_type", ""))
        if len(body) > MAX_BODY:
            status, headers, body = 413, {}, '{"error":"文件过大，请在运行环境中导出"}'.encode()
        write_atomic(bridge.root / "responses" / path.name, {
            "id": path.stem, "status": status, "headers": headers,
            "body": base64.b64encode(body).decode(),
        }, bridge.secret)
        claimed.unlink(missing_ok=True)
    except Exception:
        # 无法确认是否已经产生业务副作用；保留认领文件供人工核验。
        return False
    return True


def recover_interrupted(bridge):
    """告诉原调用者结果未知；不执行中断的业务请求。"""
    for path in (bridge.root / "processing").glob("*.json"):
        try:
            req = read_signed(path, bridge.secret)
            if req.get("id") != path.stem:
                continue
            write_atomic(bridge.root / "responses" / path.name, {
                "id": path.stem, "status": 409, "headers": {},
                "body": base64.b64encode('{"error":"运行环境曾中断，请先核对任务和发件箱，不能直接重发"}'.encode()).decode(),
            }, bridge.secret)
            path.unlink()
        except (OSError, BridgeError):
            continue
