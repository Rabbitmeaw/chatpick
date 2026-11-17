import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from grouppulse.live import LiveApp
from grouppulse.live_generation import generate_report, prompt
from grouppulse.report_profiles import select_profile
from test_chat_quality import EvidenceModel, message
from test_live import MemoryVault


class ReportProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.host = LiveApp(Path(self.tmp.name)/'live.sqlite3', vault=MemoryVault())
        self.profile = dict(id='us-stock-chat', version=1, server='s', account_id='reader',
                            instructions='专属合成背景：保留个人观点，不能写成交易建议。')
        self.host.save_meta('report-profiles', {'s':self.profile})
        self.reader = SimpleNamespace(host=self.host, account_scope=dict(account_id='reader'))
        self.scope = dict(server='s', channel='c', days=1)
        self.rows = [message('1', 'a', '我认为如果条件成立，XYZ才可能上涨。'),
                     message('2', 'b', '周末想去海边。')]

    def tearDown(self): self.tmp.cleanup()

    def test_profile_is_bound_to_account_server_and_report_kind(self):
        self.assertEqual(select_profile(self.reader,'s','daily'), self.profile)
        self.assertEqual(select_profile(self.reader,'s','member'), self.profile)
        for kind in ('weekly','discussion','activity'):
            self.assertIsNone(select_profile(self.reader,'s',kind))
        self.assertIsNone(select_profile(self.reader,'another','daily'))
        self.reader.account_scope['account_id']='someone-else'
        self.assertIsNone(select_profile(self.reader,'s','daily'))
        self.assertIsNone(select_profile(self.host,'s','daily'))
        imported=SimpleNamespace(host=self.host,metadata=dict(origin=dict(kind='account-export',account_id='reader')))
        self.assertEqual(select_profile(imported,'s','member'),self.profile)
        imported.metadata['origin']['kind']='file'
        self.assertIsNone(select_profile(imported,'s','member'))

    def test_only_scoped_prompts_change_and_source_input_stays_identical(self):
        original=prompt(self.scope,dict(kind='daily'),self.rows,[])
        tagged=prompt(self.scope,dict(kind='daily'),self.rows,[],self.profile)
        self.assertIn(self.profile['instructions'],tagged[0]['content'])
        self.assertIn('stock_opinion',tagged[0]['content'])
        self.assertTrue(tagged[0]['content'].startswith(original[0]['content']))
        self.assertEqual(tagged[1]['content'].split('DISCORD_REPORT_SCOPE')[0],original[1]['content'].split('DISCORD_REPORT_SCOPE')[0])
        for scope,kind in [(dict(self.scope,server='another'),'daily'),(self.scope,'weekly'),(self.scope,'discussion')]:
            self.assertEqual(prompt(scope,dict(kind=kind),self.rows,[],self.profile),
                             prompt(scope,dict(kind=kind),self.rows,[]))
        both=prompt(dict(self.scope,instructions='额外要求'*400),dict(kind='daily'),self.rows,[],self.profile)
        self.assertIn('额外要求',both[0]['content'])
        self.assertIn(self.profile['instructions'],both[0]['content'])

    def test_one_original_core_call_retains_sections_and_raw_evidence(self):
        class Model(EvidenceModel):
            def complete(self,messages):
                value=json.loads(super().complete(messages))
                value['hero']=dict(summary='合成速览。'*40+'结尾条件必须保留。',summarySourceMessageIds=['1'])
                value['topics']=[dict(value['topics'][0],title='个人条件看法',section='stock_opinion',sourceMessageIds=['1'],summary='合成观点。'*40+'仅是个人看法，条件尚未成立。'),
                                 dict(value['topics'][0],title='生活闲聊',section='chat',sourceMessageIds=['2'])]
                return json.dumps(value,ensure_ascii=False)
        rows=copy.deepcopy(self.rows)
        model=Model()
        result=generate_report(model,self.scope,dict(kind='daily'),rows,[],'full',profile=self.profile)
        self.assertEqual(model.calls,1)
        self.assertEqual(result['generation']['core'],'starmemo-original-js')
        self.assertEqual(result['report_profile'],dict(id='us-stock-chat',version=1,server='s'))
        for digest in result['presentations'].values():
            self.assertEqual({topic['section'] for topic in digest['topics']},{'stock_opinion','chat'})
            by_title={topic['title']:topic for topic in digest['topics']}
            self.assertEqual(by_title['个人条件看法']['participantIds'],['a'])
            self.assertEqual(by_title['生活闲聊']['sourceMessageIds'],['2'])
            self.assertTrue(by_title['个人条件看法']['summary'].endswith('仅是个人看法，条件尚未成立。'))
            self.assertTrue(digest['hero']['summary'].endswith('结尾条件必须保留。'))
        self.assertEqual(rows,self.rows)
        self.assertEqual(result['messages'],self.rows)
        plain=generate_report(Model(),self.scope,dict(kind='daily'),rows,[],'full')
        self.assertNotIn('report_profile',plain)
        self.assertTrue(all('section' not in topic for topic in plain['digest']['topics']))

    def test_focus_members_are_explicit_but_never_add_outside_or_background_speech(self):
        profile=dict(self.profile,focus_members=['a','absent'])
        result=prompt(self.scope,dict(kind='daily'),self.rows,[],profile)
        boundary=json.loads(result[1]['content'].split('DISCORD_REPORT_SCOPE\n',1)[1])
        focus=boundary['reportProfile']['focusAuthors']
        self.assertEqual([item['userId'] for item in focus],['a'])
        self.assertEqual(focus[0]['sourceMessageIds'],['1'])
        self.assertIn('单一主标的',boundary['reportProfile']['task'])
        self.assertIn('交易建议',result[0]['content'])
        member=prompt(self.scope,dict(kind='member',member='b'),self.rows[1:],[],profile)
        member_boundary=json.loads(member[1]['content'].split('DISCORD_REPORT_SCOPE\n',1)[1])
        self.assertEqual(member_boundary['reportProfile']['focusAuthors'],[])

    def test_original_deduper_does_not_drop_different_assets_using_one_message(self):
        class Model(EvidenceModel):
            def complete(self,messages):
                root=json.loads(super().complete(messages))
                base=dict(root['topics'][0],section='stock_opinion',sourceMessageIds=['1'],heat='高')
                root['topics']=[dict(base,title='AAA的个人分析',asset='AAA',summary='甲认为AAA要看条件甲。'),
                                dict(base,title='BBB的个人分析',asset='BBB',summary='甲认为BBB要看条件乙。'),
                                dict(base,title='AAA的重复观点',asset='AAA',summary='重复内容不另外展示。')]
                return json.dumps(root,ensure_ascii=False)
        rows=[message('1','a','AAA看条件甲；BBB看条件乙。')]
        original=copy.deepcopy(rows)
        result=generate_report(Model(),self.scope,dict(kind='daily'),rows,[],'full',profile=self.profile)
        self.assertEqual(len(result['digest']['topics']),2)
        self.assertEqual([topic['asset'] for topic in result['digest']['topics']],['AAA','BBB'])
        self.assertEqual(result['digest']['summaryStats']['topicCount'],2)
        for topic in result['digest']['topics']:
            self.assertEqual(topic['sourceMessageIds'],['1'])
            self.assertEqual(topic['participantIds'],['a'])
        self.assertEqual(rows,original)
        self.assertEqual(result['messages'],original)
        self.assertNotIn(':asset:',json.dumps(result))
        plain=generate_report(Model(),self.scope,dict(kind='daily'),rows,[],'full')
        self.assertEqual(len(plain['digest']['topics']),1)
        self.assertNotIn('asset',plain['digest']['topics'][0])

    def test_asset_splitting_preserves_the_news_and_chat_sections_in_both_reading_modes(self):
        class Model(EvidenceModel):
            def complete(self,messages):
                root=json.loads(super().complete(messages));base=root['topics'][0]
                root['topics']=[dict(base,title=f'{asset}的个人分析',asset=asset,section='stock_opinion',
                    sourceMessageIds=[str(index+1)],heat='高') for index,asset in enumerate(['AAA','BBB','CCC','DDD','EEE'])]
                root['topics'] += [dict(base,title='产品消息',section='news',sourceMessageIds=['6'],heat='中'),
                                   dict(base,title='旅行闲聊',section='chat',sourceMessageIds=['7'],heat='低')]
                return json.dumps(root,ensure_ascii=False)
        rows=[message(str(index),'a',f'第{index}条合成发言。') for index in range(1,8)]
        result=generate_report(Model(),self.scope,dict(kind='daily'),rows,[],'compact',profile=self.profile)
        for digest in result['presentations'].values():
            self.assertEqual(len(digest['topics']),7)
            self.assertEqual({topic['section'] for topic in digest['topics']},{'stock_opinion','news','chat'})
            self.assertEqual(digest['summaryStats']['topicCount'],7)
            self.assertEqual(digest['sectionMeta']['topics']['hiddenCount'],0)

    def test_profile_member_input_is_labeled_as_filtered_member_work_not_whole_channel(self):
        request=dict(kind='member',member='a',topic='本频道全部讨论')
        scoped=prompt(self.scope,request,self.rows[:1],[],self.profile)
        boundary=json.loads(scoped[1]['content'].split('DISCORD_REPORT_SCOPE\n',1)[1])
        self.assertIn('逐标的',boundary['request']['topic'])
        self.assertIn('已按成员筛选',scoped[1]['content'])
        self.assertNotIn('本频道全部讨论',boundary['request']['topic'])
        self.assertNotIn('1-4 个主要话题',scoped[1]['content'])
        self.assertIn('另按实际内容整理消息和闲聊',scoped[1]['content'])
        ordinary=prompt(self.scope,request,self.rows[:1],[])
        original_boundary=json.loads(ordinary[1]['content'].split('DISCORD_REPORT_SCOPE\n',1)[1])
        self.assertEqual(original_boundary['request']['topic'],'本频道全部讨论')
        self.assertNotIn('已按成员筛选',ordinary[1]['content'])


if __name__=='__main__': unittest.main()
