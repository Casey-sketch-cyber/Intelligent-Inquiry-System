"""有界读取虚拟消息列表。必须找到本次询价，且相邻页连续，才返回完整快照。"""
from dataclasses import dataclass


class HistoryReadError(RuntimeError):
    pass


@dataclass
class HistorySnapshot:
    messages: list[dict]
    anchor_id: str
    complete: bool = True


def normalized(text: str) -> str:
    return "".join(text.split())


def signature(messages: list[dict]) -> tuple:
    return tuple((m.get("id"), m.get("self"), m.get("text")) for m in messages)


def covers_previous(previous: list[dict], incoming: list[dict]) -> bool:
    """新快照不能丢掉已保存的消息。优先用 ID，旧记录按原文顺序核对。"""
    remaining = iter(incoming)
    for old in previous:
        for new in remaining:
            if old.get("id"):
                matched = old["id"] == new.get("id")
            else:
                matched = old["text"] == new["text"]
            if matched:
                break
        else:
            return False
    return True


def prepend_page(older: list[dict], newer: list[dict]) -> list[dict]:
    """按消息 ID 拼接有重叠的页面，不能以文字去重（同文可能是两条消息）。"""
    for overlap in range(min(len(older), len(newer)), 0, -1):
        left, right = older[-overlap:], newer[:overlap]
        if all(a.get("id") and a["id"] == b.get("id") for a, b in zip(left, right)):
            return older[:-overlap] + newer
    raise HistoryReadError("消息翻页没有连续重叠，保留已有回复，稍后重新读取")


def read_history(read_page, scroll, cancelled, inquiry_text: str, anchor_id: str | None,
                 sent_at: float | None, parse_time, max_pages: int = 80) -> HistorySnapshot:
    """回到底部后向上查找精确询价锚点。旧记录额外核对发送时间，不能只匹配型号。"""
    def read():
        if cancelled():
            raise HistoryReadError("读取已停止，已有回复保留")
        page = read_page()
        if not page:
            raise HistoryReadError("消息列表未加载，稍后重试")
        return page

    page = read()
    # 从用户可能停留的历史位置回到最新消息；连续三次不变才视为到底。
    stable = 0
    for _ in range(max_pages):
        scroll("down")
        new = read()
        stable = stable + 1 if signature(new) == signature(page) else 0
        page = new
        if stable >= 3:
            break
    else:
        raise HistoryReadError("未能定位最新消息，保留已有回复")

    messages, stable = page, 0
    for _ in range(max_pages):
        candidates = []
        for i, message in enumerate(messages):
            if not message.get("self") or normalized(message.get("text", "")) != normalized(inquiry_text):
                continue
            if anchor_id:
                if message.get("id") == anchor_id:
                    candidates.append(i)
            else:
                stamp = parse_time(message.get("time", ""))
                if sent_at and stamp and abs(stamp - sent_at) <= 120:
                    candidates.append(i)
        if len(candidates) > 1:
            raise HistoryReadError("找到多条同文询价，无法确定本次起点，需要人工核对")
        if candidates:
            start = candidates[0]
            return HistorySnapshot(messages[start:], messages[start].get("id", ""))
        scroll("up")
        older = read()
        if signature(older) == signature(page):
            stable += 1
            if stable >= 3:
                break
        else:
            messages = prepend_page(older, messages)
            stable = 0
        page = older
    raise HistoryReadError("未找到本次询价原文或发送时间不明确，未把旧消息作为新回复；请人工核对后补收")
