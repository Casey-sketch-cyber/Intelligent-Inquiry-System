"""有界的本机被动采样：只看前台句柄/进程名/鼠标位置，绝不激活窗口或输入。"""
import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry.runtime import DATA_DIR
from lanhost import config
from lanhost.transport import Remote
import win32api
import win32gui
import win32process


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{32}", args.request_id):
        raise ValueError("Invalid trial identifier")
    remote = Remote(config.validate_connection(config.read_json(config.CONNECTION)))
    folder = DATA_DIR / "qa/live-three-suppliers-20261022" / ("office-monitor-" + args.request_id)
    folder.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    status = {"active": False, "finished": False, "last_checked_at": None}

    def poll():
        while not stop.is_set():
            try:
                code, _, raw = remote.call("GET", "/api/worker/status", timeout=4)
                s = json.loads(raw)
                if code == 200 and s.get("trial_request_id") == args.request_id:
                    status["active"] = bool(s.get("trial_running"))
                    status["finished"] = not status["active"]
                status["last_checked_at"] = time.time()
            except Exception:
                pass
            stop.wait(2)
    threading.Thread(target=poll, daemon=True).start()
    deadline = time.monotonic() + 1800
    began = None
    count, qq_samples, changes = 0, 0, 0
    last, last_pid, exe = None, None, ""
    with (folder / "samples.jsonl").open("w", encoding="utf-8") as stream:
        while time.monotonic() < deadline:
            if status["finished"] and not status["active"]:
                break
            if not status["active"]:
                stop.wait(.5)
                continue
            if began is None:
                began = time.time()
                deadline = time.monotonic() + 1800
            hwnd = win32gui.GetForegroundWindow()
            _, pid = win32process.GetWindowThreadProcessId(hwnd) if hwnd else (0, 0)
            if pid != last_pid:
                exe = ""
                if pid:
                    try:
                        handle = win32api.OpenProcess(0x1000, False, pid)
                        try:
                            exe = Path(win32process.GetModuleFileNameEx(handle, 0)).name.lower()
                        finally:
                            handle.Close()
                    except Exception:
                        pass
                last_pid = pid
            changes += int(last is not None and hwnd != last)
            last = hwnd
            count += 1
            qq_samples += int(exe == "qq.exe")
            stream.write(json.dumps({"at": time.time(), "foreground": hwnd, "pid": pid, "exe": exe,
                                     "cursor": win32api.GetCursorPos(), "worker_checked_at": status["last_checked_at"]}) + "\n")
            if count % 20 == 0:
                stream.flush()
            stop.wait(.2)
    stop.set()
    result = {"request_id": args.request_id, "started_sampling_at": began, "samples": count,
              "qq_foreground_samples": qq_samples, "foreground_changes": changes,
              "worker_finished_observed": status["finished"], "input_or_activation_calls": 0,
              "limitation": "采样只能观察前台/鼠标；用户自己的切换与移动不能自动归因。未覆盖的时间不能判定为通过。"}
    (folder / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
