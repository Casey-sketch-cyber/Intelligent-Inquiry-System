"""SQLite 存储：询价任务、供应商、聊天消息、解析出的报价。"""
import json
import sqlite3 as standard_sqlite
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

try:
    import pysqlite3 as sqlite3
except ModuleNotFoundError as error:
    if error.name != "pysqlite3":
        raise
    sqlite3 = standard_sqlite

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
    qq_launch_json TEXT,          -- 仅内部使用的 QQ 唤起属性
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
CREATE TABLE IF NOT EXISTS search_cache (
    part_no TEXT NOT NULL, page_no INTEGER NOT NULL, rows_json TEXT NOT NULL,
    ref_price REAL, fetched_at REAL NOT NULL,
    PRIMARY KEY(part_no, page_no)
);
CREATE TABLE IF NOT EXISTS inquiry_job (
    id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
    created_at REAL NOT NULL, started_at REAL, finished_at REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS incident (
    id INTEGER PRIMARY KEY, created_at REAL NOT NULL, occurred_date TEXT NOT NULL,
    kind TEXT NOT NULL, stage TEXT NOT NULL, severity TEXT NOT NULL,
    payload TEXT NOT NULL, dedupe_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS incident_date_kind ON incident(occurred_date, kind);
"""


MIGRATIONS = [
    ("task", "ref_price", "REAL"),
    ("task", "batch_id", "TEXT"),
    ("task", "search_cache_at", "REAL"),
    ("task", "search_cache_pages", "INTEGER NOT NULL DEFAULT 0"),
    ("supplier", "inquiry_message_id", "TEXT"),
    ("supplier", "attempted_at", "REAL"),
    ("supplier", "read_error", "TEXT"),
    ("supplier", "last_checked_at", "REAL"),
    ("supplier", "parsed_reply_hash", "TEXT"),
    ("supplier", "qq_launch_json", "TEXT"),
    ("message", "source_id", "TEXT"),
    ("message", "message_time", "TEXT"),
]


def is_lock_error(error):
    code = getattr(error, "sqlite_errorcode", 0) & 0xFF
    return code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or (
        code == 0 and str(error) in ("database is locked", "database table is locked", "database schema is locked"))


def _has_wal_fix(version):
    # SQLite 3.51.3 修复 WAL 重置竞态，3.50.7 和 3.44.6 含回移补丁。
    return (version >= (3, 51, 3)
            or (version[:2] == (3, 50) and version >= (3, 50, 7))
            or (version[:2] == (3, 44) and version >= (3, 44, 6)))


def _checked_journal_mode(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if mode == "wal" and not _has_wal_fix(sqlite3.sqlite_version_info):
        raise sqlite3.OperationalError("当前 SQLite 缺少 WAL 修复，请先安装项目锁定依赖再打开该数据库")
    return mode


def _configure_journal(conn, timeout):
    deadline = time.monotonic() + timeout
    enable_wal = _has_wal_fix(sqlite3.sqlite_version_info)
    while True:
        try:
            mode = _checked_journal_mode(conn)
            if not enable_wal or mode == "wal" or conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal":
                return
            raise sqlite3.OperationalError("业务数据库无法启用 WAL")
        except sqlite3.OperationalError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not is_lock_error(error):
                raise
            # 首次切换模式需要独占锁，SQLite 有时会立即报告冲突。
            time.sleep(min(0.05, remaining))


@contextmanager
def connect(*, timeout=30):
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        conn.execute("PRAGMA temp_store=MEMORY")
        _configure_journal(conn, timeout)
        if conn.execute("PRAGMA user_version").fetchone()[0] < 7:
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
            for table, col, typ in MIGRATIONS:
                if col not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            conn.execute("PRAGMA user_version=7")
            conn.commit()
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def open_existing(path, *, read_only=False, timeout=30):
    """让维护工具访问现有业务库，仅检查日志模式，事务由调用者管理。"""
    uri = Path(path).absolute().as_uri()
    mode = "ro" if read_only else "rw"
    with closing(sqlite3.connect(f"{uri}?mode={mode}", uri=True, timeout=timeout)) as conn:
        conn.execute("PRAGMA temp_store=MEMORY")
        _checked_journal_mode(conn)
        yield conn


def snapshot(source, target):
    """备份包含已提交 WAL 数据的单文件快照，成功后再替换迁移库。"""
    if source.resolve() == target.resolve():
        raise ValueError("备份目录必须不同于业务数据库")
    with tempfile.TemporaryDirectory(prefix="sqlite-backup-", dir=target.parent) as folder:
        temporary = Path(folder) / target.name
        with open_existing(source, read_only=True) as src:
            with closing(sqlite3.connect(temporary, timeout=30)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise sqlite3.OperationalError("迁移数据库未能转为单文件快照")
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.OperationalError("迁移数据库完整性校验失败")
        if any(target.with_name(target.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("现有迁移数据库仍有关联日志，请关闭后再生成迁移包")
        temporary.replace(target)


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


def _search_row_without_links(row: dict) -> dict:
    contacts = row.get("qqs")
    if contacts:
        return {
            **row,
            "qqs": [{key: value for key, value in entry.items() if key not in ("inthref", "href")} for entry in contacts],
        }
    return row


def read_search_cache(part_no: str, page_no: int, *, cutoff: float, now: float) -> tuple[list[dict], float | None, float] | None:
    with connect() as c:
        row = c.execute(
            "SELECT rows_json, ref_price, fetched_at FROM search_cache WHERE part_no=? AND page_no=? "
            "AND fetched_at>? AND fetched_at<=?", (part_no, page_no, cutoff, now),
        ).fetchone()
    if row:
        try:
            rows = json.loads(row["rows_json"])
        except ValueError:
            return None
        if isinstance(rows, list) and all(isinstance(item, dict) for item in rows):
            return rows, row["ref_price"], row["fetched_at"]
    return None


def save_search_cache(part_no: str, page_no: int, rows: list[dict], price: float | None, fetched_at: float):
    content = json.dumps([_search_row_without_links(row) for row in rows], ensure_ascii=False)
    with connect() as c:
        c.execute(
            "INSERT INTO search_cache(part_no, page_no, rows_json, ref_price, fetched_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(part_no, page_no) DO UPDATE SET rows_json=excluded.rows_json, "
            "ref_price=excluded.ref_price, fetched_at=excluded.fetched_at",
            (part_no, page_no, content, price, fetched_at),
        )


def set_search_cache_usage(task_id: int, fetched_at: float | None, count: int):
    with connect() as c:
        c.execute("UPDATE task SET search_cache_at=?, search_cache_pages=? WHERE id=?", (fetched_at, count, task_id))


def _qq_link_json(selected: dict) -> str | None:
    link = {key: selected[key] for key in ("inthref", "href") if selected.get(key)}
    return json.dumps(link, ensure_ascii=False) if link else None


def add_supplier(task_id: int, row: dict) -> int | None:
    selected = (row.get("qqs") or [{}])[0]
    public_row = _search_row_without_links(row)
    with connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO supplier(task_id, company, company_url, row_json, qq_uin, qq_launch_json) VALUES (?,?,?,?,?,?)",
            (task_id, row["company"], row.get("companyUrl"), json.dumps(public_row, ensure_ascii=False),
             selected.get("uin"), _qq_link_json(selected)),
        )
        return cur.lastrowid if cur.rowcount else None


def save_supplier_qq_link(task_id: int, company: str, selected: dict):
    if link := _qq_link_json(selected):
        with connect() as c:
            c.execute("UPDATE supplier SET qq_launch_json=? WHERE task_id=? AND company=? AND qq_uin=?",
                      (link, task_id, company, selected.get("uin")))


def saved_qq_link(task_id: int, company: str, uin: str | None) -> dict | None:
    if not uin:
        return None
    with connect() as c:
        row = c.execute(
            "SELECT qq_launch_json FROM supplier WHERE task_id=? AND company=? AND qq_uin=?",
            (task_id, company, str(uin)),
        ).fetchone()
    if not row or not row["qq_launch_json"]:
        return None
    try:
        link = json.loads(row["qq_launch_json"])
    except (ValueError, TypeError):
        return None
    return {**link, "uin": str(uin)} if isinstance(link, dict) else None


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
        d.pop("qq_launch_json", None)
        d["quote"] = json.loads(d.pop("data_json")) if d.get("data_json") else None
        d["row"] = json.loads(d.pop("row_json")) if d.get("row_json") else {}
        out.append(d)
    return out
