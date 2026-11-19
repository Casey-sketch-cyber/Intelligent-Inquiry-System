"""给指定的 QQ 会话补发名片（会话要在 QQ 左侧会话列表里能看到）。

用法：.venv\\Scripts\\python.exe -m tools.send_card --foreground-assist "供应商会话名"
"""
import argparse
import sys
import time

from inquiry import db, qq, dispatch
from inquiry.settings import ROOT, SETTINGS
from main import acquire_lock

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("chat")
parser.add_argument("--foreground-assist", action="store_true")
args = parser.parse_args()
chat = args.chat
qq.FOREGROUND_ASSIST = args.foreground_assist
card = ROOT / (SETTINGS.get("inquiry", {}).get("card_image") or "名片.jpg")
if not card.exists():
    sys.exit(f"找不到名片图片 {card}")

lock = acquire_lock()  # noqa: F841  不能和询价程序同时操作 QQ
win = qq.ready_main_window()
with db.connect() as c:
    row = c.execute("SELECT * FROM supplier WHERE chat_name=? AND sent_at IS NOT NULL ORDER BY id DESC LIMIT 1", (chat,)).fetchone()
if row is None:
    sys.exit("没有找到该会话已联系的供应商记录，无法核对身份，未发送名片")
if not qq.open_recent(win, chat):
    sys.exit(f"QQ 会话列表里找不到「{chat}」，请先在 QQ 里打开一下这个会话")
original = db.messages(row["id"], "out")
if not original:
    sys.exit("缺少原询价记录，未发送")
qq.verify_bound_history(win, chat, original[0], row["inquiry_message_id"], row["sent_at"])
qq.require_assisted_foreground(win)
while dispatch.rate_delay(SETTINGS.get("pacing", {})) > 0:
    time.sleep(min(1, dispatch.rate_delay(SETTINGS.get("pacing", {}))))
dispatch.record_attempt(row["id"], "card")
qq.send_image(win, str(card), expect_chat=chat, company=row["company"])
last = [m for m in qq.read_messages(win) if m["self"]][-1:]
print(f"已给「{chat}」发送名片；会话里我方最后一条：{last[0]['text'] if last else '（读不到）'}")
if row:
    db.add_message(row["id"], "out", "[名片]")
