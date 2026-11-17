import copy
import http.client
import json
import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from grouppulse.live import LiveApp
from grouppulse.network import ServiceError
from test_live import MemoryVault
from test_reference_questions import ToolModel


class Model(ToolModel):
    def __init__(self): super().__init__(); self.calls=0;self.report_requests=[]
    def complete_tools(self,*args,**kwargs):
        self.calls+=1
        return super().complete_tools(*args,**kwargs)
    def complete(self,messages):
        self.calls+=1
        self.report_requests.append(copy.deepcopy(messages))
        text=messages[-1]['content']
        ids=re.findall(r'^\[([^\]]+)\] \d{4}-\d{2}-\d{2} ',text,flags=re.M)
        boundary=json.loads(text.split('DISCORD_REPORT_SCOPE\n',1)[1])
        return json.dumps(dict(overview='保留了成员聊天内容。',overviewSourceMessageIds=ids,
            hero=dict(headline='聊天内容',summary='保留各人意见。',summarySourceMessageIds=ids),
            topics=[dict(title='活动意见',summary='保留各人的原话和不同意见。',sourceMessageIds=ids,
                         participants=[row['label'] for row in boundary['authors']],conclusions=[])],todos=[],unresolved=[]))


class AccountDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'live.sqlite3'
        self.vault=MemoryVault();self.vault.set('exporter-user','personal-local-only')
        self.vault.set('model','local-model-only')
        self.app=LiveApp(self.path,vault=self.vault);self.app.model=Model()
        self.app.save_config(dict(base_url='https://unused.invalid/v1',model='test',api_key='local-model-only'))
        self.connect()
        self.rows=[dict(id=str(500+i),timestamp=f'2026-10-20T09:0{i}:00+00:00',type='Default',content=text,
                       author=dict(id='901' if i==0 else '902',name='同名',isBot=False))
                   for i,text in enumerate(['我认为活动可以延后。','我反对直接发布。'])]
        self.batch=self.ingest()
    def tearDown(self): self.tmp.cleanup()
    def connect(self,person='900'):
        self.app.save_meta('exporter-account',dict(enabled=True,has_token=True,status='connected',user_id=person,username='本人',
            servers={'100':'个人服务器'},channels={'100':{'200':'频道','201':'另一个频道'}}))
    def ingest(self,rows=None,day='2026-10-20'):
        origin=dict(kind='account-export',account_id=self.app.exporter.state()['user_id'],timezone='Asia/Shanghai',date=day)
        raw=dict(guild=dict(id='100',name='个人服务器'),channel=dict(id='200',name='频道',type='GuildTextChat'),messages=copy.deepcopy(self.rows if rows is None else rows))
        item=self.app.file_imports.add(dict(filename='频道.json',content=json.dumps(raw,ensure_ascii=False)),origin=origin)
        self.app.account_data.ingest(item)
        return item
    def reader(self,person='900',zone='Asia/Shanghai'):
        return self.app.account_data.reader('100','200',person,zone)
    def ask(self,reader=None,**extra):
        reader=reader or self.reader()
        with patch.object(reader,'ensure_synced',return_value=None):
            return reader.ask('活动有哪些意见？','100','200','2026-10-20','2026-10-20',**extra)

    def test_original_query_memory_and_history_survive_export_batch_and_restart(self):
        reader=self.reader(); first=self.ask(reader)
        self.assertEqual(first['generation']['runtime'],'copied-starmemo-query-agent')
        self.assertEqual({row['author_id'] for row in first['messages']},{'901','902'})
        second=self.ingest([*self.rows,dict(self.rows[0],id='599',content='补充：我只是提出看法。')])
        self.assertNotEqual(second['id'],self.batch['id'])
        restarted=LiveApp(self.path,vault=self.vault);restarted.model=self.app.model
        restored=restarted.account_data.reader('100','200','900')
        self.assertEqual(restored.path,reader.path)
        self.assertEqual(len(restored.conversation(first['conversation_id'],'100','200')['turns']),1)
        later=self.ask(restored,conversation_id=first['conversation_id'])
        self.assertTrue(later['metrics']['memory_refreshed'])
        self.assertEqual(len(restored.conversations('100','200')['items']),1)
        self.assertIsNone(self.app.message('500'))
        with self.app.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM conversations').fetchone()[0],0)

    def test_account_switch_scope_and_old_import_records_are_isolated(self):
        old=self.reader(); first=self.ask(old)
        self.connect('999')
        for operation in [lambda:old.read_window('200',*old.window('2026-10-20')),
                          lambda:self.reader(),lambda:self.app.file_imports.saved(self.batch['id'],'100','200','a'*32)]:
            with self.assertRaises(ValueError):operation()
        self.assertEqual(self.app.file_imports.items()['items'],[])
        fresh=self.reader('999')
        self.assertNotEqual(old.path,fresh.path)
        self.assertEqual(fresh.conversations('100','200')['items'],[])
        with self.assertRaises(ValueError):fresh.conversation(first['conversation_id'],'100','200')
        with self.assertRaises(ValueError):self.app.account_data.reader('101','200','999')
        with self.assertRaises(ValueError):self.app.account_data.reader('100','202','999')

    def test_server_profile_reaches_existing_report_path_without_changing_other_report_kinds_or_history(self):
        reader=self.reader()
        old=reader.report(server='100',channel='200',date='2026-10-20',_synced=True)
        self.app.save_meta('report-profiles',{'100':dict(id='us-stock-chat',version=1,server='100',account_id='900',instructions='合成专属背景：生活与观点同等保留。')})
        for kind in ('daily','member','weekly','discussion'):
            before=len(self.app.model.report_requests)
            report=reader.report(server='100',channel='200',date='2026-10-20',kind=kind,member='901',topic='活动',_synced=True)
            self.assertEqual(len(self.app.model.report_requests)-before,1)
            self.assertEqual('report_profile' in report,kind in ('daily','member'))
            self.assertEqual('合成专属背景' in self.app.model.report_requests[-1][0]['content'],kind in ('daily','member'))
            if kind=='member': self.assertEqual({row['author_id'] for row in report['messages']},{'901'})
        with reader.db() as db:
            saved=json.loads(db.execute('SELECT body FROM report_snapshots WHERE id=?',(old['report_id'],)).fetchone()['body'])
        self.assertEqual(saved,old)
        self.assertNotIn('report_profile',saved)

    def test_complete_reexport_removes_deleted_evidence_without_deleting_history(self):
        reader=self.reader();first=self.ask(reader)
        self.ingest(self.rows[1:])
        rows=reader.read_window('200',*reader.window('2026-10-20'))
        self.assertEqual([row['id'] for row in rows],['501'])
        self.assertIsNone(reader.message('500'))
        self.assertEqual(len(reader.conversation(first['conversation_id'],'100','200')['turns'][0]['response']['messages']),2)
        self.assertEqual(len(self.app.file_imports.reader(self.batch['id']).read_window('200',*reader.window('2026-10-20'))),2)

    def test_activity_reuses_raw_counts_and_personal_coverage_without_bot_access(self):
        reader=self.reader()
        with patch.object(reader,'ensure_synced',return_value=None),patch.object(self.app,'sync_channel',side_effect=AssertionError('不能访问Bot')):
            result=reader.report(server='100',channel='200',kind='activity',date='2026-10-20')
        self.assertEqual(result['metrics']['messages'],2)
        self.assertTrue(result['coverage']['window_complete'])
        self.assertFalse(result['comparison']['coverage']['window_complete'])
        self.assertEqual(self.app.model.calls,0)

    def test_personal_sync_and_daily_tasks_start_disabled(self):
        reader=self.reader()
        self.assertFalse(self.app.account_data.state('100','200','900')['sync']['enabled'])
        task=reader.daily_tasks.save(dict(server='100',channel='200',members=['901','902']))
        self.assertFalse(task['enabled'])
        self.assertFalse(reader.daily_tasks.tick(datetime(2026,10,23,tzinfo=timezone.utc)))
        self.assertEqual(self.app.model.calls,0)

    def test_timezone_daily_generation_reuses_report_core_and_fixed_member_ids(self):
        reader=self.reader(zone='America/Los_Angeles')
        task=reader.daily_tasks.save(dict(server='100',channel='200',members=['901','902'],timezone='America/Los_Angeles',enabled=True,instructions='只整理闲聊、资讯和观点，保留不同意见。'))
        self.assertEqual(task['timezone'],'America/Los_Angeles')
        self.assertEqual(reader.daily_tasks.due(dict(task,next_day='2026-10-20')).isoformat(),'2026-10-21T00:10:00-07:00')
        start,end=reader.window('2026-10-20')
        with reader.db() as db:db.execute('INSERT OR REPLACE INTO account_windows VALUES (?,?,?)',(start.isoformat(),end.isoformat(),datetime.now(timezone.utc).isoformat()))
        run=reader.daily_tasks.queue(dict(server='100',channel='200',date='2026-10-20'))
        reader.daily_tasks.save(dict(server='100',channel='200',members=['901','902'],timezone='America/Los_Angeles',enabled=True,instructions='此修改只用于之后新建的任务。'))
        with patch('grouppulse.account_data.AccountApp.ensure_synced',return_value=None):reader.daily_tasks.tick()
        state=reader.daily_tasks.state('100','200')
        saved=next(row for row in state['runs'] if row['id']==run['id'])
        self.assertEqual(saved['status'],'complete',saved.get('note'))
        for step in saved['steps']:
            report=reader.daily_tasks.report('100','200',step['report_id'])
            self.assertEqual(report['generation']['core'],'starmemo-original-js')
            self.assertEqual(report['window']['timezone'],'America/Los_Angeles')
            if step['member']:self.assertTrue(all(row['author_id']==step['member'] for row in report['messages']))
        self.assertTrue(self.app.model.report_requests)
        for messages in self.app.model.report_requests:
            scope=json.loads(messages[-1]['content'].split('DISCORD_REPORT_SCOPE\n',1)[1])['scope']
            self.assertEqual(scope['instructions'],'只整理闲聊、资讯和观点，保留不同意见。')

    def test_fixed_instructions_are_plain_bounded_text_and_persist_without_model_calls(self):
        reader=self.reader()
        for value in [123,'字'*2001]:
            with self.assertRaises(ValueError):reader.daily_tasks.save(dict(server='100',channel='200',members=[],instructions=value))
        saved=reader.daily_tasks.save(dict(server='100',channel='200',members=['901'],instructions='保留原话条件和时间；不要展开黑话。'))
        restored=self.reader().daily_tasks.state('100','200')['task']
        self.assertEqual(restored['instructions'],saved['instructions'])
        self.assertFalse(restored['enabled'])
        self.assertEqual(self.app.model.calls,0)

    def test_existing_keyword_rules_are_checked_on_new_personal_read(self):
        reader=self.reader();reader.save_alert_rule(dict(server='100',channel='200',keyword='分享'))
        row=dict(self.rows[0],id='699',timestamp=datetime.now(timezone.utc).isoformat(),content='刚刚分享了一则消息。')
        self.ingest([*self.rows,row])
        self.assertEqual(len(reader.alerts('200','100')),1)
        self.ingest([*self.rows,row])
        self.assertEqual(len(reader.alerts('200','100')),1)

    def test_http_routes_use_explicit_account_and_keep_history_and_configuration_local(self):
        from grouppulse.server import create_server
        server=create_server(self.app,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,data=None):
            client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            client.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,
                           {'Content-Type':'application/json','X-GroupPulse':'local'})
            response=client.getresponse();body=json.loads(response.read());client.close();return response.status,body
        scope=dict(source='account',account_id='900',server='100',channel='200')
        query='source=account&account_id=900&server=100&channel=200'
        try:
            with patch.object(self.app.exporter,'prepare',side_effect=AssertionError('查看/保存不能请求Discord')):
                status,state=request('/api/account-data?'+query)
                self.assertEqual(status,200);self.assertEqual(state['members'],{'901':'同名','902':'同名'})
                self.assertFalse(state['sync']['enabled'])
                self.assertEqual(request('/api/daily-task',dict(scope,members=['901'],instructions='只摘录观点。',enabled=False))[0],200)
                self.assertEqual(request('/api/account-sync',dict(scope,enabled=False))[0],200)
                self.assertEqual(request('/api/alert-rules',dict(scope,keyword='活动'))[0],200)
                self.assertEqual(request('/api/conversations?'+query)[1]['items'],[])
                self.assertEqual(self.app.model.calls,0)
            with patch('grouppulse.account_data.AccountApp.ensure_synced',return_value=None):
                status,answer=request('/api/questions',dict(scope,question='活动有哪些意见？',start_date='2026-10-20',end_date='2026-10-20'))
                self.assertEqual(status,200,answer)
                self.assertEqual(answer['generation']['runtime'],'copied-starmemo-query-agent')
                status,activity=request('/api/report?'+query+'&kind=activity&date=2026-10-20')
                self.assertEqual(status,200);self.assertEqual(activity['metrics']['messages'],2)
            self.assertEqual(request('/api/conversation?'+query+'&id='+answer['conversation_id'])[0],200)
            self.assertEqual(request('/api/account-data?'+query.replace('account_id=900','account_id=999'))[0],400)
            self.assertEqual(request('/api/questions',dict(scope,account_id='999',question='不能读其他账号'))[0],400)
            self.assertEqual(request('/api/questions',dict(scope,account_id='',question='不能退回Bot'))[0],400)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_same_json_from_different_account_never_reuses_the_other_account_batch(self):
        self.connect('999')
        item=self.ingest()
        self.assertNotEqual(item['id'],self.batch['id'])
        self.assertEqual(self.app.file_imports.items()['items'][0]['origin']['account_id'],'999')
        self.assertEqual(len(self.reader('999').read_window('200',*self.reader('999').window('2026-10-20'))),2)

    def test_conflicting_stable_author_is_rejected_before_merging(self):
        wrong=copy.deepcopy(self.rows);wrong[0]['author']['id']='903'
        with self.assertRaisesRegex(ValueError,'身份.*冲突'):self.ingest(wrong)
        self.assertEqual(self.reader().message('500')['author_id'],'901')

    def test_exported_avatar_uses_shared_discord_url_validation(self):
        valid='https://cdn.discordapp.com/avatars/901/'+'a'*32+'.png?size=128'
        rows=copy.deepcopy(self.rows);rows[0]['author']['avatarUrl']=valid
        rows[1]['author']['avatarUrl']='https://example.invalid/track?token=private'
        item=self.ingest(rows)
        reader=self.app.file_imports.reader(item['id'])
        self.assertEqual(reader.message('500')['avatar_url'],valid)
        self.assertEqual(reader.message('501')['avatar_url'],'')

    def test_first_question_response_matches_restored_snapshot_exactly(self):
        first=self.ask()
        restored=self.reader().conversation(first['conversation_id'],'100','200')['turns'][-1]['response']
        self.assertEqual(restored,first)
        self.assertEqual(restored['scope']['source'],'account')
        self.assertEqual(restored['scope']['account_id'],'900')

    def test_confirmed_permission_loss_hides_current_evidence_and_partial_recovery_is_bounded(self):
        reader=self.reader();first=self.ask(reader)
        reader.save_alert_rule(dict(server='100',channel='200',keyword='分享'))
        recent=dict(self.rows[0],id='699',timestamp=datetime.now(timezone.utc).isoformat(),content='刚刚分享了消息。')
        self.ingest([*self.rows,recent])
        self.assertEqual(reader.alerts('200','100')[0]['status'],'active')
        self.app.account_data.configure_sync(dict(server='100',channel='200',account_id='900',enabled=True))
        with patch.object(self.app.exporter,'prepare',side_effect=ServiceError('权限已失效','http',403)):
            from datetime import timedelta
            self.app.account_data.tick(datetime.now(timezone.utc)+timedelta(minutes=2))
        self.assertEqual(reader.sync_state('200')['status'],'failed')
        event=reader.alerts('200','100')[0]
        self.assertEqual(event['status'],'source_unavailable')
        self.assertEqual(event['messages'],[])
        self.assertIsNone(reader.message('500'))
        self.assertFalse(reader.covered('200',*reader.window('2026-10-20')))
        self.assertEqual(reader.conversation(first['conversation_id'],'100','200')['turns'][-1]['response'],first)
        raw=dict(guild=dict(id='100',name='个人服务器'),channel=dict(id='200',name='频道',type='GuildTextChat'),messages=[self.rows[0]])
        origin=dict(kind='account-export',account_id='900',timezone='Asia/Shanghai',date='2026-10-20',
                    start_time='2026-10-20T17:00',end_time='2026-10-20T17:01')
        narrow=self.app.file_imports.add(dict(filename='恢复窗口.json',content=json.dumps(raw)),origin=origin)
        self.app.account_data.ingest(narrow)
        self.assertIsNotNone(reader.message('500'))
        self.assertIsNone(reader.message('501'))
        self.assertIsNone(reader.message('699'))
        self.assertFalse(reader.covered('200',*reader.window('2026-10-20')))
        self.assertEqual(reader.alerts('200','100')[0]['status'],'source_unavailable')

    def test_transient_network_failure_keeps_current_evidence_and_old_coverage(self):
        from datetime import timedelta
        reader=self.reader()
        self.app.account_data.configure_sync(dict(server='100',channel='200',account_id='900',enabled=True))
        for error in [ServiceError('连接暂不可用','connection'),ServiceError('服务器暂不可用','http',503)]:
            with patch.object(self.app.exporter,'prepare',side_effect=error):
                self.app.account_data.tick(datetime.now(timezone.utc)+timedelta(minutes=3))
            self.assertEqual(reader.sync_state('200')['status'],'failed')
            self.assertIsNotNone(reader.message('500'))
            self.assertTrue(reader.covered('200',*reader.window('2026-10-20')))


if __name__=='__main__':unittest.main()
