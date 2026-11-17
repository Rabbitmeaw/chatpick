"""个人读取数据的稳定边界；报告、Query、统计、提醒及每日任务沿用 LiveApp。"""

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone

from .core import App
from .discord import identifier
from .file_imports import FileReportApp, checked_timezone
from .live import LiveApp
from .network import ServiceError
from .time_ranges import minute_window


class AccountApp(LiveApp):
    def __init__(self, host, path, scope, zone='Asia/Shanghai'):
        self.host, self.account_scope = host, scope
        self.question_scope_metadata=dict(source='account',account_id=scope['account_id'])
        self.report_timezone, self.report_timezone_name = checked_timezone(zone), zone
        super().__init__(path, vault=host.vault)
        self.model, self.generation_lock = host.model, host.generation_lock
        self.exporter = host.exporter
        self.generation_scope = dict(data_source='personal-account', coverage='仅本人当前可见的单频道文字，不含子线程及附件正文。')
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS account_windows (start TEXT,end TEXT,checked TEXT,PRIMARY KEY(start,end))')
        self.save_meta('account-scope', scope)

    def current(self):
        self.host.account_data.check(self.account_scope['server'], self.account_scope['channel'], self.account_scope['account_id'])

    def reload_catalog(self):
        s = self.account_scope
        self.demo = dict(servers={s['server']:dict(name=s['server_name'],channels={s['channel']:s['channel_name']})},
                         channels={s['channel']:s['channel_name']},topics={},annotations={},date=datetime.now(self.report_timezone).date().isoformat())

    def server_channels(self, server):
        self.current()
        return super().server_channels(server)

    def get_config(self): return self.host.get_config()

    def bootstrap(self, server='', channel='', remember=True):
        value=super().bootstrap(server,channel,remember)
        today=datetime.now(self.report_timezone).date()
        value.update(source='account',account_id=self.account_scope['account_id'],timezone=self.report_timezone_name,
            date=today.isoformat(),question_defaults=dict(start_date=(today-timedelta(days=6)).isoformat(),end_date=today.isoformat(),memory_turns=6))
        return value

    window = FileReportApp.window

    def for_timezone(self, zone):
        scope=self.account_scope
        return self.host.account_data.reader(scope['server'],scope['channel'],scope['account_id'],zone)

    def sync_state(self, channel, start=None, end=None):
        return self.meta('account-sync',dict(enabled=False,status='idle',note='仅按需读取；后台更新未开启。'))

    def sync_bot(self, server, channel):
        self.current()
        if (server,channel)!=(self.account_scope['server'],self.account_scope['channel']):
            raise ValueError('个人记录不属于当前服务器和频道。')
        return None

    def ensure_synced(self, server, channel, start_date, end_date):
        self.sync_bot(server, channel)
        start,end=self.window(start_date,end_date=end_date)
        try:
            self.host.exporter.prepare(dict(server=server,channel=channel,timezone=self.report_timezone_name,
                start_time=start.astimezone(self.report_timezone).strftime('%Y-%m-%dT%H:%M'),
                end_time=end.astimezone(self.report_timezone).strftime('%Y-%m-%dT%H:%M')))
        except Exception as error:
            self.host.account_data.invalidate_if_unreadable(self,error)
            raise
        self.current()

    def read_window(self, channel, start, end):
        self.current()
        return [row for row in App.read_window(self,channel,start,end) if not row.get('deleted') and not row.get('unavailable')]

    def message(self, message_id):
        self.current()
        row=App.message(self,message_id)
        return row if row and not row.get('deleted') and not row.get('unavailable') else None

    def contexts(self, messages, channel, max_depth=6):
        self.current()
        return App.contexts(self,messages,channel,max_depth)

    def covered(self, channel, start, end):
        if channel!=self.account_scope['channel']: return False
        self.current()
        point=start.astimezone(timezone.utc)
        invalidated=self.meta('account-permission-invalidated-at','')
        with self.db() as db:
            windows=db.execute('SELECT start,end,checked FROM account_windows ORDER BY start').fetchall()
        for row in windows:
            if invalidated and row['checked']<=invalidated: continue
            left=datetime.fromisoformat(row['start']);right=min(datetime.fromisoformat(row['end']),datetime.fromisoformat(row['checked']))
            if left>point: break
            if right>point: point=right
            if point>=end: return True
        return False

    def coverage(self, channel, start, end, contexts):
        complete=self.covered(channel,start,end)
        missing=sum(item['status']!='available' for item in contexts)
        return dict(status='complete' if complete and not missing else 'partial',window_complete=complete,up_to_date=complete,
                    missing=missing,source='account',note='仅根据本人可见频道的本次读取窗口；不含子线程、附件及外链正文。'
                    +(' 所选日期尚未结束或读取窗口未覆盖全部时间。' if not complete else '')
                    +(f' 有{missing}处引用未能补齐。' if missing else ''))

    def report(self, **data):
        self.current()
        result=super().report(**data)
        self.current()
        scope=self.account_scope
        result.update(server_name=scope['server_name'],channel_name=scope['channel_name'],
                      source=dict(kind='account-data',account_id=scope['account_id']),generated_at=datetime.now(timezone.utc).isoformat())
        if result.get('report_id'):
            with self.db() as db:
                db.execute('UPDATE report_snapshots SET body=? WHERE id=?',(json.dumps(result,ensure_ascii=False),result['report_id']))
        return result

    def ask(self, *args, **kwargs):
        self.current()
        result=super().ask(*args, **kwargs)
        self.current()
        return result

    def conversations(self, *args, **kwargs):
        self.current()
        return super().conversations(*args, **kwargs)

    def conversation(self, *args, **kwargs):
        self.current()
        return super().conversation(*args, **kwargs)


class AccountData:
    def __init__(self, host):
        self.host=host
        self.directory=host.path.parent.resolve()/(host.path.stem+'.accounts')
        self.lock=threading.RLock()
        self.worker=threading.Lock()
        self.recovered=set()

    def invalidate_if_unreadable(self, reader, error):
        confirmed=(isinstance(error,ServiceError) and error.status in (401,403,404)) or str(error) in (
            'Bot缺少该频道的查看或读取历史权限，请在Discord中授予后重新发现频道。',
            '频道不属于所选服务器，或不是受支持的文字频道。')
        if not confirmed: return
        with self.lock:
            scope=reader.account_scope
            with reader.db() as db:
                rows=[json.loads(row['body']) for row in db.execute('SELECT body FROM messages WHERE channel=?',(scope['channel'],))]
            reader.upsert_messages([dict(row,unavailable=True) for row in rows])
            stamp=datetime.now(timezone.utc).isoformat()
            reader.save_meta('account-permission-invalidated-at',stamp)
            reader.save_meta('account-sync',dict(reader.sync_state(scope['channel']),status='failed',finished=stamp,
                note='当前频道已确认不可读；历史快照保留，恢复权限后需重新读取所选窗口。'))

    def check(self, server, channel, account_id):
        identifier(server);identifier(channel);identifier(account_id)
        current=self.host.exporter.state()
        if current.get('status')!='connected' or not current.get('has_token') or current.get('user_id')!=account_id:
            raise ValueError('个人读取身份已变化或尚未连接，请重新选择；没有使用Bot数据。')
        if channel not in current.get('channels',{}).get(server,{}):
            raise ValueError('请先发现当前个人账号可读的服务器和频道。')
        return dict(account_id=account_id,server=server,channel=channel,server_name=current['servers'][server],
                    channel_name=current['channels'][server][channel])

    def reader(self, server, channel, account_id, zone='Asia/Shanghai'):
        scope=self.check(server,channel,account_id)
        checked_timezone(zone)
        key=hashlib.sha256(json.dumps([account_id,server,channel]).encode()).hexdigest()
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=self.directory/(key+'.sqlite3')
        if path.is_symlink(): raise ValueError('个人记录存储位置无效。')
        with self.lock:
            reader=AccountApp(self.host,path,scope,zone)
            if not reader.meta('account-cache-restored',False):
                old=[item for item in self.host.file_imports.items()['items'] if item['server']==server and item['channel']==channel
                     and item.get('origin',{}).get('kind')=='account-export' and item['origin'].get('account_id')==account_id]
                for item in sorted(old,key=lambda item:item['imported_at']):
                    self.merge(reader,item)
                reader.save_meta('account-cache-restored',True)
            return reader

    def ingest(self, item):
        origin=item.get('origin',{})
        if origin.get('kind')!='account-export': return
        with self.lock:
            reader=self.reader(item['server'],item['channel'],origin['account_id'],origin['timezone'])
            self.merge(reader,item)

    def merge(self, reader, item):
        origin=item['origin']
        checked=item.get('imported_at') or datetime.now(timezone.utc).isoformat()
        invalidated=reader.meta('account-permission-invalidated-at','')
        if invalidated and checked<=invalidated:
            raise ValueError('这是权限失效前的缓存，不能恢复为当前证据；请重新读取所选窗口。')
        imported=self.host.file_imports.reader(item['id'],origin['timezone'])
        start,end=(minute_window(origin['start_time'],origin['end_time'],checked_timezone(origin['timezone'])) if origin.get('start_time')
                   else imported.window(origin['date']))
        with imported.db() as db:
            rows=[json.loads(row['body']) for row in db.execute('SELECT body FROM messages ORDER BY created,id')]
            reports=[(row['id'],row['body']) for row in db.execute('SELECT id,body FROM report_snapshots')]
        if any((row['server'],row['channel'])!=(item['server'],item['channel']) for row in rows):
            raise ValueError('个人读取批次范围不符，没有合并。')
        for row in rows:
            previous=reader.message(row['id'])
            if previous and previous['author_id']!=row['author_id']:
                raise ValueError('同一消息的稳定发言人身份发生冲突，没有合并记录。')
        # 只核对成功读取窗口的删除；窗口外引用保留真实身份，不当作正文覆盖。
        known={row['id'] for row in rows}
        removed=[dict(row,deleted=True) for row in reader.read_window(item['channel'],start,end) if row['id'] not in known]
        reader.upsert_messages(rows+removed)
        with reader.db() as db:
            db.execute('INSERT OR REPLACE INTO account_windows VALUES (?,?,?)',(start.isoformat(),end.isoformat(),checked))
            db.executemany('INSERT OR IGNORE INTO report_snapshots VALUES (?,?)',reports)
        state=reader.meta('account-sync',{})
        reader.save_meta('account-sync',dict(state,enabled=state.get('enabled',False),status='complete',finished=checked,
            last_end=min(end,datetime.fromisoformat(checked)).isoformat(),note='当前读取已保存；后台仅在开启后更新。'))
        reader.check_saved_alerts(item['server'],item['channel'])

    def state(self, server, channel, account_id, zone='Asia/Shanghai'):
        reader=self.reader(server,channel,account_id,zone)
        value=reader.bootstrap(server,channel,remember=False)
        current=self.host.exporter.state()
        return dict(value,source='account',account_id=account_id,timezone=zone,
                    servers=current['servers'],channels=current['channels'][server],sync=reader.sync_state(channel))

    def configure_sync(self, data):
        if (not isinstance(data,dict) or not {'server','channel','account_id','enabled'}<=data.keys()
                or not data.keys()<={'server','channel','account_id','enabled','timezone'} or type(data['enabled']) is not bool):
            raise ValueError('请通过当前个人频道的后台更新设置操作。')
        reader=self.reader(data['server'],data['channel'],data['account_id'],data.get('timezone','Asia/Shanghai'))
        old=reader.sync_state(data['channel'])
        reader.save_meta('account-sync',dict(old,enabled=data['enabled'],timezone=reader.report_timezone_name,
            status='pending' if data['enabled'] else 'paused',note='后台更新已开启，每60秒检查新增及最近5分钟变化。' if data['enabled'] else '后台更新已暂停，仍可按需读取。'))
        return self.state(data['server'],data['channel'],data['account_id'],reader.report_timezone_name)

    def tick(self, now=None):
        if not self.host.exporter.state()['enabled'] or not self.worker.acquire(blocking=False): return
        try:
            for path in self.directory.glob('*.sqlite3'):
                import sqlite3
                with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                    row=db.execute("SELECT body FROM live_meta WHERE key='account-scope'").fetchone()
                if not row: continue
                scope=json.loads(row[0])
                reader=None
                try:
                    reader=self.reader(scope['server'],scope['channel'],scope['account_id'])
                    if path.stem not in self.recovered:
                        reader.daily_tasks.recover();self.recovered.add(path.stem)
                    reader.daily_tasks.tick(now)
                    sync=reader.sync_state(scope['channel'])
                    if not sync.get('enabled'): continue
                    moment=(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
                    previous=datetime.fromisoformat(sync['finished']) if sync.get('finished') else moment-timedelta(minutes=2)
                    if (moment-previous).total_seconds()<60: continue
                    zone=sync.get('timezone','Asia/Shanghai');tz=checked_timezone(zone)
                    left=datetime.fromisoformat(sync['last_end'])-timedelta(minutes=5) if sync.get('last_end') else moment-timedelta(days=1)
                    reader.save_meta('account-sync',dict(sync,status='running',note='正在补齐新增记录及最近5分钟的变化；不调用模型。'))
                    self.host.exporter.prepare(dict(server=scope['server'],channel=scope['channel'],timezone=zone,
                        start_time=left.astimezone(tz).strftime('%Y-%m-%dT%H:%M'),end_time=moment.astimezone(tz).strftime('%Y-%m-%dT%H:%M')))
                except Exception as error:
                    # 账号或权限改变不借旧缓存，也不改绑到新账号；需使用者重新连接。
                    if reader is not None:
                        self.invalidate_if_unreadable(reader,error)
                        sync=reader.sync_state(scope['channel'])
                        reader.save_meta('account-sync',dict(sync,status='failed',finished=datetime.now(timezone.utc).isoformat(),
                            note=str(error) if isinstance(error,ValueError) else '后台读取未完成；请检查个人账号连接后继续。'))
                    continue
        finally:
            self.worker.release()
