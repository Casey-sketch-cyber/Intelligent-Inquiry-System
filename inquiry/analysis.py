"""单线程处理持久化解析任务；QQ 读取无需等待模型返回。"""
import json
import threading
import time
import tomllib

from . import db, llm, workbench
from .settings import ROOT

_thread = None
_lock = threading.Lock()


def process_one():
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM analysis_job WHERE status='queued' AND next_at<=? ORDER BY next_at LIMIT 1", (time.time(),)).fetchone()
        if row is None:
            return False
        job = dict(row)
        # 时间租约允许进程意外退出后恢复，不让两个解析线程处理同一条。
        c.execute("UPDATE analysis_job SET next_at=? WHERE supplier_id=?", (time.time() + 180, job["supplier_id"]))
    sid = job["supplier_id"]
    record = workbench.conversation(sid)
    replies = db.messages(sid, "in")
    outgoing = db.messages(sid, "out")
    try:
        cfg = tomllib.loads((ROOT / "settings.toml").read_text(encoding="utf-8"))
        q = llm.parse_reply(record["part_no"], record["qty"], outgoing[0] if outgoing else "", replies,
                            model=cfg.get("llm", {}).get("model"))
        suggestions = q.pop("reply_suggestions", [])
        with db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT digest FROM analysis_job WHERE supplier_id=?", (sid,)).fetchone()
            if not current or current[0] != job["digest"] or workbench.reply_digest(sid) != job["digest"]:
                return True
            valid = q["status"] == "quoted" and q["unit_price"] is not None
            c.execute("INSERT INTO quote(supplier_id,data_json,valid,updated_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(supplier_id) DO UPDATE SET data_json=excluded.data_json,valid=excluded.valid,updated_at=excluded.updated_at",
                      (sid, json.dumps(q, ensure_ascii=False), int(valid), time.time()))
            status = {"quoted": "quoted", "no_stock": "no_stock", "asking": "asking"}.get(q["status"], "sent")
            c.execute("UPDATE supplier SET status=?,parsed_reply_hash=? WHERE id=?", (status, job["digest"], sid))
            c.execute("UPDATE analysis_job SET status='done',suggestions=?,error=NULL WHERE supplier_id=? AND digest=?",
                      (json.dumps(suggestions, ensure_ascii=False), sid, job["digest"]))
    except Exception as e:
        attempt = job["attempts"] + 1
        with db.connect() as c:
            c.execute("UPDATE analysis_job SET attempts=?,status=?,next_at=?,error=? WHERE supplier_id=? AND digest=?",
                      (attempt, "failed" if attempt >= 3 else "queued", time.time() + min(300, 30 * 2 ** attempt),
                       str(e)[:300], sid, job["digest"]))
    return True


def start():
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        def loop():
            while True:
                try:
                    if process_one():
                        continue
                except Exception:
                    pass
                time.sleep(2)
        _thread = threading.Thread(target=loop, daemon=True, name="reply-analysis")
        _thread.start()
