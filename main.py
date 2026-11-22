"""智能询价系统命令行入口。

用法：
  python main.py STM32F405RGT6:1000                 询价一个型号（型号:数量）
  python main.py STM32F405RGT6:1000 GD32F405RGT6:500 同时询价多个型号
  python main.py --file 型号清单.xlsx                从 Excel 读型号（第一列型号，第二列数量，第一行表头）
  python main.py --dry-run STM32F405RGT6:1000        试运行：只搜索并列出会询价的供应商，不发消息
  python main.py --only 恒佳达 STM32F405RGT6:1000     只询价公司名包含"恒佳达"的供应商（测试用）
  python main.py --message "您好，料号：{part}..." ...  本次用自定义话术（{part} 型号、{qty} 数量）
  python main.py --max 4 STM32F405RGT6:5000           本次每个型号最多问 4 家
  python main.py --resume 3                          继续收任务 #3 的回复（只收不发；想多问几家就对同一型号再跑一次）
  python main.py --collect 3 4                       把任务 #3、#4 的回复全部读一遍再出报告（不监听、不发送）
  python main.py --report 3 4                        重新生成任务 #3、#4 的报告
  python main.py --stop                              让正在运行的程序做完手头这一家后安全停止（也可以在运行窗口按一次 Ctrl+C）
"""
import argparse
import msvcrt
import sys

from inquiry import report
from openpyxl import load_workbook
from inquiry.runner import STOP_FILE, Runner, log
from inquiry.settings import DATA_DIR


def acquire_lock():
    """同一时间只允许一个询价程序（两个程序同时连浏览器会互相卡住，也会抢 QQ 窗口）。"""
    f = open(DATA_DIR / "runner.lock", "a+")
    f.seek(0)
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        sys.exit("已经有一个询价程序在运行，请等它结束或先关掉它。")
    return f


def parse_parts(items: list[str]) -> list[tuple[str, int]]:
    parts = []
    for it in items:
        if ":" not in it and "：" not in it:
            sys.exit(f"格式不对：{it}，应为 型号:数量，例如 STM32F405RGT6:1000")
        part, qty = it.replace("：", ":").rsplit(":", 1)
        parts.append((part.strip(), int(qty)))
    return parts


def parts_from_excel(path: str) -> list[tuple[str, int]]:
    ws = load_workbook(path, read_only=True).active
    parts = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and row[0] and len(row) > 1 and row[1]:
            parts.append((str(row[0]).strip(), int(float(row[1]))))
    return parts


def main():
    ap = argparse.ArgumentParser(description="IC交易网 + QQ 自动询价")
    ap.add_argument("parts", nargs="*", help="型号:数量")
    ap.add_argument("--file", help="型号清单 Excel")
    ap.add_argument("--dry-run", action="store_true", help="只搜索并列出供应商，不发消息")
    ap.add_argument("--refresh-search", action="store_true", help="本次重新搜索，跳过已有缓存")
    ap.add_argument("--only", help="只询价公司名包含该关键字的供应商")
    ap.add_argument("--message", help="本次使用的询价话术，可用 {part} {qty}")
    ap.add_argument("--max", type=int, help="本次每个型号最多询价几家（覆盖 settings.toml 的 max_suppliers）")
    ap.add_argument("--resume", nargs="+", type=int, help="继续之前中断的任务")
    ap.add_argument("--collect", nargs="+", type=int, help="把指定任务的回复全部读一遍并生成报告（不监听、不发送）")
    ap.add_argument("--report", nargs="+", type=int, help="重新生成指定任务的报告")
    ap.add_argument("--stop", action="store_true", help="让正在运行的询价程序做完手头这一家后安全停止")
    ap.add_argument("--reply-worker", action="store_true", help="处理用户在界面确认发送的回复队列")
    ap.add_argument("--foreground-assist", action="store_true", help="允许在 QQ 已在前台时使用鼠标键盘；用户切走即暂停")
    ap.add_argument("--vm-guest", action="store_true", help="仅供已配对虚拟机中的执行进程使用")
    ap.add_argument("--worker", action="store_true", help="仅供已配对的另一台 QQ 工作机使用")
    args = ap.parse_args()
    from inquiry import qq, dispatch
    if args.worker or (DATA_DIR / "worker.json").exists():
        if args.vm_guest or (DATA_DIR / "vm_guest.json").exists():
            raise RuntimeError("执行环境角色冲突，未启动 QQ 操作")
        from inquiry import isolation
        isolation.enable_worker()
    elif args.vm_guest or (DATA_DIR / "vm_guest.json").exists():
        from inquiry import isolation
        isolation.enable_guest()
    qq.FOREGROUND_ASSIST = args.foreground_assist

    if args.stop:
        STOP_FILE.write_text("stop", encoding="utf-8")
        print("已发出停止请求：正在运行的程序会做完手头这一家后停止并生成报告。")
        return
    if args.report:
        print(report.build(args.report))
        return
    if args.reply_worker:
        lock = acquire_lock()
        dispatch.run_worker()
        return
    if args.collect:
        lock = acquire_lock()  # noqa: F841
        Runner([], resume=args.collect, collect=True).run()
        return
    parts = parse_parts(args.parts) + (parts_from_excel(args.file) if args.file else [])
    if not parts and not args.resume:
        ap.print_help()
        return
    lock = acquire_lock()  # noqa: F841  进程结束时自动释放
    if parts:
        log("开始询价：" + "，".join(f"{p}×{q}" for p, q in parts) + ("（试运行）" if args.dry_run else ""))
    Runner(parts, dry_run=args.dry_run, only=args.only, message=args.message, resume=args.resume,
           max_suppliers=args.max, refresh_search=args.refresh_search).run()


if __name__ == "__main__":
    main()
