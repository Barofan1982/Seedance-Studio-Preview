"""Direct video creation must not wait for an optional analysis platform."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from app import StudioWindow
from studio_controller import StudioController
from openai_prompt import PromptDraft


class StartupIndependenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth = Mock()
        self.auth.active_account.return_value = {'subject':'synthetic-user'}
        self.auth.label.return_value = '测试连接'
        self.auth.access_token.return_value = 'synthetic-token'
        self.auth.models.return_value = [{'slug':'synthetic-model'}]
        with patch('studio_controller.load_key', return_value=''), patch('studio_controller.load_openai_key', return_value=''):
            self.controller = StudioController(Path(self.temp.name), auth=self.auth)
        self.controller.api_key = 'synthetic-seedance-token'
        self.controller.state.update(base_url='https://example.invalid/api/v3', mode='纯文本', prompt='小猫跑步', brief='小猫跑步',
                                     auto_analyze=False, duration='4', openai_model='synthetic-model')
        self.errors = []
        self.controller.failed.connect(self.errors.append)
        self.window = None

    def tearDown(self):
        if self.window:
            self.window.close()
        self.controller.close()
        self.application.processEvents()
        self.temp.cleanup()

    def wait(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(.01)
        self.assertTrue(condition())

    def test_startup_does_not_connect_analysis_platform_or_disable_generation(self):
        with patch.object(self.controller, 'resume') as resume:
            self.window = StudioWindow(self.controller)
            self.window.show()
            QTest.qWait(1100)
            self.auth.models.assert_not_called()
            self.auth.access_token.assert_not_called()
            resume.assert_called_once()
            self.assertFalse(self.controller.busy)
            self.assertTrue(self.window.generate_button.isEnabled())
            self.assertFalse(any(c['operation'] == 'models' for c in self.controller.history.calls()))

    def test_direct_submission_can_queue_without_chatgpt_while_old_task_query_is_pending(self):
        self.auth.active_account.return_value = None
        self.window = StudioWindow(self.controller, start_services=False)
        self.window.show()
        self.controller.query_busy = True
        with patch.object(self.controller, 'seedance_client', return_value=Mock()):
            self.window.generate()
        self.assertEqual(len(self.controller.queue.rows()), 1)
        row = self.controller.queue.rows()[0]
        self.assertEqual(row['status'], 'waiting')
        snapshot = self.controller.queue.get(row['queue_id'])['snapshot']
        self.assertEqual(snapshot['payload']['content'][0]['text'], '小猫跑步')
        self.auth.models.assert_not_called()
        self.auth.access_token.assert_not_called()
        self.auth.sign_in.assert_not_called()
        self.assertFalse(self.errors)
        self.controller.pause_queue()
        self.controller.query_busy = False

    def test_first_analysis_fetches_models_then_continues_the_same_optimization(self):
        with patch('studio_controller.OpenAIPromptClient') as factory:
            factory.return_value.draft.return_value = PromptDraft('猫咪跑步', [], '小猫向前跑步', [])
            self.controller.analyze()
            self.wait(lambda: not self.controller.busy and self.controller.state['prompt'] == '小猫向前跑步')
        self.auth.models.assert_called_once()
        factory.return_value.draft.assert_called_once()
        self.assertFalse(self.errors)
        self.assertEqual([c['operation'] for c in self.controller.history.calls()].count('models'), 1)

    def test_empty_model_list_does_not_retry_or_start_paid_analysis(self):
        self.auth.models.return_value = []
        with patch('studio_controller.OpenAIPromptClient') as factory:
            self.controller.analyze()
            self.wait(lambda: not self.controller.busy and bool(self.errors))
        self.auth.models.assert_called_once()
        factory.assert_not_called()
        self.assertIn('仍可直接生成视频', self.errors[0])
        self.assertEqual(self.controller.state['prompt'], '小猫跑步')


if __name__ == '__main__':
    unittest.main()
