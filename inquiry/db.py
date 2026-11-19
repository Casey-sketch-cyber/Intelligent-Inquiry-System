"""SQLite 存储：询价任务、供应商、聊天消息、解析出的报价。"""
import json
import sqlite3
import time
from contextlib import contextmanager

from .settings import DATA_DIR

DB_PATH = DATA_DIR / "inquiry.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    id INTEGER PRIMARY KEY,
    part_no TEXT NOT NULL,
    qty INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',   -- running / done / stopped
    created_at REAL NOT NULL,
    finished_at REAL
);
-- 每个任务里挑出来的供应商（一行搜索结果）
CREATE TABLE IF NOT EXISTS supplier (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES task(id),
    company TEXT NOT NULL,
    company_url TEXT,
    row_json TEXT,                -- 搜索结果原始行
    qq_uin TEXT,                  -- 用来唤起的 QQ 号
    chat_name TEXT,               -- QQ 里会话的名字
    status TEXT NOT NULL DEFAULT 'pending',
        -- pending 待询价 / sent 已发送 / quoted 已报价 / no_stock 无货 / asking 反问 / failed 唤起或发送失败 / skipped
    error TEXT,
    sent_at REAL,
    last_reply_at REAL,
    UNIQUE(task_id, company)
);
CREATE TABLE IF NOT EXISTS message (
    id INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES supplier(id),
    direction TEXT NOT NULL,      -- out 我方 / in 供应商
    content TEXT NOT NULL,
    seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quote (
    supplier_id INTEGER PRIMARY KEY REFERENCES supplier(id),
    data_json TEXT NOT NULL,      -- 大模型解析结果
    valid INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
    supplier_id INTEGER NOT NULL REFERENCES supplier(id),
    company TEXT NOT NULL, chat_name TEXT NOT NULL, qq_uin TEXT NOT NULL,
    content TEXT NOT NULL, status TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS analysis_job (
    supplier_id INTEGER PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
    suggestions TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY, chat_name TEXT NOT NULL, source_id TEXT NOT NULL,
    content TEXT NOT NULL, message_time TEXT, reason TEXT,
    created_at REAL NOT NULL, assigned_to INTEGER,
    UNIQUE(chat_name,source_id)
);
CREATE TABLE IF NOT EXISTS send_attempt (
    id INTEGER PRIMARY KEY, created_at REAL NOT NULL, supplier_id INTEGER, kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS message_supplier ON message(supplier_id, direction);
CREATE INDEX IF NOT EXISTS supplier_chat ON supplier(chat_name, sent_at);
CREATE INDEX IF NOT EXISTS attempts_time ON send_attempt(created_at);
CREATE TABLE IF NOT EXISTS search_attempt (
    id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
    part_no TEXT NOT NULL, page_no INTEGER NOT NULL, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS search_attempts_time ON search_attempt(created_at);
CREATE TABLE IF NOT EXISTS inquiry_job (
    id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
    created_at REAL NOT NULL, started_at REAL, finished_at REAL, error TEXT
);
"""


MIGRATIONS = [
    ("task", "ref_price", "REAL"),
    ("task", "batch_id", "TEXT"),
    ("supplier", "inquiry_message_id", "TEXT"),
    ("supplier", "attempted_at", "REAL"),
    ("supplier", "read_error", "TEXT"),
    ("supplier", "last_checked_at", "REAL"),
    ("supplier", "parsed_reply_hash", "TEXT"),
    ("message", "source_id", "TEXT"),
    ("message", "message_time", "TEXT"),
]


@contextmanager
def connect(*, timeout=30):
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] < 4:
            conn.executescript(SCHEMA)
            for table, col, typ in MIGRATIONS:
                if col not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    except sqlite3.OperationalError:
                        if col not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                            raise
            conn.execute("PRAGMA user_version=4")
        yield conn
        conn.commit()
    finally:
        conn.close()


def set_ref_price(task_id: int, price: float | None):
    if price:
        with connect() as c:
            c.execute("UPDATE task SET ref_price=? WHERE id=?", (price, task_id))


def create_task(part_no: str, qty: int) -> int:
    with connect() as c:
        return c.execute("INSERT INTO task(part_no, qty, created_at) VALUES (?,?,?)", (part_no, qty, time.time())).lastrowid


def finish_task(task_id: int, status: str = "done"):
    with connect() as c:
        c.execute("UPDATE task SET status=?, finished_at=? WHERE id=?", (status, time.time(), task_id))


def add_supplier(task_id: int, row: dict) -> int | None:
    with connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO supplier(task_id, company, company_url, row_json, qq_uin) VALUES (?,?,?,?,?)",
            (task_id, row["company"], row.get("companyUrl"), json.dumps(row, ensure_ascii=False), row["qqs"][0]["uin"] if row.get("qqs") else None),
        )
        return cur.lastrowid if cur.rowcount else None


def update_supplier(supplier_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE supplier SET {cols} WHERE id=?", (*fields.values(), supplier_id))


def suppliers(task_id: int, status: str | None = None) -> list[sqlite3.Row]:
    with connect() as c:
        if status:
            return c.execute("SELECT * FROM supplier WHERE task_id=? AND status=? ORDER BY id", (task_id, status)).fetchall()
        return c.execute("SELECT * FROM supplier WHERE task_id=? ORDER BY id", (task_id,)).fetchall()


def recently_asked(part_no: str, hours: float) -> set[str]:
    """最近 hours 小时内已经就这个型号询过价的公司（跨任务去重，避免反复打扰）。"""
    since = time.time() - hours * 3600
    with connect() as c:
        rows = c.execute(
            "SELECT DISTINCT s.company FROM supplier s JOIN task t ON t.id = s.task_id "
            "WHERE t.part_no = ? AND ((s.sent_at IS NOT NULL AND s.sent_at >= ?) OR s.status='send_uncertain')",
            (part_no, since),
        ).fetchall()
    return {r["company"] for r in rows}


def contacted_chat_names() -> dict[str, int]:
    """所有任务里已经联系过的 QQ 会话名 -> 最近的 supplier_id。"""
    with connect() as c:
        rows = c.execute("SELECT chat_name, MAX(id) AS sid FROM supplier WHERE chat_name IS NOT NULL GROUP BY chat_name").fetchall()
    return {r["chat_name"]: r["sid"] for r in rows}


def add_message(supplier_id: int, direction: str, content: str):
    with connect() as c:
        c.execute("INSERT INTO message(supplier_id, direction, content, seen_at) VALUES (?,?,?,?)", (supplier_id, direction, content, time.time()))


def messages(supplier_id: int, direction: str | None = None) -> list[str]:
    with connect() as c:
        if direction:
            rows = c.execute("SELECT content FROM message WHERE supplier_id=? AND direction=? ORDER BY id", (supplier_id, direction)).fetchall()
        else:
            rows = c.execute("SELECT content FROM message WHERE supplier_id=? ORDER BY id", (supplier_id,)).fetchall()
    return [r["content"] for r in rows]


def incoming_records(supplier_id: int) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT source_id AS id, content AS text, message_time AS time FROM message WHERE supplier_id=? AND direction='in' ORDER BY message.id",
                         (supplier_id,)).fetchall()
    return [dict(r) for r in rows]


def replace_incoming(supplier_id: int, contents: list[str], records: list[dict] | None = None):
    """只接受已确认询价起点、连续完整的快照；保留 QQ 消息 ID 和时间标签。"""
    now = time.time()
    with connect() as c:
        old = {r["source_id"]: dict(r) for r in c.execute("SELECT source_id,seen_at,message_time FROM message WHERE supplier_id=? AND direction='in' AND source_id IS NOT NULL", (supplier_id,))}
        c.execute("DELETE FROM message WHERE supplier_id=? AND direction='in'", (supplier_id,))
        records = records or [{} for _ in contents]
        c.executemany("INSERT INTO message(supplier_id, direction, content, seen_at, source_id, message_time) VALUES (?,?,?,?,?,?)",
                      [(supplier_id, "in", m, old.get(r.get("id"), {}).get("seen_at", now + i * 0.000001),
                        r.get("id"), r.get("time") or old.get(r.get("id"), {}).get("message_time"))
                       for i, (m, r) in enumerate(zip(contents, records))])


def supplier(supplier_id: int) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM supplier WHERE id=?", (supplier_id,)).fetchone()
    return dict(row) if row else {}


def inquiry_texts(chat_name: str) -> set[str]:
    with connect() as c:
        rows = c.execute("SELECT m.content FROM message m JOIN supplier s ON s.id=m.supplier_id "
                         "WHERE s.chat_name=? AND m.direction='out' AND m.content!='[名片]' "
                         "AND m.id=(SELECT MIN(m2.id) FROM message m2 WHERE m2.supplier_id=s.id AND m2.direction='out')", (chat_name,)).fetchall()
    return {r[0] for r in rows}


def save_quote(supplier_id: int, data: dict, valid: bool):
    with connect() as c:
        c.execute(
            "INSERT INTO quote(supplier_id, data_json, valid, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(supplier_id) DO UPDATE SET data_json=excluded.data_json, valid=excluded.valid, updated_at=excluded.updated_at",
            (supplier_id, json.dumps(data, ensure_ascii=False), int(valid), time.time()),
        )


def task_quotes(task_id: int) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT s.*, q.data_json, q.valid FROM supplier s LEFT JOIN quote q ON q.supplier_id = s.id WHERE s.task_id=? ORDER BY s.id",
            (task_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["quote"] = json.loads(d.pop("data_json")) if d.get("data_json") else None
        d["row"] = json.loads(d.pop("row_json")) if d.get("row_json") else {}
        out.append(d)
    return out
