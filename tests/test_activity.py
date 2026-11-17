import copy
import json
import tempfile
import unittest
from pathlib import Path

from grouppulse.live import LiveApp


class Vault:
    storage='test-memory'
    def get(self,_key): return ''


class Model:
    input_budget=48*1024
    engine='openai-compatible'
    def __init__(self):
        self.calls=0;self.groups=[('版本计划',['p1','c0'])];self.invalid=False
    def complete(self,messages):
        self.calls+=1
        text=messages[-1]['content']
        if 'DISCORD_REPORT_SCOPE\n' not in text:
            return '无法判定' if self.invalid else '1'
        return json.dumps(dict(overview='版本发布讨论',overviewSourceMessageIds=list(dict.fromkeys(identifier for _,ids in self.groups for identifier in ids)),
            topics=[dict(title=title,summary='版本计划讨论',heat='高',sourceMessageIds=ids,participants=[],conclusions=[])
                    for title,ids in self.groups],importantMessages=[],todos=[],unresolved=[]),ensure_ascii=False)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'live.sqlite3'
        self.app=self.new_app();self.model=Model();self.app.model=self.model
        self.synced=[];self.app.ensure_synced=lambda *args:self.synced.append(args)
        self.complete=True
        self.app.coverage=lambda *args:dict(status='complete' if self.complete else 'partial',
            window_complete=self.complete,up_to_date=False,missing=0,note='测试覆盖')
        row=dict(server='s',channel='c',author_id='a',name='同名',bot=False,stance='',topic='unclassified',reference_id=None,
                 created='2026-10-20T01:00:00+00:00',content='发布计划讨论',text_available=True)
        self.app.upsert_messages([dict(row,id='p1',created='2026-10-19T01:00:00+00:00'),
            *[dict(row,id=f'c{i}',author_id='a' if i%2 else 'b') for i in range(7)],
            dict(row,id='bot',bot=True),dict(row,id='system',message_type=7),
            dict(row,id='hidden',unavailable=True),dict(row,id='foreign',server='other')])

    def new_app(self):
        app=LiveApp(self.path,vault=Vault())
        app.demo=dict(servers={'s':dict(name='测试',channels={'c':'当前','d':'其他'})},channels={'c':'当前','d':'其他'},topics={},date='2026-10-20')
        app.save_meta('catalog',app.demo)
        return app

    def tearDown(self): self.app.auto_sync.stop();self.tmp.cleanup()

    def activity(self,**extra):
        return self.app.report(date='2026-10-20',server='s',channel='c',kind='activity',**extra)

    def topics(self,**extra):
        return self.app.topic_trends(date='2026-10-20',server='s',channel='c',**extra)

    def test_raw_comparison_is_scoped_deduplicated_and_does_not_call_model(self):
        value=self.activity()
        self.assertEqual(value['metrics']['messages'],7)
        self.assertEqual(value['metrics']['members'],2)
        self.assertEqual(value['metrics']['previous'],1)
        self.assertTrue(value['metrics']['comparable'])
        self.assertEqual(value['comparison']['window']['start_date'],'2026-10-19')
        self.assertEqual(self.model.calls,0)
        self.assertEqual(self.synced,[('s','c','2026-10-19','2026-10-20')])
        self.assertEqual(sum(item['messages'] for item in value['daily']),8)

    def test_custom_range_uses_equal_previous_period_and_incomplete_is_not_a_drop(self):
        value=self.activity(end_date='2026-10-22')
        self.assertEqual(value['window']['days'],3)
        self.assertEqual(value['comparison']['window']['start_date'],'2026-10-17')
        self.assertEqual(value['comparison']['window']['end_date'],'2026-10-19')
        self.complete=False
        self.assertFalse(self.activity()['metrics']['comparable'])

    def test_topic_trends_reuse_report_and_original_classifier_then_cache_without_calls(self):
        value=self.topics()
        analysis=value['topic_analysis']
        self.assertEqual(analysis['generation']['report_runtime'],'copied-starmemo')
        self.assertEqual(analysis['generation']['classifier_runtime'],'copied-chat-digest')
        self.assertEqual(analysis['topics'][0]['current'],7)
        self.assertEqual(analysis['topics'][0]['previous'],1)
        self.assertEqual(analysis['topics'][0]['state'],'升温')
        self.assertEqual(analysis['classified_messages'],8)
        calls=self.model.calls
        self.assertGreater(calls,1)
        self.assertTrue(self.topics()['topic_analysis']['cached'])
        self.assertEqual(self.model.calls,calls)
        self.assertTrue(self.activity()['topic_analysis']['cached'])

    def test_stable_identity_uses_sources_not_titles_and_cross_scope_is_rejected(self):
        first=self.topics()['topic_analysis']['topics'][0]['id']
        self.model.groups=[('上线安排',['p1','c0'])]
        second=self.topics(refresh=True)['topic_analysis']['topics'][0]['id']
        self.assertEqual(first,second)
        for kwargs in [dict(server='other',channel='c'),dict(server='s',channel='unknown')]:
            with self.assertRaises(ValueError): self.app.topic_trends(date='2026-10-20',**kwargs)
        other=self.app.topic_trends(date='2026-10-20',server='s',channel='d')
        self.assertEqual(other['trends'],[])

    def test_same_title_on_nonoverlapping_evidence_does_not_merge_topic_identity(self):
        first=self.topics()['topic_analysis']['topics'][0]['id']
        original=self.app.message('p1')
        self.app.upsert_messages([dict(original,id='new1',created='2026-10-16T01:00:00+00:00'),
            dict(original,id='new2',created='2026-10-17T01:00:00+00:00')])
        self.model.groups=[('版本计划',['new1','new2'])]
        other=self.app.topic_trends(date='2026-10-17',server='s',channel='c')
        self.assertNotEqual(other['topic_analysis']['topics'][0]['id'],first)

    def test_classifier_functions_are_exact_source_copies(self):
        import ast,hashlib
        from grouppulse.reference_classifier import SOURCE
        host={};exec(compile(SOURCE,'classifier','exec'),host)
        tree=ast.parse(SOURCE)
        for name,expected in host['FUNCTION_SHA256'].items():
            node=next(item for item in tree.body if isinstance(item,ast.FunctionDef) and item.name==name)
            self.assertEqual(hashlib.sha256(ast.get_source_segment(SOURCE,node).encode()).hexdigest(),expected)

    def test_invalid_classification_or_missing_coverage_never_claims_definite_temperature(self):
        self.model.invalid=True
        analysis=self.topics()['topic_analysis']
        self.assertGreater(analysis['unclassified_messages'],0)
        self.assertNotIn(analysis['topics'][0]['state'],('升温','降温'))
        self.model.invalid=False;self.complete=False
        self.assertNotIn(self.topics(refresh=True)['topic_analysis']['topics'][0]['state'],('升温','降温'))

    def test_edits_invalidate_cache_and_forged_sources_are_not_persisted(self):
        self.topics()
        self.app.upsert_messages([dict(self.app.message('c0'),content='发布计划更新')])
        self.assertEqual(self.activity()['topic_analysis']['status'],'not_generated')
        self.model.groups=[('伪造来源',['foreign'])]
        with self.assertRaises(ValueError): self.topics()

    def test_new_messages_do_not_abort_but_edits_reject_stale_analysis(self):
        original=self.model.complete
        def append(messages):
            self.app.upsert_messages([dict(self.app.message('c0'),id='during',content='新发布消息')])
            return original(messages)
        self.model.complete=append
        value=self.topics()
        self.assertEqual(value['topic_analysis']['status'],'partial')
        self.assertEqual(value['topic_analysis']['topics'][0]['state'],'待核对')
        self.assertEqual(self.activity()['topic_analysis']['status'],'not_generated')
        def edit(messages):
            self.app.upsert_messages([dict(self.app.message('c0'),content='已被编辑的依据')])
            return original(messages)
        self.model.complete=edit
        with self.assertRaisesRegex(ValueError,'已编辑'):self.topics()
