"""Portable directory isolation and startup logging failures."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_paths import data_directory, output_directory, open_error_log, TECHNICAL_CONTACT


class PortableStartupTests(unittest.TestCase):
    def test_frozen_uses_executable_not_appdata_or_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / '同学测试' / 'Seedance Studio.exe'
            self.assertEqual(data_directory(frozen=True, executable=exe, appdata='ignored'), exe.parent / 'data')
            self.assertEqual(output_directory(frozen=True, executable=exe), exe.parent / '成片')
            self.assertEqual(data_directory(frozen=False, appdata=folder), Path(folder) / 'SeedanceVideoStudioPreview')

    def test_locked_log_uses_sibling_without_losing_data(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / 'logs' / 'error.log').mkdir(parents=True)
            sentinel = directory / 'state.json'; sentinel.write_text('keep')
            original = Path.open
            def denied(path, *args, **kwargs):
                if path == directory / 'logs' / 'error.log':
                    raise PermissionError(13, 'synthetic denial')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'open', denied):
                stream, target = open_error_log(directory)
            with stream: stream.write('diagnostic')
            self.assertEqual(target.parent, directory / 'logs')
            self.assertEqual(target.read_text(), 'diagnostic')
            self.assertEqual(sentinel.read_text(), 'keep')

    def test_no_log_destination_returns_without_crashing(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError(13, 'denied')):
            self.assertEqual(open_error_log(Path('blocked')), (None, None))

    def test_normal_startup_survives_blocked_default_log(self):
        import app
        import secure_store
        from studio_controller import StudioController
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / 'logs' / 'error.log').mkdir(parents=True)
            with patch.object(app, 'APP_DIR', directory), \
                 patch.object(secure_store, 'KEY_FILE', directory / 'api_key.bin'), \
                 patch.object(secure_store, 'OPENAI_KEY_FILE', directory / 'openai_api_key.bin'), \
                 patch.object(app, 'StudioController', side_effect=lambda: StudioController(directory)), \
                 patch.object(app.QMessageBox, 'critical') as message:
                self.assertEqual(app.main(login_platform='invalid', start_services=False, quit_after_ms=50), 0)
            message.assert_not_called()

    def test_unwritable_data_has_chinese_message_not_unhandled_exception(self):
        import app
        with tempfile.TemporaryDirectory() as folder:
            blocked = Path(folder) / 'data'
            blocked.write_text('blocked directory')
            with patch.object(app, 'APP_DIR', blocked), patch.object(app.QMessageBox, 'critical') as message:
                self.assertEqual(app.main(start_services=False), 1)
            self.assertIn('数据目录', message.call_args.args[2])

    def test_contact_is_current(self):
        self.assertIn('吴仕敏', TECHNICAL_CONTACT)
        self.assertIn('Token', TECHNICAL_CONTACT)


if __name__ == '__main__':
    unittest.main()
