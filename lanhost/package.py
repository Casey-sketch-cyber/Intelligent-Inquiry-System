"""双机离线部署包；默认不打包客户配置，迁移包仅属于生成它的企业。"""
import argparse
import email.parser
import hashlib
import json
import shutil
import sqlite3
import threading
import tomllib
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from inquiry.runtime import ROOT, DATA_DIR, require_d_path
from lanhost import config

BUILD_LOCK = threading.Lock()
PYTHON_INSTALLER = "python.3.12.10.nupkg"
PYTHON_SHA256 = "0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8"
QQ_INSTALLER = "QQ_9.9.36_260924_x64_01.exe"


def source_archive(path):
    files = [ROOT / name for name in ("main.py", "requirements.lock.txt", "settings.example.toml", ".env.example")]
    for folder in ("inquiry", "ui", "vmhost", "lanhost"):
        files.extend(p for p in (ROOT / folder).rglob("*") if p.is_file()
                     and p.suffix.lower() in (".py", ".js", ".css", ".html", ".ps1", ".cmd", ".vbs")
                     and "__pycache__" not in p.parts)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, file.relative_to(ROOT).as_posix())


def copy_seed(dest):
    dest.mkdir()
    for name in (".env", "settings.toml"):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, dest / name)
    source = DATA_DIR / "inquiry.db"
    if source.is_file():
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(dest / "inquiry.db") as dst:
                src.backup(dst)
    settings = ROOT / "settings.toml"
    if settings.is_file():
        name = tomllib.loads(settings.read_text(encoding="utf-8")).get("inquiry", {}).get("card_image", "")
        if name:
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("名片需使用项目内的相对路径，迁移前请先调整设置")
            source = require_d_path(ROOT / relative)
            if source.is_file():
                (dest / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest / relative)


def copy_materials(target):
    candidates = (ROOT / "offline", DATA_DIR / "lan/installers", DATA_DIR / "vm/setup")
    for name in (PYTHON_INSTALLER, QQ_INSTALLER):
        source = next((folder / name for folder in candidates if (folder / name).is_file()), None)
        if source is None:
            raise RuntimeError("缺少已核验的离线安装材料：" + name)
        if name == PYTHON_INSTALLER and hashlib.sha256(source.read_bytes()).hexdigest() != PYTHON_SHA256:
            raise ValueError("便携 Python 材料校验失败，未打包")
        shutil.copy2(source, target / name)
    wheel_dirs = [p / "wheels" for p in candidates if (p / "wheels").is_dir()]
    available = {}
    for folder in wheel_dirs:
        for wheel in folder.glob("*.whl"):
            with zipfile.ZipFile(wheel) as archive:
                meta = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
                fields = email.parser.Parser().parsestr(archive.read(meta).decode("utf-8"))
                available[(fields["Name"].lower().replace("_", "-"), fields["Version"])] = wheel
    dest = target / "wheels"
    dest.mkdir()
    for line in (ROOT / "requirements.lock.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, version = line.split("==")
        source = available.get((name.lower().replace("_", "-"), version))
        if source is None:
            raise RuntimeError("缺少锁定版本的离线依赖：" + line)
        shutil.copy2(source, dest / source.name)


def build(role="worker", with_data=False):
    if role not in ("office", "worker") or with_data and role != "worker":
        raise ValueError("仅工作机包可包含本企业迁移数据")
    if not BUILD_LOCK.acquire(blocking=False):
        raise ValueError("已有部署包正在生成，请稍候")
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        parent = require_d_path(DATA_DIR / "lan/packages")
        folder = parent / (role + "-" + stamp)
        folder.mkdir(parents=True)
        source_archive(folder / "application.zip")
        copy_materials(folder)
        shutil.copy2(ROOT / "lanhost/install.ps1", folder / "install.ps1")
        shutil.copy2(ROOT / "lanhost/install_helpers.ps1", folder / "install_helpers.ps1")
        shutil.copy2(ROOT / "lanhost/allow_connection.ps1", folder / "allow_connection.ps1")
        (folder / "role.json").write_text(json.dumps({"role": role, "private_migration": with_data}), encoding="utf-8")
        if role == "worker":
            office = config.controller()
            config.save_json(folder / "bootstrap.json", {"controller_id": office["controller_id"],
                                                         "office_device": office["office_device"]})
        if with_data:
            copy_seed(folder / "seed")
        title = "QQ工作机" if role == "worker" else "办公电脑"
        entry = '@echo off\r\ncd /d "%~dp0"\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"\r\nif errorlevel 1 pause\r\n'
        (folder / ("1_安装" + title + ".cmd")).write_bytes(entry.encode("ascii"))
        if role == "worker":
            rule = '@echo off\r\ncd /d "%~dp0"\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0allow_connection.ps1"\r\npause\r\n'
            (folder / "2_允许办公电脑连接.cmd").write_bytes(rule.encode("ascii"))
        text = ["天芯智能询价系统 · " + title + "部署包", "", "请在目标电脑 D 盘解压，运行 1_安装入口。",
                "系统安装到 D:\\TianxinWorker（工作机）或 D:\\TianxinOffice（办公电脑）；同包中断可续装，已有业务数据保留。",
                "工作机必须是另一台 Windows 电脑。需要本人登录 Windows、QQ 和 IC交易网；QQ 自身数据目录也需核验位于 D 盘。",
                "工作机安装后先运行 2_允许办公电脑连接，再将 data\\lan\\办公电脑连接.json 复制回办公电脑导入。",
                "工作机启动默认暂停；完成登录后再开始采集。本版本锁定真实发送。",
                "工作机重启后需要本人登录 Windows，程序随登录启动；不保存自动登录密码。",
                "本包含本企业历史及模型密钥，不能给其他客户。" if with_data else "此包不含客户历史数据库、模型密钥或 QQ 登录态。"]
        (folder / "部署说明.txt").write_text("\n".join(text) + "\n", encoding="utf-8-sig")
        manifest = {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in folder.rglob("*") if p.is_file()}
        config.save_json(folder / "manifest.json", manifest)
        name = title + "安装包" + ("-仅本企业迁移" if with_data else "") + "-" + stamp + ".zip"
        archive = parent / name
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for path in folder.rglob("*"):
                if path.is_file():
                    z.write(path, path.relative_to(folder).as_posix())
        return {"path": str(archive), "folder": str(folder), "private_migration": with_data,
                "bytes": archive.stat().st_size}
    finally:
        BUILD_LOCK.release()


def build_repair(target):
    """小补丁只更新同一离线包的安装脚本，原应用/迁移资料和运行目录不改写。"""
    target = require_d_path(Path(target))
    manifest = config.read_json(target / "manifest.json")
    archive = target / "application.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != manifest.get("application.zip"):
        raise ValueError("原安装包应用校验不通过，未生成修复工具")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    parent = require_d_path(DATA_DIR / "lan/repairs")
    folder = parent / ("repair-" + stamp)
    tool = folder / "安装修复工具"
    tool.mkdir(parents=True)
    for name in ("install.ps1", "install_helpers.ps1", "apply-repair.ps1"):
        shutil.copy2(ROOT / "lanhost" / name, tool / name)
    materials = (ROOT / "offline", DATA_DIR / "lan/installers")
    python = next((p / PYTHON_INSTALLER for p in materials if (p / PYTHON_INSTALLER).is_file()), None)
    if python is None:
        raise ValueError("缺少便携 Python 运行包，请先准备官方离线材料")
    if hashlib.sha256(python.read_bytes()).hexdigest() != PYTHON_SHA256:
        raise ValueError("便携 Python 材料校验失败，未生成修复工具")
    shutil.copy2(python, tool / PYTHON_INSTALLER)
    config.save_json(tool / "repair.json", {"version": 1, "application_sha256": digest,
        "files": {name: hashlib.sha256((tool / name).read_bytes()).hexdigest()
                  for name in ("install.ps1", "install_helpers.ps1", PYTHON_INSTALLER)}})
    entry = '@echo off\r\ncd /d "%~dp0"\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply-repair.ps1"\r\npause\r\n'
    (tool / "修复并继续安装.cmd").write_bytes(entry.encode("ascii"))
    (tool / "使用说明.txt").write_text(
        "将本文件夹「安装修复工具」放进第二台电脑原来解压的工作机安装包目录，\n"
        "使「安装修复工具」与 application.zip、manifest.json 在同一层。\n"
        "进入「安装修复工具」，双击「修复并继续安装.cmd」。不要删除 D:\\TianxinWorker。\n"
        "工具先核对原安装包，备份旧安装脚本，再继续未完成安装；保留已有数据库、设置和配对。\n"
        "本次包含独立 Python，解压在 D 盘，不再运行系统 Python 安装器。无需卸载已有 Python。\n"
        "如仍失败，保留新错误画面和 D:\\TianxinWorker\\data\\logs 的安装日志。\n"
        "此工具不操作 QQ，也不解除真实发送限制。\n", encoding="utf-8-sig")
    path = parent / ("QQ工作机安装修复-" + stamp + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for file in tool.iterdir():
            z.write(file, file.relative_to(folder).as_posix())
    return {"path": str(path), "folder": str(folder), "bytes": path.stat().st_size}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("office", "worker"), default="worker")
    parser.add_argument("--with-data", action="store_true")
    parser.add_argument("--repair-package", type=Path, help="为原解压安装包生成小修复工具")
    args = parser.parse_args()
    print(json.dumps(build_repair(args.repair_package) if args.repair_package else build(args.role, args.with_data), ensure_ascii=False))


if __name__ == "__main__":
    main()
