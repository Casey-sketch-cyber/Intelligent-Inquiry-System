"""型号工作台、可靠回复队列。此模块只处理数据，不操作 QQ。"""
import hashlib
import json
import re
import time
import uuid

from . import db


def part_key(value):
    return "".join(str(value).upper().split())


def validate_parts(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("每批请填写 1–100 个型号")
    result, seen = [], set()
    for item in items:
        part = part_key(item.get("part", ""))
        qty_text = str(item.get("qty", "")).strip().lower().replace(",", "")
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(k|千|w|万)?", qty_text)
        if not part or len(part) > 100 or not re.fullmatch(r"[A-Z0-9][A-Z0-9._/()+#-]*", part):
            raise ValueError(f"型号格式不正确：{part[:100]}")
        value = float(match[1]) * {"k": 1000, "千": 1000, "w": 10000, "万": 10000}.get(match[2], 1) if match else 0
        if not 0 < value <= 1_000_000_000 or value != int(value):
            raise ValueError(f"{part} 的数量须为正整数")
        if part in seen:
            raise ValueError(f"型号 {part} 重复，请合并后再提交")
        seen.add(part)
        result.append((part, int(value)))
    return result


def validate_template(template):
    from string import Formatter
    fields = []
    try:
        for _, field, spec, conversion in Formatter().parse(template):
            if field is not None:
                if field not in {"part", "qty", "qtyk"} or spec or conversion:
                    raise ValueError()
                fields.append(field)
    except (ValueError, TypeError):
        raise ValueError("话术只支持 {part}、{qty}、{qtyk} 占位符")
    if "part" not in fields or not {"qty", "qtyk"}.intersection(fields):
        raise ValueError("话术必须包含 {part} 和 {qty} 或 {qtyk}，避免型号或数量发错")
    if len(template) > 2000:
        raise ValueError("话术最多 2000 字")


def reply_digest(sid):
    return hashlib.sha256(json.dumps(db.messages(sid, "in"), ensure_ascii=False).encode()).hexdigest()


def conversation(sid):
    with db.connect() as c:
        row = c.execute("SELECT s.*, t.part_no, t.qty FROM supplier s JOIN task t ON t.id=s.task_id WHERE s.id=?", (sid,)).fetchone()
    if not row:
        raise ValueError("供应商记录不存在")
    return dict(row)


def queue_reply(sid, text, request_id):
    record = conversation(sid)
    text = str(text).strip()
    if not text or len(text) > 2000:
        raise ValueError("回复请输入 1–2000 字")
    try:
        request_id = str(uuid.UUID(str(request_id)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("缺少有效的发送标识，请刷新后再试")
    if not record["sent_at"] or not record["chat_name"] or not record["qq_uin"]:
        raise ValueError("该供应商尚未建立已核实的 QQ 会话绑定，不能发送回复")
    if record["status"] in ("identity_unverified", "send_uncertain"):
        raise ValueError("请先核实该供应商的身份或上次发送结果")
    # 型号/收件人通过 supplier_id 绑定；只发送用户确认的正文。
    content = text
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT * FROM outbox WHERE request_id=?", (request_id,)).fetchone()
        if old:
            if old["supplier_id"] != sid or old["content"] != content:
                raise ValueError("同一发送标识不能用于不同回复")
            return dict(old)
        pending = c.execute("SELECT id FROM outbox WHERE supplier_id=? AND status IN ('queued','deferred','sending','uncertain')", (sid,)).fetchone()
        if pending:
            raise ValueError("该会话还有待处理或结果待核实的回复，请先处理")
        cur = c.execute(
            "INSERT INTO outbox(request_id,supplier_id,company,chat_name,qq_uin,content,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?)",
            (request_id, sid, record["company"], record["chat_name"], record["qq_uin"], content, time.time(), time.time()))
        return dict(c.execute("SELECT * FROM outbox WHERE id=?", (cur.lastrowid,)).fetchone())


def outbox_for(sid):
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM outbox WHERE supplier_id=? ORDER BY id DESC LIMIT 20", (sid,))]


def cancel_reply(oid):
    with db.connect() as c:
        cur = c.execute("UPDATE outbox SET status='cancelled',updated_at=? WHERE id=? AND status IN ('queued','deferred')", (time.time(), oid))
        if not cur.rowcount:
            raise ValueError("这条回复已开始发送或已结束，不能取消")


def enqueue_analysis(sid, refresh=False):
    digest = reply_digest(sid)
    if not db.messages(sid, "in"):
        return
    with db.connect() as c:
        c.execute(
            "INSERT INTO analysis_job(supplier_id,digest,status,next_at) VALUES(?,?,'queued',0) "
            "ON CONFLICT(supplier_id) DO UPDATE SET digest=excluded.digest,status='queued',attempts=0,next_at=0,error=NULL "
            "WHERE analysis_job.digest!=excluded.digest OR ?",
            (sid, digest, refresh))


def suggestions_for(sid):
    digest = reply_digest(sid)
    with db.connect() as c:
        row = c.execute("SELECT * FROM analysis_job WHERE supplier_id=?", (sid,)).fetchone()
    if not row:
        return {"status": "none", "items": []}
    return {"status": row["status"], "items": json.loads(row["suggestions"] or "[]") if row["digest"] == digest else [],
            "error": row["error"], "digest": row["digest"]}


def save_unassigned(chat_name, message, reason):
    source_id = message.get("id")
    if not source_id:
        raise ValueError("回复缺少消息 ID，无法安全保存为待归属消息")
    with db.connect() as c:
        c.execute("INSERT OR IGNORE INTO inbox(chat_name,source_id,content,message_time,reason,created_at) VALUES(?,?,?,?,?,?)",
                  (chat_name, source_id, message["text"], message.get("time"), reason, time.time()))


def assign_message(mid, sid):
    target = conversation(sid)
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM inbox WHERE id=?", (mid,)).fetchone()
        if not row or row["chat_name"] != target["chat_name"] or not target["sent_at"]:
            raise ValueError("只能关联到同一个 QQ 会话内已发送的型号")
        if row["assigned_to"] is not None:
            if row["assigned_to"] == sid:
                return
            raise ValueError("该回复已归属到其他型号")
        exists = c.execute("SELECT 1 FROM message WHERE supplier_id=? AND source_id=? AND direction='in'", (sid, row["source_id"])).fetchone()
        if not exists:
            c.execute("INSERT INTO message(supplier_id,direction,content,seen_at,source_id,message_time) VALUES(?,'in',?,?,?,?)",
                      (sid, row["content"], row["created_at"], row["source_id"], row["message_time"]))
        c.execute("UPDATE inbox SET assigned_to=? WHERE id=?", (sid, mid))
        c.execute("UPDATE supplier SET status='parse_pending',last_reply_at=? WHERE id=?", (time.time(), sid))
        c.execute("UPDATE quote SET valid=0 WHERE supplier_id=?", (sid,))
    enqueue_analysis(sid)


def model_groups(include_dry=False, query=""):
    """先聚合全部历史，再搜索；保留型号中的 / - 等有意义字符。"""
    with db.connect() as c:
        tasks = [dict(r) for r in c.execute("SELECT * FROM task WHERE (? OR status!='dry-run') ORDER BY id DESC", (include_dry,))]
        sups = [dict(r) for r in c.execute("SELECT s.*, COALESCE(q.valid,0) AS valid FROM supplier s LEFT JOIN quote q ON q.supplier_id=s.id WHERE s.sent_at IS NOT NULL")]
        replied = {r[0] for r in c.execute("SELECT DISTINCT supplier_id FROM message WHERE direction='in'")}
    groups = {}
    for t in tasks:
        key = part_key(t["part_no"])
        if part_key(query) not in key:
            continue
        g = groups.setdefault(key, {**t, "part_no": key, "task_ids": [], "runs": 0, "suppliers": {}})
        g["task_ids"].append(t["id"])
        g["runs"] += 1
        if t["status"] == "running":
            g["status"] = "running"
    by_task = {tid: g for g in groups.values() for tid in g["task_ids"]}
    for s in sorted(sups, key=lambda r: (bool(r["sent_at"]), r["id"]), reverse=True):
        g = by_task.get(s["task_id"])
        if g:
            g["suppliers"].setdefault(s["company"], s)
    result = []
    for g in groups.values():
        chosen = list(g.pop("suppliers").values())
        g.update(total=len(chosen), sent=sum(bool(s["sent_at"]) for s in chosen),
                 replied=sum(s["id"] in replied for s in chosen), valid=sum(bool(s["valid"]) for s in chosen),
                 no_stock=sum(s["status"] == "no_stock" for s in chosen),
                 asking=sum(s["status"] == "asking" for s in chosen), supplier_ids=[s["id"] for s in chosen])
        result.append(g)
    return result
