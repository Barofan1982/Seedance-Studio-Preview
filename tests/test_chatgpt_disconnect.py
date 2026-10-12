"""Keep connection failure evidence without retrying inference or exposing tokens."""
import http.client
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from openai_prompt import OpenAIPromptClient, OpenAIError
from error_diagnostics import call_report


class ChatGPTDisconnectTests(unittest.TestCase):
    def test_disconnect_retains_request_size_route_and_never_retries(self):
        client = OpenAIPromptClient('synthetic-oauth-token', chatgpt_plan=True)
        request = {'model':'test', 'input':[{'content':[
            {'type':'input_image', 'image_url':'data:image/jpeg;base64,YWJj'},
            {'type':'input_text', 'text':'原提示词'}]}], 'stream':True}
        opener = Mock(); opener.open.side_effect = http.client.RemoteDisconnected('Remote end closed connection without response')
        with patch('openai_prompt.urllib.request.build_opener', return_value=opener), \
             patch('openai_prompt.urllib.request.getproxies', return_value={'https':'http://name:private-password@127.0.0.1:18081'}), \
             patch('openai_prompt.urllib.request.proxy_bypass', return_value=False), \
             self.assertRaises(OpenAIError) as caught:
            client._stream_response(request)
        diag = caught.exception.diagnostics
        self.assertEqual(opener.open.call_count, 1)
        self.assertIsNone(diag['http_status'])
        self.assertEqual(diag['request_body_bytes'], len(json.dumps(request,ensure_ascii=False).encode('utf-8')))
        self.assertEqual(diag['input_image_count'], 1)
        self.assertEqual(diag['proxy_address'], '127.0.0.1:18081')
        self.assertNotIn('private-password', json.dumps(diag))
        self.assertNotIn('synthetic-oauth-token', json.dumps(diag))
        report = call_report({'operation':'draft','status':'failed','error':str(caught.exception),
                              'response_json':{'error_diagnostics':diag}})
        self.assertIn('ChatGPT 会员授权', report)
        self.assertIn('系统代理', report)
        self.assertNotIn('核对中转日志', report)
        self.assertNotIn('旧记录未保存 字节', report)
        self.assertIn('不会自动重发', report)

    def test_http_failure_also_retains_size_and_direct_route(self):
        client = OpenAIPromptClient('synthetic-oauth-token', chatgpt_plan=True)
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://api.openai.com/v1/responses', 503, 'unavailable',
                                                        {}, io.BytesIO(b'{"error":{"code":"unavailable"}}'))
        with patch('openai_prompt.urllib.request.build_opener', return_value=opener), \
             patch('openai_prompt.urllib.request.getproxies', return_value={}), self.assertRaises(OpenAIError) as caught:
            client._stream_response({'input':[], 'stream':True})
        self.assertEqual(caught.exception.diagnostics['http_status'], 503)
        self.assertGreater(caught.exception.diagnostics['request_body_bytes'], 0)
        self.assertEqual(caught.exception.diagnostics['connection_route'], '直连')
        self.assertEqual(opener.open.call_count, 1)
