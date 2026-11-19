"""只在配对的执行环境启用桌面操作；办公电脑保持原保护。"""
import json
import subprocess
import threading
import time
import winreg

from .runtime import DATA_DIR, require_d_path

ENABLED = False
MODE = "local"
ALLOW_SEND = False
DRAINING = threading.Event()
MANUAL = threading.Event()


def machine_identity():
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as key:
        model = winreg.QueryValueEx(key, "SystemProductName")[0]
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
        capture_output=True, text=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("无法核验执行电脑硬件标识")
    return str(model), result.stdout.strip().lower()


def enable_guest(path=None):
    global ENABLED, ALLOW_SEND, MODE
    path = require_d_path(path or DATA_DIR / "vm_guest.json")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    model, identity = machine_identity()
    if model.casefold() != "virtualbox" or identity != str(cfg.get("vm_uuid", "")).lower():
        raise RuntimeError("仅允许在配对的 VirtualBox 内接管桌面；本机桌面未被接管")
    if len(cfg.get("secret", "")) < 48 or cfg.get("bridge") != r"\\VBOXSVR\TianxinBridge":
        raise RuntimeError("虚拟机配对信息不完整")
    ENABLED = True
    MODE = "vm"
    ALLOW_SEND = cfg.get("allow_send") is True
    return cfg


def enable_worker():
    global ENABLED, ALLOW_SEND, MODE
    from lanhost.config import worker_config, sending_enabled
    cfg = worker_config()
    ENABLED, ALLOW_SEND, MODE = True, sending_enabled(cfg), "worker"
    return cfg


def check_sending():
    if ENABLED and (not ALLOW_SEND or DRAINING.is_set()):
        from .qq import DesktopDeferred
        raise DesktopDeferred("运行环境处于只读验收或停止状态，未发送任何消息")


def fit_worker_window(win):
    """Keep QQ controls inside its monitor's usable area on the dedicated PC only."""
    if not ENABLED or MODE != "worker" or MANUAL.is_set() or DRAINING.is_set():
        return
    from . import qq
    hwnd = win.NativeWindowHandle
    _, pid = qq.win32process.GetWindowThreadProcessId(hwnd)
    if not qq.exe_of(pid).lower().endswith("\\qq.exe"):
        return
    monitor = qq.win32api.MonitorFromWindow(hwnd, qq.win32con.MONITOR_DEFAULTTONEAREST)
    wl, wt, wr, wb = qq.win32api.GetMonitorInfo(monitor)["Work"]
    left, top, right, bottom = qq.win32gui.GetWindowRect(hwnd)
    width, height = min(right-left, wr-wl), min(bottom-top, wb-wt)
    if width < 400 or height < 300:
        return
    x, y = min(max(left, wl), wr-width), min(max(top, wt), wb-height)
    if (left, top, right, bottom) != (x, y, x+width, y+height):
        qq.win32gui.SetWindowPos(hwnd, 0, x, y, width, height,
                                qq.win32con.SWP_NOZORDER | qq.win32con.SWP_NOACTIVATE)


def activate_worker_window(win):
    """Use a verified visible QQ title bar on the dedicated PC when Windows rejects programmatic focus."""
    from . import qq
    if MODE!="worker" or not ENABLED or MANUAL.is_set() or DRAINING.is_set(): return False
    hwnd=win.NativeWindowHandle
    try:
        win.SetFocus()
        if qq.win32gui.GetForegroundWindow()==hwnd:return True
    except Exception: pass
    try:
        qq.win32gui.SetWindowPos(hwnd,qq.win32con.HWND_TOP,0,0,0,0,
                                qq.win32con.SWP_NOMOVE|qq.win32con.SWP_NOSIZE|qq.win32con.SWP_NOACTIVATE)
        left,top,right,bottom=qq.win32gui.GetWindowRect(hwnd)
        x,y=left+(right-left)//3,top+16
        if MANUAL.is_set() or DRAINING.is_set() or right-left<400 or bottom-top<300:return False
        if qq._root_at(x,y)!=hwnd:return False
        qq.auto.Click(x,y,waitTime=.2)
        return qq.win32gui.GetForegroundWindow()==hwnd
    except Exception:return False


def ensure_qq_foreground(win):
    """仅在配对执行机激活 QQ，等待异步激活完成；办公端不会进入此分支。"""
    if not ENABLED:
        return False
    from . import qq
    if DRAINING.is_set() or MANUAL.is_set():
        raise qq.DesktopDeferred("运行环境正在停止或等待人工登录")
    hwnd = win.NativeWindowHandle
    _, pid = qq.win32process.GetWindowThreadProcessId(hwnd)
    if not qq.exe_of(pid).lower().endswith("\\qq.exe"):
        raise qq.DesktopDeferred("目标不是 QQ 窗口，已停止桌面操作")
    try:
        if qq.win32gui.IsIconic(hwnd):
            qq.win32gui.ShowWindow(hwnd, qq.win32con.SW_RESTORE)
        fit_worker_window(win)
        if qq.win32gui.GetForegroundWindow() != hwnd:
            qq.win32gui.SetForegroundWindow(hwnd)
            # 不同输入队列的激活是异步的，立即 GetForegroundWindow 会误报未就绪。
            # WM_NULL 等待窗口处理激活；不连接输入队列，也不发送模拟 Alt 等按键。
            qq.win32gui.SendMessageTimeout(hwnd, qq.win32con.WM_NULL, 0, 0,
                                           qq.win32con.SMTO_ABORTIFHUNG, 1000)
    except Exception as exc:
        if not activate_worker_window(win):
            raise qq.DesktopDeferred("执行电脑桌面暂不可操作："+str(exc)[:180]) from exc
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        if DRAINING.is_set() or MANUAL.is_set():
            raise qq.DesktopDeferred("执行电脑已暂停，未继续操作 QQ")
        if qq.win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(.05)
    raise qq.DesktopDeferred("工作机未能将 QQ 切到前台；请在第二台点击 QQ 主窗口，检查弹窗或锁屏后再继续（未发送）")
