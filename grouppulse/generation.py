"""纯本地的模拟模型边界：提示词 → JSON → 校验 → 完整快照，无网络实现。"""

import copy
import json

from .prompts import OUTPUT_CONTRACT, PROMPT_VERSION, SYSTEM_PROMPT, TASK_PROMPT, build_prompt
from .reports import LIMITS, build_digest, clock


class GenerationError(ValueError):
    """仅返回固定文案，不向页面暴露模型正文或异常原值。"""


class InvalidOutput(ValueError):
    pass


def require(condition):
    if not condition:
        raise InvalidOutput()


def check_shape(value, shape):
    if isinstance(shape, dict):
        require(isinstance(value, dict) and value.keys() == shape.keys())
        for key in shape:
            check_shape(value[key], shape[key])
    elif isinstance(shape, list):
        require(isinstance(value, list))
        for item in value:
            check_shape(item, shape[0])
    else:
        require((value is None and shape == 'text|null') or (isinstance(value, str) and len(value) <= 100000))
        if isinstance(value, str):
            value.encode('utf-8')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def reject_constant(_value):
    raise InvalidOutput()


def parse_model_output(raw, echoed_input=None):
    decoder = json.JSONDecoder(object_pairs_hook=unique_object, parse_constant=reject_constant)
    text = raw.strip()
    value, end = decoder.raw_decode(text)
    tail = text[end:].strip()
    if tail:
        # 仅兼容服务完整回显本次输入后再返回答案；不寻找、拼接或修补任意 JSON。
        require(echoed_input is not None and value == echoed_input)
        value, end = decoder.raw_decode(tail)
        require(not tail[end:].strip())
    return value


def facts(messages, coverage):
    return dict(messageCount=len(messages), activeUsers=len({m['author_id'] for m in messages}),
                topicCount=len({m['topic'] for m in messages}), coverage=coverage)


def mock_digest(messages, titles, annotations, weekly):
    # 摘录算法在模拟模型内部运行，只接收提示词内的数据，没有 App/数据库旁路。
    local = build_digest(messages, titles, annotations, mode='full', weekly=weekly)
    by_id = {m['id']: m for m in messages}
    def quote(item, **fields):
        return dict(**fields, sourceMessageIds=item['sourceMessageIds'], contextMessageIds=[])
    topics = []
    for topic in local['topics']:
        topics.append(dict(id=topic['id'], title=topic['title'], summary=topic['summary'],
                           summarySourceMessageIds=topic['summarySourceMessageIds'],
                           sourceMessageIds=topic['sourceMessageIds'], contextMessageIds=[],
                           conclusions=[quote(c, userId=by_id[c['sourceMessageIds'][0]]['author_id'], text=c['text'])
                                        for c in topic['conclusions']],
                           viewpoints=[quote(v, userId=v['userId'], stance=v['stance'], text=v['text'])
                                       for v in topic['viewpoints']]))
    first = topics[0] if topics else None
    overview = first['summary'] if first else ''
    sources = first['summarySourceMessageIds'] if first else []
    return dict(overview=overview, overviewSourceMessageIds=sources,
                hero=dict(headline=first['title'] if first else '当前范围暂无消息',
                          headlineSourceMessageIds=sources, summary=overview, summarySourceMessageIds=sources,
                          keyTakeaway=local['hero']['keyTakeaway'],
                          keyTakeawaySourceMessageIds=local['hero']['keyTakeawaySourceMessageIds']),
                topics=topics,
                importantMessages=[quote(i, userId=by_id[i['sourceMessageIds'][0]]['author_id'], content=i['content'])
                                   for i in local['importantMessages']],
                todos=[quote(i, task=i['task'], ownerId=None, deadlineText=i['deadline']) for i in local['todos']],
                unresolved=[quote(i, question=i['question'], askerId=by_id[i['sourceMessageIds'][0]]['author_id'],
                                  status='待跟进') for i in local['unresolved']])


class LocalMockModel:
    def complete(self, messages):
        from .questions import QA_SYSTEM_PROMPT, mock_answer
        if messages[0] == {'role': 'system', 'content': QA_SYSTEM_PROMPT}:
            return mock_answer(messages)
        require(messages[0] == {'role': 'system', 'content': SYSTEM_PROMPT})
        require(messages[1]['role'] == 'user' and messages[1]['content'].startswith(TASK_PROMPT + '\nINPUT_JSON\n'))
        payload = json.loads(messages[1]['content'].split('\nINPUT_JSON\n', 1)[1])
        data = payload['input']
        require(payload['promptVersion'] == PROMPT_VERSION)
        require(payload['output_contract'] == {'digest': OUTPUT_CONTRACT,
                'dimensions': {key: OUTPUT_CONTRACT for key in data['dimensionMessageIds']}})
        def digest(ids):
            rows = [m for m in data['messages'] if m['id'] in ids]
            return mock_digest(rows, data['topicTitles'], data['annotations'], data['request']['period'] == 'weekly')
        return json.dumps(dict(digest=digest(data['allowedMessageIds']),
                               dimensions={key: digest(ids) for key, ids in data['dimensionMessageIds'].items()}),
                          ensure_ascii=False)


def validate_digest(value, messages, contexts, annotations):
    check_shape(value, OUTPUT_CONTRACT)
    by_id = {m['id']: m for m in messages}
    background = {c['id'] for c in contexts if c['status'] == 'available'} - by_id.keys()

    def ids(values, allowed, needed=True):
        require(len(values) == len(set(values)) and set(values) <= set(allowed))
        require(bool(values) or not needed)

    def sources(item, allowed=None):
        ids(item['sourceMessageIds'], by_id if allowed is None else allowed)
        ids(item['contextMessageIds'], background, needed=False)

    def text_sources(text, values):
        ids(values, by_id, needed=bool(text) and bool(messages))
        require(bool(text) or not values)

    def original(item, text_field, author_field=None, stances=None):
        sources(item)
        require(len(item['sourceMessageIds']) == 1)
        source = by_id[item['sourceMessageIds'][0]]
        require(item[text_field] == source['content'])
        if author_field:
            require(item[author_field] == source['author_id'])
        if stances:
            require(source['stance'] in stances)
        return source

    text_sources(value['overview'], value['overviewSourceMessageIds'])
    hero = value['hero']
    for field in ('headline', 'summary', 'keyTakeaway'):
        text_sources(hero[field], hero[field + 'SourceMessageIds'])
    if not messages:
        require(value['overview'] == '' and hero['summary'] == '' and hero['headline'] == '当前范围暂无消息')
        require(all(not hero[k] for k in ('headlineSourceMessageIds', 'summarySourceMessageIds', 'keyTakeawaySourceMessageIds')))
    if hero['keyTakeaway'] is not None:
        original(dict(text=hero['keyTakeaway'], sourceMessageIds=hero['keyTakeawaySourceMessageIds'],
                      contextMessageIds=[]), 'text', stances={'决定'})
    grouped = {}
    for message in messages:
        grouped.setdefault(message['topic'], []).append(message)
    topic_ids = [t['id'] for t in value['topics']]
    require(len(topic_ids) == len(set(topic_ids)) and set(topic_ids) == set(grouped))
    for topic in value['topics']:
        allowed = {m['id'] for m in grouped[topic['id']]}
        sources(topic, allowed)
        require(set(topic['sourceMessageIds']) == allowed and bool(topic['title'].strip()) and bool(topic['summary'].strip()))
        ids(topic['summarySourceMessageIds'], allowed)
        viewpoint_ids = []
        for item in topic['viewpoints']:
            source = original(item, 'text', 'userId')
            require(source['id'] in allowed and item['stance'] == source['stance'])
            viewpoint_ids.append(source['id'])
        require(len(viewpoint_ids) == len(allowed) and set(viewpoint_ids) == allowed)
        conclusion_ids = []
        for item in topic['conclusions']:
            source = original(item, 'text', 'userId', {'决定'})
            require(source['id'] in allowed)
            conclusion_ids.append(source['id'])
        require(len(conclusion_ids) == len(set(conclusion_ids)))
    for item in value['importantMessages']:
        original(item, 'content', 'userId', {'通知'})
    for item in value['unresolved']:
        original(item, 'question', 'askerId', {'待确认', '提问'})
        require(item['status'] == '待跟进')
    for item in value['todos']:
        sources(item)
        require(len(item['sourceMessageIds']) == 1)
        source = by_id[item['sourceMessageIds'][0]]
        annotation = annotations.get(source['id'], {})
        require(bool(item['task']) and item['task'] == annotation.get('todoText') and item['task'] in source['content'])
        require(item['ownerId'] == annotation.get('ownerId'))
        if item['ownerId'] is not None:
            require(item['ownerId'] in {m['author_id'] for m in messages})
        deadline = item['deadlineText']
        require(deadline is None or (bool(deadline) and deadline == annotation.get('deadlineText') and deadline in item['task']))
    for section in ('importantMessages', 'todos', 'unresolved'):
        keys = [tuple(i['sourceMessageIds']) for i in value[section]]
        require(len(keys) == len(set(keys)))


def integrate_digest(value, messages, titles, annotations, weekly):
    """显示名、计数、时间、原始发言均由本地回填，不能来自模型。"""
    full = build_digest(messages, titles, annotations, mode='full', weekly=weekly)
    by_id = {m['id']: m for m in messages}
    local_topics = {t['id']: t for t in full['topics']}
    def authored(item, field):
        source = by_id[item['sourceMessageIds'][0]]
        return dict(item, **{field: source['name']})
    for topic in value['topics']:
        local = local_topics[topic['id']]
        local.update(copy.deepcopy(topic))
        local['keywords'] = [topic['title']]
        local['conclusions'] = [authored(c, 'sender') for c in topic['conclusions']]
        local['viewpoints'] = [authored(v, 'sender') for v in topic['viewpoints']]
    full['topics'] = sorted(local_topics.values(), key=lambda t: (-bool(t['conclusions']), -t['messageCount'], t['id']))
    full['importantMessages'] = [dict(authored(i, 'sender'), time=clock(by_id[i['sourceMessageIds'][0]]['created']),
                                       note='原文明示的通知。') for i in value['importantMessages']]
    names = {m['author_id']: m['name'] for m in messages}
    full['todos'] = [dict(i, owner=names.get(i['ownerId']), deadline=i['deadlineText'],
                         topic=titles[by_id[i['sourceMessageIds'][0]]['topic']], note='原文任务，未知负责人或期限为空。')
                     for i in value['todos']]
    full['unresolved'] = [dict(authored(i, 'asker'), owner=None,
                              lastDiscussedAt=clock(by_id[i['sourceMessageIds'][0]]['created']),
                              note='原文明示的提问或待确认；后续解决状态未判定。') for i in value['unresolved']]
    full['overview'], full['overviewSourceMessageIds'] = value['overview'], value['overviewSourceMessageIds']
    full['hero'].update(copy.deepcopy(value['hero']))
    stats = full['summaryStats']
    stats.update(conclusionCount=sum(len(t['conclusions']) for t in full['topics']),
                 todoCount=len(full['todos']), unresolvedCount=len(full['unresolved']))
    full['hero']['statusLine'] = f'{stats["conclusionCount"]} 条明确决定 · {stats["todoCount"]} 条待办线索 · {stats["unresolvedCount"]} 条待跟进线索'
    full['hero']['pendingNote'] = full['unresolved'][0]['question'] if full['unresolved'] else None
    full['keywords'] = [t['title'] for t in full['topics']]
    return present_digest(full, 'full')


def present_digest(full, mode):
    if mode not in ('compact', 'full'):
        raise ValueError('请选择精简或完整阅读方式。')
    result = copy.deepcopy(full)
    result['mode'] = mode
    for name, limit in LIMITS.items():
        items = full[name]
        shown = items[:limit] if mode == 'compact' else items
        result[name] = copy.deepcopy(shown)
        result['sectionMeta'][name] = dict(enabled=bool(items), totalCount=len(items),
                                         displayedCount=len(shown), hiddenCount=len(items) - len(shown))
    return result


def generate(model, data, dimensions, mode):
    # 验证和整合使用调用前的快照，不信任模型对输入对象的潜在修改。
    data = copy.deepcopy(data)
    try:
        raw = model.complete(build_prompt(data))
    except Exception:
        raise GenerationError('离线模拟生成未完成，请重新生成。') from None
    try:
        require(isinstance(raw, str) and len(raw) <= 2_000_000)
        value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
        require(isinstance(value, dict) and value.keys() == {'digest', 'dimensions'})
        require(isinstance(value['dimensions'], dict) and value['dimensions'].keys() <= dimensions.keys())
        validate_digest(value['digest'], data['messages'], data['contexts'], data['annotations'])
    except (ValueError, TypeError, KeyError, RecursionError):
        raise GenerationError('离线摘要未通过内容校验，请重新生成。') from None
    weekly = data['request']['period'] == 'weekly'
    def result(digest, rows):
        full = integrate_digest(digest, rows, data['topicTitles'], data['annotations'], weekly)
        return dict(digest=present_digest(full, mode), fullDigest=full,
                    generation=dict(engine='local-mock', promptVersion=PROMPT_VERSION, status='complete'))
    main = result(value['digest'], data['messages'])
    for key, dimension in dimensions.items():
        try:
            digest = value['dimensions'][key]
            validate_digest(digest, dimension['messages'], dimension['contexts'], data['annotations'])
            dimension.update(result(digest, dimension['messages']))
        except (ValueError, TypeError, KeyError, RecursionError):
            dimension.update(digest=None, fullDigest=None,
                             generation=dict(engine='local-mock', promptVersion=PROMPT_VERSION, status='failed',
                                             message='此摘要未通过内容校验，请重新生成报告。'))
            main['generation']['status'] = 'partial'
    return main
