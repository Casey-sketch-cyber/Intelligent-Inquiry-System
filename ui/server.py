"""天芯智能询价系统 · 本地网页界面。

只监听 127.0.0.1，别的电脑访问不到。真正的询价由 main.py 在后台子进程里完成，界面只负责：
展示状态/任务/报价、启动或安全停止询价、补收回复、生成报告、修改设置。

用法：.venv\\Scripts\\python.exe ui\\server.py [--no-browser]
"""
import datetime
import argparse
import json
import msvcrt
import os
import socket
import subprocess
import sys
import time
import threading
import math
import tomllib
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inquiry.runtime import configure_runtime  # noqa: E402

configure_runtime()

import win32gui  # noqa: E402
import win32process  # noqa: E402
from flask import Flask, abort, jsonify, request, send_file, send_from_directory  # noqa: E402

from inquiry import browser, db, report, workbench, analysis, observer  # noqa: E402
from inquiry.qq import exe_of  # noqa: E402
from inquiry.runner import render_message  # noqa: E402
from inquiry.settings import DATA_DIR  # noqa: E402
from ui import service  # noqa: E402

APP_NAME = "天芯智能询价系统"
PORT = service.DEFAULT_PORT
INSTANCE_ID = uuid.uuid4().hex
PY = ROOT / ".venv" / "Scripts" / "python.exe"
SETTINGS_FILE = ROOT / "settings.toml"
LOG_DIR = DATA_DIR / "logs"
REPORT_DIR = DATA_DIR / "reports"
STOP_FILE = DATA_DIR / "stop.flag"
LOCK_FILE = DATA_DIR / "runner.lock"
RUN_OUT = LOG_DIR / "ui_run.out"

app = Flask(__name__, static_folder=str(Path(__file__).parent / "static"), static_url_path="/static")
RUN = {"proc": None, "kind": None, "label": None, "started": None}
SPAWN_LOCK = threading.RLock()


@app.after_request
def no_stale_ui(response):
    response.headers["Cache-Control"] = "no-store"
    return response

STATUS_CN = {"pending": "未询价", "sent": "已发送", "quoted": "已报价", "no_stock": "无货", "asking": "对方反问",
             "failed": "失败", "skipped": "跳过", "identity_unverified": "身份待核实（未发送）", "parse_pending": "回复已保存·待解析", "send_uncertain": "发送结果待核实", "waiting_desktop": "等待 QQ 协助（未发送）"}
TASK_STATUS_CN = {"running": "进行中", "done": "已结束", "stopped": "已停止", "dry-run": "试运行",
                  "needs_review": "已结束·待补收", "failed": "运行异常", "queued": "等待工作机执行", "waiting_verification": "等待网站人工校验"}

# 设置项：(分组, 键, 类型, 说明)。写回 settings.toml 时按这个顺序、带中文注释
SETTINGS_SCHEMA = [
    ("inquiry", "template", str, "询价话术：{part} 换成型号，{qty} 换成数量（5000），{qtyk} 换成数量简写（5k）"),
    ("inquiry", "card_image", str, "发完询价文字后再发的名片图片（放在项目文件夹里；留空就不发）"),
    ("inquiry", "send_card", bool, "询价后发送名片（关闭后保留图片）"),
    ("inquiry", "target_quotes", int, "每个型号要凑齐的有效报价条数"),
    ("inquiry", "first_batch", int, "第一批询价几家"),
    ("inquiry", "next_batch", int, "报价不够时每批再补几家"),
    ("inquiry", "wave_wait_minutes", float, "每批发完后等几分钟，再看要不要补发"),
    ("inquiry", "max_suppliers", int, "一个型号最多询价几家"),
    ("inquiry", "max_wait_minutes", float, "一个型号最多等多少分钟，到时间就用已有的报价出报告"),
    ("inquiry", "skip_recent_hours", float, "同一型号多少小时内问过的供应商不再重复问"),
    ("pacing", "min_interval", float, "两条询价之间最短间隔（秒），太快容易被 QQ 风控"),
    ("pacing", "max_interval", float, "两条询价之间最长间隔（秒）"),
    ("pacing", "max_per_hour", int, "每小时最多发几条"),
    ("filter", "require_enough_stock", bool, "库存数量够需求的供应商优先"),
    ("filter", "only_today", bool, "只询价当天更新过库存的供应商"),
    ("filter", "max_pages", int, "最多翻几页搜索结果"),
    ("llm", "model", str, "解析回复用的 DeepSeek 模型"),
    ("desktop", "foreground_assist", bool, "允许在 QQ 已在前台时使用鼠标键盘；切到其他软件立即暂停，不抢回窗口"),
]


# ---------- 工具 ----------

def load_settings() -> dict:
    return tomllib.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}


def save_settings(data: dict) -> None:
    lines = ["# 天芯智能询价系统配置。可以在界面的「设置」里修改，也可以直接改这个文件。", ""]
    section = None
    for sec, key, typ, desc in SETTINGS_SCHEMA:
        if sec != section:
            if section is not None:
                lines.append("")
            lines.append(f"[{sec}]")
            section = sec
        val = data.get(sec, {}).get(key)
        if val is None:
            continue
        if typ is bool:
            text = "true" if val else "false"
        elif typ is str:
            text = json.dumps(str(val), ensure_ascii=False)
        elif typ is int:
            text = str(int(val))
        else:
            text = f"{float(val):g}"
        lines.append(f"# {desc}")
        lines.append(f"{key} = {text}")
    SETTINGS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def lock_held() -> bool:
    """询价程序运行时会锁住 runner.lock 的第一个字节。"""
    if not LOCK_FILE.exists():
        return False
    try:
        with open(LOCK_FILE, "a+") as f:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    except OSError:
        return True


def run_state() -> dict:
    proc = RUN["proc"]
    if proc is not None and proc.poll() is not None:
        code = proc.returncode
        RUN.update(proc=None, last_exit=code, last_kind=RUN["kind"], last_label=RUN["label"])
    running = lock_held() or (RUN["proc"] is not None and RUN["proc"].poll() is None)
    state = {"running": running, "kind": RUN["kind"] if running else None, "label": RUN["label"] if running else None,
             "started": RUN["started"] if running else None, "stopping": STOP_FILE.exists()}
    if running:
        with db.connect() as c:
            waiting = c.execute("SELECT s.error FROM supplier s JOIN task t ON t.id=s.task_id WHERE s.status='waiting_desktop' AND t.status='running' ORDER BY s.id DESC LIMIT 1").fetchone()
        state["waiting_reason"] = waiting[0] if waiting else None
    if not running and RUN.get("last_exit") not in (None, 0):
        tail = RUN_OUT.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-8:] if RUN_OUT.exists() else []
        state["last_error"] = "\n".join(tail)
    return state


def spawn(args: list[str], kind: str, label: str, module=None):
    with SPAWN_LOCK:
        from inquiry import isolation
        if isolation.DRAINING.is_set():
            raise ValueError("运行环境正在停止，暂不接受新任务")
        if isolation.ENABLED and isolation.MANUAL.is_set():
            raise ValueError("正在人工登录，请先恢复后台运行")
        if isolation.ENABLED:
            args = ["--worker" if isolation.MODE == "worker" else "--vm-guest", *args]
        if run_state()["running"]:
            raise ValueError("已有任务在执行，请稍后再试")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        with open(RUN_OUT, "w", encoding="utf-8") as out:
            entry = ["-m", module] if module else ["main.py"]
            RUN.update(proc=subprocess.Popen([str(PY), "-B", *entry, *args], cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, env=env,
                                             creationflags=subprocess.CREATE_NO_WINDOW),
                       kind=kind, label=label, started=time.time(), last_exit=None)


@app.errorhandler(ValueError)
def invalid_request(error):
    return jsonify(error=str(error)), 400


@app.before_request
def protect_local_actions():
    from inquiry import isolation
    if isolation.ENABLED and request.method in ("POST", "PUT"):
        sending = request.path.endswith("/reply") or request.path.endswith("/retry")
        sending |= request.path == "/api/run" and not (request.get_json(silent=True) or {}).get("dry_run")
        if sending and not isolation.ALLOW_SEND:
            return jsonify(error="运行环境处于只读验收模式，未启用真实发送"), 423
        if isolation.MANUAL.is_set() and (sending or request.path in ("/api/run", "/api/collect")):
            return jsonify(error="正在人工登录，请先恢复后台运行"), 409
        if isolation.DRAINING.is_set() and request.path not in ("/api/stop", "/api/vm/prepare-shutdown"):
            return jsonify(error="运行环境正在停止，暂不接受新操作"), 409
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="仅允许从本系统页面操作"), 403


def qq_status() -> dict:
    """只用窗口枚举判断 QQ 主窗口在不在（不碰 QQ 界面，避免和正在运行的询价程序抢）。"""
    found = []

    def cb(hwnd, _):
        if win32gui.GetClassName(hwnd) == "Chrome_WidgetWin_1" and win32gui.GetWindowText(hwnd) == "QQ" \
                and win32gui.IsWindowVisible(hwnd):
            l, t, r, b = win32gui.GetWindowPlacement(hwnd)[4] if win32gui.IsIconic(hwnd) else win32gui.GetWindowRect(hwnd)
            if r - l >= 400 and b - t >= 300:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if exe_of(pid).lower().endswith("\\qq.exe"):
                    found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return {"ok": bool(found), "text": "已打开" if found else "未找到主窗口"}


def browser_status() -> dict:
    if not browser.cdp_alive():
        return {"ok": False, "text": "未启动", "code": "closed"}
    try:
        with urllib.request.urlopen(f"{browser.CDP_URL}/json/list", timeout=2) as r:
            urls = [t.get("url", "") for t in json.loads(r.read()) if t.get("type") == "page"]
    except OSError:
        urls = []
    if any("member.ic.net.cn/login" in u for u in urls):
        return {"ok": False, "text": "需要登录", "code": "login"}
    if any("ic.net.cn/searchPnCode.php" in u for u in urls):
        return {"ok": False, "text": "需要人工校验", "code": "verification"}
    return {"ok": True, "text": "已就绪", "code": "ok"}


def env_status() -> dict:
    from inquiry.settings import DEEPSEEK_API_KEY  # noqa: F401  （.env 只在启动时读一次）
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    card = (load_settings().get("inquiry", {}).get("card_image") or "").strip()
    return {
        "qq": qq_status(),
        "browser": browser_status(),
        "deepseek": {"ok": bool(key), "text": "已配置" if key else "未配置密钥"},
        "card": {"ok": bool(card) and (ROOT / card).exists(), "text": "已设置" if card and (ROOT / card).exists() else "未设置"},
    }


def task_summaries(limit: int = 50, include_dry: bool = False) -> list[dict]:
    target = int(load_settings().get("inquiry", {}).get("target_quotes", 5))
    with db.connect() as c:
        rows = c.execute(
            """SELECT t.*,
                      COUNT(s.id) AS total,
                      SUM(s.sent_at IS NOT NULL) AS sent,
                      SUM(EXISTS(SELECT 1 FROM message m WHERE m.supplier_id=s.id AND m.direction='in')) AS replied,
                      SUM(s.status = 'no_stock') AS no_stock,
                      SUM(s.status = 'asking') AS asking,
                      SUM(s.status = 'failed') AS failed,
                      (SELECT COUNT(*) FROM quote q JOIN supplier s2 ON s2.id = q.supplier_id
                        WHERE s2.task_id = t.id AND q.valid = 1 AND s2.sent_at IS NOT NULL) AS valid
               FROM task t LEFT JOIN supplier s ON s.task_id = t.id AND s.sent_at IS NOT NULL
               WHERE (? OR t.status != 'dry-run')
               GROUP BY t.id ORDER BY t.id DESC LIMIT ?""",
            (include_dry, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        for k in ("total", "sent", "replied", "no_stock", "asking", "failed", "valid"):
            d[k] = d[k] or 0
        d["status_cn"] = TASK_STATUS_CN.get(d["status"], d["status"])
        d["target"] = target
        out.append(d)
    return out


def best_price(task_id: int) -> float | None:
    prices = [report.tax_price(r["quote"]) for r in db.task_quotes(task_id) if r.get("valid")]
    prices = [p for p in prices if p]
    return min(prices) if prices else None


# ---------- 页面 ----------


@app.get("/api/health")
def health():
    return jsonify(app_id=service.APP_ID, workspace_id=service.WORKSPACE_ID, instance_id=INSTANCE_ID)

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/overview")
def overview():
    tasks = task_summaries(limit=8)
    for t in tasks:
        t["best"] = best_price(t["id"])
    today = datetime.date.today().strftime("%Y-%m-%d")
    since = time.mktime(datetime.date.today().timetuple())
    with db.connect() as c:
        sent_today = c.execute("SELECT COUNT(*) FROM supplier WHERE sent_at >= ?", (since,)).fetchone()[0]
        replied_today = c.execute("SELECT COUNT(*) FROM supplier WHERE last_reply_at >= ?", (since,)).fetchone()[0]
        valid_today = c.execute("SELECT COUNT(*) FROM quote WHERE valid = 1 AND updated_at >= ?", (since,)).fetchone()[0]
    from inquiry import isolation
    from inquiry import collection, jobs
    return jsonify(app_name=APP_NAME, today=today, run=run_state(), env=env_status(), tasks=tasks, observer=observer.STATE,
                   runtime={"virtual_machine": isolation.ENABLED and isolation.MODE != "worker",
                            "dedicated_worker": isolation.ENABLED and isolation.MODE == "worker",
                            "allow_send": isolation.ALLOW_SEND if isolation.ENABLED else True},
                   collection=collection.status(), jobs=jobs.recent() if isolation.MODE == "worker" else [],
                   stats={"sent_today": sent_today, "replied_today": replied_today, "valid_today": valid_today})


@app.get("/api/tasks")
def tasks():
    return jsonify(task_summaries(limit=200, include_dry=request.args.get("dry") == "1"))


@app.get("/api/models")
def models():
    groups = workbench.model_groups(request.args.get("dry") == "1", request.args.get("q", ""))
    for g in groups:
        g["status_cn"] = TASK_STATUS_CN.get(g["status"], g["status"])
        g["target"] = int(load_settings().get("inquiry", {}).get("target_quotes", 5))
    return jsonify(groups)


@app.get("/api/models/detail")
def model_detail():
    key = workbench.part_key(request.args.get("part", ""))
    group = next((g for g in workbench.model_groups(request.args.get("dry") == "1") if g["part_no"] == key), None)
    if not group:
        abort(404)
    details = [task_detail(tid).get_json() for tid in group["task_ids"]]
    by_id = {s["id"]: s for d in details for s in d["suppliers"]}
    latest = details[0]["task"]
    latest.update(part_no=key, runs=group["runs"], task_ids=group["task_ids"], status=group["status"],
                  status_cn=TASK_STATUS_CN.get(group["status"], group["status"]))
    suppliers = [by_id[sid] for sid in group["supplier_ids"]]
    return jsonify(task=latest, suppliers=suppliers, history=[d["task"] for d in details])


@app.get("/api/tasks/<int:task_id>")
def task_detail(task_id: int):
    with db.connect() as c:
        row = c.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
    if row is None:
        abort(404)
    task = {k: row[k] for k in row.keys()}
    task["status_cn"] = TASK_STATUS_CN.get(task["status"], task["status"])
    suppliers = []
    for r in db.task_quotes(task_id):
        if r["sent_at"] is None:
            continue
        q = r["quote"] or {}
        tp = report.tax_price(q) if q else None
        suppliers.append({
            "id": r["id"], "company": r["company"], "chat_name": r["chat_name"], "status": r["status"],
            "status_cn": STATUS_CN.get(r["status"], r["status"]), "error": r["error"],
            "read_error": r.get("read_error"), "last_checked_at": r.get("last_checked_at"),
            "sent_at": r["sent_at"], "last_reply_at": r["last_reply_at"], "valid": bool(r.get("valid")),
            "quote": q, "tax_price": tp, "vs_ref": report.vs_ref(tp, task.get("ref_price")),
            "replies": db.messages(r["id"], "in"),
            "outbox": workbench.outbox_for(r["id"]),
            "suggestions": workbench.suggestions_for(r["id"]),
            "can_reply": bool(r["sent_at"] and r["chat_name"] and r["qq_uin"]),
            "site": {k: r["row"].get(k) for k in ("qty", "batch", "warehouse", "desc", "date", "mfg")},
        })
    outs = [m for s in suppliers for m in db.messages(s["id"], "out") if m != "[名片]"]
    task["message"] = outs[0] if outs else ""
    task["target"] = int(load_settings().get("inquiry", {}).get("target_quotes", 5))
    return jsonify(task=task, suppliers=suppliers)


@app.get("/api/suppliers/<int:sid>/conversation")
def supplier_conversation(sid):
    s = workbench.conversation(sid)
    with db.connect() as c:
        messages = [dict(r) for r in c.execute("SELECT direction,content,seen_at,message_time,source_id FROM message WHERE supplier_id=? ORDER BY seen_at,id", (sid,))]
        unassigned = [dict(r) for r in c.execute("SELECT * FROM inbox WHERE chat_name=? AND assigned_to IS NULL ORDER BY id", (s["chat_name"],))]
    return jsonify(supplier=s, messages=messages, unassigned=unassigned, outbox=workbench.outbox_for(sid), suggestions=workbench.suggestions_for(sid))


@app.post("/api/suppliers/<int:sid>/suggestions")
def reply_suggestions(sid):
    workbench.conversation(sid)
    workbench.enqueue_analysis(sid, refresh=True)
    analysis.start()
    return jsonify(ok=True)


def start_reply_worker(assist=False):
    with SPAWN_LOCK:
        if not run_state()["running"]:
            STOP_FILE.unlink(missing_ok=True)
            args = ["--reply-worker"] + (["--foreground-assist"] if assist else [])
            spawn(args, "reply", "发送已确认的回复")


def start_reply_pump():
    """询价/补收结束后接续用户已明确提交的回复，不与其他执行进程争用 QQ。"""
    def loop():
        while True:
            try:
                if not run_state()["running"] and not STOP_FILE.exists():
                    with db.connect() as c:
                        pending = c.execute("SELECT 1 FROM outbox WHERE status='queued' LIMIT 1").fetchone()
                    if pending:
                        start_reply_worker(load_settings().get("desktop", {}).get("foreground_assist", False))
            except Exception:
                pass
            time.sleep(3)
    threading.Thread(target=loop, daemon=True, name="reply-dispatch").start()


@app.post("/api/suppliers/<int:sid>/reply")
def reply(sid):
    body = request.get_json(force=True)
    if body.get("confirm_send") is not True:
        raise ValueError("请在回复区点击「发送到 QQ」确认")
    result = workbench.queue_reply(sid, body.get("text", ""), body.get("request_id"))
    if result["status"] == "queued":
        start_reply_worker(load_settings().get("desktop", {}).get("foreground_assist", False))
    return jsonify(result), 202


@app.post("/api/outbox/<int:oid>/cancel")
def cancel_reply(oid):
    workbench.cancel_reply(oid)
    return jsonify(ok=True)


@app.post("/api/outbox/<int:oid>/retry")
def retry_reply(oid):
    with db.connect() as c:
        changed = c.execute("UPDATE outbox SET status='queued',error=NULL,updated_at=? WHERE id=? AND status='deferred'", (time.time(), oid)).rowcount
    if not changed:
        raise ValueError("仅能继续执行明确未发送的暂停项；结果待核实的回复不会重发")
    start_reply_worker(load_settings().get("desktop", {}).get("foreground_assist", False))
    return jsonify(ok=True)


@app.post("/api/inbox/<int:mid>/assign")
def assign_reply(mid):
    workbench.assign_message(mid, int(request.get_json(force=True)["supplier_id"]))
    analysis.start()
    return jsonify(ok=True)


@app.post("/api/tasks/<int:task_id>/report")
def task_report(task_id: int):
    path = Path(report.build([task_id]))
    return jsonify(name=path.name)


@app.post("/api/run")
def start_run():
    from inquiry import isolation, jobs
    if isolation.ENABLED and isolation.MODE == "worker":
        result = jobs.submit(request.get_json(force=True), load_settings())
        return jsonify(ok=True, **result), 202
    if run_state()["running"]:
        return jsonify(error="已经有询价程序在运行"), 409
    body = request.get_json(force=True)
    parts = [f"{part}:{qty}" for part, qty in workbench.validate_parts(body.get("parts", []))]
    template = str(body.get("template") or load_settings().get("inquiry", {}).get("template", ""))
    workbench.validate_template(template)
    args = []
    args += ["--message", template]
    if load_settings().get("desktop", {}).get("foreground_assist", False):
        args.append("--foreground-assist")
    if body.get("dry_run"):
        args.append("--dry-run")
    if body.get("max"):
        cap = int(body["max"])
        if not 1 <= cap <= 100:
            raise ValueError("每个型号最多询价家数须为 1–100")
        args += ["--max", str(cap)]
    STOP_FILE.unlink(missing_ok=True)
    spawn(args + parts, "dry-run" if body.get("dry_run") else "run", "、".join(p.split(":")[0] for p in parts))
    return jsonify(ok=True)


@app.post("/api/jobs/<jid>/cancel")
def cancel_job(jid):
    from inquiry import jobs
    jobs.cancel(jid)
    return jsonify(ok=True)


@app.post("/api/jobs/<jid>/resume")
def resume_job(jid):
    from inquiry import jobs, isolation
    if not isolation.ALLOW_SEND:
        return jsonify(error="常规实发尚未开启"), 423
    jobs.resume(jid)
    return jsonify(ok=True)


@app.route("/api/collection", methods=["GET", "POST"])
def collection_control():
    from inquiry import collection, isolation
    if request.method == "POST":
        body = request.get_json(force=True)
        if body.get("action") == "pause" or body.get("enabled") is False:
            return jsonify(collection.pause())
        if body.get("action") != "start":
            raise ValueError("请刷新页面，点击「收集回复」或「暂停收集回复」")
        if isolation.MANUAL.is_set():
            return jsonify(error="全部自动操作已暂停，请先恢复自动执行后再收集"), 409
        return jsonify(collection.start(body.get("request_id"), body.get("task_ids"))), 202
    return jsonify(collection.status())


@app.post("/api/stop")
def stop_run():
    if not run_state()["running"]:
        return jsonify(error="当前没有在运行的询价程序"), 409
    STOP_FILE.write_text("stop", encoding="utf-8")
    from inquiry import isolation
    if isolation.ENABLED and isolation.MODE == "worker":
        isolation.MANUAL.set()
    with db.connect() as c:
        c.execute("UPDATE outbox SET status='deferred',error='用户已停止，请点击继续发送后执行',updated_at=? WHERE status='queued'", (time.time(),))
    return jsonify(ok=True)


@app.post("/api/collect")
def collect():
    from inquiry import collection, isolation
    if isolation.ENABLED:
        body = request.get_json(force=True)
        return jsonify(collection.start(body.get("request_id") or uuid.uuid4().hex, body.get("task_ids"))), 202
    if run_state()["running"]:
        return jsonify(error="询价程序正在运行，请等它结束后再补收"), 409
    ids = [str(int(i)) for i in request.get_json(force=True).get("task_ids", [])]
    if not ids:
        return jsonify(error="没有指定任务"), 400
    assist = ["--foreground-assist"] if load_settings().get("desktop", {}).get("foreground_assist", False) else []
    STOP_FILE.unlink(missing_ok=True)
    spawn([*assist, "--collect", *ids], "collect", "补收任务 #" + "、#".join(ids))
    return jsonify(ok=True)


@app.get("/api/log")
def log_tail():
    """offset=-1 取最后 300 行；之后按字节偏移增量取。"""
    path = LOG_DIR / f"run_{time.strftime('%Y%m%d')}.log"
    name = path.name
    if not path.exists():
        return jsonify(file=name, offset=0, lines=[])
    data = path.read_bytes()
    offset = int(request.args.get("offset", -1))
    if request.args.get("file") != name or offset < 0 or offset > len(data):
        lines = data.decode("utf-8", errors="replace").splitlines()[-300:]
        return jsonify(file=name, offset=len(data), lines=lines)
    return jsonify(file=name, offset=len(data), lines=data[offset:].decode("utf-8", errors="replace").splitlines())


@app.get("/api/reports")
def reports():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(REPORT_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonify([{"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime} for p in files[:100]])


@app.get("/api/reports/<path:name>")
def report_file(name: str):
    return send_from_directory(REPORT_DIR, name, as_attachment=True)


@app.post("/api/open-folder")
def open_folder():
    from inquiry import isolation
    if isolation.ENABLED:
        return jsonify(error="文件位于虚拟机中。请使用报告下载，或打开运行窗口查看"), 400
    target = {"reports": REPORT_DIR, "logs": LOG_DIR, "project": ROOT}.get(request.get_json(force=True).get("which"))
    if target is None:
        abort(400)
    target.mkdir(parents=True, exist_ok=True)
    os.startfile(target)
    return jsonify(ok=True)


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    s.setdefault("inquiry", {})
    s["inquiry"].setdefault("send_card", False)
    s.setdefault("desktop", {}).setdefault("foreground_assist", False)
    preview = ""
    try:
        preview = render_message(s["inquiry"].get("template", ""), "STM32F405RGT6", 5000)
    except (KeyError, IndexError, ValueError):
        preview = "（话术模板格式有误，请检查花括号）"
    return jsonify(settings=s, preview=preview, schema=[[a, b, c.__name__, d] for a, b, c, d in SETTINGS_SCHEMA])


@app.put("/api/settings")
def put_settings():
    body = request.get_json(force=True)
    current = load_settings()
    for sec, key, typ, _ in SETTINGS_SCHEMA:
        if key not in body.get(sec, {}):
            continue
        val = body[sec][key]
        try:
            if typ is bool:
                if not isinstance(val, bool):
                    raise ValueError()
            elif typ is int:
                val = int(val)
            elif typ is float:
                val = float(val)
            else:
                val = str(val)
        except (TypeError, ValueError):
            return jsonify(error=f"「{key}」的值不对：{val}"), 400
        if typ in (int, float) and (not math.isfinite(val) or val < 0):
            return jsonify(error=f"「{key}」不能是负数"), 400
        if key in {"target_quotes", "first_batch", "next_batch", "max_suppliers", "max_wait_minutes", "max_per_hour", "max_pages"} and val <= 0:
            return jsonify(error=f"「{key}」必须大于 0"), 400
        current.setdefault(sec, {})[key] = val
    try:
        workbench.validate_template(current.get("inquiry", {}).get("template", ""))
        render_message(current.get("inquiry", {}).get("template", ""), "X", 1)
    except (KeyError, IndexError, ValueError) as e:
        return jsonify(error=str(e) or "话术模板格式不正确"), 400
    p = current.get("pacing", {})
    if p.get("min_interval", 0) > p.get("max_interval", 0):
        return jsonify(error="最短间隔不能大于最长间隔"), 400
    if float(p.get("min_interval", 20)) < 20 or int(p.get("max_per_hour", 60)) > 60:
        return jsonify(error="发送间隔至少 20 秒，每小时最多 60 条；这不能保证第三方不触发风控"), 400
    save_settings(current)
    return jsonify(ok=True)


@app.get("/api/card")
def card():
    name = (load_settings().get("inquiry", {}).get("card_image") or "").strip()
    path = ROOT / name if name else None
    if not path or not path.exists():
        abort(404)
    resp = send_file(path)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/card")
def upload_card():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="没有收到图片"), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
        return jsonify(error="只支持 jpg / png / bmp / webp 图片"), 400
    name = f"名片{ext}"
    f.save(ROOT / name)
    s = load_settings()
    s.setdefault("inquiry", {})["card_image"] = name
    save_settings(s)
    return jsonify(ok=True, name=name)


@app.post("/api/browser/open")
def open_browser():
    if run_state()["running"]:
        return jsonify(error="询价程序正在使用专用浏览器，请稍后再试"), 409
    if browser.cdp_alive():
        req = urllib.request.Request(f"{browser.CDP_URL}/json/new?https://member.ic.net.cn/login.php", method="PUT")
        urllib.request.urlopen(req, timeout=3).close()
    else:
        browser.launch_edge("https://member.ic.net.cn/login.php")
    return jsonify(ok=True)


@app.post("/api/deepseek/test")
def deepseek_test():
    from openai import OpenAI

    from inquiry.settings import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
    if not DEEPSEEK_API_KEY:
        return jsonify(error="没有配置 DEEPSEEK_API_KEY（在项目文件夹的 .env 里）"), 400
    try:
        models = [m.id for m in OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, timeout=15).models.list().data]
    except Exception as e:
        return jsonify(error=f"连接失败：{e}"), 502
    return jsonify(ok=True, models=models)


# ---------- 启动 ----------

def start_background_services():
    """本地界面与虚拟机来宾共享启动逻辑；重启不会续发残留队列。"""
    if not run_state()["running"]:
        with db.connect() as c:
            c.execute("UPDATE outbox SET status='deferred',error='系统已重启，请点击继续发送',updated_at=? WHERE status='queued'", (time.time(),))
            c.execute("UPDATE outbox SET status='uncertain',error='上次进程中断，请核实 QQ，不自动重发',updated_at=? WHERE status='sending'", (time.time(),))
    analysis.start()
    from inquiry import collection
    observer.STATE["text"] = "仅在点击「收集回复」时读取一轮，不自动核对"
    collection.start_pump()
    start_reply_pump()

def open_window(url: str):
    """用 D 盘独立 Edge 配置打开界面，避免项目页面缓存写入默认用户配置。"""
    edge = next((p for p in browser.EDGE_CANDIDATES if p.exists()), None)
    if edge:
        subprocess.Popen([str(edge), *browser.edge_args(DATA_DIR / "ui_edge_profile", "edge-ui"),
                          f"--app={url}", "--window-size=1440,920"], cwd=ROOT, env=os.environ.copy())
    else:
        print(f"没有找到 Edge，请使用配置目录位于 D 盘的浏览器打开：{url}")


def main():
    import ctypes

    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, help="指定固定端口；被占用时明确报错，不自动改端口")
    args = parser.parse_args()
    if (DATA_DIR / "vm_guest.json").exists() or (DATA_DIR / "worker.json").exists():
        raise RuntimeError("此为配对执行环境，请使用专用启动入口；工作台在办公电脑上打开")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    ctypes.windll.kernel32.SetConsoleTitleW(f"{APP_NAME} · 后台服务（请勿关闭）")
    lock = service.acquire_lock()
    if lock is None:
        for _ in range(20):
            url = service.existing_url()
            if url:
                if args.port is not None and url != f"http://127.0.0.1:{args.port}/":
                    raise RuntimeError(f"界面已运行于 {url}，与指定端口 {args.port} 不同；请先关闭该界面后台服务再使用固定端口")
                print(f"{APP_NAME} 界面已经在运行：{url}", flush=True)
                if not args.no_browser:
                    open_window(url)
                return
            time.sleep(0.2)
        raise RuntimeError("界面服务正在启动或无响应，请稍后重试；没有打开其他程序的页面")
    import logging

    import flask.cli

    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # 不打印每个请求
    flask.cli.show_server_banner = lambda *a, **k: None  # 不打印英文的开发服务器提示
    server = None
    try:
        server = service.bind_server(app, args.port or PORT, fallback=args.port is None)
        url = f"http://127.0.0.1:{server.port}/"
        service.write_state(server.port, INSTANCE_ID)
        if args.port is None and server.port != PORT:
            print(f"默认端口 {PORT} 被占用，已自动选择 {server.port}，原服务不受影响。", flush=True)
        print(f"{APP_NAME} 已启动：{url}", flush=True)
        print("这是界面的后台服务窗口，请不要关闭；关闭它界面就不能用了（已经在后台运行的询价不受影响）。", flush=True)
        start_background_services()
        if not args.no_browser:
            open_window(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        service.STATE_FILE.unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    main()
