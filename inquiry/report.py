"""把询价结果导出为 Excel 报告。"""
import datetime
import time

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import db
from .settings import DATA_DIR

REPORT_DIR = DATA_DIR / "reports"
STATUS_CN = {"pending": "未询价", "sent": "已发送待回复", "quoted": "已报价", "no_stock": "无货", "asking": "对方反问(需人工)",
             "failed": "唤起/发送失败", "skipped": "跳过", "identity_unverified": "身份待核实(未发送)", "parse_pending": "回复已保存(待解析)", "send_uncertain": "发送结果待核实"}
HEAD_FILL = PatternFill("solid", fgColor="DDEBF7")
BEST_FILL = PatternFill("solid", fgColor="E2EFDA")


def tax_price(q: dict) -> float | None:
    """含税人民币比较价；税务不明/美元不参与排序，未税且缺税率时才默认 13%。"""
    if not q or q.get("unit_price") is None or q.get("currency") != "CNY":
        return None
    p = float(q["unit_price"])
    if q.get("tax_included") is None or p <= 0:
        return None
    rate = q.get("tax_rate") if q.get("tax_rate") is not None else 13
    return round(p * (1 + float(rate) / 100), 4) if q.get("tax_included") is False else p


def vs_ref(price: float | None, ref: float | None) -> str:
    """含税单价和网站参考价比较，如 "高 91%"。"""
    if not price or not ref:
        return ""
    pct = (price / ref - 1) * 100
    if abs(pct) < 5:
        return "接近"
    return f"{'高' if pct > 0 else '低'} {abs(pct):.0f}%"


def _fmt_time(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else ""


def _write(ws, header, rows, widths):
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = HEAD_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for r in rows:
        ws.append(r)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"


def build(task_ids: list[int]) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    summary = wb.active
    summary.title = "报价汇总"
    detail = wb.create_sheet("全部询价记录")
    sum_rows, detail_rows, best_rows = [], [], []
    for tid in task_ids:
        with db.connect() as c:
            task = dict(c.execute("SELECT * FROM task WHERE id=?", (tid,)).fetchone())
        records = db.task_quotes(tid)
        valid = [r for r in records if r.get("valid")]
        valid.sort(key=lambda r: (tax_price(r["quote"]) is None, tax_price(r["quote"]) or 0))
        for rank, r in enumerate(valid, 1):
            q = r["quote"]
            sum_rows.append([
                task["part_no"], task["qty"], rank, r["company"], r["chat_name"],
                q.get("unit_price"), q.get("currency"),
                {True: "含税", False: "未税", None: "未说明"}[q.get("tax_included")],
                tax_price(q), task.get("ref_price"), vs_ref(tax_price(q), task.get("ref_price")),
                q.get("lead_time"), q.get("batch"), q.get("available_qty"), q.get("brand"), q.get("note"),
                "\n".join(db.messages(r["id"], "in")), _fmt_time(r.get("last_reply_at")),
            ])
            if rank == 1 and tax_price(q) is not None:
                best_rows.append(len(sum_rows) + 1)
        for r in records:
            q = r["quote"] or {}
            row = r["row"]
            detail_rows.append([
                task["part_no"], task["qty"], r["company"], r["chat_name"], STATUS_CN.get(r["status"], r["status"]),
                q.get("unit_price"), q.get("currency"), {True: "含税", False: "未税"}.get(q.get("tax_included"), ""),
                q.get("lead_time"), q.get("batch"), q.get("question"),
                row.get("qty"), row.get("batch"), row.get("warehouse"), row.get("desc"),
                "\n".join(db.messages(r["id"], "in")), "；".join(x for x in (r.get("error"), r.get("read_error")) if x), _fmt_time(r.get("sent_at")), _fmt_time(r.get("last_reply_at")),
            ])
    _write(summary,
           ["型号", "需求数量", "排名", "供应商", "QQ会话", "单价", "币种", "含税", "含税单价(估算)", "网站参考价", "对比参考价",
            "交期", "批次", "可供数量", "品牌", "备注", "供应商原话", "回复时间"],
           sum_rows, [16, 9, 6, 26, 18, 9, 6, 7, 11, 10, 10, 14, 10, 10, 8, 20, 40, 11])
    for r in best_rows:
        for c in summary[r]:
            c.fill = BEST_FILL
    _write(detail,
           ["型号", "需求数量", "供应商", "QQ会话", "状态", "单价", "币种", "含税", "交期", "批次", "对方反问",
            "网站库存", "网站批号", "仓库", "网站说明", "供应商原话", "错误", "发送时间", "回复时间"],
           detail_rows, [16, 9, 26, 18, 14, 9, 6, 7, 14, 10, 20, 9, 9, 12, 20, 40, 20, 11, 11])
    path = REPORT_DIR / f"询价报告_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(path)
    return str(path)
