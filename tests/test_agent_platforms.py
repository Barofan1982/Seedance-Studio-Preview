"""DeepSeek API setup, image transport, failure handling and Qt routing regression."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QLineEdit
from agent_platforms import DeepSeekApiSession, INFERENCE_ORIGIN, PlatformError, parse_draft, json_request
from agent_prompt import AgentPromptClient
from openai_prompt import RESPONSES_URL, _response_text, PromptDraft
from secure_store import load_protected_json, save_protected_json
from studio_controller import StudioController
from platform_login_view import PlatformLoginDialog

DRAFT = {'analysis':'保留镜头节奏', 'shots':[{'time':'0-4秒','visual':'小猫','camera':'固定','motion':'挥手'}],
         'seedance_prompt':'小猫挥手', 'uncertainties':[], 'keyframe_handles':[]}
MODELS = {'data': [{'id':'deepseek-flash'}, {'id':'deepseek-v4-pro'}]}


class PlatformApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name)
        self.session = DeepSeekApiSession(self.path / 'key.bin')

    def tearDown(self):
        self.temp.cleanup()

    def ready(self):
        self.session.record = {'api_key': 'synthetic-api-key', 'verified': True}
        save_protected_json(self.session.file, self.session.record)

    def request(self, model='deepseek-flash'):
        return {'model': model, 'instructions': '遵守原要求', 'text': {'format': {'schema': {'type':'object'}}},
                'input': [{'role':'user', 'content': [{'type':'input_text','text':'替换角色'},
                                                     {'type':'input_image','image_url':'data:image/jpeg;base64,YWJj'}]}]}

    def test_setup_only_reads_models_and_saves_encrypted_key(self):
        with patch('agent_platforms.json_request', return_value=MODELS) as send:
            models = self.session.configure_key('synthetic-api-key')
        self.assertEqual(send.call_args.args[0], INFERENCE_ORIGIN + '/models')
        self.assertNotIn('body', send.call_args.kwargs)
        self.assertEqual(send.call_args.kwargs['headers']['Authorization'], 'Bearer synthetic-api-key')
        self.assertTrue(self.session.active_account()); self.assertEqual(len(models), 2)
        self.assertNotIn(b'synthetic-api-key', self.session.file.read_bytes())
        self.assertTrue(DeepSeekApiSession(self.session.file).active_account())

    def test_invalid_replacement_and_cancelled_validation_preserve_working_key(self):
        self.ready()
        with patch('agent_platforms.json_request', side_effect=PlatformError('密钥无效', diagnostics={'http_status':401})), self.assertRaises(PlatformError):
            self.session.configure_key('bad-api-key')
        self.assertEqual(self.session.key(), 'synthetic-api-key')
        def cancel(*args, **kwargs):
            self.session.cancel_pending(); return MODELS
        with patch('agent_platforms.json_request', side_effect=cancel), self.assertRaises(PlatformError):
            self.session.configure_key('replacement-key')
        self.assertEqual(load_protected_json(self.session.file)['api_key'], 'synthetic-api-key')

    def test_invalid_key_and_unknown_model_list_do_not_commit(self):
        with patch('agent_platforms.json_request') as send, self.assertRaises(PlatformError):
            self.session.configure_key('bad key')
        send.assert_not_called()
        with patch('agent_platforms.json_request', return_value={'data':[{'id':'unknown-model'}]}), self.assertRaises(PlatformError):
            self.session.configure_key('synthetic-api-key')
        self.assertFalse(self.session.active_account())

    def test_401_clears_only_rejected_key_and_429_keeps_existing_key(self):
        self.ready(); self.session.reject_key('old-key'); self.assertTrue(self.session.active_account())
        with patch('agent_platforms.json_request', side_effect=PlatformError('限流', diagnostics={'http_status':429})), self.assertRaises(PlatformError):
            self.session.models()
        self.assertTrue(self.session.active_account())
        with patch('agent_platforms.json_request', side_effect=PlatformError('无效', diagnostics={'http_status':401})), self.assertRaises(PlatformError):
            self.session.models()
        self.assertFalse(self.session.active_account())

    def test_image_bytes_schema_and_api_header_reach_chat_completions(self):
        self.ready()
        result = {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(DRAFT)}}],
                  'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}
        with patch('agent_platforms.json_request', return_value=result) as send:
            self.assertEqual(self.session.complete(self.request()), DRAFT)
        self.assertEqual(send.call_args.args[0], INFERENCE_ORIGIN + '/chat/completions')
        body = send.call_args.kwargs['body']
        self.assertEqual(body['messages'][1]['content'][1]['image_url']['url'], 'data:image/jpeg;base64,YWJj')
        self.assertEqual(body['response_format'], {'type':'json_object'})
        self.assertIn('JSON', body['messages'][0]['content']); self.assertFalse(body['stream'])
        self.assertEqual(self.session.usage['total_tokens'], 15)
        self.assertNotIn('x-dsh-auth-token', send.call_args.kwargs['headers'])

    def test_text_model_blocks_images_before_paid_request(self):
        self.ready()
        with patch('agent_platforms.json_request') as send, self.assertRaises(PlatformError):
            self.session.complete(self.request('deepseek-v4-pro'))
        send.assert_not_called()

    def test_truncated_empty_and_invalid_drafts_are_rejected(self):
        self.ready()
        for choice in ({'finish_reason':'length','message':{'content':json.dumps(DRAFT)}},
                       {'finish_reason':'stop','message':{'content':''}}):
            with patch('agent_platforms.json_request', return_value={'choices':[choice]}), self.assertRaises(PlatformError):
                self.session.complete(self.request())
        with self.assertRaises(PlatformError): parse_draft({'seedance_prompt':'残缺方案'})
        self.assertEqual(parse_draft('```json\n'+json.dumps(DRAFT)+'\n```'), DRAFT)

    def test_blocked_setup_exposes_event_id_without_retry_or_key_commit(self):
        from error_diagnostics import possible_causes
        body = b'<html><h1>Request Blocked</h1><p>Event ID: test-event-12345678</p></html>'
        error = urllib.error.HTTPError(INFERENCE_ORIGIN + '/models',429,'Blocked',{'Content-Type':'text/html'},io.BytesIO(body))
        with patch('agent_platforms.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(PlatformError) as caught: self.session.configure_key('synthetic-api-key')
        self.assertEqual(opener.return_value.open.call_count, 1); self.assertFalse(self.session.active_account())
        trace = caught.exception.diagnostics
        self.assertTrue(trace['access_blocked']); self.assertEqual(trace['response_ids']['event_id'], 'test-event-12345678')
        self.assertEqual(trace['timeout_seconds'], 30); self.assertEqual(trace['request_body_bytes'], 0)
        self.assertIn('访问防护', possible_causes(message=trace['api_error_message'],status=429)[0])

    def test_http_diagnostics_redact_key_and_retain_api_status(self):
        error=urllib.error.HTTPError(INFERENCE_ORIGIN+'/models',401,'Unauthorized',{},io.BytesIO(b'{"error":{"message":"bad synthetic-api-key","type":"unauthorized"}}'))
        with patch('agent_platforms.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(PlatformError) as caught:
                json_request(INFERENCE_ORIGIN+'/models',secret_values=('synthetic-api-key',))
        self.assertEqual(caught.exception.diagnostics['http_status'],401)
        self.assertNotIn('synthetic-api-key',json.dumps(caught.exception.diagnostics))

    def test_api_adapter_never_falls_back_to_openai_paid_api(self):
        session=Mock();session.complete.return_value=DRAFT
        client=AgentPromptClient(session,'deepseek-flash')
        with patch('openai_prompt.urllib.request.build_opener') as paid:
            result=client._post(RESPONSES_URL,json.dumps({'model':'deepseek-flash'}).encode(),'application/json')
        self.assertEqual(json.loads(_response_text(result)),DRAFT);paid.assert_not_called()


class PlatformUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_api_dialog_validates_without_browser_and_does_not_log_key(self):
        import time
        with tempfile.TemporaryDirectory() as directory, patch('studio_controller.load_key', return_value=''), patch('studio_controller.load_openai_key', return_value=''):
            auth = Mock(); auth.active_account.return_value = None; auth.label.return_value = '尚未登录'
            controller = StudioController(Path(directory), auth=auth)
            controller.state.update(ai_provider='deepseek', prompt='保留要求')
            dialog = PlatformLoginDialog(controller); dialog.show(); self.application.processEvents()
            self.assertTrue(dialog.api_key.isVisible()); self.assertEqual(dialog.login_button.text(), '保存并校验')
            dialog.api_key.setText('synthetic-api-key')
            with patch('agent_platforms.json_request', return_value=MODELS) as send, patch('chatgpt_auth.webbrowser.open') as browser:
                dialog.login()
                deadline = time.monotonic() + 5
                while controller.busy and time.monotonic() < deadline:
                    self.application.processEvents(); time.sleep(.01)
                self.assertFalse(controller.busy)
                browser.assert_not_called()
                self.assertEqual(send.call_args.args[0], INFERENCE_ORIGIN + '/models')
            self.assertTrue(controller.analysis_account()); self.assertEqual(controller.state['prompt'], '保留要求')
            self.assertEqual(dialog.api_key.text(), '')
            call = controller.history.calls()[0]
            self.assertEqual(call['status'], 'succeeded'); self.assertEqual(call['operation'], 'configure_api')
            self.assertNotIn('synthetic-api-key', json.dumps(call))
            self.assertIn('单独计费', dialog.fees.text())
            dialog.close(); controller.close(); self.application.processEvents()

    def test_saved_unavailable_provider_falls_back_without_losing_draft(self):
        with tempfile.TemporaryDirectory() as directory, patch('studio_controller.load_key', return_value=''), patch('studio_controller.load_openai_key', return_value=''):
            saved = {'ai_provider': 'unavailable-provider',
                     'ai_models': {'unavailable-provider': 'old-model', 'deepseek': 'deepseek-flash'},
                     'prompt': '保留提示词', 'brief': '保留要求', 'resolution': '720p', 'duration': '8', 'ratio': '16:9'}
            (Path(directory) / 'state.json').write_text(json.dumps(saved), encoding='utf-8')
            auth = Mock(); auth.active_account.return_value = None; auth.label.return_value = '尚未登录'
            controller = StudioController(Path(directory), auth=auth)
            self.assertEqual(controller.state['ai_provider'], 'chatgpt')
            self.assertEqual(controller.state['ai_models'], {'deepseek': 'deepseek-flash'})
            for key in ('prompt', 'brief', 'resolution', 'duration', 'ratio'):
                self.assertEqual(controller.state[key], saved[key])
            self.assertEqual(set(controller.platform_accounts.sessions), {'deepseek'})
            controller.close(); self.application.processEvents()
            persisted = json.loads((Path(directory) / 'state.json').read_text(encoding='utf-8'))
            self.assertEqual(persisted['ai_provider'], 'chatgpt')
            self.assertEqual(persisted['ai_models'], {'deepseek': 'deepseek-flash'})


    def test_switching_and_login_dialog_preserve_draft_and_hide_registration_fields(self):
        with tempfile.TemporaryDirectory() as directory,patch('studio_controller.load_key',return_value=''),patch('studio_controller.load_openai_key',return_value=''):
            auth = Mock();auth.active_account.return_value={'email':'test@example.invalid'}
            auth.label.return_value='test@example.invalid'
            auth.models.return_value=[{'slug':'chatgpt-model'}]
            controller = StudioController(Path(directory),auth=auth)
            controller.state.update(prompt='保留我的提示词',brief='创作要求',openai_model='chatgpt-model')
            controller.models=[{'slug':'chatgpt-model'}]
            dialog = PlatformLoginDialog(controller)
            dialog.show();self.application.processEvents()
            self.assertEqual([dialog.provider.itemData(i) for i in range(dialog.provider.count())], ['chatgpt','deepseek'])
            for provider in ('deepseek','chatgpt'):
                dialog.provider.setCurrentIndex(dialog.provider.findData(provider));self.application.processEvents()
                self.assertEqual(controller.state['prompt'],'保留我的提示词')
                self.assertEqual(controller.state['brief'],'创作要求')
                self.assertEqual(controller.state['openai_model'],'chatgpt-model')
            self.assertEqual(dialog.findChildren(QLineEdit), [dialog.api_key])
            self.assertFalse(dialog.api_key.isVisible())
            self.assertEqual(dialog.api_key.echoMode(), QLineEdit.EchoMode.Password)
            for size in ((650,460),(550,420)):
                dialog.resize(*size);self.application.processEvents()
                self.assertTrue(dialog.rect().contains(dialog.login_button.geometry()))
                self.assertTrue(dialog.rect().contains(dialog.done_button.geometry()))
            dialog.close();controller.close();self.application.processEvents()

    def test_controller_routes_analysis_and_keeps_draft_after_failure(self):
        import time
        with tempfile.TemporaryDirectory() as directory,patch('studio_controller.load_key',return_value=''),patch('studio_controller.load_openai_key',return_value=''):
            auth=Mock();auth.active_account.return_value=None;auth.label.return_value='未连接'
            controller=StudioController(Path(directory),auth=auth)
            controller.state.update(ai_provider='deepseek', prompt='原始要求',brief='原始要求',mode='纯文本')
            session=controller.platform_accounts.sessions['deepseek']
            session.record={'api_key':'synthetic-api-key','verified':True}
            controller.models=[{'slug':'deepseek-flash','vision':True}]
            def pump():
                deadline=time.monotonic()+3
                while controller.busy and time.monotonic()<deadline:
                    self.application.processEvents();time.sleep(.01)
                self.assertFalse(controller.busy)
            with patch('studio_controller.AgentPromptClient') as factory,patch('studio_controller.OpenAIPromptClient') as paid:
                factory.return_value.draft.return_value=PromptDraft('简短核对',[],'优化后的要求',[])
                controller.analyze();pump()
                factory.assert_called_once_with(session,'deepseek-flash',transcription_api_key='',reasoning_effort='high')
                paid.assert_not_called()
                self.assertEqual(controller.state['prompt'],'优化后的要求')
                request=json.loads(controller.history.calls()[0]['request_json'])
                self.assertEqual(request['provider'],'deepseek')
                self.assertIn('DeepSeek',request['billing_source'])
                self.assertEqual(controller.state['prompt_draft']['ai_model'],'deepseek-flash')
                factory.return_value.draft.side_effect=PlatformError('模拟平台分析失败')
                failures=[];controller.failed.connect(failures.append)
                controller.analyze();pump()
                self.assertEqual(controller.state['prompt'],'优化后的要求')
                self.assertIn('模拟平台分析失败',failures[-1])
            controller.close();self.application.processEvents()


if __name__ == '__main__':unittest.main()
