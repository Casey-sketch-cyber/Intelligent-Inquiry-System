"""Real PowerShell bootstrap and paired remote code update on D:, no QQ input."""
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry.runtime import ROOT, DATA_DIR
from lanhost import config
from lanhost.transport import Remote
from tools.build_support_bootstrap import build


def main():
    source = Path(json.loads((DATA_DIR / "qa/trial-upgrade-result.json").read_text(encoding="utf-8"))["log"]).parent / "runtime"
    output = DATA_DIR / "qa" / ("support-smoke-"+time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])
    target, package = output / "runtime", output / "package"
    target.mkdir(parents=True)
    for name in ("inquiry", "lanhost", "ui", "vmhost"):
        shutil.copytree(source / name, target / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("main.py", "settings.example.toml", ".env.example"):
        shutil.copy2(source / name, target / name)
    shutil.copy2(target / "settings.example.toml", target / "settings.toml")
    shutil.copy2(target / ".env.example", target / ".env")
    shutil.copytree(source / "python-portable", target / "python-portable")
    env = dict(os.environ, TEMP=str(output / "tmp"), TMP=str(output / "tmp"), PYTHONIOENCODING="utf-8",
               PYTHONPYCACHEPREFIX=str(output / "pycache"), PSModuleAnalysisCachePath=str(output / "powershell/ModuleAnalysisCache"))
    (output / "tmp").mkdir()
    subprocess.run([str(target / "python-portable/3.12.10/python.exe"), "-B", "-m", "venv", "--without-pip", str(target / ".venv")], cwd=target, env=env, check=True, capture_output=True, timeout=45)
    shutil.copytree(source / ".venv/Lib/site-packages", target / ".venv/Lib/site-packages", dirs_exist_ok=True)
    py = target / ".venv/Scripts/python.exe"
    fixture = target / "fixture.py"
    fixture.write_text('''from lanhost import config
from inquiry import db
import uuid
config.prepare_worker({"office_device":"0"*64,"controller_id":uuid.uuid4().hex})
for part in ("LPC2387FBD100K","LPC1788FBD208K","LPC2366FBD100K"): db.create_task(part,5000)
''', encoding="utf-8")
    subprocess.run([str(py), "-B", str(fixture)], cwd=target, env=env, check=True, capture_output=True, timeout=25)
    cfg = json.loads((target / "data/worker.json").read_text(encoding="utf-8"))
    def loopback(content):
        needle = "from lanhost.transport import Journal, make_server"
        return content.replace(needle, needle+'\nfrom functools import partial\nmake_server=partial(make_server,host="127.0.0.1")')
    new_worker = loopback((ROOT / "lanhost/worker.py").read_text(encoding="utf-8"))
    build(package, cfg, target, payload_overrides={"lanhost/worker.py": new_worker.encode()})
    preserved = {name: (target / name).read_bytes() for name in ("data/worker.json", "settings.toml", ".env")}
    ps = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    with (output / "initial.log").open("w", encoding="utf-8") as log:
        initial = subprocess.Popen([str(py), "-B", "-m", "lanhost.worker", "--no-browser"], cwd=target, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            if (target / "data/lan/worker-ui.json").exists(): break
            time.sleep(.5)
        with (output / "bootstrap.log").open("w", encoding="utf-8") as log:
            run = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(package / "support_bootstrap.ps1"), "-WorkerRoot", str(target)], cwd=target, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=100)
        if run.returncode: raise RuntimeError((output / "bootstrap.log").read_text(encoding="utf-8", errors="replace")[-7000:])
        def remote():
            port = json.loads((target / "data/lan/worker-ui.json").read_text(encoding="utf-8"))["port"]
            return Remote(config.descriptor(cfg, port, ["127.0.0.1"]))
        code, _, raw = remote().call("GET", "/api/worker/status", timeout=8)
        assert code == 200 and json.loads(raw)["support_capability"] == 1
        code, _, raw = remote().call("GET", "/api/worker/support/versions", timeout=8)
        actual = json.loads(raw)
        assert code == 200 and actual["versions"]["lanhost/support.py"] == hashlib.sha256((ROOT / "lanhost/support.py").read_bytes()).hexdigest()
        name = "inquiry/support_smoke_fixture.py"
        content = b'"""Reviewed application update fixture; never imported by QQ."""\nVALUE=42\n'
        rid = uuid.uuid4().hex
        value = {"request_id": rid, "worker_id": cfg["worker_id"], "files": {name: {
            "old": None, "new": hashlib.sha256(content).hexdigest(), "content": base64.b64encode(content).decode()}}}
        code, _, raw = remote().call("POST", "/api/worker/support/update", json.dumps(value).encode(), "application/json", timeout=12)
        assert code == 200 and json.loads(raw)["accepted"]
        end = time.monotonic()+80
        final = None
        while time.monotonic() < end:
            time.sleep(1)
            file = target / "data/lan/support-jobs" / rid / "result.json"
            if file.exists():
                final = json.loads(file.read_text(encoding="utf-8"))
                if final["state"] in {"verified", "failed"}: break
        if not final or final["state"] != "verified":
            raise RuntimeError((target / "data/lan/support-jobs" / rid / "installer.log").read_text(encoding="utf-8", errors="replace")[-7000:])
        assert (target / name).read_bytes() == content
        code, _, raw = remote().call("GET", "/api/worker/support/update-result?request_id="+rid, timeout=8)
        assert code == 200 and json.loads(raw)["state"] == "verified"
        assert all((target / name).read_bytes() == previous for name, previous in preserved.items())
        with sqlite3.connect(target / "data/inquiry.db") as conn:
            assert conn.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM message WHERE direction='out'").fetchone()[0] == 0
        result = {"passed": True, "powershell_51_bootstrap": True, "paired_remote_update_and_restart": True,
                  "tasks_preserved": 3, "config_preserved": True, "actual_qq_actions": 0, "physical_worker_touched": False,
                  "folder": str(output)}
        (DATA_DIR / "qa/support-smoke-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        stop = output / "stop.ps1"
        stop.write_text('''param([string]$Target)
$env:PSModulePath=(Join-Path $PSHOME 'Modules')+[IO.Path]::PathSeparator+(Join-Path $env:ProgramFiles 'WindowsPowerShell\\Modules')
Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -and $_.ExecutablePath.StartsWith($Target+'\\',[StringComparison]::OrdinalIgnoreCase) -and $_.CommandLine -match '(?i)\\s-m\\s+lanhost\\.worker(?:\\s|$)'} | ForEach-Object {Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue}
''', encoding="utf-8-sig")
        subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(stop), str(target)], cwd=target, env=env, capture_output=True, timeout=20)


if __name__ == "__main__": main()
