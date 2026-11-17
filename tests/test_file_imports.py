import copy
import http.client
import json
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import test_live
from grouppulse.live import LiveApp


def export_file(rows):
    return dict(guild=dict(id='100', name='测试服务器'),
                channel=dict(id='200', name='产品频道', type='GuildTextChat'),
                exportedAt='2026-10-21T10:00:00+00:00', messageCount=len(rows),
                messages=[dict(id=r['id'], timestamp=r['timestamp'], type='Reply' if r.get('message_reference') else 'Default',
                               content=r['content'], author=dict(id=r['author']['id'], name=r['author']['username'],
                               nickname=r['author']['username'], isBot=False),
                               reference=dict(messageId=r['message_reference']['message_id'], channelId=r['message_reference']['channel_id'], guildId='100')
                               if r.get('message_reference') else None) for r in rows])


class FileImportTests(unittest.TestCase):
    def setUp(self):
        self.live = test_live.LiveTests()
        self.live.setUp()
        self.app = self.live.app

    def tearDown(self):
        self.live.tearDown()

    def upload(self, value=None):
        return self.app.file_imports.add(dict(filename='频道.json', content=json.dumps(value or export_file(self.live.rows), ensure_ascii=False)))

    def generate(self, item, **extra):
        return self.app.file_imports.report(dict(import_id=item['id'], server='100', channel='200',
                                               date='2026-10-20', member='901', timezone='Asia/Shanghai', **extra))

    def test_import_is_idempotent_atomic_and_independent_of_bot(self):
        bot = self.live.configure()
        self.app.bootstrap('100', '200')
        self.app.sync_channel(bot['id'], '100', '200', '2026-10-20', '2026-10-20')
        before = self.app.message(self.live.rows[0]['id'])
        selection = self.app.meta('selection')
        calls = len(self.live.calls)
        value = export_file(self.live.rows)
        value['messages'][0]['content'] = '文件中的另一版本，不能覆盖 Bot 记录。'
        item = self.upload(value)
        self.assertEqual(item['message_count'], 3)
        self.assertEqual(set(item['members']), {'901', '902'})
        self.assertEqual(self.upload(value)['id'], item['id'])
        self.assertEqual(len(self.app.file_imports.items()['items']), 1)
        self.assertEqual(self.app.message(self.live.rows[0]['id']), before)
        self.assertEqual(self.app.meta('selection'), selection)
        self.assertEqual(len(self.live.calls), calls)
        self.assertFalse(self.live.model_inputs)
        broken = copy.deepcopy(value)
        broken['messages'][-1]['author']['id'] = 'not-an-id'
        with self.assertRaises(ValueError): self.upload(broken)
        self.assertEqual(len(self.app.file_imports.items()['items']), 1)

    def test_strict_identity_scope_timestamp_count_and_duplicate_validation(self):
        changes = [lambda d: d.update(guild=dict(id='0', name='私信')),
                   lambda d: d.update(channel=dict(id='200', name='私信', type='DirectTextChat')),
                   lambda d: d['messages'][0].update(channel_id='201'),
                   lambda d: d['messages'][0].update(guild_id='101'),
                   lambda d: d['messages'][0].update(timestamp='2026-10-20T09:00:00'),
                   lambda d: d['messages'][0]['author'].update(isBot='false'),
                   lambda d: d.update(messageCount=99)]
        for change in changes:
            value = export_file(self.live.rows); change(value)
            with self.subTest(change=change), self.assertRaises(ValueError): self.upload(value)
        value = export_file(self.live.rows)
        value['messages'].append(dict(value['messages'][0], content='同 ID 冲突'))
        value['messageCount'] = 4
        with self.assertRaisesRegex(ValueError, '冲突'): self.upload(value)
        with self.assertRaises(ValueError):
            self.app.file_imports.add(dict(filename='错误.json', content='{"guild":{},"guild":{}}'))
        self.assertEqual(self.app.file_imports.items()['items'], [])

    def test_generation_executes_original_core_preserves_author_context_and_history(self):
        self.live.configure()
        item = self.upload()
        calls = len(self.live.calls)
        with patch.object(self.app, 'sync_channel', side_effect=AssertionError('文件不应触发 Bot 同步')):
            report = self.generate(item)
        self.assertEqual(report['generation']['core'], 'starmemo-original-js')
        self.assertEqual({r['author_id'] for r in report['messages']}, {'901'})
        self.assertEqual(report['source']['kind'], 'discord-export')
        self.assertEqual(report['source']['import_id'], item['id'])
        self.assertFalse(report['coverage']['window_complete'])
        self.assertIn('文件', report['coverage']['note'])
        self.assertTrue(all(r['url'].endswith('/' + r['id']) for r in report['messages']))
        self.assertTrue(self.live.model_inputs)
        self.assertTrue(all(path == '/v1/chat/completions' for _, path in self.live.calls[calls:]))
        self.assertEqual(self.app.message(self.live.rows[0]['id']), None)
        restored = LiveApp(self.app.path, vault=self.live.vault)
        saved = restored.file_imports.saved(item['id'], '100', '200', report['report_id'])
        self.assertEqual(saved['messages'], report['messages'])
        self.assertEqual(saved['source'], report['source'])
        self.assertEqual(saved['fullDigest'], report['fullDigest'])
        self.assertEqual(restored.file_imports.inspect(item['id'])['reports'][0]['id'], report['report_id'])
        newer = export_file(self.live.rows)
        newer['exportedAt'] = '2026-10-22T10:00:00+00:00'
        second = self.upload(newer)
        second_report = self.generate(second)
        history = restored.file_imports.history('100','200')['reports']
        self.assertEqual({r['import_id'] for r in history}, {item['id'],second['id']})
        self.assertEqual({r['id'] for r in history}, {report['report_id'],second_report['report_id']})
        self.assertEqual(restored.file_imports.history('100','201')['reports'], [])
        for data in [dict(server='101'), dict(channel='201'), dict(member='missing')]:
            request = dict(import_id=item['id'], server='100', channel='200', date='2026-10-20', member='901')
            request.update(data)
            with self.assertRaises(ValueError): self.app.file_imports.report(request)
        with self.assertRaises(ValueError): restored.file_imports.saved(item['id'], '100', '201', report['report_id'])

    def test_missing_and_cross_channel_references_never_use_bot_cache(self):
        bot = self.live.configure()
        self.app.sync_channel(bot['id'], '100', '200', '2026-10-20', '2026-10-20')
        value = export_file(self.live.rows[1:])
        item = self.upload(value)
        report = self.generate(item)
        self.assertEqual(report['contexts'][0]['status'], 'missing')
        self.assertIn('文件', report['contexts'][0]['reason'])
        value = export_file(self.live.rows)
        value['messages'][2]['reference']['channelId'] = '201'
        item = self.upload(value)
        report = self.generate(item)
        self.assertTrue(any(c['status'] == 'missing' for c in report['contexts']))
        self.live.alter_response = lambda root: root['topics'][0]['sourceMessageIds'].append(self.live.rows[1]['id'])
        self.app.save_config(dict(max_tokens=4096))
        repaired=self.generate(item)
        self.assertTrue(all(self.live.rows[1]['id'] not in topic['sourceMessageIds'] for topic in repaired['fullDigest']['topics']))
        self.assertEqual({r['author_id'] for r in repaired['messages']},{'901'})

    def test_import_daily_timezone_uses_natural_day_and_handles_dst(self):
        self.live.configure()
        value = export_file(self.live.rows[:1])
        value['messages'][0]['timestamp'] = '2026-10-21T03:00:00+00:00'
        item = self.upload(value)
        local = self.app.file_imports.inspect(item['id'], 'America/Los_Angeles')
        self.assertEqual(local['dates'], ['2026-10-20'])
        report = self.app.file_imports.report(dict(import_id=item['id'], server='100', channel='200', date='2026-10-20',
                                                  member='901', timezone='America/Los_Angeles'))
        self.assertEqual(report['window']['timezone'], 'America/Los_Angeles')
        self.assertEqual(report['selected_count'], 1)
        text = self.live.model_inputs[-1]['messages'][-1]['content']
        self.assertIn('2026-10-20 20:00:00', text)
        reader = self.app.file_imports.reader(item['id'], 'America/Los_Angeles')
        left, right = reader.window('2026-11-01')
        self.assertEqual((right-left).total_seconds(), 25*3600)
        empty = self.app.file_imports.report(dict(import_id=item['id'], server='100', channel='200', date='2026-11-01',
                                                 member='901', timezone='America/Los_Angeles'))
        self.assertEqual(empty['selected_count'], 0)
        self.assertEqual(empty['generation']['calls'], 0)

    def test_http_file_payload_large_enough_and_routes_cannot_generate_by_get(self):
        from grouppulse.server import create_server
        server = create_server(self.app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def request(path, data=None):
            c = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            c.request('POST' if data is not None else 'GET', path, body=json.dumps(data) if data is not None else None,
                      headers={'Content-Type':'application/json','X-GroupPulse':'local'})
            r = c.getresponse(); result = json.loads(r.read()); status = r.status; c.close()
            return status, result
        try:
            value = export_file(self.live.rows)
            value['messages'][0]['content'] = '较大的导出文件。' * 6000
            status, item = request('/api/imports', dict(filename='记录.json', content=json.dumps(value)))
            self.assertEqual(status, 200)
            self.assertEqual(request('/api/imports')[1]['items'][0]['id'], item['id'])
            self.assertEqual(request('/api/import-report?import_id='+item['id'])[0], 400)
            self.assertEqual(request('/api/report', dict(_synced=True))[0], 400)
            self.assertEqual(request('/api/imports', dict(filename='bad.json', content='{}'))[0], 400)
            self.assertEqual(self.live.calls, [])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_minute_window_and_channel_reports_share_core_and_preserve_history(self):
        self.live.configure()
        item = self.upload()
        request = dict(import_id=item['id'], server='100', channel='200', kind='member', member='902',
                       start_time='2026-10-20 17:01', end_time='2026-10-20 17:02', timezone='Asia/Shanghai')
        report = self.app.file_imports.report(request)
        self.assertEqual([r['id'] for r in report['messages']], [self.live.rows[1]['id']])
        self.assertEqual(report['window']['start'], '2026-10-20T09:01:00+00:00')
        self.assertEqual(report['window']['end'], '2026-10-20T09:02:00+00:00')
        self.assertEqual(report['window']['precision'], 'minute')
        self.assertIn('17:01', report['window']['label'])
        self.assertEqual(report['contexts'][0]['message']['id'], self.live.rows[0]['id'])
        self.assertEqual(report['generation']['core'], 'starmemo-original-js')
        for kind in ('daily', 'weekly', 'discussion'):
            channel = self.app.file_imports.report(dict(request, kind=kind, member=''))
            self.assertEqual(channel['kind'], kind)
            self.assertEqual([r['id'] for r in channel['messages']], [self.live.rows[1]['id']])
            self.assertEqual(channel['generation']['core'], 'starmemo-original-js')
        history = self.app.file_imports.history('100', '200')['reports']
        self.assertEqual({r['kind'] for r in history}, {'member', 'daily', 'weekly', 'discussion'})
        self.assertTrue(all(r['window']['label'] == report['window']['label'] for r in history))
        restored = self.app.file_imports.saved(item['id'], '100', '200', report['report_id'])
        self.assertEqual(restored['window'], report['window'])
        calls = len(self.live.model_inputs)
        for change in [dict(end_time=request['start_time']), dict(end_time=''), dict(start_time='2026-10-20 17:01:30'), dict(channel='201')]:
            with self.assertRaises(ValueError): self.app.file_imports.report(dict(request, **change))
        self.assertEqual(len(self.live.model_inputs), calls)
