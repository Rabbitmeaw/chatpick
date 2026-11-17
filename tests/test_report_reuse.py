import copy
import json
import unittest

from grouppulse.reference_report import call, system_prompt


def source_report():
    return dict(overview='分享时间已定，报名与录屏待跟进。',
                hero=dict(headline='读书分享准备', summary='分享时间已定，报名与录屏待跟进。'),
                topics=[dict(title='读书分享', summary='周六20:00分享。', heat='高',
                             sourceMessageIds=['a'], participants=['成员1'], keywords=['分享'], conclusions=[])],
                todos=[], unresolved=[], importantMessages=[], keywords=[])


def source_facts(count=3):
    return dict(metadata=dict(groupName='测试服务器 / #产品', dateRange='2026-10-21', messageCount=count,
                              activeUsers=1, reportMode='full'),
                transcriptRows=[dict(id='a', datetime='2026-10-21 09:00', sender='成员1', content='周六20:00分享。')],
                topSpeakers=[dict(name='成员1', count=count)], activeTimeline='09:00', voiceLeaderboard=[],
                media=dict(gallery=[], visionGallery=[], voiceHighlights=[], funBadges=[]))


class ReportReuseTests(unittest.TestCase):
    def parse(self, value, count=3, mode='full'):
        facts=source_facts(count)
        return call('parseGroupDailyReport', json.dumps(value, ensure_ascii=False), facts['topSpeakers'],
                    facts['activeTimeline'], [], dict(facts['metadata'], reportMode=mode), facts['media'])

    def test_original_editor_rules_and_original_transcript_builder_are_used(self):
        self.assertIn('先合并同一事件，再按重要性排序', system_prompt())
        self.assertIn('不为了填满模板而生成内容', system_prompt())
        facts=source_facts()
        result=call('buildGroupReportInput', [], None, True, 'full', facts=facts)
        self.assertIn('[a] 2026-10-21 09:00 成员1：周六20:00分享。', result['prompt'])
        self.assertIn('按实际内容提炼 1-4 个主要话题，不要为了凑数编造', result['prompt'])
        self.assertEqual(result['metadata'], facts['metadata'])

    def test_original_confidence_sort_source_deduplication_and_trimming_execute(self):
        report=source_report()
        report['importantMessages']=[
            dict(sender='成员1', content='低优先级重复', note='通知', sourceMessageIds=['a'], confidence=.6),
            dict(sender='成员1', content='高优先级原件', note='通知', sourceMessageIds=['a'], confidence=.95),
            dict(sender='成员1', content='低置信度', note='待核对', sourceMessageIds=['b'], confidence=.2)]
        full=self.parse(report)
        compact=self.parse(report, mode='compact')
        self.assertEqual([i['content'] for i in full['importantMessages']], ['高优先级原件', '低置信度'])
        self.assertEqual([i['content'] for i in compact['importantMessages']], ['高优先级原件'])
        report['topics'][0]['summary']='长'*200
        self.assertEqual(len(self.parse(report)['topics'][0]['summary']), 140)
        self.assertEqual(len(self.parse(report, mode='compact')['topics'][0]['summary']), 100)

    def test_original_topic_limit_uses_message_volume(self):
        report=source_report()
        report['topics']=[dict(copy.deepcopy(report['topics'][0]), title='话题'+str(i), sourceMessageIds=[str(i)]) for i in range(9)]
        self.assertEqual(len(self.parse(report, count=3, mode='compact')['topics']), 3)
        self.assertEqual(len(self.parse(report, count=80, mode='compact')['topics']), 4)
        self.assertEqual(len(self.parse(report, count=1000)['topics']), 8)

    def test_original_local_jsonrepair_is_used_without_another_model_call(self):
        value=call('extractJson', "```json\n{overview: '可修复', topics: [],}\n```")
        self.assertEqual(value, dict(overview='可修复', topics=[]))
