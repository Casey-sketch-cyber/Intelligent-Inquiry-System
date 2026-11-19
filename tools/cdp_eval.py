"""开发调试用：连接系统专用 Edge（CDP 端口 9333），在匹配的标签页里执行一段 JS，打印 JSON 结果。

用法：python tools/cdp_eval.py <网址关键字> <js文件或JS表达式>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry.runtime import configure_runtime
configure_runtime()
from playwright.sync_api import sync_playwright

url_kw, js = sys.argv[1], sys.argv[2]
if Path(js).is_file():
    js = Path(js).read_text(encoding="utf-8")

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9333")
    pages = [pg for ctx in browser.contexts for pg in ctx.pages]
    print("标签页：", [pg.url for pg in pages])
    page = next((pg for pg in pages if url_kw in pg.url), None)
    if page is None:
        sys.exit(f"没找到网址包含 {url_kw!r} 的标签页")
    result = page.evaluate(js)
    print(json.dumps(result, ensure_ascii=False, indent=1))
