import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grouppulse.core import App, DEMO_PATH


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'demo.sqlite3'
        self.app = App(self.path)
        self.app.import_demo()

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, **kwargs):
        return self.app.report(date='2026-10-21', channel='lobby', **kwargs)

    def test_original_metrics_idempotence_and_edit(self):
        first = self.report()
        self.app.import_demo()
        again = self.report()
        self.assertEqual(first['metrics'], again['metrics'])
        self.assertEqual(first['metrics']['messages'], 8)
        self.assertEqual(first['metrics']['members'], 3)
        items = json.loads(DEMO_PATH.read_text())['messages']
        row = next(m for m in items if m['id'] == 'm101')
        row['content'] = '编辑后的决定'
        self.app.upsert_messages([row])
        self.assertEqual(self.report()['metrics']['messages'], 8)
        self.assertEqual(self.app.message('m101')['content'], '编辑后的决定')

    def test_member_identity_context_and_missing(self):
        report = self.report(kind='member', member='u1')
        self.assertTrue(all(m['author_id'] == 'u1' for m in report['messages']))
        self.assertNotIn('m103', [m['id'] for m in report['messages']])
        contexts = {c['id']: c for c in report['contexts']}
        self.assertEqual(contexts['m090']['status'], 'available')
        self.assertEqual(contexts['m090']['message']['author_id'], 'u2')
        self.assertEqual(contexts['missing-1']['status'], 'missing')
        self.assertEqual(report['coverage']['status'], 'partial')
        self.assertEqual(report['metrics']['messages'], 8)
        self.assertEqual(report['selected_count'], 3)

    def test_week_raw_evidence_and_empty(self):
        day_ids = {m['id'] for m in self.report()['messages']}
        week = self.report(kind='weekly')
        week_ids = {m['id'] for m in week['messages']}
        self.assertIn('m080', week_ids - day_ids)
        self.assertEqual(week['window']['start'], '2026-10-18T16:00:00+00:00')
        empty = self.app.report(date='2026-11-01', channel='lobby')
        self.assertEqual(empty['metrics']['messages'], 0)
        self.assertEqual(empty['coverage']['status'], 'unknown')

    def test_topics_keep_disagreement_and_trends(self):
        report = self.report(kind='discussion', topic='launch')
        self.assertIn('反对', {m['stance'] for m in report['messages']})
        self.assertIn('待确认', {m['stance'] for m in report['messages']})
        trends = {t['id']: t for t in self.report()['trends']}
        self.assertEqual(trends['launch']['previous'], 2)
        self.assertEqual(trends['launch']['current'], 5)
        self.assertEqual(trends['docs']['previous'], 0)
        self.assertEqual(trends['docs']['state'], '本期出现')
        missing = self.app.report(date='2026-10-19', channel='lobby')
        self.assertTrue(all(t['state'] == '覆盖不足，暂不比较' for t in missing['trends']))

    def test_context_cycle_and_depth(self):
        row = self.app.message('m101')
        row['reference_id'] = 'm101'
        self.app.upsert_messages([row])
        result = self.report()
        self.assertIn('cycle', {c['status'] for c in result['contexts']})
        chain = []
        for index in range(8):
            chain.append({**row, 'id': f'chain{index}', 'reference_id': f'chain{index + 1}',
                          'created': '2026-10-20T01:00:00+00:00'})
        self.app.upsert_messages(chain)
        row['reference_id'] = 'chain0'
        self.app.upsert_messages([row])
        self.assertIn('truncated', {c['status'] for c in self.report()['contexts']})

    def test_alert_dedup_and_restart(self):
        rule = dict(keyword='发布', channel='lobby', date='2026-10-21')
        first = self.app.check_alerts(**rule)
        self.assertGreater(first['created'], 0)
        self.assertEqual(self.app.check_alerts(**rule)['created'], 0)
        restarted = App(self.path)
        self.assertEqual(restarted.check_alerts(**rule)['created'], 0)
        row = self.app.message('m101')
        row['content'] += ' 发布更新'
        restarted.upsert_messages([row])
        self.assertEqual(restarted.check_alerts(**rule)['created'], 1)

    def test_config_never_calls_network_or_persists_secret(self):
        config = dict(base_url='https://example.invalid/v1', model='demo-model',
                      api_key='demo-private-key', protocol='openai-compatible',
                      timeout=30, retries=1, max_tokens=2048, temperature=None)
        with patch('socket.socket.connect', side_effect=AssertionError('禁止联网')):
            response = self.app.save_config(config)
            self.assertTrue(response['has_key'])
            self.assertNotIn('demo-private-key', json.dumps(response))
            result = self.app.check_config(config)
            self.assertEqual(result['status'], 'offline_valid')
        self.assertNotIn(b'demo-private-key', self.path.read_bytes())
        self.assertFalse(App(self.path).get_config()['has_key'])
        self.app.save_config({**config, 'api_key': ''})
        self.assertTrue(self.app.get_config()['has_key'])
        self.app.save_config({**config, 'api_key': '', 'clear_key': True})
        self.assertFalse(self.app.get_config()['has_key'])

    def test_invalid_inputs(self):
        for data in [dict(base_url='javascript:bad', model='x'),
                     dict(base_url='https://example.invalid', model=''),
                     dict(base_url='https://example.invalid', model='x', retries=-1),
                     dict(base_url='https://example.invalid', model='x', temperature='NaN')]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.app.save_config(data)
        with self.assertRaises(ValueError):
            self.app.report(date='bad')

    def test_server_switching_and_isolation(self):
        data = self.app.bootstrap('builders')
        self.assertEqual(set(data['channels']), {'dev', 'share'})
        self.assertEqual(set(data['members']), {'u5', 'u6'})
        result = self.app.report(server='builders', channel='dev')
        self.assertEqual(result['metrics']['messages'], 3)
        self.assertEqual(result['coverage']['status'], 'complete')
        self.assertTrue(all(m['id'].startswith('v') for m in result['messages']))
        with self.assertRaises(ValueError):
            self.app.report(server='builders', channel='lobby')
        with self.assertRaises(ValueError):
            self.app.report(server='community', channel='dev')
        with self.assertRaises(ValueError):
            self.app.report(server='builders', channel='dev', kind='member', member='u1')
        self.app.check_alerts(keyword='发布', channel='lobby')
        self.assertEqual(self.app.alerts(server='builders'), [])
        self.app.check_alerts(keyword='发布', channel='dev', server='builders')
        self.assertTrue(all(a['message']['channel'] == 'dev' for a in self.app.alerts(server='builders')))
        self.assertTrue(all(a['message']['channel'] == 'lobby' for a in self.app.alerts(server='community')))


if __name__ == '__main__':
    unittest.main()
