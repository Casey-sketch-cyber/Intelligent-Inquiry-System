"""多个进程共享的型号搜索额度，记录保存在业务数据库。"""

import math
import sqlite3
import time

from . import db, settings

WINDOW_SECONDS = 600
WAIT_STEP = 0.25


class SearchRateLimited(ValueError):
    """同步诊断不等待长时间搜索额度，提示稍后手动重试。"""


def _limits():
    pacing = settings.load_settings().get("pacing", {})
    interval = float(pacing.get("search_min_interval", 30))
    limit = pacing.get("search_max_per_10min", 10)
    if not math.isfinite(interval) or interval <= 0 or type(limit) is not int or limit <= 0:
        raise ValueError("搜索最短间隔和十分钟搜索上限必须大于 0")
    return interval, limit


def _reserve(part_no, page_no, source, interval, limit):
    with db.connect(timeout=WAIT_STEP) as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        recent = conn.execute(
            "SELECT created_at FROM search_attempt WHERE created_at > ? ORDER BY created_at DESC LIMIT ?",
            (now - max(WINDOW_SECONDS, interval), limit),
        ).fetchall()
        delay = max(0, recent[0][0] + interval - now) if recent else 0
        if len(recent) == limit:
            delay = max(delay, recent[-1][0] + WINDOW_SECONDS - now)
        if delay <= 0:
            conn.execute("INSERT INTO search_attempt(created_at,part_no,page_no,source) VALUES(?,?,?,?)",
                         (now, part_no, page_no, source))
        return delay


def wait_for_search(part_no, page_no, source, check, *, wait=True):
    """在等待和登记前检查暂停/校验状态，提交记录后才允许发起请求。"""
    interval, limit = _limits()
    while True:
        check()
        try:
            delay = _reserve(part_no, page_no, source, interval, limit)
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            delay = WAIT_STEP
        if delay <= 0:
            return
        if not wait:
            raise SearchRateLimited("IC交易网搜索正在限速，请稍后重试这项诊断")
        deadline = time.monotonic() + delay
        while (remaining := deadline - time.monotonic()) > 0:
            check()
            time.sleep(min(WAIT_STEP, remaining))
