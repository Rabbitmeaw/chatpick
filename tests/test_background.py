import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from grouppulse import background


class BackgroundTests(unittest.TestCase):
    def test_workspace_lock_prevents_two_services_and_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'service.lock'
            first,second=background.ServiceLock(path),background.ServiceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.close()
            self.assertTrue(second.acquire())
            second.close()

    def test_same_named_foreign_login_job_is_never_overwritten_or_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'agent.plist'
            content=plistlib.dumps(dict(Label='another.application',WorkingDirectory='/not-this-workspace'))
            path.write_bytes(content)
            with patch.object(background,'agent_path',return_value=path):
                with self.assertRaises(ValueError): background.verify_agent(8765)
            self.assertEqual(path.read_bytes(),content)

    def test_foreground_stop_checks_command_and_working_directory_before_signalling(self):
        with patch.object(background.subprocess,'run',side_effect=[
            SimpleNamespace(stdout='python -m grouppulse\n'),SimpleNamespace(stdout='p123\nn/not-this-workspace\n')
        ]),patch.object(background.os,'kill') as signal:
            with self.assertRaises(ValueError): background.stop_foreground(dict(pid=123))
            signal.assert_not_called()

    def test_agent_preserves_runtime_and_never_embeds_credentials(self):
        value=background.specification()
        self.assertEqual(value['WorkingDirectory'],str(background.ROOT))
        self.assertTrue(value['KeepAlive'])
        self.assertTrue(value['RunAtLoad'])
        self.assertEqual(value['Umask'],0o077)
        self.assertEqual(value['EnvironmentVariables'],dict(CHATPICK_BACKGROUND='1',PYTHONUNBUFFERED='1'))
        self.assertNotIn('Token',plistlib.dumps(value).decode())
