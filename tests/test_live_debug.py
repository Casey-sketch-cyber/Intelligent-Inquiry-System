import threading
from unittest.mock import Mock,patch
from test_reliability import IsolatedTest
from inquiry import identity,isolation,qq,icnet
from inquiry.runner import Runner,Task
from lanhost import live_trial


class LiveDebugTests(IsolatedTest):
    def test_traditional_and_sales_department_normalize_without_fuzzy_company_match(self):
        self.assertIsNone(identity.identity_error("互联通信有限公司","staff","互聯通信有限公司"))
        self.assertIsNone(identity.identity_error("深圳友联伟业科技有限公司销售二部","staff","友联伟业科技"))
        self.assertIsNotNone(identity.identity_error("恒佳有限公司","staff","恒佳达有限公司"))
        self.assertIsNone(identity.identity_error("深圳市金圳芯科技有限公司（原科讯达伟业）","staff","金圳芯科技"))

    def test_successful_launch_tab_does_not_steal_focus_before_send(self):
        row={"company":"fixture","qqs":[{"uin":"123456"}]};win=Mock();runner=Runner([]);runner.win=win
        task=Task(1,"LPC2387FBD100K",5000,"text");page,jump=Mock(),Mock()
        with patch.object(isolation,"ENABLED",True),patch.object(isolation,"MODE","worker"), \
             patch.object(isolation,"MANUAL",threading.Event()),patch.object(isolation,"DRAINING",threading.Event()), \
             patch.object(icnet,"find_row",return_value=(page,row)),patch.object(icnet,"launch_qq",return_value=jump), \
             patch.object(icnet,"settle_origin",return_value="admin.qidian.qq.com"),patch.object(icnet,"close_quietly") as close, \
             patch.object(qq,"require_assisted_foreground") as activate,patch.object(qq,"current_chat_name",return_value="previous"), \
             patch.object(qq,"wait_chat_change",return_value="new"),patch('lanhost.support.confirm_qq_dialog',return_value=False):
            self.assertEqual(runner.open_chat(task,row),"new")
        close.assert_not_called();activate.assert_not_called()

    def test_focus_fallback_operates_only_on_verified_dedicated_title_bar(self):
        win=Mock(NativeWindowHandle=123);win.SetFocus.side_effect=RuntimeError('unsupported')
        with patch.object(isolation,"ENABLED",True),patch.object(isolation,"MODE","worker"), \
             patch.object(isolation,"MANUAL",threading.Event()),patch.object(isolation,"DRAINING",threading.Event()), \
             patch.object(qq.win32gui,"SetWindowPos"),patch.object(qq.win32gui,"GetWindowRect",return_value=(100,100,1100,900)), \
             patch.object(qq,"_root_at",return_value=123),patch.object(qq.win32gui,"GetForegroundWindow",return_value=123), \
             patch.object(qq.auto,"Click") as click:
            self.assertTrue(isolation.activate_worker_window(win))
            click.assert_called_once()
            isolation.MODE="local"
            self.assertFalse(isolation.activate_worker_window(win))
            self.assertEqual(click.call_count,1)

    def test_known_unsent_deferral_does_not_consume_contact_quota_but_unknown_does(self):
        records=[{"status":"waiting_desktop","attempted_at":1,"sent_at":None},
                 {"status":"send_uncertain","attempted_at":1,"sent_at":None},
                 {"status":"sent","attempted_at":1,"sent_at":2}]
        self.assertEqual(live_trial.occupied(records),records[1:])

    def test_reply_count_uses_saved_messages_even_when_parser_classifies_other(self):
        from inquiry import db
        from ui import server
        task=db.create_task("fixture",5000)
        sid=db.add_supplier(task,{"company":"fixture","qqs":[{"uin":"12345"}]})
        db.update_supplier(sid,status="sent",sent_at=1)
        db.add_message(sid,"in","客服暂时不在线")
        db.add_message(sid,"in","请留言")
        summary=next(t for t in server.task_summaries() if t["id"]==task)
        self.assertEqual(summary["replied"],1)

    def test_virtual_offscreen_contacts_are_not_visible_or_clickable(self):
        from types import SimpleNamespace
        win=Mock(NativeWindowHandle=123);lst=Mock();a,b=Mock(),Mock()
        lst.BoundingRectangle=SimpleNamespace(left=0,top=50,right=400,bottom=600)
        a.ClassName=b.ClassName="recent-contact-item"
        a.BoundingRectangle=SimpleNamespace(left=0,top=80,right=400,bottom=160)
        b.BoundingRectangle=SimpleNamespace(left=0,top=800,right=400,bottom=880)
        lst.GetChildren.return_value=[a,b]
        info=Mock();info.GetChildren.return_value=[Mock(Name="visible")]
        def find(parent,name,**kwargs):
            if parent is win and name=="recent-contact-list":return lst
            if parent is a and name=="item__info":return info
            return None
        with patch.object(qq,"find",side_effect=find),patch.object(qq.win32gui,"IsIconic",return_value=False), \
             patch.object(qq.win32gui,"GetWindowRect",return_value=(0,0,1000,800)):
            self.assertEqual([c["name"] for c in qq.recent_contacts(win)],["visible"])

    def test_pacing_wait_can_read_without_processing_outbox(self):
        runner=Mock();runner.stop_requested.return_value=False
        with patch.object(live_trial.time,"monotonic",side_effect=[0,0,0,0,1,1]), \
             patch.object(live_trial.time,"sleep"),patch('inquiry.dispatch.process_one') as send:
            live_trial.idle_readonly(runner,.5)
        runner.poll_replies.assert_called_once_with(max_reads=1)
        send.assert_not_called()

    def test_reply_poll_rotates_to_never_read_chat_before_recently_read_prefix(self):
        import time
        runner=Runner([]);runner.win=Mock()
        runner.chat_owner={"old":[(Task(1,"fixture",5000,""),1)],"new":[(Task(2,"fixture2",5000,""),2)]}
        runner.last_attempt_at[1]=time.time()-60
        with patch.object(qq,"recent_contacts",return_value=[]),patch.object(runner,"read_chat",return_value=True) as read:
            runner.poll_replies(max_reads=1)
        self.assertEqual(read.call_args.args[0],"new")
