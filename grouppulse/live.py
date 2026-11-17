"""真实接入业务层；数据独立落库，Discord仅只读同步。"""

import json
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from .core import App, DEFAULT_CONFIG, TZ
from .credentials import CredentialVault
from .discord import DiscordClient, normalize, snowflake
from .generation import GenerationError
from .layouts import DEFAULT_LAYOUT, LAYOUTS
from .live_generation import generate_report
from .network import ChatModel, ServiceError
from .auto_sync import AutoSync
from .subthreads import sync_subthreads
from .daily_tasks import DailyTasks
from .time_ranges import minute_window, describe_window

LIVE_CONFIG = {**DEFAULT_CONFIG, 'timeout':300, 'max_tokens':8192, 'input_kib':48,
               'json_mode':False, 'output_parameter':'auto'}


class LiveApp(App):
    live = True

    def __init__(self, db_path, vault=None, discord_factory=None):
        super().__init__(db_path)
        self.vault = vault if vault is not None else CredentialVault(self.path)
        self.discord_factory = discord_factory or DiscordClient
        self.sync_lock, self.generation_lock = threading.RLock(), threading.Lock()
        self.jobs = {}
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS live_meta (key TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sync_windows (
                    channel TEXT, start TEXT, end TEXT, status TEXT, body TEXT,
                    PRIMARY KEY(channel,start,end));
                CREATE TABLE IF NOT EXISTS context_failures (
                    channel TEXT, message_id TEXT, reason TEXT, PRIMARY KEY(channel,message_id));
                CREATE TABLE IF NOT EXISTS activity_topics (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS topic_classifications (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS activity_topic_ids (id TEXT PRIMARY KEY, server TEXT, channel TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS alert_rules (id TEXT PRIMARY KEY, server TEXT, channel TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS alert_seen (rule_id TEXT, rule_version INTEGER, message_id TEXT, version TEXT,
                    PRIMARY KEY(rule_id,rule_version,message_id,version));
                CREATE TABLE IF NOT EXISTS local_alert_events (id TEXT PRIMARY KEY, server TEXT, channel TEXT, rule_id TEXT, body TEXT);
            ''')
        self.reload_catalog()
        self.model = ChatModel(self.get_config, lambda:self.vault.get('model'))
        self.auto_sync=AutoSync(self)
        self.daily_tasks=DailyTasks(self)
        from .file_imports import FileImports
        self.file_imports=FileImports(self)
        from .exporter import Exporter
        self.exporter=Exporter(self)
        from .report_jobs import ReportJobs
        self.report_jobs=ReportJobs(self)
        from .question_jobs import QuestionJobs
        self.question_jobs=QuestionJobs(self)
        if not getattr(self,'account_scope',None):
            from .account_data import AccountData
            self.account_data=AccountData(self)

    def meta(self, key, default=None):
        with self.db() as db:
            row=db.execute('SELECT body FROM live_meta WHERE key=?',(key,)).fetchone()
        return json.loads(row['body']) if row else default

    def save_meta(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO live_meta VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))

    def reload_catalog(self):
        self.demo=self.meta('catalog',dict(servers={},channels={},topics={'unclassified':'尚未分析的话题'},annotations={}))
        self.demo['date']=datetime.now(TZ).date().isoformat()

    def import_demo(self):
        raise ValueError('真实接入使用独立消息库，不能导入合成样例。')

    def ensure_demo(self):
        return None

    def server_channels(self, server):
        if not isinstance(server,str) or server not in self.demo['servers']:
            raise ValueError('请先连接 Bot，再选择一个服务器。')
        return self.demo['servers'][server]['channels']

    def bootstrap(self, server='', channel='', remember=True):
        self.reload_catalog()
        selection=self.meta('selection',{})
        servers={key:value['name'] for key,value in self.demo['servers'].items()}
        server=server or selection.get('server') or next(iter(servers),'')
        if server and server not in servers:
            raise ValueError('请选择已发现的服务器。')
        channels=self.server_channels(server) if server else {}
        channel=channel or (selection.get('channel') if selection.get('channel') in channels else next(iter(channels),''))
        if channel and channel not in channels:
            raise ValueError('频道不属于当前服务器，请重新选择。')
        rows=[]
        if channel:
            with self.db() as db:
                rows=[json.loads(r['body']) for r in db.execute('SELECT body FROM messages WHERE channel=? ORDER BY created,id',(channel,))]
            rows=[r for r in rows if not r.get('deleted') and not r.get('unavailable') and not r['bot']]
        people={m['author_id']:m['name'] for m in rows}
        if server and channel and remember:
            self.save_meta('selection',dict(server=server,channel=channel))
        today=datetime.now(TZ).date()
        return dict(offline=False,readonly=True,date=today.isoformat(),selected_server=server,selected_channel=channel,
                    servers=servers,channels=channels,members=people,topics={},
                    config=self.get_config(),layouts=LAYOUTS,default_layout=DEFAULT_LAYOUT,
                    question_defaults=dict(start_date=(today-timedelta(days=6)).isoformat(),end_date=today.isoformat(),memory_turns=6),
                    sync=self.sync_state(channel) if channel else None)

    def get_config(self):
        with self.db() as db:
            row=db.execute('SELECT body FROM settings WHERE id=1').fetchone()
        stored=json.loads(row['body']) if row else {}
        has_key=stored.get('_has_key',False) if self.vault.storage=='macos-keychain' else bool(self.vault.get('model'))
        return {**LIVE_CONFIG,**{k:v for k,v in stored.items() if k in LIVE_CONFIG},
                'has_key':bool(has_key),'storage':self.vault.storage,'checked_at':stored.get('checked_at','')}

    def validate_live_config(self, data):
        if not isinstance(data,dict) or not data.keys() <= set(LIVE_CONFIG) | {'api_key','clear_key'}:
            raise ValueError('模型配置字段有误，请使用设置页面。')
        values={**self.get_config(),**data}
        config=super().validate_config({k:values[k] for k in DEFAULT_CONFIG})
        try:
            kib=int(values['input_kib'])
            if isinstance(values['input_kib'],bool) or float(values['input_kib'])!=kib or not 8<=kib<=1024:
                raise ValueError()
        except (ValueError,TypeError,OverflowError):
            raise ValueError('输入预算应为8至1024之间的整数KiB。') from None
        json_mode=values['json_mode']
        if json_mode not in (True,False,'true','false'):
            raise ValueError('JSON模式设置无效。')
        parameter=values['output_parameter']
        if parameter not in ('auto','max_tokens','max_completion_tokens'):
            raise ValueError('请选择支持的输出长度参数。')
        key=data.get('api_key','')
        if not isinstance(key,str) or len(key)>4096 or any(ch.isspace() for ch in key):
            raise ValueError('密钥格式无效，请只粘贴密钥本身。')
        old=self.get_config()
        if old['has_key'] and config['base_url'].rstrip('/')!=old['base_url'].rstrip('/') and not (key or data.get('clear_key')):
            raise ValueError('服务地址已改变，请重新填写该服务的密钥，避免把旧密钥发送到另一地址。')
        return {**config,'input_kib':kib,'json_mode':json_mode in (True,'true'),'output_parameter':parameter}

    def save_config(self, data):
        config=self.validate_live_config(data)
        old=self.get_config()
        has_key=old['has_key']
        if data.get('clear_key'):
            self.vault.delete('model'); has_key=False
        elif data.get('api_key'):
            self.vault.set('model',data['api_key']); has_key=True
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (1,?)',(json.dumps(dict(config,_has_key=has_key)),))
        return self.get_config()

    def check_config(self, data):
        config=self.validate_live_config(data)
        key=data.get('api_key','') or ('' if data.get('clear_key') else self.vault.get('model'))
        model=ChatModel(lambda:config,lambda:key)
        model.complete([dict(role='system',content='只做连接检查。请返回JSON。'),dict(role='user',content='请返回 {"status":"ok"}。')])
        return dict(status='connected',offline=False,message='模型已返回有效响应。只发送固定测试文本，未发送聊天记录；当前表单如有修改请保存。')

    def bots(self, server=''):
        with self.db() as db:
            rows=[json.loads(r['body']) for r in db.execute('SELECT body FROM bots ORDER BY rowid')]
        items=[]
        for row in rows:
            if server and server not in row.get('servers',{}):
                continue
            has_token=row.get('_has_token',False) if self.vault.storage=='macos-keychain' else bool(self.vault.get('bot:'+row['id']))
            items.append({k:v for k,v in row.items() if not k.startswith('_')} | dict(has_token=has_token,storage=self.vault.storage))
        return dict(offline=False,items=items)

    def bot(self, bot_id):
        if not isinstance(bot_id,str):
            raise ValueError('请选择已保存的 Bot。')
        bot=next((b for b in self.bots()['items'] if b['id']==bot_id),None)
        if not bot:
            raise ValueError('Bot不存在，请先保存配置。')
        return bot

    def write_bot(self, bot):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO bots VALUES (?,?)',(bot['id'],json.dumps(bot,ensure_ascii=False)))

    def save_bot(self, data):
        if not isinstance(data,dict) or not data.keys() <= {'id','name','token','clear_token'}:
            raise ValueError('Bot配置字段有误，请使用接入页面。')
        name,token,bot_id=data.get('name',''),data.get('token',''),data.get('id','')
        if not isinstance(name,str) or not 1<=len(name.strip())<=50 or not isinstance(token,str) or len(token)>4096 or any(ch.isspace() for ch in token):
            raise ValueError('请填写Bot名称及正确的Token，不要包含前缀或空格。')
        old=self.bot(bot_id) if bot_id else None
        if not old and not token:
            raise ValueError('添加Bot时请填写Bot Token。')
        bot={**(old or {}),'id':bot_id or uuid.uuid4().hex,'name':name.strip()}
        if data.get('clear_token'):
            self.vault.delete('bot:'+bot['id'])
        elif token:
            self.vault.set('bot:'+bot['id'],token)
        has_token=bool(token or old and old.get('has_token')) and not bool(data.get('clear_token'))
        bot.pop('has_token',None);bot.pop('storage',None)
        if token or data.get('clear_token') or not old:
            bot.update(status='not_connected',servers={},channels={},message_content=False)
        bot['_has_token']=has_token
        self.write_bot(bot)
        return self.bot(bot['id'])

    def client(self, bot_id):
        self.bot(bot_id)
        token=self.vault.get('bot:'+bot_id)
        if not token:
            raise ValueError('请先在接入页保存该Bot的Token。')
        return self.discord_factory(token)

    def connect_bot(self, bot_id):
        bot=self.bot(bot_id)
        client=self.client(bot_id)
        identity=client.identity()
        guilds=client.guilds()
        bot.update(identity,status='connected',servers=guilds,checked_at=datetime.now(timezone.utc).isoformat(),_has_token=True)
        bot.pop('has_token',None);bot.pop('storage',None)
        bot['channels']={k:v for k,v in bot.get('channels',{}).items() if k in guilds}
        self.write_bot(bot)
        catalog=self.meta('catalog',dict(servers={},channels={},topics={'unclassified':'尚未分析的话题'},annotations={}))
        for key,name in guilds.items():
            catalog['servers'].setdefault(key,dict(channels={}))['name']=name
        self.save_meta('catalog',catalog);self.reload_catalog()
        return self.bot(bot_id)

    def discover_channels(self, bot_id, server):
        bot=self.bot(bot_id)
        if not isinstance(server,str) or server not in bot.get('servers',{}):
            raise ValueError('该Bot未发现此服务器，请先连接并从列表选择。')
        channels=self.client(bot_id).readable_channels(server,bot['user_id'])
        bot['channels'][server]=channels
        bot['_has_token']=bot.pop('has_token');bot.pop('storage',None)
        self.write_bot(bot)
        catalog=self.meta('catalog')
        existing=catalog['servers'][server]['channels']
        # 保留其他Bot已发现的范围，当前Bot每次同步仍重新验证真实权限。
        existing.update(channels);catalog['channels'].update(channels)
        self.save_meta('catalog',catalog);self.reload_catalog()
        return dict(offline=False,server=server,channels=channels,message_content=bot['message_content'])

    def sync_state(self, channel, start=None, end=None):
        with self.db() as db:
            query='SELECT body FROM sync_windows WHERE channel=?'
            params=[channel]
            if start is not None and end is not None:
                query+=" AND start<? AND json_extract(body,'$.effective_end')>?"
                params.extend((end.isoformat(),start.isoformat()))
            row=db.execute(query+' ORDER BY rowid DESC LIMIT 1',params).fetchone()
        return json.loads(row['body']) if row else None

    def sync_window_state(self, channel, start_date, end_date):
        start,end=self.window(start_date,end_date=end_date)
        with self.db() as db:
            row=db.execute('SELECT body FROM sync_windows WHERE channel=? AND start=? AND end=?',
                           (channel,start.isoformat(),end.isoformat())).fetchone()
        return json.loads(row['body']) if row else None

    def sync_bot(self, server, channel):
        if channel not in self.server_channels(server):
            raise ValueError('请先选择服务器和频道。')
        eligible=[bot for bot in self.bots()['items'] if bot.get('status')=='connected' and bot['has_token'] and channel in bot.get('channels',{}).get(server,{})]
        if not eligible: raise ValueError('当前频道尚无可用 Bot，请到接入页连接并发现可读频道。')
        return eligible[0]

    def ensure_synced(self, server, channel, start_date, end_date):
        bot=self.sync_bot(server,channel)
        with self.sync_lock:
            resume=False
            while True:
                state=self.sync_channel(bot['id'],server,channel,start_date,end_date,max_pages=25,resume=resume)
                if state['status']=='complete': break
                resume=True

    def sync_channel(self, bot_id, server, channel, start_date, end_date, max_pages=100, resume=False, progress=None,_after_id='',_cancel=None,_incremental=False,_previous=None):
        if not all(isinstance(v,str) for v in (server,channel,start_date,end_date)) or channel not in self.server_channels(server):
            raise ValueError('请先选择一个已发现的服务器和频道。')
        if isinstance(max_pages,bool) or not isinstance(max_pages,int) or not 1<=max_pages<=1000:
            raise ValueError('单次页数应为1至1000的整数；达到上限会保留游标供继续同步。')
        start,end=self.window(start_date,end_date=end_date)
        until=min(end,datetime.now(timezone.utc))
        if start>=until:
            raise ValueError('不能同步未来的消息，请调整开始日期。')
        bot=self.bot(bot_id)
        if server not in bot.get('servers',{}) or channel not in bot.get('channels',{}).get(server,{}):
            raise ValueError('请先用该Bot发现当前服务器的可读频道。')
        if not self.sync_lock.acquire(blocking=False):
            raise ValueError('已有同步正在执行，请等待完成。')
        state=dict(bot_id=bot_id,server=server,channel=channel,start=start.isoformat(),end=end.isoformat(),
                   start_date=start_date,end_date=end_date,
                   effective_end=until.isoformat(),cursor=str(snowflake(until)),pages=0,messages=0,status='running',after_id=_after_id,
                   started=datetime.now(timezone.utc).isoformat(),parent_complete=False,threads_complete=False,incremental=_incremental,
                   note='正在读取当前频道及直属可读子线程；不包含附件正文。')
        with self.db() as db:
            previous=db.execute('SELECT body FROM sync_windows WHERE channel=? AND start=? AND end=?',(channel,state['start'],state['end'])).fetchone()
        old=json.loads(previous['body']) if previous else (_previous or {})
        if any(old.get(key)!=state[key] for key in ('bot_id','server','channel','start')): old={}
        if resume and old:
            if old['bot_id']==bot_id and old['status']=='partial':
                state.update(cursor=old['cursor'],effective_end=old['effective_end'],messages=old['messages'],pages=old['pages'],after_id=old.get('after_id',''))
                for key in ('parent_complete','threads','thread_scan','incremental'):
                    if key in old: state[key]=old[key]
                _after_id=state['after_id']
                until=datetime.fromisoformat(state['effective_end'])
        def save():
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO sync_windows VALUES (?,?,?,?,?)',
                    (channel,state['start'],state['end'],state['status'],json.dumps(state,ensure_ascii=False)))
            if progress: progress(dict(state))
        try:
            save()
            if _cancel and _cancel(): raise ServiceError('已切换频道或暂停，停止上一个范围的同步。')
            client=self.client(bot_id)
            identity=client.identity()
            if identity['user_id']!=bot['user_id']:
                raise ServiceError('Bot身份已改变，请重新连接和发现频道。')
            if not identity['message_content']:
                raise ServiceError('请在Discord开发者页面为该Bot启用Message Content Intent后重试；未把空正文当作没有聊天。')
            parent_info=client.check_channel(server,channel,bot['user_id'])
            if parent_info.get('type')==5:
                scan=state.setdefault('thread_scan',dict(active=False,public=dict(done=False,before=''),private=dict(done=False,before='')))
                scan['private']['done']=True  # 公告频道只有公开的公告线程。
            fetched, references = set(), []
            complete=state['parent_complete']
            used_pages=0
            for _ in range(0 if complete else max_pages):
                if _cancel and _cancel(): raise ServiceError('已切换频道或暂停，停止上一个范围的同步。')
                page=client.history(channel,state['cursor'])
                state['pages']+=1;used_pages+=1
                if not page:
                    complete=True;break
                rows=[normalize(row,server,channel) for row in page]
                cursor=min(int(r['id']) for r in rows)
                if cursor>=int(state['cursor']):
                    raise ServiceError('Discord分页游标未前进，已停止；请重新同步。')
                selected=[r for r in rows if start<=datetime.fromisoformat(r['created'])<until]
                self.upsert_messages(selected)
                with self.db() as db:
                    db.executemany('DELETE FROM context_failures WHERE channel=? AND message_id=?',[(channel,r['id']) for r in selected])
                state['messages']+=len(selected)
                fetched.update(r['id'] for r in selected);references.extend(selected)
                state['cursor']=str(cursor)
                save()
                if (_after_id and cursor<=int(_after_id)) or min(datetime.fromisoformat(r['created']) for r in rows)<start:
                    complete=True;break
            state['parent_complete']=complete
            if complete:
                complete,thread_references=sync_subthreads(self,client,state,start,until,max_pages-used_pages,
                    old,state['incremental'],save,_cancel)
                references.extend(thread_references)
            thread_scan=state.get('thread_scan',{})
            state['threads_complete']=bool(complete and not thread_scan.get('active_error')
                and all(not thread_scan.get(key,{}).get('error') for key in ('public','private'))
                and all(item['status']=='complete' for item in state.get('threads',{}).values()))
            # 仅在已核验的父频道与直属子线程中补读引用，不把其他频道当背景。
            allowed={channel}|{key for key,item in state.get('threads',{}).items() if item['status']!='unavailable'}
            checked=set(fetched)|{row['id'] for row in references}
            for source in references:
                target=source.get('reference_id'); depth=0
                while target and target not in checked and depth<6:
                    if _cancel and _cancel(): raise ServiceError('已切换频道或暂停，停止上一个范围的同步。')
                    actual=source.get('reference_channel',source.get('source_channel',channel))
                    if actual not in allowed:
                        with self.db() as db:
                            db.execute('INSERT OR REPLACE INTO context_failures VALUES (?,?,?)',
                                (channel,target,'引用位于其他频道或未经核验的子线程，未跨范围读取。'))
                        break
                    checked.add(target)
                    try:
                        if actual!=channel: client.check_thread(server,channel,actual)
                        row=normalize(client.message(actual,target),server,channel,source_channel=actual,
                            thread_name=state.get('threads',{}).get(actual,{}).get('name',''))
                    except ServiceError as error:
                        if error.status in (403,404):
                            with self.db() as db:
                                db.execute('INSERT OR REPLACE INTO context_failures VALUES (?,?,?)',(channel,target,'引用当前不可读、已删除或缺少权限，未使用旧缓存充当完整背景。'))
                            break
                        raise
                    self.upsert_messages([row])
                    with self.db() as db: db.execute('DELETE FROM context_failures WHERE channel=? AND message_id=?',(channel,row['id']))
                    target=row.get('reference_id');source=row;depth+=1
            if complete and not resume and not _after_id:
                cached=super().read_window(channel,start,until)
                deleted=[dict(row,deleted=True) for row in cached if row.get('source_channel',channel)==channel
                         and row['id'] not in fetched and not row.get('deleted')]
                self.upsert_messages(deleted)
            state['status']='complete' if complete else 'partial'
            state['note']=('当前频道及可读子线程已遍历至同步时刻。' if complete else '达到本次页数预算，仍有频道或子线程历史未读取，将继续同步。')
            state['note']+=f' 已核对{sum(item["status"]=="complete" for item in state.get("threads",{}).values())}个可读子线程。'
            if complete and not state['threads_complete']: state['note']+=' 部分子线程不可读，覆盖不完整，未使用其旧缓存。'
            state['note']+=' 不读取未加入的归档私密线程、附件及外链正文；编辑/删除需要重新完整同步确认。'
            state['finished']=datetime.now(timezone.utc).isoformat()
            if complete:self.check_saved_alerts(server,channel)
            save()
            return state
        except Exception as error:
            state['status']='failed'
            state['note']=str(error) if isinstance(error,ServiceError) else '同步未完成，请检查频道读取权限、消息内容访问和网络后重试。'
            save()
            if isinstance(error,ServiceError):
                raise ServiceError(state['note'],error.code,error.status,error.retry_after) from None
            raise ServiceError(state['note']) from None
        finally:
            self.sync_lock.release()

    def start_sync(self, data):
        required={'bot_id','server','channel','start_date','end_date'}
        if not isinstance(data,dict) or not required <= data.keys() or not data.keys() <= required | {'max_pages','resume'}:
            raise ValueError('同步字段有误，请使用页面选择范围。')
        if not all(isinstance(data[key],str) and data[key] for key in required) or not isinstance(data.get('resume',False),bool):
            raise ValueError('同步参数有误，请重新选择 Bot、频道和日期。')
        job_id=uuid.uuid4().hex
        self.jobs[job_id]=dict(id=job_id,status='running',pages=0,messages=0)
        def progress(state): self.jobs[job_id]={**state,'id':job_id}
        def run():
            try: progress(self.sync_channel(**data,progress=progress))
            except ValueError as error: self.jobs[job_id]={**self.jobs[job_id],'status':'failed','note':str(error)}
            except Exception: self.jobs[job_id]={**self.jobs[job_id],'status':'failed','note':'同步未完成，请重新连接 Bot 后重试。'}
        threading.Thread(target=run,daemon=True).start()
        return dict(id=job_id,status='running')

    def sync_job(self, job_id):
        if not isinstance(job_id,str) or job_id not in self.jobs:
            raise ValueError('同步任务不存在；服务重启后请重新同步。')
        return dict(self.jobs[job_id])

    def covered(self, channel, start, end):
        with self.sync_lock:
            latest=self.sync_state(channel,start,end)
            if (latest and not latest.get('threads_complete')
                    and datetime.fromisoformat(latest['start'])<end and datetime.fromisoformat(latest['effective_end'])>start):
                return False
            with self.db() as db:
                rows=db.execute('SELECT start,body FROM sync_windows WHERE channel=? AND status=? ORDER BY start',(channel,'complete')).fetchall()
            cursor=start
            for row in rows:
                state=json.loads(row['body'])
                if not state.get('threads_complete'): continue
                left,right=datetime.fromisoformat(row['start']),datetime.fromisoformat(state['effective_end'])
                if left<=cursor<right: cursor=right
            return cursor>=end

    def coverage(self, channel, start, end, contexts):
        # A running sync has temporary incomplete thread flags; read only its final state.
        with self.sync_lock:
            complete=self.covered(channel,start,end)
            missing=sum(c['status']!='available' for c in contexts)
            state=self.sync_state(channel,start,end)
            current=bool(state and state['status']=='complete' and state.get('threads_complete') and datetime.fromisoformat(state['start'])<=start<datetime.fromisoformat(state['effective_end'])<end
                         and datetime.fromisoformat(state['end'])>=end and end>datetime.now(timezone.utc))
            note=('所选日期的频道正文已完整分页同步。' if complete else
                  '已分页同步至本次读取时刻；所选结束日期尚未结束，不能视为整日或整个周期结束后的完整记录。' if current else
                  '所选日期尚未完整同步，结果仅反映本地已同步记录。')
            if state:
                stamp=datetime.fromisoformat(state.get('finished',state['started'])).astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S')
                note+=f' 最近同步：{stamp}（北京时间）。'
            note+=' 范围包含当前频道及直属可读子线程，不读取其他频道或未加入的归档私密线程；不含附件及外链正文，离线期间的编辑删除未必已反映。'
            if state and not state.get('threads_complete'): note+=' 子线程尚未补齐或有不可读记录。'
            if missing: note+=f' 有{missing}处引用未补齐。'
            return dict(status='partial' if missing or not complete else 'complete',window_complete=complete,up_to_date=current,missing=missing,note=note)

    def read_window(self, channel, start, end):
        return [r for r in super().read_window(channel,start,end) if not r.get('deleted') and not r.get('unavailable')]

    def contexts(self, messages, channel, max_depth=6):
        result=super().contexts(messages,channel,max_depth)
        with self.db() as db:
            failures={r['message_id']:r['reason'] for r in db.execute('SELECT message_id,reason FROM context_failures WHERE channel=?',(channel,))}
        for item in result:
            if item['id'] in failures:
                item.pop('message',None);item.update(status='missing',reason=failures[item['id']])
                continue
            if item['status']=='missing':
                item['reason']='引用原文未能补读，可能已删除、缺少权限或位于其他频道；未跨范围读取。'
            elif item['status']=='available' and (item['message'].get('deleted') or item['message'].get('unavailable')):
                item.pop('message');item.update(status='missing',reason='该引用原文已删除或当前不可读，未使用旧缓存。')
        return result

    def report(self, date='', channel='', kind='daily', member='', topic='', server='', mode='compact',period='daily',focus_member='',focus_topic='',end_date='', start_time='', end_time='', instructions='', _snapshot_id='', _synced=False):
        if not isinstance(server,str) or not isinstance(channel,str) or channel not in self.server_channels(server):
            raise ValueError('请先选择一个服务器和其中的一个频道。')
        if kind not in ('daily','weekly','member','discussion','activity') or mode not in ('compact','full') or period not in ('daily','weekly'):
            raise ValueError('报告类型或阅读方式无效。')
        if not all(isinstance(s,str) for s in (member,topic,focus_member,focus_topic,date,end_date,start_time,end_time,instructions)) or len(instructions)>2000:
            raise ValueError('报告范围格式有误，请重新选择。')
        if kind=='activity':
            if focus_member or focus_topic:raise ValueError('关注维度仅适用于日报周报。')
            from .activity import activity_report,public_report
            return public_report(activity_report(self,server,channel,date,end_date))
        periodic=kind in ('daily','weekly')
        period=kind if periodic else ('daily' if kind=='activity' else period)
        if not periodic and (focus_member or focus_topic):
            raise ValueError('关注维度仅适用于日报周报。')
        report_tz=getattr(self,'report_timezone',TZ)
        precise=bool(start_time or end_time)
        start,end=minute_window(start_time,end_time,report_tz) if precise else self.window(date,period=='weekly',end_date)
        window=describe_window(start,end,report_tz,getattr(self,'report_timezone_name','Asia/Shanghai'),precise)
        if not precise and period=='daily' and window['days']!=1:
            raise ValueError('单日模式只支持一天。')
        if not _synced:
            self.ensure_synced(server,channel,window['start_date'],window['end_date'])
        raw=self.read_window(channel,start,end)
        from .activity import user_message
        users=[r for r in raw if user_message(r)]
        text_rows=[r for r in users if r.get('text_available',bool(r['content'].strip()))]
        if not raw and not self.covered(channel,start,end) and not getattr(self,'generation_scope',None):
            raise ValueError('所选日期没有可读取的记录，请调整日期或检查频道权限。')
        names=self.bootstrap(server,channel,remember=False)['members']
        if (kind=='member' and member not in names) or (focus_member and focus_member not in names):
            raise ValueError('请选择当前频道已同步的成员。')
        contexts=self.contexts(text_rows,channel)
        coverage=self.coverage(channel,start,end,contexts)
        if len(text_rows)<len(users):
            coverage['status']='partial';coverage['note']+=f' {len(users)-len(text_rows)}条无文字消息未作语义分析。'
        from .report_profiles import select_profile
        profile = select_profile(self, server, kind)
        hours=Counter(datetime.fromisoformat(r['created']).astimezone(report_tz).hour for r in users)
        previous=[r for r in self.read_window(channel,start-(end-start),start) if user_message(r)]
        metrics=dict(messages=len(users),members=len({r['author_id'] for r in users}),replies=sum(bool(r['reference_id']) for r in users),
                     bots=sum(bool(row['bot']) for row in raw),system=sum(not row['bot'] and not user_message(row) for row in raw),
                     previous=len(previous),comparable=self.covered(channel,start-(end-start),start) and coverage['window_complete'],
                     hours=[dict(hour=h,count=hours[h]) for h in range(24)])
        def generate_one(which, person='', query=''):
            rows=[r for r in text_rows if r['author_id']==person] if which=='member' else text_rows
            context=self.contexts(rows,channel)
            generated=generate_report(report_model,dict(server=server,channel=channel,instructions=instructions,server_name=self.demo['servers'][server]['name'],
                                                     channel_name=self.server_channels(server)[channel],**window,**getattr(self,'generation_scope',{})),
                                      dict(kind=which,member=person,topic=query or '本频道全部讨论'),rows,context,mode,profile=profile)
            context=self.contexts(generated['messages'],channel)
            subject=dict(id=person,name=names[person]) if which=='member' else (dict(id=query or '本频道全部讨论',name=query or '本频道全部讨论') if which=='discussion' else None)
            own_coverage=self.coverage(channel,start,end,context)
            unparsed=sum(not r.get('text_available',bool(r['content'].strip())) for r in users if which!='member' or r['author_id']==person)
            if unparsed:
                own_coverage['status']='partial';own_coverage['note']+=f' {unparsed}条无文字消息未作语义分析。'
            if generated['generation']['omitted_contexts']:
                own_coverage['status']='partial';own_coverage['note']+=' 部分引用背景超出输入预算，未提供给模型。'
            return dict(**generated,kind=which,period=period,subject=subject,selected_count=len(generated['messages']),contexts=context,coverage=own_coverage)
        if not self.generation_lock.acquire(blocking=False):
            raise ValueError('已有报告或问答正在生成，请等待完成后再试。')
        before_calls=self.model.calls
        from .report_response_cache import CachedReportModel
        try:
            report_model=CachedReportModel(self.model,getattr(self,'host',self))
            generated=generate_one(kind,member,topic) if kind!='activity' else generate_report(self.model,window,dict(kind='daily'),[],[],mode)
            dimensions={}
            for key,value in [('member',focus_member),('discussion',focus_topic)]:
                if not value: continue
                try: dimensions[key]=generate_one(key,value if key=='member' else '',value if key=='discussion' else '')
                except ValueError as error:
                    dimensions[key]=dict(kind=key,period=period,subject=dict(id=value,name=names.get(value,value)),selected_count=0,messages=[],contexts=[],
                                         coverage=coverage,digest=None,fullDigest=None,generation=dict(engine='openai-compatible',status='failed',message=str(error)))
                    generated['generation']['status']='partial'
        finally:
            self.generation_lock.release()
        generated['generation']['calls']=self.model.calls-before_calls
        generated.pop('kind',None);generated.pop('period',None)
        if kind=='activity': generated.update(messages=users,subject=None,contexts=contexts,coverage=coverage,selected_count=len(users))
        if kind=='activity': generated['generation']=dict(engine='none',status='not_requested')
        result=dict(offline=False,readonly=True,kind=kind,period=period,date=window['start_date'],server=server,channel=channel,
                    window=window,metrics=metrics,dimensions=dimensions,groups=[],trends=[],**generated)
        if kind not in ('member','discussion','activity'):
            result['coverage']=coverage
            if generated['generation']['omitted_contexts']:
                result['coverage']['status']='partial';result['coverage']['note']+=' 部分引用超出输入预算。'
        result['metrics']['topics']=result['fullDigest']['summaryStats']['topicCount']
        if kind!='activity':
            result['report_id']=_snapshot_id or uuid.uuid4().hex
            with self.db() as db: db.execute('INSERT INTO report_snapshots VALUES (?,?)',(result['report_id'],json.dumps(result,ensure_ascii=False)))
        return result

    def topic_trends(self, server, channel, date, end_date='', refresh=False):
        from .activity import topic_trends
        return topic_trends(self,server,channel,date,end_date,refresh)

    def alert_rules(self, channel, server):
        from .local_alerts import rules
        return rules(self,server,channel)

    def save_alert_rule(self, data):
        from .local_alerts import save_rule
        return save_rule(self,data)

    def check_saved_alerts(self, server, channel):
        from .local_alerts import check
        return check(self,server,channel)

    def alerts(self, channel='', server=''):
        from .local_alerts import events
        return events(self,server,channel)

    def check_alerts(self, keyword, channel='', date='', server=''):
        if not isinstance(keyword,str) or not 1<=len(keyword.strip())<=50:
            raise ValueError('请填写1至50字的关键词。')
        if channel not in self.server_channels(server):raise ValueError('请先选择当前服务器和频道。')
        self.ensure_synced(server,channel,date,date)
        start,end=self.window(date)
        from .activity import user_message
        matches=[row for row in self.read_window(channel,start,end) if row.get('server')==server
            and user_message(row) and keyword.strip().casefold() in row['content'].casefold()]
        return dict(offline=False,preview=True,created=0,matches=matches,alerts=self.alerts(channel,server))

    def ask(self, question, server, channel, start_date='',end_date='',conversation_id=''):
        asked_at=datetime.now(timezone.utc).isoformat()
        if not isinstance(server,str) or not isinstance(channel,str) or channel not in self.server_channels(server):
            raise ValueError('请先选择一个服务器和其中的一个频道。')
        if bool(start_date)!=bool(end_date):
            raise ValueError('请同时选择开始和结束日期。')
        end_date=end_date or datetime.now(TZ).date().isoformat()
        start_date=start_date or (datetime.fromisoformat(end_date).date()-timedelta(days=6)).isoformat()
        start,end=self.window(start_date,end_date=end_date)
        self.ensure_synced(server,channel,start_date,end_date)
        if not any(not m['bot'] and m['content'].strip() for m in self.read_window(channel,start,end)):
            raise ValueError('所选日期已检查，没有可用于回答的文字消息，请调整日期；没有调用模型。')
        if not self.generation_lock.acquire(blocking=False):
            raise ValueError('已有报告或问答正在生成，请等待完成后再试。')
        try:
            return super().ask(question,server,channel,start_date,end_date,conversation_id,_asked_at=asked_at)
        finally:
            self.generation_lock.release()

    def preview_delivery(self, *_args, **_kwargs):
        raise ValueError('当前为真实数据只读阶段，Discord消息发送和模拟发送均已关闭。')

    def simulate_delivery(self, *_args, **_kwargs):
        raise ValueError('当前为真实数据只读阶段，不向Discord发送消息。')
