"""离线原文摘录与事实骨架，供本机模拟模型及结果回填使用。

字段依据与当前工作区快照见 docs/starmemo-reference.md。
本模块为 GroupPulse 独立实现，不导入 Electron 或模型调用链。
"""

from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
LIMITS = {'topics': 3, 'importantMessages': 3, 'todos': 4, 'unresolved': 3}


def clock(created):
    return datetime.fromisoformat(created).astimezone(TZ).strftime('%H:%M')


def build_digest(messages, titles, annotations, mode='compact', weekly=False):
    if mode not in ('compact', 'full'):
        raise ValueError('请选择精简或完整阅读方式。')
    # 即使调用方传入重复行，统计和出处也只使用唯一的用户消息。
    rows = sorted({m['id']: m for m in messages if not m['bot']}.values(),
                  key=lambda m: (m['created'], m['id']))
    grouped = {}
    for message in rows:
        grouped.setdefault(message['topic'], []).append(message)
    topics = []
    for topic_id, evidence in grouped.items():
        people = {}
        for m in evidence:
            people[m['author_id']] = m['name']
        decisions = [m for m in evidence if m['stance'] == '决定']
        summary_source = decisions[-1] if decisions else evidence[-1]
        time_range = f'{clock(evidence[0]["created"])}–{clock(evidence[-1]["created"])}'
        if weekly:
            time_range = ' → '.join(datetime.fromisoformat(m['created']).astimezone(TZ).strftime('%m-%d %H:%M')
                                     for m in (evidence[0], evidence[-1]))
        conclusions = [dict(text=m['content'], sender=m['name'], sourceMessageIds=[m['id']])
                       for m in decisions]
        topics.append(dict(
            id=topic_id, title=titles.get(topic_id, '未命名话题'), timeRange=time_range,
            heat='高' if len(evidence) >= 5 else ('中' if len(evidence) >= 2 else '低'),
            participants=list(people.values()), participantIds=list(people),
            messageCount=len(evidence), summary=f'{summary_source["name"]}：{summary_source["content"]}',
            summarySourceMessageIds=[summary_source['id']], conclusions=conclusions,
            keywords=[titles.get(topic_id, '未命名话题')], sourceMessageIds=[m['id'] for m in evidence],
            viewpoints=[dict(sender=m['name'], userId=m['author_id'], stance=m['stance'],
                             text=m['content'], sourceMessageIds=[m['id']]) for m in evidence],
            messages=evidence))
    topics.sort(key=lambda t: (-bool(t['conclusions']), -t['messageCount'], t['id']))
    important = [dict(sender=m['name'], time=clock(m['created']), content=m['content'],
                      note='原文标注的通知', sourceMessageIds=[m['id']])
                 for m in rows if m['stance'] == '通知']
    todos = []
    for m in rows:
        # 演示待办由合成样例明确标注，并在每次生成时重新核验原文。
        text = annotations.get(m['id'], {}).get('todoText', '')
        if text and text in m['content']:
            deadline = annotations.get(m['id'], {}).get('deadlineText', '')
            todos.append(dict(task=text, owner=None,
                              deadline=deadline if deadline and deadline in text else None,
                              topic=titles.get(m['topic']),
                              note='合成样例中标注的原文任务；未知负责人为空，期限仅显示原文措辞。',
                              sourceMessageIds=[m['id']]))
    unresolved = [dict(question=m['content'], owner=None, asker=m['name'], status='待跟进',
                       note='原文明示提问或待确认；离线版未判断后续是否已解决。',
                       lastDiscussedAt=clock(m['created']), sourceMessageIds=[m['id']])
                  for m in rows if m['stance'] in ('待确认', '提问')]
    all_decisions = [m for m in rows if m['stance'] == '决定']
    period = '本期' if weekly else '本次'
    overview = (f'{period}整理了 {len(rows)} 条用户消息，'
                f'{len({m["author_id"] for m in rows})} 位成员参与，涉及 {len(topics)} 个样例话题。')
    status_line = f'{len(all_decisions)} 条明确决定 · {len(todos)} 条待办线索 · {len(unresolved)} 条待跟进线索'
    sections = dict(topics=topics, importantMessages=important, todos=todos, unresolved=unresolved)
    metadata = {}
    shown = {}
    for name, items in sections.items():
        displayed = items[:LIMITS[name]] if mode == 'compact' else items
        metadata[name] = dict(enabled=bool(items), totalCount=len(items), displayedCount=len(displayed),
                              hiddenCount=len(items) - len(displayed))
        shown[name] = displayed
    return dict(
        mode=mode, overview=overview,
        hero=dict(headline=topics[0]['title'] if topics else '当前范围暂无消息', summary=overview,
                  keyTakeaway=all_decisions[-1]['content'] if all_decisions else None,
                  keyTakeawaySourceMessageIds=[all_decisions[-1]['id']] if all_decisions else [],
                  pendingNote=unresolved[0]['question'] if unresolved else None, statusLine=status_line),
        **shown, keywords=[t['title'] for t in topics], sectionMeta=metadata,
        summaryStats=dict(messageCount=len(rows), activeUsers=len({m['author_id'] for m in rows}),
                          topicCount=len(topics), conclusionCount=len(all_decisions),
                          todoCount=len(todos), unresolvedCount=len(unresolved)))
