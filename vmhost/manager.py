"""VirtualBox 生命周期。只管理本项目登记的 UUID，所有文件位于 D 盘。"""
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from inquiry.runtime import ROOT, DATA_DIR, require_d_path

VM_ROOT = DATA_DIR / "vm"
CONFIG = VM_ROOT / "machine.json"


def environment():
    env = dict(os.environ)
    env["VBOX_USER_HOME"] = str(require_d_path(VM_ROOT / "virtualbox-config"))
    Path(env["VBOX_USER_HOME"]).mkdir(parents=True, exist_ok=True)
    return env


def executable():
    for path in (Path(r"D:\VirtualBox\VBoxManage.exe"), Path(r"D:\software\VirtualBox\VBoxManage.exe")):
        if path.is_file():
            return path
    path = shutil.which("VBoxManage")
    if path:
        return Path(path)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Oracle\VirtualBox") as key:
            path = Path(winreg.QueryValueEx(key, "InstallDir")[0]) / "VBoxManage.exe"
            if path.is_file():
                return path
    except OSError:
        pass
    raise RuntimeError("尚未安装 VirtualBox，请先完成运行组件安装")


def command(*args, timeout=40):
    result = subprocess.run([str(executable()), *map(str, args)], cwd=ROOT, env=environment(),
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("虚拟机操作失败：" + result.stderr.strip()[-1200:])
    return result.stdout


def read_config():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    uuid.UUID(cfg["uuid"])
    if require_d_path(Path(cfg["folder"])) != require_d_path(VM_ROOT / "machines" / cfg["name"]):
        raise RuntimeError("虚拟机目录与本项目登记信息不符")
    if len(cfg["secret"]) < 48:
        raise RuntimeError("配对配置不完整")
    if require_d_path(Path(cfg["bridge"])) != require_d_path(VM_ROOT / "bridge"):
        raise RuntimeError("共享目录与本项目不符")
    return cfg


def parse_info(text):
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip('"')] = value.strip('"')
    return out


def info():
    if not CONFIG.exists():
        return {"state": "not_created", "text": "尚未创建运行环境"}
    cfg = read_config()
    data = parse_info(command("showvminfo", cfg["uuid"], "--machinereadable"))
    if data.get("UUID", "").lower() != cfg["uuid"].lower():
        raise RuntimeError("虚拟机身份不匹配，操作已中止")
    expected = Path(cfg["folder"]) / (cfg["name"] + ".vbox")
    if require_d_path(Path(data.get("CfgFile", ""))) != require_d_path(expected):
        raise RuntimeError("虚拟机配置位置不匹配")
    return {"state": data.get("VMState", "unknown"), "name": cfg["name"],
            "memory_mb": int(data.get("memory", 0)), "cpus": int(data.get("cpus", 0)),
            "installation_media": bool(cfg.get("iso")),
            "text": "虚拟机已建好，等待 Windows ISO；尚未安装系统。" if not cfg.get("iso") else ""}


def create(iso=None):
    import secrets
    if iso is not None:
        iso = require_d_path(Path(iso))
        if not iso.is_file() or iso.suffix.lower() != ".iso":
            raise ValueError("请选择 D 盘上的 Windows 安装 ISO")
    if CONFIG.exists():
        return info()
    VM_ROOT.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(VM_ROOT).free < 50 * 1024**3:
        raise RuntimeError("D 盘至少需要 50GB 当前可用空间")
    machine_id = str(uuid.uuid4())
    name = "TianxinQQ-" + machine_id[:8]
    folder = require_d_path(VM_ROOT / "machines" / name)
    if folder.exists():
        raise RuntimeError("目标目录已存在，未覆盖")
    bridge = VM_ROOT / "bridge"
    export = VM_ROOT / "setup"
    bridge.mkdir(parents=True, exist_ok=True)
    export.mkdir(parents=True, exist_ok=True)
    cfg = {"uuid": machine_id, "name": name, "folder": str(folder), "iso": str(iso) if iso else None,
           "secret": secrets.token_hex(32), "bridge": str(bridge), "memory_mb": 8192, "cpus": 4}
    # 配置预先落盘：中途失败保留现场，不创建重复机器或删除磁盘。
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        command("createvm", "--name", name, "--uuid", machine_id, "--ostype", "Windows11_64",
                "--basefolder", VM_ROOT / "machines", "--register")
        command("modifyvm", machine_id, "--memory", 8192, "--cpus", 4, "--firmware", "efi",
                "--hardware-uuid", machine_id,
                "--tpm-type", "2.0", "--graphicscontroller", "vboxsvga", "--vram", 128,
                "--accelerate-3d", "off", "--clipboard-mode", "disabled", "--drag-and-drop", "disabled",
                "--nic1", "nat", "--audio-enabled", "off", "--boot1", "dvd", "--boot2", "disk")
        command("createmedium", "disk", "--filename", folder / "system.vdi", "--size", 65536, "--format", "VDI", timeout=120)
        command("createmedium", "disk", "--filename", folder / "data.vdi", "--size", 36864, "--format", "VDI", timeout=120)
        command("storagectl", machine_id, "--name", "SATA", "--add", "sata", "--controller", "IntelAhci")
        for port, filename in enumerate(("system.vdi", "data.vdi")):
            command("storageattach", machine_id, "--storagectl", "SATA", "--port", port,
                    "--device", 0, "--type", "hdd", "--medium", folder / filename)
        command("storageattach", machine_id, "--storagectl", "SATA", "--port", 2, "--device", 0,
                "--type", "dvddrive", "--medium", iso or "emptydrive")
        command("sharedfolder", "add", machine_id, "--name", "TianxinSetup", "--hostpath", export, "--readonly")
        command("sharedfolder", "add", machine_id, "--name", "TianxinBridge", "--hostpath", bridge)
        (export / "pairing.json").write_text(json.dumps({
            "vm_uuid": machine_id, "secret": cfg["secret"], "bridge": r"\\VBOXSVR\TianxinBridge",
            "allow_send": False,
        }), encoding="utf-8")
        return info()
    except Exception as exc:
        cfg["setup_error"] = str(exc)
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        raise


def mount_windows(iso):
    iso = require_d_path(Path(iso))
    if not iso.is_file() or iso.suffix.lower() != ".iso":
        raise ValueError("请选择 D 盘上的 Windows ISO")
    cfg = read_config()
    if info()["state"] != "poweroff":
        raise RuntimeError("请先正常关闭运行环境，再挂载 Windows 安装镜像")
    command("storageattach", cfg["uuid"], "--storagectl", "SATA", "--port", 2,
            "--device", 0, "--type", "dvddrive", "--medium", iso)
    cfg["iso"] = str(iso)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return info()


def start(show=False):
    cfg = read_config()
    if not cfg.get("iso"):
        raise RuntimeError("尚未准备 Windows 安装镜像，请先完成镜像下载和挂载")
    state = info()["state"]
    if state in ("poweroff", "saved", "aborted"):
        command("startvm", cfg["uuid"], "--type", "separate" if show else "headless", timeout=60)
    elif state == "running":
        if show:
            # 只显示已经运行的本项目 VM，不启动第二份 QQ。
            gui = executable().with_name("VirtualBoxVM.exe")
            subprocess.Popen([str(gui), "--startvm", cfg["uuid"], "--separate"], cwd=ROOT, env=environment())
    else:
        raise RuntimeError(f"运行环境状态为 {state}，请先处理再启动")
    return info()


def mount_tools():
    """挂载已安装版本自带的 Guest Additions；不下载扩展包。"""
    cfg = read_config()
    state = info()["state"]
    if state not in ("poweroff", "running"):
        raise RuntimeError("请先正常关机或启动运行环境")
    iso = require_d_path(executable().with_name("VBoxGuestAdditions.iso"))
    if not iso.is_file():
        raise RuntimeError("未找到 VirtualBox 自带的来宾组件镜像")
    command("storageattach", cfg["uuid"], "--storagectl", "SATA", "--port", 2,
            "--device", 0, "--type", "dvddrive", "--medium", iso)
    return {"text": "已挂载辅助组件。在运行窗口的光驱中运行 VBoxWindowsAdditions.exe，按提示完成后重启。"}


def shutdown(bridge, timeout=60):
    """先停业务并等 runner 锁释放，再发 ACPI 正常关机；不提供强制断电。"""
    cfg = read_config()
    state = info()["state"]
    if state == "poweroff":
        return {"state": state, "text": "已关机，运行资源已释放"}
    if state != "running":
        raise RuntimeError("当前状态无法正常关机，请打开运行窗口检查")
    status, _, body = bridge.call("POST", "/api/vm/prepare-shutdown", timeout=15)
    if status != 200:
        raise RuntimeError("未确认任务已停止，虚拟机继续运行：" + body.decode("utf-8", errors="replace")[:300])
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        heartbeat = bridge.status()
        if heartbeat.get("online") and heartbeat.get("shutdown_ready"):
            command("controlvm", cfg["uuid"], "acpipowerbutton")
            return {"state": "stopping", "text": "已请求正常关机；显示已关机后运行资源才释放"}
        time.sleep(0.5)
    raise RuntimeError("仍有任务未退出，已保留虚拟机运行。请查看任务状态后重试")
