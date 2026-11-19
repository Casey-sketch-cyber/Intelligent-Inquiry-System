"""系统空闲时只读 QQ 当前可见会话；不切换、不滚动、不点击。"""
import threading
import time

from . import db, qq
from .history import HistorySnapshot, normalized, signature

_thread = None
_guard = threading.Lock()
STATE = {"text": "等待 QQ 可见会话", "last_seen": None}


def visible_snapshot(record, messages):
    outgoing = db.messages(record["id"], "out")
    if not outgoing:
        return None
    text = outgoing[0]
    anchors = []
    for i, m in enumerate(messages):
        if m["self"] and normalized(m["text"]) == normalized(text):
            stamp = qq.parse_qq_time(m.get("time", ""))
            if (record["inquiry_message_id"] and m.get("id") == record["inquiry_message_id"]) or (
                not record["inquiry_message_id"] and stamp and abs(stamp - record["sent_at"]) <= 120
            ):
                anchors.append(i)
    if len(anchors) == 1:
        return HistorySnapshot(messages[anchors[0]:], messages[anchors[0]].get("id", ""), False)
    previous = db.incoming_records(record["id"])
    if previous and previous[-1].get("id"):
        for i, m in enumerate(messages):
            if m.get("id") == previous[-1]["id"]:
                anchor = {"id": record["inquiry_message_id"], "text": text, "self": True}
                return HistorySnapshot([anchor, *messages[i:]], record["inquiry_message_id"], False)
    return None


def observe_once(busy=lambda: False):
    if busy():
        return
    win = qq.main_window()
    if win is None:
        STATE["text"] = "QQ 未打开或已最小化，等待恢复"
        return
    name = qq.current_chat_name(win)
    if not name:
        return
    with db.connect() as c:
        records = [dict(r) for r in c.execute(
            "SELECT s.*,t.part_no,t.qty FROM supplier s JOIN task t ON t.id=s.task_id "
            "WHERE s.chat_name=? AND s.sent_at IS NOT NULL ORDER BY s.sent_at DESC LIMIT 100", (name,))]
    if not records:
        STATE["text"] = "当前 QQ 会话未绑定询价"
        return
    messages = qq.read_messages(win)
    time.sleep(0.15)
    if busy() or qq.current_chat_name(win) != name or signature(qq.read_messages(win)) != signature(messages):
        return
    from .runner import Runner, Task
    reader = Runner([])
    reader.win, reader.async_analysis = win, True
    for record in records:
        if busy() or qq.current_chat_name(win) != name:
            break
        snapshot = visible_snapshot(record, messages)
        if not snapshot:
            continue
        task = Task(record["task_id"], record["part_no"], record["qty"], db.messages(record["id"], "out")[0])
        reader.read_chat(name, task, record["id"], snapshot=snapshot)
    STATE.update(text="后台只读当前会话；其他会话由运行中的任务或补收读取", last_seen=time.time())


def start(busy=lambda: False):
    global _thread
    with _guard:
        if _thread and _thread.is_alive():
            return
        def loop():
            with qq.auto.UIAutomationInitializerInThread():
                while True:
                    try:
                        observe_once(busy)
                    except Exception:
                        STATE["text"] = "QQ 暂不可读，稍后自动检查"
                    time.sleep(12)
        _thread = threading.Thread(target=loop, daemon=True, name="qq-visible-observer")
        _thread.start()
