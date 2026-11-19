"""旧应用真实升级、远程有界启动、九次替身发送；全部 D 盘，绝不操作 QQ。"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry.runtime import ROOT, DATA_DIR
from lanhost import live_trial, upgrade_trial, config
from lanhost.reset_history import task_fingerprint
from lanhost.transport import Remote


def main():
    original = Path(json.loads((DATA_DIR / "qa/portable-install-result.json").read_text(encoding="utf-8"))["runtime_root"])
    output = DATA_DIR / "qa" / ("trial-upgrade-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    target, package = output / "runtime", output / "package"
    target.mkdir(parents=True); package.mkdir()
    env = dict(os.environ, TEMP=str(output / "tmp"), TMP=str(output / "tmp"), PYTHONIOENCODING="utf-8",
               PYTHONPYCACHEPREFIX=str(output / "pycache"), PSModuleAnalysisCachePath=str(output / "powershell/ModuleAnalysisCache"))
    (output / "tmp").mkdir()
    with zipfile.ZipFile(DATA_DIR / "lan/packages/worker-20261022-115143-26e648/application.zip") as z:
        z.extractall(target)
    shutil.copytree(original / "python-portable", target / "python-portable")
    subprocess.run([str(target / "python-portable/3.12.10/python.exe"), "-B", "-m", "venv", "--without-pip", str(target / ".venv")],
                   cwd=target, env=env, check=True, capture_output=True, timeout=45)
    shutil.copytree(original / ".venv/Lib/site-packages", target / ".venv/Lib/site-packages", dirs_exist_ok=True)
    py = target / ".venv/Scripts/python.exe"
    for name in ("settings",):
        shutil.copy2(target / "settings.example.toml", target / "settings.toml")
    shutil.copy2(target / ".env.example", target / ".env")

    def loopback(content):
        needle = "from lanhost.transport import Journal, make_server"
        return content.replace(needle, needle + '\nfrom functools import partial\nmake_server=partial(make_server,host="127.0.0.1")')
    worker = target / "lanhost/worker.py"
    worker.write_text(loopback(worker.read_text(encoding="utf-8")), encoding="utf-8")
    fixture = target / "fixture.py"
    fixture.write_text('''from lanhost import config
from inquiry import db
import uuid
config.prepare_worker({"office_device":"0"*64,"controller_id":uuid.uuid4().hex})
for part in ("LPC2387FBD100K","LPC1788FBD208K","LPC2366FBD100K"):
    tid=db.create_task(part,5000)
    sid=db.add_supplier(tid,{"company":"previous-unopened","qqs":[{"uin":"old"}]})
    db.update_supplier(sid,status="waiting_desktop",error="old foreground timing")
''', encoding="utf-8")
    subprocess.run([str(py), "-B", str(fixture)], cwd=target, env=env, check=True, capture_output=True, timeout=20)
    cfg = json.loads((target / "data/worker.json").read_text(encoding="utf-8"))
    with closing(sqlite3.connect(target / "data/inquiry.db")) as c:
        c.row_factory = sqlite3.Row
        fingerprint = task_fingerprint(list(c.execute("SELECT * FROM task")))
    body = {"kind": "tianxin-live-trial-v2", "worker_id": cfg["worker_id"], "request_id": uuid.uuid4().hex,
            "created_at": time.time(), "expires_at": time.time() + 3600,
            "parts": [{"part": p, "qty": 5000} for p in live_trial.PARTS], "template": live_trial.TEMPLATE,
            "max_per_model": 3, "max_total": 9, "task_fingerprint": fingerprint}
    signed = body | {"signature": live_trial.sign(body, cfg["secret"])}
    (package / "live-permit.json").write_text(json.dumps(signed), encoding="utf-8")
    for name in ("upgrade_trial.py", "upgrade_trial.ps1", "reset_history.py", "live_trial.py"):
        shutil.copy2(ROOT / "lanhost" / name, package / name)
    manifest = {"files": {}}
    for name in upgrade_trial.TARGETS:
        dest = package / "payload" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = (ROOT / name).read_text(encoding="utf-8")
        if name == "lanhost/worker.py":
            content = loopback(content)
        if name == "lanhost/live_trial.py":
            # 仅测试副本：发件行为替身，仍走原始签名/名额/恢复/报告/远程 API。
            hook = '''if __name__ == "__main__":
    from unittest.mock import Mock, MagicMock
    from inquiry import qq, browser, db, dispatch, isolation
    from inquiry.runner import Runner
    import playwright.sync_api
    playwright.sync_api.sync_playwright=MagicMock()
    browser.connect=Mock(return_value=(None,object()))
    qq.ready_main_window=Mock(return_value=object())
    desktop_snapshot=lambda win: {}
    isolation.ensure_qq_foreground=lambda win: True
    def send(win,text,*,expect_chat=None,company=None):
        assert isolation.ALLOW_SEND and "5000pcs" in text and expect_chat and company
    qq.send_text=send
    def candidates(self,context,task):
        task.candidates.extend({"company":"new-"+str(i),"qqs":[{"uin":str(i)}]} for i in range(5))
    def inquire(self,task,row):
        sid=db.add_supplier(task.id,row)
        dispatch.record_attempt(sid,"fixture")
        qq.send_text(object(),task.text,expect_chat=task.part_no,company=row["company"])
        db.update_supplier(sid,status="sent",sent_at=time.time(),chat_name=task.part_no)
        db.add_message(sid,"out",task.text)
        return True
    Runner.load_candidates=candidates
    Runner.inquire=inquire
    main()'''
            assert 'if __name__ == "__main__":\n    main()' in content
            content = content.replace('if __name__ == "__main__":\n    main()', hook)
        dest.write_text(content, encoding="utf-8")
        manifest["files"][name] = {"old": upgrade_trial.digest(target / name) if (target / name).exists() else None,
                                    "new": upgrade_trial.digest(dest)}
    (package / "upgrade-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    before = {name: (target / name).read_bytes() for name in ("data/worker.json", ".env", "settings.toml")}
    ps = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    with (output / "initial-worker.log").open("w", encoding="utf-8") as stream:
        subprocess.Popen([str(py), "-B", "-m", "lanhost.worker", "--no-browser"], cwd=target, env=env, stdout=stream, stderr=subprocess.STDOUT)
    try:
        for _ in range(30):
            if (target / "data/lan/worker-ui.json").exists(): break
            time.sleep(.5)
        log = output / "upgrade.log"
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(package / "upgrade_trial.ps1"),
                                     "-WorkerRoot", str(target)], cwd=target, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=100)
        if result.returncode:
            raise RuntimeError(log.read_text(encoding="utf-8", errors="replace")[-7000:])
        port = json.loads((target / "data/lan/worker-ui.json").read_text(encoding="utf-8"))["port"]
        remote = Remote(config.descriptor(cfg, port, ["127.0.0.1"]))
        route = "/api/worker/trial-result?request_id=" + body["request_id"]
        for _ in range(40):
            code, _, raw = remote.call("GET", route, timeout=5)
            actual = json.loads(raw)
            if code == 200 and actual.get("state") != "running": break
            time.sleep(.5)
        assert actual["state"] == "finished", actual
        assert actual["send_calls_claimed"] == 9
        code, _, raw = remote.call("POST", "/api/worker/live-trial", json.dumps(signed).encode(), "application/json")
        assert code == 409
        code, _, raw = remote.call("GET", "/api/worker/trial-audit?request_id=" + body["request_id"])
        assert code == 200 and len([r for r in json.loads(raw) if r["event"] == "inquiry_result"]) == 9
        code, _, raw = remote.call("GET", "/api/worker/trial-log?request_id=" + body["request_id"])
        assert code == 200 and json.loads(raw)["lines"]
        assert all((target / name).read_bytes() == data for name, data in before.items())
        with closing(sqlite3.connect(target / "data/inquiry.db")) as c:
            assert c.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 3
            assert c.execute("SELECT COUNT(*) FROM supplier WHERE sent_at IS NOT NULL").fetchone()[0] == 9
            assert c.execute("SELECT COUNT(*) FROM message WHERE direction='out'").fetchone()[0] == 9
        facts = {"passed": True, "windows_powershell_51": True, "original_application_upgraded": True,
                 "existing_tasks_preserved": 3, "simulated_send_calls": 9, "duplicate_request_rejected": True,
                 "audit_api_verified": True, "pairing_and_settings_preserved": True, "qq_operations": 0,
                 "physical_worker_touched": False, "log": str(log)}
        (DATA_DIR / "qa/trial-upgrade-result.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")
        print(json.dumps(facts, indent=2))
    finally:
        stop = output / "stop.ps1"
        stop.write_text('''param([string]$Target)
$env:PSModulePath=(Join-Path $PSHOME 'Modules')+[IO.Path]::PathSeparator+(Join-Path $env:ProgramFiles 'WindowsPowerShell\\Modules')
Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -and $_.ExecutablePath.StartsWith($Target+'\\',[StringComparison]::OrdinalIgnoreCase) -and $_.CommandLine -match '(?i)\\s-m\\s+lanhost\\.(worker|live_trial)(?:\\s|$)'} | ForEach-Object {Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue}
''', encoding="utf-8-sig")
        subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(stop), str(target)], cwd=target, env=env, capture_output=True, timeout=20)


if __name__ == "__main__":
    main()
