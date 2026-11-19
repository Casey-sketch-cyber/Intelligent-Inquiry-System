"""One local bootstrap for the paired diagnostic and application-update channel."""
import argparse
import hmac
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

TARGETS = {"lanhost/support.py", "lanhost/support_update.py", "lanhost/support_update.ps1",
           "lanhost/worker.py", "inquiry/qq.py", "inquiry/runner.py", "lanhost/upgrade_trial.py"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--phase", choices=("prepare", "install", "verify"), required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.root))
    os.chdir(args.root)
    from inquiry.runtime import ROOT
    from lanhost import config, live_trial
    from lanhost.reset_history import safe_path, worker_ui
    root, package = safe_path(args.root, args.root), safe_path(args.package, args.package)
    spec = importlib.util.spec_from_file_location("lanhost.bootstrap_updater", package / "upgrade_trial.py")
    upgrade_trial = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upgrade_trial)
    if root != ROOT: raise ValueError("Worker path mismatch")
    cfg = config.worker_config()
    signed = json.loads((package / "authorization.json").read_text(encoding="utf-8"))
    body = {k: v for k, v in signed.items() if k != "signature"}
    now = time.time()
    if (body.get("kind") != "tianxin-support-bootstrap-v1" or body.get("worker_id") != cfg["worker_id"]
            or not body.get("created_at", now+1000)-120 <= now < body.get("expires_at", 0)
            or not 0 < body["expires_at"]-body["created_at"] <= 86400
            or not hmac.compare_digest(signed.get("signature", ""), live_trial.sign(body, cfg["secret"]))):
        raise ValueError("Bootstrap does not match this paired worker or has expired")
    manifest = json.loads((package / "upgrade-manifest.json").read_text(encoding="utf-8"))
    if set(manifest.get("files", {})) != TARGETS or body.get("manifest_hash") != upgrade_trial.digest(package / "upgrade-manifest.json"):
        raise ValueError("Bootstrap source manifest mismatch")
    upgrade_trial.TARGETS = TARGETS
    if args.phase == "install":
        value = upgrade_trial.install(root, package, manifest)
    else:
        if args.phase == "prepare": upgrade_trial.validate_files(root, package, manifest)
        port, rpc, call = worker_ui(root, cfg)
        if args.phase == "prepare": call("api/pause", "POST")
        status = call("api/status")
        if not status.get("manual_ready"): raise ValueError("Worker is not paused and idle")
        if args.phase == "verify" and status.get("support_capability") != 1: raise ValueError("Diagnostic channel was not installed")
        value = {"ui_port": port, "rpc_port": rpc,
                 "process_executables": [sys.executable, sys._base_executable], "support_capability": status.get("support_capability")}
    print(json.dumps(value))


if __name__ == "__main__": main()
