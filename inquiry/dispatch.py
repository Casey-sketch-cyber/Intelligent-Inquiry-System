"""唯一 QQ 执行进程使用的发件箱；任何不确定的发送都不自动重试。"""
import time

from . import db, incidents, qq, workbench
from .identity import SendUncertainError, SupplierIdentityError


def rate_delay(pacing):
    # 这是保守的发送限制，不承诺或绕过第三方风控。
    interval = max(20.0, float(pacing.get("min_interval", 20)))
    cap = min(60, max(1, int(pacing.get("max_per_hour", 60))))
    now = time.time()
    with db.connect() as c:
        rows = [r[0] for r in c.execute("SELECT created_at FROM send_attempt WHERE created_at>? ORDER BY created_at", (now - 3600,))]
    if not rows:
        return 0
    delay = max(0, rows[-1] + interval - now)
    if len(rows) >= cap:
        delay = max(delay, rows[-cap] + 3600 - now)
    return delay


def record_attempt(sid, kind):
    with db.connect() as c:
        c.execute("INSERT INTO send_attempt(created_at,supplier_id,kind) VALUES(?,?,?)", (time.time(), sid, kind))


def recover_interrupted():
    """只能在拿到 runner.lock 后调用。"""
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        interrupted = c.execute("SELECT supplier_id FROM outbox WHERE status='sending' "
                                "UNION SELECT id FROM supplier WHERE status='sending'").fetchall()
        c.execute("UPDATE outbox SET status='uncertain',error='执行进程中断，结果待核实；请查看 QQ，禁止自动重发',updated_at=? WHERE status='sending'", (time.time(),))
        c.execute("UPDATE supplier SET status='send_uncertain',error='发送期间进程中断，请核实 QQ；禁止自动重发' WHERE status='sending'")
    for row in interrupted:
        incidents.record("send_uncertain", "recovery", severity="error", supplier_id=row[0])


def process_one(win, pacing, cancelled=lambda: False):
    from . import isolation, selfcheck

    if cancelled() or rate_delay(pacing) > 0:
        return False
    with db.connect() as c:
        row = c.execute("SELECT * FROM outbox WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
    if not row:
        return False
    item = dict(row)
    oid, sid = item["id"], item["supplier_id"]
    invoked = False
    try:
        if isolation.ENABLED and isolation.MODE == "worker":
            selfcheck.require_ready(win=win)
        target = workbench.conversation(sid)
        if any(item[k] != target[k] for k in ("company", "chat_name", "qq_uin")):
            raise SupplierIdentityError("会话绑定已变化，请重新核对后提交")
        if not qq.open_recent(win, item["chat_name"], cancelled=cancelled):
            raise qq.DesktopDeferred("QQ 暂不支持后台切换到该会话，请在 QQ 打开对应会话后重试")
        qq.verify_supplier(win, item["company"], item["chat_name"], cancelled=cancelled)
        original = db.messages(sid, "out")
        if not original:
            raise SupplierIdentityError("找不到原询价，无法确认会话绑定")
        qq.verify_bound_history(win, item["chat_name"], original[0], target.get("inquiry_message_id"), target.get("sent_at"))
        if cancelled():
            return False
        with db.connect() as c:
            changed = c.execute("UPDATE outbox SET status='sending',updated_at=?,error=NULL WHERE id=? AND status IN ('queued','deferred')",
                                (time.time(), oid)).rowcount
        if not changed:
            return False
        record_attempt(sid, "reply")
        invoked = True
        qq.send_text(win, item["content"], expect_chat=item["chat_name"], company=item["company"])
        with db.connect() as c:
            c.execute("INSERT INTO message(supplier_id,direction,content,seen_at) VALUES(?,'out',?,?)", (sid, item["content"], time.time()))
            c.execute("UPDATE outbox SET status='sent',updated_at=?,error=NULL WHERE id=?", (time.time(), oid))
        return True
    except (qq.DesktopDeferred, selfcheck.SelfcheckDeferred) as e:
        status, error = "deferred", str(e)
        if isinstance(e, qq.EditorUnavailable) and item.get("error") != error[:300]:
            incidents.record("uia_control_missing", "qq_controls", severity="warning", supplier_id=sid,
                             extra={"control": "editor"})
    except SendUncertainError as e:
        status, error = "uncertain", str(e)
    except SupplierIdentityError as e:
        status, error = "failed", str(e)
        incidents.record("identity_unverified", "reply_send", severity="warning", supplier_id=sid, error=e)
    except Exception as e:
        status = "uncertain" if invoked else "failed"
        error = str(e)
    with db.connect() as c:
        c.execute("UPDATE outbox SET status=?,error=?,updated_at=? WHERE id=? AND status!='cancelled'", (status, error[:300], time.time(), oid))
    if status == "uncertain":
        incidents.record("send_uncertain", "reply_send", severity="error", supplier_id=sid)
    return True


def run_worker():
    from . import isolation, selfcheck
    from .settings import SETTINGS
    from .runner import STOP_FILE
    recover_interrupted()
    try:
        if isolation.ENABLED and isolation.MODE == "worker":
            selfcheck.require_ready()
        win = qq.ready_main_window()
    except Exception as e:
        with db.connect() as c:
            c.execute("UPDATE outbox SET status='deferred',error=?,updated_at=? WHERE status='queued'", (str(e)[:300], time.time()))
        return
    # 后台能力不足时保留队列并退出；用户点击“继续发送”才重新尝试。
    deadline = time.time() + 3600
    while time.time() < deadline and not STOP_FILE.exists():
        with db.connect() as c:
            pending = c.execute("SELECT COUNT(*) FROM outbox WHERE status='queued'").fetchone()[0]
        if not pending:
            return
        process_one(win, SETTINGS.get("pacing", {}), cancelled=STOP_FILE.exists)
        time.sleep(0.5)
