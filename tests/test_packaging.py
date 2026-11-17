import os
import importlib.util
import errno
import signal
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

from grouppulse import background
from grouppulse.packaged import default_data_dir


class PackagingTests(unittest.TestCase):
    def test_data_directory_separates_mutable_files_from_bundled_resources(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'CHATPICK_DATA_DIR':folder}):
            self.assertEqual(background.runtime_path(),Path(folder).resolve())
            spec=background.specification(17865)
            self.assertEqual(Path(spec['StandardOutPath']).parent,Path(folder).resolve())
            self.assertEqual(spec['WorkingDirectory'],str(background.ROOT))
            self.assertIn('--data-dir',spec['ProgramArguments'])
            self.assertEqual(spec['ProgramArguments'][-1],str(Path(folder).resolve()))
            self.assertNotEqual(background.label(17865),f'com.chatpick.local.{background.WORKSPACE}.17865')

    def test_existing_source_checkout_keeps_its_original_login_job_identity(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(background.runtime_path(),background.ROOT/'runtime')
            self.assertEqual(background.label(8765),f'com.chatpick.local.{background.WORKSPACE}.8765')

    def test_frozen_background_runs_the_bundled_executable_instead_of_system_python(self):
        with patch.object(background.sys,'frozen',True,create=True), patch.object(background.sys,'executable','/Applications/拾聊 ChatPick.app/Contents/MacOS/ChatPick'):
            command=background.specification(17865)['ProgramArguments']
            self.assertEqual(command[:2],['/Applications/拾聊 ChatPick.app/Contents/MacOS/ChatPick','--serve'])
            self.assertNotIn('-m',command)
            self.assertNotIn('python',command[0].lower())

    def test_distributed_data_default_is_outside_the_application_bundle(self):
        with patch('grouppulse.packaged.Path.home',return_value=Path('/Users/example')):
            self.assertEqual(default_data_dir(),Path('/Users/example/Library/Application Support/ChatPick'))

    def test_release_audit_rejects_database_and_external_symlink(self):
        spec=importlib.util.spec_from_file_location('build_macos',background.ROOT/'scripts/build-macos.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'web').mkdir();(root/'web/index.html').write_text('仅静态资源')
            self.assertEqual(len(module.audit(root)),1)
            database=root/'live.sqlite3';database.write_text('真实记录禁止进入分发包')
            with self.assertRaisesRegex(ValueError,'运行数据'): module.audit(root)
            database.unlink();(root/'outside').symlink_to(background.ROOT)
            with self.assertRaisesRegex(ValueError,'外部符号链接'): module.audit(root)

    def test_cli_never_opens_another_data_directory_using_the_same_port(self):
        from grouppulse import __main__ as entry
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{},clear=True), \
                patch.object(entry,'App',return_value=MagicMock()),patch.object(entry,'ServiceLock',return_value=MagicMock()), \
                patch.object(entry,'probe',return_value=None),patch.object(entry,'urlopen') as request, \
                patch.object(entry,'create_server',side_effect=OSError(errno.EADDRINUSE,'busy')), \
                patch('sys.argv',['chatpick','--demo','--port','17865','--data-dir',folder]):
            with self.assertRaisesRegex(SystemExit,'端口被其他程序占用'): entry.main()
            request.assert_not_called()

    def test_intel_audit_rejects_arm_only_dependency_or_newer_system_requirement(self):
        spec=importlib.util.spec_from_file_location('build_macos',background.ROOT/'scripts/build-macos.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'dependency.so').write_bytes(b'\xcf\xfa\xed\xfe'+bytes(32))
            with patch.object(module.subprocess,'check_output',return_value='arm64\n'):
                with self.assertRaisesRegex(ValueError,'架构'): module.audit_native(root,'x86_64','13.0')
            with patch.object(module.subprocess,'check_output',side_effect=['x86_64\n','cmd LC_BUILD_VERSION\n minos 14.0\n sdk 26.0\n']):
                with self.assertRaisesRegex(ValueError,'系统'): module.audit_native(root,'x86_64','13.0')
            with patch.object(module.subprocess,'check_output',side_effect=['x86_64\n','cmd LC_VERSION_MIN_MACOSX\n version 10.13\n sdk 14.0\n']):
                value=module.audit_native(root,'x86_64','13.0')
                self.assertEqual(value[0]['minimum_macos'],'10.13')

    def test_owned_exited_process_waiting_for_parent_reaping_is_already_stopped(self):
        replies=[SimpleNamespace(stdout='python -m grouppulse\n'),
                 SimpleNamespace(stdout='p123\nn'+str(background.ROOT)+'\n'),SimpleNamespace(stdout='Z\n')]
        with patch.object(background.subprocess,'run',side_effect=replies),patch.object(background.os,'kill') as send, \
                patch.object(background.time,'sleep'):
            background.stop_foreground(dict(pid=123))
        self.assertEqual(send.call_args_list[0].args,(123,signal.SIGTERM))
        self.assertNotIn(signal.SIGKILL,[call.args[1] for call in send.call_args_list])


if __name__=='__main__': unittest.main()
