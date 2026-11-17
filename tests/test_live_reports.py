import json
import copy
import unittest

from grouppulse.live_generation import generate_report
from grouppulse.generation import parse_model_output


class LiveReportTests(unittest.TestCase):
    def test_explicit_summary_sources_complete_parent_evidence_without_weakening_checks(self):
        from grouppulse.live_generation import validate
        rows=[dict(id='1',author_id='one',name='甲',content='聊到近况。'),
              dict(id='2',author_id='two',name='乙',content='分享了一则消息。')]
        value=dict(overview='聊天和消息。',overviewSourceMessageIds=['1','2'],
            topics=[dict(title='近况',summary='聊天和消息。',sourceMessageIds=['1'],
                         summarySourceMessageIds=['1','2'],participants=['甲','乙'],conclusions=[])])
        changes=[]
        validate(value,rows,adjustments=changes)
        self.assertEqual(value['topics'][0]['sourceMessageIds'],['1','2'])
        self.assertEqual(changes,[dict(field='topics.sourceMessageIds',sourceMessageIds=['2'],reason='explicit-summary-evidence')])
        for sources in (['1','unknown'],['1','2','2']):
            wrong=copy.deepcopy(value);wrong['topics'][0]['summarySourceMessageIds']=sources
            with self.assertRaises(ValueError): validate(wrong,rows)
        wrong=copy.deepcopy(value);wrong['topics'][0]['participants']=['其他人']
        with self.assertRaises(ValueError): validate(wrong,rows)
        wrong=copy.deepcopy(value);wrong['quotes']=[dict(sourceMessageIds=['2'],messages=[dict(sourceMessageId='2',sender='甲',content='分享了一则消息。')])]
        with self.assertRaises(ValueError): validate(wrong,rows)
        wrong['quotes'][0]['messages'][0].update(sender='乙',content='编造引语')
        with self.assertRaises(ValueError): validate(wrong,rows)

    def test_bot_minute_window_syncs_calendar_dates_and_filters_exact_evidence(self):
        import test_live
        from unittest.mock import patch
        live=test_live.LiveTests(); live.setUp()
        try:
            bot=live.configure()
            live.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
            with patch.object(live.app,'ensure_synced',wraps=live.app.ensure_synced) as sync:
                result=live.app.report(server='100',channel='200',kind='member',member='902',
                    start_time='2026-10-20T17:01',end_time='2026-10-20T17:02')
            sync.assert_called_once_with('100','200','2026-10-20','2026-10-20')
            self.assertEqual(result['metrics']['messages'],1)
            self.assertEqual([r['id'] for r in result['messages']],[live.rows[1]['id']])
            self.assertEqual(result['window']['label'],'2026-10-20 17:01 → 2026-10-20 17:02')
            self.assertEqual(result['contexts'][0]['message']['id'],live.rows[0]['id'])
            from grouppulse.reference_report import system_prompt
            from grouppulse.live_generation import CHAT_EDITING
            self.assertTrue(live.model_inputs[-1]['messages'][0]['content'].startswith(system_prompt()))
            self.assertIn(CHAT_EDITING,live.model_inputs[-1]['messages'][0]['content'])
        finally: live.tearDown()

    def test_scope_identity_and_hidden_foreign_sources_fail_before_normalization(self):
        rows=[dict(id=str(i),author_id='person'+str(i),name='同名',created='2026-10-21T01:00:00+00:00',
                   content='决定：周六分享。',reference_id=None,topic='unclassified',stance='发言',bot=False,
                   server='s',channel='c') for i in range(1,15)]
        value=dict(overview='周六分享。',overviewSourceMessageIds=['1'],
                   topics=[dict(title='分享',summary='周六分享。',heat='高',participants=['同名'],
                                sourceMessageIds=[row['id'] for row in rows],conclusions=[])],
                   importantMessages=[],todos=[],unresolved=[])
        class Model:
            input_budget=48*1024
            calls=0
            def complete(self,_messages):
                self.calls+=1
                return json.dumps(value,ensure_ascii=False)
        model=Model()
        scope=dict(days=1,server='s',channel='c')
        result=generate_report(model,scope,dict(kind='daily'),rows,[],'full')
        self.assertCountEqual(result['fullDigest']['topics'][0]['participantIds'],[row['author_id'] for row in rows])
        self.assertEqual(len(result['fullDigest']['topics'][0]['sourceMessageIds']),14)
        value['topics'][0]['sourceMessageIds'].append('foreign-after-original-source-limit')
        repaired=generate_report(model,scope,dict(kind='daily'),rows,[],'full')
        self.assertNotIn('foreign-after-original-source-limit',repaired['fullDigest']['topics'][0]['sourceMessageIds'])
        self.assertEqual(repaired['fullDigest']['topics'][0]['summary'],'周六分享。')
        value['topics'][0]['sourceMessageIds'].pop()
        value['importantMessages']=[dict(sender='其他人',content='错误归因',sourceMessageIds=['1'])]
        repaired=generate_report(model,scope,dict(kind='daily'),rows,[],'full')
        self.assertNotEqual(repaired['fullDigest']['importantMessages'][0]['sender'],'其他人')
        value['importantMessages']=[]
        before=model.calls
        wrong=copy.deepcopy(rows);wrong[0]['channel']='other'
        with self.assertRaises(ValueError): generate_report(model,scope,dict(kind='daily'),wrong,[],'full')
        self.assertEqual(model.calls,before)
        model.input_budget=100
        with self.assertRaisesRegex(ValueError,'输入预算'):
            generate_report(model,scope,dict(kind='daily'),rows,[],'full')
        self.assertEqual(model.calls,before)

    def test_reference_output_is_consumed_and_unimportant_rows_are_not_forced_into_topics(self):
        rows=[dict(id=str(i),author_id='person',name='成员',created='2026-10-21T01:00:00+00:00',
                   content=text,reference_id=None,topic='unclassified',stance='发言',bot=False,
                   server='server',channel='channel')
              for i,text in enumerate(['啊哦','决定：周六20:00线上分享。'],1)]
        value=dict(overview='分享定于周六20:00。',overviewSourceMessageIds=['2'],
                   hero=dict(headline='分享安排',summary='分享定于周六20:00。',summarySourceMessageIds=['2']),
                   topics=[dict(title='分享',summary='周六20:00线上分享。',heat='高',participants=['成员'],
                                sourceMessageIds=['2'],conclusions=[])],
                   importantMessages=[],todos=[],unresolved=[],keywords=['分享'])
        class Model:
            input_budget=48*1024
            def complete(self, messages):
                self.sent=messages
                return json.dumps(value,ensure_ascii=False)
        model=Model()
        result=generate_report(model,dict(days=1,server='server',channel='channel'),dict(kind='daily'),rows,[],'compact')
        self.assertIn('先合并同一事件，再按重要性排序',model.sent[0]['content'])
        self.assertEqual(result['generation']['core'],'starmemo-original-js')
        self.assertEqual(result['fullDigest']['summaryStats']['messageCount'],2)
        self.assertEqual(len(result['messages']),2)
        self.assertEqual([t['title'] for t in result['fullDigest']['topics']],['分享'])
        self.assertIn('presentations',result)
        value['topics'][0]['sourceMessageIds'].append('outside-channel')
        repaired=generate_report(model,dict(days=1,server='server',channel='channel'),dict(kind='daily'),rows,[],'full')
        self.assertEqual(repaired['fullDigest']['topics'][0]['sourceMessageIds'],['2'])

    def test_deadline_is_verified_in_original_message_not_in_extracted_task(self):
        rows=[dict(id='1',author_id='person',name='成员',created='2026-10-21T01:00:00+00:00',
                   content='周五18:00前需要整理报名名单，负责人还没确定。',reference_id=None,topic='unclassified',stance='发言',bot=False)]
        value=dict(overview='需整理名单，负责人未定。',overviewSourceMessageIds=['1'],excludedMessageIds=[],
                   topics=[dict(title='报名',summary='周五18:00前整理名单。',sourceMessageIds=['1'],summarySourceMessageIds=['1'],
                                heat='高',participants=['成员'],conclusions=[])],
                   importantMessages=[],unresolved=[],
                   todos=[dict(task='整理报名名单',sourceMessageIds=['1'],owner=None,deadline='周五18:00前',confidence=.9)])
        value.pop('excludedMessageIds')
        class Model:
            input_budget=48*1024
            def complete(self,_messages): return json.dumps(value,ensure_ascii=False)
        report=generate_report(Model(),dict(days=3),dict(kind='weekly'),rows,[],'full')
        self.assertEqual(report['fullDigest']['todos'][0]['deadline'],'周五18:00前')
        self.assertIsNone(report['fullDigest']['todos'][0]['owner'])
        # 原方案允许归纳任务措辞；不能用逐字匹配误拒绝有有效出处的待办。
        value['todos'][0]['task']='准备分享会报名名单'
        paraphrased=generate_report(Model(),dict(days=3),dict(kind='weekly'),rows,[],'full')
        self.assertEqual(paraphrased['fullDigest']['todos'][0]['task'],'准备分享会报名名单')
        value['todos'][0]['deadline']='周四18:00前'
        repaired=generate_report(Model(),dict(days=3),dict(kind='weekly'),rows,[],'full')
        self.assertIsNone(repaired['fullDigest']['todos'][0]['deadline'])
        self.assertEqual(repaired['fullDigest']['todos'][0]['task'],'准备分享会报名名单')

    def test_only_exact_input_echo_can_precede_a_single_json_answer(self):
        original=dict(scope=dict(server='one',channel='selected'),messages=[dict(id='123',content='原文')])
        answer=dict(overview='有效回答')
        raw=json.dumps(original,ensure_ascii=False)+'\n'+json.dumps(answer,ensure_ascii=False)
        self.assertEqual(parse_model_output(raw,original),answer)
        for invalid in [raw+'\n{}',json.dumps(dict(original,scope=dict(channel='other')))+'\n'+json.dumps(answer),
                        '解释\n'+raw,'{"x":1,"x":2}\n'+json.dumps(answer)]:
            with self.assertRaises(ValueError): parse_model_output(invalid,original)

    def test_model_date_format_is_restored_only_when_original_source_is_unambiguous(self):
        rows=[dict(id='1',author_id='person',name='成员',created='2026-10-21T02:00:00+00:00',
                   content='今晚复核名单。',reference_id=None,topic='unclassified',stance='发言',bot=False)]
        rows.append(dict(rows[0],id='2',content='明天开会。'))
        value=dict(overview='需复核名单和开会。',overviewSourceMessageIds=['1','2'],
                   topics=[dict(title='安排',summary='今晚复核名单，明天开会。',sourceMessageIds=['1','2'],participants=['成员'],conclusions=[])],
                   importantMessages=[],unresolved=[],todos=[dict(task='复核名单',owner=None,deadline='今晚（2026-10-21 晚间）',sourceMessageIds=['1']),
                                                             dict(task='开会',owner=None,deadline='2026-10-22',sourceMessageIds=['2'])])
        class Model:
            input_budget=48*1024
            def complete(self,_messages): return json.dumps(value,ensure_ascii=False)
        report=generate_report(Model(),dict(days=1,timezone='Asia/Shanghai'),dict(kind='daily'),rows,[],'full')
        self.assertEqual({i['deadline'] for i in report['fullDigest']['todos']},{'今晚','明天'})
        value['todos'][1]['deadline']='2026-10-24'
        self.assertIsNone(generate_report(Model(),dict(days=1),dict(kind='daily'),rows,[],'full')['fullDigest']['todos'][1]['deadline'])
        value['todos'][1]['deadline']='2026-10-22'; value['todos'][0]['deadline']='明晚（2026-10-22）'
        self.assertIsNone(generate_report(Model(),dict(days=1),dict(kind='daily'),rows,[],'full')['fullDigest']['todos'][0]['deadline'])
        value['todos'][0]['deadline']=None; value['todos'][1]['owner']='其他人'
        self.assertIsNone(generate_report(Model(),dict(days=1),dict(kind='daily'),rows,[],'full')['fullDigest']['todos'][1]['owner'])

    def test_relative_deadline_uses_source_timezone_and_rejects_ambiguous_phrases(self):
        rows=[dict(id='1',author_id='person',name='成员',created='2026-10-21T20:00:00+00:00',
                   content='明天开会。',reference_id=None,topic='unclassified',stance='发言',bot=False)]
        value=dict(overview='开会。',overviewSourceMessageIds=['1'],topics=[dict(title='安排',summary='明天开会。',sourceMessageIds=['1'],participants=['成员'],conclusions=[])],
                   importantMessages=[],unresolved=[],todos=[dict(task='开会',owner=None,deadline='2026-10-23',sourceMessageIds=['1'])])
        class Model:
            input_budget=48*1024
            def complete(self,_messages): return json.dumps(value,ensure_ascii=False)
        report=generate_report(Model(),dict(days=1,timezone='Asia/Shanghai'),dict(kind='daily'),rows,[],'full')
        self.assertEqual(report['fullDigest']['todos'][0]['deadline'],'明天')
        self.assertIsNone(generate_report(Model(),dict(days=1,timezone='America/Los_Angeles'),dict(kind='daily'),rows,[],'full')['fullDigest']['todos'][0]['deadline'])
        rows.append(dict(rows[0],id='2',created='2026-10-23T01:00:00+00:00',content='今天开会。'))
        value['todos'][0]['sourceMessageIds'].append('2')
        self.assertIsNone(generate_report(Model(),dict(days=2),dict(kind='daily'),rows,[],'full')['fullDigest']['todos'][0]['deadline'])

    def test_unimportant_messages_are_preserved_without_forcing_a_report_topic(self):
        rows=[dict(id=str(i),author_id='person',name='成员',created='2026-10-21T01:00:00+00:00',
                   content=text,reference_id=None,topic='unclassified',stance='发言',bot=False)
              for i,text in enumerate(['啊哦','决定：周六20:00线上分享。'],1)]
        value=dict(overview='分享定于周六20:00。',overviewSourceMessageIds=['2'],
                   topics=[dict(title='分享',summary='周六20:00线上分享。',sourceMessageIds=['2'],
                                summarySourceMessageIds=['2'],heat='高',participants=['成员'],conclusions=[])],
                   importantMessages=[],todos=[],unresolved=[])
        class Model:
            input_budget=48*1024
            def complete(self,_messages): return json.dumps(value,ensure_ascii=False)
        result=generate_report(Model(),dict(days=1),dict(kind='daily'),rows,[],'full')
        self.assertEqual({r['id'] for r in result['messages']},{'1','2'})
        self.assertEqual(result['fullDigest']['summaryStats']['messageCount'],2)
        self.assertNotIn('其他交流',[t['title'] for t in result['fullDigest']['topics']])
        value['topics'][0]['sourceMessageIds'].append('foreign')
        repaired=generate_report(Model(),dict(days=1),dict(kind='daily'),rows,[],'full')
        self.assertEqual(repaired['fullDigest']['topics'][0]['sourceMessageIds'],['2'])

    def test_missing_duplicate_and_unknown_sources_do_not_discard_the_summary(self):
        rows=[dict(id='1',author_id='person',name='成员',created='2026-10-21T01:00:00+00:00',
                   content='去海边散步。',reference_id=None,topic='unclassified',stance='发言',bot=False)]
        value=dict(overview='聊了周末安排。',topics=[dict(title='周末',summary='打算去海边散步。',sourceMessageIds=['unknown','unknown'],conclusions=[])])
        class Model:
            input_budget=48*1024
            def complete(self,_messages): return json.dumps(value)
        result=generate_report(Model(),dict(days=1),dict(kind='daily'),rows,[],'full')
        topic=result['fullDigest']['topics'][0]
        self.assertEqual(topic['summary'],'打算去海边散步。')
        self.assertEqual(topic['sourceMessageIds'],[])
        self.assertEqual(topic['participantIds'],[])
        self.assertEqual(len(result['messages']),1)
