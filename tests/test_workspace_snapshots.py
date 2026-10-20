"""Data-loss and confirmation checks for clear/restore; no network calls."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import json
import datetime
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QImage
from app import StudioWindow
from media_assets import MediaAsset
from studio_controller import StudioController, manifest
from workspace_snapshot_view import WorkspaceSnapshotDialog
from video_upload import needs_upload


class WorkspaceSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.auth = Mock()
        self.auth.active_account.return_value = None
        self.auth.label.return_value = '未登录'
        self.controller = self.make_controller()
        self.errors = []
        self.controller.failed.connect(self.errors.append)
        self.window = None

    def make_controller(self):
        with patch('studio_controller.load_key', return_value=''), patch('studio_controller.load_openai_key', return_value=''):
            return StudioController(self.directory / 'profile', auth=self.auth)

    def tearDown(self):
        if self.window:
            self.window.close()
        self.controller.close()
        self.application.processEvents()
        self.temp.cleanup()

    def wait(self):
        deadline = time.monotonic() + 6
        while self.controller.busy and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(.01)
        self.application.processEvents()
        self.assertFalse(self.controller.busy)

    def sample(self):
        image_path = self.directory / '人物.png'
        image = QImage(160, 240, QImage.Format.Format_RGB32)
        image.fill(0xff00bb88)
        image.save(str(image_path))
        video_path = self.directory / '参考.mp4'
        video_path.write_bytes(b'synthetic video bytes')
        audio_path = self.directory / '声音.mp3'
        audio_path.write_bytes(b'synthetic audio bytes')
        self.controller.assets = [MediaAsset(video_path, 'video', 'other', '@{参考.mp4}', duration=5),
                                  MediaAsset(image_path, 'image', 'character', '@{人物.png}', selected=False),
                                  MediaAsset(audio_path, 'audio', 'audio', '@{声音.mp3}')]
        self.controller.state.update(prompt='参考 @{参考.mp4}，保留动作', brief='原始要求',
                                     resolution='480p', ratio='9:16', duration='-1',
                                     analysis='原分析', iteration_task_id='parent-task', review_output=True)
        return [video_path, image_path, audio_path]

    def test_clear_restart_restore_retains_bytes_selection_prompt_parameters_and_running_queue(self):
        sources = self.sample()
        original_bytes = [path.read_bytes() for path in sources]
        self.controller.state['task_id'] = 'running-task'
        self.controller.history.add('running-task', {'model':'test', 'content':[], 'duration':5, 'resolution':'480p', 'ratio':'9:16'})
        queued = self.controller.queue.add(self.controller.generation_snapshot({'model':'test', 'content':[], 'duration':5}))
        old_queue = self.controller.queue.get(queued)
        self.controller.state['api_key'] = 'must-never-be-in-snapshot'
        self.controller.state['base_url'] = 'https://example.invalid/private-endpoint'
        self.controller.save()
        self.assertTrue(self.controller.clear_workspace())
        self.wait()
        self.assertFalse(self.errors)
        self.assertFalse(self.controller.workspace_has_content())
        self.assertEqual(self.controller.state['resolution'], '480p')
        self.assertEqual(self.controller.state['task_id'], 'running-task')
        self.assertEqual(self.controller.queue.get(queued), old_queue)
        records = self.controller.workspace_snapshots.rows()
        self.assertEqual(len(records), 1)
        self.assertNotIn('api_key', records[0]['state'])
        self.assertNotIn('base_url', records[0]['state'])
        for path in sources:
            path.unlink()
        self.controller.close()
        self.controller = self.make_controller()
        self.controller.failed.connect(self.errors.append)
        self.assertTrue(self.controller.restore_workspace(records[0]['snapshot_id']))
        self.wait()
        self.assertFalse(self.errors)
        self.assertEqual([a.path.read_bytes() for a in self.controller.assets], original_bytes)
        self.assertEqual([a.selected for a in self.controller.assets], [True, False, True])
        self.assertEqual(self.controller.assets[1].filename, '人物.png')
        self.assertEqual(self.controller.state['prompt'], '参考 @{参考.mp4}，保留动作')
        self.assertEqual(self.controller.state['brief'], '原始要求')
        self.assertEqual(self.controller.state['duration'], '-1')
        self.assertEqual(self.controller.state['task_id'], 'running-task')
        self.assertEqual(self.controller.queue.get(queued), old_queue)
        self.assertFalse(any(c['operation'] == 'create' for c in self.controller.history.calls()))

    def test_empty_and_cancel_do_not_create_snapshots_and_confirmation_defaults_to_no(self):
        self.window = StudioWindow(self.controller, start_services=False)
        with patch.object(QMessageBox, 'information') as info, patch.object(QMessageBox, 'question') as confirm:
            self.window.clear_workspace()
        info.assert_called_once()
        confirm.assert_not_called()
        self.assertEqual(self.controller.workspace_snapshots.rows(), [])
        self.controller.state.update(prompt='未保存的提示词', brief='未保存的提示词')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No) as confirm:
            self.window.clear_workspace()
        self.assertEqual(confirm.call_args.args[-1], QMessageBox.StandardButton.No)
        self.assertEqual(self.controller.state['prompt'], '未保存的提示词')
        self.assertEqual(self.controller.workspace_snapshots.rows(), [])
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            self.window.clear_workspace()
        self.wait()
        self.assertEqual(len(self.controller.workspace_snapshots.rows()), 1)
        self.assertFalse(self.controller.workspace_has_content())
        self.window.resize(1280, 800)
        self.window.show()
        self.application.processEvents()
        self.assertTrue(self.window.rect().contains(self.window.clear_workspace_button.mapTo(self.window, self.window.clear_workspace_button.rect().bottomRight())))

    def test_restoring_preserves_replaced_current_work_and_saves_again_deduplicating_media(self):
        self.sample()
        self.controller.clear_workspace()
        self.wait()
        identity = self.controller.workspace_snapshots.rows()[0]['snapshot_id']
        self.controller.state.update(prompt='第二个创作，不可丢失', brief='第二个创作，不可丢失')
        self.controller.restore_workspace(identity)
        self.wait()
        records = self.controller.workspace_snapshots.rows()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['reason'], '恢复前保存')
        self.assertEqual(records[0]['state']['prompt'], '第二个创作，不可丢失')
        media = self.controller.workspace_snapshots.directory / 'media'
        self.assertEqual(sum(p.is_file() for p in media.rglob('*')), 3)
        self.controller.clear_workspace()
        self.wait()
        self.assertEqual(sum(p.is_file() for p in media.rglob('*')), 3)

    def test_backup_failure_and_state_write_failure_never_clear_the_live_workspace(self):
        self.sample()
        before = deepcopy(self.controller.state), manifest(self.controller.assets)
        with patch.object(self.controller.workspace_snapshots, '_copy_media', side_effect=OSError('磁盘空间不足')):
            self.controller.clear_workspace()
            self.wait()
        self.assertEqual((self.controller.state, manifest(self.controller.assets)), before)
        self.assertEqual(self.controller.workspace_snapshots.rows(), [])
        self.assertIn('磁盘空间不足', self.errors[-1])
        with patch.object(self.controller, 'save', side_effect=OSError('资料文件不可写')):
            self.controller.clear_workspace()
            self.wait()
        self.assertEqual((self.controller.state, manifest(self.controller.assets)), before)
        self.assertEqual(len(self.controller.workspace_snapshots.rows()), 1)
        self.assertIn('资料文件不可写', self.errors[-1])

    def test_missing_media_blocks_clear_but_image_library_can_rescue_a_deleted_original(self):
        sources = self.sample()
        self.controller.remember_images(self.controller.assets)
        sources[1].unlink()
        self.controller.clear_workspace()
        self.wait()
        self.assertFalse(self.errors)
        self.assertTrue(Path(self.controller.workspace_snapshots.rows()[0]['assets'][1]['path']).is_file())
        self.controller.assets = [MediaAsset(self.directory / '丢失.mp4', 'video', 'other', '@{丢失.mp4}')]
        self.controller.state['prompt'] = '视频不能丢'
        self.controller.clear_workspace()
        self.wait()
        self.assertTrue(self.controller.workspace_has_content())
        self.assertIn('素材文件缺失', self.errors[-1])

    def test_url_expiry_encryption_integrity_and_uploaded_video_metadata(self):
        paths = self.sample()
        stat = paths[0].stat()
        video = self.controller.assets[0]
        video.source_url = 'https://example.invalid/upload.mp4?signature=private-value'
        video.upload_id = 'synthetic-upload'
        video.source_expires_at = datetime.datetime.fromtimestamp(time.time() + 3600, datetime.timezone.utc).isoformat()
        video.upload_file_size, video.upload_file_mtime_ns = stat.st_size, stat.st_mtime_ns
        store = self.controller.workspace_snapshots
        identity = store.save(self.controller.state, manifest(self.controller.assets))
        record = store.get(identity)
        self.assertEqual(record['assets'][0]['original_path'], str(paths[0]))
        from studio_controller import load_assets
        self.assertFalse(needs_upload(load_assets(record['assets'])[0]))
        self.assertNotIn(b'private-value', store.path.read_bytes())
        self.assertNotIn('must-never-be-in-snapshot', json.dumps(record))
        Path(record['assets'][0]['path']).write_bytes(b'corrupt backup')
        with self.assertRaisesRegex(ValueError, '副本已变化'):
            store.warnings(record, verify=True)
        expired = manifest([video])
        expired[0].update(path='', source_expires_at='2000-01-01T00:00:00+00:00')
        with self.assertRaisesRegex(ValueError, '已过期'):
            store.save(self.controller.state, expired)
        with store.connect() as connection:
            connection.execute('UPDATE snapshots SET contents=? WHERE snapshot_id=?', (b'corrupt', identity))
        self.assertIn('无法读取', store.rows()[0]['error'])

    def test_snapshot_browser_search_and_restore_confirmation(self):
        self.controller.state.update(prompt='第一份无素材', brief='第一份无素材')
        self.controller.clear_workspace()
        self.wait()
        self.window = StudioWindow(self.controller, start_services=False)
        self.assertTrue(hasattr(self.window.workbench_history, 'snapshot_button'))
        dialog = WorkspaceSnapshotDialog(self.controller, self.window.restore_workspace, self.window)
        dialog.show()
        self.application.processEvents()
        self.assertEqual(dialog.table.rowCount(), 1)
        dialog.search.setText('找不到的提示词')
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertFalse(dialog.restore_button.isEnabled())
        dialog.search.clear()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            dialog.restore_selected()
        self.assertFalse(self.controller.workspace_has_content())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            dialog.restore_selected()
        self.wait()
        self.assertEqual(self.controller.state['prompt'], '第一份无素材')
        dialog.close()

    def test_restore_preserves_valid_analysis_without_certifying_stale_analysis(self):
        from video_analysis import analysis_signature, needs_video_analysis
        sources = self.sample()
        self.controller.state.update(auto_analyze=True, frame_catalog=[{
            'path':str(sources[1]), 'handle':'@{分析画面.png}', 'source_handle':'@{参考.mp4}', 'timestamp':0}], prompt_draft={
            'frame_count':1,
            'analysis_signature': analysis_signature(self.controller.assets), 'source_assets': manifest(self.controller.assets)})
        self.controller.clear_workspace()
        self.wait()
        record = self.controller.workspace_snapshots.rows()[0]
        self.assertTrue(record['analysis_current'])
        self.controller.restore_workspace(record['snapshot_id'])
        self.wait()
        self.assertFalse(needs_video_analysis(self.controller.state, self.controller.assets))
        self.controller.state['prompt_draft']['analysis_signature'] = 'stale-analysis'
        self.controller.clear_workspace()
        self.wait()
        stale = self.controller.workspace_snapshots.rows()[0]
        self.assertFalse(stale['analysis_current'])
        self.controller.restore_workspace(stale['snapshot_id'])
        self.wait()
        self.assertTrue(needs_video_analysis(self.controller.state, self.controller.assets))


if __name__ == '__main__':
    unittest.main()
