import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from grouppulse import network


class NetworkTests(unittest.TestCase):
    def test_model_wait_uses_longer_default_and_explicit_caller_budget(self):
        from grouppulse.live import LIVE_CONFIG
        self.assertEqual(LIVE_CONFIG['timeout'],300)
        config=dict(LIVE_CONFIG,base_url='https://example.invalid/v1',model='test-model')
        model=network.ChatModel(lambda:config,lambda:'')
        response=dict(choices=[dict(message=dict(content='完成'),finish_reason='stop')])
        with patch.object(network,'http_json',return_value=response) as http:
            self.assertEqual(model.complete([dict(role='user',content='检查等待值')]),'完成')
            self.assertEqual(http.call_args.args[3],300)
            model.complete_tools([dict(role='user',content='原引擎独立预算')],[],timeout=120_000)
            self.assertEqual(http.call_args.args[3],120)
            config['timeout']=900
            model.complete([dict(role='user',content='用户选择更长等待')])
            self.assertEqual(http.call_args.args[3],900)
        config['retries']=5
        with patch.object(network,'http_json',side_effect=network.ServiceError('等待超时','connection')) as http:
            with self.assertRaises(network.ServiceError):model.complete([dict(role='user',content='不自动重试')])
            http.assert_called_once()

    def test_long_model_wait_is_saved_restored_and_validated(self):
        from grouppulse.live import LiveApp
        vault=SimpleNamespace(storage='test-memory',get=lambda _key:'')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'live.sqlite3'
            app=LiveApp(path,vault=vault)
            self.assertEqual(app.get_config()['timeout'],300)
            app.save_config(dict(base_url='https://example.invalid/v1',model='test-model',timeout=900))
            self.assertEqual(LiveApp(path,vault=vault).get_config()['timeout'],900)
            for value in (0,1801,2.5,True):
                with self.assertRaises(ValueError):app.save_config(dict(timeout=value))

    def test_missing_python_roots_are_loaded_without_disabling_verification(self):
        empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(empty.cert_store_stats()['x509_ca'], 0)
        with patch.object(network.ssl, 'create_default_context', return_value=empty), \
             patch.object(network.ssl, 'get_default_verify_paths', return_value=SimpleNamespace(cafile=None, capath=None)):
            context = network.tls_context()
        self.assertGreater(context.cert_store_stats()['x509_ca'], 0)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_existing_trust_configuration_is_preserved(self):
        configured = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.object(network.ssl, 'create_default_context', return_value=configured), \
             patch.object(network.ssl, 'get_default_verify_paths', return_value=SimpleNamespace(cafile=None, capath='/configured/roots')):
            self.assertIs(network.tls_context(), configured)
        self.assertEqual(configured.cert_store_stats()['x509_ca'], 0)
        self.assertEqual(configured.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(configured.check_hostname)

    def test_certificate_failures_are_classified_without_secret_or_retry(self):
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                failure = ssl.SSLCertVerificationError(1, 'private-credential-in-diagnostic')
                opener = MagicMock()
                opener.open.side_effect = URLError(failure) if wrapped else failure
                with patch.object(network, 'build_opener', return_value=opener):
                    with self.assertRaises(network.ServiceError) as caught:
                        network.http_json('https://example.invalid/api', {'Authorization':'Bot test-only-secret'})
                self.assertEqual(caught.exception.code, 'certificate')
                self.assertIn('证书', str(caught.exception))
                self.assertNotIn('private-credential', str(caught.exception))
                self.assertNotIn('test-only-secret', str(caught.exception))
                opener.open.assert_called_once()
