"""Manual one-round collection: no timers, no auto reads after send, no QQ sends."""
import json
import threading
import uuid
from contextlib import nullcontext
from unittest.mock import patch, Mock
from test_reliability import IsolatedTest
from inquiry import collection, db, isolation, qq, dispatch
from inquiry.runner import Runner, Task
from ui import server


class ManualCollectionTest(IsolatedTest):
    def setUp(self):
        super().setUp()
        for obj,key,value in ((collection,'STATE_FILE',self.path/'collection.json'),(collection,'ACTIVITY',{'active':False}),
                              (isolation,'MANUAL',threading.Event()),(isolation,'DRAINING',threading.Event()),
                              (isolation,'ENABLED',True),(isolation,'MODE','worker')):
            p=patch.object(obj,key,value);p.start();self.addCleanup(p.stop)
        p=patch.object(qq.auto,'UIAutomationInitializerInThread',return_value=nullcontext());p.start();self.addCleanup(p.stop)

    def seed(self,n=3):
        sids=[]
        for _ in range(n):
            _,_,sid,_=self.fixture();sids.append(sid)
        return sids

    def test_idle_old_enabled_and_finished_round_do_not_even_open_qq(self):
        self.seed()
        collection.STATE_FILE.write_text(json.dumps({'enabled':True,'hours':72}),encoding='utf-8')
        with patch.object(qq,'ready_main_window') as ready,patch.object(Runner,'read_chat',return_value=True) as read:
            for _ in range(3):collection.process_round();collection.status()
            ready.assert_not_called();read.assert_not_called()
            collection.start(uuid.uuid4().hex);collection.process_round()
            self.assertEqual(read.call_count,3)
            self.assertEqual(collection.status()['phase'],'done')
            for _ in range(3):collection.process_round()
            self.assertEqual(read.call_count,3)

    def test_round_snapshot_excludes_suppliers_added_after_click(self):
        original=self.seed();rid=uuid.uuid4().hex
        collection.start(rid);self.seed(1)
        collection.start(rid)
        with patch.object(qq,'ready_main_window'),patch.object(Runner,'read_chat',return_value=True) as read,patch.object(dispatch,'process_one') as send:
            collection.process_round()
        self.assertEqual({c.args[2] for c in read.call_args_list},set(original))
        send.assert_not_called()

    def test_pause_cancels_remaining_without_pausing_inquiry_queue(self):
        self.seed();collection.start(uuid.uuid4().hex)
        def interrupt(*args):
            collection.pause()
            return False
        with patch.object(qq,'ready_main_window'),patch.object(Runner,'read_chat',side_effect=interrupt) as read:
            collection.process_round();collection.process_round()
        self.assertEqual(read.call_count,1)
        self.assertEqual(collection.status()['phase'],'paused')
        self.assertFalse(isolation.MANUAL.is_set())
        self.assertFalse(server.STOP_FILE.exists())

    def test_cancel_before_execution_touches_no_qq(self):
        self.seed();collection.start(uuid.uuid4().hex);collection.pause()
        with patch.object(qq,'ready_main_window') as ready:collection.process_round();ready.assert_not_called()

    def test_each_failed_supplier_is_tried_once_and_reported(self):
        self.seed();collection.start(uuid.uuid4().hex)
        with patch.object(qq,'ready_main_window'),patch.object(Runner,'read_chat',return_value=False) as read:
            collection.process_round();collection.process_round()
        s=collection.status();self.assertEqual((s['phase'],s['processed'],s['failed']),('done',3,3))
        self.assertEqual(read.call_count,3)

    def test_restart_never_resumes_a_requested_round_without_new_click(self):
        self.seed();collection.start(uuid.uuid4().hex);collection.recover()
        with patch.object(qq,'ready_main_window') as ready:collection.process_round();ready.assert_not_called()
        self.assertEqual(collection.status()['phase'],'interrupted')

    def test_scoped_round_collects_only_selected_task(self):
        self.seed();record=collection.records()[0]
        collection.start(uuid.uuid4().hex,[record['task_id']])
        with patch.object(qq,'ready_main_window'),patch.object(Runner,'read_chat',return_value=True) as read:collection.process_round()
        self.assertEqual([c.args[2] for c in read.call_args_list],[record['id']])

    def test_send_only_idle_and_completion_do_not_read_replies(self):
        runner,task,sid,_=self.fixture();runner.send_only=True;runner.cfg['max_suppliers']=1;task.sent=1
        with patch.object(runner,'poll_replies') as poll,patch.object(runner,'sweep') as sweep,patch.object(dispatch,'process_one'):
            runner.idle(.01)
            self.assertTrue(runner.check_done(task))
            poll.assert_not_called();sweep.assert_not_called()

    def test_collection_api_works_while_real_send_is_locked_and_does_not_queue_replies(self):
        self.seed()
        client=server.app.test_client()
        with patch.object(isolation,'ALLOW_SEND',False),patch.object(qq,'send_text') as send:
            response=client.post('/api/collection',json={'action':'start','request_id':uuid.uuid4().hex})
            self.assertEqual(response.status_code,202)
            self.assertEqual(client.post('/api/collection',json={'action':'pause'}).status_code,200)
            self.assertEqual(client.post('/api/collection',json={'enabled':True}).status_code,400)
            send.assert_not_called()
        with db.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)

    def test_offscreen_qq_is_fitted_only_on_verified_dedicated_worker(self):
        win=Mock(NativeWindowHandle=123)
        with patch.object(qq.win32process,'GetWindowThreadProcessId',return_value=(1,2)),patch.object(qq,'exe_of',return_value=r'D:\QQ\QQ.exe'),patch.object(qq.win32api,'MonitorFromWindow',return_value=1),patch.object(qq.win32api,'GetMonitorInfo',return_value={'Work':(0,0,2560,1368)}),patch.object(qq.win32gui,'GetWindowRect',return_value=(554,230,2301,1506)),patch.object(qq.win32gui,'SetWindowPos') as resize:
            isolation.fit_worker_window(win)
            args=resize.call_args.args
            self.assertLessEqual(args[3]+args[5],1368)
            resize.reset_mock()
            isolation.MODE='local'
            isolation.fit_worker_window(win);resize.assert_not_called()
