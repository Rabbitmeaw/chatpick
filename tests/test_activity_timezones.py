import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import MethodType
from zoneinfo import ZoneInfo

from grouppulse.activity import counts, window_info
from grouppulse.file_imports import FileReportApp
from grouppulse.live import LiveApp


class Vault:
    storage = 'test-memory'
    def get(self, _key): return ''


class Model:
    input_budget = 48 * 1024
    engine = 'test-local'
    def __init__(self):
        self.calls = 0
        self.requests = []

    def complete(self, messages):
        self.calls += 1
        self.requests.append(copy.deepcopy(messages))
        return json.dumps(dict(overview='合成跨午夜讨论。',
            overviewSourceMessageIds=['previous', 'early', 'late'],
            topics=[dict(title='合成讨论', summary='保留原记录的时间与观点。',
                         sourceMessageIds=['previous', 'early', 'late'], participants=[], conclusions=[])],
            importantMessages=[], todos=[], unresolved=[]), ensure_ascii=False)


class ActivityTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = LiveApp(Path(self.tmp.name) / 'test.sqlite3', vault=Vault())
        self.app.model = self.model = Model()
        self.app.demo = dict(servers={'s':dict(name='合成服务器', channels={'c':'合成频道'})},
                             channels={'c':'合成频道'}, topics={}, date='2026-10-20')
        self.app.report_timezone = ZoneInfo('America/Los_Angeles')
        self.app.report_timezone_name = 'America/Los_Angeles'
        # 个人读取和导入共用此原有日期适配；本测试不启动读取服务。
        self.app.window = MethodType(FileReportApp.window, self.app)
        self.synced = []
        self.app.ensure_synced = lambda *args: self.synced.append(args)
        self.app.coverage = lambda *args: dict(status='complete', window_complete=True,
                                              up_to_date=True, missing=0, note='合成完整窗口')
        base = dict(server='s', channel='c', author_id='a', name='同名', bot=False,
                    stance='', topic='unclassified', reference_id=None,
                    content='合成跨午夜讨论。', text_available=True)
        self.app.upsert_messages([
            dict(base, id='previous', created='2026-10-20T06:30:00+00:00'),
            dict(base, id='early', created='2026-10-20T07:30:00+00:00'),
            dict(base, id='late', created='2026-10-21T06:30:00+00:00'),
            dict(base, id='outside', created='2026-10-21T07:00:00+00:00')])

    def tearDown(self):
        self.app.auto_sync.stop()
        self.tmp.cleanup()

    def test_selected_timezone_controls_window_daily_groups_and_hours(self):
        value = self.app.report(server='s', channel='c', kind='activity', date='2026-10-20')
        self.assertEqual(value['window']['timezone'], 'America/Los_Angeles')
        self.assertEqual((value['window']['start_date'], value['window']['end_date'], value['window']['days']),
                         ('2026-10-20', '2026-10-20', 1))
        self.assertEqual(value['comparison']['window']['start_date'], '2026-10-19')
        self.assertEqual(value['metrics']['messages'], 2)
        self.assertEqual(value['metrics']['previous'], 1)
        self.assertEqual(value['metrics']['hours'][0]['count'], 1)
        self.assertEqual(value['metrics']['hours'][23]['count'], 1)
        self.assertEqual(sum(row['count'] for row in value['metrics']['hours']), 2)
        self.assertEqual([(row['date'], row['period'], row['messages']) for row in value['daily']],
                         [('2026-10-19', 'previous', 1), ('2026-10-20', 'current', 2)])
        self.assertEqual(value['daily'][0]['hours'][23]['count'], 1)
        self.assertEqual([row['id'] for row in value['messages']], ['early', 'late'])
        self.assertEqual(self.synced, [('s', 'c', '2026-10-19', '2026-10-20')])
        self.assertEqual(self.model.calls, 0)

    def test_dst_short_and_long_local_days_stay_one_calendar_day(self):
        for day, hours in [('2026-03-08', 23), ('2026-11-01', 25)]:
            with self.subTest(day=day):
                start, end = self.app.window(day)
                self.assertEqual((end-start).total_seconds()/3600, hours)
                result = self.app.report(server='s', channel='c', kind='activity', date=day)
                self.assertEqual((result['window']['start_date'], result['window']['end_date']), (day, day))
                self.assertEqual(result['window']['days'], 1)
                self.assertEqual(result['period'], 'daily')
        self.assertEqual(self.model.calls, 0)

    def test_original_topic_pipeline_receives_selected_timezone_and_calendar_dates(self):
        value = self.app.topic_trends(server='s', channel='c', date='2026-10-20')
        self.assertEqual(value['topic_analysis']['generation']['report_runtime'], 'copied-starmemo')
        self.assertEqual((value['trends'][0]['current'], value['trends'][0]['previous']), (2, 1))
        self.assertEqual(self.model.calls, 1)
        boundary = json.loads(self.model.requests[0][1]['content'].split('DISCORD_REPORT_SCOPE\n', 1)[1])
        self.assertEqual(boundary['scope']['timezone'], 'America/Los_Angeles')
        self.assertEqual((boundary['scope']['start_date'], boundary['scope']['end_date'], boundary['scope']['days']),
                         ('2026-10-19', '2026-10-20', 2))

    def test_direct_call_defaults_preserve_beijing_dates_and_hours(self):
        start = datetime.fromisoformat('2026-10-19T16:00:00+00:00')
        end = datetime.fromisoformat('2026-10-20T16:00:00+00:00')
        result = window_info(start, end)
        self.assertEqual(result['timezone'], 'Asia/Shanghai')
        self.assertEqual((result['start_date'], result['end_date'], result['days']),
                         ('2026-10-20', '2026-10-20', 1))
        self.assertEqual(counts([self.app.message('early')])['hours'][15]['count'], 1)


if __name__ == '__main__':
    unittest.main()
