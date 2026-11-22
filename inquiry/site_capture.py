"""通过专用浏览器查看登录和校验页，转发用户的手动操作。"""
import asyncio
import base64
import http.client
import json
import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from . import browser
from .runtime import CACHE_DIR


MAX_SCREENSHOT_BYTES = 384 * 1024
MAX_CDP_LIST_BYTES = 256 * 1024
CAPTURE_TIMEOUT = 6
MAX_DRAG_POINTS = 40
MAX_INPUT_TEXT = 256
_KEY_CODES = {"Tab": 9, "Enter": 13, "Backspace": 8, "Delete": 46, "Escape": 27,
              "ArrowLeft": 37, "ArrowRight": 39, "ArrowUp": 38, "ArrowDown": 40}
STATUSES = {
    "closed": {"ok": False, "text": "未启动", "code": "closed"},
    "login": {"ok": False, "text": "需要登录", "code": "login"},
    "verification": {"ok": False, "text": "需要人工校验", "code": "verification"},
    "ok": {"ok": True, "text": "已就绪", "code": "ok"},
}


def _site_address(url: str):
    if not isinstance(url, str) or re.search(r"[\s\x00-\x1f\x7f\\]", url):
        return None
    try:
        address = urlsplit(url)
    except ValueError:
        return None
    return address if address.scheme == "https" else None


def is_member_portal(url: str) -> bool:
    address = _site_address(url)
    return address is not None and address.netloc.lower() == "member.ic.net.cn"


def page_kind(url: str) -> str | None:
    """只识别允许展示和手动操作的登录、校验地址。"""
    address = _site_address(url)
    if address is None:
        return None
    host = address.netloc.lower()
    if host == "member.ic.net.cn":
        path = address.path
        if path in {"/login", "/login.php"} or (path.startswith("/login/") and "%" not in path
                                               and all(seg not in {".", ".."} for seg in path.split("/"))):
            return "login"
    if host in {"ic.net.cn", "www.ic.net.cn"} and address.path == "/searchPnCode.php":
        return "verification"
    return None


def _cdp_json(path):
    connection = http.client.HTTPConnection("127.0.0.1", browser.CDP_PORT, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            return None
        payload = response.read(MAX_CDP_LIST_BYTES + 1)
        if len(payload) <= MAX_CDP_LIST_BYTES:
            return json.loads(payload)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    return None


def _page_targets():
    entries = _cdp_json("/json/list")
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict) and entry.get("type") == "page"]
    return None


def _valid_id(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value))


def inspect_browser() -> tuple[dict, str | None]:
    """返回专用浏览器状态。仅登录或校验状态可返回对应的页面编号。"""
    targets = _page_targets()
    if targets is None:
        return STATUSES["closed"].copy(), None
    for kind in ("login", "verification"):
        for target in targets:
            if page_kind(target.get("url")) == kind:
                target_id = target.get("id")
                return STATUSES[kind].copy(), target_id if _valid_id(target_id) else None
    ready = STATUSES["ok"].copy()
    if any(is_member_portal(target.get("url")) for target in targets):
        ready["member_portal_open"] = True
    return ready, None


def _socket_url() -> str | None:
    version = _cdp_json("/json/version")
    if not isinstance(version, dict) or not isinstance(version.get("webSocketDebuggerUrl"), str):
        return None
    endpoint = version["webSocketDebuggerUrl"]
    # 只接受 Chromium 在固定本机端口返回的浏览器调试地址。
    if re.fullmatch(rf"ws://127\.0\.0\.1:{browser.CDP_PORT}/devtools/browser/[a-zA-Z0-9_-]+", endpoint):
        return endpoint
    return None


async def _document_identity(session, target_id, expected_url):
    target = (await session.send("Target.getTargetInfo"))["targetInfo"]
    if target.get("type") != "page" or target.get("targetId") != target_id or target.get("url") != expected_url:
        return None
    frame = (await session.send("Page.getFrameTree"))["frameTree"]["frame"]
    address = urlsplit(expected_url)
    if (frame.get("unreachableUrl") or frame.get("url") != address._replace(fragment="").geturl()
            or frame.get("securityOrigin") != f"https://{address.netloc.lower()}"
            or not frame.get("id") or not frame.get("loaderId")):
        return None
    return frame["id"], frame["loaderId"]


@dataclass(frozen=True)
class _Viewport:
    page_x: float
    page_y: float
    width: float
    height: float
    offset_x: float
    offset_y: float
    scale: float


@dataclass(frozen=True, repr=False)
class PageFrame:
    image: bytes
    _target_id: str
    _url: str
    _kind: str
    _document: tuple[str, str]
    _viewport: _Viewport


async def _viewport(session) -> _Viewport | None:
    viewport = (await session.send("Page.getLayoutMetrics"))["cssVisualViewport"]
    values = tuple(viewport[key] for key in ("pageX", "pageY", "clientWidth", "clientHeight", "offsetX", "offsetY", "scale"))
    if not all(type(value) in (int, float) and math.isfinite(value) for value in values):
        return None
    layout = _Viewport(*values)
    if 0 <= layout.page_x and 0 <= layout.page_y and 1 <= layout.width <= 8192 and 1 <= layout.height <= 8192 and layout.scale > 0:
        return layout
    return None


class _PageWatch:
    """在同一调试会话中观察顶层页面导航和可见区域变化。"""

    def __init__(self, session, target_id, url):
        self.session = session
        self.target_id = target_id
        self.url = url
        self.navigated = False
        self.main_frame = None

    async def start(self):
        self.session.on("Page.frameNavigated", self._frame_navigated)
        for event in ("Page.navigatedWithinDocument", "Page.frameStartedNavigating", "Page.frameStartedLoading", "Page.frameDetached"):
            self.session.on(event, self._frame_event)
        await self.session.send("Page.enable")

    def _frame_navigated(self, event):
        if not event.get("frame", {}).get("parentId"):
            self.navigated = True

    def _frame_event(self, event):
        if self.main_frame is None or event.get("frameId") == self.main_frame:
            self.navigated = True

    async def snapshot(self):
        if self.navigated:
            return None
        document = await _document_identity(self.session, self.target_id, self.url)
        if document is None:
            return None
        self.main_frame = document[0]
        layout = await _viewport(self.session)
        if layout is None or self.navigated:
            return None
        return document, layout


async def _capture_page(session, target_id: str, url: str, kind: str) -> PageFrame | None:
    watch = _PageWatch(session, target_id, url)
    await watch.start()
    before = await watch.snapshot()
    if before is None:
        return None
    document, layout = before
    result = await session.send("Page.captureScreenshot", {
        "format": "jpeg", "quality": 68, "fromSurface": True, "captureBeyondViewport": False,
        "clip": {"x": layout.page_x, "y": layout.page_y, "width": layout.width, "height": layout.height,
                 "scale": min(1, 1280 / layout.width, 900 / layout.height)},
    })
    if await watch.snapshot() != before:
        return None
    encoded = result.get("data")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_SCREENSHOT_BYTES + 2) // 3) * 4:
        return None
    picture = base64.b64decode(encoded, validate=True)
    if len(picture) <= MAX_SCREENSHOT_BYTES and picture.startswith(b"\xff\xd8\xff") and picture.endswith(b"\xff\xd9"):
        return PageFrame(picture, target_id, url, kind, document, layout)
    return None


async def _with_target(endpoint, target_id, expected_url, operation):
    async with async_playwright() as playwright:
        async with asyncio.timeout(CAPTURE_TIMEOUT):
            edge = await playwright.chromium.connect_over_cdp(endpoint, timeout=2000, no_defaults=True,
                                                              artifacts_dir=CACHE_DIR)
            for context in edge.contexts:
                for page in context.pages:
                    if page.url != expected_url:
                        continue
                    session = await context.new_cdp_session(page)
                    try:
                        # 页面 URL 可以重复。用会话返回的目标编号选择实际页面。
                        info = (await session.send("Target.getTargetInfo"))["targetInfo"]
                        if info.get("targetId") == target_id:
                            return await operation(session)
                    finally:
                        await session.detach()
    return None


def _target_url(target_id: str, kind: str) -> str | None:
    targets = _page_targets()
    if targets is None:
        return None
    return next((entry["url"] for entry in targets if entry.get("id") == target_id
                 and page_kind(entry.get("url")) == kind), None)


def capture_frame(target_id: str, kind: str) -> PageFrame | None:
    """仅在目标仍为同一受支持的网页文档时返回截图和对应页面信息。"""
    if not _valid_id(target_id) or kind not in ("login", "verification"):
        return None
    url = _target_url(target_id, kind)
    if url is None or (endpoint := _socket_url()) is None:
        return None
    try:
        return asyncio.run(_with_target(endpoint, target_id, url, lambda session: _capture_page(session, target_id, url, kind)))
    except Exception:
        # 页面关闭、导航或调试连接中断时只保留文字提醒。
        return None


def capture_site(target_id: str, kind: str) -> bytes | None:
    frame = capture_frame(target_id, kind)
    return frame.image if frame is not None else None


def _coordinates(point) -> bool:
    return all(type(point.get(axis)) in (int, float) and 0 <= point[axis] <= 1 for axis in ("x", "y"))


def _allowed_action(action) -> bool:
    if not isinstance(action, dict):
        return False
    kind = action.get("kind")
    if kind == "click":
        return action.keys() == {"kind", "x", "y"} and _coordinates(action)
    if kind == "drag":
        points = action.get("points")
        return (action.keys() == {"kind", "points"} and isinstance(points, list) and 2 <= len(points) <= MAX_DRAG_POINTS
                and all(isinstance(point, dict) and point.keys() == {"x", "y"} and _coordinates(point) for point in points))
    if kind == "text":
        value = action.get("text")
        return (action.keys() == {"kind", "text"} and isinstance(value, str) and 0 < len(value) <= MAX_INPUT_TEXT
                and not re.search(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]", value))
    key = action.get("key")
    return kind == "key" and action.keys() == {"kind", "key"} and isinstance(key, str) and key in _KEY_CODES


def _input_events(action, viewport):
    kind = action["kind"]
    if kind == "text":
        yield "Input.insertText", {"text": action["text"]}
    elif kind == "key":
        key = action["key"]
        base = {"key": key, "code": key, "windowsVirtualKeyCode": _KEY_CODES[key], "modifiers": 0}
        down = {"type": "keyDown", "text": "\r"} if key == "Enter" else {"type": "rawKeyDown"}
        yield "Input.dispatchKeyEvent", {**base, **down}
        yield "Input.dispatchKeyEvent", {**base, "type": "keyUp"}
    else:
        points = [action] if kind == "click" else action["points"]
        phases = [("mousePressed", points[0], 1), *(("mouseMoved", point, 1) for point in points[1:]),
                  ("mouseReleased", points[-1], 0)]
        for phase, point, buttons in phases:
            yield "Input.dispatchMouseEvent", {
                "type": phase, "x": min(viewport.width - 1, point["x"] * viewport.width),
                "y": min(viewport.height - 1, point["y"] * viewport.height), "button": "none" if phase == "mouseMoved" else "left",
                "buttons": buttons, "clickCount": 0 if phase == "mouseMoved" else 1, "modifiers": 0,
            }


async def _relay_page(session, frame, action) -> bool:
    watch = _PageWatch(session, frame._target_id, frame._url)
    await watch.start()
    expected = (frame._document, frame._viewport)
    for command, params in _input_events(action, frame._viewport):
        if await watch.snapshot() != expected:
            return False
        await session.send(command, params)
    return True


def relay_input(frame: PageFrame, action: dict) -> bool:
    """把一次已核对的手动操作发送给原截图的顶层页面。"""
    if (not isinstance(frame, PageFrame) or not _allowed_action(action) or not _valid_id(frame._target_id)
            or frame._kind not in ("login", "verification") or page_kind(frame._url) != frame._kind):
        return False
    view = frame._viewport
    if not isinstance(view, _Viewport) or (view.offset_x, view.offset_y, view.scale) != (0, 0, 1):
        # 有平移或捏合缩放的页面与桌面坐标不再一一对应。
        return False
    if _target_url(frame._target_id, frame._kind) != frame._url or (endpoint := _socket_url()) is None:
        return False
    try:
        return asyncio.run(_with_target(endpoint, frame._target_id, frame._url,
                                        lambda session: _relay_page(session, frame, action))) is True
    except Exception:
        return False
