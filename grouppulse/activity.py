"""同范围等长统计；话题由原报告及原逐条分类处理，统计只用原始消息 ID。"""

import hashlib
import json
import uuid
from collections import Counter,defaultdict
from datetime import datetime,timedelta,timezone

from .core import TZ
from .generation import require
from .live_generation import generate_report
from .reference_classifier import classify,cache_key
from .reports import build_digest


def window_info(start,end,zone=TZ,zone_name='Asia/Shanghai'):
    first=start.astimezone(zone).date()
    last=(end-timedelta(microseconds=1)).astimezone(zone).date()
    return dict(start=start.isoformat(),end=end.isoformat(),start_date=first.isoformat(),end_date=last.isoformat(),
        days=(last-first).days+1,label=f'{first} → {last}',timezone=zone_name)


def user_message(row):
    return not row.get('bot') and row.get('message_type',0) in (0,19)


def counts(rows,zone=TZ):
    users=[row for row in rows if user_message(row)]
    hours=Counter(datetime.fromisoformat(row['created']).astimezone(zone).hour for row in users)
    return dict(messages=len(users),members=len({row['author_id'] for row in users}),
        replies=sum(bool(row.get('reference_id')) for row in users),bots=sum(bool(row['bot']) for row in rows),
        system=sum(not row['bot'] and not user_message(row) for row in rows),
        hours=[dict(hour=hour,count=hours[hour]) for hour in range(24)])


def scoped_contexts(app,rows,server,channel):
    contexts=app.contexts(rows,channel)
    for context in contexts:
        if context['status']=='available' and (context['message'].get('server'),context['message'].get('channel'))!=(server,channel):
            context.pop('message');context.update(status='missing',reason='引用不属于当前服务器和频道，未读取。')
    return contexts


def activity_report(app,server,channel,date,end_date=''):
    if not isinstance(server,str) or not isinstance(channel,str) or channel not in app.server_channels(server):
        raise ValueError('请先选择一个服务器和其中的频道。')
    start,end=app.window(date,end_date=end_date)
    zone,zone_name=getattr(app,'report_timezone',TZ),getattr(app,'report_timezone_name','Asia/Shanghai')
    try: previous_start=start-(end-start)
    except OverflowError: raise ValueError('上一等长周期超出可支持的日期，请调整开始日期。') from None
    window,previous_window=window_info(start,end,zone,zone_name),window_info(previous_start,start,zone,zone_name)
    app.ensure_synced(server,channel,previous_window['start_date'],window['end_date'])
    all_rows={row['id']:row for row in app.read_window(channel,previous_start,end) if row.get('server')==server}
    current=[row for row in all_rows.values() if datetime.fromisoformat(row['created'])>=start]
    previous=[row for row in all_rows.values() if datetime.fromisoformat(row['created'])<start]
    users=[row for row in all_rows.values() if user_message(row)]
    contexts=scoped_contexts(app,users,server,channel)
    current_contexts=scoped_contexts(app,[row for row in current if user_message(row)],server,channel)
    previous_contexts=scoped_contexts(app,[row for row in previous if user_message(row)],server,channel)
    coverage=app.coverage(channel,start,end,current_contexts)
    prior_coverage=app.coverage(channel,previous_start,start,previous_contexts)
    metrics=counts(current,zone);prior_metrics=counts(previous,zone)
    metrics.update(previous=prior_metrics['messages'],comparable=coverage['window_complete'] and prior_coverage['window_complete'],topics=0)
    daily=defaultdict(list)
    for row in users: daily[datetime.fromisoformat(row['created']).astimezone(zone).date().isoformat()].append(row)
    key=hashlib.sha256(json.dumps([server,channel,window,[(row['id'],row.get('version')) for row in users],
        [(row['id'],row['status'],row.get('message',{}).get('version')) for row in contexts],
        coverage['status'],prior_coverage['status'],metrics['comparable']],sort_keys=True).encode()).hexdigest()
    with app.db() as db: cached=db.execute('SELECT body FROM activity_topics WHERE id=?',(key,)).fetchone()
    analysis=dict(json.loads(cached['body']),cached=True) if cached else dict(status='not_generated',cached=False)
    metrics['topics']=len(analysis.get('topics',[]))
    digest=build_digest([],{}, {},'full')
    return dict(offline=False,readonly=True,kind='activity',period='daily' if window['days']==1 else 'weekly',date=date,
        server=server,channel=channel,window=window,metrics=metrics,coverage=coverage,
        comparison=dict(window=previous_window,metrics=prior_metrics,coverage=prior_coverage),
        daily=[dict(date=day,period='current' if day>=window['start_date'] else 'previous',**counts(rows,zone)) for day,rows in sorted(daily.items())],
        messages=[row for row in current if user_message(row)],contexts=current_contexts,groups=[],dimensions={},
        digest=digest,fullDigest=digest,trends=analysis.get('topics',[]),topic_analysis=analysis,
        generation=dict(engine='none',status='not_requested'),_key=key,_rows=users,_contexts=contexts)


def public_report(value):
    return {key:item for key,item in value.items() if not key.startswith('_')}


def change_state(current,previous,comparable):
    if not comparable: return '待核对'
    if previous==0: return '本期出现' if current else '两期未见'
    if current==0: return '本期未见'
    if current-previous>=5 and current>=previous*1.25:return '升温'
    if previous-current>=5 and current<=previous*.75:return '降温'
    return '持续讨论'


def topic_trends(app,server,channel,date,end_date='',refresh=False):
    if not isinstance(refresh,bool):raise ValueError('话题分析参数有误。')
    value=activity_report(app,server,channel,date,end_date)
    if value['topic_analysis']['status']!='not_generated' and not refresh:return public_report(value)
    if not app.generation_lock.acquire(blocking=False):raise ValueError('已有报告或问答正在生成，请等待完成。')
    try:
        rows=[row for row in value['_rows'] if row.get('text_available',bool(row['content'].strip())) and row['content'].strip()]
        by_id={row['id']:row for row in rows}
        before_calls=app.model.calls
        combined=window_info(datetime.fromisoformat(value['comparison']['window']['start']),datetime.fromisoformat(value['window']['end']),
            getattr(app,'report_timezone',TZ),getattr(app,'report_timezone_name','Asia/Shanghai'))
        scope=dict(server=server,channel=channel,server_name=app.demo['servers'][server]['name'],
            channel_name=app.server_channels(server)[channel],**combined)
        generated=generate_report(app.model,scope,dict(kind='weekly',member='',topic='本频道全部讨论'),rows,value['_contexts'],'full')
        topics=generated['fullDigest']['topics']
        seeds=defaultdict(list)
        for i,topic in enumerate(topics):
            for identifier in topic['sourceMessageIds']: seeds[identifier].append(i)
        assigned={};unknown=[]
        choices=[dict(title=topic['title'],summary=json.dumps(dict(summary=topic['summary'],
            start=min(by_id[identifier]['created'] for identifier in topic['sourceMessageIds']),
            end=max(by_id[identifier]['created'] for identifier in topic['sourceMessageIds']),
            sourceMessageIds=topic['sourceMessageIds']),ensure_ascii=False)) for topic in topics]
        report_calls=app.model.calls-before_calls
        for row in rows:
            if len(seeds[row['id']])==1:
                assigned[row['id']]=seeds[row['id']][0];continue
            if not topics: assigned[row['id']]=len(topics);continue
            context=scoped_contexts(app,[row],server,channel)
            key=cache_key(server,channel,choices,row,context)
            with app.db() as db:cached=db.execute('SELECT body FROM topic_classifications WHERE id=?',(key,)).fetchone()
            classification=json.loads(cached['body']) if cached else classify(app.model,choices,row,context)
            if not classification['valid']:
                unknown.append(row['id']);continue
            assigned[row['id']]=classification['index']-1
            if not cached:
                with app.db() as db:db.execute('INSERT OR REPLACE INTO topic_classifications VALUES (?,?)',(key,json.dumps(classification)))
        unknown.extend(row['id'] for row in value['_rows'] if row['id'] not in by_id)
        complete=(value['metrics']['comparable'] and value['coverage']['status']=='complete'
            and value['comparison']['coverage']['status']=='complete' and not unknown
            and generated['generation']['batches']<=1 and not generated['generation']['omitted_contexts'])
        with app.db() as db:
            identities=[dict(id=row['id'],**json.loads(row['body'])) for row in db.execute(
                'SELECT id,body FROM activity_topic_ids WHERE server=? AND channel=?',(server,channel))]
        groups=[[identifier for identifier,index in assigned.items() if index==i] for i in range(len(topics))]
        candidates=[{old['id'] for old in identities if set(old['sources'])&set(group)} for group in groups]
        uses=Counter(identifier for group in candidates for identifier in group)
        results=[];identity_rows=[]
        boundary=value['window']['start']
        for i,topic in enumerate(topics):
            source_ids=groups[i]
            if not source_ids:continue
            ambiguous=len(candidates[i])>1 or any(uses[identifier]>1 for identifier in candidates[i])
            topic_id=next(iter(candidates[i])) if len(candidates[i])==1 and not ambiguous else uuid.uuid4().hex
            current=[by_id[identifier] for identifier in source_ids if by_id[identifier]['created']>=boundary]
            previous=[by_id[identifier] for identifier in source_ids if by_id[identifier]['created']<boundary]
            results.append(dict(id=topic_id,title=topic['title'],current=len(current),previous=len(previous),
                members=len({row['author_id'] for row in current}),previous_members=len({row['author_id'] for row in previous}),
                share=len(current)/max(1,value['metrics']['messages']),previous_share=len(previous)/max(1,value['comparison']['metrics']['messages']),
                state=change_state(len(current),len(previous),complete and not ambiguous),
                identity_status='ambiguous' if ambiguous else 'matched' if candidates[i] else 'new',sourceMessageIds=source_ids))
            old=next((item for item in identities if item['id']==topic_id),None)
            identity_rows.append((topic_id,server,channel,json.dumps(dict(sources=sorted(set(source_ids)|set(old['sources'] if old else []))),ensure_ascii=False)))
        analysis=dict(status='complete' if complete and all(row['identity_status']!='ambiguous' for row in results) else 'partial',cached=False,
            topics=sorted(results,key=lambda row:(-row['current'],-row['members'],row['id'])),
            classified_messages=len(assigned),unclassified_messages=len(unknown),other_messages=sum(index==len(topics) for index in assigned.values()),
            messages=rows,contexts=value['_contexts'],generated_at=datetime.now(timezone.utc).isoformat(),
            note='两期一起整理并逐条归类；数量来自去重原消息。覆盖不足、无法归类、跨批或拆分合并歧义时不作确定升降判断。',
            generation=dict(report_runtime='copied-starmemo',classifier_runtime='copied-chat-digest',
                calls=app.model.calls-before_calls,report_calls=report_calls,classification_calls=app.model.calls-before_calls-report_calls))
        with app.db() as db:
            db.execute('BEGIN IMMEDIATE')
            observed=value['_rows']+[row['message'] for row in value['_contexts'] if row['status']=='available']
            for row in observed:
                latest=db.execute('SELECT body FROM messages WHERE id=?',(row['id'],)).fetchone()
                latest=json.loads(latest['body']) if latest else None
                if latest is None or latest.get('deleted') or latest.get('unavailable') or latest.get('version')!=row.get('version'):
                    raise ValueError('分析依据已编辑、删除或不可读，本次未保存，请重新分析。')
            current_ids={row['id'] for row in db.execute("SELECT id FROM messages WHERE channel=? AND created>=? AND created<? "
                "AND json_extract(body,'$.server')=? AND json_extract(body,'$.bot')=0 "
                "AND coalesce(json_extract(body,'$.message_type'),0) IN (0,19) "
                "AND coalesce(json_extract(body,'$.deleted'),0)=0 AND coalesce(json_extract(body,'$.unavailable'),0)=0",
                (channel,combined['start'],combined['end'],server))}
            if current_ids!={row['id'] for row in value['_rows']}:
                analysis['status']='partial';analysis['note']+=' 分析期间有新增记录，本结果基于本轮已取证原文；重新查看时将重新判断缓存。'
                for topic in analysis['topics']:topic['state']='待核对'
            db.executemany('INSERT OR REPLACE INTO activity_topic_ids VALUES (?,?,?,?)',identity_rows)
            db.execute('INSERT OR REPLACE INTO activity_topics VALUES (?,?)',(value['_key'],json.dumps(analysis,ensure_ascii=False)))
        value['metrics']['topics']=len(analysis['topics'])
        value.update(topic_analysis=analysis,trends=analysis['topics']);return public_report(value)
    finally:app.generation_lock.release()
