"""Paired worker diagnostics and reviewed application updates; no remote shell."""
import base64
import hashlib
import io
import json
import re
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from inquiry.runtime import ROOT
from lanhost.reset_history import safe_path

LOCK = threading.Lock()
APP_ROOTS = {"inquiry", "lanhost", "ui", "vmhost"}
EXTENSIONS = {".py", ".html", ".js", ".css", ".ps1"}
ORIGINS = {"wpa.qq.com", "wpa1.qq.com", "qidian.qq.com", "admin.qidian.qq.com"}


def has_qq_origin(text):
    return any(re.search(r"(?<![\w.-])"+re.escape(origin)+r"(?![\w.-])",text,re.I) for origin in ORIGINS)


def idle():
    from inquiry import isolation
    from ui import server
    from lanhost import live_trial
    if not isolation.ENABLED or isolation.MODE != "worker":
        raise ValueError("Diagnostics are available only on the paired worker")
    if not isolation.MANUAL.is_set() or server.run_state()["running"] or live_trial.active_status()["trial_running"]:
        raise ValueError("Pause the worker and wait until idle before updating")


def windows():
    from inquiry import qq
    result = []
    def visit(hwnd, _):
        if not qq.win32gui.IsWindowVisible(hwnd):
            return True
        _, pid = qq.win32process.GetWindowThreadProcessId(hwnd)
        exe = Path(qq.exe_of(pid)).name.lower()
        if exe in {"qq.exe", "msedge.exe"}:
            result.append({"hwnd": hwnd, "pid": pid, "exe": exe,
                           "title": qq.win32gui.GetWindowText(hwnd)[:200],
                           "rect": qq.win32gui.GetWindowRect(hwnd),
                           "iconic": bool(qq.win32gui.IsIconic(hwnd))})
        return True
    qq.win32gui.EnumWindows(visit, None)
    return result


def checked_window(hwnd):
    if type(hwnd) is not int or hwnd <= 0:
        raise ValueError("Invalid target window")
    result = next((w for w in windows() if w["hwnd"] == hwnd), None)
    if not result:
        raise ValueError("Target must be a visible QQ or Edge window on the worker")
    return result


def tree(hwnd):
    from inquiry import qq
    target = checked_window(hwnd)
    result, end = [], time.monotonic() + 8
    with qq.auto.UIAutomationInitializerInThread():
        root = qq.auto.ControlFromHandle(hwnd)
        def walk(control, path):
            if len(result) >= 500 or time.monotonic() > end:
                return
            result.append({"path": path, "name": (control.Name or "")[:250],
                           "class": control.ClassName, "type": control.ControlTypeName,
                           "automation_id": control.AutomationId})
            if target["exe"] == "msedge.exe" and control.ControlTypeName == "DocumentControl":
                return  # Web pages must not starve native browser permission controls.
            if len(path) < 20:
                for index, child in enumerate(control.GetChildren()):
                    if len(result) >= 500 or time.monotonic() > end:
                        break
                    walk(child, path + [index])
        walk(root, [])
    return {"window": checked_window(hwnd), "controls": result,
            "truncated": len(result) >= 500 or time.monotonic() > end}


def snapshot(hwnd):
    """Capture only the selected app window, without activation or screen input."""
    from inquiry import qq
    import ctypes
    import win32ui
    from PIL import Image
    target = checked_window(hwnd)
    if target["iconic"]:
        raise ValueError("Window is minimized; no screenshot was captured")
    left, top, right, bottom = target["rect"]
    width, height = right-left, bottom-top
    if not 0 < width <= 7680 or not 0 < height <= 4320:
        raise ValueError("Window size is invalid")
    source = qq.win32gui.GetWindowDC(hwnd)
    dc = memory = bitmap = None
    try:
        dc = win32ui.CreateDCFromHandle(source)
        memory = dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(dc, width, height)
        memory.SelectObject(bitmap)
        if not ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), 2):
            raise ValueError("This client could not supply a window screenshot")
        picture = Image.frombuffer("RGB", (width, height), bitmap.GetBitmapBits(True), "raw", "BGRX", 0, 1)
        picture.thumbnail((1920, 1080))
        output = io.BytesIO()
        picture.save(output, format="PNG")
        return {"window": target, "method": "PrintWindow", "image": base64.b64encode(output.getvalue()).decode(),
                "limitation": "GPU windows may render black; check the UI control tree as well."}
    finally:
        if bitmap: qq.win32gui.DeleteObject(bitmap.GetHandle())
        if memory: memory.DeleteDC()
        if dc: dc.DeleteDC()
        qq.win32gui.ReleaseDC(hwnd, source)


def protocol_action(value):
    """Only Edge's observed QQ protocol prompt; never QQ send/login buttons."""
    from inquiry import qq
    hwnd, path = value.get("hwnd"), value.get("path")
    target = checked_window(hwnd)
    if target["exe"] != "msedge.exe" or not isinstance(path, list) or not 1 <= len(path) <= 20 or any(type(i) is not int or not 0 <= i < 1000 for i in path):
        raise ValueError("Only a specific observed Edge protocol control is allowed")
    observed = tree(hwnd)
    documents = [c["path"] for c in observed["controls"] if c["type"] == "DocumentControl"]
    def native(item):
        return not any(item["path"][:len(doc)] == doc for doc in documents)
    all_names = "\n".join(c["name"] for c in observed["controls"] if native(c))
    if "qq" not in all_names.casefold() or not has_qq_origin(all_names):
        raise ValueError("QQ protocol origin was not visible in this Edge dialog")
    expected = next((c for c in observed["controls"] if c["path"] == path), None)
    if not expected or not native(expected) or expected["name"] != value.get("name"):
        raise ValueError("The observed control changed; inspect the dialog again")
    name = expected["name"].strip()
    button = expected["type"] == "ButtonControl" and re.fullmatch(r"(?:打开|启动|允许)(?:\s*QQ)?|Open(?:\s+(?:QQ|Microsoft QQ))?|Allow", name, re.I)
    checkbox = expected["type"] == "CheckBoxControl" and any(s in name.casefold() for s in ("始终允许", "总是允许", "always allow"))
    if not button and not checkbox:
        raise ValueError("Control is outside QQ protocol permission handling")
    with qq.auto.UIAutomationInitializerInThread():
        control = qq.auto.ControlFromHandle(hwnd)
        for index in path:
            if control.ControlTypeName == "DocumentControl": raise ValueError("Web-page controls are not browser permission dialogs")
            children = control.GetChildren()
            if index >= len(children): raise ValueError("Control path changed")
            control = children[index]
        if control.Name != expected["name"] or control.ControlTypeName != expected["type"]:
            raise ValueError("Control identity changed")
        checked_window(hwnd)
        if button:
            pattern = control.GetPattern(qq.auto.PatternId.InvokePattern)
            if not pattern: raise ValueError("The protocol button has no UI Automation Invoke capability")
            pattern.Invoke()
        else:
            pattern = control.GetPattern(qq.auto.PatternId.TogglePattern)
            if not pattern: raise ValueError("The checkbox has no UI Automation Toggle capability")
            if int(pattern.ToggleState) == 0: pattern.Toggle()
    return {"ok": True, "action": "open_qq" if button else "remember_qq_origin"}


def confirm_qq_dialog(cancelled=lambda:False, opened=lambda:False, wait=6):
    """Handle the observed native prompt on a dedicated worker's first QQ launch."""
    end = time.monotonic()+wait
    while time.monotonic()<end:
        if cancelled() or opened(): return False
        for win in windows():
            if win["exe"]!="msedge.exe" or win["iconic"]: continue
            view = tree(win["hwnd"])
            controls = view["controls"]
            native_text="\n".join(c["name"] for c in controls if c["type"]!="DocumentControl")
            if "qq" not in native_text.casefold() or not has_qq_origin(native_text): continue
            checkbox=next((c for c in controls if c["type"]=="CheckBoxControl" and "始终允许" in c["name"]),None)
            button=next((c for c in controls if c["type"]=="ButtonControl" and c["name"] in {"打开","打开 QQ","Open QQ"}),None)
            if not button: continue
            if checkbox: protocol_action({"hwnd":win["hwnd"],"path":checkbox["path"],"name":checkbox["name"]})
            if cancelled(): return False
            protocol_action({"hwnd":win["hwnd"],"path":button["path"],"name":button["name"]})
            return True
        time.sleep(.3)
    return False


def file_target(root, name):
    if (not isinstance(name, str) or "\\" in name or name.startswith("/")
            or ".." in name.split("/") or ":" in name or len(name.split("/")) < 2
            or name.split("/")[0] not in APP_ROOTS or Path(name).suffix not in EXTENSIONS):
        raise ValueError("Update target is outside application source")
    return safe_path(root / name, root)


def known_trial_supplier(sid):
    from inquiry import db
    from lanhost.live_trial import PARTS
    if type(sid) is not int: raise ValueError("Invalid supplier")
    supplier = db.supplier(sid)
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM task WHERE id=?", (supplier["task_id"],)).fetchone() if supplier else None
    if (not task or task["part_no"] not in PARTS or task["qty"] != 5000
            or supplier["sent_at"] or supplier.get("attempted_at") or supplier["status"] in {"sending", "send_uncertain"}
            or not re.fullmatch(r"[0-9]{5,12}", str(supplier.get("qq_uin", "")))):
        raise ValueError("Only a known unsent trial supplier may be inspected")
    return supplier


def versions(root=ROOT):
    result = {}
    for base in sorted(APP_ROOTS):
        for file in sorted((root / base).rglob("*")):
            if file.is_file() and file.suffix in EXTENSIONS and "__pycache__" not in file.parts:
                name = file.relative_to(root).as_posix()
                result[name] = hashlib.sha256(file_target(root, name).read_bytes()).hexdigest()
    return result


def stage_update(root, cfg, value):
    idle()
    pending = safe_path(root / "data/lan/support-update-pending.json", root)
    if pending.exists():
        prior = json.loads(pending.read_text(encoding="utf-8"))
        result = safe_path(root / "data/lan/support-jobs" / prior["request_id"] / "result.json", root)
        if not result.exists() or json.loads(result.read_text(encoding="utf-8")).get("state") not in {"verified", "failed"}:
            raise ValueError("An application update is already pending; inspect its result")
    rid = value.get("request_id", "")
    if not re.fullmatch(r"[0-9a-f]{32}", rid) or value.get("worker_id") != cfg["worker_id"]:
        raise ValueError("Update identity does not match this paired worker")
    files = value.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= 100:
        raise ValueError("Invalid application update")
    payload, manifest = {}, {"files": {}}
    for name, item in files.items():
        target = file_target(root, name)
        content = base64.b64decode(item["content"], validate=True)
        if len(content) > 1024*1024 or hashlib.sha256(content).hexdigest() != item.get("new"):
            raise ValueError("Update checksum is invalid")
        old = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        if old != item.get("old"):
            raise ValueError("Installed source changed: " + name)
        if target.suffix == ".py": compile(content, name, "exec")
        payload[name] = content
        manifest["files"][name] = {"old": old, "new": item["new"]}
    folder = safe_path(root / "data/lan/support-jobs" / rid, root)
    folder.mkdir(parents=True, exist_ok=False)
    for name, content in payload.items():
        output = safe_path(folder / "payload" / name, folder)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    (folder / "upgrade-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "result.json").write_text(json.dumps({"state": "staged", "request_id": rid, "at": time.time()}), encoding="utf-8")
    pending.write_text(json.dumps({"request_id": rid}), encoding="utf-8")
    return folder


def state():
    from inquiry import qq
    tabs = []
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:9333/json/list", timeout=2) as response:
            for tab in json.load(response):
                parsed = urlsplit(tab.get("url", ""))
                if parsed.hostname in ORIGINS | {"www.ic.net.cn", "ic.net.cn", "member.ic.net.cn"}:
                    tabs.append({"id": tab.get("id"), "origin": parsed.hostname, "path": parsed.path, "title": tab.get("title", "")[:150]})
    except (OSError, ValueError): pass
    foreground = qq.win32gui.GetForegroundWindow()
    _, pid = qq.win32process.GetWindowThreadProcessId(foreground)
    detail = {"exe": Path(qq.exe_of(pid)).name, "pid": pid, "class": qq.win32gui.GetClassName(foreground),
              "title": qq.win32gui.GetWindowText(foreground)[:150], "rect": qq.win32gui.GetWindowRect(foreground),
              "visible": bool(qq.win32gui.IsWindowVisible(foreground)),
              "root_owner": qq.win32gui.GetAncestor(foreground, qq.win32con.GA_ROOTOWNER)}
    return {"windows": windows(), "foreground": foreground, "foreground_info": detail,
            "tabs": tabs, "versions": versions(), "support_capability": 1}


def dispatch(cfg, method, route, body, query):
    from inquiry import isolation
    if not isolation.ENABLED or isolation.MODE != "worker":
        raise ValueError("Not a paired worker")
    value = json.loads(body) if body else {}
    if method == "GET" and route == "state": return state()
    if method == "GET" and route == "versions": return {"versions": versions(), "support_capability": 1}
    if method == "POST" and route == "verify-supplier":
        idle()
        from inquiry import db, qq
        from ui import service
        supplier = db.supplier(value.get("supplier_id"))
        if not supplier or not supplier["sent_at"] or not supplier["chat_name"] or not supplier["qq_uin"]:
            raise ValueError("Only an existing sent conversation may be verified")
        original = db.messages(supplier["id"], "out")
        if not original or not supplier.get("inquiry_message_id"):
            raise ValueError("Original inquiry binding is missing")
        reply = None
        if value.get("outbox_id") is not None:
            with db.connect() as conn:
                reply = conn.execute("SELECT * FROM outbox WHERE id=? AND supplier_id=? AND status='sent'",
                                     (value["outbox_id"], supplier["id"])).fetchone()
            if not reply:
                raise ValueError("Only a sent reply from this supplier may be inspected")
        lock = service.acquire_lock(ROOT / "data/runner.lock")
        if lock is None:
            raise ValueError("Another QQ operation is active")
        try:
            with qq.auto.UIAutomationInitializerInThread():
                win = qq.ready_main_window()
                if qq.current_chat_name(win) != supplier["chat_name"]:
                    raise ValueError("Open the bound conversation before read-only verification")
                qq.verify_supplier(win, supplier["company"], supplier["chat_name"])
                qq.verify_bound_history(win, supplier["chat_name"], original[0],
                                        supplier["inquiry_message_id"], supplier["sent_at"])
                result = {"verified": True, "supplier_id": supplier["id"],
                          "profile_company": qq.profile_company(win),
                          "inquiry_anchor_verified": True, "sending": False}
                if reply:
                    messages = qq.read_messages(win)
                    matches = [m["id"] for m in messages if m.get("self") and m.get("id")
                               and qq._norm(m.get("text", "")) == qq._norm(reply["content"])]
                    result.update(outbox_id=reply["id"], reply_confirmed=bool(matches), reply_message_ids=matches)
                return result
        finally:
            lock.close()
    if method == "POST" and route in {"prepare-browser", "probe-qq"}:
        idle()
        supplier = known_trial_supplier(value.get("supplier_id")) if route == "probe-qq" else None
        from playwright.sync_api import sync_playwright
        from inquiry import browser
        with sync_playwright() as pw:
            _, context = browser.connect(pw)
            if route == "probe-qq":
                from inquiry import db, icnet
                with db.connect() as conn:
                    part = conn.execute("SELECT part_no FROM task WHERE id=?", (supplier["task_id"],)).fetchone()[0]
                found = icnet.find_row(context, part,
                                       {"company": supplier["company"], "qqs": [{"uin": supplier["qq_uin"]}]},
                                       1, source="diagnostic", wait=False)
                if not found: raise ValueError("The recorded supplier is not in the current IC result")
                page,row = found
                probe = icnet.launch_qq(page,row,timeout=12)
                probe.wait_for_timeout(6000)
                return {"opened": True, "supplier_id": supplier["id"], "sending": False}
            page = next((p for p in context.pages if urlsplit(p.url).hostname == "www.ic.net.cn"), None)
            granted = page.evaluate("Promise.all(['local-network-access','local-network','loopback-network'].map(async name=>({name,state:(await navigator.permissions.query({name})).state})))") if page else None
            checks=[]
            for page in context.pages:
                parsed=urlsplit(page.url)
                if parsed.hostname=="www.ic.net.cn" and parsed.path=="/searchPnCode.php":
                    checks.append({"path":parsed.path,"title":page.title(),"text":page.locator("body").inner_text(timeout=2000)[:2000]})
            return {"local_network_access": granted, "origin": "https://www.ic.net.cn","site_checks":checks}
    if method == "POST" and route == "read-supplier":
        idle()
        from inquiry import db,qq,isolation
        from inquiry.runner import Runner,Task,STOP_FILE
        from ui import service
        from lanhost.live_trial import PARTS
        supplier=db.supplier(value.get("supplier_id"))
        with db.connect() as conn:
            task=conn.execute("SELECT * FROM task WHERE id=?",(supplier["task_id"],)).fetchone() if supplier else None
        if not task or task["part_no"] not in PARTS or task["qty"]!=5000 or not supplier["sent_at"] or not supplier["chat_name"]:
            raise ValueError("Only a confirmed sent trial conversation may be read")
        lock=service.acquire_lock(ROOT / "data/runner.lock")
        if lock is None: raise ValueError("Another QQ operation is active")
        try:
            STOP_FILE.unlink(missing_ok=True)
            isolation.MANUAL.clear()
            with qq.auto.UIAutomationInitializerInThread():
                reader=Runner([]);reader.async_analysis=True;reader.win=qq.ready_main_window()
                contacts=[]
                for c in qq.recent_contacts(reader.win):
                    rect=c["control"].BoundingRectangle
                    contacts.append({k:v for k,v in c.items() if k!="control"}|{"rect":[rect.left,rect.top,rect.right,rect.bottom]})
                complete=reader.read_chat(supplier["chat_name"],Task(task["id"],task["part_no"],5000,""),supplier["id"])
                return {"complete":complete,"supplier_id":supplier["id"],"current_chat":qq.current_chat_name(reader.win),
                        "contacts_before":contacts,"read_error":db.supplier(supplier["id"])["read_error"],
                        "incoming":db.incoming_records(supplier["id"])}
        finally:
            isolation.MANUAL.set()
            lock.close()
    if method == "GET" and route in {"tree", "snapshot"}:
        return (tree if route == "tree" else snapshot)(int(query.get("hwnd", ["0"])[0]))
    if method == "POST" and route == "protocol-action":
        with LOCK: return protocol_action(value)
    if method == "GET" and route in {"update-result", "update-log"}:
        rid = query.get("request_id", [""])[0]
        if not re.fullmatch(r"[0-9a-f]{32}", rid): raise ValueError("Invalid update identifier")
        if route == "update-log":
            file = safe_path(ROOT / "data/lan/support-jobs" / rid / "installer.log", ROOT)
            return {"lines": file.read_bytes()[-65536:].decode("utf-8", errors="replace").splitlines()}
        file = safe_path(ROOT / "data/lan/support-jobs" / rid / "result.json", ROOT)
        return json.loads(file.read_text(encoding="utf-8"))
    if method == "POST" and route == "update":
        with LOCK:
            folder = stage_update(ROOT, cfg, value)
            try:
                with (folder / "installer.log").open("w", encoding="utf-8") as log:
                    subprocess.Popen([str(Path(__import__('os').environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'),
                                      "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "lanhost/support_update.ps1"),
                                      "-JobId", folder.name], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError:
                (folder / "result.json").write_text(json.dumps({"state": "failed", "request_id": folder.name}), encoding="utf-8")
                raise
            return {"accepted": True, "request_id": folder.name}
    raise ValueError("Unknown worker diagnostic operation")
