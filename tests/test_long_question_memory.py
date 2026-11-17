import copy
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from grouppulse.live import LiveApp
from grouppulse.reference_questions import ChannelQueryApi, QueryRuntime


class Vault:
    storage = 'test-memory'

    def get(self, _key):
        return ''


class LongConversationModel:
    engine = 'openai-compatible'
    input_budget = 48 * 1024

    def __init__(self):
        self.requests = []
        self.compactions = []

    def complete_tools(self, messages, tools, timeout=None):
        self.requests.append(copy.deepcopy(dict(messages=messages, tools=tools)))
        if timeout:
            payload = json.loads(messages[-1]['content'])
            self.compactions.append(payload)
            source = next(item for item in payload['sources'] if item['role'] == 'user')
            summary = dict(goals=[dict(sourceId=source['sourceId'], role='user', quote=source['quote'][:230])],
                           confirmedConditions=[], conclusions=[])
            return dict(success=True, data=json.dumps(summary, ensure_ascii=False))
        if messages[-1]['role'] == 'user':
            return dict(success=True, toolCalls=[dict(id='local-query', name='query_messages',
                arguments=json.dumps(dict(timeRange=dict(kind='all'), temporalBasis=dict(kind='none'))))])
        payload = json.loads(messages[-1]['content'])
        rows = payload.get('messages', payload.get('evidence', []))
        answer = '这是合成回放中的长回答，仅用于检查全文和记忆摘录分别保存。' * 24
        answer += '；'.join(row['sender'] + '：' + row['text'] + '[' + row['citationId'] + ']' for row in rows)
        return dict(success=True, data=answer)


class LongQuestionMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'live.sqlite3'
        self.model = LongConversationModel()
        self.app = self.new_app()
        self.app.upsert_messages([
            dict(id=str(100+i), server='s', channel='c', author_id='a' if i % 2 == 0 else 'b',
                 name='同名', created=f'2026-10-20T09:0{i}:00+00:00', content=f'第{i}条合成观点，前提没有变化。',
                 bot=False, stance='', topic='', reference_id=None) for i in range(6)])

    def new_app(self):
        app = LiveApp(self.path, vault=Vault())
        app.demo = dict(servers={'s': dict(name='合成服务器', channels={'c': '当前', 'd': '另一个'})},
                        channels={'c': '当前', 'd': '另一个'}, topics={}, date='2026-10-20')
        app.model = self.model
        app.ensure_synced = lambda *_args: None
        return app

    def tearDown(self):
        self.app.auto_sync.stop()
        self.tmp.cleanup()

    def long_dialogue(self):
        answer = self.app.ask('仅核对合成项目玫瑰-9831，不解释未定义缩写。', 's', 'c', '2026-10-20', '2026-10-20')
        for index in range(1, 9):
            answer = self.app.ask(f'继续回顾当前频道的观点，第{index}次追问。', 's', 'c',
                                  '2026-10-20', '2026-10-20', answer['conversation_id'])
        return answer

    def test_compacted_multi_author_dialogue_restores_after_restart_without_losing_full_turns(self):
        answer = self.long_dialogue()
        self.assertEqual(answer['metrics']['memory_turns'], 6)
        self.assertEqual(answer['metrics']['memory_summary']['summarizedTurnCount'], 3)
        self.assertTrue(answer['metrics']['memory_summary']['hasSummary'])
        self.assertEqual(len(self.model.compactions), 3)
        self.assertEqual({point['userId'] for point in answer['points']}, {'a', 'b'})
        with self.app.db() as db:
            state = json.loads(db.execute('SELECT body FROM question_memory WHERE id=?',
                                          (answer['conversation_id'],)).fetchone()['body'])['memory']
        self.assertEqual(len(state['turns']), 6)
        self.assertTrue(all(len(turn['answer']) <= 513 for turn in state['turns']))
        restored = self.new_app()
        stored = restored.conversation(answer['conversation_id'], 's', 'c')['turns']
        self.assertEqual(len(stored), 9)
        self.assertTrue(all(len(turn['response']['answer']) > 513 for turn in stored))
        self.assertEqual(stored[-1]['response']['messages'], answer['messages'])
        following = restored.ask('继续回顾已核对的观点。', 's', 'c', '2026-10-20', '2026-10-20', answer['conversation_id'])
        self.assertEqual(following['metrics']['memory_context']['summarizedTurnCount'], 3)
        self.assertEqual(following['metrics']['memory_summary']['summarizedTurnCount'], 4)
        query = next(request for request in reversed(self.model.requests) if request['tools'] and request['messages'][-1]['role'] == 'user')
        self.assertIn('玫瑰-9831', json.dumps(query, ensure_ascii=False))
        self.assertFalse(any(re.search(r'\[E[1-9]\d*\]', message['content'])
                             for message in query['messages'] if message['role'] == 'assistant'))
        self.assertLessEqual(following['metrics']['input_bytes'], self.model.input_budget)

    def test_compacted_memory_cannot_cross_scope_and_record_change_refreshes_without_deleting_history(self):
        answer = self.long_dialogue()
        conversation = answer['conversation_id']
        before = len(self.model.requests)
        with self.assertRaises(ValueError):
            self.app.ask('另一个频道说什么？', 's', 'd', '2026-10-20', '2026-10-20', conversation)
        with self.assertRaises(ValueError):
            self.app.ask('另一个周期说什么？', 's', 'c', '2026-10-19', '2026-10-20', conversation)
        self.assertEqual(len(self.model.requests), before)
        self.app.upsert_messages([dict(self.app.message('100'), content='第0条合成观点已更正，原前提不成立。')])
        refreshed = self.app.ask('更正后的内容是什么？', 's', 'c', '2026-10-20', '2026-10-20', conversation)
        self.assertEqual(refreshed['metrics']['memory_turns'], 0)
        self.assertTrue(refreshed['metrics']['memory_refreshed'])
        self.assertFalse(refreshed['metrics']['memory_summary']['hasSummary'])
        self.assertEqual(len(self.model.compactions), 3)
        query = next(request for request in reversed(self.model.requests) if request['tools'] and request['messages'][-1]['role'] == 'user')
        self.assertNotIn('玫瑰-9831', json.dumps(query, ensure_ascii=False))
        history = self.app.conversation(conversation, 's', 'c')['turns']
        self.assertEqual(len(history), 10)
        original = next(row for row in history[0]['response']['messages'] if row['id'] == '100')
        self.assertEqual(original['content'], '第0条合成观点，前提没有变化。')

    def test_selected_timezone_keeps_original_timestamp_identity_and_reply_link(self):
        self.app.report_timezone = ZoneInfo('America/Los_Angeles')
        self.app.report_timezone_name = 'America/Los_Angeles'
        self.app.upsert_messages([dict(self.app.message('101'), reference_id='100')])
        scope = dict(server='s', channel='c', channel_name='当前', label='合成服务器/当前',
                     timezone='America/Los_Angeles')
        runtime = QueryRuntime()
        api = ChannelQueryApi(self.app, scope, datetime(2026, 10, 20, tzinfo=timezone.utc),
                              datetime(2026, 10, 21, tzinfo=timezone.utc), runtime, 48*1024)
        original = self.app.message('101')
        packed, background = api.pack([original])
        self.assertEqual(packed[0]['datetime'], '2026-10-20T02:01:00-07:00')
        self.assertEqual(packed[0]['timestamp'], int(datetime.fromisoformat(original['created']).timestamp()*1000))
        self.assertEqual(packed[0]['senderId'], original['author_id'])
        self.assertEqual(packed[0]['text'], original['content'])
        runtime.set('QueryHost.testSource', packed[0]['messageRef'])
        decoded = json.loads(runtime.context.eval('JSON.stringify(StarMemoQueryCore.decodeMessageRef(QueryHost.testSource))'))
        self.assertEqual(decoded, dict(conversationId='s/c', messageId='101'))
        self.assertEqual(background[0]['id'], '100')
        self.assertEqual(background[0]['message']['created'], self.app.message('100')['created'])
        answer = self.app.ask('按所选时区回顾当前发言。', 's', 'c', '2026-10-20', '2026-10-20')
        self.assertEqual(answer['scope']['timezone'], 'America/Los_Angeles')
        self.assertEqual(next(row for row in answer['messages'] if row['id'] == '101')['created'], original['created'])
        tool = json.loads(self.model.requests[-1]['messages'][-1]['content'])
        seen = next(row for row in tool['messages'] if row['text'] == original['content'])
        self.assertEqual(seen['datetime'], '2026-10-20T02:01:00-07:00')
        self.assertEqual(seen['timestamp'], packed[0]['timestamp'])

    def test_timezone_change_rejects_old_dialogue_before_model_and_keeps_history(self):
        self.app.report_timezone = ZoneInfo('America/Los_Angeles')
        self.app.report_timezone_name = 'America/Los_Angeles'
        answer = self.app.ask('回顾当前频道的观点。', 's', 'c', '2026-10-20', '2026-10-20')
        self.assertEqual(answer['scope']['timezone'], 'America/Los_Angeles')
        conversation = answer['conversation_id']
        history = self.app.conversation(conversation, 's', 'c')['turns']
        before = len(self.model.requests)
        self.app.report_timezone = ZoneInfo('UTC')
        self.app.report_timezone_name = 'UTC'
        with self.assertRaisesRegex(ValueError, '新建'):
            self.app.ask('同一日期继续回顾。', 's', 'c', '2026-10-20', '2026-10-20', conversation)
        self.assertEqual(len(self.model.requests), before)
        self.assertEqual(self.app.conversation(conversation, 's', 'c')['turns'], history)
        self.assertEqual(history[0]['response']['scope']['timezone'], 'America/Los_Angeles')

    def test_legacy_oversized_snapshot_rebuilds_through_original_record_and_preserves_full_history(self):
        for collection in ('turns', 'pending'):
            with self.subTest(collection=collection):
                answer = self.long_dialogue()
                conversation = answer['conversation_id']
                old_history = self.app.conversation(conversation, 's', 'c')['turns']
                poison = '旧适配缺陷-4477不应进入后续请求。' * 70
                with self.app.db() as db:
                    state = json.loads(db.execute('SELECT body FROM question_memory WHERE id=?', (conversation,)).fetchone()['body'])
                    if collection == 'turns':
                        state['memory']['turns'][-1]['answer'] = poison
                    else:
                        state['memory']['pending'] = [dict(id='t987', question=poison, answer=poison)]
                    db.execute('UPDATE question_memory SET body=? WHERE id=?', (json.dumps(state), conversation))
                restored = self.new_app()
                following = restored.ask('继续回顾已有原文。', 's', 'c', '2026-10-20', '2026-10-20', conversation)
                self.assertEqual(following['metrics']['memory_turns'], 6)
                self.assertFalse(following['metrics']['memory_context']['hasSummary'])
                self.assertEqual(following['metrics']['memory_summary']['summaryStatus'], 'ready')
                query = next(request for request in reversed(self.model.requests) if request['tools'] and request['messages'][-1]['role'] == 'user')
                self.assertNotIn('旧适配缺陷-4477', json.dumps(query, ensure_ascii=False))
                self.assertLessEqual(following['metrics']['input_bytes'], self.model.input_budget)
                self.assertEqual(restored.conversation(conversation, 's', 'c')['turns'][:9], old_history)


if __name__ == '__main__':
    unittest.main()
