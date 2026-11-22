"""TLS 证书配对、HMAC 请求，以及提交前持久化的执行记录。"""
import base64
import contextlib
import hashlib
import hmac
import http.client
import json
import re
import socket
import sqlite3
import ssl
import threading
import time
import uuid
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from inquiry.runtime import require_d_path
from vmhost.bridge import BridgeError, MAX_BODY, MAX_FILE, decode, encode

_CLOCKS = {}
_CLOCK_LOCK = threading.Lock()
CLOCK_CACHE_SECONDS = 60
WEBSITE_ALERT_IMAGE_PATH = re.compile(r"/api/worker/website-alert/image\?event=([0-9a-f]{32})")
WEBSITE_FRAME_PATH = re.compile(r"/api/worker/website-alert/frame\?event=([0-9a-f]{32})")
WEBSITE_INPUT_PATH = "/api/worker/website-alert/input"


def check_request(value, *, now=None):
    rid, route = value.get("id", ""), value.get("path", "")
    if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{32}", rid):
        raise BridgeError("请求编号无效")
    if (value.get("method") not in ("GET", "POST", "PUT") or not isinstance(route, str)
            or len(route) > 4096 or not route.startswith("/api/")
            or any(x in unquote(route) for x in ("\r", "\n", "\\", "\x00"))
            or urlsplit(route).fragment or ".." in unquote(urlsplit(route).path).split("/")):
        raise BridgeError("请求不在允许范围内")
    expires = value.get("expires")
    now = time.time() if now is None else now
    if type(expires) not in (int, float) or not now <= expires <= now + 120:
        raise BridgeError("请求已过期或两台电脑时间不一致，未执行")
    try:
        body = base64.b64decode(value["body"], validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise BridgeError("请求内容无效") from exc
    if len(body) > MAX_BODY or not isinstance(value.get("content_type", ""), str):
        raise BridgeError("请求内容过大或格式无效")
    return body


class Journal:
    def __init__(self, path):
        self.path = require_d_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS request(id TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                      "state TEXT NOT NULL, created REAL NOT NULL, status INTEGER, headers TEXT, body BLOB)")

    @contextlib.contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def recover(self):
        with self.connect() as c:
            c.execute("UPDATE request SET state='unknown' WHERE state='processing'")

    def execute(self, value, handler):
        body = check_request(value)
        digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT digest,state,status,headers,body FROM request WHERE id=?", (value["id"],)).fetchone()
            if old:
                if old[0] != digest:
                    raise BridgeError("重复编号对应的内容不一致，拒绝执行")
                if old[1] == "done":
                    return old[2], json.loads(old[3]), bytes(old[4])
                return 409, {"Content-Type": "application/json"}, '{"error":"原请求仍在执行或结果待核实，不会重复执行"}'.encode()
            c.execute("INSERT INTO request VALUES(?,?,'processing',?,NULL,NULL,NULL)",
                      (value["id"], digest, time.time()))
        # 认领记录已提交。进程在业务执行后、响应保存前中断时，重启只标记未知，不重放。
        try:
            status, headers, result = handler(value["method"], value["path"], body, value.get("content_type", ""))
            if len(result) > MAX_BODY:
                status, headers, result = 413, {}, '{"error":"报告过大，请在工作机导出"}'.encode()
            headers = {k: v for k, v in headers.items() if k.lower() in ("content-type", "content-disposition")}
            with self.connect() as c:
                c.execute("UPDATE request SET state='done',status=?,headers=?,body=? WHERE id=?",
                          (int(status), json.dumps(headers), result, value["id"]))
                c.execute("DELETE FROM request WHERE state='done' AND created<?", (time.time() - 600,))
            return status, headers, result
        except Exception:
            with self.connect() as c:
                c.execute("UPDATE request SET state='unknown' WHERE id=?", (value["id"],))
            return 409, {"Content-Type": "application/json"}, '{"error":"工作机未能确认结果，请先核对任务；没有自动重发"}'.encode()


class Remote:
    def __init__(self, cfg):
        self.cfg = cfg

    def _connect(self, address, context, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeError("连接检查已超时，未向工作机发送请求")
        conn = http.client.HTTPSConnection(address, self.cfg["port"], timeout=min(3, remaining), context=context)
        try:
            conn.connect()
            fingerprint = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(fingerprint, self.cfg["fingerprint"]):
                raise BridgeError("工作机证书已变化，停止连接；请重新核对连接文件")
            return conn
        except Exception:
            conn.close()
            raise

    def _clock_key(self, address):
        return (self.cfg["worker_id"], self.cfg["controller_id"], self.cfg["fingerprint"], address, self.cfg["port"])

    def _peer_clock(self, conn, address, context, deadline):
        """只信任已固定证书的 TLS 通道；单调时钟推进，不改 Windows 时间。"""
        key = self._clock_key(address)
        with _CLOCK_LOCK:
            clock = _CLOCKS.get(key)
        if clock is not None and 0 <= time.monotonic() - clock[1] < CLOCK_CACHE_SECONDS:
            return conn, clock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeError("连接校时超时，未发送业务请求")
        conn.sock.settimeout(min(3, remaining))
        # 旧版 Python 工作机对 HEAD 返回 501，仍会提供自己生成的 Date。
        # HEAD 不进入 RPC/业务处理，不启动 QQ，也不携带配对密钥。
        conn.request("HEAD", "/rpc")
        response = conn.getresponse()
        try:
            stamp = parsedate_to_datetime(response.getheader("Date", ""))
            if response.status not in (200, 405, 501) or stamp.tzinfo is None:
                raise ValueError("Invalid peer clock response")
            clock = (stamp.timestamp(), time.monotonic())
        except (TypeError, ValueError, OverflowError) as exc:
            raise BridgeError("工作机时间校准未通过，未发送业务请求") from exc
        with _CLOCK_LOCK:
            _CLOCKS[key] = clock
        # HTTP/1.0 工作机会在 HEAD 后关闭连接；重新连接也必须再次核验证书。
        conn.close()
        return self._connect(address, context, deadline), clock

    def call(self, method, path, body=b"", content_type="", timeout=40):
        deadline = time.monotonic() + timeout
        # 先本地校验请求结构，再按已核验工作机时间生成最终签名有效期。
        value = {"id": uuid.uuid4().hex, "method": method, "path": path,
                 "expires": time.time() + max(30, timeout), "body": base64.b64encode(body).decode(),
                 "content_type": content_type, "controller_id": self.cfg["controller_id"]}
        check_request(value)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False  # DHCP 地址可能变化；信任精确导入的证书并再次核对指纹。
        context.load_verify_locations(cadata=self.cfg["certificate"])
        for address in self.cfg["addresses"]:
            try:
                conn = self._connect(address, context, deadline)
            except BridgeError:
                raise
            except (OSError, ssl.SSLError):
                continue  # 尚未发送应用请求，可试下一张本机接口地址。
            try:
                conn, clock = self._peer_clock(conn, address, context, deadline)
                peer_now = clock[0] + time.monotonic() - clock[1]
                value["expires"] = peer_now + max(30, timeout)
                check_request(value, now=peer_now)
                raw = encode(value, self.cfg["secret"])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError("连接检查已超时，未向工作机发送请求")
                conn.sock.settimeout(remaining)
                conn.request("POST", "/rpc", body=raw, headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                if response.status != 200:
                    with _CLOCK_LOCK:
                        _CLOCKS.pop(self._clock_key(address), None)
                    raise BridgeError("工作机拒绝连接验证，请检查两台电脑时间、配对信息或服务状态")
                result = decode(response.read(MAX_FILE + 1), self.cfg["secret"])
                if (result.get("id") != value["id"]
                        or result.get("worker_id") != self.cfg["worker_id"]):
                    raise BridgeError("工作机响应身份或编号不符")
                payload = base64.b64decode(result["body"], validate=True)
                if len(payload) > MAX_BODY:
                    raise BridgeError("工作机响应超过大小限制")
                return result["status"], result.get("headers", {}), payload
            except (OSError, ValueError, KeyError, http.client.HTTPException) as exc:
                raise BridgeError("工作机未及时确认结果，请先检查任务状态；系统没有自动重发") from exc
            finally:
                conn.close()
        raise BridgeError("无法连接 QQ 工作机，请检查开机、局域网和工作机连接权限")

    def status(self):
        try:
            status, _, body = self.call("GET", "/api/worker/status", timeout=5)
            result = json.loads(body)
            if status != 200:
                raise BridgeError("工作机状态不可用")
            return result | {"online": True}
        except (BridgeError, ValueError) as exc:
            return {"online": False, "text": "QQ 工作机暂时无法连接", "error": str(exc)}


def make_server(cfg, handler, journal, certfile, keyfile, host="0.0.0.0", preferred=8778):
    slots = threading.BoundedSemaphore(12)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_HEAD(self):
            if self.path != "/rpc":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_POST(self):
            if self.path != "/rpc" or not slots.acquire(blocking=False):
                self.send_error(503)
                return
            try:
                self.connection.settimeout(15)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_FILE or self.headers.get("Transfer-Encoding"):
                    raise BridgeError("请求大小无效")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise BridgeError("请求不完整")
                value = decode(raw, cfg["secret"])
                if value.get("controller_id") != cfg["controller_id"]:
                    raise BridgeError("办公电脑身份不符")
                request_body = check_request(value)
                method, path = value["method"], value["path"]
                private_read = method == "GET" and (path == "/api/worker/status"
                                                   or WEBSITE_ALERT_IMAGE_PATH.fullmatch(path)
                                                   or WEBSITE_FRAME_PATH.fullmatch(path))
                if private_read or (method == "POST" and path == WEBSITE_INPUT_PATH):
                    # 画面及人工输入留在当前操作的内存中，画面编号一次有效。
                    status, headers, body = handler(method, path, request_body if method == "POST" else b"",
                                                    value.get("content_type", ""))
                else:
                    status, headers, body = journal.execute(value, handler)
                result = encode({"id": value["id"], "worker_id": cfg["worker_id"], "status": status,
                                 "headers": headers, "body": base64.b64encode(body).decode()}, cfg["secret"])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(result)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(result)
            except (BridgeError, ValueError, KeyError, TypeError):
                try:
                    self.send_error(403, "Request rejected")
                except OSError:
                    pass
            except OSError:
                pass  # 客户端已断线；执行记录保留，不能因响应发送失败再执行业务。
            finally:
                slots.release()

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = False

        def server_bind(self):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

        def get_request(self):
            connection, address = self.socket.accept()
            connection.settimeout(5)
            try:
                return context.wrap_socket(connection, server_side=True), address
            except Exception:
                connection.close()
                raise

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certfile), str(keyfile))
    for port in ([0] if preferred == 0 else range(preferred, preferred + 20)):
        try:
            server = Server((host, port), Handler)
        except OSError:
            continue
        return server
    raise RuntimeError("QQ 工作机端口无法绑定，请检查占用")
