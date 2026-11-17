import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from grouppulse.live import LiveApp
from grouppulse.generation import GenerationError
from grouppulse.reference_questions import QueryRuntime, ChannelQueryApi


class Vault:
    storage = 'test-memory'
    def get(self, _key): return ''


class ToolModel:
    engine = 'openai-compatible'
    input_budget = 48 * 1024
    def __init__(self):
        self.requests, self.change, self.tool_args, self.summary = [], None, None, None

    def complete_tools(self, messages, tools, timeout=None):
        self.requests.append(copy.deepcopy(dict(messages=messages,tools=tools)))
        if timeout:
            if self.summary: return dict(success=True,data=json.dumps(self.summary))
            return dict(success=True,data=json.dumps(dict(goals=[],confirmedConditions=[],conclusions=[])))
        if messages[-1]['role'] == 'user':
            name = tools[0]['function']['name']
            args = dict(timeRange=dict(kind='all'))
            if name == 'query_messages': args['temporalBasis'] = dict(kind='none')
            if name == 'search_messages': args['queries'] = ['活动']
            if self.tool_args: args = self.tool_args
            return dict(success=True,toolCalls=[dict(id='call-1',name=name,arguments=json.dumps(args))])
        payload = json.loads(messages[-1]['content'])
        rows = payload.get('messages',payload.get('evidence',[]))
        value = dict(success=True,data='；'.join(r['sender']+'：'+r['text'][:80]+'['+r['citationId']+']' for r in rows) or '没有足够原文。')
        if self.change: self.change(value)
        return value


class ReferenceQuestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'live.sqlite3'
        self.app = self.new_app()
        self.model = ToolModel(); self.app.model = self.model
        rows = [dict(id=str(10+i),server='s',channel='c',created='2026-10-20T09:0'+str(i)+':00+00:00',
            author_id='a' if i == 0 else 'b',name='同名',bot=False,content=text,stance='',topic='',reference_id=None)
            for i,text in enumerate(['决定：周六20:00分享活动。','周五18:00前整理报名，负责人未定。','是否录屏还没决定。'])]
        self.app.upsert_messages([*rows,dict(rows[0],id='foreign',server='other',channel='d',content='不可越界的消息'),
                                 dict(rows[0],id='old',created='2020-01-01T00:00:00+00:00',content='日期外秘密')])

    def new_app(self):
        app = LiveApp(self.path,vault=Vault())
        app.demo=dict(servers={'s':dict(name='测试',channels={'c':'当前','d':'其他'})},channels={'c':'当前','d':'其他'},topics={},date='2026-10-20')
        app.ensure_synced = lambda *args: None
        return app

    def tearDown(self):
        self.app.auto_sync.stop(); self.tmp.cleanup()

    def ask(self, question='活动怎么安排？', **extra):
        return self.app.ask(question,'s','c','2026-10-20','2026-10-20',**extra)

    def test_original_runtime_prompt_tools_and_identity_sources_are_executed(self):
        answer = self.ask()
        self.assertEqual(answer['generation']['runtime'],'copied-starmemo-query-agent')
        self.assertEqual(answer['generation']['model_calls'],2)
        self.assertIn('规划原则',self.model.requests[0]['messages'][0]['content'])
        self.assertTrue(self.model.requests[0]['tools'])
        self.assertEqual({p['userId'] for p in answer['points']},{'a','b'})
        self.assertEqual({r['id'] for r in answer['messages']},{'10','11','12'})
        self.assertNotIn('日期外秘密',json.dumps(self.model.requests,ensure_ascii=False))
        self.assertNotIn('不可越界',json.dumps(self.model.requests,ensure_ascii=False))

    def test_followup_restart_restoration_new_dialogue_and_version_reset(self):
        first = self.ask()
        second = self.ask('那谁负责？',conversation_id=first['conversation_id'])
        self.assertEqual(second['metrics']['memory_turns'],1)
        restarted = self.new_app(); restarted.model = self.model
        restored = restarted.conversation(first['conversation_id'],'s','c')
        self.assertFalse(restored['offline'])
        self.assertFalse(restarted.conversations('s','c')['offline'])
        self.assertEqual(len(restored['turns']),2)
        third = restarted.ask('还有什么未定？','s','c','2026-10-20','2026-10-20',first['conversation_id'])
        self.assertEqual(third['metrics']['memory_turns'],2)
        self.assertNotIn('[E',self.model.requests[-2]['messages'][2]['content'])
        self.assertEqual(self.ask()['metrics']['memory_turns'],0)
        # A pre-reuse conversation has no source-memory snapshot: hydrate its existing full turns.
        with restarted.db() as db:
            db.execute('DELETE FROM question_memory WHERE id=?',(first['conversation_id'],))
        legacy = restarted.ask('旧存档中的安排呢？','s','c','2026-10-20','2026-10-20',first['conversation_id'])
        self.assertEqual(legacy['metrics']['memory_turns'],3)
        self.app.upsert_messages([dict(self.app.message('12'),content='更正：是否录屏仍未定。')])
        fresh = self.ask(conversation_id=first['conversation_id'])
        self.assertEqual(fresh['metrics']['memory_turns'],0)
        self.assertTrue(fresh['metrics']['memory_refreshed'])
        self.assertEqual(len(self.app.conversation(first['conversation_id'],'s','c')['turns']),5)

    def test_turn_times_and_source_snapshot_survive_restart_without_inventing_legacy_times(self):
        before = datetime.now(timezone.utc)
        first = self.ask()
        after = datetime.now(timezone.utc)
        self.assertLessEqual(before, datetime.fromisoformat(first['asked_at']))
        self.assertLessEqual(datetime.fromisoformat(first['asked_at']), datetime.fromisoformat(first['answered_at']))
        self.assertLessEqual(datetime.fromisoformat(first['answered_at']), after)
        original = copy.deepcopy(first)
        self.app.upsert_messages([dict(self.app.message('10'),content='后来编辑的内容',name='后来改名')])
        restarted = self.new_app()
        stored = restarted.conversation(first['conversation_id'],'s','c')['turns'][0]['response']
        self.assertEqual(stored, original)
        self.assertNotIn('后来编辑', json.dumps(stored, ensure_ascii=False))
        legacy = copy.deepcopy(stored)
        legacy.pop('asked_at'); legacy.pop('answered_at')
        with restarted.db() as db:
            db.execute('UPDATE question_turns SET body=? WHERE conversation_id=?',
                (json.dumps(dict(question=legacy['question'],response=legacy)),first['conversation_id']))
        missing = restarted.conversation(first['conversation_id'],'s','c')['turns'][0]['response']
        self.assertNotIn('asked_at',missing)
        self.assertNotIn('answered_at',missing)
        self.assertEqual(missing['messages'],original['messages'])

    def test_six_pairs_compaction_and_no_old_citations_in_prompt(self):
        answer = self.ask()
        for _ in range(8): answer = self.ask('那还要做什么？',conversation_id=answer['conversation_id'])
        self.assertEqual(answer['metrics']['memory_turns'],6)
        self.assertTrue(answer['metrics']['memory_summary']['summarizedTurnCount'])
        self.assertLessEqual(answer['metrics']['input_bytes'],self.model.input_budget)
        self.assertTrue(any(not r['tools'] for r in self.model.requests))

    def test_scope_forgery_invalid_citations_and_changed_records_do_not_save_memory(self):
        self.model.tool_args=dict(target=dict(query='其他'),timeRange=dict(kind='all'),temporalBasis=dict(kind='none'))
        answer=self.ask()
        self.assertEqual(answer['status'],'insufficient_evidence')
        self.assertEqual(answer['messages'],[])
        self.model.tool_args=None
        self.model.change=lambda value:value.update(data='编造事实[E999]')
        with self.assertRaisesRegex(GenerationError,'出处'): self.ask()
        self.model.change=lambda value:self.app.upsert_messages([dict(self.app.message('12'),content='更新后的内容')])
        with self.assertRaisesRegex(ValueError,'记录已更新'): self.ask()

    def test_exact_source_reference_roundtrip_and_original_memory_generation_guard(self):
        runtime=QueryRuntime()
        refs=runtime.references('服务器/频道',['消息😀'])
        runtime.set('QueryHost.testRefs',refs)
        value=json.loads(runtime.context.eval('JSON.stringify(StarMemoQueryCore.decodeMessageRef(QueryHost.testRefs[0]))'))
        self.assertEqual(value,dict(conversationId='服务器/频道',messageId='消息😀'))
        self.assertTrue(runtime.context.eval('''(() => {const memory=new StarMemoQueryCore.QueryAgentConversationMemory();
            const generation=memory.begin('a'); memory.forget('a');
            return !memory.record('a','old','old',generation) && memory.status('a').recentTurnCount===0;})()'''))

    def test_new_messages_during_generation_do_not_interrupt_but_next_question_refreshes(self):
        self.model.change=lambda value:self.app.upsert_messages([dict(self.app.message('10'),id='new',content='新增消息')])
        answer=self.ask()
        self.assertEqual(answer['status'],'answered')
        self.model.change=None
        following=self.ask(conversation_id=answer['conversation_id'])
        self.assertTrue(following['metrics']['memory_refreshed'])
        self.assertEqual(following['metrics']['memory_turns'],0)

    def test_budget_and_source_evidence_cap_are_explicit_without_loading_other_dates(self):
        row=self.app.message('10')
        self.app.upsert_messages([dict(row,id=f'large-{i}',content='分享活动的讨论内容。'*80) for i in range(120)])
        answer=self.ask()
        self.assertTrue(answer['coverage']['budget_limited'])
        self.assertFalse(answer['coverage']['retrieval_complete'])
        self.assertLessEqual(len(answer['messages']),40)
        self.assertLessEqual(answer['metrics']['input_bytes'],self.model.input_budget)
        self.assertIn('不能当作全部讨论',answer['coverage']['note'])
        self.model.tool_args=dict(timeRange=dict(kind='absolute',startTime='2020-01-01T00:00:00+08:00',
            endTime='2020-01-02T00:00:00+08:00'),temporalBasis=dict(kind='recall_hint',sourceText='2020年'))
        empty=self.ask('2020年的活动是什么？')
        self.assertEqual(empty['messages'],[])
        self.assertEqual(empty['generation']['tool_calls'],1)
        self.assertNotIn('日期外秘密',json.dumps(self.model.requests[-2:],ensure_ascii=False))

    def test_original_compaction_rejects_invented_quotes_and_keeps_recent_pairs(self):
        self.model.summary=dict(goals=[dict(sourceId='t1:user',role='user',quote='凭空增加的条件')],
            confirmedConditions=[],conclusions=[])
        answer=self.ask()
        for _ in range(6): answer=self.ask(conversation_id=answer['conversation_id'])
        state=answer['metrics']['memory_summary']
        self.assertEqual(state['summaryStatus'],'failed')
        self.assertEqual(state['recentTurnCount'],6)
        self.assertEqual(state['pendingTurnCount'],1)
        self.assertFalse(state['hasSummary'])

    def test_foreign_server_even_with_same_channel_and_forged_old_reference_are_rejected(self):
        row=self.app.message('10')
        self.app.upsert_messages([dict(row,id='corrupt',server='other',content='异服务器内容')])
        self.assertNotIn('异服务器内容',json.dumps(self.ask(),ensure_ascii=False))
        runtime=QueryRuntime()
        old=runtime.references('s/c',['10'])[0]
        self.model.tool_args=dict(messageRef=old,before=1,after=1)
        # The first tool is query_messages: an injected message_context field is rejected by the original schema.
        empty=self.ask()
        self.assertEqual(empty['messages'],[])
        runtime.set('QueryHost.oldReference',old)
        self.assertTrue(runtime.context.eval('new StarMemoQueryCore.QueryAgentMessageReferences().resolve(QueryHost.oldReference) === undefined'))

    def test_currently_unreadable_thread_cache_does_not_enter_query_tools(self):
        self.app.upsert_messages([dict(self.app.message('10'),id='hidden-thread',source_channel='thread',
            unavailable=True,content='当前无权限的历史文字')])
        answer=self.ask()
        self.assertNotIn('hidden-thread',{row['id'] for row in answer['messages']})
        self.assertNotIn('当前无权限',json.dumps(self.model.requests,ensure_ascii=False))

    def test_nearby_context_does_not_mix_sibling_threads_or_parent_chatter(self):
        original=self.app.message('10')
        anchor=dict(original,id='thread-a1',source_channel='a',thread_name='子讨论',created='2026-10-20T09:00:30+00:00')
        following=dict(anchor,id='thread-a2',created='2026-10-20T09:01:30+00:00')
        other=dict(anchor,id='thread-b1',source_channel='b',created='2026-10-20T09:00:45+00:00')
        self.app.upsert_messages([anchor,following,other])
        scope=dict(server='s',channel='c',channel_name='当前',label='测试/当前')
        start,end=self.app.window('2026-10-20')
        api=ChannelQueryApi(self.app,scope,start,end,QueryRuntime(),48*1024)
        messages,_=api.pack([self.app.message(anchor['id'])])
        result=api.context(dict(messageRef=messages[0]['messageRef'],before=5,after=5))
        self.assertEqual(result['before'],[])
        self.assertEqual([api.refs[row['messageRef']] for row in result['after']],['thread-a2'])
        self.assertEqual(result['anchor']['sourceChannelId'],'a')


if __name__ == '__main__': unittest.main()
