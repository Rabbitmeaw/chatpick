"""持久本地关注；确定性匹配、增量去重和讨论串合并，无模型或消息投递。"""

import hashlib
import json
import uuid
from datetime import datetime,timezone

from .activity import user_message


def utcnow():return datetime.now(timezone.utc).isoformat()


def scope(app,server,channel):
    if not isinstance(server,str) or not isinstance(channel,str) or channel not in app.server_channels(server):
        raise ValueError('请先选择一个服务器和其中的一个频道。')


def eligible(row,server,channel):
    return (row is not None and row.get('server')==server and row.get('channel')==channel
        and user_message(row) and not row.get('deleted') and not row.get('unavailable') and bool(row['content'].strip()))


def matches(rule,row):
    return (not rule['keyword'] or rule['keyword'].casefold() in row['content'].casefold()) and (not rule['member'] or row['author_id']==rule['member'])


def version(row):
    return hashlib.sha256(json.dumps([row['author_id'],row['content']],ensure_ascii=False).encode()).hexdigest()


def rules(app,server,channel):
    scope(app,server,channel)
    with app.db() as db:
        return [json.loads(row['body']) for row in db.execute('SELECT body FROM alert_rules WHERE server=? AND channel=? ORDER BY rowid',(server,channel))]


def save_rule(app,data):
    if not isinstance(data,dict) or not {'server','channel'}<=data.keys() or not data.keys()<={'server','channel','id','keyword','member','enabled'}:
        raise ValueError('关注规则字段有误，请使用当前频道的表单。')
    server,channel=data['server'],data['channel'];scope(app,server,channel)
    identifier=data.get('id','')
    if not isinstance(identifier,str):raise ValueError('请选择有效的关注规则。')
    with app.db() as db:
        db.execute('BEGIN IMMEDIATE')
        stored=db.execute('SELECT body FROM alert_rules WHERE id=? AND server=? AND channel=?',(identifier,server,channel)).fetchone() if identifier else None
        if identifier and not stored:raise ValueError('规则不属于当前频道，或已不存在。')
        old=json.loads(stored['body']) if stored else None
        keyword=data.get('keyword',old['keyword'] if old else '')
        member=data.get('member',old['member'] if old else '')
        enabled=data.get('enabled',old['enabled'] if old else True)
        if not isinstance(keyword,str) or len(keyword.strip())>50 or not isinstance(member,str) or not isinstance(enabled,bool):
            raise ValueError('关键词最多50字，成员从当前频道选择。')
        keyword=keyword.strip()
        if not keyword and not member:raise ValueError('填写一个关键词，或选择一位要关注的成员。')
        if not old:
            for duplicate in db.execute('SELECT body FROM alert_rules WHERE server=? AND channel=?',(server,channel)):
                candidate=json.loads(duplicate['body'])
                if (candidate['keyword'].casefold(),candidate['member'])==(keyword.casefold(),member):
                    old=candidate;identifier=old['id'];break
        if member and not (old and member==old['member']):
            found=db.execute("SELECT 1 FROM messages WHERE channel=? AND json_extract(body,'$.server')=? AND json_extract(body,'$.author_id')=? LIMIT 1",(channel,server,member)).fetchone()
            if not found:raise ValueError('请选择当前频道已同步的成员。')
        changed=not old or (keyword.casefold(),member,enabled)!=(old['keyword'].casefold(),old['member'],old['enabled'])
        if not changed:return dict(offline=False,rule=old,baseline=0)
        latest=db.execute('SELECT coalesce(max(seq),0) FROM message_updates WHERE channel=?',(channel,)).fetchone()[0]
        rule=dict(id=identifier or uuid.uuid4().hex,server=server,channel=channel,keyword=keyword,member=member,
            enabled=enabled,version=old['version']+1 if old else 1,since=utcnow(),last_seq=latest)
        rows=[json.loads(row['body']) for row in db.execute('SELECT body FROM messages WHERE channel=?',(channel,))]
        rows=[row for row in rows if eligible(row,server,channel)]
        db.executemany('INSERT OR IGNORE INTO alert_seen VALUES (?,?,?,?)',[(rule['id'],rule['version'],row['id'],version(row)) for row in rows])
        db.execute('INSERT OR REPLACE INTO alert_rules VALUES (?,?,?,?)',(rule['id'],server,channel,json.dumps(rule,ensure_ascii=False)))
    return dict(offline=False,rule=rule,baseline=len(rows))


def discussion(app,row):
    actual=row.get('source_channel',row['channel'])
    if actual!=row['channel']:return 'thread:'+actual,{row['id']}
    ids={row['id']};current=row
    for _ in range(6):
        target=current.get('reference_id')
        if not target or target in ids or current.get('reference_channel',actual)!=actual:break
        ids.add(target)
        parent=app.message(target)
        if not eligible(parent,row['server'],row['channel']) or parent.get('source_channel',actual)!=actual:break
        current=parent
    return 'reply:'+min(ids,key=lambda identifier:(len(identifier),identifier)),ids


def check(app,server,channel):
    scope(app,server,channel)
    result=dict(created=0,updated=0,processed=0,offline=False)
    with app.db() as db:
        db.execute('BEGIN IMMEDIATE')
        latest=db.execute('SELECT coalesce(max(seq),0) FROM message_updates WHERE channel=?',(channel,)).fetchone()[0]
        active=[json.loads(row['body']) for row in db.execute('SELECT body FROM alert_rules WHERE server=? AND channel=?',(server,channel))]
        for rule in active:
            if not rule['enabled'] or rule['last_seq']>=latest:continue
            events={row['id']:json.loads(row['body']) for row in db.execute('SELECT id,body FROM local_alert_events WHERE rule_id=?',(rule['id'],))}
            ids=[row['message_id'] for row in db.execute('SELECT DISTINCT message_id FROM message_updates WHERE channel=? AND seq>? AND seq<=?',(channel,rule['last_seq'],latest))]
            for identifier in ids:
                stored=db.execute('SELECT body FROM messages WHERE id=?',(identifier,)).fetchone()
                row=json.loads(stored['body']) if stored else None
                if not eligible(row,server,channel):continue
                observed=version(row)
                known=db.execute('SELECT 1 FROM alert_seen WHERE rule_id=? AND rule_version=? AND message_id=? LIMIT 1',(rule['id'],rule['version'],identifier)).fetchone()
                fresh=db.execute('INSERT OR IGNORE INTO alert_seen VALUES (?,?,?,?)',(rule['id'],rule['version'],identifier,observed)).rowcount
                result['processed']+=1
                if not fresh or (not known and row['created']<rule['since']) or not matches(rule,row):continue
                group,related=discussion(app,row)
                candidates=[(key,item) for key,item in events.items() if item['rule_version']==rule['version'] and not item.get('merged_into')
                    and (item['group']==group or related&set(item['discussion_ids']))]
                stamp=utcnow()
                if candidates:
                    candidates.sort(key=lambda item:(item[1]['created_at'],item[0]))
                    key,event=candidates[0]
                    for other_id,other in candidates[1:]:
                        event['sourceMessageIds']=list(dict.fromkeys(event['sourceMessageIds']+other['sourceMessageIds']))
                        event['discussion_ids']=list(set(event['discussion_ids'])|set(other['discussion_ids']))
                        other['merged_into']=key
                        db.execute('UPDATE local_alert_events SET body=? WHERE id=?',(json.dumps(other,ensure_ascii=False),other_id))
                    result['updated']+=1
                    event['updates']=event.get('updates',0)+1
                else:
                    key=uuid.uuid4().hex
                    event=dict(id=key,server=server,channel=channel,rule_id=rule['id'],rule_version=rule['version'],
                        keyword=rule['keyword'],member=rule['member'],created_at=stamp,sourceMessageIds=[],discussion_ids=[],group=group,updates=0)
                    result['created']+=1
                event['updated_at']=stamp
                event['sourceMessageIds']=list(dict.fromkeys(event['sourceMessageIds']+[identifier]))
                event['discussion_ids']=sorted(set(event['discussion_ids'])|related)
                events[key]=event
                db.execute('INSERT OR REPLACE INTO local_alert_events VALUES (?,?,?,?,?)',(key,server,channel,rule['id'],json.dumps(event,ensure_ascii=False)))
            rule['last_seq']=latest
            db.execute('UPDATE alert_rules SET body=? WHERE id=?',(json.dumps(rule,ensure_ascii=False),rule['id']))
    return result


def events(app,server,channel):
    scope(app,server,channel)
    with app.db() as db:
        stored=[json.loads(row['body']) for row in db.execute("SELECT body FROM local_alert_events WHERE server=? AND channel=? "
            "AND json_extract(body,'$.merged_into') IS NULL ORDER BY json_extract(body,'$.updated_at') DESC,rowid DESC LIMIT 50",(server,channel))]
        for row in db.execute("SELECT * FROM alerts WHERE json_extract(body,'$.server')=? AND json_extract(body,'$.channel')=? ORDER BY rowid DESC LIMIT 50",(server,channel)):
            stored.append(dict(id=row['fingerprint'],server=server,channel=channel,keyword=row['keyword'],member='',legacy=True,
                created_at='',updated_at='',sourceMessageIds=[row['message_id']],updates=0))
        result=[]
        for event in stored:
            if event.get('merged_into'):continue
            messages=[]
            for identifier in event['sourceMessageIds']:
                current=db.execute('SELECT body FROM messages WHERE id=?',(identifier,)).fetchone()
                row=json.loads(current['body']) if current else None
                if eligible(row,server,channel) and matches(event,row):messages.append(row)
            result.append(dict(**event,messages=sorted(messages,key=lambda row:(row['created'],row['id'])),
                status='active' if messages else 'source_unavailable',evidence_count=len(event['sourceMessageIds'])))
        return result
