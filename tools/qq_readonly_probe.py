"""真实 QQ 只读诊断：隔离数据库，禁用输入/发送/模型调用，记录焦点和读取结果。

从项目根目录执行：python -B tools/qq_readonly_probe.py --session 名称 --seconds 12
不会自动打开、恢复、切换或滚动 QQ；应先手动选择待检查的会话。
使用新的诊断名称会根据本机业务库建立隔离副本。
"""
import argparse
import contextlib
import json
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from inquiry import db, llm, observer, qq
from inquiry.history import signature
from inquiry.runner import Runner, Task
from inquiry.runtime import require_d_path


def send_counts(production):
    with db.open_existing(production, read_only=True) as conn:
        return {key: conn.execute(sql).fetchone()[0] for key, sql in {
            "outgoing": "SELECT COUNT(*) FROM message WHERE direction='out'",
            "send_attempt": "SELECT COUNT(*) FROM send_attempt",
            "queued": "SELECT COUNT(*) FROM outbox WHERE status IN ('queued','sending')",
        }.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--seconds", type=int, default=12, choices=range(1, 61))
    args = parser.parse_args()
    if not args.session.replace("-", "").replace("_", "").isalnum():
        parser.error("session 只能包含字母、数字、横线和下划线")
    production = db.DB_PATH
    baseline = send_counts(production)
    if baseline["queued"]:
        raise RuntimeError("正式发件箱有待发送内容，请先暂停，诊断未开始")
    folder = require_d_path(ROOT / "data" / "qa" / args.session)
    isolated = require_d_path(folder / "readonly.db")
    if isolated.exists():
        if production.samefile(isolated):
            raise RuntimeError("诊断副本指向正式数据库，请使用新的会话名")
        with db.open_existing(isolated, read_only=True):
            pass  # 复用上次副本前先校验其日志模式。
    else:
        folder.mkdir(parents=True, exist_ok=True)
        db.snapshot(production, isolated)
    report = {"started_at": time.time(), "production_before": baseline,
              "isolated_database": str(isolated), "forbidden_calls": [], "samples": []}

    def forbid(name):
        def blocked(*a, **kw):
            report["forbidden_calls"].append(name)
            raise AssertionError("只读诊断禁止：" + name)
        return blocked

    # 只允许实际读取；切换/滚动能力单独查询，不执行 UIA 模式动作。
    def no_navigation(*a, **kw):
        raise qq.DesktopDeferred("只读诊断不切换或翻页；已保留可见回复")

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(db, "DB_PATH", isolated))
        stack.enter_context(patch.object(qq, "FOREGROUND_ASSIST", False))
        for name in ("send_text", "send_image", "_click_send", "_paste_and_send", "human_click"):
            stack.enter_context(patch.object(qq, name, side_effect=forbid(name)))
        for name in ("SendKeys", "Click", "MoveTo", "WheelUp", "WheelDown", "SetClipboardText"):
            stack.enter_context(patch.object(qq.auto, name, side_effect=forbid(name)))
        for name in ("SetForegroundWindow", "ShowWindow", "BringWindowToTop"):
            stack.enter_context(patch.object(qq.win32gui, name, side_effect=forbid(name)))
        stack.enter_context(patch.object(llm, "_client", side_effect=forbid("LLM")))
        stack.enter_context(patch.object(qq, "open_recent", side_effect=no_navigation))
        stack.enter_context(patch.object(qq, "scroll_region", side_effect=no_navigation))
        stack.enter_context(patch("inquiry.runner.log"))
        stack.enter_context(patch("inquiry.runner.STOP_FILE", folder / "stop.flag"))
        runner = Runner([])
        runner.async_analysis = True
        win = qq.main_window()
        if win is None:
            report["unavailable"] = "QQ 未打开或已最小化"
        else:
            runner.win = win
            report["qq_handle"] = win.NativeWindowHandle
            name = qq.current_chat_name(win)
            report["chat"] = name
            contacts = qq.recent_contacts(win)
            with db.connect() as c:
                records = [dict(r) for r in c.execute(
                    "SELECT s.*,t.part_no,t.qty FROM supplier s JOIN task t ON t.id=s.task_id "
                    "WHERE s.sent_at IS NOT NULL AND s.chat_name IS NOT NULL ORDER BY s.sent_at DESC")]
            newest = {}
            for row in records:
                newest.setdefault(row["chat_name"], row)
            report["contacts"] = []
            for contact in contacts:
                if contact["name"] not in newest:
                    continue
                control = contact["control"]
                report["contacts"].append({
                    key: value for key, value in contact.items() if key != "control"})
                report["contacts"][-1]["selection_pattern"] = bool(
                    control.GetPattern(qq.auto.PatternId.SelectionItemPattern))
            report["patterns"] = {}
            for label, cls, pid in (
                ("contact_scroll", "recent-contact-list", qq.auto.PatternId.ScrollPattern),
                ("message_scroll", "chat-msg-area__vlist", qq.auto.PatternId.ScrollPattern),
                ("editor_value", "ExEditor-qq-msg-editor", qq.auto.PatternId.ValuePattern),
            ):
                control = qq.find(win, cls)
                report["patterns"][label] = bool(control and control.GetPattern(pid))
            before = {r["id"]: db.incoming_records(r["id"]) for r in records if r["chat_name"] == name}
            report["saved_before"] = before
            messages = qq.read_messages(win)
            report["visible_messages"] = messages
            report["snapshots"] = {
                str(row["id"]): bool(observer.visible_snapshot(row, messages))
                for row in records if row["chat_name"] == name}
            for chat, row in newest.items():
                task = Task(row["task_id"], row["part_no"], row["qty"],
                            (db.messages(row["id"], "out") or [""])[0])
                runner.chat_owner[chat] = [(task, row["id"])]
            read_calls = Counter()
            original_read = runner.read_chat

            def counted(chat, *a, **kw):
                read_calls[chat] += 1
                return original_read(chat, *a, **kw)

            runner.read_chat = counted
            stopped = threading.Event()

            def monitor():
                while not stopped.wait(0.05):
                    report["samples"].append({
                        "at": round(time.time(), 3),
                        "foreground": qq.win32gui.GetForegroundWindow(),
                        "cursor": list(qq.win32gui.GetCursorPos()),
                    })

            thread = threading.Thread(target=monitor, daemon=True)
            thread.start()
            try:
                end = time.monotonic() + args.seconds
                report["observer_durations"] = []
                while time.monotonic() < end:
                    if qq.current_chat_name(win) != name:
                        report["interrupted"] = "用户切换了会话，已停止本轮"
                        break
                    start = time.monotonic()
                    observer.observe_once()
                    report["observer_durations"].append(round(time.monotonic() - start, 3))
                    runner.poll_replies()
                    time.sleep(1)
                report["stable_visible"] = signature(messages) == signature(qq.read_messages(win))
            finally:
                stopped.set()
                thread.join()
            report["poll_read_calls"] = dict(read_calls)
            report["observer_state"] = dict(observer.STATE)
            report["saved_after"] = {r["id"]: db.incoming_records(r["id"]) for r in records if r["chat_name"] == name}
            report["read_errors"] = {r["id"]: db.supplier(r["id"])["read_error"]
                                     for r in records if r["chat_name"] == name}
            report["qq_foreground_samples"] = sum(s["foreground"] == win.NativeWindowHandle for s in report["samples"])
            report["foreground_changes"] = sum(a["foreground"] != b["foreground"] for a, b in zip(report["samples"], report["samples"][1:]))
            report["cursor_changes"] = sum(a["cursor"] != b["cursor"] for a, b in zip(report["samples"], report["samples"][1:]))
    report["production_after"] = send_counts(production)
    report["finished_at"] = time.time()
    output = folder / f"probe-{time.time_ns()}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in (
        "samples", "visible_messages", "saved_before", "saved_after", "contacts")}, ensure_ascii=False, indent=2))
    print("REPORT", output)


if __name__ == "__main__":
    main()
