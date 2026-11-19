"""真实 Windows PowerShell 5.1 停服/备份/重启回归，使用 D 盘隔离的便携运行环境。

只复用已有验收材料，不连接实体工作机，不调用 QQ/模型，不发送任何消息。
"""
import json
import argparse
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
from lanhost.reset_history import task_fingerprint


def main(live=False):
    portable = json.loads((DATA_DIR / "qa/portable-install-result.json").read_text(encoding="utf-8"))
    original = Path(portable["runtime_root"])
    output = DATA_DIR / "qa" / ("history-reset-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    target = output / "runtime"
    target.mkdir(parents=True)
    env = os.environ.copy()
    env.update(TEMP=str(output / "tmp"), TMP=str(output / "tmp"),
               PYTHONPYCACHEPREFIX=str(output / "pycache"), PYTHONIOENCODING="utf-8",
               PSModuleAnalysisCachePath=str(output / "powershell/ModuleAnalysisCache"))
    (output / "tmp").mkdir()
    with zipfile.ZipFile(DATA_DIR / "lan/packages/worker-20261022-115143-26e648/application.zip") as archive:
        archive.extractall(target)
    shutil.copytree(original / "python-portable", target / "python-portable")
    base = target / "python-portable/3.12.10/python.exe"
    subprocess.run([str(base), "-B", "-m", "venv", "--without-pip", str(target / ".venv")],
                   cwd=target, env=env, check=True, timeout=45, capture_output=True)
    shutil.copytree(original / ".venv/Lib/site-packages", target / ".venv/Lib/site-packages", dirs_exist_ok=True)
    py = target / ".venv/Scripts/python.exe"
    shutil.copy2(target / "settings.example.toml", target / "settings.toml")
    shutil.copy2(target / ".env.example", target / ".env")
    # 限制测试 TLS 仅回环；生产工作机源码不改。服务始终暂停，采集线程不会接触 QQ。
    source = target / "lanhost/worker.py"
    content = source.read_text(encoding="utf-8")
    needle = 'from lanhost.transport import Journal, make_server'
    assert needle in content
    content = content.replace(needle, needle + '\nfrom functools import partial\nmake_server = partial(make_server, host="127.0.0.1")')
    source.write_text(content, encoding="utf-8")
    fixture = target / "fixture_reset.py"
    fixture.write_text('''from inquiry import db
from lanhost import config
from pathlib import Path
import uuid
config.prepare_worker({"office_device":"0"*64,"controller_id":uuid.uuid4().hex})
tid=db.create_task("OLD-MODEL",5000)
sid=db.add_supplier(tid,{"company":"fixture","qqs":[]})
db.add_message(sid,"out","old message fixture, never sent")
with db.connect() as c:
    c.execute("INSERT INTO send_attempt(created_at,supplier_id,kind) VALUES(1,?,'fixture')",(sid,))
reports=Path("data/reports"); reports.mkdir()
(reports/"old.xlsx").write_bytes(b"report")
''', encoding="utf-8")
    subprocess.run([str(py), "-B", str(fixture)], cwd=target, env=env, check=True, capture_output=True, timeout=20)
    with closing(sqlite3.connect(target / "data/inquiry.db")) as conn:
        conn.row_factory = sqlite3.Row
        fingerprint = task_fingerprint(list(conn.execute("SELECT * FROM task")))
    request = output / "reset-request.json"
    request.write_text(json.dumps({"kind": "tianxin-history-reset", "task_fingerprint": fingerprint,
                                  "worker_id": json.loads((target / "data/worker.json").read_text(encoding="utf-8"))["worker_id"]}), encoding="utf-8")
    tool = output / "tool"
    if live:
        from lanhost import live_trial
        tool.mkdir()
        for name in ("live_trial.py", "reset_history.py", "reset_history.ps1", "apply_live_trial.ps1"):
            shutil.copy2(ROOT / "lanhost" / name, tool / name)
        shutil.copy2(request, tool / "reset-request.json")
        cfg = json.loads((target / "data/worker.json").read_text(encoding="utf-8"))
        body = {"kind": "tianxin-live-trial-v1", "worker_id": cfg["worker_id"], "request_id": uuid.uuid4().hex,
                "created_at": time.time(), "expires_at": time.time() + 3600,
                "parts": [{"part": p, "qty": 5000} for p in live_trial.PARTS], "template": live_trial.TEMPLATE,
                "max_per_model": 1, "max_total": 3, "task_fingerprint": fingerprint}
        (tool / "live-permit.json").write_text(json.dumps(body | {"signature": live_trial.sign(body, cfg["secret"])}), encoding="utf-8")
        # 只在测试副本注入替身；整个安装/清理/授权校验/次数守卫/建库/报告流程仍真实执行。
        trial_source = (tool / "live_trial.py").read_text(encoding="utf-8")
        main_entry = 'if __name__ == "__main__":\n    main()'
        assert main_entry in trial_source
        hooks = '''if __name__ == "__main__":
    if "--check-only" not in sys.argv:
        sys.path.insert(0, os.getcwd())
        from unittest.mock import Mock, MagicMock
        from inquiry import qq, browser, db, isolation
        from inquiry.runner import Runner
        import playwright.sync_api
        playwright.sync_api.sync_playwright = MagicMock()
        browser.connect = Mock(return_value=(None, object()))
        qq.ready_main_window = Mock(return_value=object())
        def simulated_send(win, text, *, expect_chat=None, company=None):
            assert isolation.ALLOW_SEND and "5000pcs" in text and expect_chat and company
        qq.send_text = simulated_send
        qq.send_image = Mock(side_effect=AssertionError("No images in live trial smoke"))
        def candidates(self, context, task):
            task.candidates.extend([{"company":task.part_no+"-A","qqs":[{"uin":"123"}]},
                                    {"company":task.part_no+"-B","qqs":[{"uin":"456"}]}])
        def inquire(self, task, row):
            sid = db.add_supplier(task.id, row)
            qq.send_text(object(), task.text, expect_chat=task.part_no, company=row["company"])
            db.update_supplier(sid,status="sent",chat_name=task.part_no,sent_at=time.time(),inquiry_message_id="fixture-anchor")
            db.add_message(sid,"out",task.text)
            task.sent += 1
            return True
        Runner.load_candidates = candidates
        Runner.inquire = inquire
    main()'''
        (tool / "live_trial.py").write_text(trial_source.replace(main_entry, hooks), encoding="utf-8")
    private_before = {p: (target / p).read_bytes() for p in (".env", "settings.toml", "data/worker.json")}
    windows_ps = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    log = output / "reset.log"
    worker_log = (output / "worker.log").open("w", encoding="utf-8")
    process = subprocess.Popen([str(py), "-B", "-m", "lanhost.worker", "--no-browser"],
                               cwd=target, env=env, stdout=worker_log, stderr=subprocess.STDOUT)
    try:
        for _ in range(30):
            if (target / "data/lan/worker-ui.json").exists():
                break
            if process.poll() is not None:
                raise RuntimeError("Isolated fixture worker did not start")
            time.sleep(.5)
        with log.open("w", encoding="utf-8") as stream:
            script = tool / "apply_live_trial.ps1" if live else ROOT / "lanhost/reset_history.ps1"
            arguments = ["-WorkerRoot", str(target)] + ([] if live else ["-RequestPath", str(request)])
            result = subprocess.run([windows_ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *arguments],
                                    cwd=target, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=100)
        text = log.read_text(encoding="utf-8", errors="replace")
        if result.returncode or "Verified: tasks=0" not in text:
            raise RuntimeError("PowerShell reset failed: " + text[-5000:])
        assert all((target / p).read_bytes() == b for p, b in private_before.items())
        with closing(sqlite3.connect(target / "data/inquiry.db")) as conn:
            assert conn.execute("SELECT COUNT(*) FROM task").fetchone()[0] == (3 if live else 0)
            assert conn.execute("SELECT COUNT(*) FROM send_attempt").fetchone()[0] == 1
            if live:
                assert conn.execute("SELECT COUNT(*) FROM message WHERE direction='out'").fetchone()[0] == 3
                assert conn.execute("SELECT COUNT(*) FROM supplier").fetchone()[0] == 3
        backup = next((target / "data/backups").iterdir())
        with closing(sqlite3.connect(backup / "inquiry.db")) as conn:
            assert conn.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 1
        facts = dict(passed=True, windows_powershell_51=True, portable_python=True,
                     original_delivered_application=True, stopped_and_restarted=True,
                     backup_verified=True, tasks_after=3 if live else 0, pacing_preserved=True,
                     pairing_settings_preserved=True, physical_worker_touched=False,
                     qq_operations=0, real_send_calls=0, log=str(log))
        if live:
            attempts = json.loads(next((target / "data/lan/live-trials").glob("*/send-attempts.json")).read_text(encoding="utf-8"))
            assert len(attempts) == 3
            assert all(a["state"] == "send_function_confirmed" for a in attempts)
            assert "Trial finished." in text
            facts.update(simulated_text_sends=3, regular_worker_still_readonly=True, cards_sent=0)
        result_name = "live-trial-result.json" if live else "history-reset-result.json"
        (DATA_DIR / "qa" / result_name).write_text(json.dumps(facts, indent=2), encoding="utf-8")
        print(json.dumps(facts, indent=2))
    finally:
        # 只终止本测试唯一 D 盘目录内的 worker；不枚举 QQ，不触碰实体第二台。
        cleanup = output / "stop-fixture.ps1"
        cleanup.write_text('''param([string]$Target)
$env:PSModulePath=(Join-Path $PSHOME 'Modules')+[IO.Path]::PathSeparator+(Join-Path $env:ProgramFiles 'WindowsPowerShell\\Modules')
Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Target+'\\',[StringComparison]::OrdinalIgnoreCase) -and
    $_.CommandLine -match '(?i)\\s-m\\s+lanhost\\.worker(?:\\s|$)'
} | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
''', encoding="utf-8-sig")
        subprocess.run([windows_ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(cleanup), str(target)], cwd=target, env=env,
                       capture_output=True, timeout=20)
        worker_log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-trial", action="store_true")
    main(parser.parse_args().live_trial)
