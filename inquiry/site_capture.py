"""仅从专用浏览器读取 IC交易网登录和校验截图。"""
import asyncio
import base64
import http.client
import json
import math
import re
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from . import browser
from .runtime import CACHE_DIR


MAX_SCREENSHOT_BYTES = 384 * 1024
MAX_CDP_LIST_BYTES = 256 * 1024
CAPTURE_TIMEOUT = 6
STATUSES = {
    "closed": {"ok": False, "text": "未启动", "code": "closed"},
    "login": {"ok": False, "text": "需要登录", "code": "login"},
    "verification": {"ok": False, "text": "需要人工校验", "code": "verification"},
    "ok": {"ok": True, "text": "已就绪", "code": "ok"},
}


def _page_kind(url: str) -> str | None:
    if not isinstance(url, str) or re.search(r"[\s\x00-\x1f\x7f\\]", url):
        return None
    try:
        address = urlsplit(url)
    except ValueError:
        return None
    if address.scheme != "https":
        return None
    host = address.netloc.lower()
    if host == "member.ic.net.cn" and address.path in {"/login", "/login.php"}:
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
            if _page_kind(target.get("url")) == kind:
                target_id = target.get("id")
                return STATUSES[kind].copy(), target_id if _valid_id(target_id) else None
    return STATUSES["ok"].copy(), None


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


async def _capture_page(session, target_id: str, expected_url: str) -> bytes | None:
    navigated = False
    main_frame = None

    def frame_navigated(event):
        nonlocal navigated
        if not event.get("frame", {}).get("parentId"):
            navigated = True

    def within_document(event):
        nonlocal navigated
        if event.get("frameId") == main_frame:
            navigated = True

    session.on("Page.frameNavigated", frame_navigated)
    session.on("Page.navigatedWithinDocument", within_document)
    await session.send("Page.enable")
    before = await _document_identity(session, target_id, expected_url)
    if before is None or navigated:
        return None
    main_frame = before[0]
    viewport = (await session.send("Page.getLayoutMetrics"))["cssVisualViewport"]
    x, y, width, height = (viewport[key] for key in ("pageX", "pageY", "clientWidth", "clientHeight"))
    if (not all(type(value) in (int, float) and math.isfinite(value) for value in (x, y, width, height))
            or not (0 <= x and 0 <= y and 0 < width <= 8192 and 0 < height <= 8192) or navigated):
        return None
    result = await session.send("Page.captureScreenshot", {
        "format": "jpeg", "quality": 68, "fromSurface": True, "captureBeyondViewport": False,
        "clip": {"x": x, "y": y, "width": width, "height": height,
                 "scale": min(1, 1280 / width, 900 / height)},
    })
    if await _document_identity(session, target_id, expected_url) != before or navigated:
        return None
    encoded = result.get("data")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_SCREENSHOT_BYTES + 2) // 3) * 4:
        return None
    picture = base64.b64decode(encoded, validate=True)
    if len(picture) <= MAX_SCREENSHOT_BYTES and picture.startswith(b"\xff\xd8\xff") and picture.endswith(b"\xff\xd9"):
        return picture
    return None


async def _capture_matching_page(endpoint: str, target_id: str, expected_url: str) -> bytes | None:
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
                            return await _capture_page(session, target_id, expected_url)
                    finally:
                        await session.detach()
    return None


def capture_site(target_id: str, kind: str) -> bytes | None:
    """仅在指定目标仍为同一受支持的网页文档时返回内存中的 JPEG。"""
    if not _valid_id(target_id) or kind not in ("login", "verification"):
        return None
    targets = _page_targets()
    if targets is None:
        return None
    target = next((entry for entry in targets if entry.get("id") == target_id
                   and _page_kind(entry.get("url")) == kind), None)
    if target is None or (endpoint := _socket_url()) is None:
        return None
    try:
        return asyncio.run(_capture_matching_page(endpoint, target_id, target["url"]))
    except Exception:
        # 页面关闭、导航或调试连接中断时只保留文字提醒。
        return None
