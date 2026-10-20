import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch
from error_diagnostics import MAX_ERROR_BYTES, call_details, call_report, response_snapshot
from seedance_client import SeedanceClient, SeedanceError, SubmissionUncertain, build_payload


class Response:
    def __init__(self, raw, status=200):
        self.raw, self.status = raw, status
        self.headers = {'Content-Type':'application/json', 'x-request-id':'trace-test'}
    def read(self, limit): return self.raw[:limit]
    def __enter__(self): return self
    def __exit__(self, *_args): pass


class ErrorDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.client = SeedanceClient('https://example.com/api/v3', 'private-test-key')
        self.payload = build_payload(prompt='猫跑步',duration=4,resolution='480p',ratio='9:16')

    def request(self, response, method='POST'):
        opener=Mock()
        if isinstance(response, Exception): opener.open.side_effect=response
        else: opener.open.return_value=response
        with patch('seedance_client.urllib.request.build_opener',return_value=opener):
            try:
                result=self.client.create_task(self.payload) if method=='POST' else self.client.get_task('task')
                return result,opener
            except SeedanceError as error:
                return error,opener

    def test_large_http_error_retains_every_issue_code_and_request_id_without_secrets(self):
        message='Invalid parameter: '+('detail '*1200)+'LAST-ISSUE'
        raw=json.dumps({'error':{'code':'InvalidParameter.TaskTypeConstraint','message':message,
                               'issues':['resolution','duration'], 'request_id':'body-trace'},
                        'api_key':'private-test-key', 'password':'password-that-must-not-leak',
                        'note':'Fetch failed https://example.com/video.mp4?Signature=secret-signature'}).encode()
        failure=urllib.error.HTTPError('https://example.com',400,'Bad Request',{'x-request-id':'header-trace'},io.BytesIO(raw))
        error,opener=self.request(failure)
        self.assertIsInstance(error,SeedanceError)
        self.assertNotIsInstance(error,SubmissionUncertain)
        trace=error.diagnostics
        self.assertIn('LAST-ISSUE',trace['response_body'])
        self.assertEqual(trace['api_error_code'],'InvalidParameter.TaskTypeConstraint')
        self.assertEqual(trace['response_ids']['x-request-id'],'header-trace')
        self.assertEqual(trace['response_ids']['request_id'],'body-trace')
        self.assertTrue(trace['response_body_complete'])
        self.assertEqual(opener.open.call_args.kwargs['timeout'],600)
        self.assertEqual(json.loads(opener.open.call_args.args[0].data),self.payload)
        self.assertEqual(opener.open.call_count,1)
        for secret in ('private-test-key','password-that-must-not-leak','secret-signature'):
            self.assertNotIn(secret,json.dumps(trace))

    def test_missing_id_non_json_and_gateway_timeout_remain_unconfirmed_without_retry(self):
        cases=[Response(b'{}'),Response(b'<html>Gateway diagnostics LAST-LINE</html>'),
               urllib.error.HTTPError('https://example.com',504,'Gateway Timeout',{},io.BytesIO(b'{"error":{"code":"GatewayTimeout","message":"upstream waited 60s"}}'))]
        for response in cases:
            error,opener=self.request(response)
            self.assertIsInstance(error,SubmissionUncertain)
            self.assertTrue(error.diagnostics['response_body'])
            self.assertEqual(opener.open.call_count,1)
        self.assertEqual(error.diagnostics['http_status'],504)
        self.assertIn('服务端超时设置',call_details({'operation':'create','status':'unknown','response_json':{'network_diagnostics':error.diagnostics}})['causes'][0])

    def test_http_success_with_failed_task_is_cloud_failure_and_retains_business_code(self):
        task={'id':'task','status':'failed','error':{'code':'InputVideoSensitiveContentDetected','message':'Rejected video'}}
        result,opener=self.request(Response(json.dumps(task).encode()),'GET')
        self.assertEqual(result,task)
        self.assertEqual(opener.open.call_args.kwargs['timeout'],30)
        details=call_details({'operation':'query','status':'succeeded','response_json':{**task,'api_diagnostics':self.client.last_response_diagnostics}})
        self.assertEqual(details['stage'],'云端生成视频')
        self.assertTrue(details['issue'])
        self.assertEqual(details['http_status'],200)
        self.assertEqual(details['code'],'InputVideoSensitiveContentDetected')

    def test_invalid_query_response_retains_received_html_and_error_stage(self):
        error,opener=self.request(Response(b'<html>Proxy failed with detail</html>'),'GET')
        self.assertIsInstance(error,SeedanceError)
        self.assertNotIsInstance(error,SubmissionUncertain)
        self.assertIn('Proxy failed with detail',error.diagnostics['response_body'])
        self.assertEqual(error.diagnostics['phase'],'parsing_response')

    def test_oversize_body_is_explicitly_marked_and_legacy_timeout_does_not_invent_codes(self):
        trace=response_snapshot(b'x'*(MAX_ERROR_BYTES+1),status=502)
        self.assertFalse(trace['response_body_complete'])
        self.assertIn('2 MB',trace['response_body_note'])
        report=call_report({'call_id':191,'operation':'create','status':'unknown','error':'没有收到确认',
                            'duration_ms':120000,'response_json':{'network_diagnostics':{'phase':'opening_connection_or_sending_request','timeout_seconds':120,'reason':'timed out'}}})
        self.assertIn('120 秒',report)
        self.assertIn('未收到 / 未记录',report)
        self.assertIn('尚未确认',report)

    def test_remote_reset_is_not_explained_as_local_timeout(self):
        call = {'operation':'create', 'status':'unknown', 'error':'创建请求没有收到确认',
                'response_json':{'network_diagnostics':{'reason':'[WinError 10054] 远程主机强迫关闭了一个现有的连接。',
                                                      'timeout_seconds':300, 'elapsed_ms':61000}}}
        causes = call_details(call)['causes']
        self.assertIn('提前关闭', causes[0])
        self.assertNotIn('超过本地上限', ' '.join(causes))


if __name__=='__main__': unittest.main()
