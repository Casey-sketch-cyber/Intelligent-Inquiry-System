"""设备配对材料仅存 D 盘；连接文件只授予当前办公电脑访问本工作机。"""
import functools
import hashlib
import ipaddress
import json
import secrets
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from inquiry.runtime import ROOT, DATA_DIR, require_d_path

LAN_DIR = DATA_DIR / "lan"
CONTROLLER = LAN_DIR / "controller.json"
CONNECTION = LAN_DIR / "connection.json"
WORKER = DATA_DIR / "worker.json"
PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


@functools.lru_cache(maxsize=1)
def device_identity():
    import winreg
    from inquiry.isolation import machine_identity
    _, bios = machine_identity()
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                       winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        guid = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip().lower()
    if not bios or not guid:
        raise RuntimeError("无法核验本机设备身份")
    return hashlib.sha256((bios + "|" + guid).encode()).hexdigest()


def read_json(path):
    path = require_d_path(Path(path))
    if path.stat().st_size > 65536:
        raise ValueError("配对文件过大")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("配对文件格式无效")
    return value


def save_json(path, value):
    path = require_d_path(Path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def controller():
    if WORKER.exists() or (DATA_DIR / "vm_guest.json").exists():
        raise RuntimeError("此目录是 QQ 执行环境，不能作为办公电脑工作台")
    if not CONTROLLER.exists():
        save_json(CONTROLLER, {"version": 1, "controller_id": uuid.uuid4().hex,
                               "office_device": device_identity()})
    cfg = read_json(CONTROLLER)
    if cfg.get("office_device") != device_identity():
        raise RuntimeError("办公电脑配对材料来自其他设备，请重新生成部署包")
    uuid.UUID(cfg["controller_id"])
    return cfg


def private_address(value, allow_loopback=False):
    try:
        address = ipaddress.ip_address(value)
        return address.version == 4 and (any(address in n for n in PRIVATE_NETS)
                                          or allow_loopback and address.is_loopback)
    except (TypeError, ValueError):
        return False


def local_addresses():
    # 不扫描其他电脑，只读取本机接口。多个网卡全部列出，连接时只在发请求前尝试地址。
    values = socket.gethostbyname_ex(socket.gethostname())[2]
    return sorted({x for x in values if private_address(x)})


def certificate(folder, worker_id):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Tianxin-" + worker_id)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1825))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    (folder / "server.pem").write_text(pem, encoding="ascii")
    (folder / "server-key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return pem, cert.fingerprint(hashes.SHA256()).hex()


def prepare_worker(bootstrap):
    office = bootstrap.get("office_device")
    if not isinstance(office, str) or len(office) != 64 or office == device_identity():
        raise RuntimeError("工作机必须是另一台电脑；办公电脑不会启用 QQ 桌面操作")
    uuid.UUID(bootstrap["controller_id"])
    if CONTROLLER.exists() or WORKER.exists() or (DATA_DIR / "vm_guest.json").exists():
        raise RuntimeError("此目录已配对或属于其他运行角色，未覆盖配置")
    LAN_DIR.mkdir(parents=True, exist_ok=True)
    worker_id = uuid.uuid4().hex
    pem, fingerprint = certificate(LAN_DIR, worker_id)
    cfg = {"version": 1, "role": "worker", "worker_id": worker_id,
           "controller_id": bootstrap["controller_id"], "office_device": office,
           "worker_device": device_identity(), "runtime": str(ROOT),
           "name": socket.gethostname(), "secret": secrets.token_hex(32), "allow_send": False,
           "certificate": pem, "fingerprint": fingerprint}
    save_json(WORKER, cfg)
    return cfg


def worker_config():
    cfg = read_json(WORKER)
    if (cfg.get("role") != "worker" or cfg.get("worker_device") != device_identity()
            or cfg.get("office_device") == device_identity() or CONTROLLER.exists()
            or not isinstance(cfg.get("runtime"), str) or not Path(cfg["runtime"]).is_absolute()
            or require_d_path(Path(cfg["runtime"])) != ROOT
            or cfg.get("allow_send") is not False or len(cfg.get("secret", "")) != 64):
        raise RuntimeError("QQ 工作机身份或只读配置不符，未接管桌面")
    uuid.UUID(cfg["worker_id"])
    uuid.UUID(cfg["controller_id"])
    return cfg


def sending_enabled(cfg):
    path = LAN_DIR / "sending.json"
    if not path.exists():
        return False
    value = read_json(path)
    return (value.get("enabled") is True and all(value.get(k) == cfg[k]
            for k in ("worker_id", "controller_id", "worker_device")))


def set_sending(cfg, enabled):
    if type(enabled) is not bool:
        raise ValueError("实发开关无效")
    from inquiry import isolation
    from ui import server
    from lanhost import live_trial
    if not isolation.ENABLED or isolation.MODE != "worker":
        raise ValueError("只能在已配对的第二台工作机更改实发模式")
    if not isolation.MANUAL.is_set() or server.run_state()["running"] or live_trial.active_status()["trial_running"]:
        raise ValueError("请先暂停自动操作，等工作机空闲后再更改实发模式")
    save_json(LAN_DIR / "sending.json", {k: cfg[k] for k in ("worker_id", "controller_id", "worker_device")} | {"enabled": enabled})
    isolation.ALLOW_SEND = enabled


def descriptor(cfg, port, addresses=None):
    return {k: cfg[k] for k in ("version", "worker_id", "controller_id", "worker_device", "name",
                                "secret", "certificate", "fingerprint")} | {
        "kind": "tianxin-worker-connection", "port": port,
        "addresses": addresses if addresses is not None else local_addresses(), "allow_send": False}


def validate_connection(value, office=None, allow_loopback=False):
    office = office or controller()
    if (not isinstance(value, dict) or value.get("kind") != "tianxin-worker-connection"
            or type(value.get("version")) is not int or value["version"] != 1
            or value.get("controller_id") != office["controller_id"]
            or value.get("worker_device") == office["office_device"] or value.get("allow_send") is not False):
        raise ValueError("连接文件不属于当前办公电脑，或不是受支持的独立工作机连接")
    try:
        uuid.UUID(value["worker_id"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("连接文件的工作机编号无效") from exc
    if not isinstance(value.get("secret"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["secret"]):
        raise ValueError("连接文件缺少配对凭据")
    if (not isinstance(value.get("name"), str) or not 1 <= len(value["name"]) <= 128
            or not isinstance(value.get("worker_device"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["worker_device"])):
        raise ValueError("连接文件的工作机资料无效")
    if not isinstance(value.get("fingerprint"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"]):
        raise ValueError("连接文件缺少证书指纹")
    import ssl
    try:
        der = ssl.PEM_cert_to_DER_cert(value["certificate"])
        if hashlib.sha256(der).hexdigest() != value["fingerprint"]:
            raise ValueError("连接证书与指纹不符")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("连接证书无效") from exc
    addresses = value.get("addresses")
    if (not isinstance(addresses, list) or not 1 <= len(addresses) <= 16
            or any(not private_address(x, allow_loopback) for x in addresses)
            or type(value.get("port")) is not int or not 1024 <= value["port"] <= 65535):
        raise ValueError("连接文件需要有效的局域网 IPv4 地址和端口")
    return {k: value[k] for k in ("version", "kind", "controller_id", "worker_id", "worker_device", "name",
                                 "secret", "certificate", "fingerprint", "addresses", "port", "allow_send")}
