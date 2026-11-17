from datetime import datetime
import http.client
import json
import threading
from unittest.mock import patch

import test_live as live_fixture
from grouppulse.core import TZ
from grouppulse.live import LiveApp
from grouppulse.server import create_server
from grouppulse.network import ServiceError


class DailyTaskTests(live_fixture.LiveTests):
    # Reuse the existing HTTP Discord/model fixture without rerunning its tests.
    __unittest_skip__ = False

    def prepare_task(self):
        self.configure()
        self.app.ensure_synced('100', '200', '2026-10-20', '2026-10-20')
        self.app.daily_tasks.clock = lambda: datetime(2026, 10, 20, 18, tzinfo=TZ)
        return self.app.daily_tasks.save(dict(server='100', channel='200', members=['901', '902'], enabled=True))

    def tearDown(self):
        if hasattr(self.app, 'daily_tasks'):
            self.app.daily_tasks.stop()
        super().tearDown()

    def test_fixed_members_persist_and_follow_channel_not_page(self):
        task = self.prepare_task()
        self.assertEqual(task['members'], ['901', '902'])
        self.assertEqual(task['run_time'], '00:10')
        self.assertEqual(task['next_day'], '2026-10-20')
        restored = LiveApp(self.path, vault=self.vault, discord_factory=self.app.discord_factory)
        self.assertEqual(restored.daily_tasks.state('100', '200')['task']['members'], ['901', '902'])
        self.assertFalse(restored.daily_tasks.state('100', '200')['runs'])
        with self.assertRaises(ValueError):
            self.app.daily_tasks.save(dict(server='100', channel='200', members=['foreign'], enabled=True))
        with self.assertRaises(ValueError):
            self.app.daily_tasks.save(dict(server='100', channel='201', members=['901'], enabled=True))

    def test_complete_day_runs_original_reports_once_and_survives_restart(self):
        self.prepare_task()
        calls = self.app.model.calls
        self.assertFalse(self.app.daily_tasks.tick(datetime(2026, 10, 21, 0, 9, tzinfo=TZ)))
        self.assertEqual(self.app.model.calls, calls)
        self.assertTrue(self.app.daily_tasks.tick(datetime(2026, 10, 21, 0, 10, tzinfo=TZ)))
        state = self.app.daily_tasks.state('100', '200')
        run = state['runs'][0]
        self.assertEqual(run['date'], '2026-10-20')
        self.assertEqual(run['status'], 'complete')
        self.assertEqual(self.app.model.calls - calls, 3)
        for step in run['steps']:
            report = self.app.daily_tasks.report('100', '200', step['report_id'])
            self.assertEqual(report['generation']['core'], 'starmemo-original-js')
            self.assertTrue(report['coverage']['window_complete'])
            if step['member']:
                self.assertTrue(all(row['author_id'] == step['member'] for row in report['messages']))
        again = self.app.model.calls
        self.app.daily_tasks.clock = lambda: datetime(2026, 10, 21, 1, tzinfo=TZ)
        self.app.daily_tasks.queue(dict(server='100', channel='200', date='2026-10-20'))
        self.app.daily_tasks.tick(datetime(2026, 10, 21, 1, tzinfo=TZ))
        self.assertEqual(self.app.model.calls, again)
        restored = LiveApp(self.path, vault=self.vault, discord_factory=self.app.discord_factory)
        restored.daily_tasks.recover()
        self.assertFalse(restored.daily_tasks.tick(datetime(2026, 10, 21, 1, tzinfo=TZ)))
        self.assertEqual(restored.daily_tasks.state('100', '200')['runs'][0]['status'], 'complete')
        self.assertTrue(all(method == 'GET' or path == '/v1/chat/completions' for method, path in self.calls))

    def test_failed_step_does_not_repeat_completed_daily_or_other_members(self):
        self.prepare_task()
        original = self.app.report

        def fail_member(**data):
            if data.get('member') == '901':
                raise ValueError('测试：模型设置待检查。')
            return original(**data)

        with patch.object(self.app, 'report', side_effect=fail_member):
            self.app.daily_tasks.tick(datetime(2026, 10, 21, 0, 10, tzinfo=TZ))
        run = self.app.daily_tasks.state('100', '200')['runs'][0]
        self.assertEqual(run['status'], 'needs_attention')
        completed = run['steps'][0]['report_id']
        calls = self.app.model.calls
        self.assertFalse(self.app.daily_tasks.tick(datetime(2026, 10, 21, 2, tzinfo=TZ)))
        self.assertEqual(self.app.model.calls, calls)
        self.app.daily_tasks.clock = lambda: datetime(2026, 10, 21, 2, tzinfo=TZ)
        self.app.daily_tasks.queue(dict(server='100', channel='200', date=run['date'], retry=True))
        self.app.daily_tasks.tick(datetime(2026, 10, 21, 2, tzinfo=TZ))
        run = self.app.daily_tasks.state('100', '200')['runs'][0]
        self.assertEqual(run['status'], 'complete')
        self.assertEqual(run['steps'][0]['report_id'], completed)
        self.assertEqual(self.app.model.calls - calls, 2)

    def test_interrupted_unknown_call_requires_explicit_retry(self):
        self.prepare_task()
        self.app.daily_tasks.clock = lambda: datetime(2026, 10, 21, 1, tzinfo=TZ)
        run = self.app.daily_tasks.queue(dict(server='100', channel='200', date='2026-10-20'))
        run['status'] = 'running'
        run['steps'][0]['status'] = 'running'
        self.app.daily_tasks.write_run(run)
        self.app.daily_tasks.recover()
        run = self.app.daily_tasks.state('100', '200')['runs'][0]
        self.assertEqual(run['status'], 'needs_attention')
        self.assertEqual(run['steps'][0]['status'], 'uncertain')
        with self.assertRaises(ValueError):
            self.app.daily_tasks.queue(dict(server='100', channel='200', date=run['date']))
        self.assertEqual(self.app.model.calls, 0)

    def test_snapshot_written_before_interrupt_is_recovered_without_new_model_call(self):
        self.prepare_task()
        self.app.daily_tasks.clock = lambda: datetime(2026, 10, 21, 1, tzinfo=TZ)
        run = self.app.daily_tasks.queue(dict(server='100', channel='200', date='2026-10-20'))
        step = run['steps'][0]
        run['status'] = 'running'; step['status'] = 'running'
        self.app.daily_tasks.write_run(run)
        self.app.report(server='100', channel='200', date=run['date'], _snapshot_id=step['report_id'])
        calls = self.app.model.calls
        self.app.daily_tasks.recover()
        self.app.daily_tasks.tick(datetime(2026, 10, 21, 0, 10, tzinfo=TZ))
        self.assertEqual(self.app.model.calls - calls, 2)
        self.assertEqual(self.app.daily_tasks.state('100', '200')['runs'][0]['status'], 'complete')

    def test_sleep_backlog_empty_day_and_paused_days(self):
        self.prepare_task()
        self.app.daily_tasks.tick(datetime(2026, 10, 22, 1, tzinfo=TZ))
        self.app.daily_tasks.tick(datetime(2026, 10, 22, 1, tzinfo=TZ))
        state = self.app.daily_tasks.state('100', '200')
        self.assertEqual([row['date'] for row in state['runs']], ['2026-10-21', '2026-10-20'])
        self.assertTrue(all(row['status'] == 'complete' for row in state['runs']))
        self.assertEqual(self.app.model.calls, 3)  # Empty day is saved without a model call.
        self.app.daily_tasks.clock = lambda: datetime(2026, 10, 22, 12, tzinfo=TZ)
        self.app.daily_tasks.save(dict(server='100', channel='200', members=['901', '902'], enabled=False))
        self.assertFalse(self.app.daily_tasks.tick(datetime(2026, 10, 25, 1, tzinfo=TZ)))
        self.app.daily_tasks.clock = lambda: datetime(2026, 10, 25, 12, tzinfo=TZ)
        task = self.app.daily_tasks.save(dict(server='100', channel='200', members=['901', '902'], enabled=True))
        self.assertEqual(task['next_day'], '2026-10-25')

    def test_http_tasks_sources_and_history_do_not_call_model_when_viewed(self):
        self.prepare_task()
        self.app.daily_tasks.clock = lambda: datetime(2026, 10, 21, 1, tzinfo=TZ)
        server = create_server(self.app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, data=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request('POST' if data is not None else 'GET', path,
                               json.dumps(data) if data is not None else None,
                               headers={'Content-Type':'application/json', 'X-GroupPulse':'local'})
            response = connection.getresponse()
            value = json.loads(response.read())
            connection.close()
            return response.status, value
        try:
            self.assertEqual(request('/api/daily-task?server=100&channel=201')[0], 400)
            self.assertEqual(request('/api/daily-task',dict(server='100',channel='200',members=['901'],secret='not-an-allowed-field'))[0],400)
            self.assertEqual(request('/api/daily-task/run',dict(server='100',channel='200',date='2026-10-21'))[0],400)
            self.assertEqual(request('/api/daily-task/run',dict(server='100',channel='200',date='2026-10-20'))[0],200)
            self.app.daily_tasks.tick()
            calls = self.app.model.calls
            state = request('/api/daily-task?server=100&channel=200')[1]
            identifier = state['runs'][0]['steps'][1]['report_id']
            result = request('/api/daily-report?server=100&channel=200&id='+identifier)
            self.assertEqual(result[0],200)
            self.assertTrue(all(m['author_id']=='901' for m in result[1]['messages']))
            self.assertEqual(request('/api/daily-report?server=100&channel=201&id='+identifier)[0],400)
            self.assertEqual(self.app.model.calls,calls)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_worker_is_exclusive_and_coverage_failure_never_calls_model(self):
        self.prepare_task()
        self.app.daily_tasks.worker.acquire()
        try:
            self.assertFalse(self.app.daily_tasks.tick(datetime(2026,10,21,1,tzinfo=TZ)))
            self.app.daily_tasks.clock=lambda:datetime(2026,10,21,1,tzinfo=TZ)
            # Queuing must remain responsive while another run is working.
            self.app.daily_tasks.queue(dict(server='100',channel='200'))
        finally:
            self.app.daily_tasks.worker.release()
        self.history_failure=True
        self.app.daily_tasks.tick()
        self.assertEqual(self.app.model.calls,0)
        self.assertEqual(self.app.daily_tasks.state('100','200')['runs'][0]['status'],'needs_attention')

    def test_coverage_waits_for_running_sync_instead_of_saving_a_false_gap(self):
        self.prepare_task()
        start,end=self.app.window('2026-10-20')
        finished=self.app.sync_window_state('200','2026-10-20','2026-10-20')
        running={**finished,'status':'running','threads_complete':False}
        completed=threading.Event()
        result=[]
        def inspect():
            result.append(self.app.coverage('200',start,end,[]))
            completed.set()
        thread=threading.Thread(target=inspect)
        try:
            with self.app.sync_lock:
                with self.app.db() as db:
                    db.execute('UPDATE sync_windows SET status=?,body=? WHERE channel=?',('running',json.dumps(running),'200'))
                thread.start()
                waited=not completed.wait(.05)
                with self.app.db() as db:
                    db.execute('UPDATE sync_windows SET status=?,body=? WHERE channel=?',('complete',json.dumps(finished),'200'))
            thread.join(timeout=5)
            self.assertTrue(waited,'覆盖检查不应使用同步过程中的临时状态。')
            self.assertEqual(result[0]['status'],'complete')
            self.assertTrue(result[0]['window_complete'])
        finally:
            thread.join(timeout=5)

    def test_read_only_network_failure_automatically_resumes_without_model_retry(self):
        self.prepare_task()
        self.app.daily_tasks.clock=lambda:datetime(2026,10,21,1,tzinfo=TZ)
        with patch.object(self.app,'ensure_synced',side_effect=ServiceError('暂时离线。','connection')):
            self.app.daily_tasks.tick()
        run=self.app.daily_tasks.state('100','200')['runs'][0]
        self.assertEqual(run['status'],'pending')
        self.assertEqual(self.app.model.calls,0)
        self.assertFalse(self.app.daily_tasks.tick(datetime(2026,10,21,1,1,tzinfo=TZ)))
        self.app.daily_tasks.clock=lambda:datetime(2026,10,21,1,6,tzinfo=TZ)
        self.app.daily_tasks.tick()
        self.assertEqual(self.app.daily_tasks.state('100','200')['runs'][0]['status'],'complete')
        self.assertEqual(self.app.model.calls,3)

    def test_page_selection_does_not_rebind_enabled_task(self):
        self.prepare_task()
        catalog=self.app.meta('catalog')
        catalog['servers']['100']['channels']['203']='另一个频道'
        self.app.save_meta('catalog',catalog)
        self.app.reload_catalog()
        self.app.bootstrap('100','203')
        self.assertIsNone(self.app.daily_tasks.state('100','203')['task'])
        self.app.daily_tasks.tick(datetime(2026,10,21,1,tzinfo=TZ))
        run=self.app.daily_tasks.state('100','200')['runs'][0]
        self.assertEqual(run['channel'],'200')
        self.assertEqual(run['status'],'complete')
        self.assertEqual(self.app.meta('selection')['channel'],'203')


# Only inherit fixture helpers, not the existing suite's test methods.
for _name in dir(live_fixture.LiveTests):
    if _name.startswith('test_') and _name not in DailyTaskTests.__dict__:
        setattr(DailyTaskTests, _name, None)
