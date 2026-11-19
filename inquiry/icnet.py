"""IC交易网（ic.net.cn）：搜索型号、提取供应商、点击 QQ 头像唤起 QQ 会话。"""
import datetime
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse, unquote

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from . import incidents, search_pacing
from .settings import DATA_DIR

SEARCH_URL = "https://www.ic.net.cn/search/{part}.html"
EXTRACT_JS = (Path(__file__).parent / "extract_rows.js").read_text(encoding="utf-8")


class NotLoggedIn(RuntimeError):
    pass


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


def search_page(context: BrowserContext, part_no=None) -> Page:
    """常用型号各复用一个结果页，减少批量轮询时反复加载；仍重新核对供应商行。"""
    searches = []
    for pg in context.pages:
        parsed = urlparse(pg.url)
        if parsed.hostname in ("www.ic.net.cn", "ic.net.cn") and parsed.path.startswith("/search/"):
            searches.append(pg)
            actual = unquote(parsed.path.removeprefix("/search/").removesuffix(".html"))
            if not part_no or actual.upper() == part_no.strip().upper():
                return pg
        if not part_no and "member.ic.net.cn/login" in pg.url:
            return pg
    if len(searches) >= 20:
        return searches[0]
    return context.new_page()


def search(context: BrowserContext, part_no: str, page_no: int = 1, *,
           cancelled=lambda: False, source="candidates", wait=True) -> tuple[Page, list[dict]]:
    def check():
        if cancelled():
            raise SearchCancelled("已停止等待 IC交易网搜索")
        if any(urlparse(p.url).path == "/searchPnCode.php" for p in context.pages):
            incidents.record("captcha", "search", severity="warning")
            raise NotLoggedIn("IC交易网仍在等待网站校验，请人工完成后继续；没有重新发送搜索请求")

    check()
    page = search_page(context, part_no)
    url = SEARCH_URL.format(part=quote(part_no.strip())) + (f"?page={int(page_no)}" if page_no > 1 else "")
    search_pacing.wait_for_search(part_no, page_no, source, check, wait=wait)
    check()
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    if "member.ic.net.cn/login" in page.url:
        incidents.record("login_required", "search", severity="warning")
        raise NotLoggedIn("IC交易网没有登录，请在系统打开的 Edge 窗口里扫码登录")
    if urlparse(page.url).path == "/searchPnCode.php":
        incidents.record("captcha", "search", severity="warning")
        raise NotLoggedIn("IC交易网要求人工检查，请在专用浏览器完成网站校验后继续；未自动重试搜索")
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


def find_row(context: BrowserContext, part_no: str, row: dict, max_pages: int = 3, *,
             cancelled=lambda: False, source="supplier_lookup", wait=True) -> tuple[Page, dict] | None:
    """重新在页面上找到这家供应商当前所在的行（网站每次加载排序都会变，多个型号还共用一个标签页）。"""
    if cancelled():
        raise SearchCancelled("已停止定位 IC交易网供应商")
    page = search_page(context, part_no)
    if norm_part(part_no) in norm_part(page.url) and "/search/" in page.url:
        hit = next((r for r in page.evaluate(EXTRACT_JS) if _same_supplier(r, row)), None)
        if hit:
            return page, {**row, "index": hit["index"]}
    first = int(row.get("page", 1))
    for p in [first] + [p for p in range(1, max_pages + 1) if p != first]:
        page, rows = search(context, part_no, p, cancelled=cancelled, source=source, wait=wait)
        hit = next((r for r in rows if _same_supplier(r, row)), None)
        if hit:
            return page, {**row, "index": hit["index"], "page": p}
    return None


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
        if urlparse(page.url).hostname not in {"wpa.qq.com","wpa1.qq.com","qidian.qq.com","admin.qidian.qq.com"}:
            return None
        text = page.locator("body").inner_text(timeout=600)
        if any(s in text for s in ("无法发起临时会话","不支持临时会话","对方已关闭临时会话","无法发送临时消息")):
            return "对方不支持 QQ 临时会话，已跳过（未发送，不自动添加好友）"
    except Exception:
        pass
    return None
