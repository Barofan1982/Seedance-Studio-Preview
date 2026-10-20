"""No paid calls: truthful stages, read-only preview, storyboards, direct routing."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import http.client
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
from app import StudioWindow
from direct_network import DirectHTTPConnection, BeforeSendConnectionError, direct_handlers
from generation_status import generation_feedback
from media_assets import MediaAsset
from openai_prompt import OpenAIPromptClient, OpenAIError
from seedance_client import SeedanceClient, SeedanceError, SubmissionUncertain, build_payload
from storyboard_policy import validate_storyboard
from studio_controller import StudioController
from video_upload import UploadSession


class RefinementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        auth = Mock(); auth.active_account.return_value = None; auth.label.return_value = '未登录'
        with patch('studio_controller.load_key', return_value=''), patch('studio_controller.load_openai_key', return_value=''):
            self.controller = StudioController(self.path, auth=auth)
        self.controller.api_key = 'synthetic-key'
        self.controller.state.update(prompt='猫咪跑步', brief='猫咪跑步', mode='纯文本', duration='4', base_url='https://example.invalid/api/v3')
        self.window = None

    def tearDown(self):
        if self.window: self.window.close()
        self.controller.close(); self.app.processEvents(); self.temp.cleanup()

    def wait(self, predicate):
        until = time.monotonic() + 4
        while not predicate() and time.monotonic() < until:
            self.app.processEvents(); time.sleep(.01)
        self.assertTrue(predicate())

    def test_preview_stays_enabled_and_uses_frozen_request_during_submit(self):
        self.window = StudioWindow(self.controller, start_services=False); self.window.show()
        release = threading.Event()
        client = Mock()
        client.create_task.side_effect = lambda _payload: release.wait(4) and 'submitted-task'
        client.get_task.return_value = {'id':'submitted-task', 'status':'running'}
        try:
            with patch.object(self.controller, 'seedance_client', return_value=client):
                self.window.generate()
                self.app.processEvents()
                self.assertTrue(self.controller.busy)
                self.assertEqual(self.window.ai_button.text(), '优化提示词')
                self.assertTrue(self.window.preview_button.isEnabled())
                self.assertFalse(self.window.controls['duration'].isEnabled())
                with patch.object(self.window, 'show_dialog') as show, patch.object(self.controller, 'save') as save:
                    self.window.preview_request()
                    show.assert_called_once()
                    dialog = show.call_args.args[0]
                    self.assertIn('本次提交请求', dialog.windowTitle())
                    self.assertEqual(dialog.full_value['content'][0]['text'], '猫咪跑步')
                    save.assert_not_called()
                    dialog.close()
                release.set()
                self.wait(lambda: not self.controller.busy and not self.controller.query_busy)
                self.controller._poll.stop()
        finally:
            release.set()

    def test_id_receipt_is_not_presented_as_confirmed_server_queue(self):
        value = generation_feedback({'status':'succeeded','created_at':1}, {'status':'submitted','task_id':'known-id'}, {}, [], {}, now=5)
        self.assertEqual(value['title'], '已接单 · 待查询状态')
        self.assertNotIn('分钟', value['estimate'])
        self.assertNotIn('%', value['detail'])
        client = Mock(); client.create_task.return_value = 'known-id'
        with patch.object(self.controller, 'seedance_client', return_value=client), patch.object(self.controller, 'query'):
            self.controller.generate()
            self.wait(lambda: not self.controller.busy and self.controller.row() is not None)
        self.assertEqual(self.controller.row()['status'], 'submitted')
        self.assertIsNotNone(self.controller.pending_generation())

    def test_only_pre_send_connection_failures_retry_and_keep_response_timeout_separate(self):
        connection = DirectHTTPConnection('example.invalid', timeout=300)
        observed = []
        sock = Mock()
        def connect(instance):
            observed.append(instance.timeout)
            if len(observed) == 1: raise TimeoutError('connect timeout')
            instance.sock = sock
        with patch.object(http.client.HTTPConnection, 'connect', connect):
            connection.connect()
        self.assertEqual(observed, [30, 30])
        sock.settimeout.assert_called_once_with(300)
        connection.close()
        with patch.object(http.client.HTTPConnection, 'connect', side_effect=TimeoutError('connect timeout')) as connect:
            with self.assertRaises(BeforeSendConnectionError):
                DirectHTTPConnection('example.invalid', timeout=300).connect()
        self.assertEqual(connect.call_count, 2)

    def test_confirmed_not_sent_is_distinguished_from_ambiguous_post_and_never_reposts(self):
        client = SeedanceClient('https://example.invalid/api/v3', 'synthetic-key')
        payload = build_payload(prompt='猫', duration=4, resolution='480p', ratio='9:16')
        for reason, expected in ((BeforeSendConnectionError('not sent'), SeedanceError), (http.client.RemoteDisconnected('no response'), SubmissionUncertain)):
            opener = Mock(); opener.open.side_effect = urllib.error.URLError(reason)
            with patch('seedance_client.urllib.request.build_opener', return_value=opener):
                with self.assertRaises(expected) as raised: client.create_task(payload)
            self.assertEqual(opener.open.call_count, 1)
            self.assertIn('直连', raised.exception.diagnostics['connection_route'])
            if isinstance(reason, BeforeSendConnectionError):
                self.assertNotIsInstance(raised.exception, SubmissionUncertain)
                self.assertFalse(raised.exception.diagnostics['request_sent'])
        self.assertEqual(direct_handlers()[0].proxies, {})
        with patch('video_upload.urllib.request.build_opener') as build:
            UploadSession(self.path / 'upload.bin')
        self.assertEqual(build.call_args.args[0].proxies, {})

    def images_and_script(self, count=13):
        images = []
        for index in range(count):
            path = self.path / f'分镜{index + 1:02d}.png'; path.write_bytes(b'synthetic image')
            images.append(MediaAsset(path, 'image', 'storyboard', '@{' + path.name + '}'))
        shots = [{'time':f'{i}-{i+1}秒', 'visual':asset.handle + '，人物继续动作', 'camera':'固定镜头', 'motion':'动作衔接'} for i, asset in enumerate(images)]
        data = {'analysis':'按图编排', 'shots':shots, 'seedance_prompt':'\n'.join(s['time']+' '+s['visual'] for s in shots), 'uncertainties':[], 'keyframe_handles':[]}
        return images, data

    def test_all_thirteen_storyboard_images_reach_analysis_in_order_with_valid_timeline(self):
        images, data = self.images_and_script()
        response = {'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(data)}]}]}
        with patch.object(OpenAIPromptClient, '_stream_response', return_value=response) as send, patch('openai_prompt._scaled_jpeg_data_url', return_value='data:image/jpeg;base64,eA=='):
            draft = OpenAIPromptClient('synthetic-key', chatgpt_plan=True).draft(None, images, '按分镜完成动作', generation_settings={'duration':13}, reference_frames=[])
        content = send.call_args.args[0]['input'][0]['content']
        self.assertEqual(sum(item['type']=='input_image' for item in content), 13)
        self.assertIn('分镜 13：' + images[-1].handle, content[0]['text'])
        self.assertNotIn('前 12 张', content[0]['text'])
        self.assertEqual(draft.shots[-1]['time'], '12-13秒')
        for asset in images: self.assertIn(asset.handle, draft.prompt)

    def test_missing_image_bad_timing_and_inconsistent_final_text_are_rejected(self):
        images, data = self.images_and_script(2)
        validate_storyboard(data, images, {'duration':2})
        data['seedance_prompt'] = data['seedance_prompt'].replace(images[-1].handle, '')
        with self.assertRaisesRegex(ValueError, '未完整引用'): validate_storyboard(data, images, {'duration':2})
        images, data = self.images_and_script(2)
        data['shots'][-1]['time'] = '0-2秒'
        with self.assertRaisesRegex(ValueError, '空档、重叠'): validate_storyboard(data, images, {'duration':2})
        images, data = self.images_and_script(2)
        data['seedance_prompt'] = ' '.join(a.handle for a in images)
        with self.assertRaisesRegex(ValueError, '完整时间轴'): validate_storyboard(data, images, {'duration':2})
        data['seedance_prompt'] = data['seedance_prompt'].replace(images[1].handle, '')
        with self.assertRaisesRegex(ValueError, '未完整引用'): validate_storyboard(data, images, {'duration':-1})

    def test_query_backoff_pause_one_shot_and_reopen_never_resubmit(self):
        payload = build_payload(prompt='猫', duration=4, resolution='480p', ratio='9:16')
        self.controller.history.add('retry-task', payload, initial_status='submitted')
        self.controller.state['task_id'] = 'retry-task'
        client = Mock(); client.get_task.side_effect = SeedanceError('temporary HTTP 503', diagnostics={'http_status':503})
        self.window = StudioWindow(self.controller, start_services=False)
        self.window.show()
        with patch.object(self.controller, 'seedance_client', return_value=client):
            for attempt in range(1, 8):
                self.controller.query()
                self.wait(lambda: not self.controller.query_busy)
                self.assertFalse(self.controller.query_paused)
                self.assertTrue(self.controller._poll.isActive())
                expected = 180
                self.assertEqual(self.controller._poll.interval(), expected * 1000)
            self.assertIn('下次查询', self.window.result_info.text())
            self.window.continue_generation()
            self.assertTrue(self.controller.query_paused)
            self.assertFalse(self.controller._poll.isActive())
            previous = client.get_task.call_count
            self.controller.query(automatic=True)
            self.assertEqual(client.get_task.call_count, previous)
            self.controller.query(once=True)
            self.wait(lambda: not self.controller.query_busy)
            self.assertTrue(self.controller.query_paused)
            self.assertFalse(self.controller._poll.isActive())
            self.window.continue_generation()
            self.wait(lambda: not self.controller.query_busy)
            self.assertFalse(self.controller.query_paused)
            self.controller.pause_queries()
        client.create_task.assert_not_called()
        self.assertTrue(all(c['operation'] == 'query' for c in self.controller.history.calls()))

    def test_paused_inflight_query_never_restarts_polling_and_auth_errors_pause(self):
        payload = build_payload(prompt='猫', duration=4, resolution='480p', ratio='9:16')
        self.controller.history.add('inflight-task', payload)
        self.controller.state['task_id'] = 'inflight-task'
        gate = threading.Event()
        client = Mock()
        client.get_task.side_effect = lambda _task: gate.wait(4) and {'id':'inflight-task', 'status':'running'}
        try:
            with patch.object(self.controller, 'seedance_client', return_value=client):
                self.controller.query()
                self.assertTrue(self.controller.query_busy)
                self.controller.pause_queries()
                gate.set()
                self.wait(lambda: not self.controller.query_busy)
                self.assertFalse(self.controller._poll.isActive())
                client.get_task.side_effect = SeedanceError('HTTP 401', diagnostics={'http_status':401})
                self.controller.query()
                self.wait(lambda: not self.controller.query_busy)
                self.assertTrue(self.controller.query_paused)
                self.assertFalse(self.controller._poll.isActive())
            self.controller.resume(automatic=True)
            client.create_task.assert_not_called()
        finally:
            gate.set()


if __name__ == '__main__': unittest.main()
