"""系统专用 Edge 浏览器：独立配置目录（在 D 盘），开远程调试端口，Playwright 通过 CDP 连接。

浏览器作为独立进程常驻，程序退出后登录状态和窗口都保留，下次直接连上。
"""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from .settings import DATA_DIR
from .runtime import CACHE_DIR, DOWNLOAD_DIR, ROOT, require_d_path

CDP_PORT = 9333
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
PROFILE_DIR = DATA_DIR / "edge_profile"
EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]


def edge_args(profile: Path, cache_name: str) -> list[str]:
    """界面和搜索浏览器均使用 D 盘专用配置、下载、缓存、崩溃目录。"""
    profile = require_d_path(profile)
    cache = require_d_path(CACHE_DIR / cache_name)
    default = profile / "Default"
    default.mkdir(parents=True, exist_ok=True)
    prefs_file = default / "Preferences"
    prefs = json.loads(prefs_file.read_text(encoding="utf-8")) if prefs_file.exists() else {}
    prefs.setdefault("download", {}).update(default_directory=str(DOWNLOAD_DIR), prompt_for_download=False,
                                              directory_upgrade=True)
    prefs.setdefault("savefile", {})["default_directory"] = str(DOWNLOAD_DIR)
    tmp = prefs_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    tmp.replace(prefs_file)
    cache.mkdir(parents=True, exist_ok=True)
    crash = require_d_path(DATA_DIR / "crashes")
    crash.mkdir(parents=True, exist_ok=True)
    return [f"--user-data-dir={profile}", f"--disk-cache-dir={cache}", f"--crash-dumps-dir={crash}",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync"]


def cdp_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2) as r:
            return "Browser" in json.loads(r.read())
    except OSError:
        return False


def launch_edge(url: str = "about:blank") -> None:
    edge = next((p for p in EDGE_CANDIDATES if p.exists()), None)
    if edge is None:
        raise RuntimeError("没有找到 Edge 浏览器")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([
        str(edge),
        f"--remote-debugging-port={CDP_PORT}",
        *edge_args(PROFILE_DIR, "edge-search"),
        url,
    ], cwd=ROOT, env=os.environ.copy())
    for _ in range(30):
        if cdp_alive():
            return
        time.sleep(0.5)
    raise RuntimeError("Edge 启动后连不上调试端口")


def close_stale_tabs() -> None:
    """关掉残留的企点/wpa 跳转页。用调试端口的 HTTP 接口关，不经过 Playwright（挂着确认框的页可能让连接卡住）。"""
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=3) as r:
            targets = json.loads(r.read())
    except OSError:
        return
    for t in targets:
        if t.get("type") == "page" and any(k in t.get("url", "") for k in ("qidian.qq.com", "wpa.qq.com", "wpa1.qq.com")):
            try:
                urllib.request.urlopen(f"{CDP_URL}/json/close/{t['id']}", timeout=3).close()
            except OSError:
                pass


def connect(pw, url: str = "about:blank"):
    """返回 (browser, context)。浏览器没开就先启动。同一时间只能有一个程序连接（两个 Playwright 同时连会互相卡住）。"""
    if not cdp_alive():
        launch_edge(url)
    close_stale_tabs()
    browser = pw.chromium.connect_over_cdp(CDP_URL, timeout=30000)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    session = browser.new_browser_cdp_session()
    try:
        session.send("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(DOWNLOAD_DIR)})
        # The dedicated worker must let the selected supplier site reach its local QQ launcher.
        # This applies only to the automation browser and the exact IC site origin.
        from . import isolation
        if isolation.ENABLED and isolation.MODE == "worker":
            context.grant_permissions(["local-network-access"], origin="https://www.ic.net.cn")
    finally:
        session.detach()
    return browser, context
