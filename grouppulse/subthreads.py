"""当前文字频道直属子线程的只读分页；保留真实来源 ID 和独立游标。"""

import json
from datetime import datetime

from .discord import identifier, normalize, snowflake
from .network import ServiceError


def unavailable(app, channel, thread):
    with app.db() as db:
        rows=[json.loads(r['body']) for r in db.execute(
            "SELECT body FROM messages WHERE channel=? AND json_extract(body,'$.source_channel')=?",(channel,thread))]
    app.upsert_messages([dict(row,unavailable=True) for row in rows])


def sync_subthreads(app, client, state, start, until, budget, previous, incremental, save, cancel):
    parent,guild=state['channel'],state['server']
    threads=state.setdefault('threads',{})
    scan=state.setdefault('thread_scan',dict(active=False,public=dict(done=False,before=''),private=dict(done=False,before='')))
    references=[]

    def check_cancel():
        if cancel and cancel(): raise ServiceError('已切换频道或暂停，停止上一个范围的同步。')

    def register(row, active=False):
        if not isinstance(row,dict): raise ServiceError('Discord子线程列表格式异常。')
        if active and row.get('parent_id')!=parent: return
        if row.get('parent_id')!=parent or row.get('guild_id',guild)!=guild or row.get('type') not in (10,11,12):
            raise ServiceError('子线程列表包含范围不匹配的记录，已停止读取。')
        thread=identifier(row.get('id'))
        if thread==parent: raise ServiceError('子线程归属异常，已停止读取。')
        if thread not in threads:
            old=(previous or {}).get('threads',{}).get(thread,{})
            after=old.get('latest_id','') if incremental and old.get('status')=='complete' else ''
            threads[thread]=dict(id=thread,name=row.get('name','未命名线程'),cursor=str(snowflake(until)),
                status='pending',after_id=after,latest_id=after,pages=0,messages=0,
                observed_last_id=row.get('last_message_id'))
            last=row.get('last_message_id')
            if last is not None:
                identifier(last)
                if (int(last)<snowflake(start) or (incremental and old.get('status')=='complete'
                        and old.get('observed_last_id')==last)):
                    threads[thread]['status']='complete'

    if not scan['active']:
        check_cancel()
        try:
            value=client.active_threads(guild)
            if not isinstance(value,dict) or not isinstance(value.get('threads'),list):
                raise ServiceError('Discord活动子线程列表格式异常。')
            for row in value['threads']: register(row,True)
        except ServiceError as error:
            if error.status not in (403,404): raise
            scan['active_error']='活动子线程列表不可读。'
        scan['active']=True;save()

    # 列表分页与消息分页分别记账；每次至多十页列表，后续沿保存游标继续。
    discovery_pages=0
    for kind in ('public','private'):
        cursor=scan[kind]
        while not cursor['done']:
            if discovery_pages>=10: return False,references
            check_cancel();discovery_pages+=1
            try:
                value=client.archived_threads(parent,kind=='private',cursor['before'])
            except ServiceError as error:
                if error.status not in (403,404): raise
                cursor.update(done=True,error='归档子线程列表不可读。');save();break
            if not isinstance(value,dict) or not isinstance(value.get('threads'),list) or not isinstance(value.get('has_more'),bool):
                raise ServiceError('Discord归档子线程列表格式异常。')
            page=value['threads']
            for row in page: register(row)
            if not value['has_more']:
                cursor['done']=True
            else:
                if not page: raise ServiceError('子线程列表分页未前进，已停止读取。')
                if kind=='private':
                    before=str(min(int(identifier(row.get('id'))) for row in page))
                    advanced=not cursor['before'] or int(before)<int(cursor['before'])
                else:
                    stamps=[row.get('thread_metadata',{}).get('archive_timestamp','') for row in page]
                    if not all(stamps): raise ServiceError('子线程归档时间缺失，未把列表标为完整。')
                    before=min(stamps,key=lambda stamp:datetime.fromisoformat(stamp.replace('Z','+00:00')))
                    stamp=datetime.fromisoformat(before.replace('Z','+00:00'))
                    advanced=not cursor['before'] or stamp<datetime.fromisoformat(cursor['before'].replace('Z','+00:00'))
                    if stamp<start: cursor['done']=True
                if not advanced: raise ServiceError('子线程列表分页未前进，已停止读取。')
                cursor['before']=before
            save()

    # 未在当前可读清单出现的旧线程，不用旧缓存冒充当前可读证据。
    with app.db() as db:
        cached=db.execute("SELECT DISTINCT json_extract(body,'$.source_channel') AS id FROM messages "
            "WHERE channel=? AND created>=? AND created<? AND json_extract(body,'$.source_channel') IS NOT NULL",
            (parent,start.isoformat(),until.isoformat())).fetchall()
    for row in cached:
        if row['id'] not in threads:
            threads[row['id']]=dict(id=row['id'],status='unavailable',reason='未在当前 Bot 可读子线程列表中发现。')
            unavailable(app,parent,row['id'])

    for thread,progress in threads.items():
        if progress['status'] in ('complete','unavailable'): continue
        if budget<=0: return False,references
        check_cancel()
        started_here=progress['pages']==0
        fetched=set()
        try:
            verified=client.check_thread(guild,parent,thread)
            progress['name']=verified.get('name','未命名线程')
            while budget>0:
                check_cancel()
                page=client.history(thread,progress['cursor'])
                budget-=1;progress['pages']+=1;state['pages']+=1
                if not page:
                    progress['status']='complete';break
                rows=[normalize(row,guild,parent,source_channel=thread,thread_name=progress['name']) for row in page]
                cursor=min(int(row['id']) for row in rows)
                if cursor>=int(progress['cursor']): raise ServiceError('子线程消息分页未前进，已停止读取。')
                selected=[row for row in rows if start<=datetime.fromisoformat(row['created'])<until]
                # Discord 的线程起始占位消息可能复用父频道原帖 ID，保留原帖，不重复统计。
                selected=[row for row in selected if not (row['id']==thread and app.message(thread)
                          and app.message(thread).get('source_channel',parent)==parent)]
                app.upsert_messages(selected)
                with app.db() as db:
                    db.executemany('DELETE FROM context_failures WHERE channel=? AND message_id=?',[(parent,row['id']) for row in selected])
                fetched.update(row['id'] for row in selected);references.extend(selected)
                progress['messages']+=len(selected);state['messages']+=len(selected)
                if selected: progress['latest_id']=str(max(int(progress.get('latest_id') or 0),*(int(row['id']) for row in selected)))
                progress['cursor']=str(cursor)
                if ((progress['after_id'] and cursor<=int(progress['after_id']))
                        or min(datetime.fromisoformat(row['created']) for row in rows)<start):
                    progress['status']='complete'
                save()
                if progress['status']=='complete': break
            if progress['status']=='complete' and started_here and not incremental:
                cached=app.read_window(parent,start,until)
                app.upsert_messages([dict(row,deleted=True) for row in cached
                    if row.get('source_channel')==thread and row['id'] not in fetched])
        except ServiceError as error:
            if error.status not in (403,404): raise
            progress.update(status='unavailable',reason='子线程当前不可读、已删除或 Bot 已失去访问权限。')
            unavailable(app,parent,thread)
        save()
        if progress['status']=='pending': return False,references
    return True,references
