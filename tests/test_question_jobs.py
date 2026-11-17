import http.client
import json
import threading
import time
import unittest
from unittest.mock import patch

from grouppulse.account_data import AccountApp
from grouppulse.generation import GenerationError
from grouppulse.server import create_server
import test_account_data
import test_reference_questions


class QuestionJobTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_reference_questions.ReferenceQuestionTests();self.fixture.setUp()
        self.app=self.fixture.app
        self.scope=dict(server='s',channel='c')
        self.request=dict(id='a'*32,request=dict(self.scope,question='活动怎么安排？',
            start_date='2026-10-20',end_date='2026-10-20',conversation_id=''))

    def tearDown(self): self.fixture.tearDown()

    def done(self, jobs, scope, job_id='a'*32):
        for _ in range(200):
            state=jobs.get(job_id,scope)
            if state['status']!='running': return state
            time.sleep(.01)
        self.fail('问答后台未完成')

    def test_short_http_polling_refresh_and_restart_reuse_the_original_answer(self):
        server=create_server(self.app,0);threading.Thread(target=server.serve_forever,daemon=True).start()
        entered,release=threading.Event(),threading.Event()
        original=self.app.ask
        def delayed(**data): entered.set();release.wait(5);return original(**data)
        def request(path,data=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            conn.request('GET' if data is None else 'POST',path,None if data is None else json.dumps(data),
                {} if data is None else {'Content-Type':'application/json','X-GroupPulse':'local'})
            response=conn.getresponse();value=(response.status,json.loads(response.read()));conn.close();return value
        try:
            with patch.object(self.app,'ask',side_effect=delayed) as ask:
                code,queued=request('/api/question-jobs',self.request)
                self.assertEqual(code,200);self.assertEqual(queued['status'],'running')
                self.assertTrue(entered.wait(1))
                self.assertEqual(request('/api/question-jobs',self.request)[1]['id'],queued['id'])
                for _ in range(3):
                    code,state=request('/api/question-job?id='+queued['id']+'&server=s&channel=c')
                    self.assertEqual(code,200);self.assertEqual(state['status'],'running')
                    self.assertIn('elapsed_seconds',state)
                self.assertEqual(request('/api/question-job?id='+queued['id']+'&server=s&channel=d')[0],400)
                changed=dict(self.request,request=dict(self.request['request'],question='改成另一个问题'))
                self.assertEqual(request('/api/question-jobs',changed)[0],400)
                release.set();state=self.done(self.app.question_jobs,self.scope)
                self.assertEqual(state['status'],'complete')
                self.assertEqual(state['result']['generation']['runtime'],'copied-starmemo-query-agent')
                self.assertEqual(request('/api/question-jobs',self.request)[1]['result'],state['result'])
                self.assertEqual(ask.call_count,1)
                restored=self.fixture.new_app()
                self.assertEqual(restored.question_jobs.get(queued['id'],self.scope)['result'],state['result'])
                history=restored.conversation(state['result']['conversation_id'],'s','c')
                self.assertEqual(len(history['turns']),1)
        finally:
            release.set();server.shutdown();server.server_close()

    def test_unknown_failures_and_interrupted_workers_are_not_automatically_reissued(self):
        with patch.object(self.app,'ask',side_effect=GenerationError('结果未知')) as ask:
            self.app.question_jobs.queue(self.request)
            self.assertEqual(self.done(self.app.question_jobs,self.scope)['status'],'failed')
            self.assertEqual(self.app.question_jobs.queue(self.request)['status'],'failed')
            self.assertEqual(ask.call_count,1)
        entered,release=threading.Event(),threading.Event()
        def stopped(**_data): entered.set();release.wait(5);raise GenerationError('已停止')
        later=dict(self.request,id='b'*32)
        with patch.object(self.app,'ask',side_effect=stopped) as ask:
            self.app.question_jobs.queue(later);self.assertTrue(entered.wait(1))
            try:
                restored=self.fixture.new_app()
                state=restored.question_jobs.queue(later)
                self.assertEqual(state['status'],'interrupted')
                self.assertIn('未自动重发',state['error']);self.assertEqual(ask.call_count,1)
            finally: release.set()
            self.assertEqual(self.done(restored.question_jobs,self.scope,'b'*32)['status'],'interrupted')

    def test_invalid_question_and_forged_bot_identity_are_rejected_before_execution(self):
        with patch.object(self.app,'ask') as ask:
            for changes in [dict(question=' '),dict(start_date='invalid'),dict(end_date='2026-10-19'),
                            dict(account_id='900'),dict(timezone='UTC'),dict(_synced=True)]:
                with self.assertRaises(ValueError):
                    self.app.question_jobs.queue(dict(self.request,request=dict(self.request['request'],**changes)))
            ask.assert_not_called()

    def test_personal_jobs_and_answers_stay_in_the_original_account_channel_store(self):
        fixture=test_account_data.AccountDataTests();fixture.setUp()
        scope=dict(source='account',account_id='900',server='100',channel='200',timezone='Asia/Shanghai')
        request=dict(id='c'*32,request=dict(scope,question='活动有哪些意见？',start_date='2026-10-20',end_date='2026-10-20',conversation_id=''))
        try:
            with patch.object(AccountApp,'ensure_synced',return_value=None):
                fixture.app.question_jobs.queue(request)
                state=self.done(fixture.app.question_jobs,scope,'c'*32)
            self.assertEqual(state['status'],'complete')
            self.assertEqual(state['result']['scope']['account_id'],'900')
            with fixture.app.db() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM question_turns').fetchone()[0],0)
                self.assertEqual(db.execute("SELECT count(*) FROM live_meta WHERE key LIKE 'question-job:%'").fetchone()[0],0)
            for other in [dict(scope,account_id='901'),dict(scope,channel='201'),dict(scope,timezone='UTC'),dict(server='100',channel='200')]:
                with self.assertRaises(ValueError):fixture.app.question_jobs.get('c'*32,other)
            fixture.connect('901')
            with self.assertRaises(ValueError):fixture.app.question_jobs.get('c'*32,scope)
        finally: fixture.tearDown()


if __name__=='__main__': unittest.main()
