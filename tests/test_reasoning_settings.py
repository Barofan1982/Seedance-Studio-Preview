"""Reasoning preferences must reach each real transport and survive model changes."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from reasoning_settings import supported_efforts, selected_effort
from studio_controller import StudioController
from platform_login_view import PlatformLoginDialog
from app import SettingsDialog
from openai_prompt import OpenAIPromptClient
from agent_prompt import AgentPromptClient
from agent_platforms import DeepSeekApiSession, PlatformError
from error_diagnostics import call_report

MODELS = [
    {'slug':'first-model', 'supported_reasoning_levels':[{'effort':v} for v in ('low','medium','high','xhigh','max')]},
    {'slug':'second-model', 'supported_reasoning_levels':[{'effort':'low'},{'effort':'high'}]},
    {'slug':'no-effort-model'},
]
DRAFT = {'analysis':'保留动作', 'shots':[], 'seedance_prompt':'猫咪挥手', 'uncertainties':[], 'keyframe_handles':[]}
RESPONSE = {'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(DRAFT)}]}]}


class ReasoningSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name)
        self.auth = Mock(); self.auth.active_account.return_value = {'email':'test@example.invalid'}
        self.auth.label.return_value = '测试账号'
        self.key_patch = patch('studio_controller.load_key', return_value=''); self.key_patch.start()
        self.audio_patch = patch('studio_controller.load_openai_key', return_value=''); self.audio_patch.start()
        self.controller = StudioController(self.path, auth=self.auth)
        self.controller.models = MODELS
        self.controller.set_analysis_model('first-model')

    def tearDown(self):
        self.controller.close(); self.application.processEvents()
        self.key_patch.stop(); self.audio_patch.stop(); self.temp.cleanup()

    def test_default_high_and_provider_model_preferences_survive_restart(self):
        controller = self.controller
        self.assertEqual(controller.analysis_reasoning_effort(), 'high')
        controller.set_analysis_reasoning_effort('medium')
        controller.set_analysis_model('second-model')
        self.assertEqual(controller.analysis_reasoning_effort(), 'high')
        controller.set_analysis_reasoning_effort('low')
        controller.set_analysis_model('first-model')
        self.assertEqual(controller.analysis_reasoning_effort(), 'medium')
        controller.state['ai_provider'] = 'deepseek'; controller.models = [{'slug':'deepseek-flash'}]
        self.assertEqual(controller.analysis_reasoning_effort(), 'high')
        controller.set_analysis_reasoning_effort('none')
        restarted = StudioController(self.path, auth=self.auth)
        restarted.models = [{'slug':'deepseek-flash'}]
        self.assertEqual(restarted.analysis_reasoning_effort(), 'none')
        restarted.state['ai_provider'] = 'chatgpt'; restarted.models = MODELS; restarted.set_analysis_model('first-model')
        self.assertEqual(restarted.analysis_reasoning_effort(), 'medium')
        restarted.close()

    def test_model_capability_limits_and_safe_fallback(self):
        self.controller.set_analysis_model('second-model')
        with self.assertRaises(ValueError): self.controller.set_analysis_reasoning_effort('max')
        self.controller.set_analysis_model('no-effort-model')
        self.assertEqual(self.controller.reasoning_options(), [])
        self.assertIsNone(self.controller.analysis_reasoning_effort())
        self.assertEqual(selected_effort('chatgpt', {'supported_reasoning_levels':['low','medium'], 'default_reasoning_level':'medium'}, 'max'), 'medium')
        self.assertEqual(supported_efforts('chatgpt', {'supported_reasoning_levels':[{'effort':{}}, 'unknown']}), [])

    def test_chatgpt_draft_transmits_the_exact_selected_effort(self):
        for effort in ('high','medium',None):
            with self.subTest(effort=effort), patch.object(OpenAIPromptClient, '_stream_response', return_value=RESPONSE) as send:
                client = OpenAIPromptClient('synthetic-oauth-token', 'first-model', chatgpt_plan=True, reasoning_effort=effort)
                client.draft(None, [], '猫咪挥手')
                body = send.call_args.args[0]
                if effort is None: self.assertNotIn('reasoning', body)
                else: self.assertEqual(body['reasoning'], {'effort':effort})
                self.assertNotIn('reasoning_effort', body)

    def test_deepseek_adapter_maps_thinking_off_and_effort_without_leaking_it_to_messages(self):
        session = DeepSeekApiSession(self.path / 'deepseek_api_key.bin')
        session.record = {'api_key':'synthetic-api-key', 'verified':True}
        response = {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(DRAFT)}}]}
        for effort in ('none','low','high','max'):
            with self.subTest(effort=effort), patch('agent_platforms.json_request', return_value=response) as send:
                client = AgentPromptClient(session,'deepseek-flash',reasoning_effort=effort)
                client.draft(None, [], '猫咪挥手')
                body = send.call_args.kwargs['body']
                self.assertEqual(body['thinking']['type'], 'disabled' if effort=='none' else 'enabled')
                if effort=='none': self.assertNotIn('reasoning_effort', body)
                else: self.assertEqual(body['reasoning_effort'], effort)
                self.assertNotIn('reasoning', body)
        request = {'model':'deepseek-flash','input':[], 'instructions':'JSON', 'text':{'format':{'schema':{}}}, 'reasoning':{'effort':'medium'}}
        with patch('agent_platforms.json_request') as send, self.assertRaises(PlatformError): session.complete(request)
        send.assert_not_called()

    def test_platform_dialog_is_adjustable_and_respects_busy_and_model_capability(self):
        dialog = PlatformLoginDialog(self.controller); dialog.show(); self.application.processEvents()
        self.assertEqual(dialog.reasoning.currentText(), '高')
        dialog.reasoning.setCurrentIndex(dialog.reasoning.findData('medium'))
        self.assertEqual(self.controller.analysis_reasoning_effort(), 'medium')
        dialog.model.setCurrentIndex(dialog.model.findData('second-model'))
        self.assertEqual(dialog.reasoning.currentData(), 'high')
        self.assertEqual(dialog.reasoning.count(), 2)
        self.controller.busy = True; dialog.refresh(); self.assertFalse(dialog.reasoning.isEnabled())
        self.controller.busy = False
        dialog.model.setCurrentIndex(dialog.model.findData('no-effort-model'))
        self.assertFalse(dialog.reasoning.isEnabled())
        self.assertTrue(dialog.rect().contains(dialog.done_button.geometry()))
        dialog.close(); self.application.processEvents()

    def test_settings_choose_new_model_and_save_matching_effort(self):
        dialog = SettingsDialog(self.controller); dialog.show(); self.application.processEvents()
        self.assertEqual(dialog.reasoning.currentData(), 'high')
        dialog.model.setCurrentIndex(dialog.model.findData('second-model'))
        dialog.reasoning.setCurrentIndex(dialog.reasoning.findData('low'))
        dialog.save()
        self.assertEqual(self.controller.analysis_model(), 'second-model')
        self.assertEqual(self.controller.analysis_reasoning_effort(), 'low')
        self.assertEqual(json.loads(self.controller.state_file.read_text(encoding='utf-8'))['ai_reasoning_efforts']['chatgpt']['second-model'], 'low')
        dialog.close(); self.application.processEvents()

    def test_call_report_displays_saved_effort_in_chinese(self):
        report = call_report({'operation':'draft','request_json':{'reasoning_effort':'high','auth_mode':'chatgpt_plan'}})
        self.assertIn('思考强度：高', report)


if __name__ == '__main__': unittest.main()
