"""工作机小升级：核对设备与文件版本、停服备份、更新有限测试入口，不清任务。"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request
import uuid
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

try:
    from .reset_history import safe_path, worker_ui, check_tasks
    from .live_trial import validate_permit, grant_foreground
except ImportError:
    from reset_history import safe_path, worker_ui, check_tasks
    from live_trial import validate_permit, grant_foreground

TARGETS = {"inquiry/isolation.py", "inquiry/qq.py", "inquiry/runner.py", "lanhost/live_trial.py", "lanhost/reset_history.py",
           "lanhost/worker.py", "lanhost/worker.html", "vmhost/guest.py", "lanhost/panel.html"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_files(root, package, manifest):
    if set(manifest.get("files", {})) != TARGETS:
        raise ValueError("Upgrade file list is invalid")
    for name, hashes in manifest["files"].items():
        source = safe_path(package / "payload" / name, package)
        target = safe_path(root / name, root)
        if digest(source) != hashes["new"]:
            raise ValueError("Upgrade payload checksum mismatch: " + name)
        actual = digest(target) if target.exists() else None
        if actual not in (hashes["old"], hashes["new"]):
            raise ValueError("Installed source changed; no files overwritten: " + name)


@contextmanager
def _open_database(path, *, read_only=False):
    from inquiry import db

    guard = getattr(db, "open_existing", None)
    if guard is not None:
        with guard(path, read_only=read_only) as conn:
            yield conn
        return
    # 升级包可能只带此脚本。旧工作机通过文件头确认使用回滚日志后才连接。
    with path.open("rb") as file:
        versions = file.read(20)[18:20]
    error = "Installed worker must be updated before this tool can open the business database"
    if versions != b"\x01\x01":
        raise RuntimeError(error)
    mode = "ro" if read_only else "rw"
    with closing(db.sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=30)) as conn:
        if conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
            raise RuntimeError(error)
        yield conn


def install(root, package, manifest):
    from inquiry import db
    from ui import service
    with ExitStack() as stack:
        for name in ("ui_server.lock", "runner.lock"):
            lock = service.acquire_lock(root / "data" / name)
            if lock is None:
                raise RuntimeError("Worker or QQ operation is still active; no source was updated")
            stack.enter_context(lock)
        validate_files(root, package, manifest)
        with _open_database(root / "data/inquiry.db", read_only=True) as src:
            backup = safe_path(root / "data/backups" / ("trial-upgrade-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]), root)
            backup.mkdir(parents=True)
            with closing(db.sqlite3.connect(backup / "inquiry.db")) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise RuntimeError("Database backup could not be stored as a standalone file")
        with _open_database(backup / "inquiry.db", read_only=True) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Database backup verification failed; no source was updated")
        originals = {}
        for name in sorted(TARGETS):
            target = root / name
            originals[name] = target.is_file()
            if target.is_file():
                dest = backup / "source" / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, dest)
                if digest(dest) != digest(target):
                    raise RuntimeError("Source backup verification failed")
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        changed = []
        try:
            for name in sorted(TARGETS):
                target = root / name
                temp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    shutil.copy2(package / "payload" / name, temp)
                    if digest(temp) != manifest["files"][name]["new"]:
                        raise RuntimeError("Written source checksum mismatch")
                    temp.replace(target)
                    changed.append(name)
                finally:
                    temp.unlink(missing_ok=True)
        except BaseException:
            for name in reversed(changed):
                target = safe_path(root / name, root)
                if originals[name]:
                    shutil.copy2(backup / "source" / name, target)
                else:
                    target.unlink()
            raise
        return {"backup": str(backup), "updated_files": len(changed)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--package", required=True, type=Path)
    p.add_argument("--phase", required=True, choices=("prepare", "install", "verify", "launch"))
    args = p.parse_args()
    root, package = safe_path(args.root, args.root), safe_path(args.package, args.package)
    os.chdir(root)
    sys.path.insert(0, str(root))
    from inquiry.runtime import ROOT
    from lanhost import config
    if root != ROOT:
        raise ValueError("Worker application directory does not match")
    cfg = config.worker_config()
    permit_file = config.read_json(package / "live-permit.json")
    permit = validate_permit(permit_file, cfg)
    if permit["kind"] != "tianxin-live-trial-v2":
        raise ValueError("Continuation authorization is required")
    manifest = config.read_json(package / "upgrade-manifest.json")
    if args.phase in ("prepare", "install"):
        validate_files(root, package, manifest)
        with _open_database(root / "data/inquiry.db", read_only=True) as conn:
            check_tasks(conn, permit["task_fingerprint"])
    if args.phase == "install":
        value = install(root, package, manifest)
    else:
        port, rpc_port, call = worker_ui(root, cfg)
        status = call("api/status")
        if args.phase == "prepare":
            call("api/pause", "POST")
            end = time.monotonic() + 40
            while not status.get("manual_ready"):
                if time.monotonic() >= end:
                    raise RuntimeError("Worker is not paused and idle; no files were changed")
                time.sleep(1)
                status = call("api/status")
            value = {"ui_port": port, "rpc_port": rpc_port,
                     "process_executables": [str(safe_path(Path(x), root)) for x in {sys.executable, sys._base_executable}]}
        else:
            if status.get("trial_capability") != 2 or not status.get("manual_ready"):
                raise RuntimeError("Upgraded worker is not ready and paused")
            value = {"verified": True, "rpc_port": rpc_port}
            if args.phase == "launch":
                # 用户主动运行的本地工具把前台激活资格交给已核对的工作机服务，服务再交给测试子进程。
                grant_foreground(status["service_pid"])
                saved = config.read_json(root / "data/lan/worker-ui.json")
                req = urllib.request.Request(saved["url"] + "api/live-trial", method="POST", data=json.dumps(permit_file).encode(),
                                             headers={"Origin": saved["url"].rstrip("/"), "Content-Type": "application/json"})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=10) as response:
                    value = json.load(response)
                if value.get("request_id") != permit["request_id"] or not value.get("accepted"):
                    raise RuntimeError("Trial acceptance was not verified; do not retry automatically")
    print(json.dumps(value, ensure_ascii=True))


if __name__ == "__main__":
    main()
