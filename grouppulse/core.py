"""合成数据专用业务层；仅使用本地模拟模型，不包含外部服务客户端。"""

import hashlib
import json
import math
import os
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .reports import build_digest
from .generation import LocalMockModel, facts, generate
from .layouts import DEFAULT_LAYOUT, LAYOUTS
from .delivery import payloads
from .questions import answer_question, retrieve, fit_input, memory, byte_size, request_messages, MEMORY_TURNS

ROOT = Path(__file__).resolve().parent.parent
DEMO_PATH = ROOT / 'fixtures' / 'demo.json'
TZ = timezone(timedelta(hours=8))
DEFAULT_CONFIG = dict(base_url='', model='', protocol='openai-compatible',
                      timeout=30, retries=1, max_tokens=2048, temperature=None)
KINDS = {'daily', 'weekly', 'member', 'discussion', 'activity'}


class App:
    def __init__(self, db_path, model=None):
        self.path = Path(db_path)
        self.model = model if model is not None else LocalMockModel()
        self.bot_secrets = {}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.secret = ''  # 离线演示仅内存保存，不写数据库/文件/日志。
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, channel TEXT NOT NULL,
                    created TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coverage (
                    channel TEXT, day TEXT, PRIMARY KEY(channel, day));
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts (
                    fingerprint TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                    keyword TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS message_window ON messages(channel, created, id);
                CREATE TABLE IF NOT EXISTS message_changes (channel TEXT PRIMARY KEY, version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS message_updates (seq INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, message_id TEXT);
                CREATE INDEX IF NOT EXISTS message_update_scope ON message_updates(channel,seq);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, server TEXT, channel TEXT, start_date TEXT, end_date TEXT,
                    data_version TEXT, title TEXT, updated TEXT);
                CREATE TABLE IF NOT EXISTS question_turns (
                    seq INTEGER PRIMARY KEY, conversation_id TEXT, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS conversation_turns ON question_turns(conversation_id, seq);
                CREATE TABLE IF NOT EXISTS question_memory (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bots (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS report_snapshots (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, body TEXT NOT NULL);
            ''')
        os.chmod(self.path, 0o600)
        self.demo = json.loads(DEMO_PATH.read_text(encoding='utf-8'))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_demo(self):
        self.upsert_messages(self.demo['messages'])
        with self.db() as db:
            db.executemany('INSERT OR IGNORE INTO coverage VALUES (?, ?)',
                           [(c['channel'], c['date']) for c in self.demo['coverage']])
        return {'message': '合成样例已载入。相同消息不会重复计数。', 'offline': True}

    def ensure_demo(self):
        # 合成样例更新按消息 ID 幂等导入，不清除配置和提醒账本。
        self.import_demo()

    def upsert_messages(self, messages):
        rows = []
        for m in messages:
            body = dict(m)
            body.pop('version', None)
            encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)
            body['version'] = hashlib.sha256(encoded.encode()).hexdigest()[:16]
            rows.append((body['id'], body['channel'], body['created'],
                         json.dumps(body, ensure_ascii=False)))
        with self.db() as db:
            for row in rows:
                old = db.execute('SELECT channel, body FROM messages WHERE id=?', (row[0],)).fetchone()
                if old and old['body'] == row[3]:
                    continue
                db.execute('''INSERT INTO messages VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET channel=excluded.channel,
                    created=excluded.created, body=excluded.body''', row)
                for channel in {row[1], old['channel'] if old else row[1]}:
                    db.execute('''INSERT INTO message_changes VALUES (?, 1) ON CONFLICT(channel)
                        DO UPDATE SET version=version+1''', (channel,))
                    db.execute('INSERT INTO message_updates(channel,message_id) VALUES (?,?)',(channel,row[0]))

    def message(self, message_id):
        with self.db() as db:
            row = db.execute('SELECT body FROM messages WHERE id=?', (message_id,)).fetchone()
        return json.loads(row['body']) if row else None

    def server_channels(self, server):
        if server not in self.demo['servers']:
            raise ValueError('请选择样例中的服务器。')
        return self.demo['servers'][server]['channels']

    def bootstrap(self, server='community', channel=''):
        channel_ids = self.server_channels(server)
        if channel and channel not in channel_ids:
            raise ValueError('请选择当前服务器内的频道。')
        with self.db() as db:
            messages = [json.loads(r['body']) for r in db.execute('SELECT body FROM messages ORDER BY created')]
        messages = [m for m in messages if m['channel'] in channel_ids]
        if channel:
            messages = [m for m in messages if m['channel'] == channel]
        people = {m['author_id']: m['name'] for m in messages if not m['bot']}
        topic_ids = {m['topic'] for m in messages}
        end_day = Date.fromisoformat(self.demo['date'])
        return dict(offline=True, date=self.demo['date'], question_defaults=dict(
                    start_date=(end_day - timedelta(days=6)).isoformat(), end_date=end_day.isoformat(), memory_turns=MEMORY_TURNS), selected_server=server,
                    selected_channel=channel or next(iter(channel_ids)),
                    servers={key: value['name'] for key, value in self.demo['servers'].items()},
                    channels={key: self.demo['channels'][key] for key in channel_ids},
                    topics={key: value for key, value in self.demo['topics'].items() if key in topic_ids},
                    members=people, config=self.get_config(), layouts=LAYOUTS, default_layout=DEFAULT_LAYOUT)

    def window(self, date, weekly=False, end_date=''):
        try:
            day = Date.fromisoformat(date)
            last = Date.fromisoformat(end_date) if end_date else None
        except (ValueError, TypeError):
            raise ValueError('请选择有效日期。') from None
        if last is not None and last < day:
            raise ValueError('结束日期不能早于开始日期。')
        if weekly and last is None:
            day -= timedelta(days=day.weekday())
        try:
            start = datetime.combine(day, time.min, TZ)
            end = datetime.combine(last + timedelta(days=1), time.min, TZ) if last else start + timedelta(days=7 if weekly else 1)
            return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        except OverflowError:
            raise ValueError('日期超出可支持范围，请重新选择。') from None

    def read_window(self, channel, start, end):
        with self.db() as db:
            rows = db.execute('''SELECT body FROM messages
                WHERE channel=? AND created>=? AND created<? ORDER BY created, id''',
                              (channel, start.isoformat(), end.isoformat())).fetchall()
        return [json.loads(r['body']) for r in rows]

    def covered(self, channel, start, end):
        day, end_day = start.astimezone(TZ).date(), end.astimezone(TZ).date()
        with self.db() as db:
            days = {r['day'] for r in db.execute('SELECT day FROM coverage WHERE channel=?', (channel,))}
        while day < end_day:
            if day.isoformat() not in days:
                return False
            day += timedelta(days=1)
        return True

    def contexts(self, messages, channel, max_depth=6):
        found = {}
        for source in messages:
            current = source
            target = source.get('reference_id')
            seen = {source['id']}
            depth = 0
            while target:
                if target in seen:
                    found['cycle:' + source['id']] = dict(id=target, status='cycle',
                                                         reason='引用形成循环，已停止补读。')
                    break
                seen.add(target)
                if depth >= max_depth:
                    found['limit:' + source['id']] = dict(id=target, status='truncated',
                                                         reason='已达到引用补读深度上限。')
                    break
                row = self.message(target)
                if (not row or row['channel'] != channel
                        or current.get('reference_server',row.get('server')) != row.get('server')
                        or current.get('reference_channel',row.get('source_channel',row['channel'])) != row.get('source_channel',row['channel'])):
                    found[target] = dict(id=target, status='missing', reason='合成库中无可读原文，不能还原引用。')
                    break
                found[target] = dict(id=target, status='available', message=row,
                                     reason='引用背景，不作为目标人物观点或额外活跃消息。')
                target = row.get('reference_id')
                current = row
                depth += 1
        return list(found.values())

    def report(self, date='2026-10-21', channel='lobby', kind='daily', member='', topic='', server='community',
               mode='compact', period='daily', focus_member='', focus_topic='', end_date=''):
        if channel not in self.server_channels(server):
            raise ValueError('请选择当前服务器内的频道。')
        if kind not in KINDS:
            raise ValueError('请选择有效的分析类型。')
        if period not in ('daily', 'weekly'):
            raise ValueError('请选择单日或指定周期作为摘要范围。')
        if mode not in ('compact', 'full'):
            raise ValueError('请选择精简或完整阅读方式。')
        periodic = kind in ('daily', 'weekly')
        if periodic:
            period = kind
        elif kind == 'activity':
            period = 'daily'
        if not periodic and (focus_member or focus_topic):
            raise ValueError('关注维度仅适用于日报和周报。')
        scope = self.bootstrap(server)
        if (kind == 'member' and member not in scope['members']) or (focus_member and focus_member not in scope['members']):
            raise ValueError('请选择要查看的成员。')
        if (kind == 'discussion' and topic not in scope['topics']) or (focus_topic and focus_topic not in scope['topics']):
            raise ValueError('请选择要查看的话题。')
        start, end = self.window(date, period == 'weekly', end_date)
        if period == 'daily' and end - start != timedelta(days=1):
            raise ValueError('单日模式只支持一天，请选择区间摘要或周报。')
        raw = self.read_window(channel, start, end)
        users = [m for m in raw if not m['bot']]
        complete = self.covered(channel, start, end)
        window = dict(start=start.isoformat(), end=end.isoformat(),
                      start_date=f'{start.astimezone(TZ):%Y-%m-%d}',
                      end_date=f'{(end - timedelta(days=1)).astimezone(TZ):%Y-%m-%d}',
                      days=(end - start).days,
                      label=f'{start.astimezone(TZ):%Y-%m-%d} → {(end - timedelta(days=1)).astimezone(TZ):%Y-%m-%d}',
                      timezone='Asia/Shanghai')

        def coverage_for(contexts):
            missing = sum(c['status'] != 'available' for c in contexts)
            return dict(status='partial' if missing else ('complete' if complete else 'unknown'),
                        window_complete=complete, missing=missing,
                        note=('合成样例窗口已完整导入。' if complete else '此窗口缺少完整覆盖记录，不代表真实零活跃。')
                        + (f' 有 {missing} 处引用缺失或补读停止。' if missing else ''))

        def summary_for(summary_kind, subject_id):
            field = 'author_id' if summary_kind == 'member' else 'topic'
            evidence = [m for m in users if m[field] == subject_id]
            contexts = self.contexts(evidence, channel)
            names = scope['members'] if summary_kind == 'member' else scope['topics']
            return dict(kind=summary_kind, period=period, subject=dict(id=subject_id, name=names[subject_id]),
                        selected_count=len(evidence), messages=evidence, contexts=contexts,
                        coverage=coverage_for(contexts))

        summary = summary_for(kind, member if kind == 'member' else topic) if kind in ('member', 'discussion') else None
        selected = summary['messages'] if summary else users
        contexts = summary['contexts'] if summary else self.contexts(selected, channel)
        dimensions = {}
        if focus_member:
            dimensions['member'] = summary_for('member', focus_member)
        if focus_topic:
            dimensions['discussion'] = summary_for('discussion', focus_topic)
        try:
            previous_start = start - (end - start)
        except OverflowError:
            raise ValueError('日期范围过早，无法比较上一时段，请选择较晚的日期。') from None
        previous = [m for m in self.read_window(channel, previous_start, start) if not m['bot']]
        comparable = complete and self.covered(channel, previous_start, start)
        current_topics = Counter(m['topic'] for m in users)
        previous_topics = Counter(m['topic'] for m in previous)
        trends = []
        for tid in sorted(set(current_topics) | set(previous_topics), key=lambda t: (-current_topics[t], t)):
            current, prior = current_topics[tid], previous_topics[tid]
            if not comparable:
                state = '覆盖不足，暂不比较'
            elif not prior:
                state = '本期出现'
            elif current - prior >= 5 and (current - prior) / prior >= .25:
                state = '升温'
            elif prior - current >= 5 and (prior - current) / prior >= .25:
                state = '降温'
            else:
                state = '持续讨论'
            trends.append(dict(id=tid, title=self.demo['topics'][tid], current=current,
                               previous=prior, state=state,
                               members=len({m['author_id'] for m in users if m['topic'] == tid})))
        groups = []
        for tid in dict.fromkeys(m['topic'] for m in selected):
            evidence = [m for m in selected if m['topic'] == tid]
            groups.append(dict(id=tid, title=self.demo['topics'][tid], messages=evidence))
        hours = Counter(datetime.fromisoformat(m['created']).astimezone(TZ).hour for m in users)
        coverage = summary['coverage'] if summary else coverage_for(contexts)
        if kind == 'activity':
            # 原始统计与关键词提醒不依赖摘要模型。
            generated = dict(digest=build_digest(selected, self.demo['topics'], self.demo.get('annotations', {}), mode=mode),
                             generation=dict(engine='none', status='not_requested'))
        else:
            data = dict(request=dict(kind=kind, period=period, member=member if kind == 'member' else '',
                                     topic=topic if kind == 'discussion' else '',
                                     focus_member=focus_member, focus_topic=focus_topic),
                        scope=dict(server=server, channel=channel, **window),
                        facts=facts(selected, coverage), messages=selected, contexts=contexts,
                        allowedMessageIds=[m['id'] for m in selected],
                        dimensionMessageIds={key: [m['id'] for m in item['messages']] for key, item in dimensions.items()},
                        dimensionContexts={key: item['contexts'] for key, item in dimensions.items()},
                        dimensionFacts={key: facts(item['messages'], item['coverage']) for key, item in dimensions.items()},
                        topicTitles={key: self.demo['topics'][key] for key in {m['topic'] for m in selected}},
                        annotations={m['id']: self.demo['annotations'][m['id']] for m in selected
                                     if m['id'] in self.demo.get('annotations', {})})
            generated = generate(self.model, data, dimensions, mode)
        result = dict(offline=True, kind=kind, date=date, period=period, channel=channel, server=server,
                    subject=summary['subject'] if summary else None, dimensions=dimensions,
                    window=window,
                    metrics=dict(messages=len(users), members=len({m['author_id'] for m in users}),
                                 replies=sum(bool(m.get('reference_id')) for m in users),
                                 bots=len(raw) - len(users), previous=len(previous), comparable=comparable,
                                 hours=[dict(hour=h, count=hours[h]) for h in range(24)]),
                    selected_count=len(selected), messages=selected, groups=groups, contexts=contexts,
                    **generated, coverage=coverage,
                    trends=trends)
        if kind != 'activity':
            result['report_id'] = uuid.uuid4().hex
            with self.db() as db:
                db.execute('INSERT INTO report_snapshots VALUES (?, ?)', (result['report_id'], json.dumps(result, ensure_ascii=False)))
        return result

    def channel_version(self, channel):
        with self.db() as db:
            row = db.execute('SELECT version FROM message_changes WHERE channel=?', (channel,)).fetchone()
        return str(row['version'] if row else 0)

    def conversations(self, server, channel):
        if not isinstance(server, str) or not isinstance(channel, str) or channel not in self.server_channels(server):
            raise ValueError('请先选择一个服务器和其中的一个频道。')
        with self.db() as db:
            rows = db.execute('''SELECT id,title,start_date,end_date,updated FROM conversations
                WHERE server=? AND channel=? ORDER BY updated DESC, rowid DESC LIMIT 50''', (server, channel))
            return dict(items=[dict(r) for r in rows], limit=50, offline=not getattr(self,'live',False))

    def conversation(self, conversation_id, server, channel, before=''):
        self.conversations(server, channel)  # 同样校验服务器及频道归属。
        if not isinstance(conversation_id, str) or not isinstance(before, str) or (before and
            (not before.isascii() or not before.isdecimal() or len(before) > 19 or int(before) > 9223372036854775807)):
            raise ValueError('请选择有效的历史对话。')
        with self.db() as db:
            row = db.execute('SELECT * FROM conversations WHERE id=? AND server=? AND channel=?',
                             (conversation_id, server, channel)).fetchone()
            if not row:
                raise ValueError('对话范围不匹配或对话不存在，请新建对话。')
            rows = db.execute('''SELECT seq,body FROM question_turns WHERE conversation_id=? AND seq<?
                ORDER BY seq DESC LIMIT 51''', (conversation_id, int(before) if before else 9223372036854775807)).fetchall()
        return dict(**dict(row), stale=row['data_version'] != self.channel_version(channel),
                    turns=[dict(seq=r['seq'], **json.loads(r['body'])) for r in reversed(rows[:50])],
                    has_more=len(rows) > 50, offline=not getattr(self,'live',False))

    def ask(self, question, server, channel, start_date='', end_date='', conversation_id='', *, _asked_at=None):
        asked_at = _asked_at or datetime.now(timezone.utc).isoformat()
        if not isinstance(server, str) or not isinstance(channel, str) or channel not in self.server_channels(server):
            raise ValueError('请先选择一个服务器和其中的一个频道。')
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
            raise ValueError('请输入1至2000字的问题。')
        if not all(isinstance(value, str) for value in (start_date, end_date, conversation_id)):
            raise ValueError('问答范围有误，请重新选择。')
        if bool(start_date) != bool(end_date):
            raise ValueError('限定日期时，请同时填写开始和结束日期。')
        question = question.strip()
        if not start_date:
            end_date = self.demo['date']
            start_date = (Date.fromisoformat(end_date) - timedelta(days=6)).isoformat()
        start, end = self.window(start_date, end_date=end_date)
        session = self.conversation(conversation_id, server, channel) if conversation_id else None
        if session and (session['start_date'], session['end_date']) != (start_date, end_date):
            raise ValueError('对话范围已改变，请新建对话后重新提问。')
        if session and any(turn['response']['scope'].get('timezone','Asia/Shanghai') != getattr(self,'report_timezone_name','Asia/Shanghai') for turn in session['turns']):
            raise ValueError('对话时区已改变，请新建对话后重新提问。')
        data_version = self.channel_version(channel)
        refreshed = bool(session and session['data_version'] != data_version)
        if refreshed and not getattr(self,'live',False):
            raise ValueError('本地记录已更新，历史仍可查看，请新建对话后重新提问。')
        turns = session['turns'] if session else []
        if getattr(self,'live',False):
            turns = [turn for turn in turns if turn['response']['scope']['data_version']==data_version]
            return self.ask_reference(question,server,channel,start_date,end_date,start,end,
                                      conversation_id,turns,data_version,refreshed,asked_at)
        conversation = memory(turns)
        complete = self.covered(channel, start, end)
        input_budget = getattr(self.model, 'input_budget', 24576)
        window_params = (channel, start.isoformat(), end.isoformat())
        with self.db() as db:
            where = 'channel=? AND created>=? AND created<? AND json_extract(body, \'$.bot\')=0 AND coalesce(json_extract(body, \'$.deleted\'),0)=0'
            total = db.execute(f'SELECT COUNT(*) FROM messages WHERE {where}', window_params).fetchone()[0]
            topics = {r[0] for r in db.execute(f'SELECT DISTINCT json_extract(body, \'$.topic\') FROM messages WHERE {where}', window_params)}
            rows = (json.loads(r['body']) for r in db.execute(f'SELECT body FROM messages WHERE {where} ORDER BY created,id', window_params))
            selected, matches = retrieve(rows, question, {k: v for k, v in self.demo['topics'].items() if k in topics},
                                        conversation,min(10240,input_budget // 2))
        contexts = self.contexts(selected, channel)
        coverage = dict(window_complete=complete)
        scope = dict(server=server, channel=channel, start_date=start_date, end_date=end_date,
                     label=f'{start_date} → {end_date}', timezone=getattr(self,'report_timezone_name','Asia/Shanghai'),
                     data_version=data_version)
        data = dict(question=question, scope=scope, conversation=conversation,
                    messages=selected, contexts=contexts, allowedMessageIds=[m['id'] for m in selected],
                    facts=dict(scope_messages=total, coverage=coverage))
        fit_input(data, input_budget)
        selected, contexts = data['messages'], data['contexts']
        limited = matches > len(selected)
        missing = sum(c['status'] != 'available' for c in contexts)
        budget_limited = limited or any(c['status'] == 'budget' for c in contexts)
        coverage.update(status='partial' if missing or limited else ('complete' if complete else 'unknown'),
                        missing=missing, retrieval_complete=not limited, budget_limited=budget_limited,
                        note=(coverage.get('note') or (('所选合成记录窗口完整。' if complete else '本地记录覆盖不完整，答案不代表频道全部历史。')))
                        + (f' 有{missing}处引用未补齐（含长度限制）。' if missing else '')
                        + (' 聊天已更新，本轮重新检索并清除旧回答背景；历史记录保留。' if refreshed else '')
                        + (f' 按长度预算选入{len(selected)}/{matches}条匹配原文，不能当作全部讨论；可缩小日期或问题范围。' if limited else ''))
        data['facts']['retrieved_messages'] = len(selected)
        answer = answer_question(self.model, data)
        conversation_id = conversation_id or uuid.uuid4().hex
        result = dict(offline=not getattr(self,'live',False), question=question, scope=scope, conversation_id=conversation_id,
                    messages=selected, contexts=contexts, coverage=coverage,
                    metrics=dict(scope_messages=total, scanned_messages=total, retrieved_messages=len(selected), matched_messages=matches,
                                 memory_turns=len(conversation), memory_limit=MEMORY_TURNS,
                                 memory_refreshed=refreshed,
                                 memory_shortened=bool(conversation), input_bytes=byte_size(request_messages(data)), input_limit_bytes=input_budget), **answer)
        now = datetime.now(timezone.utc).isoformat()
        result.update(asked_at=asked_at,answered_at=now)
        with self.db() as db:
            db.execute('''INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,data_version=excluded.data_version''',
                       (conversation_id, server, channel, start_date, end_date, data_version, question[:40], now))
            db.execute('INSERT INTO question_turns(conversation_id,body) VALUES (?, ?)',
                       (conversation_id, json.dumps(dict(question=question, response=result), ensure_ascii=False)))
        return result

    def ask_reference(self, question, server, channel, start_date, end_date, start, end,
                      conversation_id, turns, data_version, refreshed, asked_at):
        from .reference_questions import ask_channel
        conversation_id = conversation_id or uuid.uuid4().hex
        scope = dict(server=server,channel=channel,start_date=start_date,end_date=end_date,
            data_version=data_version,timezone=getattr(self,'report_timezone_name','Asia/Shanghai'),server_name=self.demo['servers'][server]['name'],
            channel_name=self.demo['channels'][channel],
            label=f'{self.demo["servers"][server]["name"]} / #{self.demo["channels"][channel]}')
        scope.update(getattr(self,'question_scope_metadata',{}))
        memory = None
        with self.db() as db:
            row = db.execute('SELECT body FROM question_memory WHERE id=?',(conversation_id,)).fetchone()
        if row:
            stored = json.loads(row['body'])
            if all(stored['scope'].get(key)==scope[key] for key in
                   ('server','channel','start_date','end_date','data_version','timezone')):
                candidate = stored['memory']
                # 旧宿主曾把全文写回摘录；交回复制模块从原历史恢复，不另写裁短算法。
                excerpts = candidate.get('turns',[]) + candidate.get('pending',[])
                if not any(len(turn.get('question','').encode('utf-16-le'))//2 > 257
                           or len(turn.get('answer','').encode('utf-16-le'))//2 > 513 for turn in excerpts):
                    memory = candidate
        answer = ask_channel(self,question,scope,start,end,conversation_id,turns,memory)
        state = answer.pop('_memory')
        answer['metrics']['memory_refreshed'] = refreshed
        if refreshed:
            answer['coverage']['note'] += ' 聊天已更新，本轮重新检索并清除旧回答背景；历史记录保留。'
        result = dict(offline=False,readonly=True,question=question,scope=scope,
                      conversation_id=conversation_id,**answer)
        now = datetime.now(timezone.utc).isoformat()
        result.update(asked_at=asked_at,answered_at=now)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT version FROM message_changes WHERE channel=?',(channel,)).fetchone()
            if str(current['version'] if current else 0) != data_version:
                observed = result['messages'] + [item['message'] for item in result['contexts'] if item['status']=='available']
                for original in observed:
                    row = db.execute('SELECT body FROM messages WHERE id=?',(original['id'],)).fetchone()
                    latest = json.loads(row['body']) if row else None
                    if not latest or latest.get('deleted') or latest.get('version') != original.get('version'):
                        raise ValueError('引用记录已更新，本次回答未保存；请再次提问以使用最新原文。')
                result['coverage']['note'] += ' 查询期间有新增记录；本回答基于本轮已取证原文，下次提问将重新检索。'
            db.execute('''INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,data_version=excluded.data_version''',
                (conversation_id,server,channel,start_date,end_date,data_version,question[:40],now))
            db.execute('INSERT INTO question_turns(conversation_id,body) VALUES (?, ?)',
                (conversation_id,json.dumps(dict(question=question,response=result),ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO question_memory VALUES (?, ?)',
                (conversation_id,json.dumps(dict(scope=scope,memory=state),ensure_ascii=False)))
        return result

    def get_config(self):
        with self.db() as db:
            row = db.execute('SELECT body FROM settings WHERE id=1').fetchone()
        return {**DEFAULT_CONFIG, **(json.loads(row['body']) if row else {}), 'has_key': bool(self.secret)}

    def bots(self, server=''):
        if server:
            self.server_channels(server)
        with self.db() as db:
            rows = [json.loads(r['body']) for r in db.execute('SELECT body FROM bots ORDER BY rowid')]
        return dict(offline=True, items=[dict(**row, has_token=bool(self.bot_secrets.get(row['id'])),
                    status='offline_unverified') for row in rows if not server or row['server'] == server])

    def save_bot(self, data):
        fields = {'id', 'name', 'server', 'channels', 'application_id', 'token', 'clear_token'}
        if not isinstance(data, dict) or not data.keys() <= fields:
            raise ValueError('Bot 配置字段有误。')
        name, server, channels = data.get('name'), data.get('server'), data.get('channels')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 50 or not isinstance(server, str):
            raise ValueError('请填写 Bot 名称，并选择一个服务器。')
        allowed = self.server_channels(server)
        if not isinstance(channels, list) or not channels or not all(isinstance(c, str) and c in allowed for c in channels):
            raise ValueError('请至少选择一个当前服务器的目标频道。')
        application_id, token, bot_id = data.get('application_id', ''), data.get('token', ''), data.get('id', '')
        if not all(isinstance(s, str) for s in (application_id, token, bot_id)):
            raise ValueError('Bot 配置格式有误。')
        if application_id and (not application_id.isascii() or not application_id.isdecimal() or len(application_id) > 25):
            raise ValueError('Application ID 应留空或填写最多25位数字。')
        if token and (not token.startswith('demo-') or len(token) > 4096):
            raise ValueError('离线版本仅接受 demo- 开头的演示 Token，请勿填写真实凭据。')
        old = next((b for b in self.bots()['items'] if b['id'] == bot_id), None)
        if bot_id and (not old or old['server'] != server):
            raise ValueError('Bot 不存在或不属于当前服务器，请重新选择。')
        bot_id = bot_id or uuid.uuid4().hex
        body = dict(id=bot_id, name=name.strip(), server=server, channels=list(dict.fromkeys(channels)), application_id=application_id)
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO bots VALUES (?, ?)', (bot_id, json.dumps(body, ensure_ascii=False)))
        if data.get('clear_token'):
            self.bot_secrets.pop(bot_id, None)
        elif token:
            self.bot_secrets[bot_id] = token
        return next(b for b in self.bots()['items'] if b['id'] == bot_id)

    def preview_delivery(self, report_id, bot_id, destination, server, channel, kind=''):
        if not all(isinstance(s, str) for s in (report_id, bot_id, destination, server, channel, kind)):
            raise ValueError('请重新选择报告、Bot 和目标频道。')
        if channel not in self.server_channels(server):
            raise ValueError('报告来源必须属于当前服务器。')
        bot = next((b for b in self.bots(server)['items'] if b['id'] == bot_id), None)
        if not bot or destination not in bot['channels'] or destination not in self.server_channels(server):
            raise ValueError('请选择已配置的 Bot 及同服务器内允许的目标频道。')
        with self.db() as db:
            row = db.execute('SELECT body FROM report_snapshots WHERE id=?', (report_id,)).fetchone()
        if not row:
            raise ValueError('报告快照不存在，请先生成报告。')
        report = json.loads(row['body'])
        if (report['server'], report['channel']) != (server, channel):
            raise ValueError('报告来源与当前服务器、频道不匹配，请重新生成。')
        selected = report
        if kind and kind != report['kind']:
            selected = report['dimensions'].get(kind)
            if not selected:
                raise ValueError('该摘要不在报告快照中，请先生成所需维度。')
        if selected['generation']['status'] not in ('complete', 'partial') or not selected.get('fullDigest'):
            raise ValueError('该报告或摘要尚未有效生成，无法预览。')
        rendered = payloads({**report, 'coverage': selected['coverage']}, selected['fullDigest'], selected['kind'], self.demo['servers'][server]['name'],
                            self.demo['channels'][channel], selected.get('subject'))
        fingerprint = hashlib.sha256(json.dumps([server, channel, bot_id, destination, rendered], ensure_ascii=False).encode()).hexdigest()
        with self.db() as db:
            old = db.execute('SELECT body FROM deliveries WHERE fingerprint=?', (fingerprint,)).fetchone()
            if old:
                preview = json.loads(old['body'])
                preview['bot_name'] = bot['name']
                db.execute('UPDATE deliveries SET body=? WHERE id=?', (json.dumps(preview, ensure_ascii=False), preview['id']))
                return preview
            result = dict(id=uuid.uuid4().hex, report_id=report_id, kind=selected['kind'], bot_id=bot_id, bot_name=bot['name'],
                          server=server, channel=channel, destination=destination, payloads=rendered, offline=True,
                          status='preview', sent=False, created=datetime.now(timezone.utc).isoformat())
            db.execute('INSERT INTO deliveries VALUES (?, ?, ?)', (result['id'], fingerprint, json.dumps(result, ensure_ascii=False)))
        return result

    def simulate_delivery(self, preview_id, server, channel):
        if not all(isinstance(s, str) for s in (preview_id, server, channel)) or channel not in self.server_channels(server):
            raise ValueError('请选择当前范围的发送预览。')
        with self.db() as db:
            row = db.execute('SELECT body FROM deliveries WHERE id=?', (preview_id,)).fetchone()
            if not row:
                raise ValueError('预览不存在，请重新预览。')
            result = json.loads(row['body'])
            if (result['server'], result['channel']) != (server, channel):
                raise ValueError('预览不属于当前服务器、频道，请重新预览。')
            bot = next((b for b in self.bots(server)['items'] if b['id'] == result['bot_id']), None)
            if not bot or result['destination'] not in bot['channels']:
                raise ValueError('Bot 配置已改变，请重新选择目标频道并预览。')
            duplicate = result['status'] == 'simulated'
            result.update(status='simulated', sent=False)
            db.execute('UPDATE deliveries SET body=? WHERE id=?', (json.dumps(result, ensure_ascii=False), preview_id))
        return dict(**result, duplicate=duplicate,
                    message='已记录本地模拟，不会向 Discord 发送消息。' if not duplicate else '这份内容已模拟过，未重复记录；没有实际发送。')

    def validate_config(self, data):
        if not isinstance(data, dict):
            raise ValueError('配置格式有误，请重新填写。')
        config = {**DEFAULT_CONFIG, **{k: v for k, v in data.items() if k in DEFAULT_CONFIG}}
        if not isinstance(config['base_url'], str) or not isinstance(config['model'], str):
            raise ValueError('服务地址和模型名称应为文本。')
        config['base_url'], config['model'] = config['base_url'].strip(), config['model'].strip()
        try:
            url = urlsplit(config['base_url'])
            port = url.port
        except ValueError:
            raise ValueError('服务地址格式有误，请检查域名和端口。') from None
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('请填写以 https:// 或 http:// 开头、不含密钥和查询参数的服务地址。')
        if not config['model'] or len(config['model']) > 200:
            raise ValueError('请填写模型名称，例如 demo-model。')
        if config['protocol'] != 'openai-compatible':
            raise ValueError('离线版本仅支持 OpenAI Compatible 配置。')
        for key, low, high, label in [('timeout', 1, 1800, '超时秒数'), ('retries', 0, 5, '重试次数'),
                                      ('max_tokens', 1, 131072, '最大输出长度')]:
            try:
                if isinstance(config[key], bool) or float(config[key]) != int(config[key]):
                    raise ValueError()
                value = int(config[key])
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f'{label}应填写整数。') from None
            if not low <= value <= high:
                raise ValueError(f'{label}应在 {low} 至 {high} 之间。')
            config[key] = value
        if config['temperature'] in ('', None):
            config['temperature'] = None
        else:
            try:
                value = float(config['temperature'])
            except (ValueError, TypeError):
                raise ValueError('Temperature 应留空或填写 0 至 2 的数值。') from None
            if not math.isfinite(value) or not 0 <= value <= 2:
                raise ValueError('Temperature 应在 0 至 2 之间。')
            config['temperature'] = value
        key = data.get('api_key', '')
        if not isinstance(key, str) or len(key) > 4096:
            raise ValueError('演示密钥格式不正确。')
        if key and not key.startswith('demo-'):
            raise ValueError('离线版本请使用 demo- 开头的演示密钥，不要填写真实凭据。')
        return config

    def save_config(self, data):
        config = self.validate_config(data)
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (1, ?)',
                       (json.dumps(config, ensure_ascii=False),))
        if data.get('clear_key'):
            self.secret = ''
        elif data.get('api_key'):
            self.secret = data['api_key']
        return self.get_config()

    def check_config(self, data):
        self.validate_config(data)
        if data.get('clear_key') or not (data.get('api_key') or self.secret):
            raise ValueError('请填写 demo- 开头的演示密钥，再执行离线检查。')
        return dict(status='offline_valid', offline=True,
                    message='格式检查通过，未连接模型。地址是否可达、密钥和模型是否有效尚未验证；不产生费用。')

    def check_alerts(self, keyword, channel='lobby', date='2026-10-21', server='community'):
        if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 50:
            raise ValueError('请填写 1 至 50 个字的关注关键词。')
        keyword = keyword.strip()
        report = self.report(date=date, channel=channel, server=server, kind='activity')
        created = 0
        with self.db() as db:
            for m in report['messages']:
                if keyword.casefold() not in m['content'].casefold():
                    continue
                fingerprint = hashlib.sha256(json.dumps([channel, keyword.casefold(), m['id'], m['version']]).encode()).hexdigest()
                created += db.execute('INSERT OR IGNORE INTO alerts VALUES (?, ?, ?, ?)',
                                      (fingerprint, m['id'], keyword, json.dumps(m, ensure_ascii=False))).rowcount
        return dict(created=created, offline=True, alerts=self.alerts(channel, server))

    def alerts(self, channel='', server='community'):
        channel_ids = self.server_channels(server)
        if channel and channel not in channel_ids:
            raise ValueError('请选择当前服务器内的频道。')
        with self.db() as db:
            rows = db.execute('SELECT keyword, body FROM alerts ORDER BY rowid DESC').fetchall()
        result = [dict(keyword=r['keyword'], message=json.loads(r['body'])) for r in rows]
        return [a for a in result if a['message']['channel'] in channel_ids
                and (not channel or a['message']['channel'] == channel)]
