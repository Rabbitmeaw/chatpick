import copy
import json
import re
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from grouppulse.core import App
from grouppulse.live_generation import generate_report, input_facts, prompt, size
from grouppulse.reference_report import system_prompt
from grouppulse.report_response_cache import CachedReportModel


def message(mid, person, text, *, reference=None, name='同名', created='2026-10-20T09:00:00+00:00'):
    return dict(id=mid, server='s', channel='c', author_id=person, name=name,
                content=text, created=created, reference_id=reference, bot=False,
                topic='unclassified', stance='发言')


class EvidenceModel:
    input_budget = 24 * 1024

    def __init__(self):
        self.requests = []

    @property
    def calls(self):
        return len(self.requests)

    def complete(self, messages):
        self.requests.append(copy.deepcopy(messages))
        task = json.loads(messages[1]['content'].split('DISCORD_REPORT_SCOPE\n', 1)[1])
        ids = re.findall(r'^\[([^\]]+)\] \d{4}-\d{2}-\d{2} ', messages[1]['content'], re.M)
        people = task['authors']
        return json.dumps(dict(
            overview='合成样本仅用于检查输入分批及原核心处理。', overviewSourceMessageIds=ids,
            topics=[dict(title=f'合成讨论{len(self.requests)}', summary='保留各人的条件看法。',
                         participants=[person['label'] for person in people],
                         sourceMessageIds=ids, conclusions=[])],
            importantMessages=[], todos=[], unresolved=[]), ensure_ascii=False)


class ChatQualityTests(unittest.TestCase):
    def test_original_prompt_receives_fidelity_rules_and_unexplained_jargon_verbatim(self):
        rows = [message('1', 'a', '如果业绩超过预期，XYZ可能上涨；现在只是我的猜测。'),
                message('2', 'a', '果然，1:2才是关键，哈哈。')]
        value = prompt(dict(server='s', channel='c', days=1), dict(kind='member', member='a'), rows, [])
        self.assertTrue(value[0]['content'].startswith(system_prompt()))
        self.assertIn('同一作者事后自述不等于独立验证', value[0]['content'])
        self.assertIn('未定义的缩写、比例和黑话保留原词', value[0]['content'])
        self.assertIn('条件和时间顺序', value[0]['content'])
        self.assertIn(rows[0]['content'], value[1]['content'])
        self.assertIn(rows[1]['content'], value[1]['content'])

    def test_reply_relation_is_explicit_without_promoting_background_to_target_speech(self):
        background = message('old', 'b', '朋友转来的猜测，不代表我认可。')
        rows = [message('1', 'a', '我只是转述那句话，并未认同。', reference='old')]
        contexts = [dict(id='old', status='available', message=background)]
        facts = input_facts(dict(server='s', channel='c'), dict(kind='member', member='a'), rows, contexts)
        links = json.loads(facts['factsPrompt'].split('正文回复关系：\n', 1)[1])
        self.assertEqual(links, [dict(messageId='1', replyToMessageId='old')])
        self.assertEqual([row['id'] for row in facts['transcriptRows']], ['1'])
        self.assertIn('引用背景', facts['factsPrompt'])
        self.assertNotEqual(facts['transcriptRows'][0]['sender'], background['name'])
        model = EvidenceModel()
        result = generate_report(model, dict(server='s', channel='c', days=1),
                                 dict(kind='member', member='a'), rows, contexts, 'full')
        self.assertEqual([row['id'] for row in result['messages']], ['1'])
        self.assertEqual(result['fullDigest']['topics'][0]['participantIds'], ['a'])
        self.assertEqual(result['generation']['calls'], 1)

    def test_long_multi_author_input_is_batched_without_truncation_or_identity_merging(self):
        start = datetime(2026, 10, 20, tzinfo=timezone.utc)
        rows = [message(str(1000+i), f'person-{i % 3}',
                        f'第{i}条合成观点：如果条件尚未成立，就不能当成结果。' + '合成闲聊保留不同意见。' * 24,
                        name='改名后的同名' if i >= 48 and i % 3 == 0 else '同名',
                        created=(start + timedelta(minutes=i*20)).isoformat()) for i in range(96)]
        original = copy.deepcopy(rows)
        model = EvidenceModel()
        result = generate_report(model, dict(server='s', channel='c', days=2, timezone='UTC'),
                                 dict(kind='weekly'), rows, [], 'full')
        self.assertGreater(len(model.requests), 1)
        selected = [mid for request in model.requests for mid in re.findall(
            r'^\[([^\]]+)\] \d{4}-\d{2}-\d{2} ', request[1]['content'], re.M)]
        self.assertEqual(Counter(selected), Counter(row['id'] for row in rows))
        self.assertTrue(all(size(request) <= model.input_budget for request in model.requests))
        self.assertEqual(result['generation']['calls'], len(model.requests))
        self.assertEqual(result['generation']['core'], 'starmemo-original-js')
        self.assertEqual(result['fullDigest']['summaryStats']['messageCount'], 96)
        self.assertEqual(result['fullDigest']['summaryStats']['activeUsers'], 3)
        self.assertEqual(result['messages'], original)
        self.assertEqual(rows, original)
        self.assertEqual({person for topic in result['fullDigest']['topics'] for person in topic['participantIds']},
                         {'person-0', 'person-1', 'person-2'})

    def test_saved_instructions_reach_original_pipeline_and_separate_response_cache(self):
        rows = [message('1', 'a', '这是我的条件看法，尚未得到验证。')]
        base = dict(server='s', channel='c', days=1)
        first = dict(base, instructions='优先列出每人的观点，不整理生活闲聊。')
        second = dict(base, instructions='多保留有趣的闲聊，观点简短整理。')
        with tempfile.TemporaryDirectory() as directory:
            model = EvidenceModel()
            cached = CachedReportModel(model, App(Path(directory)/'test.sqlite3'))
            one = generate_report(cached, first, dict(kind='daily'), rows, [], 'full')
            two = generate_report(cached, second, dict(kind='daily'), rows, [], 'full')
            again = generate_report(cached, first, dict(kind='daily'), rows, [], 'full')
            self.assertEqual(len(model.requests), 2)
            self.assertEqual(one['generation']['calls'], 1)
            self.assertEqual(two['generation']['calls'], 1)
            self.assertEqual(again['generation']['calls'], 0)
            self.assertEqual(again['generation']['response_cache_hits'], 1)
            for request, expected, other in [(model.requests[0], first, second), (model.requests[1], second, first)]:
                system = request[0]['content']
                self.assertTrue(system.startswith(system_prompt()))
                self.assertIn('只影响整理重点和表达格式', system)
                self.assertIn('不能补造原文没有的内容', system)
                self.assertIn(expected['instructions'], system)
                self.assertNotIn(other['instructions'], system)

    def test_empty_saved_instructions_leave_model_input_unchanged(self):
        rows = [message('1', 'a', '合成闲聊。')]
        scope = dict(server='s', channel='c', days=1)
        ordinary = prompt(scope, dict(kind='daily'), rows, [])
        for empty in ('', '  \n\t '):
            self.assertEqual(prompt(dict(scope, instructions=empty), dict(kind='daily'), rows, []), ordinary)


if __name__ == '__main__':
    unittest.main()
