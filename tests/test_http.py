import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from grouppulse.core import App
from grouppulse.server import create_server


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Path(self.tmp.name) / 'demo.sqlite3')
        self.app.import_demo()
        self.server = create_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        values = {'Content-Type': 'application/json', 'X-GroupPulse': 'offline'}
        values.update(headers or {})
        connection.request('POST' if body is not None or raw is not None else 'GET', path,
                           body=raw if raw is not None else (json.dumps(body) if body is not None else None),
                           headers=values)
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_pages_and_runtime_is_private(self):
        status, page, headers = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn('拾聊'.encode(), page)
        self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.request('/runtime/offline.sqlite3')[0], 404)
        self.assertEqual(self.request('/../AGENTS.md')[0], 404)

    def test_untrusted_origin_host_and_json(self):
        self.assertEqual(self.request('/api/config', {}, {'Origin': 'https://other.invalid'})[0], 403)
        self.assertEqual(self.request('/api/bootstrap', headers={'Host': 'other.invalid'})[0], 403)
        self.assertEqual(self.request('/api/demo', {}, {'X-GroupPulse': ''})[0], 403)
        self.assertEqual(self.request('/api/demo', raw='{not-json')[0], 400)
        self.assertEqual(self.request('/api/config', [1, 2])[0], 400)

    def test_config_errors_are_actionable_and_secret_is_not_echoed(self):
        status, body, _ = self.request('/api/config', {'base_url': 'wrong', 'model': 'demo'})
        self.assertEqual(status, 400)
        self.assertIn('服务地址', json.loads(body)['error'])
        config = {'base_url': 'https://example.invalid/v1', 'model': 'demo', 'api_key': 'demo-http-secret'}
        status, body, _ = self.request('/api/config', config)
        self.assertEqual(status, 200)
        self.assertNotIn(b'demo-http-secret', body)
        self.assertNotIn(b'demo-http-secret', self.request('/api/bootstrap')[1])
        result = json.loads(self.request('/api/config/check', config)[1])
        self.assertEqual(result['status'], 'offline_valid')

    def test_report_and_reimport(self):
        result = json.loads(self.request('/api/report?kind=member&member=u1')[1])
        self.assertEqual(result['selected_count'], 3)
        self.assertEqual(self.request('/api/demo', {})[0], 200)
        again = json.loads(self.request('/api/report')[1])
        self.assertEqual(again['metrics']['messages'], 8)
        self.assertEqual(self.request('/api/report?date=bad')[0], 400)
        second = json.loads(self.request('/api/report?server=builders&channel=dev')[1])
        self.assertEqual(second['metrics']['messages'], 3)
        self.assertEqual(self.request('/api/report?server=builders&channel=lobby')[0], 400)
        scope = json.loads(self.request('/api/bootstrap?server=builders')[1])
        self.assertEqual(set(scope['channels']), {'dev', 'share'})
        self.assertEqual(len(scope['layouts']), 11)
        self.assertEqual(self.request('/layouts.css')[0], 200)
        full = json.loads(self.request('/api/report?mode=full')[1])
        self.assertEqual(full['digest']['mode'], 'full')
        self.assertEqual(self.request('/api/report?mode=invalid')[0], 400)

    def test_periodic_dimensions_and_week_summary(self):
        status, body, _ = self.request('/api/report?kind=weekly&focus_member=u1&focus_topic=launch')
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual(set(report['dimensions']), {'member', 'discussion'})
        status, body, _ = self.request('/api/report?kind=member&member=u1&period=weekly')
        self.assertEqual(status, 200)
        summary = json.loads(body)
        self.assertEqual(summary['window'], report['window'])
        self.assertEqual(summary['messages'], report['dimensions']['member']['messages'])
        self.assertEqual(self.request('/api/report?kind=member&member=u1&period=bad')[0], 400)

    def test_explicit_date_range(self):
        status, body, _ = self.request('/api/report?kind=weekly&date=2026-10-20&end_date=2026-10-21')
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual(report['window']['days'], 2)
        self.assertEqual(report['metrics']['messages'], 11)
        self.assertEqual(self.request('/api/report?kind=weekly&date=2026-10-21&end_date=2026-10-20')[0], 400)

    def test_generation_failure_is_actionable_without_raw_output(self):
        with patch.object(self.app.model, 'complete', return_value='private-invalid-model-output'):
            status, body, _ = self.request('/api/report')
        self.assertEqual(status, 502)
        self.assertIn('重新生成', json.loads(body)['error'])
        self.assertNotIn(b'private-invalid-model-output', body)
        original = self.app.model.complete
        def broken_dimension(messages):
            value = json.loads(original(messages))
            value['dimensions']['member']['topics'][0]['viewpoints'][0]['userId'] = 'u2'
            return json.dumps(value, ensure_ascii=False)
        with patch.object(self.app.model, 'complete', side_effect=broken_dimension):
            status, body, _ = self.request('/api/report?focus_member=u1&focus_topic=launch')
        report = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(report['generation']['status'], 'partial')
        self.assertIsNone(report['dimensions']['member']['digest'])
        self.assertEqual(report['dimensions']['discussion']['generation']['status'], 'complete')
        self.assertEqual(report['digest']['summaryStats']['messageCount'], 8)
        self.assertEqual(report['fullDigest']['mode'], 'full')

    def test_global_channel_context_icons_and_chart_assets(self):
        page = self.request('/')[1].decode()
        self.assertEqual(page.count('id="channel"'), 1)
        self.assertNotIn('id="activity-channel"', page)
        self.assertNotIn('id="alert-channel"', page)
        self.assertIn('id="page-questions"', page)
        self.assertEqual(page.count('<svg viewBox="0 0 24 24"'), 6)
        self.assertNotIn('data-page="imports"', page)
        self.assertIn('id="report-source"', page)
        self.assertEqual(self.request('/charts.js')[0], 200)
        status, icon, headers = self.request('/assets/app-icon.png')
        self.assertEqual(status, 200)
        self.assertTrue(icon.startswith(b'\x89PNG'))
        self.assertEqual(headers['Content-Type'], 'image/png')

    def test_channel_bootstrap_limits_choices_and_rejects_cross_server(self):
        status, body, _ = self.request('/api/bootstrap?server=community&channel=design')
        self.assertEqual(status, 200)
        scope = json.loads(body)
        self.assertEqual(scope['selected_channel'], 'design')
        self.assertEqual(set(scope['members']), {'u2', 'u4'})
        self.assertEqual(set(scope['topics']), {'layout'})
        self.assertEqual(self.request('/api/bootstrap?server=builders&channel=lobby')[0], 400)

    def test_questions_api_scope_history_and_invalid_output(self):
        data = dict(question='发布计划有什么决定？', server='community', channel='lobby')
        status, body, _ = self.request('/api/questions', data)
        self.assertEqual(status, 200)
        answer = json.loads(body)
        self.assertEqual(answer['scope']['channel'], 'lobby')
        self.assertTrue(all(m['channel'] == 'lobby' for m in answer['messages']))
        self.assertEqual(self.request('/api/questions', {**data, 'channel':'design',
            'conversation_id':answer['conversation_id']})[0], 400)
        self.assertEqual(self.request('/api/questions', {**data, 'server':'builders'})[0], 400)
        self.assertEqual(self.request('/api/questions', {**data, 'history':[]})[0], 400)
        self.assertEqual(self.request('/api/questions', {'question':'发布'})[0], 400)
        with patch.object(self.app.model, 'complete', return_value='private-question-output'):
            status, body, _ = self.request('/api/questions', data)
        self.assertEqual(status, 502)

    def test_history_and_bot_delivery_endpoints_keep_scope_and_offline_boundary(self):
        question = dict(question='发布计划有什么决定？', server='community', channel='lobby')
        first = json.loads(self.request('/api/questions', question)[1])
        history = json.loads(self.request('/api/conversations?server=community&channel=lobby')[1])
        self.assertEqual(history['items'][0]['id'], first['conversation_id'])
        path = '/api/conversation?id=' + first['conversation_id']
        self.assertEqual(self.request(path + '&server=community&channel=design')[0], 400)
        self.assertEqual(self.request(path + '&server=community&channel=lobby')[0], 200)
        self.assertEqual(self.request('/api/bots', dict(name='真实凭据拒绝', server='community', channels=['lobby'], token='private-token'))[0], 400)
        status, body, _ = self.request('/api/bots', dict(name='示例助手', server='community', channels=['design'], token='demo-private-token'))
        self.assertEqual(status, 200)
        self.assertNotIn(b'demo-private-token', body)
        bot = json.loads(body)
        report = json.loads(self.request('/api/report')[1])
        preview_data = dict(report_id=report['report_id'], bot_id=bot['id'], destination='design', server='community', channel='lobby')
        self.assertEqual(self.request('/api/deliveries/preview', dict(**preview_data, content='伪造内容'))[0], 400)
        status, body, _ = self.request('/api/deliveries/preview', preview_data)
        self.assertEqual(status, 200)
        preview = json.loads(body)
        result = json.loads(self.request('/api/deliveries/simulate', dict(preview_id=preview['id'], server='community', channel='lobby'))[1])
        self.assertFalse(result['sent'])
        self.assertEqual(result['status'], 'simulated')
        self.assertEqual(self.request('/api/deliveries/send', {})[0], 404)
        self.assertNotIn(b'private-question-output', body)


if __name__ == '__main__':
    unittest.main()
