"""将已校验的报告快照排为文字消息；本模块没有 Discord 客户端或发送实现。"""

NAMES = dict(daily='日报', weekly='周报', member='成员发言摘要', discussion='多人讨论摘要')


def payloads(report, digest, kind, server_name, channel_name, subject=None):
    def sources(ids):
        return '〔消息 ' + '、'.join(ids) + '〕' if ids else ''

    lines = [f'拾聊 · {NAMES[kind]}', f'来源：{server_name} / #{channel_name}',
             f'周期：{report["window"]["label"]}（北京时间）',
             f'对象：{subject["name"]}' if subject else '', digest['overview'],
             '覆盖：' + report['coverage']['note']]
    def append_digest(value):
        for topic in value['topics']:
            lines.extend(['', '讨论话题：' + topic['title'], topic['summary'] + sources(topic['summarySourceMessageIds'])])
            for view in topic['viewpoints']:
                lines.append(f'{view["sender"]}（{view["stance"]}）：{view["text"]}' + sources(view['sourceMessageIds']))
        for title, section, field in [('重要消息', 'importantMessages', 'content'), ('待办', 'todos', 'task'), ('待跟进', 'unresolved', 'question')]:
            if value[section]:
                lines.extend(['', title])
                for item in value[section]:
                    lines.append(item[field] + sources(item['sourceMessageIds']))
    append_digest(digest)
    if kind in ('daily', 'weekly'):
        for dimension in report['dimensions'].values():
            lines.extend(['', '关注维度 · ' + NAMES[dimension['kind']], '对象：' + dimension['subject']['name']])
            if dimension['generation']['status'] != 'complete':
                lines.append('该维度生成失败，未包含摘要。')
                continue
            lines.extend([dimension['fullDigest']['overview'], '覆盖：' + dimension['coverage']['note']])
            append_digest(dimension['fullDigest'])
    text = '\n'.join(lines)
    # 按 UTF-16 单元保守分段，预留模拟标识空间；提及始终禁止解析。
    chunks, chunk, units = [], [], 0
    for char in text:
        size = len(char.encode('utf-16-le')) // 2
        if units + size > 1740:
            chunks.append(''.join(chunk))
            chunk, units = [], 0
        chunk.append(char)
        units += size
    if chunk:
        chunks.append(''.join(chunk))
    return [dict(content='[离线模拟 · 未实际发送]\n' + part, allowed_mentions=dict(parse=[])) for part in chunks]
