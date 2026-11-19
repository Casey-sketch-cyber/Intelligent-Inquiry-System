"""离线清空工作机询价历史：停服务、持双锁、先备份，不改配对和发送限额。

同时可作为小工具从工作机目录外运行，不需要覆盖已安装的源码。
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.request
import uuid
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

TABLES = ("analysis_job", "outbox", "inbox", "quote", "message", "supplier", "task")


def task_fingerprint(rows):
    fields = ("id", "part_no", "qty", "created_at")
    items = [{key: row[key] for key in fields} for row in sorted(rows, key=lambda r: r["id"])]
    return hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()


def check_tasks(conn, expected):
    if expected is None:
        return
    cursor = conn.execute("SELECT id,part_no,qty,created_at FROM task ORDER BY id")
    fields = [col[0] for col in cursor.description]
    if task_fingerprint([dict(zip(fields, row)) for row in cursor]) != expected:
        raise RuntimeError("Task list changed since this cleanup was prepared; no history was deleted")


def safe_path(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if root.drive.upper() != "D:" or not path.is_relative_to(root):
        raise RuntimeError("History cleanup must stay inside the D: worker directory")
    for parent in (path, *path.parents):
        if parent.exists() and getattr(parent.lstat(), "st_file_attributes", 0) & 0x400:
            raise RuntimeError("History cleanup does not follow junctions or symbolic links")
        if parent == root:
            break
    if path.resolve() != path or root.resolve() != root:
        raise RuntimeError("History cleanup does not follow redirected paths")
    return path


def clear_history(root, expected_fingerprint=None):
    """调用前必须退出工作机；双锁防止旧解析线程写入被复用的记录 ID。"""
    from ui import service
    root = safe_path(root, root)
    data = safe_path(root / "data", root)
    database = safe_path(data / "inquiry.db", root)
    if not database.is_file():
        raise RuntimeError("Business database not found; nothing was created or deleted")
    with ExitStack() as stack:
        for name in ("ui_server.lock", "runner.lock"):
            handle = service.acquire_lock(safe_path(data / name, root))
            if handle is None:
                raise RuntimeError("Worker or inquiry runner is still active; history was kept")
            stack.enter_context(handle)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)) as check:
            check_tasks(check, expected_fingerprint)
        backup = safe_path(data / "backups" / ("inquiry-reset-" + datetime.now().strftime("%Y%m%d-%H%M%S")
                                                + "-" + uuid.uuid4().hex[:8]), root)
        backup.mkdir(parents=True)
        reports = safe_path(data / "reports", root)
        report_files = sorted(p for p in reports.iterdir() if p.is_file()) if reports.exists() else []
        for file in report_files:
            safe_path(file, root)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
            source.execute("PRAGMA temp_store=MEMORY")
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Business database integrity check failed; history was kept")
            before = {table: source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in (*TABLES, "send_attempt")}
            with closing(sqlite3.connect(backup / "inquiry.db")) as target:
                source.backup(target)
        # 必须验证可恢复的 SQLite 备份，所有报告也复制成功后，才开始清空。
        with closing(sqlite3.connect(backup / "inquiry.db")) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup verification failed; history was kept")
            if before != {t: check.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                          for t in (*TABLES, "send_attempt")}:
                raise RuntimeError("Backup counts do not match; history was kept")
        if report_files:
            (backup / "reports").mkdir()
            for file in report_files:
                target = backup / "reports" / file.name
                shutil.copy2(file, target)
                if hashlib.sha256(file.read_bytes()).digest() != hashlib.sha256(target.read_bytes()).digest():
                    raise RuntimeError("Report backup verification failed; history was kept")
        result = {"version": 1, "backup": str(backup), "state": "backup_ready", "before": before,
                  "reports_backed_up": len(report_files), "allow_send": False}
        manifest = backup / "result.json"
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        with closing(sqlite3.connect(database, timeout=30)) as current, current:
            current.execute("PRAGMA temp_store=MEMORY")
            current.execute("PRAGMA foreign_keys=ON")
            current.execute("BEGIN IMMEDIATE")
            for table in TABLES:
                current.execute(f"DELETE FROM {table}")
            if current.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("History relation check failed; changes were rolled back")
            after = {table: current.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in (*TABLES, "send_attempt")}
            if any(after[t] for t in TABLES) or after["send_attempt"] != before["send_attempt"]:
                raise RuntimeError("History verification failed; changes were rolled back")
        report_errors = []
        for file in report_files:
            try:
                safe_path(file, root).unlink()
            except OSError:
                report_errors.append(file.name)
        result.update(state="cleared", after=after, reports_not_removed=report_errors)
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result


def worker_ui(root, cfg, allow_sending=False):
    from lanhost import config
    saved = config.read_json(root / "data/lan/worker-ui.json")
    parsed = urlsplit(saved["url"])
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/"
            or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise RuntimeError("Worker local panel URL is invalid")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(route, method="GET"):
        req = urllib.request.Request(saved["url"] + route, method=method,
                                     headers={"Origin": saved["url"].rstrip("/")})
        with opener.open(req, timeout=4) as response:
            value = json.load(response)
        if route == "api/status" and (value.get("worker_id") != cfg["worker_id"]
                                      or type(value.get("allow_send")) is not bool
                                      or not allow_sending and value.get("allow_send") is not False):
            raise RuntimeError("Worker identity or read-only state does not match")
        return value
    return parsed.port, saved["port"], call


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--phase", required=True, choices=("pause", "clear", "verify"))
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    root = safe_path(args.root, args.root)
    os.chdir(root)
    sys.path.insert(0, str(root))
    from inquiry.runtime import ROOT
    from lanhost import config
    if root != ROOT:
        raise RuntimeError("Loaded application is not the requested worker directory")
    cfg = config.worker_config()  # 拒绝办公电脑、异机复制或已开放真实发送的环境。
    authorization = config.read_json(args.request)
    fingerprint = authorization.get("task_fingerprint", "")
    if (authorization.get("kind") != "tianxin-history-reset" or authorization.get("worker_id") != cfg["worker_id"]
            or not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in fingerprint)):
        raise RuntimeError("Cleanup request is not for this worker and task snapshot")
    if args.phase == "clear":
        result = clear_history(root, fingerprint)
    else:
        if args.phase == "pause":
            database = safe_path(root / "data/inquiry.db", root)
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
                check_tasks(conn, fingerprint)
        port, rpc_port, call = worker_ui(root, cfg)
        call("api/status")
        if args.phase == "pause":
            call("api/pause", "POST")
        deadline = time.monotonic() + 40
        while True:
            status = call("api/status")
            if status.get("manual_ready") is True and status.get("running") is False:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Worker did not become paused and idle; no cleanup was attempted")
            time.sleep(1)
        executables = [str(safe_path(Path(value), root))
                       for value in {sys.executable, sys._base_executable}]
        result = {"ui_port": port, "rpc_port": rpc_port, "manual_ready": True, "allow_send": False,
                  "process_executables": executables}
        if args.phase == "verify":
            with closing(sqlite3.connect(root / "data/inquiry.db")) as conn:
                result["tasks"] = conn.execute("SELECT COUNT(*) FROM task").fetchone()[0]
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
