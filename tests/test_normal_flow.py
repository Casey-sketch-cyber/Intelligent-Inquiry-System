"""Normal paired inquiries: durable submission, recovery and bounded collection; no real QQ input."""
import json
import time
import uuid
from unittest.mock import patch, Mock
import test_lan
from inquiry import db, jobs, collection, isolation, qq, icnet
from inquiry.runner import Runner, Task
from ui import server
from lanhost import config, worker


class NormalFlowTest(test_lan.LanTest):
    def setUp(self):
        super().setUp()
        p = patch.object(collection, 'STATE_FILE', self.path/'collection.json')
        p.start(); self.addCleanup(p.stop)
        self.body = {'request_id':uuid.uuid4().hex,'parts':[{'part':'LPC2387FBD100K','qty':5000}], 'max':3,'dry_run':False}
        self.settings = {'inquiry':{'template':'您好，{part}，{qty}pcs，请报价','send_card':False},'pacing':{},'filter':{'max_pages':1}}

    def test_normal_tls_submission_creates_one_batch_and_duplicate_is_idempotent(self):
        isolation.ENABLED, isolation.MODE, isolation.ALLOW_SEND = True, 'worker', True
        remote,_,_ = self.start_rpc(worker.handler(self.cfg))
        with patch.object(server,'load_settings',return_value=self.settings),patch.object(server,'spawn') as spawn:
            for _ in range(2):
                code,_,raw=remote.call('POST','/api/run',json.dumps(self.body).encode(),'application/json')
                self.assertEqual(code,202)
                self.assertEqual(json.loads(raw)['task_ids'],[1])
            changed=self.body|{'max':4}
            self.assertEqual(remote.call('POST','/api/run',json.dumps(changed).encode(),'application/json')[0],400)
            spawn.assert_not_called()
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM task').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM send_attempt').fetchone()[0],0)

    def test_send_permission_persists_only_for_same_pair_and_office_cannot_enable(self):
        isolation.ENABLED,isolation.MODE=True,'worker'
        isolation.MANUAL.set()
        with patch.object(server,'run_state',return_value={'running':False}):
            config.set_sending(self.cfg,True)
        self.assertTrue(config.sending_enabled(self.cfg))
        self.assertFalse(config.sending_enabled(self.cfg|{'worker_id':uuid.uuid4().hex}))
        config.save_json(config.WORKER,self.cfg)
        with patch.object(config,'device_identity',return_value=self.cfg['worker_device']):
            isolation.enable_worker()
            self.assertTrue(isolation.ALLOW_SEND)
        with patch.object(config,'device_identity',return_value=self.office['office_device']):
            with self.assertRaises(RuntimeError):isolation.enable_worker()
        isolation.ENABLED,isolation.MODE=False,'local'
        with self.assertRaises(ValueError):config.set_sending(self.cfg,True)

    def test_captcha_waits_without_spawning_and_releases_when_user_completed(self):
        job=jobs.submit(self.body,self.settings)
        isolation.ALLOW_SEND=True
        with patch.object(server,'run_state',return_value={'running':False}),patch.object(server,'spawn') as spawn:
            with patch.object(server,'browser_status',return_value={'code':'verification'}):
                jobs.dispatch_one()
                self.assertEqual(jobs.get(job['id'])['status'],'waiting_verification')
                spawn.assert_not_called()
            with patch.object(server,'browser_status',return_value={'code':'ok'}):
                jobs.dispatch_one()
                self.assertEqual(jobs.get(job['id'])['status'],'queued')
                self.assertEqual(spawn.call_args.kwargs['module'],'inquiry.jobs')

    def test_restarted_in_progress_job_requires_review_and_uncertain_cannot_resume(self):
        job=jobs.submit(self.body,self.settings)
        with db.connect() as c:c.execute("UPDATE inquiry_job SET status='running'")
        jobs.recover()
        self.assertEqual(jobs.get(job['id'])['status'],'interrupted')
        sid=db.add_supplier(job['task_ids'][0],{'company':'测试公司','qqs':[{'uin':'12345'}]})
        db.update_supplier(sid,status='send_uncertain')
        with self.assertRaises(ValueError):jobs.resume(job['id'])

    def test_collector_lock_race_does_not_lose_job(self):
        job=jobs.submit(self.body,self.settings)
        with patch('ui.service.acquire_lock',return_value=None):jobs.execute(job['id'])
        self.assertEqual(jobs.get(job['id'])['status'],'queued')

    def test_cancelled_waiting_batch_never_runs_after_verification(self):
        job=jobs.submit(self.body,self.settings);jobs.cancel(job['id'])
        isolation.ALLOW_SEND=True
        with patch.object(server,'run_state',return_value={'running':False}),patch.object(server,'browser_status',return_value={'code':'ok'}),patch.object(server,'spawn') as spawn:
            jobs.dispatch_one();spawn.assert_not_called()

    def test_job_execution_reuses_created_tasks_and_marks_dry_run_finished(self):
        job=jobs.submit(self.body|{'dry_run':True},self.settings)
        def fake_run(runner):
            runner.make_tasks()
            self.assertEqual([t.id for t in runner.tasks],job['task_ids'])
            db.finish_task(runner.tasks[0].id,'dry-run')
        with patch.object(Runner,'run',fake_run):jobs.execute(job['id'])
        self.assertEqual(jobs.get(job['id'])['status'],'done')
        with db.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM task').fetchone()[0],1)

    def test_new_search_verification_preserves_batch_for_later_and_does_not_recreate_tasks(self):
        job=jobs.submit(self.body|{'dry_run':True},self.settings)
        with patch.object(Runner,'run',side_effect=icnet.NotLoggedIn('需要人工校验')):jobs.execute(job['id'])
        self.assertEqual(jobs.get(job['id'])['status'],'waiting_verification')
        jobs.resume(job['id'])
        self.assertEqual(jobs.get(job['id'])['task_ids'],job['task_ids'])

    def test_collection_requires_click_and_includes_old_bound_inquiries(self):
        _,task,sid,_=self.fixture()
        db.update_supplier(sid,sent_at=time.time()-3*3600)
        with db.connect() as c:c.execute("UPDATE message SET seen_at=?",(time.time()-3*3600,))
        self.assertEqual([s['id'] for s in collection.records()],[sid])
        self.assertFalse(collection.status()['pending'])
        collection.start(uuid.uuid4().hex)
        collection.pause()
        self.assertEqual(collection.status()['phase'],'paused')
        self.assertFalse(server.STOP_FILE.exists())

    def test_send_cap_ends_sending_without_waiting_for_quotes(self):
        runner,task,sid,_=self.fixture()
        runner.send_only=True;runner.cfg['max_suppliers']=3;task.sent=3
        with patch.object(runner,'sweep') as sweep:
            self.assertTrue(runner.check_done(task))
            sweep.assert_not_called()
        with db.connect() as c:self.assertEqual(c.execute('SELECT status FROM task WHERE id=?',(task.id,)).fetchone()[0],'done')

    def test_exception_after_send_invocation_is_uncertain_not_retryable_failure(self):
        runner=Runner([]);runner.win=Mock();task=Task(db.create_task('TEST',5000),'TEST',5000,'TEST 5000')
        row={'company':'测试公司','qqs':[{'uin':'12345'}]}
        with patch.object(runner,'wait_rate_limit',return_value=True),patch.object(runner,'open_chat',return_value='chat'),patch.object(qq,'wait_layout_stable'),patch.object(qq,'verify_supplier'),patch.object(qq,'read_messages',return_value=[]),patch.object(qq,'send_text',side_effect=RuntimeError('after invocation')),patch.object(runner,'cleanup_draft'):
            self.assertFalse(runner.inquire(task,row))
        self.assertTrue(runner._stop)
        self.assertEqual(db.suppliers(task.id)[0]['status'],'send_uncertain')

    def test_same_qq_under_different_company_cannot_receive_same_model_twice(self):
        runner,task,sid,_=self.fixture()
        row={'company':'另一个公司','qqs':[{'uin':'12345'}]}
        with patch.object(runner,'open_chat') as open_chat:
            self.assertFalse(runner.inquire(task,row))
            open_chat.assert_not_called()

    def test_pause_between_spawn_and_child_start_keeps_job_queued(self):
        job=jobs.submit(self.body,self.settings)
        server.STOP_FILE.write_text('stop',encoding='utf-8')
        with patch.object(Runner,'run') as run:jobs.execute(job['id']);run.assert_not_called()
        self.assertEqual(jobs.get(job['id'])['status'],'queued')

    def test_normal_batch_dispatches_three_per_model_and_finishes_send_phase(self):
        from collections import deque
        from contextlib import nullcontext
        parts=[{'part':p,'qty':5000} for p in ('LPC2387FBD100K','LPC1788FBD208K','LPC2366FBD100K')]
        job=jobs.submit(self.body|{'parts':parts},self.settings)
        observed=[]
        def load(runner,context,task):
            task.pages_loaded=1
            task.candidates=deque({'company':f'{task.part_no}-{i}','qqs':[{'uin':str(10000+task.id*10+i)}]} for i in range(4))
            return True
        def inquire(runner,task,row):
            sid=db.add_supplier(task.id,row)
            db.update_supplier(sid,sent_at=time.time(),status='sent')
            observed.append((task.part_no,task.qty,row['company']))
            task.sent+=1
            return True
        with patch('inquiry.runner.sync_playwright',return_value=nullcontext(Mock())),patch('inquiry.browser.connect',return_value=(None,Mock())),patch.object(Runner,'wait_for_qq',return_value=True),patch.object(Runner,'load_candidates',load),patch.object(Runner,'inquire',inquire),patch.object(Runner,'sweep') as sweep,patch.object(Runner,'poll_replies') as poll,patch.object(Runner,'idle'),patch('inquiry.dispatch.process_one'),patch('inquiry.report.build',return_value='fixture'):
            jobs.execute(job['id'])
            sweep.assert_not_called();poll.assert_not_called()
        self.assertEqual(len(observed),9)
        self.assertEqual(len(set(observed)),9)
        self.assertTrue(all(sum(x[0]==p['part'] for x in observed)==3 for p in parts))
        self.assertEqual(jobs.get(job['id'])['status'],'done')

    def test_source_update_can_verify_live_mode_but_history_reset_still_requires_readonly(self):
        import threading
        from ui import service
        from lanhost import reset_history
        root=self.path/'ui-worker'
        local_state={'worker_id':self.cfg['worker_id'],'allow_send':True,'manual':True,'manual_ready':True,'running':False}
        with patch.object(worker,'state',return_value=local_state),patch.object(config,'local_addresses',return_value=[]):
            httpd=service.bind_server(worker.local_app(self.cfg,self.path/'connection.json',8778),0)
            threading.Thread(target=httpd.serve_forever,daemon=True).start()
            try:
                config.save_json(root/'data/lan/worker-ui.json',{'url':f'http://127.0.0.1:{httpd.port}/','port':8778})
                _,_,call=reset_history.worker_ui(root,self.cfg,allow_sending=True)
                self.assertTrue(call('api/status')['allow_send'])
                _,_,call=reset_history.worker_ui(root,self.cfg)
                with self.assertRaises(RuntimeError):call('api/status')
            finally:
                httpd.shutdown();httpd.server_close()
