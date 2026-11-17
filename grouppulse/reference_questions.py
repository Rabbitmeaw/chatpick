"""原 Query Agent 的 QuickJS 宿主与 Discord 只读 SQLite 适配。"""

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .generation import GenerationError, require
from .questions import NO_EVIDENCE, byte_size

VENDOR = Path(__file__).resolve().parents[1] / 'vendor/starmemo/query'
SOURCES = tuple((VENDOR / name).read_text() for name in ('host.js', 'query-core.js'))
QA_VERSION = 'starmemo-query-agent-05bda4a-discord-v1'
TZ = timezone(timedelta(hours=8))


class QueryRuntime:
    def __init__(self):
        try:
            import quickjs
        except ImportError:
            raise GenerationError('缺少问答运行依赖，请使用“启动拾聊”入口。') from None
        self.context = quickjs.Context()
        self.context.set_memory_limit(64 * 1024 * 1024)
        self.context.set_max_stack_size(1024 * 1024)
        self.context.set_time_limit(5)
        for source in SOURCES:
            self.context.eval(source)
        self.set('QueryHost.randomHex', secrets.token_hex(96))

    def set(self, name, value):
        self.context.set('_hostData', json.dumps(value, ensure_ascii=False))
        self.context.eval(name + ' = JSON.parse(_hostData)')

    def references(self, conversation, ids):
        self.set('QueryHost.referenceInput', dict(conversation=conversation, ids=ids))
        return json.loads(self.context.eval('JSON.stringify(QueryHost.referenceInput.ids.map(id => '
            'StarMemoQueryCore.encodeMessageRef(QueryHost.referenceInput.conversation, id)))'))

    def run(self, model, api, payload):
        self.set('QueryHost.input', payload)
        self.context.eval('QueryHost.start(QueryHost.input)')
        fatal = None
        model_bytes, model_calls = [], 0
        # The copied runtime bounds tool calls; this also bounds a broken host Promise bridge.
        for _ in range(40):
            self.context.set_time_limit(5)
            while self.context.execute_pending_job():
                pass
            requests = json.loads(self.context.eval('JSON.stringify(QueryHost.requests.splice(0))'))
            if not requests:
                if self.context.eval('QueryHost.error || QueryHost.result === undefined'):
                    raise GenerationError('原问答引擎未完成本次查询，请重试。')
                if fatal:
                    raise fatal
                result = json.loads(self.context.eval('QueryHost.exportResult()'))
                result.update(input_bytes=max(model_bytes, default=0), model_calls=model_calls)
                return result
            for request in requests:
                data = request['data']
                if request['kind'] == 'model':
                    size = byte_size(dict(messages=data['messages'], tools=data['tools']))
                    model_bytes.append(size)
                    api.input_bytes = size
                    model_calls += 1
                    try:
                        value = model.complete_tools(data['messages'], data['tools'],
                            timeout=(data.get('options') or {}).get('timeoutMs'))
                    except GenerationError as error:
                        # Failed background compaction must not invalidate the completed answer.
                        if not data.get('options'):
                            fatal = error
                        value = dict(success=False, error=str(error))
                elif request['kind'] == 'tool':
                    value = api.execute(data['name'], data['args'])
                else:
                    raise GenerationError('问答宿主收到了不支持的请求。')
                self.set('QueryHost.resolved', value)
                self.context.eval(f'QueryHost.finish({int(request["id"])}, QueryHost.resolved)')
        raise GenerationError('问答未在有限步骤内完成，请简化问题后重试。')


class ChannelQueryApi:
    """模型不提供 SQL、服务器或频道；四个原工具只映射到此固定窗口。"""
    def __init__(self, app, scope, start, end, runtime, budget):
        self.app, self.scope, self.start, self.end = app, scope, start, end
        self.runtime, self.budget, self.input_bytes = runtime, budget, 0
        self.conversation = scope['server'] + '/' + scope['channel']
        self.rows, self.refs, self.backgrounds = {}, {}, {}
        self.matched, self.limited, self.tool_calls = 0, False, []
        self.complete = app.covered(scope['channel'], start, end)
        self.where = ("channel=? AND created>=? AND created<? AND json_extract(body,'$.server')=? "
                      "AND json_extract(body,'$.bot')=0 AND coalesce(json_extract(body,'$.deleted'),0)=0 "
                      "AND coalesce(json_extract(body,'$.unavailable'),0)=0 "
                      "AND coalesce(json_extract(body,'$.message_type'),0) IN (0,19) "
                      "AND trim(json_extract(body,'$.content'))<>''")
        self.params = (scope['channel'], start.isoformat(), end.isoformat(), scope['server'])
        with app.db() as db:
            self.total = db.execute('SELECT COUNT(*) FROM messages WHERE ' + self.where, self.params).fetchone()[0]

    def invalid(self, field, constraint):
        return dict(status='invalid_tool_arguments', field=field, constraint=constraint,
                    hint='只能查询当前服务器的当前频道及界面所选日期。省略 target；timeRange=all 表示所选日期。')

    def query_window(self, time_range):
        kind = time_range.get('kind', 'all')
        start, end = self.start, self.end
        now = datetime.now(TZ)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if kind == 'absolute':
            start = datetime.fromtimestamp(time_range.get('startTime', start.timestamp()), timezone.utc)
            end = datetime.fromtimestamp(time_range.get('endTime', end.timestamp()), timezone.utc)
        elif kind != 'all':
            if kind == 'today': start, end = day, day + timedelta(days=1)
            elif kind == 'yesterday': start, end = day - timedelta(days=1), day
            elif kind == 'last_7_days': start, end = day - timedelta(days=6), day + timedelta(days=1)
            elif kind == 'this_week': start, end = day - timedelta(days=day.weekday()), day + timedelta(days=1)
            elif kind == 'this_month': start, end = day.replace(day=1), day + timedelta(days=1)
            elif kind == 'previous_month':
                end = day.replace(day=1); start = (end - timedelta(days=1)).replace(day=1)
            elif kind == 'this_year': start, end = day.replace(month=1, day=1), day + timedelta(days=1)
            elif kind == 'previous_year':
                end = day.replace(month=1, day=1); start = end.replace(year=end.year - 1)
            else: raise ValueError()
        # Relative model hints may narrow, but never expand the selected date window.
        start, end = max(start, self.start), min(end, self.end)
        return start, max(start, end)

    @staticmethod
    def sender(row):
        return f'{row["name"]}（成员 ID {row["author_id"]}）'

    def message(self, row, reference):
        return dict(messageRef=reference, timestamp=int(datetime.fromisoformat(row['created']).timestamp()*1000),
            datetime=datetime.fromisoformat(row['created']).astimezone(getattr(self.app,'report_timezone',TZ)).isoformat(),
            sender=self.sender(row), senderId=row['author_id'], conversationName=self.scope['channel_name'],
            conversationType='group', messageType='text', sourceKind='text', text=row['content'],
            sourceChannelId=row.get('source_channel',row['channel']),threadName=row.get('thread_name',''))

    def execute(self, name, args):
        self.tool_calls.append(name)
        if args.get('scope') != dict(kind='current', conversationId=self.conversation):
            return self.invalid('scope', 'fixed_channel')
        target = args.get('target', {}).get('query', '')
        if target and target not in (self.scope['channel'], self.scope['channel_name'], '#' + self.scope['channel_name'], self.scope['label']):
            return self.invalid('target', 'fixed_channel')
        if args.get('direction', 'any') != 'any':
            return dict(status='invalid_tool_arguments', field='direction', constraint='unknown_self',
                        hint='当前没有操作者身份映射，不能猜“我”是谁。请用 direction=any，并按返回的稳定成员 ID 区分作者。')
        if name == 'context':
            return self.context(args)
        if name not in ('messages', 'search', 'overview'):
            return self.invalid('name', 'tool_allowlist')
        try:
            start, end = self.query_window(args.get('timeRange', {}))
        except (ValueError, TypeError, OverflowError):
            return self.invalid('timeRange', 'fixed_dates')
        where, params = self.where, list(self.params)
        params[1:3] = [start.isoformat(), end.isoformat()]
        if args.get('messageTypes') and 'text' not in args['messageTypes']:
            where += ' AND 0'
        if name == 'search':
            terms = [args.get('query', ''), *args.get('variants', [])]
            terms = [term for term in terms if isinstance(term, str) and term.strip()]
            if not terms: return self.invalid('queries', 'nonempty')
            where += ' AND (' + ' OR '.join("instr(lower(json_extract(body,'$.content')),?)>0" for _ in terms) + ')'
            params.extend(term.casefold() for term in terms)
        order = 'DESC' if args.get('order') == 'desc' else 'ASC'
        with self.app.db() as db:
            matched = db.execute('SELECT COUNT(*) FROM messages WHERE ' + where, params).fetchone()[0]
            self.matched = max(self.matched, matched)
            # Source runtime assigns at most 40 citation IDs. Unshown messages do not enter the model.
            limit = min(40, args.get('limit', 40))
            if name == 'overview' and matched > limit:
                sql = ('SELECT body FROM (SELECT body,created,id,ntile(?) OVER (ORDER BY created,id) AS bucket '
                       'FROM messages WHERE ' + where + ') GROUP BY bucket ORDER BY created,id')
                rows = db.execute(sql, [limit, *params])
            else:
                rows = db.execute('SELECT body FROM messages WHERE ' + where + ' ORDER BY created ' + order + ',id ' + order + ' LIMIT ?', [*params, limit])
            selected = [json.loads(row['body']) for row in rows]
        messages, backgrounds = self.pack(selected)
        limited = len(messages) < matched
        self.limited |= limited
        result = dict(status='completed', target=dict(displayName=self.scope['channel_name'], type='group'),
            scope=dict(kind='current', conversationCount=1, displayName=self.scope['channel_name']),
            resolvedTimeRange=dict(kind='absolute',startTime=int(start.timestamp()),endTime=int(end.timestamp()),label=self.scope['label']),
            coverage=dict(state='partial' if limited or not self.complete else 'complete'),
            returnedCount=len(messages), matchedCount=matched, referenceBackground=backgrounds,
            coverageNote='仅所选频道、日期内的文字；引用背景不能单独支撑结论。' + (' 按原引擎证据数量或输入预算选取，不能当作全部讨论。' if limited else ''))
        if name == 'search':
            result.update(evidence=messages,evidenceCount=len(messages),probeCount=len(terms),
                indexCoverage=dict(covered=self.complete,summary='已直接检查所选日期的本地原始文字记录；同步覆盖仍以 coverage 为准。'))
        else:
            result['messages'] = messages
        if name == 'overview': result.update(sourceMessageCount=matched, sampled=limited)
        return result

    def pack(self, rows):
        refs = self.runtime.references(self.conversation, [row['id'] for row in rows])
        messages, backgrounds, size = [], [], 0
        available = max(0, self.budget - self.input_bytes - 4096)
        for row, ref in zip(rows, refs):
            message = self.message(row, ref)
            cost = byte_size(message) + 100
            if size + cost > available or (row['id'] not in self.rows and len(self.rows) >= 40):
                self.limited = True
                continue
            size += cost; messages.append(message)
            self.rows[row['id']] = row; self.refs[ref] = row['id']
        for item in self.app.contexts([self.rows[self.refs[m['messageRef']]] for m in messages], self.scope['channel']):
            if item['status'] == 'available':
                row = item['message']
                if (row['server'], row['channel']) != (self.scope['server'], self.scope['channel']):
                    item = dict(id=item['id'],status='missing',reason='引用不属于当前服务器频道，未读取。')
            cost = byte_size(item)
            if size + cost > available:
                item = dict(id=item['id'], status='budget', reason='引用超过本轮输入预算，未提供。')
                cost = byte_size(item); self.limited = True
            if size + cost > available: continue
            size += cost; self.backgrounds[item['id']] = item; backgrounds.append(item)
        return messages, backgrounds

    def context(self, args):
        message_id = self.refs.get(args.get('messageRef'))
        if not message_id: return self.invalid('messageRef', 'issued_reference')
        anchor = self.rows[message_id]
        stamp = (anchor['created'], anchor['created'], anchor['id'])
        where=self.where+" AND coalesce(json_extract(body,'$.source_channel'),channel)=?"
        params=(*self.params,anchor.get('source_channel',anchor['channel']))
        with self.app.db() as db:
            before = db.execute('SELECT body FROM messages WHERE ' + where + ' AND (created<? OR (created=? AND id<?)) ORDER BY created DESC,id DESC LIMIT ?', [*params,*stamp,min(50,args.get('before',5))]).fetchall()
            after = db.execute('SELECT body FROM messages WHERE ' + where + ' AND (created>? OR (created=? AND id>?)) ORDER BY created,id LIMIT ?', [*params,*stamp,min(50,args.get('after',5))]).fetchall()
        left, right = [json.loads(row['body']) for row in reversed(before)], [json.loads(row['body']) for row in after]
        messages, backgrounds = self.pack([anchor, *left, *right])
        by_id = {self.refs[m['messageRef']]:m for m in messages}
        return dict(status='completed',anchor=by_id.get(message_id),before=[by_id[r['id']] for r in left if r['id'] in by_id],
            after=[by_id[r['id']] for r in right if r['id'] in by_id],referenceBackground=backgrounds,
            coverage=dict(state='partial' if self.limited or not self.complete else 'complete'),
            returnedCount=len(messages),note='邻近消息限定同一来源频道或子线程，并受所选日期限制。')


def ask_channel(app, question, scope, start, end, conversation_id, history, memory):
    runtime = QueryRuntime()
    budget = app.model.input_budget
    api = ChannelQueryApi(app, scope, start, end, runtime, budget)
    payload = dict(question=question,scope=scope,conversationId=conversation_id,memory=memory,
        history=[dict(question=turn['question'],answer=turn['response']['answer']) for turn in history],
        model=app.get_config()['model'],now=datetime.now(timezone.utc).isoformat())
    try:
        output = runtime.run(app.model, api, payload)
    except GenerationError:
        raise
    except Exception:
        raise GenerationError('原问答引擎未能处理本次请求，请重新提问；没有改用模拟结果。') from None
    value = output['result']
    if value.get('status') != 'answered':
        raise GenerationError(value.get('message') or '本次查询未完成，没有改用模拟结果。')
    try:
        require(not value.get('invalidCitationIds'))
        require(isinstance(value.get('answer'),str) and 0 < len(value['answer'].encode()) <= 16000)
        evidence = value.get('evidence', [])
        require(len(evidence) == len(output['identities']))
        by_citation = {}
        for item, identity in zip(evidence, output['identities']):
            require(identity is not None and identity['conversationId'] == api.conversation)
            row = api.rows.get(identity['messageId'])
            require(row is not None and item['sender'] == api.sender(row) and item['text'] == row['content'])
            require(re.fullmatch(r'E[1-9]\d*',item['citationId']) is not None)
            require(item['citationId'] not in by_citation)
            by_citation[item['citationId']] = row
        citations = list(dict.fromkeys(re.findall(r'\[(E[1-9]\d*)\]',value['answer'])))
        require(set(citations) <= by_citation.keys())
        points = [dict(text=by_citation[c]['content'],userId=by_citation[c]['author_id'],
            sourceMessageIds=[by_citation[c]['id']],contextMessageIds=[],citationId=c) for c in citations]
    except (ValueError,KeyError,TypeError):
        raise GenerationError('问答内容未通过身份及出处校验，请重新提问。') from None
    answer = value['answer'] if points else NO_EVIDENCE
    state = output['memory']
    if not points and state.get('turns'): state['turns'][-1]['answer'] = NO_EVIDENCE
    contexts = list(api.backgrounds.values())
    coverage = app.coverage(scope['channel'],start,end,contexts)
    coverage.update(retrieval_complete=not api.limited,budget_limited=api.limited)
    if api.limited:
        coverage['status']='partial'
        coverage['note']+=' 本轮按原引擎证据上限及输入预算选取，不能当作全部讨论。'
    return dict(status='answered' if points else 'insufficient_evidence',answer=answer,
        answerSourceMessageIds=[p['sourceMessageIds'][0] for p in points],points=points,
        messages=list(api.rows.values()),contexts=contexts,coverage=coverage,
        metrics=dict(scope_messages=api.total,scanned_messages=api.total,retrieved_messages=len(api.rows),
            matched_messages=max(api.matched,len(api.rows)),memory_turns=output['before']['recentTurnCount'],
            memory_limit=output['before']['maxRecentTurns'],memory_shortened=True,
            memory_context=output['before'],memory_summary=output['after'],input_bytes=output['input_bytes'],input_limit_bytes=budget,
            model_calls=output['model_calls'],tool_calls=len(api.tool_calls)),
        generation=dict(engine=app.model.engine,promptVersion=QA_VERSION,status='complete',
            runtime='copied-starmemo-query-agent',model_calls=output['model_calls'],tool_calls=len(api.tool_calls)),
        query_stats=value.get('stats',{}),
        _memory=state)
