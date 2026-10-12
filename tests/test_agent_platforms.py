"""Account routing, callback security, platform capability and Qt state regression."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QLineEdit
from agent_platforms import (DeepSeekSession, ServiceSession, PLATFORM_ORIGIN, INFERENCE_ORIGIN,
                             PlatformError, challenge, parse_draft, read_messages_stream, validated_browser_url)
from agent_prompt import AgentPromptClient
from openai_prompt import RESPONSES_URL, _response_text
from openai_prompt import PromptDraft
from request_records import compact_record
from secure_store import load_protected_json, save_protected_json
from studio_controller import StudioController
from platform_login_view import PlatformLoginDialog

DRAFT = {'analysis':'保留镜头节奏', 'shots':[{'time':'0-4秒','visual':'小猫','camera':'固定','motion':'挥手'}],
         'seedance_prompt':'小猫挥手', 'uncertainties':[], 'keyframe_handles':[]}


def stream_events(events):
    return io.BytesIO(b''.join(('data: '+json.dumps(event)+'\n\n').encode() for event in events))


class PlatformAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.session = DeepSeekSession(self.path/'deepseek.bin')

    def tearDown(self):
        self.temp.cleanup()

    def auth_flow(self, *, cancel_exchange=False, malicious=False):
        observed = {}
        def remote(path, *, body=None, token=''):
            if path.endswith('/auth_init'):
                observed['init'] = body
                return {'authorize_url': 'https://evil.example/dsh/authorize' if malicious else PLATFORM_ORIGIN+'/dsh/authorize',
                        'authorize_id':'attempt', 'expires_in':60}
            if path.endswith('/auth_exchange'):
                observed['exchange'] = body
                if cancel_exchange:
                    self.session.cancel_sign_in()
                return {'token':'synthetic-account-token', 'authorized_url':PLATFORM_ORIGIN+'/dsh/authorized',
                        'user':{'email':'test@example.invalid'}}
            if path.endswith('/auth_cancel'):
                observed['cancel'] = body
                return {}
            raise AssertionError(path)
        return observed, remote

    def test_pkce_callback_state_and_encrypted_persistence(self):
        observed, remote = self.auth_flow()
        with patch.object(self.session,'_platform',side_effect=remote):
            # Use a callback sender independent of the sign-in worker.
            def open_browser(url):
                import threading
                def deliver():
                    callback = observed['init']['redirect_uri']
                    for suffix in ('?state=wrong&code=bad','?state=x&state=y&code=bad', '?state=%E9%9D%9E%E6%B3%95&code=bad'):
                        try: urllib.request.urlopen(callback+suffix,timeout=2).close()
                        except urllib.error.HTTPError as e:
                            observed.setdefault('rejected',[]).append(e.code)
                            e.close()
                    urllib.request.urlopen(callback+'?'+urllib.parse.urlencode({'state':observed['init']['state'],'code':'auth-code'}),timeout=2).close()
                threading.Thread(target=deliver,daemon=True).start()
                return True
            result = self.session.sign_in(browser=open_browser,timeout=3)
        self.assertEqual(result,'test@example.invalid')
        self.assertEqual(observed['rejected'],[400,400,400])
        self.assertEqual(observed['init']['code_challenge'],challenge(observed['exchange']['code_verifier']))
        self.assertEqual(observed['init']['redirect_uri'],observed['exchange']['redirect_uri'])
        self.assertNotIn(b'synthetic-account-token',(self.path/'deepseek.bin').read_bytes())
        self.assertEqual(load_protected_json(self.path/'deepseek.bin')['issuer'],PLATFORM_ORIGIN)

    def test_cancelled_late_exchange_cannot_replace_existing_account(self):
        self.session.record = {'issuer':PLATFORM_ORIGIN,'token':'old-token','label':'原账号'}
        save_protected_json(self.session.file,self.session.record)
        observed, remote = self.auth_flow(cancel_exchange=True)
        def browser(url):
            import threading
            threading.Thread(target=lambda:urllib.request.urlopen(observed['init']['redirect_uri']+'?'+urllib.parse.urlencode({'state':observed['init']['state'],'code':'auth-code'}),timeout=2).close(),daemon=True).start()
            return True
        with patch.object(self.session,'_platform',side_effect=remote), self.assertRaises(PlatformError):
            self.session.sign_in(browser=browser,timeout=3)
        self.assertEqual(self.session.record['token'],'old-token')
        self.assertEqual(load_protected_json(self.session.file)['token'],'old-token')
        self.assertIn('cancel',observed)

    def test_untrusted_authorization_url_never_opens_browser(self):
        observed, remote = self.auth_flow(malicious=True)
        browser = Mock()
        with patch.object(self.session,'_platform',side_effect=remote), self.assertRaises(PlatformError):
            self.session.sign_in(browser=browser)
        browser.assert_not_called()
        self.assertFalse(self.session.active_account())

    def test_old_401_cannot_clear_replacement_login(self):
        self.session.record = {'issuer':PLATFORM_ORIGIN,'token':'new-token'}
        self.session.reject_token('old-token')
        self.assertTrue(self.session.active_account())
        self.session.reject_token('new-token')
        self.assertFalse(self.session.active_account())

    def test_frame_bytes_and_account_header_reach_deepseek_messages(self):
        self.session.record = {'issuer':PLATFORM_ORIGIN,'token':'account-secret'}
        request = {'model':'deepseek-flash', 'instructions':'遵守原要求', 'text':{'format':{'schema':{'type':'object'}}},
                   'input':[{'role':'user','content':[{'type':'input_text','text':'替换角色'}, {'type':'input_image','image_url':'data:image/jpeg;base64,YWJj'}]}]}
        events = [{'type':'message_start','message':{'usage':{'input_tokens':10}}},
                  {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                  {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':json.dumps(DRAFT)}},
                  {'type':'content_block_stop','index':0}, {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':5}}, {'type':'message_stop'}]
        response = stream_events(events)
        with patch('agent_platforms.open_request',return_value=response) as send:
            self.assertEqual(self.session.complete(request),DRAFT)
        args,kwargs = send.call_args
        self.assertEqual(args[0],INFERENCE_ORIGIN+'/v1/messages')
        self.assertEqual(kwargs['headers']['x-dsh-auth-token'],'account-secret')
        self.assertNotIn('Authorization',kwargs['headers'])
        self.assertEqual(kwargs['body']['messages'][0]['content'][1]['source']['data'],'YWJj')
        self.assertEqual(self.session.usage,{'input_tokens':10,'output_tokens':5})
        request['model'] = 'deepseek-v4-pro'
        with patch('agent_platforms.open_request') as send,self.assertRaises(PlatformError):
            self.session.complete(request)
        send.assert_not_called()

    def test_partial_stream_and_invalid_draft_are_rejected(self):
        with self.assertRaises(PlatformError):
            read_messages_stream(stream_events([{'type':'message_start','message':{}}, {'type':'message_stop'}]))
        with self.assertRaises(PlatformError):parse_draft({'seedance_prompt':'残缺方案'})
        self.assertEqual(parse_draft('```json\n'+json.dumps(DRAFT)+'\n```'),DRAFT)

    def test_service_login_uses_pkce_and_only_opaque_session_locally(self):
        session = ServiceSession('workbuddy',self.path/'wb.bin',{'service_url':'https://publisher.example'})
        calls = []
        def request(path, **kwargs):
            calls.append((path,kwargs))
            if path == 'metadata':return {'provider':'workbuddy','configured':True,'models':[{'slug':'assistant','vision':True}]}
            if path == 'auth/start':return {'attempt_id':'test-attempt','authorize_url':'https://publisher.example/login','expires_in':60}
            if path == 'auth/poll':return {'status':'authorized','session_token':'opaque-session','label':'WB账号'}
            raise AssertionError(path)
        with patch.object(session,'_request',side_effect=request),patch.object(session.cancelled,'wait',return_value=False):
            self.assertEqual(session.sign_in(browser=lambda url:True),'WB账号')
        self.assertEqual(calls[1][1]['body']['code_challenge'],challenge(calls[2][1]['body']['code_verifier']))
        record = load_protected_json(session.file)
        self.assertEqual(record['session_token'],'opaque-session')
        self.assertNotIn('refresh_token',record)
        self.assertNotIn('client_secret',record)
        self.assertNotIn(b'opaque-session',session.file.read_bytes())
        session.config['service_url']='https://other.example'
        self.assertFalse(session.active_account())

    def test_unconfigured_service_fails_before_any_network_or_browser(self):
        for provider in ('workbuddy','doubao_work'):
            session = ServiceSession(provider,self.path/(provider+'.bin'),{})
            with patch('agent_platforms.json_request') as request,self.assertRaisesRegex(PlatformError,'暂未开通'):
                session.sign_in(browser=Mock())
            request.assert_not_called()
        with self.assertRaises(PlatformError):validated_browser_url('https://evil.example/login','https://publisher.example')
        self.assertEqual(compact_record({'client_secret':'x','session_token':'y','code_verifier':'z','token':'q'}),
                         {'client_secret':'[已隐藏]','session_token':'[已隐藏]','code_verifier':'[已隐藏]','token':'[已隐藏]'})

    def test_account_adapter_never_falls_back_to_openai_paid_api(self):
        session = Mock();session.complete.return_value=DRAFT
        client = AgentPromptClient(session,'deepseek-flash')
        with patch('openai_prompt.urllib.request.build_opener') as paid:
            result = client._post(RESPONSES_URL,json.dumps({'model':'deepseek-flash'}).encode(),'application/json')
            self.assertEqual(json.loads(_response_text(result)),DRAFT)
        paid.assert_not_called()

    def test_service_adapter_forwards_frames_schema_and_audits_returned_usage(self):
        for provider in ('workbuddy','doubao_work'):
            session = ServiceSession(provider,self.path/(provider+'.bin'),{'service_url':'https://publisher.example'})
            session.record={'service_url':session.origin,'session_token':'session-token'}
            session.catalog=[{'slug':'vision-agent','vision':True}]
            request={'model':'vision-agent','instructions':'保留原意','input':[{'content':[{'type':'input_image','image_url':'data:image/jpeg;base64,YWJj'}]}], 'text':{'format':{'schema':{'type':'object'}}}}
            with patch.object(session,'_request',return_value={'draft':DRAFT,'usage':{'tokens':12}}) as send:
                self.assertEqual(session.complete(request),DRAFT)
            self.assertEqual(send.call_args.kwargs['body']['request'],request)
            self.assertTrue(send.call_args.kwargs['authorized'])
            self.assertEqual(session.usage,{'tokens':12})

    def test_http_error_retains_status_and_redacts_credential(self):
        from agent_platforms import json_request
        error=urllib.error.HTTPError(INFERENCE_ORIGIN+'/v1/messages',401,'Unauthorized',{},io.BytesIO(b'{"error":{"message":"bad account-secret","type":"unauthorized"}}'))
        with patch('agent_platforms.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(PlatformError) as caught:
                json_request(INFERENCE_ORIGIN+'/v1/messages',body={},secret_values=('account-secret',))
        self.assertEqual(caught.exception.diagnostics['http_status'],401)
        self.assertEqual(caught.exception.diagnostics['method'],'POST')
        self.assertNotIn('account-secret',json.dumps(caught.exception.diagnostics))


class PlatformUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_switching_and_login_dialog_preserve_draft_and_hide_registration_fields(self):
        with tempfile.TemporaryDirectory() as directory,patch('studio_controller.load_key',return_value=''),patch('studio_controller.load_openai_key',return_value=''):
            auth = Mock();auth.active_account.return_value={'email':'test@example.invalid'}
            auth.label.return_value='test@example.invalid'
            controller = StudioController(Path(directory),auth=auth)
            controller.state.update(prompt='保留我的提示词',brief='创作要求',openai_model='chatgpt-model')
            controller.models=[{'slug':'chatgpt-model'}]
            errors=[];controller.failed.connect(errors.append)
            dialog = PlatformLoginDialog(controller)
            dialog.show();self.application.processEvents()
            for provider in ('deepseek','workbuddy','doubao_work'):
                dialog.provider.setCurrentIndex(dialog.provider.findData(provider));self.application.processEvents()
                self.assertEqual(controller.state['prompt'],'保留我的提示词')
                self.assertEqual(controller.state['brief'],'创作要求')
                self.assertEqual(controller.state['openai_model'],'chatgpt-model')
                if provider != 'deepseek':
                    self.assertFalse(dialog.login_button.isEnabled())
                    with patch('agent_platforms.json_request') as network:controller.sign_in()
                    network.assert_not_called()
            self.assertFalse(dialog.findChildren(QLineEdit))
            self.assertIn('暂未开通',errors[-1])
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
            session.record={'issuer':PLATFORM_ORIGIN,'token':'synthetic-account-token'}
            controller.models=[{'slug':'deepseek-flash','vision':True}]
            def pump():
                deadline=time.monotonic()+3
                while controller.busy and time.monotonic()<deadline:
                    self.application.processEvents();time.sleep(.01)
                self.assertFalse(controller.busy)
            with patch('studio_controller.AgentPromptClient') as factory,patch('studio_controller.OpenAIPromptClient') as paid:
                factory.return_value.draft.return_value=PromptDraft('简短核对',[],'优化后的要求',[])
                controller.analyze();pump()
                factory.assert_called_once_with(session,'deepseek-flash',transcription_api_key='')
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
