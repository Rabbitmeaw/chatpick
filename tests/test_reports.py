import tempfile
import unittest
from pathlib import Path

from grouppulse.core import App
from grouppulse.reports import build_digest


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Path(self.tmp.name) / 'demo.sqlite3')
        self.app.import_demo()

    def tearDown(self):
        self.tmp.cleanup()

    def test_structured_daily_evidence_and_identity(self):
        report = self.app.report()
        digest = report['digest']
        self.assertEqual(digest['summaryStats']['messageCount'], 8)
        self.assertEqual(digest['summaryStats']['activeUsers'], 3)
        topic = next(t for t in digest['topics'] if t['id'] == 'launch')
        self.assertEqual(topic['timeRange'], '09:10–10:20')
        self.assertEqual(set(topic['participantIds']), {'u1', 'u2', 'u3'})
        self.assertEqual(topic['messageCount'], 5)
        self.assertEqual(topic['conclusions'][0]['sourceMessageIds'], ['m105'])
        self.assertEqual(digest['todos'][0]['sourceMessageIds'], ['m105'])
        self.assertIsNone(digest['todos'][0]['owner'])
        self.assertEqual(digest['todos'][0]['deadline'], '明天')
        allowed = {m['id'] for m in report['messages']}
        for section in ['topics', 'importantMessages', 'todos', 'unresolved']:
            for item in digest[section]:
                self.assertTrue(set(item['sourceMessageIds']) <= allowed)

    def test_member_does_not_inherit_another_persons_decision(self):
        report = self.app.report(kind='member', member='u1')
        digest = report['digest']
        self.assertEqual(digest['summaryStats']['messageCount'], 3)
        self.assertEqual(digest['summaryStats']['activeUsers'], 1)
        self.assertEqual(digest['summaryStats']['conclusionCount'], 0)
        self.assertEqual(digest['todos'], [])
        self.assertIsNone(digest['hero']['keyTakeaway'])
        for topic in digest['topics']:
            self.assertEqual(topic['participantIds'], ['u1'])
            self.assertNotIn('小林：', topic['summary'])

    def test_compact_hides_only_display_and_full_recovers_all(self):
        messages = []
        titles = {}
        for index in range(5):
            topic = f't{index}'
            titles[topic] = f'话题 {index}'
            messages.append(dict(self.app.message('m101'), id=f'x{index}', topic=topic))
        compact = build_digest(messages, titles, {}, mode='compact')
        full = build_digest(messages, titles, {}, mode='full')
        self.assertEqual(len(compact['topics']), 3)
        self.assertEqual(compact['sectionMeta']['topics']['hiddenCount'], 2)
        self.assertEqual(compact['summaryStats']['topicCount'], 5)
        self.assertEqual(len(full['topics']), 5)
        self.assertEqual(full['sectionMeta']['topics']['hiddenCount'], 0)
        self.assertEqual(compact['summaryStats'], full['summaryStats'])

    def test_annotations_rechecked_after_source_edit(self):
        row = self.app.message('m105')
        row['content'] = '这条消息已编辑，不再包含原来的待办。'
        self.app.upsert_messages([row])
        self.assertEqual(self.app.report()['digest']['todos'], [])

    def test_week_and_empty(self):
        week = self.app.report(kind='weekly')['digest']
        self.assertEqual(week['summaryStats']['messageCount'], 12)
        self.assertTrue(any('m080' in c['sourceMessageIds'] for t in week['topics'] for c in t['conclusions']))
        empty = self.app.report(date='2026-11-01')['digest']
        self.assertEqual(empty['topics'], [])
        self.assertEqual(empty['todos'], [])
        self.assertIsNone(empty['hero']['keyTakeaway'])

    def test_focus_dimensions_do_not_filter_periodic_report(self):
        plain = self.app.report()
        report = self.app.report(focus_member='u1', focus_topic='launch')
        self.assertEqual(report['metrics'], plain['metrics'])
        self.assertEqual(report['messages'], plain['messages'])
        self.assertEqual(report['digest'], plain['digest'])
        member = report['dimensions']['member']
        discussion = report['dimensions']['discussion']
        self.assertEqual(member['selected_count'], 3)
        self.assertEqual(member['subject']['id'], 'u1')
        self.assertTrue(all(m['author_id'] == 'u1' for m in member['messages']))
        self.assertEqual(member['digest']['summaryStats']['conclusionCount'], 0)
        self.assertEqual({c['id']: c for c in member['contexts']}['m090']['message']['author_id'], 'u2')
        self.assertEqual(discussion['selected_count'], 5)
        self.assertIn('反对', {m['stance'] for m in discussion['messages']})
        self.assertEqual(discussion['digest']['summaryStats']['activeUsers'], 3)

    def test_weekly_dimensions_match_independent_summaries(self):
        report = self.app.report(kind='weekly', focus_member='u1', focus_topic='launch', mode='full')
        for kind, field, subject in [('member', 'member', 'u1'), ('discussion', 'topic', 'launch')]:
            independent = self.app.report(kind=kind, period=report['period'], mode='full', **{field: subject})
            self.assertEqual(independent['window'], report['window'])
            self.assertEqual(independent['period'], 'weekly')
            self.assertEqual(independent['dimensions'], {})
            for key in ['kind', 'period', 'subject', 'messages', 'contexts', 'digest', 'coverage', 'selected_count']:
                self.assertEqual(independent[key], report['dimensions'][kind][key], key)
        self.assertIn('m091', {m['id'] for m in report['dimensions']['member']['messages']})
        self.assertIn('m080', {m['id'] for m in report['dimensions']['discussion']['messages']})

    def test_dimension_scope_and_period_validation(self):
        for options in [dict(server='builders', channel='dev', focus_member='u1'),
                        dict(server='builders', channel='dev', focus_topic='launch'),
                        dict(kind='member', member='u1', period='bad'),
                        dict(kind='discussion', topic='launch', focus_member='u1')]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.app.report(**options)
        empty = self.app.report(channel='design', focus_member='u1', focus_topic='launch')
        self.assertEqual(empty['selected_count'], 2)
        self.assertEqual(empty['dimensions']['member']['selected_count'], 0)
        self.assertEqual(empty['dimensions']['discussion']['selected_count'], 0)

    def test_custom_range_includes_end_day_and_compares_equal_period(self):
        report = self.app.report(kind='weekly', date='2026-10-20', end_date='2026-10-21')
        self.assertEqual(report['window']['start'], '2026-10-19T16:00:00+00:00')
        self.assertEqual(report['window']['end'], '2026-10-21T16:00:00+00:00')
        self.assertEqual(report['window']['days'], 2)
        self.assertEqual(report['metrics']['messages'], 11)
        self.assertEqual(report['metrics']['previous'], 1)
        row = self.app.message('m101')
        self.app.upsert_messages([
            dict(row, id='end-last', created='2026-10-21T15:59:59+00:00'),
            dict(row, id='next-day', created='2026-10-21T16:00:00+00:00')])
        again = self.app.report(kind='weekly', date='2026-10-20', end_date='2026-10-21')
        self.assertIn('end-last', {m['id'] for m in again['messages']})
        self.assertNotIn('next-day', {m['id'] for m in again['messages']})

    def test_specified_period_can_be_long_or_historical(self):
        long_report = self.app.report(kind='weekly', date='2026-09-10', end_date='2026-10-21',
                                      focus_member='u1', focus_topic='launch')
        self.assertEqual(long_report['window']['days'], 42)
        self.assertEqual(long_report['metrics']['messages'], 12)
        self.assertFalse(long_report['coverage']['window_complete'])
        historical = self.app.report(kind='member', member='u1', period='weekly',
                                      date='2026-10-18', end_date='2026-10-20')
        self.assertEqual(historical['window']['days'], 3)
        self.assertEqual([m['id'] for m in historical['messages']], ['m091'])
        self.assertEqual(historical['window']['end_date'], '2026-10-20')
        self.assertNotIn('m101', {m['id'] for m in historical['messages']})

    def test_custom_range_dimensions_match_independent_summaries(self):
        options = dict(date='2026-10-20', end_date='2026-10-21', mode='full')
        report = self.app.report(kind='weekly', focus_member='u1', focus_topic='launch', **options)
        for kind, field, subject in [('member', 'member', 'u1'), ('discussion', 'topic', 'launch')]:
            independent = self.app.report(kind=kind, period='weekly', **{field: subject}, **options)
            self.assertEqual(independent['window'], report['window'])
            for key in ['messages', 'contexts', 'digest', 'coverage']:
                self.assertEqual(independent[key], report['dimensions'][kind][key], key)
        self.assertEqual(report['window']['start_date'], '2026-10-20')
        self.assertEqual(report['window']['end_date'], '2026-10-21')

    def test_custom_range_validation_and_single_day(self):
        for options in [dict(kind='weekly', end_date='bad'),
                        dict(kind='weekly', end_date='2026-10-20'),
                        dict(kind='daily', end_date='2026-10-22'),
                        dict(kind='weekly', date='9999-12-31', end_date='9999-12-31')]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.app.report(**options)
        single = self.app.report(kind='weekly', end_date='2026-10-21')
        self.assertEqual(single['window']['days'], 1)
        self.assertEqual(single['metrics']['messages'], 8)


if __name__ == '__main__':
    unittest.main()
