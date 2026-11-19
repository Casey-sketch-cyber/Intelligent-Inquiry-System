"""Build a device-bound private bootstrap; never put its authorization in Git."""
import hashlib
import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry.runtime import ROOT, DATA_DIR
from lanhost import config, live_trial
from lanhost.support_bootstrap import TARGETS


def build(folder, cfg, baseline, *, payload_overrides=None):
    folder.mkdir(parents=True)
    manifest = {"files": {}}
    for name in sorted(TARGETS):
        source = (payload_overrides or {}).get(name, (ROOT / name).read_bytes())
        target = folder / "payload" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)
        old = baseline / name
        manifest["files"][name] = {"old": hashlib.sha256(old.read_bytes()).hexdigest() if old.exists() else None,
                                   "new": hashlib.sha256(source).hexdigest()}
    manifest_file = folder / "upgrade-manifest.json"
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    now = time.time()
    body = {"kind": "tianxin-support-bootstrap-v1", "worker_id": cfg["worker_id"],
            "created_at": now, "expires_at": now+86400, "manifest_hash": hashlib.sha256(manifest_file.read_bytes()).hexdigest()}
    (folder / "authorization.json").write_text(json.dumps(body | {"signature": live_trial.sign(body, cfg["secret"])}), encoding="utf-8")
    shutil.copy2(ROOT / "lanhost/support_bootstrap.py", folder / "support_bootstrap.py")
    shutil.copy2(ROOT / "lanhost/upgrade_trial.py", folder / "upgrade_trial.py")
    (folder / "support_bootstrap.ps1").write_text((ROOT / "lanhost/support_bootstrap.ps1").read_text(encoding="utf-8"), encoding="utf-8-sig")
    (folder / "启用远程调试.cmd").write_bytes(b'@echo off\r\ncd /d "%~dp0"\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0support_bootstrap.ps1"\r\necho.\r\necho Keep this window if any error is shown.\r\npause\r\n')
    (folder / "使用说明.txt").write_text("在第二台 QQ 工作机的 D 盘解压，双击「启用远程调试.cmd」。\n出现 SUCCESS 后保留 QQ 和工作机服务运行；无需再次运行询价测试脚本。\n此包只安装已配对办公端使用的 QQ/专用 Edge 窗口诊断与应用源码更新入口。\n保留现有任务、数据库、登录配置与配对，安装前备份，不改变常规发送锁定。\n不会自动发送；后续继续原授权三型号、每款5000个、累计最多三家。\n授权绑定此工作机，24小时内有效。所有资料保存在 D 盘。\n", encoding="utf-8-sig")
    return manifest


def main():
    cfg = config.validate_connection(config.read_json(config.CONNECTION))
    previous = DATA_DIR / "lan/repairs/QQ工作机三家测试升级-20261022-162929/payload"
    if not previous.is_dir(): raise RuntimeError("Previous installed payload baseline not found")
    output = DATA_DIR / "lan/repairs" / ("QQ工作机远程调试接入-"+time.strftime("%Y%m%d-%H%M%S"))
    folder = output / "远程调试接入"
    build(folder, cfg, previous)
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for file in sorted(folder.rglob("*")):
            if file.is_file(): z.write(file, file.relative_to(output))
    with zipfile.ZipFile(archive) as z:
        if z.testzip(): raise RuntimeError("Archive verification failed")
    result = {"path": str(archive), "bytes": archive.stat().st_size, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    (DATA_DIR / "qa/support-bootstrap-delivery.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__": main()
