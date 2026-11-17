import http.client
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_live
from test_file_imports import export_file
from grouppulse.generation import GenerationError
from grouppulse.live import LiveApp
from grouppulse.server import create_server


class ReportJobTests(unittest.TestCase):
    def setUp(self):
        self.live=test_live.LiveTests(); self.live.setUp()
        self.app=self.live.app
        self.live.configure()
        self.item=self.app.file_imports.add(dict(filename='频道.json',content=json.dumps(export_file(self.live.rows))))
        self.request=dict(id='a'*32,source='account',request=dict(import_id=self.item['id'],server='100',channel='200',
                          date='2026-10-20',kind='member',member='901',timezone='Asia/Shanghai'))

    def tearDown(self):
        self.live.tearDown()

    def wait_done(self, job_id):
        for _ in range(200):
            state=self.app.report_jobs.get(job_id,'100','200')
            if state['status']!='running': return state
            time.sleep(.01)
        self.fail('后台任务未完成')

    def test_short_http_polling_duplicate_submit_and_restored_result_never_regenerate(self):
        server=create_server(self.app,0); threading.Thread(target=server.serve_forever,daemon=True).start()
        entered,release=threading.Event(),threading.Event()
        original=self.app.file_imports.report
        def delayed(data):
            entered.set(); release.wait(5)
            return original(data)
        def request(path,data=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            conn.request('GET' if data is None else 'POST',path,body=None if data is None else json.dumps(data),
                         headers={} if data is None else {'Content-Type':'application/json','X-GroupPulse':'local'})
            response=conn.getresponse(); result=response.status,json.loads(response.read()); conn.close()
            return result
        try:
            with patch.object(self.app.file_imports,'report',side_effect=delayed) as generate:
                status,queued=request('/api/report-jobs',self.request)
                self.assertEqual(status,200); self.assertEqual(queued['status'],'running')
                self.assertTrue(entered.wait(1))
                self.assertEqual(request('/api/report-jobs',self.request)[1]['id'],queued['id'])
                for _ in range(3):
                    status,state=request('/api/report-job?id='+queued['id']+'&server=100&channel=200')
                    self.assertEqual(status,200);self.assertEqual(state['status'],'running')
                    self.assertIn('elapsed_seconds',state); self.assertEqual(state['model_wait_seconds'],300)
                self.assertEqual(request('/api/report-job?id='+queued['id']+'&server=100&channel=other')[0],400)
                self.assertEqual(request('/api/report-jobs',dict(self.request,request=dict(self.request['request'],_synced=True)))[0],400)
                release.set(); result=self.wait_done(queued['id'])
                self.assertEqual(result['status'],'complete');self.assertEqual(result['result']['generation']['core'],'starmemo-original-js')
                self.assertEqual(request('/api/report-jobs',self.request)[1]['result']['report_id'],result['result']['report_id'])
                self.assertEqual(generate.call_count,1)
                restored=LiveApp(self.app.path,vault=self.live.vault)
                self.assertEqual(restored.report_jobs.get(queued['id'],'100','200')['result']['report_id'],result['result']['report_id'])
        finally:
            release.set();server.shutdown();server.server_close()

    def test_failed_unknown_request_and_interrupted_worker_are_not_retried(self):
        with patch.object(self.app.file_imports,'report',side_effect=GenerationError('请求结果未知。')) as generate:
            queued=self.app.report_jobs.queue(self.request)
            failed=self.wait_done(queued['id'])
            self.assertEqual(failed['status'],'failed');self.assertIn('结果未知',failed['error'])
            self.assertEqual(self.app.report_jobs.queue(self.request)['status'],'failed')
            self.assertEqual(generate.call_count,1)
        interrupted=dict(self.request,id='b'*32)
        entered,release=threading.Event(),threading.Event()
        def stopped(_data): entered.set();release.wait(5);raise GenerationError('已停止')
        with patch.object(self.app.file_imports,'report',side_effect=stopped) as generate:
            self.app.report_jobs.queue(interrupted);self.assertTrue(entered.wait(1))
            try:
                restored=LiveApp(self.app.path,vault=self.live.vault)
                state=restored.report_jobs.queue(interrupted)
                self.assertEqual(state['status'],'interrupted');self.assertIn('未自动重发',state['error'])
                self.assertEqual(generate.call_count,1)
            finally: release.set()
            self.wait_done(interrupted['id'])


if __name__=='__main__': unittest.main()
