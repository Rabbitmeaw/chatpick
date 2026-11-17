import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grouppulse.live import LiveApp


class Vault:
    storage='test-memory'
    def get(self,_key):return ''


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'live.sqlite3'
        self.app=self.new_app()
        self.now='2026-10-20T10:00:00+00:00'
        self.clock=patch('grouppulse.local_alerts.utcnow',side_effect=lambda:self.now);self.clock.start()
        self.add('old',created='2026-10-20T09:00:00+00:00')

    def new_app(self):
        app=LiveApp(self.path,vault=Vault())
        app.demo=dict(servers={'s':dict(name='测试',channels={'c':'当前','d':'其他'})},channels={'c':'当前','d':'其他'},topics={},date='2026-10-20')
        app.save_meta('catalog',app.demo)
        return app

    def tearDown(self):self.clock.stop();self.app.auto_sync.stop();self.tmp.cleanup()

    def add(self,identifier,**extra):
        row=dict(id=identifier,server='s',channel='c',author_id='a',name='同名',bot=False,content='发布计划需要确认',
            created='2026-10-20T11:00:00+00:00',reference_id=None,reference_channel='c',topic='',stance='')
        row.update(extra);self.app.upsert_messages([row]);return self.app.message(identifier)

    def save(self,**extra):
        return self.app.save_alert_rule(dict(server='s',channel='c',keyword='发布',**extra))['rule']

    def check(self):return self.app.check_saved_alerts('s','c')

    def events(self):return self.app.alerts('c','s')

    def test_baseline_and_late_backfill_do_not_fire_but_new_evidence_does(self):
        rule=self.save();self.assertEqual(self.save()['id'],rule['id'])
        self.assertEqual(len(self.app.alert_rules('c','s')),1)
        self.assertEqual(self.check()['created'],0)
        self.add('late',created='2026-10-20T08:00:00+00:00')
        self.assertEqual(self.check()['created'],0)
        self.add('new');self.assertEqual(self.check()['created'],1)
        self.assertEqual(self.check()['created'],0)
        self.assertEqual(self.events()[0]['sourceMessageIds'],['new'])

    def test_reply_updates_merge_and_renames_or_repeated_versions_do_not_realert(self):
        self.save();first=self.add('new1');self.check()
        self.add('new2',reference_id='new1');self.assertEqual(self.check()['updated'],1)
        event=self.events()[0];self.assertEqual(len(self.events()),1)
        self.assertEqual(set(event['sourceMessageIds']),{'new1','new2'})
        self.app.upsert_messages([dict(first,name='后来改名')])
        self.assertEqual(self.check()['updated'],0)
        self.app.upsert_messages([dict(first,content='发布计划已经调整')])
        self.assertEqual(self.check()['updated'],1)
        self.app.upsert_messages([first]);self.assertEqual(self.check()['updated'],0)

    def test_deleted_or_unreadable_sources_are_not_returned_as_current_text(self):
        self.save();row=self.add('new');self.check()
        self.app.upsert_messages([dict(row,deleted=True)]);self.check()
        event=self.events()[0]
        self.assertEqual(event['messages'],[]);self.assertEqual(event['status'],'source_unavailable')
        self.app.upsert_messages([dict(row,unavailable=True)]);self.check()
        self.assertEqual(self.events()[0]['messages'],[])

    def test_member_and_keyword_use_stable_identity_and_scope_is_not_expandable(self):
        self.save(member='a')
        self.add('other',author_id='b');self.add('wrong',channel='d');self.add('foreign',server='other')
        self.assertEqual(self.check()['created'],0)
        self.add('right',author_id='a');self.assertEqual(self.check()['created'],1)
        rule=self.app.alert_rules('c','s')[0]
        with self.assertRaises(ValueError):self.app.save_alert_rule(dict(server='s',channel='d',id=rule['id'],enabled=False))
        with self.assertRaises(ValueError):self.app.alerts('','s')

    def test_pause_resume_rebaselines_and_restart_keeps_deduplication(self):
        rule=self.save()
        self.app.save_alert_rule(dict(server='s',channel='c',id=rule['id'],enabled=False))
        self.add('paused');self.assertEqual(self.check()['created'],0)
        self.now='2026-10-20T12:00:00+00:00'
        self.app.save_alert_rule(dict(server='s',channel='c',id=rule['id'],enabled=True))
        self.assertEqual(self.check()['created'],0)
        self.add('after',created='2026-10-20T13:00:00+00:00');self.check()
        restarted=self.new_app()
        self.assertEqual(restarted.check_saved_alerts('s','c')['created'],0)
        self.assertEqual(len(restarted.alerts('c','s')),1)

    def test_new_rule_and_preview_have_no_network_or_model_calls(self):
        self.app.ensure_synced=lambda *args:None
        with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')):
            self.save();self.check()
            preview=self.app.check_alerts('发布','c','2026-10-20','s')
        self.assertTrue(preview['preview']);self.assertFalse(preview['offline'])
        self.assertEqual(preview['created'],0);self.assertEqual(len(preview['matches']),1)
        self.assertEqual(self.events(),[])
