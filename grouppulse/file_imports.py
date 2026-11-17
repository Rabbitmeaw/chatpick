"""DiscordChatExporter 文件适配；独立消息库，直接执行现有成员报告入口。"""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import App
from .discord import normalize, normalize_avatar_url
from .live import LiveApp
from .time_ranges import minute_window

MAX_FILE_BYTES = 25 * 1024 * 1024
TIMEZONES = ('Asia/Shanghai', 'America/Los_Angeles', 'UTC')


def object_value(value):
    if not isinstance(value, dict):
        raise ValueError('JSON 结构不符合 DiscordChatExporter 格式，请选择原始 JSON 导出文件。')
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,21}', value):
        raise ValueError('服务器、频道、消息或成员 ID 无效；请使用原始 JSON 导出文件。')
    return value


def timestamp(value):
    try:
        if not isinstance(value, str): raise ValueError()
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None: raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        raise ValueError('消息时间必须包含时区；请重新导出 JSON，避免把发言分到错误日期。') from None


def checked_timezone(value):
    if not isinstance(value, str) or value not in TIMEZONES:
        raise ValueError('请选择支持的统计时区。')
    try: return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        raise ValueError('本机缺少所选时区数据，请联系维护者补齐。') from None


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('JSON 含重复字段，无法确认消息身份；请重新导出。')
        result[key] = value
    return result


def parse_export(content, filename):
    if not isinstance(content, str) or not content or len(content.encode('utf-8')) > MAX_FILE_BYTES:
        raise ValueError('请选择不超过 25 MiB 的 JSON 文件；大文件请按日期分段导出。')
    if not isinstance(filename, str) or not 1 <= len(filename) <= 240 or '/' in filename or '\\' in filename:
        raise ValueError('文件名称无效，请重新选择 JSON 文件。')
    try:
        value = json.loads(content.lstrip('\ufeff'), object_pairs_hook=no_duplicate_keys,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('JSON 含无效数值。')))
    except (json.JSONDecodeError, RecursionError):
        raise ValueError('无法读取 JSON，请选择 DiscordChatExporter 原始 JSON 文件。') from None
    root = object_value(value)
    guild, channel = object_value(root.get('guild')), object_value(root.get('channel'))
    server_id, channel_id = identifier(guild.get('id')), identifier(channel.get('id'))
    if channel.get('type') not in ('GuildTextChat', 'GuildNews', 'GuildPublicThread', 'GuildPrivateThread', 'GuildNewsThread'):
        raise ValueError('只支持一个服务器内的单个文字频道或线程导出；不支持私信或混合频道。')
    if any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in (guild.get('name'), channel.get('name'))):
        raise ValueError('文件缺少服务器或频道名称，请使用完整的 JSON 导出。')
    messages = root.get('messages')
    if not isinstance(messages, list): raise ValueError('文件缺少 messages 消息数组。')
    count = root.get('messageCount', len(messages))
    if type(count) is not int or count != len(messages):
        raise ValueError('文件声明的消息条数与实际内容不符，可能被截断；请重新导出。')
    exported = timestamp(root['exportedAt']) if root.get('exportedAt') else ''
    date_range = object_value(root.get('dateRange') or {})
    for key in ('after', 'before'):
        if date_range.get(key): timestamp(date_range[key])
    rows = {}
    for item in messages:
        item = object_value(item)
        for key, expected in [('channelId', channel_id), ('channel_id', channel_id), ('guildId', server_id), ('guild_id', server_id)]:
            if key in item and item[key] != expected:
                raise ValueError('文件含其他服务器或频道的消息，已拒绝混合导入。')
        person = object_value(item.get('author'))
        mid, uid = identifier(item.get('id')), identifier(person.get('id'))
        bot = person.get('isBot', False)
        name = person.get('nickname') or person.get('globalName') or person.get('name')
        if type(bot) is not bool or not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise ValueError('消息发言人信息不完整，请使用原始 JSON 导出。')
        if not isinstance(item.get('content'), str) or not isinstance(item.get('type'), str):
            raise ValueError('消息正文或类型无效，请使用原始 JSON 导出。')
        created = timestamp(item.get('timestamp'))
        edited = timestamp(item['timestampEdited']) if item.get('timestampEdited') else None
        ref = object_value(item.get('reference') or {})
        ref_id = identifier(ref['messageId']) if ref.get('messageId') else None
        ref_channel = identifier(ref['channelId']) if ref.get('channelId') else channel_id
        ref_server = identifier(ref['guildId']) if ref.get('guildId') else server_id
        row = normalize(dict(id=mid, channel_id=channel_id, guild_id=server_id, timestamp=created, content=item['content'],
                             author=dict(id=uid, username=name, bot=bot), edited_timestamp=edited,
                             type=0 if item['type'] == 'Default' else 19 if item['type'] == 'Reply' else 6,
                             message_reference=dict(message_id=ref_id, channel_id=ref_channel) if ref_id else None), server_id, channel_id)
        attachments = item.get('attachments', [])
        embeds = item.get('embeds', [])
        if not isinstance(attachments, list) or not isinstance(embeds, list): raise ValueError('消息附件字段无效。')
        row.update(original_timestamp=item['timestamp'], reference_server=ref_server,avatar_url=normalize_avatar_url(person.get('avatarUrl','')),
                   attachment_names=[object_value(a).get('fileName', '附件') for a in attachments], has_embeds=bool(embeds))
        if mid in rows and rows[mid] != row:
            raise ValueError('文件中同一消息 ID 的身份、时间或正文存在冲突，未导入任何记录。')
        rows[mid] = row
    digest = hashlib.sha256(json.dumps(root, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    metadata = dict(id=digest, filename=filename, server=server_id, server_name=guild['name'], channel=channel_id,
                    channel_name=channel['name'], message_count=len(rows), file_message_count=len(messages),
                    exported_at=exported, imported_at=datetime.now(timezone.utc).isoformat(), date_range=date_range)
    return metadata, list(rows.values())


class FileReportApp(LiveApp):
    """只替换数据边界；report、generate_report 与原 JS 核心继续使用现有实现。"""
    def __init__(self, host, path, metadata, zone='Asia/Shanghai'):
        App.__init__(self, path)
        self.metadata = metadata
        self.model, self.generation_lock = host.model, host.generation_lock
        self.report_timezone, self.report_timezone_name = checked_timezone(zone), zone
        self.generation_scope = dict(data_source='discord-export', coverage='仅根据导入文件，不代表服务器完整记录。')
        self.demo = dict(servers={metadata['server']:dict(name=metadata['server_name'], channels={metadata['channel']:metadata['channel_name']})})
        self.host = host

    def server_channels(self, server):
        if server != self.metadata['server']: raise ValueError('报告服务器与导入文件不符。')
        return self.demo['servers'][server]['channels']

    def bootstrap(self, server='', channel='', remember=True):
        if server != self.metadata['server'] or channel != self.metadata['channel']:
            raise ValueError('报告频道与导入文件不符。')
        with self.db() as db:
            rows = [json.loads(r['body']) for r in db.execute('SELECT body FROM messages ORDER BY created,id')]
        from .activity import user_message
        return dict(members={r['author_id']:r['name'] for r in rows if user_message(r)})

    def window(self, day, weekly=False, end_date=''):
        try:
            first = date.fromisoformat(day)
            last = date.fromisoformat(end_date) if end_date else first
            if last < first: raise ValueError()
            start = datetime.combine(first, time.min, self.report_timezone)
            end = datetime.combine(last + timedelta(days=1), time.min, self.report_timezone)
            return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError): raise ValueError('请选择有效的日报日期。') from None

    def ensure_synced(self, *_args): return None

    def covered(self, *_args): return False

    def read_window(self, channel, start, end): return App.read_window(self, channel, start, end)

    def contexts(self, messages, channel, max_depth=6):
        result = App.contexts(self, messages, channel, max_depth)
        for item in result:
            if item['status'] == 'missing':
                item['reason'] = '导入文件中没有同服务器同频道的这条引用原文；未使用 Bot 缓存或跨范围补读。'
        return result

    def coverage(self, channel, start, end, contexts):
        missing = sum(item['status'] != 'available' for item in contexts)
        if self.metadata.get('origin',{}).get('kind') == 'account-export':
            note = '原 DiscordChatExporter 已读取所选频道和日期的可见记录，并补读同频道引用。仅代表本次读取时可见正文，不含子线程、附件及外链正文；未使用 Bot 缓存。'
        else:
            note = '仅分析这份导入文件中的记录，无法确认导出前是否按作者、条数或日期筛选，不代表服务器全量。未读取 Discord 或 Bot 缓存；不含附件及外链正文。'
        if missing: note += f' 有 {missing} 处引用未包含在文件中或不属于当前范围。'
        return dict(status='partial', window_complete=False, up_to_date=False, missing=missing, note=note, source='file')


class FileImports:
    def __init__(self, app):
        self.app = app
        self.directory = app.path.parent.resolve() / (app.path.stem + '.imports')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def path(self, import_id):
        if not isinstance(import_id, str) or not re.fullmatch('[a-f0-9]{64}', import_id):
            raise ValueError('请选择已导入的文件。')
        path = self.directory / (import_id + '.sqlite3')
        if path.is_symlink(): raise ValueError('导入文件存储位置无效。')
        return path

    def metadata(self, import_id):
        path = self.path(import_id)
        if not path.is_file(): raise ValueError('导入记录不存在，请重新选择文件。')
        try:
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            try: value = json.loads(db.execute('SELECT body FROM import_metadata WHERE id=1').fetchone()[0])
            finally: db.close()
            if value['id'] != import_id: raise ValueError()
            return value
        except (sqlite3.Error, TypeError, KeyError, ValueError): raise ValueError('导入记录无法读取，请重新导入原文件。') from None

    def reader(self, import_id, zone='Asia/Shanghai'):
        metadata=self.metadata(import_id)
        origin=metadata.get('origin',{})
        if origin.get('kind')=='account-export' and self.app.exporter.state().get('user_id')!=origin.get('account_id'):
            raise ValueError('这份记录属于其他个人读取身份，请切回原账号；没有混用记录。')
        return FileReportApp(self.app, self.path(import_id), metadata, zone)

    def add(self, data, *, origin=None):
        if not isinstance(data, dict) or data.keys() != {'filename', 'content'}: raise ValueError('请选择 JSON 文件导入。')
        metadata, rows = parse_export(data['content'], data['filename'])
        if origin:
            metadata['origin'] = origin
            if origin.get('kind')=='account-export':
                # 同一JSON可被不同账号或窗口读取，不能借另一个身份的批次复用来源声明。
                metadata['id']=hashlib.sha256(json.dumps([metadata['id'],origin],sort_keys=True,ensure_ascii=True).encode()).hexdigest()
        destination = self.path(metadata['id'])
        with self.lock:
            if not destination.exists():
                handle, temporary = tempfile.mkstemp(prefix='.import-', suffix='.tmp', dir=self.directory)
                os.close(handle)
                try:
                    reader = App(temporary)
                    reader.upsert_messages(rows)
                    with reader.db() as db:
                        db.execute('CREATE TABLE import_metadata (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
                        db.execute('INSERT INTO import_metadata VALUES (1,?)', (json.dumps(metadata, ensure_ascii=False),))
                    os.replace(temporary, destination)
                finally:
                    Path(temporary).unlink(missing_ok=True)
        return self.inspect(metadata['id'])

    def items(self):
        items = [self.metadata(path.stem) for path in self.directory.glob('*.sqlite3')]
        account_id=self.app.exporter.state().get('user_id')
        items=[item for item in items if item.get('origin',{}).get('kind')!='account-export' or item['origin'].get('account_id')==account_id]
        return dict(items=sorted(items, key=lambda item:item['imported_at'], reverse=True))

    def inspect(self, import_id, zone='Asia/Shanghai'):
        reader = self.reader(import_id, zone)
        with reader.db() as db:
            rows = [json.loads(r['body']) for r in db.execute('SELECT body FROM messages ORDER BY created,id')]
            reports = [json.loads(r['body']) for r in db.execute('SELECT body FROM report_snapshots ORDER BY rowid DESC')]
        from .activity import user_message
        users = [row for row in rows if user_message(row)]
        origin = reader.metadata.get('origin', {})
        if origin.get('kind') == 'account-export':
            zone_reader = self.reader(import_id, origin['timezone'])
            start, end = (minute_window(origin['start_time'], origin['end_time'], zone_reader.report_timezone)
                          if origin.get('start_time') else zone_reader.window(origin['date']))
            users = [row for row in users if start <= datetime.fromisoformat(row['created']) < end]
            context_count = sum(not start <= datetime.fromisoformat(row['created']) < end for row in rows)
        else: context_count = 0
        counts = Counter(row['author_id'] for row in users)
        return dict(**reader.metadata, timezone=zone, members={row['author_id']:row['name'] for row in users}, member_counts=dict(counts),
                    analysis_count=len(users), context_count=context_count,
                    dates=sorted({datetime.fromisoformat(row['created']).astimezone(reader.report_timezone).date().isoformat() for row in users}),
                    reports=[dict(id=r['report_id'], member=r['subject'], kind=r['kind'], date=r['date'], window=r['window'], timezone=r['window']['timezone']) for r in reports])

    def history(self, server, channel):
        identifier(server); identifier(channel)
        reports = []
        for item in self.items()['items']:
            if item['server'] != server or item['channel'] != channel: continue
            with self.reader(item['id']).db() as db:
                snapshots = [json.loads(row['body']) for row in db.execute('SELECT body FROM report_snapshots')]
            reports.extend(dict(id=r['report_id'], import_id=item['id'], member=r['subject'], kind=r['kind'], date=r['date'], window=r['window'],
                                timezone=r['window']['timezone'], created=r.get('generated_at',item['imported_at'])) for r in snapshots)
        return dict(reports=sorted(reports,key=lambda r:r['created'],reverse=True))

    def decorate(self, reader, report):
        return dict(report, source=dict(kind='discord-export', import_id=reader.metadata['id'], filename=reader.metadata['filename'],
                                       method=reader.metadata.get('origin',{}).get('kind','file')),
                    server_name=reader.metadata['server_name'], channel_name=reader.metadata['channel_name'])

    def report(self, data):
        fields = {'date','end_date','member','kind','period','mode','topic','focus_member','focus_topic','start_time','end_time','instructions'}
        if (not isinstance(data, dict) or not {'import_id','server','channel'} <= data.keys()
                or not data.keys() <= fields | {'import_id','server','channel','timezone'}):
            raise ValueError('请选择记录、报告内容及时间范围。')
        if data.get('kind','member') not in ('daily','weekly','member','discussion'):
            raise ValueError('请选择有效的报告类型。')
        reader = self.reader(data['import_id'], data.get('timezone', 'Asia/Shanghai'))
        # FileReportApp 只替换取数，LiveApp.report 实际执行原生成、身份/出处校验与存档。
        request = {key:value for key,value in data.items() if key in fields}
        request.setdefault('kind','member')
        origin = reader.metadata.get('origin',{})
        if origin.get('kind') == 'account-export':
            left,right = (minute_window(request.get('start_time',''),request.get('end_time',''),reader.report_timezone)
                          if request.get('start_time') or request.get('end_time') else
                          reader.window(request.get('date',''),request.get('kind')=='weekly' or request.get('period')=='weekly',request.get('end_date','')))
            original_zone = checked_timezone(origin['timezone'])
            first,last = (minute_window(origin['start_time'],origin['end_time'],original_zone) if origin.get('start_time') else
                          self.reader(data['import_id'],origin['timezone']).window(origin['date']))
            if left < first or right > last:
                raise ValueError('报告周期超出这次读取的范围，请先读取所选时间；引用背景不能作为其他周期的正文。')
        result = reader.report(server=data['server'], channel=data['channel'], **request)
        result['generated_at'] = datetime.now(timezone.utc).isoformat()
        with reader.db() as db:
            db.execute('UPDATE report_snapshots SET body=? WHERE id=?', (json.dumps(result,ensure_ascii=False),result['report_id']))
        return self.decorate(reader, result)

    def saved(self, import_id, server, channel, report_id):
        reader = self.reader(import_id)
        reader.bootstrap(server, channel)
        if not isinstance(report_id, str) or not re.fullmatch('[a-f0-9]{32}', report_id): raise ValueError('请选择文件内已生成的日报。')
        with reader.db() as db:
            row = db.execute('SELECT body FROM report_snapshots WHERE id=?', (report_id,)).fetchone()
        if not row: raise ValueError('这份文件中没有所选日报。')
        report = json.loads(row['body'])
        if report['server'] != server or report['channel'] != channel: raise ValueError('日报范围与文件不符。')
        return self.decorate(reader, report)
