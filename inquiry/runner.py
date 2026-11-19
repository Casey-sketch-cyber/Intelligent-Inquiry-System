"""询价主流程：搜索 → 分批唤起 QQ 发询价 → 轮询回复 → 大模型解析 → 凑够有效报价后出报告。"""
import datetime
import hashlib
import json
import random
import re
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from playwright.sync_api import sync_playwright

from . import browser as browser_mod
from . import db, icnet, incidents, llm, qq, report, workbench, dispatch
from .history import HistoryReadError, covers_previous, normalized
from .identity import SendUncertainError, SupplierIdentityError, name_matches
from .settings import DATA_DIR, ROOT, SETTINGS

LOG_DIR = DATA_DIR / "logs"
STOP_FILE = DATA_DIR / "stop.flag"
POLL_SECONDS = 8
FULL_READ_SECONDS = 3 * 60
MAX_READ_RETRY_SECONDS = 15 * 60


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / f"run_{time.strftime('%Y%m%d')}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def qty_k(q: int) -> str:
    """数量简写：5000 → 5k，2500 → 2.5k，300 → 300。"""
    if q >= 1000 and q % 100 == 0:
        return f"{q / 1000:g}k"
    return str(q)


def render_message(template: str, part: str, qty: int) -> str:
    """话术模板占位符：{part} 型号、{qty} 数量（5000）、{qtyk} 数量简写（5k）。"""
    return template.format(part=part, qty=qty, qtyk=qty_k(qty))


@dataclass
class Task:
    id: int
    part_no: str
    qty: int
    text: str
    started_at: float = field(default_factory=time.time)
    candidates: deque = field(default_factory=deque)
    pages_loaded: int = 0
    sent: int = 0
    waves: int = 0
    last_wave_at: float = 0
    listen_only: bool = False  # --resume 继续的任务只收回复，不再补发
    done: bool = False
    blocked_until: float = 0
    wave_remaining: int = 0


class Runner:
    def __init__(self, parts: list[tuple[str, int]], dry_run: bool = False, only: str | None = None,
                 message: str | None = None, resume: list[int] | None = None, max_suppliers: int | None = None,
                 collect: bool = False):
        self.collect = collect  # 只把 resume 的任务回复收一遍就出报告，不监听、不发送
        self.send_only = False
        self.full_read_seconds = FULL_READ_SECONDS
        self.read_fail: dict[int, int] = {}
        self.cfg = dict(SETTINGS.get("inquiry", {}))
        if max_suppliers:
            self.cfg["max_suppliers"] = max_suppliers
        self.pacing = SETTINGS.get("pacing", {})
        self.filter = SETTINGS.get("filter", {})
        self.parts = parts
        self.dry_run = dry_run
        self.only = only
        self.message = message  # 自定义话术，覆盖 settings.toml 里的模板
        self.resume = resume or []
        self.tasks: list[Task] = []
        self.sent_times: deque = deque()
        self.next_send_at = 0.0
        self.chat_owner: dict[str, list[tuple[Task, int]]] = {}
        self.last_sig: dict[int, tuple] = {}
        self.last_read_at: dict[int, float] = {}
        self.last_attempt_at: dict[int, float] = {}
        self.async_analysis = False  # run() 开启；离线单步调用可同步验证
        self.batch_id = uuid.uuid4().hex
        self.ok_origins = icnet.load_ok_origins()  # 已经成功唤起过 QQ 的跳转页来源
        card = (self.cfg.get("card_image") or "").strip() if self.cfg.get("send_card", False) else ""
        self.card_image = (ROOT / card) if card else None
        if self.card_image and not self.card_image.exists():
            log(f"找不到名片图片 {self.card_image}，本次只发文字")
            self.card_image = None
        self.context = None
        self.page = None
        self.win = None
        self._stop = False
        self.paused = False

    def wait_for_qq(self):
        """等待用户恢复 QQ；不主动恢复、前置或点击窗口。"""
        while not self.stop_requested():
            try:
                self.win = qq.ready_main_window()
                return True
            except qq.DesktopDeferred as e:
                if not self.paused:
                    log(str(e))
                    self.paused = True
                self.idle(5, poll=False)
        return False

    # ---------- 安全停止 ----------
    def stop_requested(self) -> bool:
        """Ctrl+C 或 `main.py --stop`（建 stop.flag）都只是"请求停止"：做完手头这一步再停，不会停在发送一半。"""
        if not self._stop and STOP_FILE.exists():
            try:
                STOP_FILE.unlink()
            except OSError:
                pass
            self._stop = True
            log("收到停止请求：处理完当前这一步就停")
        return self._stop

    def _on_sigint(self, signum, frame):
        if self._stop:
            raise KeyboardInterrupt
        self._stop = True
        log("收到 Ctrl+C：处理完当前这一步就停（再按一次立即退出）")

    def make_tasks(self):
        if self.parts:
            self.parts = workbench.validate_parts([{"part": p, "qty": q} for p, q in self.parts])
            workbench.validate_template(self.message or self.cfg["template"])
        for part, qty in self.parts:
            text = render_message(self.message or self.cfg["template"], part, qty)
            t = Task(db.create_task(part, qty), part, qty, text)
            self.tasks.append(t)
            with db.connect() as c:
                c.execute("UPDATE task SET batch_id=? WHERE id=?", (self.batch_id, t.id))
            log(f"任务 #{t.id}：{part} × {qty}pcs，话术：{text}")
        for tid in self.resume:
            with db.connect() as c:
                row = c.execute("SELECT * FROM task WHERE id=?", (tid,)).fetchone()
            if row is None:
                log(f"没有任务 #{tid}")
                continue
            sups = db.suppliers(tid)
            outs = [m for s in sups for m in db.messages(s["id"], "out")]
            t = Task(tid, row["part_no"], row["qty"], outs[0] if outs else render_message(self.cfg["template"], row["part_no"], row["qty"]))
            t.sent = sum(1 for s in sups if s["sent_at"])
            t.listen_only = True
            for s in sups:
                if s["chat_name"] and s["status"] in ("sent", "quoted", "asking", "no_stock", "parse_pending"):
                    self.chat_owner.setdefault(s["chat_name"], []).append((t, s["id"]))
            with db.connect() as c:
                c.execute("UPDATE task SET status='running', finished_at=NULL WHERE id=?", (tid,))
            self.tasks.append(t)
            log(f"继续任务 #{tid}：{t.part_no} × {t.qty}pcs，已询价 {t.sent} 家，监听 {sum(1 for owners in self.chat_owner.values() for v in owners if v[0] is t)} 个会话")

    # ---------- 入口 ----------
    def run(self) -> str | None:
        if self.parts and not self.resume and not self.collect:
            self.send_only = True
        if not self.send_only:
            STOP_FILE.unlink(missing_ok=True)  # 队列任务保留启动期间收到的暂停请求。
        signal.signal(signal.SIGINT, self._on_sigint)
        self.async_analysis = True
        from . import analysis
        analysis.start()
        dispatch.recover_interrupted()
        if self.collect:  # 只需要 QQ，不用浏览器
            if not self.wait_for_qq():
                return None
            self.make_tasks()
            for t in self.tasks:
                complete = self.sweep(t)
                t.done = True
                db.finish_task(t.id, "stopped" if self.stop_requested() else ("done" if complete else "needs_review"))
                log(f"任务 #{t.id} {t.part_no}：有效报价 {self.valid_count(t)} 条")
            path = report.build([t.id for t in self.tasks])
            log(f"报告已生成：{path}")
            return path
        with sync_playwright() as pw:
            _, context = browser_mod.connect(pw)
            self.context = context
            if not self.dry_run:
                if not self.wait_for_qq():
                    return None
            self.make_tasks()
            try:
                for t in self.tasks:
                    if self.stop_requested():
                        break
                    if t.id not in self.resume and t.sent < int(self.cfg.get("max_suppliers", 30)):
                        self.load_candidates(context, t)
                if self.dry_run:
                    for t in self.tasks:
                        log(f"[试运行] {t.part_no} 可询价供应商 {len(t.candidates)} 家：")
                        for r in list(t.candidates)[:20]:
                            log(f"    {r['company']} | 库存 {r['qty']} | 批号 {r['batch']} | 日期 {r['date']} | QQ {','.join(q['uin'] for q in r['qqs'])}")
                        db.finish_task(t.id, "stopped" if self.stop_requested() else "dry-run")
                    return None
                self.loop(context)
            except KeyboardInterrupt:
                self._stop = True
                log("立即退出")
            except Exception:
                for t in self.tasks:
                    if not t.done:
                        db.finish_task(t.id, "failed")
                raise
            if self._stop:
                log("已停止询价，用已有数据出报告")
            for t in self.tasks:
                if not t.done:
                    db.finish_task(t.id, "stopped")
            path = report.build([t.id for t in self.tasks])
            log(f"报告已生成：{path}")
            return path

    def loop(self, context):
        while any(not t.done for t in self.tasks) and not self.stop_requested():
            for t in self.tasks:
                if t.done or self.stop_requested():
                    continue
                if self.check_done(t):
                    continue
                if self.should_send_wave(t):
                    self.send_wave(context, t, quota=1)
            if not self.send_only:
                self.poll_replies()
            dispatch.process_one(self.win, self.pacing, self.stop_requested)
            self.idle(POLL_SECONDS, poll=False)

    # ---------- 供应商 ----------
    def load_candidates(self, context, t: Task) -> bool:
        max_pages = int(self.filter.get("max_pages", 3))
        if t.pages_loaded >= max_pages:
            return False
        page_no = t.pages_loaded + 1
        try:
            self.page, rows = icnet.search(context, t.part_no, page_no, cancelled=self.stop_requested)
        except icnet.SearchCancelled:
            return False
        except icnet.NotLoggedIn:
            raise
        except Exception as e:
            t.pages_loaded = page_no
            log(f"{t.part_no} 第 {page_no} 页加载失败：{e}")
            return False
        t.pages_loaded = page_no
        if t.pages_loaded == 1:
            db.set_ref_price(t.id, icnet.ref_price(self.page))
        exclude = ({s["company"] for s in db.suppliers(t.id) if s["status"] != "waiting_search"}
                   | {r["company"] for r in t.candidates}
                   | db.recently_asked(t.part_no, float(self.cfg.get("skip_recent_hours", 24))))
        picked = icnet.candidate_rows(rows, t.part_no, t.qty, self.filter, exclude)
        if self.only:
            picked = [r for r in picked if self.only in r["company"]]
        t.candidates.extend(picked)
        log(f"{t.part_no} 第 {t.pages_loaded} 页：{len(rows)} 行，可询价 {len(picked)} 家")
        return bool(picked)

    def valid_count(self, t: Task) -> int:
        return sum(1 for r in db.task_quotes(t.id) if r.get("valid"))

    def check_done(self, t: Task) -> bool:
        target = int(self.cfg.get("target_quotes", 5))
        valid = self.valid_count(t)
        has_sent = t.sent or t.listen_only or any(s["sent_at"] for s in db.suppliers(t.id))
        waited = (time.time() - t.started_at) / 60 if has_sent else 0
        reason = None
        if valid >= target:
            reason = f"已凑齐 {valid} 条有效报价"
        elif self.send_only and t.sent >= int(self.cfg.get("max_suppliers", 30)):
            reason = f"本批发信完成，已询价 {t.sent} 家；请手动收集回复"
        elif self.send_only and not t.candidates and t.pages_loaded >= int(self.filter.get("max_pages", 3)):
            reason = f"候选供应商已检查完，已询价 {t.sent} 家；请手动收集回复"
        elif has_sent and waited >= float(self.cfg.get("max_wait_minutes", 90)):
            reason = f"已等待 {waited:.0f} 分钟，有效报价 {valid} 条"
        else:
            cap_reached = t.sent >= int(self.cfg.get("max_suppliers", 30))
            exhausted = not t.candidates and t.pages_loaded >= int(self.filter.get("max_pages", 3))
            if exhausted and not t.sent and not t.listen_only:
                reason = "没有能成功询价的供应商"
            elif (t.listen_only or cap_reached or exhausted) and not any(s["status"] in ("sent", "parse_pending") for s in db.suppliers(t.id)):
                reason = f"不再询价新供应商，已询价的都回复了，有效报价 {valid} 条"
        if reason:
            complete = True if self.send_only else self.sweep(t)
            t.done = True
            db.finish_task(t.id, "stopped" if self.stop_requested() else ("done" if complete else "needs_review"))
            log(f"任务 #{t.id} {t.part_no} 结束：{reason}（最终有效报价 {self.valid_count(t)} 条）")
        return t.done

    def should_send_wave(self, t: Task) -> bool:
        if time.time() < t.blocked_until:
            return False
        if t.listen_only or t.sent >= int(self.cfg.get("max_suppliers", 30)):
            return False
        if t.wave_remaining:
            return True
        if t.waves == 0:
            return True
        return time.time() - t.last_wave_at >= float(self.cfg.get("wave_wait_minutes", 15)) * 60

    def send_wave(self, context, t: Task, quota=None):
        if not t.wave_remaining:
            size = int(self.cfg.get("first_batch" if t.waves == 0 else "next_batch", 10 if t.waves == 0 else 5))
            t.wave_remaining = min(size, int(self.cfg.get("max_suppliers", 30)) - t.sent)
            t.waves += 1
            log(f"{t.part_no} 第 {t.waves} 批：计划询价 {t.wave_remaining} 家")
        size = min(t.wave_remaining, quota) if quota else t.wave_remaining
        done = 0
        while done < size and not self.stop_requested():
            if not t.candidates and not self.load_candidates(context, t):
                log(f"{t.part_no} 没有更多可询价的供应商了")
                t.wave_remaining = 0
                break
            row = t.candidates.popleft()
            result = self.inquire(t, row)
            if result is None:
                t.candidates.appendleft(row)
                t.blocked_until = time.time() + 30
                # 等待用户/后台能力期间不消耗报价等待期限。
                t.started_at += 30
                break
            if result:
                done += 1
                t.wave_remaining -= 1
                if done < size:
                    self.idle(random.uniform(float(self.pacing.get("min_interval", 20)), float(self.pacing.get("max_interval", 45))))
            if self.check_done(t):
                break
        if t.wave_remaining == 0:
            t.last_wave_at = time.time()

    def wait_rate_limit(self) -> bool:
        cap = int(self.pacing.get("max_per_hour", 60))
        if cap < 1:
            raise ValueError("每小时发送上限必须大于 0")
        while not self.stop_requested():
            now = time.time()
            while self.sent_times and now - self.sent_times[0] > 3600:
                self.sent_times.popleft()
            delay = max(dispatch.rate_delay(self.pacing), self.next_send_at - now)
            if len(self.sent_times) < cap and delay <= 0:
                return True
            self.idle(min(30, max(1, delay)) if len(self.sent_times) < cap else 30)
        return False

    def open_chat(self, t: Task, row: dict) -> str:
        """在网页上点这家供应商的 QQ 头像，等 QQ 打开对应会话，返回会话名。失败抛异常。"""
        company = row["company"]
        jump = None
        name = None
        # 已建立的绑定可直接复用，省去每次网页跳转。发送时仍重新核对公司。
        uin = row.get("qqs", [{}])[0].get("uin")
        with db.connect() as c:
            known_chat = c.execute("SELECT chat_name FROM supplier WHERE company=? AND qq_uin=? AND sent_at IS NOT NULL AND chat_name IS NOT NULL ORDER BY sent_at DESC LIMIT 1",
                                   (company, uin)).fetchone()
        if known_chat and qq.open_recent(self.win, known_chat[0], cancelled=self.stop_requested):
            return known_chat[0]
        # 浏览器的 QQ 协议唤起会切换窗口；只能由用户明确开启前台协助且 QQ 已在前台时执行。
        from . import isolation
        dedicated = isolation.ENABLED and isolation.MODE == "worker"
        if not dedicated:
            qq.require_assisted_foreground(self.win)
        elif isolation.MANUAL.is_set() or isolation.DRAINING.is_set():
            raise qq.DesktopDeferred("工作机已暂停，未打开新会话")

        def check_refusal():
            if message := icnet.qq_launch_error(jump):
                raise icnet.TemporarySessionRefused(message)

        found = icnet.find_row(self.context, t.part_no, row, int(self.filter.get("max_pages", 3)),
                               cancelled=self.stop_requested)
        if found is None:
            raise RuntimeError("搜索结果里找不到这家供应商了（可能已下架）")
        self.page, row = found
        try:
            before = qq.current_chat_name(self.win)
            jump = icnet.launch_qq(self.page, row, before_launch=(lambda: None) if dedicated else lambda: qq.require_assisted_foreground(self.win))
            origin = icnet.settle_origin(jump)
            if dedicated:
                from lanhost.support import confirm_qq_dialog
                if confirm_qq_dialog(cancelled=self.stop_requested,opened=lambda:bool((current:=qq.current_chat_name(self.win))) and current!=before):
                    log("  已确认专用浏览器的腾讯 QQ 唤起许可")
            known = origin in self.ok_origins
            if not known:
                log(f"  如果浏览器弹出“打开QQ”的确认框（来自 {origin}）：请先勾选“始终允许”，再点“打开”")
            t0 = time.time()
            name = qq.wait_chat_change(self.win, before, timeout=20 if known else 90, cancelled=self.stop_requested,
                                       launch_error=check_refusal)
        except (qq.DesktopDeferred, icnet.SearchCancelled, icnet.NotLoggedIn, icnet.TemporarySessionRefused):
            raise
        except Exception as exc:
            raise icnet.QQLaunchFailed(str(exc)) from exc
        else:
            # 几秒内自动打开说明浏览器已"始终允许"这个来源；要等人点确认框的不算
            if name is not None and not known and time.time() - t0 < 10:
                self.ok_origins.add(origin)
                icnet.save_ok_origins(self.ok_origins)
        finally:
            # Closing the active Edge tab here steals focus back from the newly opened QQ chat.
            # A successful launch stays open until browser.connect() cleans stale launch tabs.
            if name is None:
                icnet.close_quietly(jump)
        if name is None:
            cur = qq.current_chat_name(self.win)
            if cur and name_matches(company, cur, qq.profile_company(self.win)):
                return cur  # 本来就停在这个会话上
            raise icnet.QQLaunchFailed("QQ 没有打开新的会话（对方可能不支持临时会话，或 QQ 弹出了其他窗口）")
        return name

    def inquire(self, t: Task, row: dict) -> bool:
        """给一家供应商发询价（文字 + 名片）。成功返回 True。"""
        uin = row.get("qqs", [{}])[0].get("uin") if row.get("qqs") else None
        if uin:
            with db.connect() as c:
                duplicate = c.execute("SELECT 1 FROM supplier s JOIN task t ON t.id=s.task_id WHERE t.part_no=? AND s.qq_uin=? AND ((s.sent_at IS NOT NULL AND s.sent_at>=?) OR s.status IN ('sending','send_uncertain')) LIMIT 1",
                                      (t.part_no, str(uin), time.time()-float(self.cfg.get("skip_recent_hours",24))*3600)).fetchone()
            if duplicate:
                log(f"  跳过 {row['company']}：同型号的这个 QQ 已联系或发送结果待核实")
                return False
        sid = db.add_supplier(t.id, row)
        if sid is None:
            old = next((dict(s) for s in db.suppliers(t.id) if s["company"] == row["company"]), {})
            if old.get("status") not in {"waiting_desktop", "waiting_search"}:
                return False
            sid = old["id"]
        if not self.wait_rate_limit():
            db.update_supplier(sid, status="skipped", error="发送前收到停止请求")
            return False
        company = row["company"]
        name = None
        invoked = False
        try:
            name = self.open_chat(t, row)
            qq.wait_layout_stable(self.win)
            qq.verify_supplier(self.win, company, name)
            before_messages = qq.read_messages(self.win)
            before_ids = {m.get("id") for m in before_messages if m.get("id")}
            same = [m for m in before_messages if m["self"] and normalized(m["text"]) == normalized(t.text)]
            already = False
            original_message = None
            for m in reversed(same):
                sent_time = qq.parse_qq_time(m.get("time", ""))
                if sent_time is None:
                    raise SendUncertainError("聊天中有同文询价，但无法确定发送时间；暂不重发，请人工核对")
                if 0 <= time.time() - sent_time <= float(self.cfg.get("skip_recent_hours", 24)) * 3600:
                    already, original_message = True, m
                    break
            if not already:
                if self.stop_requested():
                    db.update_supplier(sid, status="skipped", chat_name=name, error="发送前收到停止请求")
                    return False
                dispatch.record_attempt(sid, "inquiry")
                db.update_supplier(sid, status="sending", chat_name=name, attempted_at=time.time())
                invoked = True
                qq.send_text(self.win, t.text, expect_chat=name, company=company)
        except icnet.SearchCancelled as e:
            db.update_supplier(sid, status="waiting_search", error=str(e))
            return False
        except icnet.NotLoggedIn as e:
            db.update_supplier(sid, status="waiting_search", error=str(e))
            raise
        except qq.DesktopDeferred as e:
            prior = db.supplier(sid)
            db.update_supplier(sid, status="waiting_desktop", chat_name=name or prior.get("chat_name"), error=str(e)[:300])
            if prior.get("error") != str(e)[:300]:
                log(f"  暂停 {company}：{e}")
                if isinstance(e, qq.EditorUnavailable):
                    incidents.record("uia_control_missing", "qq_controls", severity="warning", task_id=t.id,
                                     supplier_id=sid, extra={"control": "editor"})
            return None
        except SendUncertainError as e:
            db.update_supplier(sid, status="send_uncertain", chat_name=name, attempted_at=time.time(), error=str(e)[:300])
            log(f"  ! {company}：发送结果待核实，已禁止自动重发：{e}")
            self._stop = True
            incidents.record("send_uncertain", "inquiry_send", severity="error", task_id=t.id, supplier_id=sid, error=e)
            return False
        except SupplierIdentityError as e:
            db.update_supplier(sid, status="identity_unverified", chat_name=name, error=str(e)[:300])
            log(f"  ✗ {company}：身份未核实，未发送：{e}")
            incidents.record("identity_unverified", "inquiry_send", severity="warning", task_id=t.id, supplier_id=sid, error=e)
            return False
        except Exception as e:
            self.cleanup_draft(name, t.text)
            db.update_supplier(sid, status="send_uncertain" if invoked else "failed", chat_name=name, error=str(e)[:300])
            if invoked:
                self._stop = True
                incidents.record("send_uncertain", "inquiry_send", severity="error", task_id=t.id, supplier_id=sid, error=e)
            elif isinstance(e, icnet.QQLaunchFailed):
                incidents.record("qq_launch_failed", "qq_launch", severity="error", task_id=t.id, supplier_id=sid, error=e)
            elif isinstance(e, icnet.TemporarySessionRefused):
                incidents.record("temp_session_refused", "qq_launch", severity="warning", task_id=t.id, supplier_id=sid, error=e)
            log(f"  ✗ {company}：{e}")
            return False
        # 文字一发出去就立刻记账，再发名片；这样就算后面出问题，也不会因为漏记而重复询价
        now = time.time()
        self.sent_times.append(now)
        self.next_send_at = now + random.uniform(max(20, float(self.pacing.get("min_interval", 20))),
                                                 max(20, float(self.pacing.get("max_interval", 45))))
        if t.sent == 0:
            t.started_at = now
        t.sent += 1
        notes = []
        if already:
            notes.append("聊天记录里已有同样的询价，没有重复发送")
        db.update_supplier(sid, status="sent", chat_name=name, sent_at=now, error="；".join(notes) or None)
        db.add_message(sid, "out", t.text)
        self.chat_owner.setdefault(name, []).append((t, sid))
        self.last_sig.pop(sid, None)
        # 先记账再取发送消息 ID；取不到时后续只允许按原文+时间查找，不猜测起点。
        try:
            if already and original_message:
                db.update_supplier(sid, inquiry_message_id=original_message.get("id") or None,
                                   sent_at=qq.parse_qq_time(original_message.get("time", "")) or now)
            else:
                for _ in range(8):
                    if qq.current_chat_name(self.win) != name:
                        break
                    sent = [m for m in qq.read_messages(self.win) if m["self"] and m.get("id")
                            and m["id"] not in before_ids and normalized(m["text"]) == normalized(t.text)]
                    if sent and qq.current_chat_name(self.win) == name:
                        db.update_supplier(sid, inquiry_message_id=sent[-1]["id"])
                        break
                    time.sleep(0.35)
        except Exception:
            pass
        if self.card_image and not already:
            try:
                if not self.wait_rate_limit():
                    raise qq.DesktopDeferred("已停止，名片未发送")
                dispatch.record_attempt(sid, "card")
                qq.send_image(self.win, str(self.card_image), expect_chat=name, company=company)
                db.add_message(sid, "out", "[名片]")
            except Exception as e:
                self.cleanup_draft(name, None)
                notes.append(f"名片没发出去：{e}")
                db.update_supplier(sid, error="；".join(notes))
                log(f"  ! {company}：询价已发，但名片没发出去：{e}")
                if isinstance(e, SendUncertainError):
                    incidents.record("send_uncertain", "inquiry_send", severity="error", task_id=t.id, supplier_id=sid, error=e)
                elif isinstance(e, SupplierIdentityError):
                    incidents.record("identity_unverified", "inquiry_send", severity="warning", task_id=t.id, supplier_id=sid, error=e)
        log(f"  ✓ 已询价 {company} → QQ会话「{name}」"
            + ("（聊天记录里已有同样询价，未重复发送）" if already else ""))
        return True

    def cleanup_draft(self, name: str | None, text: str | None):
        """发送失败后清掉输入框里我们自己留下的内容（text=None 表示清掉任何残留，比如没发出去的图片）。"""
        try:
            if not name or qq.current_chat_name(self.win) != name:
                return
            ed = qq.editor_of(self.win)
            if ed is None or "is-empty" in ed.ClassName:
                return
            if text is None or qq._norm(qq.editor_text(ed)) == qq._norm(text):
                if qq.clear_editor(self.win, name):
                    log("    已清掉输入框里没发出去的内容")
        except Exception:
            pass

    # ---------- 回复 ----------
    def idle(self, seconds: float, poll: bool = True):
        """等待 seconds 秒，期间（poll=True 时）每隔几秒看一次回复；收到停止请求就提前结束。"""
        end = time.time() + seconds
        next_poll = time.time()
        while time.time() < end and not self.stop_requested():
            if poll and time.time() >= next_poll:
                if not self.send_only:
                    self.poll_replies()
                if self.win is not None:
                    dispatch.process_one(self.win, self.pacing, self.stop_requested)
                next_poll = time.time() + POLL_SECONDS
            time.sleep(min(0.5, max(0, end - time.time())))

    def poll_replies(self, max_reads=None):
        if not self.chat_owner:
            return
        reads = 0
        try:
            contacts = qq.recent_contacts(self.win)
        except Exception as e:
            log(f"读取 QQ 会话列表失败：{e}")
            return
        visible = {c["name"]: c for c in contacts}
        for name, owners in sorted(self.chat_owner.items(), key=lambda item:min(self.last_attempt_at.get(sid,0) for _,sid in item[1])):
            for t, sid in owners:
                if self.stop_requested():
                    return
                if t.done:
                    continue
                now = time.time()
                c = visible.get(name)
                sig = (c["time"], c["summary"], c["unread"]) if c else None
                if sid not in self.last_sig and sid in self.last_read_at and sig and not c["unread"]:
                    self.last_sig[sid] = sig
                # 可见会话以摘要变化为触发；同样的五家不会每分钟重新点开。
                # 看不到摘要的会话低频补读，并对失败做指数退避。
                # 同一分钟的同文回复可能不改变摘要/未读数；低频核对消息 ID，防止永久漏收。
                due = now - self.last_read_at.get(sid, 0) >= self.full_read_seconds
                if not due and (sig is None or self.last_sig.get(sid) == sig):
                    continue
                retry = min(MAX_READ_RETRY_SECONDS, 30 * 2 ** min(self.read_fail.get(sid, 0), 5))
                if now - self.last_attempt_at.get(sid, 0) < retry:
                    continue
                self.last_attempt_at[sid] = now
                if self.read_chat(name, t, sid):
                    # 只能确认读取前观察到的签名，解析期间到达的消息留待下轮读取。
                    if sig is not None:
                        self.last_sig[sid] = sig
                    self.last_read_at[sid] = time.time()
                    self.read_fail.pop(sid, None)
                else:
                    self.read_fail[sid] = self.read_fail.get(sid, 0) + 1
                    if self.read_fail[sid] in (1, 10, 50):
                        log(f"  「{name}」本次未完整读取，已有数据保留，稍后重试")
                reads += 1
                if max_reads is not None and reads >= max_reads:
                    return

    def read_chat(self, name: str, t: Task, sid: int, snapshot=None) -> bool:
        """点开会话读取对方在我方询价之后的回复，有新内容就交给大模型解析。真正读到了返回 True。"""
        record = db.supplier(sid)
        outs = [m for m in db.messages(sid, "out") if m != "[名片]"]
        inquiry_text = outs[0] if outs else t.text
        try:
            if snapshot is None:
                if qq.current_chat_name(self.win) != name and not qq.open_recent(self.win, name, cancelled=self.stop_requested):
                    raise HistoryReadError("会话未找到或暂时打不开，请稍后补收")
                snapshot = qq.read_inquiry_history(self.win, name, inquiry_text,
                                                   record.get("inquiry_message_id"), record.get("sent_at"), self.stop_requested)
            if qq.current_chat_name(self.win) != name:
                raise HistoryReadError("读取后会话变化，请稍后补收")
        except Exception as e:
            reason = str(e)[:300]
            db.update_supplier(sid, read_error=reason)
            if reason != record.get("read_error") and not self.stop_requested():
                incidents.record("read_incomplete", "collection", severity="warning", task_id=t.id, supplier_id=sid, error=e)
            return False
        if snapshot.anchor_id:
            db.update_supplier(sid, inquiry_message_id=snapshot.anchor_id)
        known_inquiries = {normalized(text) for text in db.inquiry_texts(name)}
        with db.connect() as c:
            other_models = [r[0] for r in c.execute(
                "SELECT DISTINCT t.part_no FROM supplier s JOIN task t ON t.id=s.task_id "
                "WHERE s.chat_name=? AND s.sent_at IS NOT NULL AND s.id!=? AND s.sent_at>=? AND s.sent_at<=?",
                (name, sid, (record.get("sent_at") or time.time()) - 86400, record.get("sent_at") or time.time()))]
            manual_owners = {r["source_id"]: r["assigned_to"] for r in c.execute(
                "SELECT source_id,assigned_to FROM inbox WHERE chat_name=? AND assigned_to IS NOT NULL", (name,))}
        ambiguous = bool(other_models)
        incoming = []
        for m in snapshot.messages[1:]:
            if m["self"] and normalized(m["text"]) in known_inquiries:
                ambiguous = True
            if not m["self"] and m["text"]:
                if m.get("id") in manual_owners and manual_owners[m["id"]] != sid:
                    continue
                # 多型号同会话的无型号回复无法可靠推断，原文放入待归属区。
                explicit = re.search(r"(?<![A-Z0-9])" + re.escape(t.part_no.upper()) + r"(?![A-Z0-9])", m["text"].upper())
                if ambiguous and not explicit:
                    workbench.save_unassigned(name, m, "此 QQ 会话有多个型号，请选择归属")
                else:
                    incoming.append(m)
        # 人工归属的消息不被后续自动读取覆盖。
        with db.connect() as c:
            assigned = [dict(r) for r in c.execute("SELECT source_id AS id,content AS text,message_time AS time FROM inbox WHERE assigned_to=? ORDER BY created_at,id", (sid,))]
        for m in assigned:
            if m["id"] not in {x.get("id") for x in incoming}:
                incoming.append(m)
        replies = [m["text"] for m in incoming]
        previous = db.incoming_records(sid)
        if not snapshot.complete and all(m.get("id") for m in previous + incoming):
            incoming = previous + [m for m in incoming if m["id"] not in {p["id"] for p in previous}]
            replies = [m["text"] for m in incoming]
        if not covers_previous(previous, incoming):
            reason = "本次快照缺少此前已保存的回复，保留原数据，请人工核对后补收"
            db.update_supplier(sid, read_error=reason)
            if reason != record.get("read_error"):
                incidents.record("read_incomplete", "collection", severity="warning", task_id=t.id, supplier_id=sid)
            return False
        # 已标记待补收时，重复读到同一份不完整快照不再重复记日志。
        if not snapshot.complete and not (record.get("read_error") or "").endswith("补收"):
            incidents.record("read_incomplete", "collection", severity="warning", task_id=t.id, supplier_id=sid)
        if not replies:
            db.update_supplier(sid, read_error=None if snapshot.complete else "仅检查了当前可见记录，完整历史待补收", last_checked_at=time.time())
            return snapshot.complete
        digest = hashlib.sha256(json.dumps(replies, ensure_ascii=False).encode()).hexdigest()
        if digest == record.get("parsed_reply_hash"):
            db.update_supplier(sid, read_error=None if snapshot.complete else "已保存可见回复；完整历史待补收", last_checked_at=time.time())
            return snapshot.complete
        # 即使模型暂时不可用也保存原话；只有解析成功才更新 hash，失败后会重试。
        db.replace_incoming(sid, replies, incoming)
        reply_at = qq.parse_qq_time(incoming[-1].get("time", "")) or time.time()
        if self.async_analysis:
            with db.connect() as c:
                c.execute("UPDATE quote SET valid=0 WHERE supplier_id=?", (sid,))
            db.update_supplier(sid, status="parse_pending", last_reply_at=reply_at, last_checked_at=time.time(),
                               read_error=None if snapshot.complete else "已保存可见回复；后台不能翻页，完整历史待前台补收")
            workbench.enqueue_analysis(sid)
            return snapshot.complete
        try:
            q = llm.parse_reply(t.part_no, t.qty, inquiry_text, replies)
        except Exception as e:
            with db.connect() as c:
                c.execute("UPDATE quote SET valid=0 WHERE supplier_id=?", (sid,))
            db.update_supplier(sid, status="parse_pending", read_error=f"回复已保存，解析待重试：{str(e)[:200]}", last_reply_at=reply_at)
            log(f"  解析 {name} 的回复失败（稍后重试）：{e}")
            incidents.record_parse_failure("analysis", e, task_id=t.id, supplier_id=sid)
            return False
        valid = q["status"] == "quoted" and q["unit_price"] is not None
        db.save_quote(sid, q, valid)
        status = {"quoted": "quoted", "no_stock": "no_stock", "asking": "asking"}.get(q["status"], "sent")
        db.update_supplier(sid, status=status, last_reply_at=reply_at, read_error=None,
                           last_checked_at=time.time(), parsed_reply_hash=digest)
        brief = " / ".join(str(x) for x in (q.get("unit_price"), q.get("currency"), "含税" if q.get("tax_included") else ("未税" if q.get("tax_included") is False else None), q.get("lead_time"), q.get("batch")) if x)
        log(f"  ← {name}（{t.part_no}）：{replies[-1][:40]} ⇒ {status} {brief}")
        return True

    def sweep(self, t: Task) -> bool:
        """把这个任务所有询价过的会话都点开读一遍（任务结束前的最后清点，或 --collect）。"""
        sups = [s for s in db.suppliers(t.id) if s["chat_name"] and s["sent_at"]]
        if not sups:
            return True
        log(f"清点任务 #{t.id} {t.part_no} 的 {len(sups)} 个询价会话……")
        complete = True
        for s in sups:
            if self.stop_requested():
                return False
            if not self.read_chat(s["chat_name"], t, s["id"]):
                complete = False
                log(f"  「{s['chat_name']}」未完整读取，已标记待补收")
        return complete
