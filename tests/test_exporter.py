import json
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_live
from test_file_imports import export_file


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.live = test_live.LiveTests(); self.live.setUp()
        self.app = self.live.app

    def tearDown(self): self.live.tearDown()

    def test_personal_reading_requires_explicit_opt_in_even_with_a_saved_token(self):
        exporter = self.app.exporter
        token = 'personal-fixture'
        saved = dict(has_token=True, status='connected', user_id='901', revision='legacy',
                     servers={'100':'演示服务器'}, channels={'100':{'200':'演示频道'}})
        self.live.vault.set('exporter-user', token)
        self.app.save_meta('exporter-account', saved)
        request = dict(server='100', channel='200', date='2026-10-20')
        self.assertFalse(exporter.state()['enabled'])
        self.assertEqual(exporter.state()['status'], 'disabled')
        with patch('grouppulse.discord.http_json') as read:
            for operation in (exporter.connect, lambda: exporter.channels('100'),
                              lambda: exporter.prepare(request), lambda: exporter.queue(request)):
                with self.assertRaisesRegex(ValueError, '读取已关闭'):
                    operation()
            with self.assertRaisesRegex(ValueError, '风险提示'):
                exporter.save(dict(token=token))
            read.assert_not_called()

            state = exporter.save(dict(enabled=True))
            self.assertTrue(state['enabled'])
            self.assertTrue(type(exporter)(self.app).state()['enabled'])
            client = exporter.client(state, token)
            read.return_value = dict(id='901', username='演示成员')
            self.assertEqual(client.identity()['user_id'], '901')
            self.assertNotIn('method', read.call_args.kwargs)
            exporter.save(dict(enabled=False))
            read.reset_mock()
            with self.assertRaisesRegex(ValueError, '读取已关闭'):
                client.identity()
            read.assert_not_called()

    def test_disabling_personal_reading_stops_an_active_export(self):
        exporter = self.app.exporter
        exporter.save(dict(enabled=True, token='personal-fixture'))
        self.app.save_meta('exporter-account', dict(exporter.state(), status='connected', user_id='901',
            servers={'100':'演示服务器'}, channels={'100':{'200':'演示频道'}}))
        process = MagicMock()
        process.__enter__.return_value = process
        process.poll.return_value = None

        def wait(**_options):
            exporter.save(dict(enabled=False))
            return -9

        process.wait.side_effect = wait
        client = MagicMock()
        client.identity.return_value = dict(user_id='901')
        with patch.object(exporter, 'client', return_value=client), \
             patch('grouppulse.exporter.ensure_binary', return_value='/verified/exporter'), \
             patch('grouppulse.exporter.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(ValueError, '记录导出未完成'):
                exporter.prepare(dict(server='100', channel='200', date='2026-10-20'))
        process.kill.assert_called_once()
        self.assertFalse(exporter.state()['enabled'])
        self.assertEqual(self.app.file_imports.items()['items'], [])
        self.assertFalse(self.live.model_inputs)

    def test_exact_minute_export_filters_background_and_rejects_invalid_windows(self):
        self.app.exporter.save(dict(enabled=True, token='personal-test-token'))
        self.app.save_meta('exporter-account', dict(enabled=True, has_token=True,status='connected', revision='first', user_id='901',
            servers={'100':'服务器'}, channels={'100':{'200':'频道'}}))
        class Client:
            def identity(self): return dict(user_id='901', username='本人')
            def check_channel(self, *_args): return None
            def message(_self, channel, mid): return self.live.rows[0]
        def run(binary, token, channel, start, end, output, **_options):
            self.assertEqual((start, end), ('2026-10-20T09:01:00+00:00', '2026-10-20T09:02:00+00:00'))
            output.write_text(json.dumps(export_file(self.live.rows[1:2])))
        request = dict(server='100', channel='200', start_time='2026-10-20T17:01', end_time='2026-10-20T17:02')
        with patch.object(self.app.exporter, 'client', return_value=Client()), \
             patch('grouppulse.exporter.ensure_binary', return_value='/verified/exporter'), \
             patch('grouppulse.exporter.run_export', side_effect=run):
            item = self.app.exporter.prepare(request)
            self.assertEqual(item['message_count'], 2)
            self.assertEqual(item['analysis_count'], 1)
            self.assertEqual(item['context_count'], 1)
            self.assertEqual(item['member_counts'], {'902':1})
            with self.assertRaisesRegex(ValueError,'超出.*范围'):
                self.app.file_imports.report(dict(import_id=item['id'],server='100',channel='200',member='901',
                    start_time='2026-10-20T17:00',end_time='2026-10-20T17:02'))
        for change in [dict(end_time='2026-10-20T17:01'), dict(end_time=''),
                       dict(start_time='2026-03-08T02:30', end_time='2026-03-08T03:30', timezone='America/Los_Angeles'),
                       dict(start_time='2026-11-01T01:30', end_time='2026-11-01T02:30', timezone='America/Los_Angeles')]:
            with self.assertRaises(ValueError): self.app.exporter.prepare(dict(request, **change))
        self.assertFalse(self.live.model_inputs)

    def test_token_is_protected_and_does_not_replace_bot_or_model(self):
        self.live.configure()
        config = self.app.get_config(); bots = self.app.bots()
        state = self.app.exporter.save(dict(enabled=True, token='personal-test-token'))
        self.assertTrue(state['has_token'])
        self.assertNotIn('personal-test-token', json.dumps(state))
        self.assertEqual(self.app.get_config(), config)
        self.assertEqual(self.app.bots(), bots)
        self.assertEqual(self.live.vault.get('exporter-user'), 'personal-test-token')
        with self.assertRaises(ValueError): self.app.exporter.save(dict(token='bad token'))

    def test_cli_is_called_without_secret_arguments_and_scope_is_enforced(self):
        from grouppulse.exporter import run_export
        value = export_file(self.live.rows)
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / 'records.json'
            def run(arguments, **kwargs):
                self.assertEqual(arguments[1], 'export')
                self.assertNotIn('personal-test-token', ' '.join(arguments))
                self.assertEqual(kwargs['env']['DISCORD_TOKEN'], 'personal-test-token')
                self.assertNotIn('exportguild', arguments)
                self.assertEqual(arguments[arguments.index('--markdown')+1], 'false')
                output.write_text(json.dumps(value))
                process = MagicMock()
                process.__enter__.return_value = process
                process.wait.return_value = 0
                return process
            with patch('grouppulse.exporter.subprocess.Popen', side_effect=run):
                run_export('/verified/exporter', 'personal-test-token', '200', '2026-10-20T00:00:00+00:00',
                           '2026-10-21T00:00:00+00:00', output)

    def test_prepare_reads_one_scope_into_internal_file_without_model_or_bot_mutation(self):
        self.live.configure()
        before = self.app.meta('catalog'); selection = self.app.meta('selection', {})
        self.app.exporter.save(dict(enabled=True, token='personal-test-token'))
        class Client:
            def identity(self): return dict(user_id='901', username='本人')
            def guilds(self): return {'100':'测试服务器'}
            def readable_channels(self, guild, user): return {'200':'产品频道'}
            def check_channel(self, guild, channel, user):
                if (guild, channel) != ('100','200'): raise ValueError('范围不符')
            def message(self, *_args): return None
        def run(binary, token, channel, start, end, output, **_options):
            output.write_text(json.dumps(export_file(self.live.rows)))
        with patch.object(self.app.exporter, 'client', return_value=Client()), \
             patch('grouppulse.exporter.ensure_binary', return_value='/verified/exporter'), \
             patch('grouppulse.exporter.run_export', side_effect=run):
            self.app.exporter.connect()
            self.app.exporter.channels('100')
            item = self.app.exporter.prepare(dict(server='100', channel='200', date='2026-10-20', timezone='Asia/Shanghai'))
            self.assertEqual(item['message_count'], 3)
            self.assertEqual(set(item['members']), {'901','902'})
            with self.assertRaises(ValueError):
                self.app.exporter.prepare(dict(server='100', channel='201', date='2026-10-20'))
        self.assertFalse(self.live.model_inputs)
        self.assertEqual(self.app.meta('catalog'), before)
        self.assertEqual(self.app.meta('selection', {}), selection)
        self.assertIsNone(self.app.message(self.live.rows[0]['id']))

    def test_replacing_account_during_export_discards_result_and_keeps_original_credential(self):
        self.app.exporter.save(dict(enabled=True, token='first-token'))
        self.app.save_meta('exporter-account', dict(enabled=True, has_token=True, status='connected', revision='first',
            user_id='901', servers={'100':'服务器'}, channels={'100':{'200':'频道'}}))
        class Client:
            def identity(self): return dict(user_id='901', username='本人')
            def check_channel(self, *_args): return None
        def run(binary, token, channel, start, end, output, **_options):
            self.assertEqual(token, 'first-token')
            self.app.exporter.save(dict(token='replacement-token'))
            output.write_text(json.dumps(export_file(self.live.rows)))
        with patch.object(self.app.exporter, 'client', return_value=Client()), \
             patch('grouppulse.exporter.ensure_binary', return_value='/verified/exporter'), \
             patch('grouppulse.exporter.run_export', side_effect=run):
            with self.assertRaisesRegex(ValueError, '账号已变化'):
                self.app.exporter.prepare(dict(server='100', channel='200', date='2026-10-20'))
        self.assertEqual(self.app.file_imports.items()['items'], [])
        self.assertFalse(self.live.model_inputs)

    def test_export_supplements_only_same_channel_reference_and_excludes_background_from_member_picker(self):
        self.app.exporter.save(dict(enabled=True, token='personal-test-token'))
        self.app.save_meta('exporter-account', dict(enabled=True, has_token=True, status='connected', revision='first',
            user_id='901', servers={'100':'服务器'}, channels={'100':{'200':'频道'}}))
        extra = copy.deepcopy(self.live.rows[0])
        extra.update(timestamp='2026-10-19T10:00:00+00:00',author=dict(id='903',username='昨日作者'))
        fetched = []
        class Client:
            def identity(self): return dict(user_id='901', username='本人')
            def check_channel(self, *_args): return None
            def message(self, channel, mid):
                fetched.append((channel,mid))
                return extra
        value=export_file(self.live.rows[1:])
        def run(binary, token, channel, start, end, output, **_options): output.write_text(json.dumps(value))
        with patch.object(self.app.exporter,'client',return_value=Client()), \
             patch('grouppulse.exporter.ensure_binary',return_value='/verified/exporter'), \
             patch('grouppulse.exporter.run_export',side_effect=run):
            item=self.app.exporter.prepare(dict(server='100',channel='200',date='2026-10-20'))
            self.assertEqual(fetched,[('200',extra['id'])])
            self.assertEqual(set(item['members']),{'901','902'})
            reader=self.app.file_imports.reader(item['id'])
            contexts=reader.contexts([reader.message(self.live.rows[2]['id'])],'200')
            self.assertEqual(contexts[0]['status'],'available')
            self.assertEqual(contexts[0]['message']['author_id'],'903')
            for row in value['messages']: row['reference']['channelId']='201'
            fetched.clear()
            second=self.app.exporter.prepare(dict(server='100',channel='200',date='2026-10-20'))
            self.assertEqual(fetched,[])
            self.assertEqual(second['message_count'],2)
