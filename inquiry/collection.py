"""One explicit click collects one frozen set of bound conversations. Never sends."""
import json
import threading
import time
import uuid
from . import db
from .runtime import DATA_DIR

STATE_FILE = DATA_DIR / "collection.json"
LOCK = threading.RLock()
ACTIVITY = {"active": False}
_pump = None


def options():
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {}
    # An old enabled=true setting must never start collection after this upgrade.
    if value.get("version") != 2:
        value = {"version": 2, "phase": "idle", "plan": [], "processed": 0, "failed": 0}
    return value


def save(value):
    from lanhost.config import save_json
    save_json(STATE_FILE, value)


def records(task_ids=None):
    if task_ids is not None and (not isinstance(task_ids, list) or not task_ids or
            any(type(i) is not int or i < 1 for i in task_ids)):
        raise ValueError("任务编号无效")
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT s.*,t.part_no,t.qty,(SELECT content FROM message m WHERE m.supplier_id=s.id "
            "AND m.direction='out' AND m.content!='[名片]' ORDER BY m.id LIMIT 1) AS inquiry_text "
            "FROM supplier s JOIN task t ON t.id=s.task_id WHERE s.sent_at IS NOT NULL "
            "AND s.chat_name IS NOT NULL ORDER BY s.chat_name,s.sent_at,s.id")]
    return [r for r in rows if r["inquiry_text"] and (task_ids is None or r["task_id"] in task_ids)]


def status():
    with LOCK:
        value = options()
        phase = value["phase"]
        total, done = len(value["plan"]), value.get("processed", 0)
        texts = {"idle": "等待手动收集回复", "queued": "已排队，等待收集一轮", "running": f"正在收集回复 {done}/{total}",
                 "pausing": "正在暂停收集回复", "paused": f"收集已暂停 {done}/{total}",
                 "done": f"本轮收集完成 {done}/{total}", "interrupted": "收集中断，等待手动重新开始"}
        text = texts.get(phase, "等待手动收集回复")
        if value.get("failed"):
            text += f" · {value['failed']} 家未完整读取"
        return {k: value.get(k) for k in ("id", "started_at", "finished_at", "error")} | {
            "mode": "manual", "phase": phase, "pending": phase in ("queued", "running", "pausing"),
            "active": ACTIVITY["active"], "eligible": len(records()), "total": total,
            "processed": done, "failed": value.get("failed", 0), "text": text}


def start(request_id, task_ids=None):
    try:
        request_id = uuid.UUID(str(request_id)).hex
    except (ValueError, TypeError, AttributeError):
        raise ValueError("缺少有效的收集标识，请刷新后重试")
    with LOCK:
        value = options()
        if value.get("id") == request_id or value["phase"] in ("queued", "running", "pausing"):
            return status()
        plan = [r["id"] for r in records(task_ids)]
        if not plan:
            raise ValueError("没有已发送且绑定 QQ 会话的询价可收集")
        save({"version": 2, "id": request_id, "phase": "queued", "plan": plan,
              "processed": 0, "failed": 0, "requested_at": time.time()})
        return status()


def pause():
    with LOCK:
        value = options()
        if value["phase"] in ("queued", "running", "pausing"):
            value["phase"] = "pausing" if ACTIVITY["active"] else "paused"
            save(value)
        return status()


def recover():
    with LOCK:
        value = options()
        if value["phase"] in ("queued", "running", "pausing"):
            value.update(phase="interrupted", finished_at=time.time())
        save(value)


def configure(enabled, hours=None):
    # Safely stop older pages, but never interpret an old automatic toggle as a new round.
    if enabled is False:
        return pause()
    raise ValueError("已改为手动单轮收集，请刷新页面后点击「收集回复」")


def process_round():
    from . import isolation, qq
    from .runner import Runner, Task
    from ui import server, service
    if isolation.MANUAL.is_set() or isolation.DRAINING.is_set() or server.STOP_FILE.exists():
        return
    if options()["phase"] != "queued":
        return  # No QQ enumeration, focus changes, contact reads or timers while idle.
    lease = service.acquire_lock(server.LOCK_FILE)
    if lease is None:
        return
    run_id = None
    try:
        with LOCK:
            value = options()
            if value["phase"] != "queued":
                return
            run_id = value["id"]
            value.update(phase="running", started_at=time.time())
            save(value)
            ACTIVITY["active"] = True
        def cancelled():
            state = options()
            return (state.get("id") != run_id or state["phase"] != "running" or isolation.MANUAL.is_set()
                    or isolation.DRAINING.is_set() or server.STOP_FILE.exists())
        reader = Runner([])
        reader.async_analysis = True
        reader.stop_requested = cancelled
        by_id = {r["id"]: r for r in records()}
        with qq.auto.UIAutomationInitializerInThread():
            if cancelled():
                return
            reader.win = qq.ready_main_window()
            for sid in value["plan"]:
                if cancelled():
                    break
                record = by_id.get(sid)
                complete = False
                try:
                    if record:
                        task = Task(record["task_id"], record["part_no"], record["qty"], record["inquiry_text"])
                        complete = reader.read_chat(record["chat_name"], task, sid)
                except Exception as exc:
                    db.update_supplier(sid, read_error=str(exc)[:300])
                if cancelled():
                    break
                with LOCK:
                    current = options()
                    current["processed"] += 1
                    current["failed"] += int(not complete)
                    save(current)
    except Exception as exc:
        with LOCK:
            current = options()
            current["error"] = str(exc)[:300]
            save(current)
    finally:
        with LOCK:
            if run_id:
                current = options()
                current.update(phase="done" if current["processed"] == len(current["plan"]) else "paused", finished_at=time.time())
                save(current)
            ACTIVITY["active"] = False
        lease.close()


def start_pump():
    global _pump
    with LOCK:
        if _pump and _pump.is_alive():
            return
        recover()
        from . import isolation
        def loop():
            while not isolation.DRAINING.wait(1):
                process_round()
        _pump = threading.Thread(target=loop, daemon=True, name="manual-reply-collection")
        _pump.start()
