import json
import re
import http.client
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from grouppulse.live import LiveApp
from grouppulse.discord import DiscordClient, snowflake


class SyncFixtureClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 21, 4, tzinfo=timezone.utc).astimezone(tz)


class MemoryVault:
    storage = 'test-memory'
    def __init__(self): self.values = {}
    def get(self, key): return self.values.get(key, '')
    def set(self, key, value): self.values[key] = value
    def delete(self, key): self.values.pop(key, None)


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls, self.model_inputs = [], []
        self.model_failure = ''
        self.history_failure = False
        self.threads, self.thread_failures = [], {}
        self.alter_response = None
        self.rows = [dict(id=str(snowflake('2026-10-20T09:00:00+00:00') + i), channel_id='200',
            timestamp=f'2026-10-20T09:0{i}:00+00:00', content=text,
            author=dict(id='901' if i != 1 else '902', username='小林' if i != 1 else '小禾', bot=False),
            attachments=[], embeds=[], type=0,
            message_reference=dict(message_id=str(snowflake('2026-10-20T09:00:00+00:00')), channel_id='200') if i else None)
            for i, text in enumerate(['我们先检查恢复流程。', '我反对直接发布。', '决定：先演练再发布。'])]
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def reply(self, value, status=200):
                body = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                path = urlsplit(self.path).path
                owner.calls.append(('GET', path))
                if self.headers.get('Authorization') != 'Bot test-discord-token':
                    return self.reply(dict(message='private-test-discord-token'), 401)
                if path == '/users/@me': return self.reply(dict(id='900', username='只读助手', bot=True))
                if path == '/applications/@me': return self.reply(dict(id='900', flags=1 << 19))
                if path == '/users/@me/guilds': return self.reply([dict(id='100', name='测试服务器')])
                if path == '/guilds/100/roles': return self.reply([dict(id='100', permissions=str((1 << 10) | (1 << 16)))])
                if path == '/guilds/100/members/900': return self.reply(dict(roles=[]))
                if path == '/guilds/100/threads/active':
                    return self.reply(dict(threads=[t for t in owner.threads if not t.get('thread_metadata',{}).get('archived')]))
                if path in ('/channels/200/threads/archived/public','/channels/200/users/@me/threads/archived/private'):
                    private = '/@me/' in path
                    items = [t for t in owner.threads if t.get('parent_id')=='200' and t.get('thread_metadata',{}).get('archived')
                             and (t['type']==12 if private else t['type'] in (10,11))]
                    cursor = lambda t: int(t['id']) if private else t['thread_metadata']['archive_timestamp']
                    before = parse_qs(urlsplit(self.path).query).get('before',[None])[0]
                    if before is not None: items=[t for t in items if cursor(t)<(int(before) if private else before)]
                    items.sort(key=cursor,reverse=True)
                    return self.reply(dict(threads=items[:100],has_more=len(items)>100))
                channel = dict(id='200', guild_id='100', name='产品频道', type=0, permission_overwrites=[])
                if path == '/guilds/100/channels':
                    return self.reply([channel, dict(channel, id='201', name='不可读频道', permission_overwrites=[dict(id='100', type=0, allow='0', deny=str(1 << 16))])])
                if path == '/channels/200': return self.reply(channel)
                thread = next((t for t in owner.threads if path==f'/channels/{t["id"]}'),None)
                if thread: return self.reply(thread)
                actual_channel = next((c for c in ['200',*[t['id'] for t in owner.threads]] if path==f'/channels/{c}/messages'),None)
                if actual_channel:
                    if owner.history_failure: return self.reply(dict(message='private-context'), 403)
                    if actual_channel in owner.thread_failures: return self.reply({},owner.thread_failures[actual_channel])
                    before = int(parse_qs(urlsplit(self.path).query)['before'][0])
                    return self.reply(sorted([r for r in owner.rows if r['channel_id']==actual_channel and int(r['id']) < before], key=lambda r:int(r['id']), reverse=True)[:100])
                if re.fullmatch(r'/channels/[0-9]+/messages/[0-9]+',path):
                    row = next((r for r in owner.rows if r['id']==path.rsplit('/',1)[1] and r['channel_id']==path.split('/')[2]),None)
                    return self.reply(row or {}, 200 if row else 404)
                return self.reply({}, 404)
            def do_POST(self):
                path = urlsplit(self.path).path
                owner.calls.append(('POST', path))
                self.assert_model_path = path == '/v1/chat/completions'
                if not self.assert_model_path: return self.reply({}, 403)
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.model_inputs.append(data)
                if owner.model_failure == 'auth': return self.reply(dict(error=dict(message='private-model-token')), 401)
                if owner.model_failure == 'length': return self.reply(dict(choices=[dict(message=dict(content='{}'), finish_reason='length')]))
                text = data['messages'][-1]['content']
                if data.get('tools') and data['messages'][-1]['role']=='user':
                    call=dict(id='native-call',type='function',function=dict(name='query_messages',
                        arguments=json.dumps(dict(timeRange=dict(kind='all'),temporalBasis=dict(kind='none')))))
                    return self.reply(dict(choices=[dict(message=dict(role='assistant',content=None,
                        reasoning_content='读取当前频道证据。',tool_calls=[call]),finish_reason='tool_calls')]))
                if data['messages'][-1]['role']=='tool':
                    payload=json.loads(text)
                    rows=payload.get('messages',payload.get('evidence',[]))
                    opposed=next(row for row in rows if '反对' in row['text'])
                    return self.reply(dict(choices=[dict(message=dict(content='小禾反对直接发布。['+opposed['citationId']+']'),finish_reason='stop')]))
                if 'DISCORD_REPORT_SCOPE\n' in text:
                    payload = json.loads(text.split('DISCORD_REPORT_SCOPE\n',1)[1])
                    selected = re.findall(r'^\[([^\]]+)\] \d{4}-\d{2}-\d{2} ', text, flags=re.M)
                    rows = [row for row in owner.rows if row['id'] in selected]
                    labels={person['userId']:person['label'] for person in payload['authors']}
                    value = dict(overview='模型返回的真实接口摘要', overviewSourceMessageIds=[r['id'] for r in rows],
                        hero=dict(headline='恢复与发布',summary='先演练；保留不同意见。',summarySourceMessageIds=[r['id'] for r in rows]),
                        topics=[dict(title='恢复与发布', summary='先演练；有人反对直接发布。', heat='高',
                                     sourceMessageIds=[r['id'] for r in rows], summarySourceMessageIds=[rows[-1]['id']],
                                     participants=list(dict.fromkeys(labels[r['author']['id']] for r in rows)),
                                     conclusions=[dict(text=r['content'],sourceMessageIds=[r['id']]) for r in rows if '决定' in r['content']])],
                        importantMessages=[],unresolved=[],todos=[],keywords=['发布'])
                elif 'INPUT_JSON\n' in text:
                    rows = json.loads(text.split('INPUT_JSON\n',1)[1])['input']['messages']
                    value = dict(status='answered', answer='小禾反对直接发布。', answerSourceMessageIds=[r['id'] for r in rows],
                        points=[dict(text=r['content'], userId=r['author_id'], sourceMessageIds=[r['id']], contextMessageIds=[]) for r in rows])
                else:
                    return self.reply(dict(choices=[dict(message=dict(content='连接正常'), finish_reason='stop')]))
                if owner.alter_response: owner.alter_response(value)
                return self.reply(dict(choices=[dict(message=dict(content=json.dumps(value,ensure_ascii=False)), finish_reason='stop')]))
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.vault = MemoryVault()
        self.path = Path(self.tmp.name) / 'live.sqlite3'
        self.app = LiveApp(self.path, vault=self.vault, discord_factory=lambda token:DiscordClient(token, base_url=self.url))

    def tearDown(self):
        self.app.auto_sync.stop()
        if self.app.auto_sync.thread: self.app.auto_sync.thread.join(timeout=5)
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def configure(self):
        self.app.save_config(dict(base_url=self.url+'/v1', model='configured-model', api_key='test-model-token'))
        bot = self.app.save_bot(dict(name='只读助手', token='test-discord-token'))
        self.app.connect_bot(bot['id'])
        self.app.discover_channels(bot['id'], '100')
        return bot

    def add_thread(self, identifier='300', parent='200', archived=False, private=False):
        value=dict(id=identifier,guild_id='100',parent_id=parent,type=12 if private else 11,name='线程讨论',
            thread_metadata=dict(archived=archived,archive_timestamp='2026-10-20T12:00:00+00:00'))
        self.threads.append(value)
        return value

    def test_subthreads_keep_actual_source_and_never_read_another_parent(self):
        bot=self.configure()
        self.add_thread();self.add_thread('301',archived=True);self.add_thread('302',private=True,archived=True)
        self.add_thread('400',parent='201')
        for i,channel in enumerate(('300','301','302','400')):
            self.rows.append(dict(self.rows[0],id=str(int(self.rows[-1]['id'])+1),channel_id=channel,content=f'线程 {channel} 的文字',message_reference=None))
        result=self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        self.assertTrue(result['threads_complete'])
        start,end=self.app.window('2026-10-20')
        rows=self.app.read_window('200',start,end)
        self.assertEqual(len(rows),6)
        self.assertEqual({r.get('source_channel','200') for r in rows},{'200','300','301','302'})
        self.assertTrue(all(r['channel']=='200' and r['server']=='100' for r in rows))
        self.assertTrue(all('/'+r['source_channel']+'/' in r['url'] for r in rows if r.get('source_channel')))
        self.assertNotIn(('GET','/channels/400/messages'),self.calls)
        self.assertTrue(self.app.covered('200',start,end))
        report=self.app.report(date='2026-10-20',channel='200',server='100')
        self.assertEqual(report['metrics']['messages'],6)
        self.assertEqual({row.get('source_channel','200') for row in report['messages']},{'200','300','301','302'})

    def test_subthread_pagination_resumes_and_incremental_is_per_thread(self):
        bot=self.configure();self.add_thread()
        for i in range(105):
            self.rows.append(dict(self.rows[0],id=str(int(self.rows[0]['id'])+1000+i),channel_id='300',message_reference=None))
        result=self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20',max_pages=2)
        self.assertEqual(result['status'],'partial')
        for _ in range(5):
            if result['status']=='complete':break
            result=self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20',max_pages=2,resume=True)
        self.assertEqual(result['status'],'complete')
        self.assertTrue(result['threads_complete'])
        # A thread watermark must not hide a new parent message with a lower ID.
        parent=dict(self.rows[0],id=str(int(self.rows[0]['id'])+500),content='父频道新增')
        child=dict(self.rows[0],id=str(int(self.rows[0]['id'])+2000),channel_id='300',content='旧线程新增')
        self.rows.extend([parent,child])
        self.app.auto_sync.select('100','200')
        self.app.auto_sync.tick()
        self.assertEqual(self.app.message(parent['id'])['content'],'父频道新增')
        self.assertEqual(self.app.message(child['id'])['content'],'旧线程新增')

    def test_unchanged_thread_watermark_avoids_fetching_old_history_on_every_tick(self):
        bot=self.configure();thread=self.add_thread()
        child=dict(self.rows[0],id=str(int(self.rows[-1]['id'])+1),channel_id='300',message_reference=None)
        self.rows.append(child);thread['last_message_id']=child['id']
        self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        before=self.calls.count(('GET','/channels/300/messages'))
        self.app.auto_sync.select('100','200');self.app.auto_sync.tick()
        self.assertEqual(self.calls.count(('GET','/channels/300/messages')),before)
        new=dict(child,id=str(int(child['id'])+1),content='老线程里的新增文字')
        self.rows.append(new);thread['last_message_id']=new['id']
        self.app.auto_sync.tick()
        self.assertEqual(self.app.message(new['id'])['content'],'老线程里的新增文字')
        self.assertGreater(self.calls.count(('GET','/channels/300/messages')),before)

    def test_unreadable_subthread_is_partial_and_old_text_is_not_reused(self):
        bot=self.configure();self.add_thread()
        child=dict(self.rows[0],id=str(int(self.rows[-1]['id'])+1),channel_id='300',content='曾经可读',message_reference=None)
        self.rows.append(child)
        self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        self.thread_failures['300']=403
        state=self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        self.assertEqual(state['status'],'complete')
        self.assertFalse(state['threads_complete'])
        start,end=self.app.window('2026-10-20')
        self.assertNotIn(child['id'],{r['id'] for r in self.app.read_window('200',start,end)})
        self.assertFalse(self.app.covered('200',start,end))
        self.assertIn('子线程',self.app.coverage('200',start,end,[])['note'])

    def test_coverage_uses_latest_overlapping_sync_instead_of_stale_exact_window(self):
        bot=self.configure()
        old=self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        start,end=self.app.window('2026-10-19',end_date='2026-10-21')
        legacy=dict(old,start=start.isoformat(),end=end.isoformat(),effective_end=end.isoformat(),
                    threads_complete=False,finished='2026-10-20T00:00:00+00:00')
        def save(state):
            with self.app.db() as db:
                db.execute('INSERT OR REPLACE INTO sync_windows VALUES (?,?,?,?,?)',
                    ('200',state['start'],state['end'],state['status'],json.dumps(state)))
        save(legacy)
        self.app.sync_channel(bot['id'],'100','200','2026-10-18','2026-10-21')
        coverage=self.app.coverage('200',start,end,[])
        self.assertTrue(coverage['window_complete'])
        self.assertEqual(coverage['status'],'complete')
        self.assertNotIn('子线程尚未补齐',coverage['note'])
        # Later work outside this window must not hide its latest permission failure.
        save(dict(legacy,finished='2026-10-22T00:00:00+00:00'))
        left,right=self.app.window('2026-10-17')
        save(dict(old,start=left.isoformat(),end=right.isoformat(),effective_end=right.isoformat()))
        self.assertFalse(self.app.covered('200',start,end))
        coverage=self.app.coverage('200',start,end,[])
        self.assertEqual(coverage['status'],'partial')
        self.assertIn('子线程尚未补齐',coverage['note'])

    def test_archived_lists_paginate_independently_and_keep_a_resumable_snapshot(self):
        bot=self.configure()
        for i in range(101):
            stamp=f'2026-10-20T12:{i//60:02d}:{i%60:02d}+00:00'
            for private,offset in ((False,1000),(True,2000)):
                thread=self.add_thread(str(offset+i),archived=True,private=private)
                thread['thread_metadata']['archive_timestamp']=stamp
        state=self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20',max_pages=3)
        self.assertEqual(state['status'],'partial')
        self.assertEqual(len(state['threads']),202)
        self.assertTrue(state['thread_scan']['public']['done'])
        self.assertTrue(state['thread_scan']['private']['done'])
        self.assertEqual(self.calls.count(('GET','/channels/200/threads/archived/public')),2)
        self.assertEqual(self.calls.count(('GET','/channels/200/users/@me/threads/archived/private')),2)
        self.assertTrue(state['parent_complete'])

    def test_replies_can_load_same_parent_thread_background_but_not_foreign_thread(self):
        bot=self.configure();self.add_thread();self.add_thread('400',parent='201')
        old=dict(self.rows[0],id=str(snowflake('2020-01-01T00:00:00+00:00')),channel_id='300',
            timestamp='2020-01-01T00:00:00+00:00',author=dict(id='903',username='原作者',bot=False),content='日期外引用背景')
        self.rows.append(old)
        self.rows[0]['message_reference']=dict(channel_id='300',message_id=old['id'])
        self.rows[1]['message_reference']=dict(channel_id='400',message_id='999')
        self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        start,end=self.app.window('2026-10-20')
        contexts={row['id']:row for row in self.app.contexts(self.app.read_window('200',start,end),'200')}
        self.assertEqual(contexts[old['id']]['status'],'available')
        self.assertEqual(contexts[old['id']]['message']['author_id'],'903')
        self.assertEqual(contexts[old['id']]['message']['source_channel'],'300')
        self.assertEqual(contexts['999']['status'],'missing')
        self.assertNotIn(('GET','/channels/400/messages/999'),self.calls)
        self.assertEqual(len(self.app.read_window('200',start,end)),3)

    def test_thread_scope_is_checked_again_before_history_and_foreign_payloads_are_rejected(self):
        bot=self.configure();self.add_thread()
        original=DiscordClient.get
        def changed(client,path,params=None):
            value=original(client,path,params)
            return dict(value,parent_id='201') if path=='/channels/300' else value
        with patch.object(DiscordClient,'get',changed), self.assertRaisesRegex(ValueError,'同步未完成'):
            self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        self.assertNotIn(('GET','/channels/300/messages'),self.calls)

    def test_saving_has_no_network_and_credentials_are_not_in_database_or_responses(self):
        self.app.save_config(dict(base_url=self.url+'/v1', model='configured-model', api_key='test-model-token'))
        bot = self.app.save_bot(dict(name='只读助手', token='test-discord-token'))
        self.assertEqual(self.calls, [])
        self.assertNotIn('test-discord-token', json.dumps(bot))
        self.assertNotIn(b'test-model-token', self.path.read_bytes())
        self.assertNotIn(b'test-discord-token', self.path.read_bytes())
        self.assertEqual(self.app.bootstrap()['servers'], {})
        result = self.app.check_config({})
        self.assertEqual(result['status'], 'connected')
        self.assertNotIn('群聊', self.model_inputs[-1]['messages'][-1]['content'])

    def test_discovery_sync_report_summaries_and_questions_use_http_provider(self):
        bot = self.configure()
        self.app.bootstrap('100','200',remember=False)
        self.assertEqual(self.app.meta('selection',{}),{})
        scope = self.app.bootstrap('100','200')
        self.assertEqual(scope['channels'], {'200':'产品频道'})
        self.assertFalse(scope['offline'])
        result = self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['messages'],3)
        self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        report = self.app.report(date='2026-10-20',server='100',channel='200',focus_member='901',focus_topic='发布')
        self.assertEqual(report['metrics']['messages'],3)
        self.assertEqual(report['fullDigest']['overview'],'模型返回的真实接口摘要')
        self.assertEqual(report['generation']['engine'],'openai-compatible')
        member = self.app.report(date='2026-10-20',server='100',channel='200',kind='member',member='901')
        self.assertTrue(all(r['author_id']=='901' for r in member['messages']))
        self.app.report(date='2026-10-20',server='100',channel='200',kind='weekly',end_date='2026-10-20')
        self.app.report(date='2026-10-20',server='100',channel='200',kind='discussion',topic='发布')
        answer = self.app.ask('谁反对发布？','100','200','2026-10-20','2026-10-20')
        self.assertIn('小禾反对直接发布。',answer['answer'])
        self.assertEqual(answer['points'][0]['userId'],'902')
        self.assertIn('reasoning_content',next(m for m in self.model_inputs[-1]['messages'] if m.get('tool_calls')))
        self.app.ask('为什么？','100','200','2026-10-20','2026-10-20',answer['conversation_id'])
        self.assertEqual(self.app.conversations('100','200')['items'][0]['id'],answer['conversation_id'])
        self.assertFalse(any(method!='GET' and path!='/v1/chat/completions' for method,path in self.calls))

    def test_pagination_permission_failure_and_no_silent_global_fallback(self):
        bot = self.configure()
        original = self.rows[0]
        self.rows = [dict(original,id=str(int(original['id'])+i)) for i in range(205)]
        result = self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        self.assertEqual(result['messages'],205)
        self.assertGreaterEqual(result['pages'],3)
        with self.assertRaises(ValueError): self.app.sync_channel(bot['id'],'100','201','2026-10-20','2026-10-20')
        self.history_failure=True
        with self.assertRaises(ValueError): self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        start,end=self.app.window('2026-10-20')
        self.assertFalse(self.app.covered('200',start,end))
        with self.assertRaises(ValueError): self.app.import_demo()
        with self.assertRaises(ValueError): self.app.simulate_delivery('anything','100','200')

    def test_model_errors_are_classified_and_never_fall_back_to_mock(self):
        bot=self.configure()
        self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        self.model_failure='auth'
        with self.assertRaisesRegex(ValueError,'密钥') as error:
            self.app.report(date='2026-10-20',server='100',channel='200')
        self.assertNotIn('private-model-token',str(error.exception))
        self.model_failure='length'
        with self.assertRaisesRegex(ValueError,'截断'):
            self.app.ask('发布决定？','100','200','2026-10-20','2026-10-20')

    def test_current_day_reports_synced_through_time_without_claiming_finished_day(self):
        from datetime import datetime
        from grouppulse.core import TZ
        bot=self.configure()
        today=datetime.now(TZ).date().isoformat()
        self.app.sync_channel(bot['id'],'100','200',today,today)
        start,end=self.app.window(today)
        coverage=self.app.coverage('200',start,end,[])
        self.assertTrue(coverage['up_to_date'])
        self.assertFalse(coverage['window_complete'])
        self.assertIn('尚未结束',coverage['note'])
        self.assertIn('北京时间',coverage['note'])

    def test_auto_sync_backfills_adds_messages_and_persists_pause_without_model_calls(self):
        clock = patch('grouppulse.auto_sync.datetime', SyncFixtureClock)
        clock.start()
        self.addCleanup(clock.stop)
        self.configure()
        auto=self.app.auto_sync
        state=auto.select('100','200')
        self.assertTrue(state['enabled'])
        with self.assertRaises(ValueError): auto.select('100','201')
        auto.tick()
        self.assertEqual(auto.status()['status'],'complete')
        start,end=self.app.window('2026-10-20')
        self.assertEqual(len(self.app.read_window('200',start,end)),3)
        added=dict(self.rows[-1],id=str(int(self.rows[-1]['id'])+1),content='新增：演练时间待定。')
        self.rows.append(added)
        auto.tick(); auto.tick()
        self.assertEqual(len(self.app.read_window('200',start,end)),4)
        self.assertFalse(self.model_inputs)
        self.assertTrue(all(method=='GET' for method,_path in self.calls))
        state=auto.select('100','200',False)
        self.assertEqual(state['status'],'paused')
        calls=len(self.calls); auto.tick()
        self.assertEqual(len(self.calls),calls)
        from grouppulse.auto_sync import AutoSync
        self.assertFalse(AutoSync(self.app).status()['enabled'])

    def test_auto_sync_resumes_pagination_and_cancels_paused_scope(self):
        clock = patch('grouppulse.auto_sync.datetime', SyncFixtureClock)
        clock.start()
        self.addCleanup(clock.stop)
        self.configure()
        original=self.rows[0]
        self.rows=[dict(original,id=str(int(original['id'])+i)) for i in range(205)]
        auto=self.app.auto_sync; auto.select('100','200')
        sync=self.app.sync_channel
        def one_page(*args,**kwargs):
            kwargs['max_pages']=1
            return sync(*args,**kwargs)
        with patch.object(self.app,'sync_channel',side_effect=one_page):
            self.assertTrue(auto.tick())
            self.assertEqual(auto.status()['status'],'partial')
            while auto.tick(): pass
        self.assertEqual(auto.status()['status'],'complete')
        start,end=self.app.window('2026-10-20')
        self.assertEqual(len(self.app.read_window('200',start,end)),205)
        self.assertEqual(auto.status()['messages'],205)
        class Tomorrow(datetime):
            @classmethod
            def now(cls,tz=None): return SyncFixtureClock.now(tz)+timedelta(days=1)
        calls=sum(path=='/channels/200/messages' for _method,path in self.calls)
        with patch('grouppulse.auto_sync.datetime',Tomorrow): auto.tick()
        self.assertEqual(sum(path=='/channels/200/messages' for _method,path in self.calls)-calls,1)
        def pause_on_page(*args,**kwargs):
            progress=kwargs['progress']
            def update(state):
                progress(state)
                if state['pages']: auto.select('100','200',False)
            kwargs['progress']=update
            return sync(*args,**kwargs)
        with patch.object(self.app,'sync_channel',side_effect=pause_on_page): auto.tick()
        self.assertEqual(auto.status()['status'],'paused')
        self.assertFalse(auto.status()['enabled'])

    def test_report_and_questions_fill_requested_dates_without_manual_sync(self):
        self.configure()
        report=self.app.report(date='2026-10-20',server='100',channel='200')
        self.assertEqual(report['metrics']['messages'],3)
        self.assertTrue(self.model_inputs)
        answer=self.app.ask('谁反对发布？','100','200','2026-10-20','2026-10-20')
        followup=self.app.ask('为什么？','100','200','2026-10-20','2026-10-20',answer['conversation_id'])
        self.assertEqual(followup['metrics']['memory_turns'],1)
        self.assertEqual(followup['status'],'answered')
        self.assertEqual(followup['generation']['runtime'],'copied-starmemo-query-agent')
        self.alter_response=None
        self.rows.append(dict(self.rows[-1],id=str(int(self.rows[-1]['id'])+1),content='补充：先确认恢复。'))
        refreshed=self.app.ask('还有什么要确认？','100','200','2026-10-20','2026-10-20',answer['conversation_id'])
        self.assertEqual(refreshed['conversation_id'],answer['conversation_id'])
        self.assertEqual(refreshed['metrics']['memory_turns'],0)
        self.assertTrue(refreshed['metrics']['memory_refreshed'])
        self.assertEqual(len(self.app.conversation(answer['conversation_id'],'100','200')['turns']),3)
        latest=self.app.ask('再说明一下？','100','200','2026-10-20','2026-10-20',answer['conversation_id'])
        self.assertEqual(latest['metrics']['memory_turns'],1)

    def test_background_worker_continues_without_browser_requests_and_recovers_after_failure(self):
        clock = patch('grouppulse.auto_sync.datetime', SyncFixtureClock)
        clock.start()
        self.addCleanup(clock.stop)
        self.configure()
        auto=self.app.auto_sync; auto.select('100','200')
        self.history_failure=True
        auto.start(interval=.05)
        deadline=time.monotonic()+5
        while auto.status()['status']!='failed' and time.monotonic()<deadline: time.sleep(.02)
        self.assertEqual(auto.status()['status'],'failed')
        self.history_failure=False
        deadline=time.monotonic()+5
        while auto.status()['status']!='complete' and time.monotonic()<deadline: time.sleep(.02)
        self.assertEqual(auto.status()['status'],'complete')
        self.rows.append(dict(self.rows[-1],id=str(int(self.rows[-1]['id'])+1),content='服务运行期间的新消息。'))
        start,end=self.app.window('2026-10-20')
        deadline=time.monotonic()+5
        while len(self.app.read_window('200',start,end))!=4 and time.monotonic()<deadline: time.sleep(.02)
        self.assertEqual(len(self.app.read_window('200',start,end)),4)
        self.assertFalse(self.model_inputs)

    def test_long_reports_are_batched_invalid_sources_rejected_and_deleted_cache_is_excluded(self):
        bot=self.configure()
        row=self.rows[0]
        self.rows=[dict(row,id=str(int(row['id'])+i),content='讨论发布与恢复演练。'*25) for i in range(25)]
        self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        self.app.save_config(dict(input_kib=16))
        report=self.app.report(date='2026-10-20',server='100',channel='200')
        self.assertGreater(report['generation']['batches'],1)
        self.assertEqual(len(report['messages']),25)
        self.assertTrue(all(len(json.dumps(r['messages'],ensure_ascii=False).encode())<=16384 for r in self.model_inputs))
        self.alter_response=lambda value:value['topics'][0]['sourceMessageIds'].append('outside-channel')
        self.app.save_config(dict(max_tokens=4096))
        repaired=self.app.report(date='2026-10-20',server='100',channel='200')
        self.assertNotIn('outside-channel',repaired['fullDigest']['topics'][0]['sourceMessageIds'])
        self.alter_response=None
        self.rows=self.rows[:-1]
        self.app.sync_channel(bot['id'],'100','200','2026-10-20','2026-10-20')
        start,end=self.app.window('2026-10-20')
        self.assertEqual(len(self.app.read_window('200',start,end)),24)

    def test_real_http_routes_require_explicit_generation_and_async_sync(self):
        from grouppulse.server import create_server
        server=create_server(self.app,0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,data=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            c.request('POST' if data is not None else 'GET',path,
                      body=json.dumps(data) if data is not None else None,
                      headers={'Content-Type':'application/json','X-GroupPulse':'local'})
            r=c.getresponse();value=json.loads(r.read());status=r.status;c.close()
            return status,value
        try:
            self.assertEqual(request('/api/bootstrap')[1]['version'],'live-1')
            self.assertEqual(self.calls,[])
            self.assertEqual(request('/api/report')[0],405)
            self.assertEqual(request('/api/sync',{})[0],400)
            self.assertEqual(request('/api/sync',dict(bot_id=[],server='100',channel='200',start_date='2026-10-20',end_date='2026-10-20'))[0],400)
            bot=self.configure()
            self.assertEqual(request('/api/auto-sync',dict(server='100',channel='201'))[0],400)
            self.assertEqual(request('/api/auto-sync',dict(server='100',channel='200',enabled='yes'))[0],400)
            self.assertEqual(request('/api/auto-sync',dict(server='100',channel='200'))[1]['channel'],'200')
            self.assertTrue(request('/api/auto-sync')[1]['enabled'])
            _,job=request('/api/sync',dict(bot_id=bot['id'],server='100',channel='200',start_date='2026-10-20',end_date='2026-10-20'))
            for _ in range(100):
                status,state=request('/api/sync?id='+job['id'])
                if state['status']!='running': break
                time.sleep(.02)
            self.assertEqual(state['status'],'complete')
            status,report=request('/api/report',dict(server='100',channel='200',date='2026-10-20'))
            self.assertEqual(status,200)
            self.assertEqual(report['generation']['engine'],'openai-compatible')
            calls=len(self.model_inputs)
            status,statistics=request('/api/report?server=100&channel=200&kind=activity&date=2026-10-20')
            self.assertEqual(status,200);self.assertEqual(statistics['metrics']['messages'],3)
            self.assertEqual(len(self.model_inputs),calls)
            status,topics=request('/api/topic-trends',dict(server='100',channel='200',date='2026-10-20'))
            self.assertEqual(status,200);self.assertEqual(topics['topic_analysis']['topics'][0]['current'],3)
            calls=len(self.model_inputs)
            self.assertTrue(request('/api/report?server=100&channel=200&kind=activity&date=2026-10-20')[1]['topic_analysis']['cached'])
            self.assertEqual(len(self.model_inputs),calls)
            with patch('grouppulse.local_alerts.utcnow',return_value='2026-10-20T09:00:00+00:00'):
                status,rule=request('/api/alert-rules',dict(server='100',channel='200',keyword='发布'))
                self.assertEqual(status,200)
                self.assertEqual(request('/api/alerts?server=100&channel=200')[1]['alerts'],[])
                fresh=dict(self.rows[0],id=str(snowflake('2026-10-20T09:04:00+00:00')),timestamp='2026-10-20T09:04:00+00:00',content='发布准备新增确认')
                self.rows.append(fresh)
                request('/api/report?server=100&channel=200&kind=activity&date=2026-10-20')
                alerts=request('/api/alerts?server=100&channel=200')[1]
                self.assertEqual(len(alerts['alerts']),1)
                self.assertEqual(alerts['alerts'][0]['sourceMessageIds'],[fresh['id']])
                request('/api/report?server=100&channel=200&kind=activity&date=2026-10-20')
                self.assertEqual(len(request('/api/alerts?server=100&channel=200')[1]['alerts']),1)
                self.assertEqual(len(self.model_inputs),calls)
            self.app.save_config(dict(json_mode=True))
            self.app.model.complete_text([dict(role='user',content='原函数只返回编号')])
            self.assertNotIn('response_format',self.model_inputs[-1])
            self.assertEqual(request('/api/topic-trends',dict(server='100',channel='201',date='2026-10-20'))[0],400)
            self.assertEqual(request('/api/deliveries/simulate',dict(preview_id='x',server='100',channel='200'))[0],400)
        finally:
            server.shutdown();server.server_close();thread.join()
