"""工作台可选的企业微信网站状态提醒。"""

import json
import re
from contextlib import closing
from http.client import HTTPException, HTTPSConnection

from . import config

PATH = config.LAN_DIR / "website-alert-webhook.json"
HOST = "qyapi.weixin.qq.com"
SEND_PATH = "/cgi-bin/webhook/send"
TIMEOUT_SECONDS = 3
KEY = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
WEBHOOK = re.compile(rf"https://{re.escape(HOST)}{SEND_PATH}\?key=({KEY})")
MESSAGES = {
    "verification": "IC交易网需要人工完成验证码核验。",
    "login": "IC交易网需要人工登录。",
}


def load() -> str:
    try:
        value = config.read_json(PATH).get("url")
    except (OSError, ValueError):
        return ""
    return value if isinstance(value, str) and WEBHOOK.fullmatch(value) else ""


def save(value: str) -> None:
    if not isinstance(value, str) or (value and not WEBHOOK.fullmatch(value)):
        raise ValueError("请输入有效的企业微信群机器人 Webhook 地址，留空关闭")
    config.save_json(PATH, {"url": value})


def push(kind: str) -> None:
    message = MESSAGES.get(kind)
    if message is None:
        return
    value = load()
    if not value:
        return
    body = json.dumps({"msgtype": "text", "text": {"content": message}}, ensure_ascii=False).encode("utf-8")
    try:
        # 固定主机的 HTTPS 请求，直接连接，不读取系统代理配置或跟随重定向。
        with closing(HTTPSConnection(HOST, timeout=TIMEOUT_SECONDS)) as connection:
            connection.request("POST", value.removeprefix(f"https://{HOST}"), body=body,
                               headers={"Content-Type": "application/json; charset=utf-8"})
            connection.getresponse().close()
    except (OSError, HTTPException):
        pass
