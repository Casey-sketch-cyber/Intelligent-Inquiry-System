"""Apply a staged paired-worker update while the worker is stopped."""
import argparse
import json
import re
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--phase", choices=("prepare", "install", "verify", "failed"), required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.root))
    from inquiry.runtime import ROOT
    from lanhost import config, support, upgrade_trial
    from lanhost.reset_history import safe_path, worker_ui
    root = safe_path(args.root, args.root)
    if root != ROOT or not re.fullmatch(r"[0-9a-f]{32}", args.job_id):
        raise ValueError("Invalid update target")
    cfg = config.worker_config()
    folder = safe_path(root / "data/lan/support-jobs" / args.job_id, root)
    manifest = json.loads((folder / "upgrade-manifest.json").read_text(encoding="utf-8"))
    if not 1 <= len(manifest["files"]) <= 100: raise ValueError("Invalid manifest")
    for name in manifest["files"]: support.file_target(root, name)
    upgrade_trial.TARGETS = set(manifest["files"])
    if args.phase == "prepare":
        upgrade_trial.validate_files(root, folder, manifest)
        port, rpc, call = worker_ui(root, cfg, allow_sending=True)
        call("api/pause", "POST")
        if not call("api/status").get("manual_ready"): raise ValueError("Worker is busy")
        value = {"ui_port": port, "rpc_port": rpc, "process_executables": [sys.executable, sys._base_executable]}
    elif args.phase == "install":
        value = upgrade_trial.install(root, folder, manifest)
        value["state"] = "installed_pending_restart"
    elif args.phase == "verify":
        _, _, call = worker_ui(root, cfg, allow_sending=True)
        if not call("api/status").get("manual_ready"): raise ValueError("Updated worker is not idle")
        for name, item in manifest["files"].items():
            if upgrade_trial.digest(support.file_target(root, name)) != item["new"]: raise ValueError("Installed source mismatch")
        value = {"state": "verified", "request_id": args.job_id, "at": time.time(), "files": list(manifest["files"])}
    else:
        value = {"state": "failed", "request_id": args.job_id, "at": time.time(), "log": str(folder / "installer.log")}
    if args.phase != "prepare": (folder / "result.json").write_text(json.dumps(value), encoding="utf-8")
    print(json.dumps(value))


if __name__ == "__main__": main()
