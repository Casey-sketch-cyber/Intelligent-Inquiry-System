"""操作桌面版 QQ（QQNT，Electron）：通过 Windows UI Automation 读取界面、发送消息。

QQNT 的网页内容默认不对外暴露控件树；第一次有 UIA 客户端访问时会自动打开，所以先 warm_up() 再读。
控件靠网页里的 class 名定位（UIA 的 ClassName 就是 HTML class）。
"""
import random
import time

import uiautomation as auto
import win32api
import win32con
import win32gui
import win32process

from . import incidents
from .history import HistoryReadError, read_history
from .identity import SendUncertainError, SupplierIdentityError, identity_error
from .runtime import DATA_DIR

auto.SetGlobalSearchTimeout(2)
auto.Logger.SetLogFile(str(DATA_DIR / "uiautomation.log"))
FOREGROUND_ASSIST = False


class DesktopDeferred(HistoryReadError):
    """后台能力不足或用户切走；保留进度，绝不夺取焦点。"""


class EditorUnavailable(DesktopDeferred):
    """编辑器控件暂时不可读，等待 QQ 就绪后可重试。"""


def require_assisted_foreground(win):
    from . import isolation
    if isolation.ENABLED:
        if not isolation.ensure_qq_foreground(win):
            raise DesktopDeferred("执行电脑中的 QQ 暂未就绪")
        return
    if not FOREGROUND_ASSIST or win32gui.IsIconic(win.NativeWindowHandle) or not activate(win):
        raise DesktopDeferred("此步骤需要 QQ 前台协助；已暂停，未移动鼠标。请开启前台协助并手动打开 QQ")


def exe_of(pid: int) -> str:
    try:
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        return win32process.GetModuleFileNameEx(h, 0)
    except Exception:
        return ""


def qq_windows() -> list:
    return [w for w in auto.GetRootControl().GetChildren() if exe_of(w.ProcessId).lower().endswith("\\qq.exe")]


def _main_window_handles():
    """只枚举可见且尺寸符合 QQ 主窗口的候选句柄。"""
    found = []

    def cb(hwnd, _):
        if (win32gui.GetClassName(hwnd) == "Chrome_WidgetWin_1" and win32gui.GetWindowText(hwnd) == "QQ"
                and win32gui.IsWindowVisible(hwnd)):
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if exe_of(pid).lower().endswith("\\qq.exe"):
                rect = win32gui.GetWindowPlacement(hwnd)[4] if win32gui.IsIconic(hwnd) else win32gui.GetWindowRect(hwnd)
                l, t, r, b = rect
                if r - l >= 400 and b - t >= 300:
                    found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return found


def passive_main_window():
    """读取未最小化的 QQ 主窗口，保留没有暴露会话列表的候选供自检。"""
    fallback = None
    for hwnd in _main_window_handles():
        if win32gui.IsIconic(hwnd):
            continue
        try:
            win = auto.ControlFromHandle(hwnd)
            if fallback is None:
                fallback = win
            if find(win, "recent-contact-list") is not None:
                return win
            if warm_up(win, 1) and find(win, "recent-contact-list") is not None:
                return win
        except Exception:
            continue
    return fallback


def main_window():
    """读取具备会话列表的 QQ 主窗口。独立环境可恢复窗口。"""
    for hwnd in _main_window_handles():
        if win32gui.IsIconic(hwnd):
            from . import isolation
            if not isolation.ENABLED or isolation.MANUAL.is_set() or isolation.DRAINING.is_set():
                continue
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win = auto.ControlFromHandle(hwnd)
        if warm_up(win, 3) and find(win, "recent-contact-list") is not None:
            return win
    return None


def find(parent, cls_part: str, control_type=None, depth: int = 40):
    """在 parent 下找 class 名包含 cls_part 的第一个控件，找不到返回 None。"""
    def cmp(c, d):
        return cls_part in c.ClassName and (control_type is None or c.ControlType == control_type)
    ctrl = parent.Control(searchDepth=depth, Compare=cmp)
    return ctrl if ctrl.Exists(0, 0) else None


def find_all(parent, cls_part: str, depth: int = 40) -> list:
    return [c for c, _ in auto.WalkControl(parent, maxDepth=depth) if cls_part in c.ClassName]


def warm_up(win, timeout: float = 6.0) -> bool:
    """触发 Chromium 打开无障碍树，等到网页内容可读为止。"""
    end = time.time() + timeout
    while time.time() < end:
        root = win.DocumentControl(AutomationId="RootWebArea", searchDepth=12)
        if root.Exists(0.5, 0.1) and root.GetFirstChildControl() is not None:
            return True
        time.sleep(0.3)
    return False


def ready_main_window():
    win = main_window()
    if win is None:
        raise DesktopDeferred("请登录 QQ 并打开主窗口（可被其他窗口遮挡，请勿最小化或只留托盘）")
    return win


def activate(win) -> bool:
    """仅检测；即使协助模式也不恢复窗口、不抢前台。"""
    return win32gui.GetForegroundWindow() == win.NativeWindowHandle


def ensure_foreground(win):
    """按键前确认 QQ 在前台，否则按键会打到别的程序里。"""
    require_assisted_foreground(win)


# ---------- 当前会话 ----------

def current_chat_name(win) -> str | None:
    btn = find(win, "chat-header__contact-name")
    return btn.Name.strip() if btn else None


def wait_chat_change(win, before: str | None, timeout: float = 20, cancelled=lambda: False, launch_error=lambda:None) -> str | None:
    """等当前会话切到一个新会话，并等名字稳定下来再返回；超时返回 None。

    新会话刚打开时标题先显示对方 QQ 号，过一会儿才变成昵称，所以要等名字连续 1.5 秒不变、
    且不是纯数字（一直是号码的，等 6 秒后也接受）。
    """
    end = time.time() + timeout
    last, since = None, 0.0
    while time.time() < end:
        if cancelled():
            raise DesktopDeferred("已暂停，停止等待 QQ 会话；未发送")
        error = launch_error()
        if error: raise RuntimeError(error)
        name = current_chat_name(win)
        if name and name != before:
            if name != last:
                last, since = name, time.time()
            elif time.time() - since >= (6 if name.isdigit() else 1.5):
                return name
        time.sleep(0.3)
    return last if last and last != before else None


def profile_company(win) -> str | None:
    """右侧资料卡里的"公司"一栏（企点账号才有）。"""
    card = win.DocumentControl(Name="AIO资料卡", searchDepth=30)
    if not card.Exists(0, 0):
        card = win.DocumentControl(Name="资料卡", searchDepth=30)
        if not card.Exists(0, 0): return None
    controls = [c for c, _ in auto.WalkControl(card, maxDepth=30)]
    texts = [c.Name.strip() for c in controls if c.ControlType == auto.ControlType.TextControl and c.Name.strip()]
    for i, t in enumerate(texts):
        if t == "公司" and i + 1 < len(texts):
            return texts[i + 1]
    if any("company-verify" in c.ClassName for c in controls):
        company = next((text[1:].strip() for text in texts if text.startswith("@") and len(text)>2),None)
        if company: return company
    return None


def verify_supplier(win, company: str, expect_chat: str, *, timeout=4.0, cancelled=lambda: False) -> None:
    """资料卡可能晚于标题加载；只等待缺失资料，不容忍矛盾或会话变化。"""
    deadline = time.monotonic() + timeout
    while True:
        if cancelled():
            raise DesktopDeferred("已暂停身份核对，未发送")
        if current_chat_name(win) != expect_chat:
            raise SupplierIdentityError("会话已变化，已取消发送")
        profile = profile_company(win)
        error = identity_error(company, expect_chat, profile)
        if current_chat_name(win) != expect_chat:
            raise SupplierIdentityError("核对期间会话已变化，已取消发送")
        if not error:
            return
        if profile and profile.strip() or time.monotonic() >= deadline:
            raise SupplierIdentityError(error + "；未发送，请人工核对")
        time.sleep(0.2)


def verify_bound_history(win, name, inquiry_text, anchor_id, sent_at):
    """回复前验证原询价的消息锚点，避免仅凭同名会话发错收件人。"""
    verify = [m for m in read_messages(win) if m.get("id") == anchor_id and m.get("self")
              and _norm(m.get("text", "")) == _norm(inquiry_text)] if anchor_id else []
    if not verify:
        try:
            read_inquiry_history(win, name, inquiry_text, anchor_id, sent_at)
        except Exception as e:
            raise DesktopDeferred("暂未确认绑定会话的原询价，请在 QQ 打开对应会话并补收后继续") from e
    if current_chat_name(win) != name:
        raise SupplierIdentityError("核对绑定期间会话变化，未发送")


# ---------- 发送 ----------

def editor_of(win):
    return find(win, "ExEditor-qq-msg-editor")


def pause(lo: float, hi: float):
    """像人一样随机停一下。"""
    time.sleep(random.uniform(lo, hi))


def _root_at(x: int, y: int) -> int:
    hwnd = win32gui.WindowFromPoint((x, y))
    return win32gui.GetAncestor(hwnd, win32con.GA_ROOT) if hwnd else 0


def _inside(r, x: int, y: int) -> bool:
    return r.left < x < r.right and r.top < y < r.bottom


def human_click(win, ctrl, before_click=None) -> bool:
    """真实移动鼠标到控件上（中心附近随机一点）再点击。

    点击前确认这个位置最上层是 QQ 窗口、控件还在原位（界面刚加载时按钮会挪位置）；
    被别的窗口挡住或一直对不准就不点，返回 False。
    """
    for _ in range(3):
        require_assisted_foreground(win)
        r = ctrl.BoundingRectangle
        if r.width() <= 0 or r.height() <= 0:
            return False
        x = random.randint(r.left + r.width() // 3, r.right - r.width() // 3)
        y = random.randint(r.top + r.height() // 3, r.bottom - r.height() // 3)
        if _root_at(x, y) != win.NativeWindowHandle:
            return False
        auto.MoveTo(x, y, moveSpeed=random.uniform(1.5, 2.5))
        pause(0.15, 0.35)
        if _root_at(x, y) == win.NativeWindowHandle and _inside(ctrl.BoundingRectangle, x, y):
            if before_click:
                before_click()
            require_assisted_foreground(win)
            auto.Click(x, y)
            return True
    return False


def wait_layout_stable(win, timeout: float = 6) -> None:
    """会话刚打开时右侧资料栏是后加载的，会把输入框和发送按钮挤到别处；等发送按钮位置连续 1 秒不变。"""
    end = time.time() + timeout
    last, since = None, time.time()
    while time.time() < end:
        btn = find(win, "send-msg", auto.ControlType.ButtonControl)
        r = btn.BoundingRectangle if btn else None
        cur = (r.left, r.top, r.right, r.bottom) if r else None
        if cur != last:
            last, since = cur, time.time()
        elif cur and time.time() - since >= 1.0:
            return
        time.sleep(0.25)


def editor_text(editor) -> str:
    return "".join(c.Name for c, _ in auto.WalkControl(editor, maxDepth=10)
                   if c.ControlType == auto.ControlType.TextControl and c.Name)


def _norm(s: str) -> str:
    return "".join(s.split())


def _ready_editor(win, expect_chat: str):
    ensure_foreground(win)
    name = current_chat_name(win)
    if name != expect_chat:
        raise RuntimeError(f"当前会话是 {name!r}，不是 {expect_chat!r}，已取消发送")
    editor = editor_of(win)
    if editor is None:
        incidents.record("uia_control_missing", "qq_controls", severity="warning", extra={"control": "editor"})
        raise RuntimeError("找不到 QQ 输入框")
    return editor


def _focus_editor(win, editor):
    if not human_click(win, editor):
        raise DesktopDeferred("QQ 输入框被遮挡，等待前台协助")
    pause(0.4, 1.0)


def _paste_and_send(win, expect_chat: str, put_clipboard, what: str, company: str):
    """把内容放进剪贴板 → Ctrl+V → 确认输入框有内容 → 点发送 → 确认输入框清空。"""
    from . import isolation
    isolation.check_sending()
    ensure_foreground(win)
    verify_supplier(win, company, expect_chat)
    put_clipboard()
    if current_chat_name(win) != expect_chat:
        raise RuntimeError(f"粘贴{what}前会话被切换，已取消发送")
    ensure_foreground(win)
    auto.SendKeys("{Ctrl}v", waitTime=0.2)
    end = time.time() + 5
    while True:
        editor = editor_of(win)
        if editor is not None and "is-empty" not in editor.ClassName:
            break
        if time.time() > end:
            raise RuntimeError(f"粘贴{what}后输入框仍为空，发送失败")
        time.sleep(0.3)
    pause(1.0, 2.5)  # 粘贴后"检查一下"再发
    _click_send(win, expect_chat, what, company)


def _click_send(win, expect_chat: str, what: str, company: str):
    """只点一次并确认结果；不确定时不重发。"""
    from . import isolation
    isolation.check_sending()
    for attempt in range(1):
        if current_chat_name(win) != expect_chat:
            raise RuntimeError(f"发送{what}前会话被切换，已取消（输入框里可能残留内容）")
        wait_layout_stable(win, timeout=3)
        verify_supplier(win, company, expect_chat)
        btn = find(win, "send-msg", auto.ControlType.ButtonControl)
        if btn is None:
            incidents.record("uia_control_missing", "qq_controls", severity="warning", extra={"control": "send_button"})
            raise RuntimeError("找不到发送按钮")
        verify = lambda: verify_supplier(win, company, expect_chat)
        try:
            if not human_click(win, btn, before_click=verify):
                verify()
                btn.GetInvokePattern().Invoke()
        except (SupplierIdentityError, DesktopDeferred):
            raise
        except Exception as e:
            raise SendUncertainError("点击发送时发生异常，结果待核实，不能自动重发") from e
        end = time.time() + 3
        while time.time() < end:
            time.sleep(0.4)
            editor = editor_of(win)
            if current_chat_name(win) != expect_chat:
                raise SendUncertainError("点击后会话发生变化，无法确认发送结果，请人工核对；不要自动重发")
            if editor is not None and "is-empty" in editor.ClassName:
                return
        if editor is None:
            raise SendUncertainError("点击发送后无法读取输入框，发送结果待核实；不要自动重发")
        pause(0.5, 1.0)
    raise SendUncertainError(f"已点击发送但未确认结果，请核实 QQ 中的{what}；不自动重发")


def clear_editor(win, expect_chat: str) -> bool:
    """清空当前会话的输入框（发送失败后清掉残留草稿，免得以后被误发）。"""
    try:
        editor = _ready_editor(win, expect_chat)
        if "is-empty" in editor.ClassName:
            return True
        editor.SetFocus()
        time.sleep(0.2)
        ensure_foreground(win)
        if current_chat_name(win) != expect_chat:
            return False
        auto.SendKeys("{Ctrl}a", waitTime=0.2)
        auto.SendKeys("{Delete}", waitTime=0.3)
        editor = editor_of(win)
        return editor is not None and "is-empty" in editor.ClassName
    except Exception:
        return False


def send_text(win, text: str, expect_chat: str, *, company: str) -> None:
    """在当前会话里发送 text：点输入框 → 粘贴 → 点"发送"，步骤间随机停顿。

    发送前确认会话名等于 expect_chat，不对就抛异常、不发送。
    输入框里如果正好是同一段文字（上次没发出去的草稿），直接发送；是别的内容就不动它。
    """
    from . import isolation
    isolation.check_sending()
    verify_supplier(win, company, expect_chat)
    editor = editor_of(win)
    if editor is None:
        raise EditorUnavailable("QQ 输入框暂不可读")
    # 标准 UIA Value/Invoke 无需全局键盘、剪贴板或鼠标。
    try:
        value = editor.GetPattern(auto.PatternId.ValuePattern)
        btn = find(win, "send-msg", auto.ControlType.ButtonControl)
        invoke = btn.GetPattern(auto.PatternId.InvokePattern) if btn else None
    except Exception:
        value, invoke = None, None
    if value is not None and invoke is not None and not value.IsReadOnly:
        if "is-empty" not in editor.ClassName and _norm(editor_text(editor)) != _norm(text):
            raise DesktopDeferred("QQ 输入框中有其他草稿，已保留，暂不发送")
        verify_supplier(win, company, expect_chat)
        value.SetValue(text)
        if _norm(editor_text(editor)) != _norm(text):
            raise DesktopDeferred("QQ 后台填入内容未能确认，暂不发送")
        verify_supplier(win, company, expect_chat)
        try:
            invoke.Invoke()
            for _ in range(10):
                time.sleep(0.2)
                if current_chat_name(win) != expect_chat:
                    break
                ed = editor_of(win)
                if ed is not None and "is-empty" in ed.ClassName:
                    return
        except Exception as e:
            raise SendUncertainError("后台发送结果待核实，不能自动重发") from e
        raise SendUncertainError("后台已请求发送，但未确认输入框清空，请核实 QQ")
    require_assisted_foreground(win)
    editor = _ready_editor(win, expect_chat)
    pause(0.8, 2.0)  # 看一眼会话再动手
    wait_layout_stable(win)
    editor = editor_of(win) or editor
    if "is-empty" not in editor.ClassName:
        if _norm(editor_text(editor)) == _norm(text):
            _click_send(win, expect_chat, "文字", company)
            return
        raise RuntimeError("输入框里已有其他内容（可能是你正在打字），已取消发送")
    _focus_editor(win, editor)
    _paste_and_send(win, expect_chat, lambda: auto.SetClipboardText(text), "文字", company)


def set_clipboard_image(path: str) -> None:
    from io import BytesIO

    import win32clipboard
    from PIL import Image

    buf = BytesIO()
    Image.open(path).convert("RGB").save(buf, "BMP")
    dib = buf.getvalue()[14:]  # 去掉 BMP 文件头就是 CF_DIB
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("剪贴板被其他程序占用")
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
    finally:
        win32clipboard.CloseClipboard()


def send_image(win, path: str, expect_chat: str, *, company: str) -> None:
    """在当前会话里发送一张图片（粘贴图片再点发送）。"""
    from . import isolation
    isolation.check_sending()
    verify_supplier(win, company, expect_chat)
    editor = _ready_editor(win, expect_chat)
    if "is-empty" not in editor.ClassName:
        raise RuntimeError("输入框里有内容，没有发送图片")
    pause(1.0, 2.5)
    wait_layout_stable(win)
    _focus_editor(win, editor_of(win) or editor)
    _paste_and_send(win, expect_chat, lambda: set_clipboard_image(path), "图片", company)


# ---------- 消息列表 ----------

def read_messages(win) -> list[dict]:
    """当前会话里可见的消息，从上到下：[{"id", "self", "sender", "text"}]。系统灰字提示不算。读不到返回 []。

    结构：ml-item(aid=消息ID) > message > message-container[--self] > avatar-span(name=发送人)
          + message-content__wrapper > ... > message-content > TextControl/ImageControl
    """
    try:
        lst = find(win, "chat-msg-area__vlist")
        if lst is None:
            return []
        out = []
        stamp = ""  # QQ 只在每组消息上方显示一次时间，往下沿用
        for item in find_all(lst, "ml-item", depth=4):
            ts = find(item, "message__timestamp", depth=3)
            if ts is not None:
                stamp = " ".join(c.Name for c, _ in auto.WalkControl(ts, maxDepth=3)
                                 if c.ControlType == auto.ControlType.TextControl and c.Name.strip())
            container = find(item, "message-container", depth=6)
            if container is None:
                continue
            avatar = find(container, "avatar-span", depth=2)
            content = find(container, "message-content", depth=8)
            parts = []
            for c, _ in auto.WalkControl(content or container, maxDepth=20):
                if c.ControlType == auto.ControlType.TextControl and c.Name.strip():
                    parts.append(c.Name.strip())
                elif c.ControlType == auto.ControlType.ImageControl and content is not None:
                    parts.append(f"[{c.Name.strip()}]" if c.Name.strip() else "[图片]")
            out.append({
                "id": item.AutomationId,
                "self": "message-container--self" in container.ClassName,
                "sender": avatar.Name if avatar else "",
                "text": "".join(parts),
                "time": stamp,
            })
        return out
    except Exception:
        return []


def parse_qq_time(s: str) -> float | None:
    try:
        return _parse_qq_time(s)
    except (ValueError, OverflowError, OSError):
        return None


def _parse_qq_time(s: str) -> float | None:
    """把 QQ 聊天里的时间标签（"17:12"、"昨天 17:12"、"10-05 17:12"、"2026-10-05 17:12"）转成时间戳。"""
    import datetime
    import re

    m = re.search(r"(\d{1,2}):(\d{2})", s or "")
    if not m:
        return None
    today = datetime.date.today()
    if "昨天" in s:
        day = today - datetime.timedelta(days=1)
    elif "前天" in s:
        day = today - datetime.timedelta(days=2)
    elif d := re.search(r"(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})", s):
        day = datetime.date(int(d.group(1) or today.year), int(d.group(2)), int(d.group(3)))
        if not d.group(1) and day > today:
            day = day.replace(year=day.year - 1)
    elif w := re.search(r"(?:星期|周)([一二三四五六日天])", s):
        target = "一二三四五六日".index(w.group(1).replace("天", "日"))
        day = today - datetime.timedelta(days=(today.weekday() - target) % 7 or 7)
    else:
        day = today
    return datetime.datetime.combine(day, datetime.time(int(m.group(1)), int(m.group(2)))).timestamp()


# ---------- 会话列表 ----------

def recent_contacts(win) -> list[dict]:
    """左侧会话列表里当前可见的会话：名字、时间、摘要、未读数、控件。"""
    if win32gui.IsIconic(win.NativeWindowHandle):
        return []
    lst = find(win, "recent-contact-list")
    if lst is None:
        return []
    bounds=lst.BoundingRectangle
    left,top,right,bottom=win32gui.GetWindowRect(win.NativeWindowHandle)
    out = []
    for item in lst.GetChildren():
        if "recent-contact-item" not in item.ClassName:
            continue
        rect=item.BoundingRectangle
        cx,cy=(rect.left+rect.right)/2,(rect.top+rect.bottom)/2
        # QQ exposes virtual rows beyond the clipped list. They must not be clicked or used as a scroll signature.
        if not (bounds.left<cx<bounds.right and bounds.top<cy<bounds.bottom and left<cx<right and top<cy<bottom):
            continue
        info = find(item, "item__info", depth=6)
        if info is None:
            continue
        texts = info.GetChildren()
        name = texts[0].Name.strip() if texts else ""
        time_ctrl = find(info, "secondary-info", depth=3)
        summary_ctrl = find(info, "summary-main", depth=3)
        badge = find(item, "q-badge-num", depth=8)
        unread = 0
        if badge is not None:
            digits = "".join(c.Name for c, _ in auto.WalkControl(badge, maxDepth=3) if c.Name)
            unread = int("".join(ch for ch in digits if ch.isdigit()) or 1)
        out.append({
            "name": name,
            "time": " ".join(c.Name for c in time_ctrl.GetChildren()) if time_ctrl else "",
            "summary": "".join(c.Name for c in summary_ctrl.GetChildren()) if summary_ctrl else "",
            "unread": unread,
            "control": item,
        })
    return out


def scroll_region(win, class_name: str, direction: str) -> None:
    """优先用 UIA 滚动；不支持时只在已核对的 QQ 区域内滚动鼠标，不发送按键。"""
    region = find(win, class_name)
    if region is None:
        control = {"chat-msg-area__vlist": "message_list", "recent-contact-list": "conversation_list"}.get(class_name)
        incidents.record("uia_control_missing", "qq_controls", severity="warning", extra={"control": control})
        raise HistoryReadError("找不到需要滚动的 QQ 列表")
    try:
        pattern = region.GetPattern(auto.PatternId.ScrollPattern)
        if pattern and pattern.VerticallyScrollable:
            amount = auto.ScrollAmount.SmallDecrement if direction == "up" else auto.ScrollAmount.SmallIncrement
            pattern.Scroll(auto.ScrollAmount.NoAmount, amount, waitTime=0.4)
            return
    except Exception:
        pass
    require_assisted_foreground(win)
    r = region.BoundingRectangle
    x, y = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    if r.width() <= 0 or r.height() <= 0 or _root_at(x, y) != win.NativeWindowHandle:
        raise HistoryReadError("QQ 列表被遮挡，暂缓读取")
    auto.MoveTo(x, y)
    if _root_at(x, y) != win.NativeWindowHandle:
        raise HistoryReadError("滚动前 QQ 列表被遮挡")
    wheel = auto.WheelUp if direction == "up" else auto.WheelDown
    require_assisted_foreground(win)
    wheel(wheelTimes=2, waitTime=0.4)


def read_inquiry_history(win, name: str, inquiry_text: str, anchor_id: str | None,
                         sent_at: float | None, cancelled=lambda: False):
    def page():
        last = None
        for _ in range(8):
            if cancelled() or current_chat_name(win) != name:
                raise HistoryReadError("读取期间会话变化或收到停止请求")
            current = read_messages(win)
            if current and current == last:
                return current
            last = current
            time.sleep(0.35)
        raise HistoryReadError("QQ 消息仍在加载，稍后重试")

    def scroll(direction):
        if cancelled() or current_chat_name(win) != name:
            raise HistoryReadError("滚动期间会话变化或收到停止请求")
        scroll_region(win, "chat-msg-area__vlist", direction)

    try:
        return read_history(page, scroll, cancelled, inquiry_text, anchor_id, sent_at, parse_qq_time)
    except DesktopDeferred:
        # 不支持后台滚动时，仍保存已确认锚点之后可见的消息，不能谎称完整。
        from .history import HistorySnapshot
        visible = page()
        if anchor_id:
            for i, m in enumerate(visible):
                if m.get("id") == anchor_id and m["self"] and _norm(m["text"]) == _norm(inquiry_text):
                    return HistorySnapshot(visible[i:], anchor_id, complete=False)
        raise


def _click_recent(win, name: str) -> bool:
    if current_chat_name(win) == name:
        return True
    for _ in range(3):
        item = next((c for c in recent_contacts(win) if c["name"] == name), None)
        if item is None:
            return False
        # Invoke 的默认动作在部分 QQ 版本是关闭会话，不能用它切换。
        try:
            select = item["control"].GetPattern(auto.PatternId.SelectionItemPattern)
        except Exception:
            select = None
        if select:
            select.Select()
        else:
            require_assisted_foreground(win)
            if not human_click(win, item["control"]):
                return False
        end = time.time() + 5
        while time.time() < end:
            if current_chat_name(win) == name:
                return True
            time.sleep(0.3)
    return False


def open_recent(win, name: str, cancelled=lambda: False, max_pages: int = 80) -> bool:
    """在整个最近会话列表里查找，覆盖滚出可见范围的供应商。"""
    if cancelled():
        return False
    if _click_recent(win, name):
        return True
    try:
        # 先向上找到列表顶部，再完整向下遍历。以名字序列判断翻页，忽略未读变化。
        for direction in ("up", "down"):
            previous, stable = None, 0
            for _ in range(max_pages):
                if cancelled():
                    return False
                contacts = recent_contacts(win)
                if any(c["name"] == name for c in contacts) and _click_recent(win, name):
                    return True
                current = tuple(c["name"] for c in contacts)
                stable = stable + 1 if current == previous else 0
                if stable >= 3:
                    break
                previous = current
                scroll_region(win, "recent-contact-list", direction)
    except Exception:
        return False
    return False
