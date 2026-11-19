"""用几段典型的供应商回复测试 DeepSeek 解析效果。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry.llm import parse_reply  # noqa: E402
from inquiry.settings import SETTINGS  # noqa: E402
from inquiry.runner import render_message  # noqa: E402

PART, QTY = "STM32F405RGT6", 1000
INQ = render_message(SETTINGS["inquiry"]["template"], PART, QTY)
CASES = [
    ["在的", "13.8含13 24+ 现货"],
    ["13.5 未税，25+，2-3天"],
    ["要多少？目标价多少？"],
    ["没货了"],
    ["您好，我是恒佳达张小姐，有什么可以帮您？"],
    ["405RGT6 25+ 原装现货 含税14.2 1K起", "量大可谈"],
    ["$1.95 26+ 现货"],
    ["稍等我查下"],
    ["100-499: 15.5  500+: 14.8  1K+: 14.3 含税 现货 26+"],
]

for replies in CASES:
    t = time.time()
    r = parse_reply(PART, QTY, INQ, replies)
    keep = {k: v for k, v in r.items() if v not in (None, [], "")}
    print(f"{replies} -> {json.dumps(keep, ensure_ascii=False)}  ({time.time() - t:.1f}s)")
