"""QQ 发送前的只读控件自检和版本状态。"""
import logging
import threading
import time

from . import incidents, qq


CACHE_SECONDS = 6
LABELS = {
    "qq_window": "QQ 主窗口",
    "conversation_list": "会话列表",
    "send_button": "发送按钮",
    "message_list": "聊天记录列表",
    "editor": "输入框",
}
_lock = threading.Lock()
_cached = None
_logger = logging.getLogger(__name__)


class SelfcheckDeferred(RuntimeError):
    """关键控件缺失，等待工作机 QQ 就绪。"""


def _missing_controls(win):
    with qq.auto.UIAutomationInitializerInThread():
        if win is None:
            win = qq.passive_main_window()
        if win is None:
            return ["qq_window"]
        checks = {
            "conversation_list": lambda: qq.find(win, "recent-contact-list"),
            "send_button": lambda: qq.find(win, "send-msg", qq.auto.ControlType.ButtonControl),
            "message_list": lambda: qq.find(win, "chat-msg-area__vlist"),
            "editor": lambda: qq.editor_of(win),
        }
        missing = []
        for control, read in checks.items():
            try:
                found = read()
            except Exception:
                found = None
            if found is None:
                missing.append(control)
        return missing


def _probe(win):
    try:
        missing = _missing_controls(win)
    except Exception:
        missing = ["qq_window"]
    try:
        versions = incidents._versions()
    except Exception:
        versions = {}

    text = "QQ 自检通过"
    if missing == ["qq_window"]:
        text = "自检缺少 QQ 主窗口，请在工作机登录 QQ 并打开主窗口"
    elif missing:
        labels = "、".join(LABELS[control] for control in missing)
        text = f"QQ 自检缺少{labels}，请在工作机打开一个 QQ 会话后重试"
    return {"ok": not missing, "missing": missing, "qq_version": versions.get("qq_version"),
            "edge_version": versions.get("edge_version"), "text": text, "checked_at": time.time()}


def latest() -> dict | None:
    """复制最近结果。查询状态时不访问 QQ，也不等待正在执行的探测。"""
    cached = _cached
    if cached is None:
        return None
    state = cached[1]
    return {**state, "missing": state["missing"].copy()}


def snapshot(*, force=False, win=None) -> dict:
    """使用短缓存或在当前线程只读检查 QQ 界面。"""
    global _cached
    with _lock:
        if not force and win is None and _cached is not None and time.monotonic() < _cached[0]:
            return latest()

        previous = _cached[1]["missing"] if _cached is not None else ()
        state = _probe(win)
        _cached = (time.monotonic() + CACHE_SECONDS, state)
        for control in state["missing"]:
            if control not in previous:
                try:
                    incidents.record("uia_control_missing", "qq_controls", severity="warning", extra={"control": control})
                except Exception:
                    _logger.warning("无法记录 QQ 自检事件")
        return latest()


def require_ready(*, win=None) -> dict:
    state = snapshot(force=True, win=win)
    if not state["ok"]:
        raise SelfcheckDeferred(state["text"])
    return state
