"""生成本机虚拟机初始化材料。默认不包含任何用户数据或账号配置。"""
import argparse
import hashlib
import json
import shutil
import sqlite3
import tomllib
import zipfile
from pathlib import Path

from inquiry.runtime import ROOT, DATA_DIR, require_d_path

SETUP = DATA_DIR / "vm" / "setup"


def build(with_data=False):
    SETUP.mkdir(parents=True, exist_ok=True)
    source = SETUP / "application.zip"
    files = [ROOT / n for n in ("main.py", "requirements.lock.txt", "settings.example.toml", ".env.example")]
    for directory in ("inquiry", "ui", "vmhost"):
        files.extend(p for p in (ROOT / directory).rglob("*")
                     if p.is_file() and p.suffix in (".py", ".js", ".css", ".html", ".ps1", ".vbs", ".cmd")
                     and "__pycache__" not in p.parts)
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files:
            if path.is_file():
                z.write(path, path.relative_to(ROOT).as_posix())
    shutil.copy2(ROOT / "vmhost" / "install_guest.ps1", SETUP / "install_guest.ps1")
    shutil.copy2(ROOT / "vmhost" / "InstallGuest.cmd", SETUP / "InstallGuest.cmd")
    shutil.copy2(ROOT / "requirements.lock.txt", SETUP / "requirements.lock.txt")
    seed = SETUP / "seed"
    if seed.exists() and not with_data:
        raise RuntimeError("目录中已有本机迁移资料；不能把此目录作为无客户数据的交付包")
    if with_data:
        # 不复制浏览器、QQ 登录态或操作系统用户配置。历史库仅通过 SQLite backup 取得一致副本。
        seed.mkdir(exist_ok=True)
        for name in ("settings.toml", ".env"):
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, seed / name)
        dbpath = DATA_DIR / "inquiry.db"
        if dbpath.is_file():
            target = seed / "inquiry.db"
            target.unlink(missing_ok=True)
            with sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
                src.backup(dst)
        settings = tomllib.loads((ROOT / "settings.toml").read_text(encoding="utf-8"))
        card = settings.get("inquiry", {}).get("card_image", "")
        if card:
            original = require_d_path(ROOT / card)
            # 使用工作目录内的相对路径，不能把宿主绝对路径带到来宾。
            if original.is_file() and not Path(card).is_absolute() and ".." not in Path(card).parts:
                dest = seed / card
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, dest)
    # 配对凭据由 create 写入，只属于本机 VM，不进入源码仓库。
    manifest = {p.relative_to(SETUP).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in SETUP.rglob("*") if p.is_file() and p.name not in ("manifest.json", "pairing.json")}
    (SETUP / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(SETUP), "files": len(manifest), "private_migration": with_data}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-data", action="store_true", help="仅供本用户迁移，包含私有配置与历史，不可交付给其他客户")
    parser.add_argument("--iso", help="D 盘 Windows x64 ISO；提供时创建本项目 VM，但不自动启动")
    parser.add_argument("--create", action="store_true", help="创建虚拟机；尚无 ISO 时保留空光驱等待安装")
    args = parser.parse_args()
    print(json.dumps(build(args.with_data), ensure_ascii=False))
    if args.iso or args.create:
        from vmhost import manager
        if args.iso and manager.CONFIG.exists():
            result = manager.mount_windows(args.iso)
        else:
            result = manager.create(args.iso)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
