"""Durable normal inquiries submitted by the office UI and executed on the worker."""
import argparse
import json
import time
import uuid
from . import db, isolation, workbench


def submit(body, settings):
    parts = workbench.validate_parts(body.get("parts", []))
    template = str(body.get("template") or settings.get("inquiry", {}).get("template", ""))
    workbench.validate_template(template)
    try:
        jid = uuid.UUID(str(body.get("request_id"))).hex
    except (ValueError, TypeError, AttributeError):
        raise ValueError("缺少有效的任务标识，请刷新后重试")
    cap = body.get("max")
    cap = 3 if cap is None else cap
    if type(cap) is not int or not 1 <= cap <= 100:
        raise ValueError("每个型号最多询价家数须为 1–100")
    if type(body.get("dry_run", False)) is not bool:
        raise ValueError("试运行开关无效")
    request = {"parts": parts, "template": template, "max": cap, "dry_run": body.get("dry_run", False)}
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT * FROM inquiry_job WHERE id=?", (jid,)).fetchone()
        if old:
            if json.loads(old["payload"])["request"] != json.loads(json.dumps(request)):
                raise ValueError("同一任务标识不能提交不同内容")
        else:
            payload = {"request": request, "settings": settings}
            c.execute("INSERT INTO inquiry_job(id,payload,status,created_at) VALUES(?,?,'queued',?)",
                      (jid, json.dumps(payload, ensure_ascii=False), time.time()))
            for part, qty in parts:
                c.execute("INSERT INTO task(part_no,qty,status,created_at,batch_id) VALUES(?,?,'queued',?,?)",
                          (part, qty, time.time(), jid))
    return get(jid)


def get(jid):
    with db.connect() as c:
        row = c.execute("SELECT id,status,created_at,started_at,finished_at,error,payload FROM inquiry_job WHERE id=?", (jid,)).fetchone()
        if not row:
            raise ValueError("询价批次不存在")
        result = dict(row)
        result["dry_run"] = json.loads(result.pop("payload"))["request"]["dry_run"]
        return result | {"task_ids": [r[0] for r in c.execute("SELECT id FROM task WHERE batch_id=? ORDER BY id", (jid,))]}


def recent():
    with db.connect() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM inquiry_job ORDER BY created_at DESC LIMIT 30")]
    return [get(jid) for jid in ids]


def recover():
    # Worker restart never silently replays a job that might already have sent.
    with db.connect() as c:
        c.execute("UPDATE task SET status='needs_review' WHERE batch_id IN (SELECT id FROM inquiry_job WHERE status='running') AND status='running'")
        c.execute("UPDATE inquiry_job SET status='interrupted',error='工作机重启，已保留进度；核对后可继续，发送结果不确定的消息不会重发' WHERE status='running'")


def cancel(jid):
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        changed = c.execute("UPDATE inquiry_job SET status='stopped',finished_at=?,error='用户已取消此批次' WHERE id=? AND status IN ('queued','waiting_verification','interrupted','failed')", (time.time(), jid)).rowcount
        if not changed:
            raise ValueError("此批次已开始或已结束；运行中的任务请使用安全停止")
        dry_run = json.loads(c.execute("SELECT payload FROM inquiry_job WHERE id=?",(jid,)).fetchone()[0])["request"]["dry_run"]
        c.execute("UPDATE task SET status=?,finished_at=? WHERE batch_id=?", ('dry-run' if dry_run else 'stopped', time.time(), jid))


def resume(jid):
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM supplier s JOIN task t ON t.id=s.task_id WHERE t.batch_id=? AND s.status IN ('sending','send_uncertain')", (jid,)).fetchone():
            raise ValueError("此批次存在发送结果待核实项，禁止自动继续")
        changed = c.execute("UPDATE inquiry_job SET status='queued',error=NULL,finished_at=NULL WHERE id=? AND status IN ('waiting_verification','interrupted','stopped','failed')", (jid,)).rowcount
        if not changed:
            raise ValueError("此批次不能继续")
        c.execute("UPDATE task SET status='queued',finished_at=NULL WHERE batch_id=? AND status NOT IN ('done','dry-run')", (jid,))


def dispatch_one():
    from ui import server
    if isolation.MANUAL.is_set() or isolation.DRAINING.is_set() or server.STOP_FILE.exists():
        return
    with server.SPAWN_LOCK:
        if server.run_state()["running"]:
            return
        state = server.browser_status()
        blocked = state.get("code") in ("verification", "login")
        with db.connect() as c:
            if blocked:
                c.execute("UPDATE inquiry_job SET status='waiting_verification',error=? WHERE status='queued'", ("请在第二台完成网站登录或验证码；本批次没有重新搜索",))
                c.execute("UPDATE task SET status='waiting_verification' WHERE status='queued' AND batch_id IN (SELECT id FROM inquiry_job WHERE status='waiting_verification')")
                return
            c.execute("UPDATE inquiry_job SET status='queued',error=NULL WHERE status='waiting_verification'")
            c.execute("UPDATE task SET status='queued' WHERE status='waiting_verification' AND batch_id IN (SELECT id FROM inquiry_job WHERE status='queued')")
            row = c.execute("SELECT id,payload FROM inquiry_job WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if row:
            request = json.loads(row["payload"])["request"]
            if not request["dry_run"] and not isolation.ALLOW_SEND:
                return
            server.spawn(["--job", row["id"]], "dry-run" if request["dry_run"] else "run",
                         "、".join(p[0] for p in request["parts"]), module="inquiry.jobs")


def start_pump():
    import threading
    recover()
    def pump():
        while not isolation.DRAINING.wait(2):
            try:
                dispatch_one()
            except Exception as exc:
                from .runner import log
                log("询价队列等待执行：" + str(exc)[:200])
    threading.Thread(target=pump, daemon=True, name="inquiry-queue").start()


def execute(jid):
    from .runner import Runner, Task, log, render_message
    from . import dispatch, icnet
    from ui import service, server
    lock = service.acquire_lock(server.LOCK_FILE)
    if lock is None:
        return  # Collector won the race; job is still queued, not lost.
    try:
        if server.STOP_FILE.exists():
            return
        dispatch.recover_interrupted()
        with db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM inquiry_job WHERE id=? AND status='queued'", (jid,)).fetchone()
            if not row:
                return
            payload = json.loads(row["payload"]); req = payload["request"]
            if not req["dry_run"]:
                isolation.check_sending()
            c.execute("UPDATE inquiry_job SET status='running',started_at=?,error=NULL WHERE id=?", (time.time(), jid))
            records = [dict(r) for r in c.execute("SELECT * FROM task WHERE batch_id=? ORDER BY id", (jid,))]
            c.execute("UPDATE task SET status='running',finished_at=NULL WHERE batch_id=? AND status NOT IN ('done','dry-run')", (jid,))
        runner = Runner([], dry_run=req["dry_run"], message=req["template"], max_suppliers=req["max"])
        runner.cfg = dict(payload["settings"].get("inquiry", {}), max_suppliers=req["max"])
        runner.pacing = payload["settings"].get("pacing", {})
        runner.filter = payload["settings"].get("filter", {})
        from .runtime import ROOT
        card = runner.cfg.get("card_image") if runner.cfg.get("send_card") else None
        runner.card_image = ROOT / card if card and (ROOT / card).is_file() else None
        # Normal jobs send only. Reading replies requires an explicit collection round.
        runner.send_only = True
        runner.batch_id = jid
        for record in records:
            if record["status"] in ("done", "dry-run"):
                continue
            task = Task(record["id"], record["part_no"], record["qty"], render_message(runner.message, record["part_no"], record["qty"]))
            for supplier in db.suppliers(task.id):
                if supplier["sent_at"] or supplier["status"] in ("sending", "send_uncertain"):
                    task.sent += 1
                if supplier["sent_at"] and supplier["chat_name"]:
                    runner.chat_owner.setdefault(supplier["chat_name"], []).append((task, supplier["id"]))
            runner.tasks.append(task)
        try:
            runner.run()
            uncertain = any(s["status"] in ("sending", "send_uncertain") for t in runner.tasks for s in db.suppliers(t.id))
            status = "interrupted" if uncertain else ("stopped" if runner._stop else "done")
            error = "存在发送结果待核实项，已停止后续发送" if uncertain else None
        except icnet.NotLoggedIn as exc:
            status, error = "waiting_verification", str(exc)
        except Exception as exc:
            status, error = "failed", str(exc)[:400]
            log("询价批次未完成：" + error)
        with db.connect() as c:
            c.execute("UPDATE inquiry_job SET status=?,finished_at=?,error=? WHERE id=?", (status, time.time(), error, jid))
            if status == "waiting_verification":
                c.execute("UPDATE task SET status='waiting_verification',finished_at=NULL WHERE batch_id=? AND status NOT IN ('done','dry-run')", (jid,))
    finally:
        lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if not args.worker:
        raise ValueError("询价队列只能由配对的第二台工作机执行")
    isolation.enable_worker()
    execute(uuid.UUID(args.job).hex)


if __name__ == "__main__":
    main()
