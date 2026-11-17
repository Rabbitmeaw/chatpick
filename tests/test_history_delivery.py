import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grouppulse.core import App
from grouppulse.generation import LocalMockModel


class CaptureModel(LocalMockModel):
    def complete(self, messages):
        self.request = copy.deepcopy(messages)
        return super().complete(messages)


class HistoryDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'offline.sqlite3'
        self.model = CaptureModel()
        self.app = App(self.path, self.model)
        self.app.import_demo()

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, **extra):
        return self.app.ask('总结这个频道的讨论', 'community', 'lobby', **extra)

    def bot(self, **extra):
        return self.app.save_bot(dict(name='日报助手', server='community', channels=['lobby', 'design'],
                                      application_id='123456789', token='demo-bot', **extra))

    def test_default_dates_and_entire_model_input_are_bounded(self):
        old = dict(self.app.message('m101'), id='old-record', created='2020-01-01T01:00:00+00:00')
        huge = dict(self.app.message('m101'), id='huge-record', content='超长聊天' * 30000)
        self.app.upsert_messages([old, huge])
        answer = self.ask()
        self.assertEqual(answer['scope']['start_date'], '2026-10-15')
        self.assertEqual(answer['scope']['end_date'], '2026-10-21')
        self.assertNotIn('old-record', [m['id'] for m in answer['messages']])
        self.assertLessEqual(len(json.dumps(self.model.request, ensure_ascii=False).encode()), 24 * 1024)
        self.assertTrue(answer['coverage']['budget_limited'])
        self.assertNotIn('huge-record', [m['id'] for m in answer['messages']])
        self.assertLessEqual(answer['metrics']['input_bytes'], 24 * 1024)

    def test_candidates_context_and_followup_have_hard_limits(self):
        source = self.app.message('m101')
        self.app.upsert_messages([dict(source, id=f'candidate-{i}') for i in range(1010)])
        context = self.app.message('m090')
        self.app.upsert_messages([dict(context, content='长引用' * 20000)])
        answer = self.ask()
        self.assertGreater(answer['metrics']['scope_messages'], 1000)
        self.assertEqual(answer['metrics']['scanned_messages'], answer['metrics']['scope_messages'])
        for _ in range(8):
            answer = self.ask(conversation_id=answer['conversation_id'])
            self.assertLessEqual(len(json.dumps(self.model.request, ensure_ascii=False).encode()), 24 * 1024)
        self.assertLessEqual(answer['metrics']['memory_turns'], 6)
        self.assertFalse(answer['coverage']['retrieval_complete'])

    def test_history_survives_restart_new_dialogue_and_is_scope_filtered(self):
        first = self.ask()
        second = self.ask()
        self.assertNotEqual(first['conversation_id'], second['conversation_id'])
        app = App(self.path, self.model)
        self.assertEqual(len(app.conversations('community', 'lobby')['items']), 2)
        self.assertEqual(app.conversations('community', 'design')['items'], [])
        restored = app.conversation(first['conversation_id'], 'community', 'lobby')
        self.assertEqual(restored['turns'][0]['response']['answer'], first['answer'])
        followup = app.ask('那谁反对？', 'community', 'lobby', conversation_id=first['conversation_id'])
        self.assertEqual(followup['metrics']['memory_turns'], 1)
        with self.assertRaises(ValueError):
            app.conversation(first['conversation_id'], 'community', 'design')
        with app.db() as db:
            body = json.dumps(dict(question='存档分页样例', response=first), ensure_ascii=False)
            db.executemany('INSERT INTO question_turns(conversation_id,body) VALUES (?, ?)',
                           [(first['conversation_id'], body) for _ in range(55)])
        page = app.conversation(first['conversation_id'], 'community', 'lobby')
        older = app.conversation(first['conversation_id'], 'community', 'lobby', str(page['turns'][0]['seq']))
        self.assertTrue(page['has_more'])
        self.assertFalse(older['has_more'])
        self.assertEqual(len(page['turns']) + len(older['turns']), 57)
        self.assertLess(older['turns'][-1]['seq'], page['turns'][0]['seq'])

    def test_changed_records_allow_reading_history_but_not_reusing_stale_memory(self):
        first = self.ask()
        self.app.upsert_messages([dict(self.app.message('m101'), content='修改后的原文')])
        self.assertTrue(self.app.conversation(first['conversation_id'], 'community', 'lobby')['stale'])
        with self.assertRaisesRegex(ValueError, '记录已更新'):
            self.ask(conversation_id=first['conversation_id'])

    def test_multiple_bot_profiles_are_local_and_secrets_are_not_persisted(self):
        bot = self.bot()
        other = self.app.save_bot(dict(name='开发助手', server='builders', channels=['dev'], token='demo-other'))
        self.assertNotEqual(bot['id'], other['id'])
        self.assertEqual(len(self.app.bots()['items']), 2)
        self.assertEqual(len(self.app.bots('community')['items']), 1)
        self.assertNotIn('demo-bot', self.path.read_bytes().decode(errors='ignore'))
        self.assertNotIn('token', bot)
        self.assertEqual(bot['status'], 'offline_unverified')
        self.assertFalse(App(self.path).bots()['items'][0]['has_token'])
        for change in [dict(token='real-secret'), dict(channels=['dev']), dict(server=['community'])]:
            data = dict(name='错误配置', server='community', channels=['lobby'], token='demo-token')
            data.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.app.save_bot(data)

    def test_delivery_preview_uses_server_snapshot_and_simulation_is_idempotent(self):
        bot = self.bot()
        self.app.upsert_messages([dict(self.app.message('m105'), content=self.app.message('m105')['content'] + '😀' * 2000)])
        report = self.app.report(focus_member='u1', focus_topic='launch')
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            for kind in ['', 'member', 'discussion']:
                preview = self.app.preview_delivery(report['report_id'], bot['id'], 'design', 'community', 'lobby', kind)
                self.assertTrue(preview['payloads'])
                self.assertTrue(all(len(p['content'].encode('utf-16-le')) // 2 <= 1800 for p in preview['payloads']))
                self.assertTrue(all(p['allowed_mentions'] == {'parse': []} for p in preview['payloads']))
                rendered = '\n'.join(p['content'] for p in preview['payloads'])
                selected = report if not kind else report['dimensions'][kind]
                self.assertIn(selected['coverage']['note'], rendered)
                if not kind:
                    self.assertGreater(len(preview['payloads']), 1)
                    self.assertIn('关注维度 · 成员发言摘要', rendered)
                    self.assertIn('关注维度 · 多人讨论摘要', rendered)
                if kind == 'member':
                    self.assertNotIn('〔消息 m102〕', rendered)
                result = self.app.simulate_delivery(preview['id'], 'community', 'lobby')
                again = self.app.simulate_delivery(preview['id'], 'community', 'lobby')
                self.assertEqual(result['status'], 'simulated')
                self.assertFalse(result['sent'])
                self.assertTrue(again['duplicate'])
        for values in [('dev', 'community', 'lobby'), ('design', 'community', 'design')]:
            with self.assertRaises(ValueError):
                self.app.preview_delivery(report['report_id'], bot['id'], *values)
        with self.assertRaises(ValueError):
            self.app.simulate_delivery(preview['id'], 'builders', 'dev')


if __name__ == '__main__':
    unittest.main()
