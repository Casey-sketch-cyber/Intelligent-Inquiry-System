"""已配对工作机的一次三型号实发送测试；常规服务仍保持只读。

只接受办公端配对密钥签名的有限授权，不修改 worker.json 的发送配置。
"""
import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sys
import time
from contextlib import ExitStack
from pathlib import Path
import subprocess
import threading

try:
    from .reset_history import safe_path, worker_ui, check_tasks
except ImportError:
    from reset_history import safe_path, worker_ui, check_tasks

PARTS = ("LPC2387FBD100K", "LPC1788FBD208K", "LPC2366FBD100K")
TEMPLATE = "您好，请问 {part}，数量 {qty}pcs 有现货吗？请报含税单价、批次和交期，谢谢。"
ACTIVE = {"proc": None, "request_id": None, "starting": False}
START_LOCK = threading.Lock()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def sign(value, secret):
    return hmac.new(secret.encode("ascii"), canonical(value), hashlib.sha256).hexdigest()


def validate_permit(value, cfg, now=None):
    now = time.time() if now is None else now
    if not isinstance(value, dict):
        raise ValueError("Live trial authorization is invalid")
    body = {k: v for k, v in value.items() if k != "signature"}
    signature = value.get("signature", "")
    if (not isinstance(signature, str) or not hmac.compare_digest(signature, sign(body, cfg["secret"]))
            or body.get("kind") not in ("tianxin-live-trial-v1", "tianxin-live-trial-v2") or body.get("worker_id") != cfg["worker_id"]
            or cfg.get("allow_send") is not False or not re.fullmatch(r"[0-9a-f]{32}", str(body.get("request_id", "")))):
        raise ValueError("Live trial authorization does not match this paired worker")
    created, expires = body.get("created_at"), body.get("expires_at")
    if (type(created) not in (int, float) or type(expires) not in (int, float)
            or not math.isfinite(created) or not math.isfinite(expires)
            or not created - 120 <= now < expires or not 0 < expires - created <= 86400):
        raise ValueError("Live trial authorization has expired or clocks do not match")
    limit = 3 if body.get("kind") == "tianxin-live-trial-v2" else 1
    if (body.get("parts") != [{"part": p, "qty": 5000} for p in PARTS]
            or body.get("template") != TEMPLATE or type(body.get("max_per_model")) is not int
            or body["max_per_model"] != limit or type(body.get("max_total")) is not int or body["max_total"] != limit * 3
            or not re.fullmatch(r"[0-9a-f]{64}", str(body.get("task_fingerprint", "")))):
        raise ValueError("Live trial model, quantity or sending quota is outside the signed scope")
    return body


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


class TrialBudget:
    """按型号、公司与 QQ 去重；发送调用前持久化认领，未知结果不能重发。"""
    def __init__(self, folder, permit, deadline):
        self.folder, self.permit, self.deadline = folder, permit, deadline
        self.active = None
        self.selected = set()
        self.blocked = set()
        self.uins = set()
        self.attempts = []

    def begin(self, part, company, uin=None):
        key = (part, re.sub(r"\W+", "", company or "").casefold())
        count = sum(p == part for p, _ in self.selected)
        max_attempts = 6 if self.permit.get("max_per_model", 1) == 3 else 1
        if (part not in PARTS or not company or key in self.selected or key in self.blocked or count >= max_attempts
                or uin and (part, str(uin)) in self.uins):
            raise ValueError("Supplier was already attempted, or the model attempt limit was reached")
        self.selected.add(key)
        if uin:
            self.uins.add((part, str(uin)))
        self.active = {"part": part, "company": company, "invoked": False}

    def send(self, original, win, text, *, expect_chat=None, company=None):
        from inquiry.identity import SendUncertainError
        from inquiry.qq import DesktopDeferred
        if time.monotonic() >= self.deadline:
            raise DesktopDeferred("本轮限时测试已结束，未继续发送")
        item = self.active
        part_attempts = sum(a["part"] == item["part"] for a in self.attempts) if item else 0
        if (not item or item["invoked"] or len(self.attempts) >= self.permit.get("max_total", 3)
                or part_attempts >= self.permit.get("max_per_model", 1)):
            raise SendUncertainError("本轮发送名额已认领，禁止再次调用发送")
        if (company != item["company"] or not expect_chat
                or text != TEMPLATE.format(part=item["part"], qty=5000)):
            raise ValueError("Message, quantity or supplier does not match the permitted trial")
        item["invoked"] = True
        record = {"part": item["part"], "company": company, "chat_name": expect_chat,
                  "claimed_at": time.time(), "state": "claimed_result_unknown"}
        self.attempts.append(record)
        write_json(self.folder / "send-attempts.json", self.attempts)
        try:
            result = original(win, text, expect_chat=expect_chat, company=company)
        except DesktopDeferred:
            record["state"] = "not_sent_desktop_deferred"
            write_json(self.folder / "send-attempts.json",self.attempts)
            raise
        record["state"] = "send_function_confirmed"
        write_json(self.folder / "send-attempts.json", self.attempts)
        return result


def claim_trial(root, permit):
    parent = safe_path(root / "data/lan/live-trials", root)
    parent.mkdir(parents=True, exist_ok=True)
    folder = safe_path(parent / permit["request_id"], root)
    try:
        folder.mkdir()  # 同请求的重跑、双击和进程中断均不自动恢复发送。
    except FileExistsError as exc:
        raise ValueError("This live trial was already claimed; inspect its results, do not resend") from exc
    write_json(folder / "claim.json", {"request_id": permit["request_id"], "claimed_at": time.time(),
                                        "parts": permit["parts"], "state": "claimed_no_auto_retry"})
    return folder


def audit(folder, event, **fields):
    with (folder / "audit.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": time.time(), "event": event, **fields}, ensure_ascii=False) + "\n")


def desktop_snapshot(win):
    """只记录句柄/进程名，不记录其他软件标题或内容。"""
    from inquiry import qq
    foreground = qq.win32gui.GetForegroundWindow()
    _, pid = qq.win32process.GetWindowThreadProcessId(foreground) if foreground else (0, 0)
    return {"target": win.NativeWindowHandle, "foreground": foreground,
            "foreground_exe": Path(qq.exe_of(pid)).name if pid else "", "iconic": bool(qq.win32gui.IsIconic(win.NativeWindowHandle))}


def occupied(suppliers):
    return [s for s in suppliers if s.get("sent_at") is not None or s.get("attempted_at") is not None and s.get("status")!="waiting_desktop"
            or s.get("status") in ("sending", "send_uncertain")]


def audit_read_result(folder, name, sid, before, seen, ok):
    from inquiry import db
    after = {r.get("id") for r in db.incoming_records(sid)}
    expected = {str(m["id"]) for m in seen.messages if not m.get("self") and m.get("id")} if seen else set()
    with db.connect() as c:
        saved = {str(r[0]) for r in c.execute("SELECT m.source_id FROM message m JOIN supplier s ON s.id=m.supplier_id "
                                            "WHERE m.direction='in' AND s.chat_name=? UNION SELECT source_id FROM inbox WHERE chat_name=?", (name, name)) if r[0] is not None}
    audit(folder, "read_finished", chat=name, supplier_id=sid, complete=ok, saved_count=len(after),
          previous_preserved=before.issubset(after), visible_reply_ids=sorted(expected), unsaved_visible_ids=sorted(expected - saved),
          unidentified_reply_count=sum(not m.get("self") and not m.get("id") for m in seen.messages) if seen else None,
          error=db.supplier(sid).get("read_error"))


def idle_readonly(runner,seconds,poll=True):
    """Use pacing waits to collect one reply at a time, without ever processing the outbox."""
    end=time.monotonic()+seconds
    next_poll=0
    while time.monotonic()<end and not runner.stop_requested():
        if poll and time.monotonic()>=next_poll:
            runner.poll_replies(max_reads=1)
            next_poll=time.monotonic()+8
        time.sleep(min(.5,max(0,end-time.monotonic())))


def run_trial(root, cfg, permit):
    from inquiry import db, isolation, qq, dispatch, browser, report
    from inquiry.runner import Runner, Task, log, STOP_FILE
    from ui import service
    from playwright.sync_api import sync_playwright
    _, _, call = worker_ui(root, cfg)
    state = call("api/status")
    own_remote_start = state.get("trial_request_id") == permit["request_id"]
    if not state.get("manual") and not state.get("manual_ready") or state.get("running") and not own_remote_start:
        raise RuntimeError("Pause the worker and wait until idle before this trial")
    with ExitStack() as stack:
        lock = service.acquire_lock(root / "data/runner.lock")
        if lock is None:
            raise RuntimeError("Another QQ operation is active; no trial was started")
        stack.enter_context(lock)
        extending = permit.get("kind") == "tianxin-live-trial-v2"
        limit = permit.get("max_per_model", 1)
        with db.connect() as conn:
            if extending:
                check_tasks(conn, permit["task_fingerprint"])
                existing = [dict(r) for r in conn.execute("SELECT * FROM task")]
                if len(existing) != 3 or {r["part_no"] for r in existing} != set(PARTS) or any(r["qty"] != 5000 for r in existing):
                    raise ValueError("The three existing model tasks do not match this trial")
            elif conn.execute("SELECT COUNT(*) FROM task").fetchone()[0]:
                raise RuntimeError("Old tasks have not been cleared; no trial was started")
        folder = claim_trial(root, permit)
        deadline = time.monotonic() + (1500 if extending else 600)
        budget = TrialBudget(folder, permit, deadline)
        last_history = {}

        class BoundedRunner(Runner):
            def stop_requested(self):
                if time.monotonic() >= deadline:
                    self._stop = True
                return super().stop_requested()

            def idle(self, seconds, poll=True):
                # 本轮不处理发件箱，也不自动回复任何供应商。
                return idle_readonly(self,seconds,poll)

            def open_chat(self, task, row):
                audit(folder, "supplier_open", part=task.part_no, company=row["company"], uin=row["qqs"][0]["uin"])
                return super().open_chat(task, row)

            def read_chat(self, name, task, sid, snapshot=None):
                audit(folder, "read_started", chat=name, part=task.part_no, supplier_id=sid)
                last_history.clear()
                before = {r.get("id") for r in db.incoming_records(sid)}
                ok = super().read_chat(name, task, sid, snapshot=snapshot)
                seen = last_history.get("snapshot", snapshot)
                audit_read_result(folder, name, sid, before, seen, ok)
                return ok

        runner = BoundedRunner([] if extending else [(p, 5000) for p in PARTS], message=TEMPLATE, max_suppliers=limit)
        runner.card_image = None
        runner.async_analysis = True  # 原工作机解析线程处理持久化任务，不占用 QQ。
        runner.filter = dict(runner.filter, max_pages=1)
        runner.pacing = dict(runner.pacing)
        if int(runner.pacing.get("max_per_hour", 60)) < 1:
            raise ValueError("Hourly sending limit must be positive")
        runner.pacing["min_interval"] = max(45, float(runner.pacing.get("min_interval", 20)))
        runner.pacing["max_interval"] = max(60, runner.pacing["min_interval"], float(runner.pacing.get("max_interval", 45)))
        original_text, original_image, original_recent = qq.send_text, qq.send_image, qq.open_recent
        original_history = qq.read_inquiry_history
        result = {"request_id": permit["request_id"], "started_at": time.time(), "parts": permit["parts"],
                  "state": "running", "max_text_sends": permit.get("max_total", 3), "card_sending": False, "auto_reply": False}
        write_json(folder / "result.json", result)

        def guarded_text(win, text, *, expect_chat=None, company=None):
            return budget.send(original_text, win, text, expect_chat=expect_chat, company=company)

        def no_images(*args, **kwargs):
            raise ValueError("Card/image sending is outside this text-only trial")

        def tracked_recent(win, name, *args, **kwargs):
            before = qq.current_chat_name(win)
            ok = original_recent(win, name, *args, **kwargs)
            audit(folder, "recent_open", target=name, previous=before, changed=before != name, ok=bool(ok))
            return ok

        def tracked_history(*args, **kwargs):
            snapshot = original_history(*args, **kwargs)
            last_history["snapshot"] = snapshot
            return snapshot

        try:
            isolation.enable_worker()  # 复核物理设备；常规 worker.json 必须仍为只读。
            isolation.MANUAL.clear()
            isolation.ALLOW_SEND = True  # 仅此进程、仅下面的精确话术/型号/次数守卫。
            qq.send_text, qq.send_image, qq.open_recent = guarded_text, no_images, tracked_recent
            qq.read_inquiry_history = tracked_history
            if not own_remote_start:
                STOP_FILE.unlink(missing_ok=True)
            log(f"已授权实发送测试：3 个型号，每款 5000pcs，每款累计最多 {limit} 家；不发名片、不自动回复、不重发")
            with sync_playwright() as pw:
                _, context = browser.connect(pw)
                runner.context = context
                if extending:
                    for part in PARTS:
                        row = next(r for r in existing if r["part_no"] == part)
                        task = Task(row["id"], part, 5000, TEMPLATE.format(part=part, qty=5000))
                        with db.connect() as conn:
                            conn.execute("UPDATE task SET status='running',finished_at=NULL WHERE id=?",(task.id,))
                        suppliers = [dict(s) for s in db.suppliers(task.id)]
                        for s in suppliers:
                            budget.blocked.add((part, re.sub(r"\W+", "", s["company"]).casefold()))
                            if s.get("qq_uin"):
                                budget.uins.add((part, str(s["qq_uin"])))
                        task.sent = len(occupied(suppliers))
                        runner.tasks.append(task)
                        for s in suppliers:
                            if s["sent_at"] and s["chat_name"]:
                                runner.chat_owner.setdefault(s["chat_name"], []).append((task, s["id"]))
                else:
                    runner.make_tasks()
                runner.win = qq.ready_main_window()
                if extending:
                    audit(folder, "desktop_before", **desktop_snapshot(runner.win))
                    isolation.ensure_qq_foreground(runner.win)
                    audit(folder, "desktop_ready", **desktop_snapshot(runner.win))
                for task in runner.tasks:
                    if runner.stop_requested():
                        break
                    if len(occupied([dict(s) for s in db.suppliers(task.id)])) >= limit:
                        continue
                    runner.load_candidates(context, task)
                    if not task.candidates:
                        log(f"[实发送测试] {task.part_no}：无符合当前筛选的供应商，未发送")
                        continue
                    tries = 0
                    while task.candidates and tries < (6 if extending else 1) and not runner.stop_requested():
                        if len(occupied([dict(s) for s in db.suppliers(task.id)])) >= limit:
                            break
                        row = task.candidates.popleft()
                        try:
                            budget.begin(task.part_no, row["company"], row["qqs"][0]["uin"])
                        except ValueError:
                            continue
                        tries += 1
                        outcome = runner.inquire(task, row)
                        audit(folder, "inquiry_result", part=task.part_no, company=row["company"], result=outcome)
                        log(f"[实发送测试] {task.part_no}：" + ("已询价，等待回复" if outcome is True else "未确认成功，不自动重发该公司"))
                        if extending and (outcome is None or any(s["status"] == "send_uncertain" for s in db.suppliers(task.id))):
                            runner._stop = True
                            break
                end = min(deadline, time.monotonic() + (300 if extending else 90))
                while runner.chat_owner and not runner.stop_requested() and time.monotonic() < end:
                    runner.poll_replies(max_reads=1)
                    runner.idle(min(10, max(0, end - time.monotonic())), poll=False)
            result["state"] = "finished"
        except BaseException as exc:
            result.update(state="stopped_with_error", error=str(exc)[:500])
            raise
        finally:
            isolation.ALLOW_SEND = False
            isolation.MANUAL.set()
            qq.send_text, qq.send_image, qq.open_recent = original_text, original_image, original_recent
            qq.read_inquiry_history = original_history
            dispatch.recover_interrupted()
            result["finished_at"] = time.time()
            result["send_calls_claimed"] = len(budget.attempts)
            result["tasks"] = []
            for task in runner.tasks:
                db.finish_task(task.id, "stopped")
                suppliers = [dict(s) for s in db.suppliers(task.id)]
                result["tasks"].append({"id": task.id, "part": task.part_no, "qty": task.qty,
                                        "suppliers": [{k: s.get(k) for k in ("id", "company", "chat_name", "status", "error", "read_error", "sent_at", "last_reply_at", "last_checked_at")} for s in suppliers]})
            if extending and result["state"] == "finished" and any(
                    sum(s["sent_at"] is not None for s in t["suppliers"]) < limit for t in result["tasks"]):
                result["state"] = "needs_review"
            write_json(folder / "result.json", result)
            if runner.tasks:
                try:
                    path = report.build([t.id for t in runner.tasks])
                    log("实发送测试报告：" + str(path))
                except Exception as exc:
                    log("测试数据已保存，报告生成失败：" + str(exc)[:200])
            log("本轮实发送测试结束；工作机继续暂停，常规发送仍锁定。结果：" + str(folder / "result.json"))
        return result


def active_status():
    proc = ACTIVE["proc"]
    return {"trial_running": ACTIVE["starting"] or proc is not None and proc.poll() is None, "trial_request_id": ACTIVE["request_id"], "trial_capability": 2}


def grant_foreground(pid):
    import ctypes
    try:
        return bool(ctypes.windll.user32.AllowSetForegroundWindow(int(pid)))
    except (TypeError, ValueError, OSError):
        return False


def start_remote(root, cfg, value):
    """唯一新增写入口；不能提交代码、任意命令或扩大签名范围。"""
    from inquiry import isolation
    from ui import server
    permit = validate_permit(value, cfg)
    if permit["kind"] != "tianxin-live-trial-v2":
        raise ValueError("Remote trials require the bounded continuation authorization")
    with START_LOCK:
        if not isolation.MANUAL.is_set() or server.run_state()["running"] or active_status()["trial_running"]:
            raise ValueError("Pause the idle worker before starting a signed trial")
        path = safe_path(root / "data/lan/live-trial-requests" / (permit["request_id"] + ".json"), root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        log = root / "data/logs" / ("live-trial-" + permit["request_id"] + ".log")
        server.STOP_FILE.unlink(missing_ok=True)
        ACTIVE.update(starting=True, request_id=permit["request_id"])
        try:
            with log.open("w", encoding="utf-8") as output:
                proc = subprocess.Popen([str(root / ".venv/Scripts/python.exe"), "-B", "-m", "lanhost.live_trial",
                                         "--root", str(root), "--permit", str(path)], cwd=root,
                                        stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            ACTIVE.update(proc=proc)
            granted = grant_foreground(proc.pid)
        finally:
            ACTIVE["starting"] = False
        return {"accepted": True, "request_id": permit["request_id"], "foreground_permission_granted": granted}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--permit", required=True, type=Path)
    parser.add_argument("--reset-request", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    root = safe_path(args.root, args.root)
    os.chdir(root)
    sys.path.insert(0, str(root))
    from inquiry.runtime import ROOT
    from lanhost import config
    if root != ROOT:
        raise RuntimeError("Loaded app does not match the D: worker directory")
    cfg = config.worker_config()
    permit = validate_permit(config.read_json(args.permit), cfg)
    if args.check_only:
        if args.reset_request is None:
            raise ValueError("The signed live trial must include its matching cleanup request")
        reset = config.read_json(args.reset_request)
        if (reset.get("kind") != "tianxin-history-reset" or reset.get("worker_id") != cfg["worker_id"]
                or reset.get("task_fingerprint") != permit["task_fingerprint"]):
            raise ValueError("The cleanup request does not match the signed live trial")
        print("Live trial permit verified: 3 models, 5000 pcs each, at most 3 text messages.")
        return
    result = run_trial(root, cfg, permit)
    print(json.dumps({"state": result["state"], "task_ids": [t["id"] for t in result["tasks"]],
                      "send_calls_claimed": result["send_calls_claimed"], "regular_sending_locked": True}))


if __name__ == "__main__":
    main()
