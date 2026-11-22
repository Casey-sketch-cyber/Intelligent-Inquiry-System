"""把办公端的人工输入绑定到同一次提醒和专用浏览器画面。"""
from __future__ import annotations

import base64
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass

from inquiry import site_capture


TOKEN = re.compile(r"[0-9a-f]{32}")
FRAME_TTL = 12
CAPTURE_INTERVAL = 2
MAX_TEXT_LENGTH = 256
MAX_DRAG_POINTS = 40
KEYS = frozenset(("Tab", "Enter", "Backspace", "Delete", "Escape",
                  "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"))


class ManualPageConflict(RuntimeError):
    pass


class ManualPageUnavailable(RuntimeError):
    pass


def is_token(value) -> bool:
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def _is_point(point) -> bool:
    return (isinstance(point, dict) and set(point) == {"x", "y"}
            and all(type(value) in (int, float) and 0 <= value <= 1 for value in point.values()))


def input_action(action):
    if isinstance(action, dict):
        kind = action.get("kind")
        if kind == "click" and set(action) == {"kind", "x", "y"} and _is_point({"x": action["x"], "y": action["y"]}):
            return action
        if kind == "drag" and set(action) == {"kind", "points"}:
            points = action["points"]
            if isinstance(points, list) and 2 <= len(points) <= MAX_DRAG_POINTS and all(map(_is_point, points)):
                return action
        if kind == "text" and set(action) == {"kind", "text"}:
            text = action["text"]
            if (isinstance(text, str) and 1 <= len(text) <= MAX_TEXT_LENGTH
                    and all(unicodedata.category(char) not in {"Cc", "Cs"} for char in text)):
                return action
        if kind == "key" and set(action) == {"kind", "key"}:
            if isinstance(action["key"], str) and action["key"] in KEYS:
                return action
    raise ValueError("人工输入不在允许范围内")


@dataclass(frozen=True)
class FrameTicket:
    token: str
    event: str
    selection: tuple[str, str]
    captured_at: float
    frame: site_capture.PageFrame

    def response(self):
        return {"frame_token": self.token, "image": base64.b64encode(self.frame.image).decode("ascii")}


class ManualWebsite:
    def __init__(self, alerts, available):
        self._alerts = alerts
        self._available = available
        self._lock = threading.Lock()
        self._tickets: dict[str, FrameTicket] = {}
        self._last_attempt = None

    def _selection(self, event):
        selection = self._alerts.control_target(event)
        if selection is None:
            raise ManualPageConflict("网站提示已解除或变化，请查看当前提醒")
        if not self._available():
            raise ManualPageConflict("工作机正在执行任务，请稍后再处理")
        return selection

    def _prune(self, event, selection, now):
        self._tickets = {token: ticket for token, ticket in self._tickets.items()
                         if ticket.event == event and ticket.selection == selection
                         and now - ticket.captured_at < FRAME_TTL}

    def frame(self, event: str):
        if not is_token(event):
            raise ValueError("网站提醒编号无效")
        if not self._lock.acquire(blocking=False):
            raise ManualPageConflict("正在处理上一项操作，请稍后刷新画面")
        try:
            selection = self._selection(event)
            now = time.monotonic()
            self._prune(event, selection, now)
            for ticket in reversed(self._tickets.values()):
                if now - ticket.captured_at < CAPTURE_INTERVAL:
                    return ticket.response()
            if self._last_attempt and self._last_attempt[:2] == (event, selection):
                if now - self._last_attempt[2] < CAPTURE_INTERVAL:
                    raise ManualPageUnavailable("正在等待下一帧，请稍后刷新")
            self._last_attempt = (event, selection, now)
            picture = site_capture.capture_frame(*selection)
            if picture is None:
                raise ManualPageUnavailable("暂时无法取得网站画面，请稍后刷新")
            if self._selection(event) != selection:
                raise ManualPageConflict("网站画面已变化，请重新获取")
            ticket = FrameTicket(uuid.uuid4().hex, event, selection, time.monotonic(), picture)
            self._tickets[ticket.token] = ticket
            if len(self._tickets) > 2:
                self._tickets.pop(next(iter(self._tickets)))
            return ticket.response()
        finally:
            self._lock.release()

    def relay(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"event", "frame_token", "action"}:
            raise ValueError("人工输入格式无效")
        event, token = payload["event"], payload["frame_token"]
        if not is_token(event) or not is_token(token):
            raise ValueError("网站提醒或画面编号无效")
        action = input_action(payload["action"])
        if not self._lock.acquire(blocking=False):
            raise ManualPageConflict("工作机正在处理上一项操作，请刷新画面")
        try:
            selection = self._selection(event)
            self._prune(event, selection, time.monotonic())
            ticket = self._tickets.get(token)
            if ticket is None:
                raise ManualPageConflict("画面已过期，请刷新后再操作")
            # 一次输入作废此前所有画面，传输中断也不能用旧编号重复输入。
            self._tickets.clear()
            self._last_attempt = None
            if not site_capture.relay_input(ticket.frame, action):
                raise ManualPageConflict("页面已变化或输入结果未确认，请刷新画面核对")
        finally:
            self._lock.release()
