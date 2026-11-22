"""IC交易网（ic.net.cn）：搜索型号、提取供应商、点击 QQ 头像唤起 QQ 会话。"""
import datetime
import json
import random
import re
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlparse, urlsplit, urlunsplit

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from . import incidents, search_pacing, site_capture
from .settings import DATA_DIR

SEARCH_URL = "https://www.ic.net.cn/search/{part}.html"
DEFAULT_SEARCH_CACHE_HOURS = 4
EXTRACT_JS = (Path(__file__).parent / "extract_rows.js").read_text(encoding="utf-8")
QQ_LINK_HOSTS = {"wpa.qq.com", "wpa1.qq.com"}
QQ_JUMP_HOSTS = QQ_LINK_HOSTS | {"qidian.qq.com", "admin.qidian.qq.com"}


class NotLoggedIn(RuntimeError):
    pass


class MemberPortalRedirect(NotLoggedIn):
    """搜索跳到了未经授权远程操作的 IC 会员页面，需要线下处理。"""


MEMBER_PORTAL_WAIT_TEXT = "搜索跳转到 IC交易网会员页面，请在第二台核对登录并关闭该会员页面"


class SearchCancelled(RuntimeError):
    pass


class QQLaunchFailed(RuntimeError):
    pass


class TemporarySessionRefused(RuntimeError):
    pass


def norm_part(s: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", s.upper())


def parse_qty(s: str) -> int:
    s = s.upper().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*([KW万]?)", s)
    if not m:
        return 0
    n = float(m.group(1))
    return int(n * {"K": 1000, "W": 10000, "万": 10000}.get(m.group(2), 1))


def _result_part(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.hostname in ("www.ic.net.cn", "ic.net.cn") and parsed.path.startswith("/search/"):
        return unquote(parsed.path.removeprefix("/search/").removesuffix(".html")).strip().upper()
    return None


def _result_page_no(url: str) -> int:
    values = parse_qs(urlsplit(url).query, keep_blank_values=True).get("page", ["1"])
    return int(values[0]) if len(values) == 1 and re.fullmatch(r"[1-9][0-9]{0,8}", values[0]) else 0


class SearchTabs:
    """最多保留两张本批次的结果页，人工标签和登录校验页不受影响。"""

    def __init__(self, context: BrowserContext):
        self.context = context
        self._opened = OrderedDict()

    @property
    def pages(self):
        return self.context.pages

    @staticmethod
    def _is_owned_result(page: Page, part: str) -> bool:
        return not page.is_closed() and (page.url == "about:blank" or _result_part(page.url) == part)

    def results(self, part_no: str, preferred: int) -> list[Page]:
        part = part_no.strip().upper()
        pages = [page for key, page in reversed(self._opened.items()) if key[0] == part
                 and self._is_owned_result(page, part) and _result_part(page.url) == part]
        return sorted(pages, key=lambda page: _result_page_no(page.url) != preferred)

    def mark_used(self, page: Page) -> None:
        key = next((key for key, candidate in self._opened.items() if candidate is page), None)
        if key is not None:
            self._opened.move_to_end(key)

    def open(self, part_no: str, page_no: int = 1) -> Page:
        part = part_no.strip().upper()
        key = (part, page_no)
        for old, page in tuple(self._opened.items()):
            if not self._is_owned_result(page, old[0]):
                self._opened.pop(old)
        if key in self._opened:
            self._opened.move_to_end(key)
            return self._opened[key]
        if len(self._opened) >= 2:
            _, oldest = self._opened.popitem(last=False)
            close_quietly(oldest)
        self._opened[key] = page = self.context.new_page()
        return page

    def close(self, part_no: str) -> None:
        part = part_no.strip().upper()
        for key in tuple(self._opened):
            if key[0] == part:
                page = self._opened.pop(key)
                if self._is_owned_result(page, part):
                    close_quietly(page)

    def close_all(self) -> None:
        for part in {key[0] for key in self._opened}:
            self.close(part)


def _existing_search_page(context: BrowserContext | SearchTabs, part_no: str) -> Page | None:
    part = part_no.strip().upper()
    return next((page for page in context.pages if _result_part(page.url) == part), None)


def search_page(context: BrowserContext | SearchTabs, part_no: str, page_no: int = 1) -> Page:
    if isinstance(context, SearchTabs):
        return context.open(part_no, page_no)
    return _existing_search_page(context, part_no) or context.new_page()


def _raise_search_block(kind: str | None) -> None:
    if kind == "login":
        incidents.record("login_required", "search", severity="warning")
        raise NotLoggedIn("IC交易网仍有登录页，请在专用浏览器完成登录后继续")
    if kind == "verification":
        incidents.record("captcha", "search", severity="warning")
        raise NotLoggedIn("IC交易网仍在等待网站校验，请人工完成后继续；没有重新发送搜索请求")


def check_search_session(context: BrowserContext | SearchTabs, *, cancelled=lambda: False):
    if cancelled():
        raise SearchCancelled("已停止等待 IC交易网搜索")
    kinds = {site_capture.page_kind(page.url) for page in context.pages}
    if "login" in kinds:
        _raise_search_block("login")
    if "verification" in kinds:
        _raise_search_block("verification")


def search(context: BrowserContext | SearchTabs, part_no: str, page_no: int = 1, *,
           cancelled=lambda: False, source="candidates", wait=True) -> tuple[Page, list[dict]]:
    def check():
        check_search_session(context, cancelled=cancelled)

    check()
    url = SEARCH_URL.format(part=quote(part_no.strip(), safe="")) + (f"?page={int(page_no)}" if page_no > 1 else "")
    search_pacing.wait_for_search(part_no, page_no, source, check, wait=wait)
    check()
    page = search_page(context, part_no, page_no)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    _raise_search_block(site_capture.page_kind(page.url))
    if site_capture.is_member_portal(page.url):
        incidents.record("login_required", "search", severity="warning")
        raise MemberPortalRedirect(MEMBER_PORTAL_WAIT_TEXT)
    if _result_part(page.url) != part_no.strip().upper() or _result_page_no(page.url) != page_no:
        raise RuntimeError("IC交易网返回的型号或页码与请求不一致")
    page.wait_for_selector("#resultList > li", timeout=30000)
    time.sleep(1)  # 等脚本把混淆用的隐藏元素处理完
    rows = page.evaluate(EXTRACT_JS)
    for r in rows:
        r["page"] = page_no
        r["qtyNum"] = parse_qty(r.get("qty", ""))
    return page, rows


def ref_price(page: Page) -> float | None:
    """搜索结果页顶部的"参考价：￥14.771"。"""
    try:
        return page.evaluate(
            r"() => { const m = document.body.innerText.match(/参考价[：:]\s*[￥¥]\s*([\d.]+)/); return m ? parseFloat(m[1]) : null; }"
        )
    except Exception:
        return None


def candidate_rows(rows: list[dict], part_no: str, qty: int, cfg: dict, exclude: set[str]) -> list[dict]:
    """按配置挑出可以询价的供应商，保持网页上的原有顺序。"""
    today = datetime.date.today().strftime("%m-%d")
    want = norm_part(part_no)
    picked, seen = [], set(exclude)
    for r in rows:
        if not r.get("company") or not r.get("qqs") or r["company"] in seen:
            continue
        if not norm_part(r.get("partNo", "")).startswith(want):
            continue
        if cfg.get("only_today") and r.get("date") != today:
            continue
        r["enoughStock"] = r["qtyNum"] >= qty
        seen.add(r["company"])
        picked.append(r)
    if cfg.get("require_enough_stock", True):
        enough = [r for r in picked if r["enoughStock"]]
        # 库存够的优先；不够的排在后面兜底
        picked = enough + [r for r in picked if not r["enoughStock"]]
    return picked


def _same_supplier(a: dict, b: dict) -> bool:
    return a["company"] == b["company"] and bool({q["uin"] for q in a["qqs"]} & {q["uin"] for q in b["qqs"]})


def find_visible_row(context: BrowserContext | SearchTabs, part_no: str, row: dict, *,
                     cancelled=lambda: False, selected_only: bool = False) -> tuple[Page, dict] | None:
    """只核对已经打开的对应型号结果，不创建页面或触发搜索。"""
    if cancelled():
        raise SearchCancelled("已停止定位 IC交易网供应商")
    part, first = part_no.strip().upper(), int(row.get("page", 1))
    if isinstance(context, SearchTabs):
        pages = context.results(part, first)
    else:
        pages = [page for page in context.pages if _result_part(page.url) == part]
    expected = {**row, "qqs": row["qqs"][:1]} if selected_only else row
    for page in pages:
        hit = next((r for r in page.evaluate(EXTRACT_JS) if _same_supplier(r, expected)), None)
        if hit:
            if isinstance(context, SearchTabs):
                context.mark_used(page)
            return page, _located_row(row, hit, _result_page_no(page.url))
    return None


def _located_row(row: dict, hit: dict, page_no: int) -> dict:
    selected = next((entry for entry in hit["qqs"] if entry["uin"] == row["qqs"][0]["uin"]), row["qqs"][0])
    return {**row, "index": hit["index"], "page": page_no, "qqs": [selected, *row["qqs"][1:]]}


def find_row(context: BrowserContext | SearchTabs, part_no: str, row: dict, max_pages: int = 3, *,
             cancelled=lambda: False, source="supplier_lookup", wait=True) -> tuple[Page, dict] | None:
    """在当前型号的搜索结果中核对供应商行，找不到时按限速翻页。"""
    check_search_session(context, cancelled=cancelled)
    if match := find_visible_row(context, part_no, row, cancelled=cancelled, selected_only=True):
        return match
    first = int(row.get("page", 1))
    expected = {**row, "qqs": row["qqs"][:1]}
    for p in [first] + [p for p in range(1, max_pages + 1) if p != first]:
        page, rows = search(context, part_no, p, cancelled=cancelled, source=source, wait=wait)
        hit = next((r for r in rows if _same_supplier(r, expected)), None)
        if hit:
            return page, _located_row(row, hit, p)
    return None


def direct_qq_url(qq_entry: dict) -> str | None:
    """只接受可按固定格式重建的腾讯 WPA 链接，账号必须与供应商首个 QQ 一致。"""
    if not isinstance(qq_entry, dict):
        return None
    uin = qq_entry.get("uin")
    link = qq_entry.get("inthref") or qq_entry.get("href")
    if not isinstance(uin, str) or not re.fullmatch(r"[1-9][0-9]{4,11}", uin):
        return None
    if not isinstance(link, str) or len(link) > 2048 or not link.isascii() or re.search(r"[\s\x00-\x1f\x7f\\#]", link):
        return None
    try:
        url = urlsplit(link)
        values = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
    except ValueError:
        return None
    params = dict(values)
    host = url.netloc.lower()
    if (url.scheme not in {"https", "http", ""} or host not in QQ_LINK_HOSTS
            or url.path != "/msgrd" or len(params) != len(values) or params.get("uin") != uin):
        return None
    allowed = {"uin": uin, "v": "3", "site": "qq", "menu": "yes"}
    if any(allowed.get(key) != value for key, value in values):
        return None
    return urlunsplit(("https", host, "/msgrd", urlencode(params), ""))


def launch_saved_qq(context: BrowserContext | SearchTabs, qq_entry: dict, timeout: float = 20,
                    before_launch=lambda: None) -> Page:
    """在新标签页打开已核验的 WPA 链接，HTTP 页面导航限于允许的腾讯主机。"""
    url = direct_qq_url(qq_entry)
    if url is None:
        raise QQLaunchFailed("保存的 QQ 链接不符合直开格式")
    check_search_session(context)
    raw_context = context.context if isinstance(context, SearchTabs) else context
    jump = raw_context.new_page()
    try:
        session = raw_context.new_cdp_session(jump)
        blocked = False

        def check_navigation(event):
            nonlocal blocked
            target = urlsplit(event["request"]["url"])
            request = {"requestId": event["requestId"]}
            if target.scheme == "https" and target.netloc.lower() in QQ_JUMP_HOSTS:
                session.send("Fetch.continueRequest", request)
            else:
                blocked = True
                session.send("Fetch.failRequest", {**request, "errorReason": "BlockedByClient"})

        # Chromium 逐请求检查文档导航及重定向，作用域只有这个新标签页。
        session.on("Fetch.requestPaused", check_navigation)
        session.send("Fetch.enable", {"patterns": [{"resourceType": "Document"}]})
        before_launch()
        response = jump.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        if blocked or (response is not None and response.status >= 400):
            raise QQLaunchFailed("保存的 QQ 链接无法打开有效的腾讯页面")
        return jump
    except BaseException:
        close_quietly(jump)
        raise


def launch_qq(page: Page, row: dict, timeout: float = 20, before_launch=lambda: None) -> Page:
    """点击这一行的 QQ 头像 -> 弹窗里点"继续唤起QQ" -> 浏览器打开企点跳转页 -> 唤起 QQ。

    返回新打开的跳转页。QQ 有没有打开对应会话由调用方在 QQ 那边确认，确认后再 close_quietly(跳转页)；
    不能提前关：浏览器"打开QQ"确认框挂在这个标签页上，关掉它确认框也没了。
    """
    li = page.locator("#resultList > li").nth(row["index"])
    current = page.evaluate(
        "i => { const a = Array.from(document.querySelectorAll('#resultList > li')[i].querySelectorAll('.result_supply a.result_goCompany')).find(e => e.checkVisibility()); return a ? a.innerText.trim() : ''; }",
        row["index"],
    )
    if current != row["company"]:
        raise RuntimeError(f"页面行已变化（期望 {row['company']}，实际 {current}），请重新搜索")
    uin = row["qqs"][0]["uin"]
    icon = li.locator(f'.result_askPrice a.qqicon[inthref*="uin={uin}&"]').locator("visible=true").first
    icon.scroll_into_view_if_needed()
    time.sleep(random.uniform(0.6, 1.5))
    icon.hover()
    time.sleep(random.uniform(0.4, 1.0))
    context = page.context
    with context.expect_page(timeout=timeout * 1000) as new_page_info:
        before_launch()
        icon.click()
        btn = page.locator("#qq_div button.jixuqq").locator("visible=true")
        try:
            btn.first.wait_for(timeout=5000)
            before_launch()
            btn.first.click()
        except PlaywrightTimeoutError:
            pass  # 网站去掉弹窗时，点头像会直接打开链接
    return new_page_info.value


OK_ORIGINS_FILE = DATA_DIR / "qq_launch_origins.json"


def load_ok_origins() -> set[str]:
    try:
        return set(json.loads(OK_ORIGINS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def save_ok_origins(origins: set[str]) -> None:
    OK_ORIGINS_FILE.write_text(json.dumps(sorted(origins), ensure_ascii=False), encoding="utf-8")


def settle_origin(jump: Page, wait: float = 4) -> str:
    """等跳转页走完 wpa.qq.com 的重定向，返回最终停留的域名（浏览器的"打开QQ"确认框按域名记忆）。"""
    end = time.time() + wait
    host = ""
    while time.time() < end:
        try:
            host = urlparse(jump.url).netloc
        except Exception:
            break
        if host and host not in ("wpa.qq.com", "wpa1.qq.com") or "qidian" in host:
            break
        time.sleep(0.3)
    return host


def close_quietly(pg: Page | None) -> None:
    try:
        if pg is not None and not pg.is_closed():
            pg.close()
    except Exception:
        pass


def qq_launch_error(page):
    """A Tencent refusal is a terminal result, not a reason to wait or retry that supplier."""
    try:
        if urlparse(page.url).hostname not in QQ_JUMP_HOSTS:
            return None
        text = page.locator("body").inner_text(timeout=600)
        if any(s in text for s in ("无法发起临时会话","不支持临时会话","对方已关闭临时会话","无法发送临时消息")):
            return "对方不支持 QQ 临时会话，已跳过（未发送，不自动添加好友）"
    except Exception:
        pass
    return None
