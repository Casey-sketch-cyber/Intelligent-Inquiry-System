"""Local, structured telemetry. Only explicitly allowed diagnostics reach disk."""

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from . import db

LABELS = {
    "captcha": "网站验证码",
    "login_required": "需要登录网站",
    "qq_launch_failed": "QQ 会话打开失败",
    "temp_session_refused": "QQ 临时会话被拒绝",
    "identity_unverified": "供应商身份待核对",
    "send_uncertain": "QQ 发送结果待核实",
    "uia_control_missing": "QQ 控件缺失",
    "read_incomplete": "回复读取不完整",
    "llm_parse_failed": "模型解析失败",
    "llm_timeout": "模型请求超时",
    "suggestion_edited": "编辑建议后提交",
    "suggestion_rejected": "明确不采用建议",
}
STAGES = {"search", "queue", "qq_launch", "inquiry_send", "collection", "qq_controls",
          "analysis", "reply_send", "suggestions", "recovery"}
SEVERITIES = {"info", "warning", "error"}
CONTROLS = {"qq_window", "editor", "send_button", "message_list", "conversation_list"}
logger = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _file_version(path, modified_ns):
    import win32api

    info = win32api.GetFileVersionInfo(str(path), "\\")
    major_minor, build_revision = info["FileVersionMS"], info["FileVersionLS"]
    return f"{major_minor >> 16}.{major_minor & 0xffff}.{build_revision >> 16}.{build_revision & 0xffff}"


def _version(path):
    try:
        path = Path(path)
        return _file_version(path, path.stat().st_mtime_ns)
    except Exception:
        return None


@lru_cache(maxsize=1)
def _qq_version(time_bucket):
    try:
        import win32gui
        import win32process

        from .qq import exe_of

        pids = set()

        def collect(hwnd, _):
            pids.add(win32process.GetWindowThreadProcessId(hwnd)[1])
            return True

        win32gui.EnumWindows(collect, None)
        for pid in sorted(pids):
            path = Path(exe_of(pid))
            if path.name.casefold() == "qq.exe":
                version = _version(path)
                if version:
                    return version
    except Exception:
        pass
    return None


def _versions():
    from .browser import EDGE_CANDIDATES

    edge = next((version for path in EDGE_CANDIDATES if (version := _version(path))), None)
    return {"qq_version": _qq_version(int(time.monotonic() // 15)), "edge_version": edge}


def _salt(directory):
    path = directory / ".salt"
    try:
        salt = path.read_bytes()
    except FileNotFoundError:
        salt = secrets.token_bytes(32)
        with path.open("xb") as handle:
            handle.write(salt)
    if len(salt) != 32:
        raise ValueError("Invalid incident salt")
    return salt


def _subject_hash(salt, field, value):
    return hmac.new(salt, f"{field}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _counts(conn, table, now):
    # The only callers use the two fixed attempt table names below.
    recent, hour = conn.execute(
        f"SELECT COALESCE(SUM(created_at > ?), 0), COUNT(*) FROM {table} WHERE created_at > ? AND created_at <= ?",
        (now - 600, now - 3600, now),
    ).fetchone()
    return {"10m": recent, "1h": hour}


def record(kind, stage, *, severity, task_id=None, supplier_id=None, error=None, extra=None):
    """Store a sanitized incident and append it to the local date's JSONL file."""
    if kind not in LABELS or stage not in STAGES or severity not in SEVERITIES:
        raise ValueError("Unknown incident type, stage or severity")
    now = time.time()
    date = datetime.fromtimestamp(now).date()
    directory = db.DB_PATH.parent / "incidents"
    event = {"created_at": now, "date": date.isoformat(), "kind": kind, "stage": stage, "severity": severity}
    event.update(_versions())
    if isinstance(error, Exception):
        event["exception"] = "timeout" if _is_timeout(error) else "error"
    if isinstance(extra, dict) and type(extra.get("control")) is str and extra["control"] in CONTROLS:
        event["control"] = extra["control"]
    try:
        with db.connect(timeout=2) as conn:
            conn.execute("BEGIN IMMEDIATE")
            directory.mkdir(parents=True, exist_ok=True)
            if type(task_id) is int and task_id > 0:
                event["task_id"] = task_id
            dedupe = None
            if type(supplier_id) is int and supplier_id > 0:
                event["supplier_id"] = supplier_id
                row = conn.execute("SELECT task_id, company, qq_uin FROM supplier WHERE id=?", (supplier_id,)).fetchone()
                salt = _salt(directory)
                if row:
                    event["task_id"] = row["task_id"]
                    for field in ("company", "qq_uin"):
                        if row[field]:
                            event[f"{field}_hash"] = _subject_hash(salt, field, row[field])
                if kind == "suggestion_rejected" and isinstance(extra, dict):
                    digest = extra.get("suggestion_digest")
                    if type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest):
                        dedupe = _subject_hash(salt, kind, f"{supplier_id}:{digest}")
            event["searches"] = _counts(conn, "search_attempt", now)
            event["sends"] = _counts(conn, "send_attempt", now)
            payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            inserted = conn.execute(
                "INSERT INTO incident(created_at,occurred_date,kind,stage,severity,payload,dedupe_key) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO NOTHING",
                (now, date.isoformat(), kind, stage, severity, payload, dedupe),
            )
            if not inserted.rowcount:
                return
            try:
                with (directory / f"{date:%Y%m%d}.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload + "\n")
            except OSError:
                logger.warning("Incident JSONL could not be written")
    except (OSError, db.sqlite3.Error, ValueError):
        logger.warning("Incident could not be stored")


def _is_timeout(error):
    from openai import APITimeoutError

    return isinstance(error, (TimeoutError, APITimeoutError))


def record_parse_failure(stage, error, *, task_id=None, supplier_id=None):
    kind = "llm_timeout" if _is_timeout(error) else "llm_parse_failed"
    record(kind, stage, severity="warning", task_id=task_id, supplier_id=supplier_id, error=error)


def summary(days=14):
    if not 1 <= days <= 90:
        raise ValueError("运行问题统计支持最近 1–90 天")
    start = datetime.now().date() - timedelta(days=days - 1)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT occurred_date, kind, COUNT(*) AS count FROM incident WHERE occurred_date>=? "
            "GROUP BY occurred_date, kind ORDER BY occurred_date DESC, kind",
            (start.isoformat(),),
        ).fetchall()
    totals = {}
    by_date = []
    for row in rows:
        kind = row["kind"]
        if kind in LABELS:
            by_date.append({"date": row["occurred_date"], "kind": kind, "label": LABELS[kind], "count": row["count"]})
            totals[kind] = totals.get(kind, 0) + row["count"]
    by_type = [{"kind": kind, "label": LABELS[kind], "count": count} for kind, count in sorted(totals.items())]
    return {"days": days, "by_type": by_type, "by_date": by_date}
