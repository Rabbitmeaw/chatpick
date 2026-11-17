import copy
import json
import re
import unittest

from grouppulse.live_generation import generate_report, prompt, size
from test_chat_quality import EvidenceModel, message


PROFILE = dict(id='us-stock-chat', version=1, server='s', account_id='reader',
               instructions='合成背景：保留观点、条件和闲聊。')
SCOPE = dict(server='s', channel='c', timezone='UTC', days=3,
             label='2026-10-19 00:00 → 2026-10-22 00:00')


def body_ids(sent):
    return re.findall(r'^\[([^\]]+)\] \d{4}-\d{2}-\d{2} ', sent[1]['content'], re.M)


def backgrounds(sent):
    text = sent[1]['content'].split('引用背景（仅供理解，不计入目标人物观点或正文来源）：\n', 1)[1]
    return json.JSONDecoder().raw_decode(text)[0]


class ScopedAssemblyTests(unittest.TestCase):
    def test_ancestor_order_does_not_omit_or_duplicate_body_quotes(self):
        grandparent = message('g', 'b', '原始条件。')
        parent = message('p', 'b', '回复原始条件。', reference='g')
        rows = [parent, message('r', 'a', '仅回应上一句。', reference='p')]
        contexts = [dict(id=row['id'], status='available', message=row) for row in (grandparent, parent)]
        original = copy.deepcopy((rows, contexts))
        model = EvidenceModel()
        result = generate_report(model, SCOPE, dict(kind='daily'), rows, contexts, 'full', PROFILE)
        self.assertEqual([item['id'] for item in backgrounds(model.requests[0])], ['g'])
        self.assertEqual(result['generation']['omitted_contexts'], 0)
        self.assertEqual((rows, contexts), original)
        model = EvidenceModel()
        generate_report(model, SCOPE, dict(kind='daily'), rows[1:], contexts, 'full', PROFILE)
        self.assertEqual([item['id'] for item in backgrounds(model.requests[0])], ['p','g'])

    def test_oversized_single_quote_marks_only_actual_omission_without_dropping_body(self):
        row = message('r', 'a', '仍保留完整正文。', reference='p')
        parent = message('p', 'b', '超长必要引用。' * 3000)
        model = EvidenceModel()
        result = generate_report(model, SCOPE, dict(kind='daily'), [row],
            [dict(id='p',status='available',message=parent)], 'full', PROFILE)
        self.assertEqual(body_ids(model.requests[0]), ['r'])
        self.assertEqual(result['generation']['omitted_contexts'], 1)
        self.assertEqual(result['generation']['context_batches'][0]['budget_omitted_ids'], ['p'])
        self.assertEqual(result['digest']['topics'][0]['review']['confidence'], 'low')

    def test_body_and_required_quotes_share_budget_before_batching(self):
        parent = message('p', 'b', '这是必要的上一句。' * 40)
        rows = [message(str(index), 'a', '不同作者的合成闲聊。' * 20, reference='p') for index in range(7)]
        contexts = [dict(id='p', status='available', message=parent)]
        model = EvidenceModel()
        model.input_budget = size(prompt(SCOPE, dict(kind='daily'), rows[:3], contexts, PROFILE)) + 1400
        result = generate_report(model, SCOPE, dict(kind='daily'), rows, contexts, 'full', PROFILE)
        self.assertGreater(model.calls, 1)
        self.assertTrue(all(size(sent) <= model.input_budget for sent in model.requests))
        self.assertEqual([mid for sent in model.requests for mid in body_ids(sent)], [row['id'] for row in rows])
        self.assertTrue(all('p' in [item['id'] for item in backgrounds(sent)] for sent in model.requests))
        self.assertEqual(result['generation']['omitted_contexts'], 0)

    def test_unavailable_quote_is_not_filled_from_another_channel(self):
        row = message('r', 'a', '这是回复，不代表我认同。', reference='p')
        contexts = [dict(id='p', status='missing', reason='原文不可读。')]
        result = generate_report(EvidenceModel(), SCOPE, dict(kind='daily'), [row], contexts, 'full', PROFILE)
        topic = result['digest']['topics'][0]
        self.assertEqual(topic['review']['confidence'], 'low')
        self.assertIn('引用', ' '.join(topic['review']['reasons']))
        self.assertEqual(topic['messages'], [row])
        bad = [dict(id='p', status='available', message=dict(message('p','b','另一频道'), channel='elsewhere'))]
        with self.assertRaises(ValueError):
            generate_report(EvidenceModel(), SCOPE, dict(kind='daily'), [row], bad, 'full', PROFILE)

    def test_body_presence_does_not_override_a_foreign_reference_scope(self):
        parent=message('p','b','本频道的独立发言。')
        row=dict(message('r','a','引用标注来自其他频道。',reference='p'),reference_channel='other')
        result=generate_report(EvidenceModel(),SCOPE,dict(kind='daily'),[parent,row],
            [dict(id='p',status='missing',reason='引用归属不匹配。')],'full',PROFILE)
        self.assertEqual(result['digest']['topics'][0]['review']['confidence'],'low')

    def test_dated_same_asset_entries_survive_original_deduper_and_sort_by_time(self):
        class Model(EvidenceModel):
            def complete(self, messages):
                root = json.loads(super().complete(messages))
                mid = body_ids(messages)[0]
                root['overview'] = '只在这一批得出的局部速览。'
                root['hero'] = dict(summary=root['overview'], keyTakeaway='仅第一批的结论。')
                root['topics'] = [dict(title='AAA的个人分析', summary='当时仍要看条件：' + mid,
                    section='stock_opinion', asset='AAA', heat='高' if mid == 'late' else '低',
                    sourceMessageIds=[mid], conclusions=[])]
                return json.dumps(root, ensure_ascii=False)
        rows = [message('late', 'a', '较晚的新条件。', created='2026-10-21T09:00:00+00:00'),
                message('early', 'a', '较早的旧条件。', created='2026-10-19T09:00:00+00:00')]
        model = Model()
        result = generate_report(model, SCOPE, dict(kind='daily'), rows, [], 'full', PROFILE)
        self.assertEqual(model.calls, 2)
        for sent, date in zip(model.requests, ['2026-10-19','2026-10-21']):
            boundary = json.loads(sent[1]['content'].split('DISCORD_REPORT_SCOPE\n',1)[1])
            self.assertTrue(boundary['scope']['batch_window']['first_message_at'].startswith(date))
        for digest in result['presentations'].values():
            self.assertEqual(len(digest['topics']), 1)
            topic = digest['topics'][0]
            self.assertEqual([item['messages'][0]['id'] for item in topic['timeline']], ['early','late'])
            self.assertEqual([item['summary'] for item in topic['timeline']],
                             ['当时仍要看条件：early','当时仍要看条件：late'])
            self.assertNotIn('只在这一批', digest['hero']['summary'])
            self.assertFalse(digest['hero'].get('keyTakeaway'))
            self.assertEqual(topic['sourceMessageIds'], ['early','late'])

    def test_low_confidence_is_explicit_with_original_evidence_not_a_paid_reviewer(self):
        class Model(EvidenceModel):
            def complete(self, messages):
                root = json.loads(super().complete(messages))
                root['topics'][0].update(confidence='low', reviewReason='原话里的比例没有解释。')
                return json.dumps(root, ensure_ascii=False)
        rows = [message('1','a','还是那个1:2。')]
        model = Model()
        result = generate_report(model, SCOPE, dict(kind='member',member='a'), rows, [], 'compact', PROFILE)
        topic = result['digest']['topics'][0]
        self.assertEqual(model.calls, 1)
        self.assertEqual(topic['review'], dict(confidence='low', reasons=['原话里的比例没有解释。']))
        self.assertEqual(topic['messages'], rows)
        self.assertIn('不改变原发言对象', model.requests[0][0]['content'])

    def test_explicit_news_assets_are_separate_and_do_not_merge_into_personal_opinions(self):
        class Model(EvidenceModel):
            def complete(self, messages):
                root = json.loads(super().complete(messages))
                base = root['topics'][0]
                root['topics'] = [dict(base,title='AAA的个人看法',asset='AAA',section='stock_opinion'),
                    dict(base,title='AAA的产品消息',asset='AAA',section='news'),
                    dict(base,title='BBB的产品消息',asset='BBB',section='news')]
                return json.dumps(root,ensure_ascii=False)
        result = generate_report(Model(), SCOPE, dict(kind='daily'),
            [message('1','a','AAA是我的看法；另转述AAA和BBB的产品信息。')], [], 'full', PROFILE)
        self.assertEqual([(item['section'],item['asset']) for item in result['digest']['topics']],
            [('stock_opinion','AAA'),('news','AAA'),('news','BBB')])


if __name__ == '__main__':
    unittest.main()
