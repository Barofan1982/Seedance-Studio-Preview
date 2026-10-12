"""Upload protocol, credential persistence and video validation without real network calls."""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from media_assets import MediaAsset
from secure_store import load_protected_json, save_protected_json
from video_upload import BASE_URL, MultipartVideo, UploadError, UploadSession, needs_upload, validate_video


class Response:
    def __init__(self, data, status=200):
        self.status = status
        self.headers = {'Content-Type':'application/json'}
        self.raw = json.dumps(data).encode()
    def read(self, _limit): return self.raw
    def __enter__(self): return self
    def __exit__(self, *_args): pass


class VideoUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root/'角色参考.mp4'
        self.video.write_bytes(b'local-video')
        self.clock = 1791600000
        self.tokens = {'accessToken':'access-secret','refreshToken':'refresh-secret',
                       'expires':(self.clock+86400)*1000,'username':'13100000000'}
        self.media = {'id':'opaque-media','url':'https://example.com/video.mp4?X-Tos-Signature=a%2Fb&X-Tos-Credential=x%2Fy',
                      'expires_at':'2026-10-17T03:20:00.000Z','duration':5,'filename':'角色参考.mp4'}
        self.probe = {'format':{'format_name':'mov,mp4,m4a,3gp,3g2,mj2','duration':'5'},
                      'streams':[{'codec_type':'video','codec_name':'h264','width':480,'height':854,'avg_frame_rate':'30/1'},
                                 {'codec_type':'audio','codec_name':'aac'}]}
    def tearDown(self): self.temp.cleanup()
    def session(self, responses):
        opener = Mock(); opener.open.side_effect = responses
        return UploadSession(self.root/'account.bin',opener=opener,clock=lambda:self.clock),opener
    def probe_patch(self):
        return patch('video_upload._run_media_tool',return_value=Mock(stdout=json.dumps(self.probe)))

    def test_login_encrypts_tokens_without_password_and_upload_preserves_signature(self):
        session,opener=self.session([Response({'data':self.tokens},201),Response({'status':200,'data':self.media})])
        session.login('13100000000','password-secret')
        persisted=load_protected_json(session.path)
        self.assertNotIn('password',persisted)
        self.assertEqual(persisted['refreshToken'],'refresh-secret')
        ciphertext=session.path.read_bytes()
        self.assertNotIn(b'password-secret',ciphertext);self.assertNotIn(b'access-secret',ciphertext)
        self.assertEqual(session.label(),'已登录：131****0000')
        progress=[]
        with self.probe_patch():result=session.upload(self.video,lambda sent,total:progress.append((sent,total)))
        self.assertEqual(result['url'],self.media['url'])
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,BASE_URL+'/media-upload/videos')
        self.assertEqual(request.headers['Authorization'],'Bearer access-secret')
        self.assertEqual(opener.open.call_args.kwargs['timeout'],300)
        self.assertIn('boundary=',request.headers['Content-type'])
        body=b''.join(request.data)
        self.assertEqual(len(body),int(request.headers['Content-length']))
        self.assertIn(b'name="file"',body);self.assertIn(self.video.name.encode(),body)
        self.assertIn(self.video.read_bytes(),body);self.assertNotIn(b'password-secret',body)
        self.assertEqual(progress[-1],(len(b'local-video'),len(b'local-video')))
        self.assertEqual(UploadSession(session.path).account()['username'],'13100000000')

    def test_upload_401_refreshes_all_tokens_and_retries_only_once(self):
        def unauthorized(): return urllib.error.HTTPError(BASE_URL,401,'Unauthorized',{},io.BytesIO(b'{}'))
        fresh={**self.tokens,'accessToken':'new-access','refreshToken':'new-refresh'}
        session,opener=self.session([unauthorized(),Response({'data':fresh}),Response({'data':self.media})])
        save_protected_json(session.path,self.tokens);session._account=load_protected_json(session.path)
        with self.probe_patch():session.upload(self.video)
        calls=opener.open.call_args_list
        self.assertEqual(len(calls),3)
        self.assertEqual(calls[1].args[0].full_url,BASE_URL+'/auth/refreshToken')
        self.assertEqual(json.loads(calls[1].args[0].data),{'refreshToken':'refresh-secret'})
        self.assertNotIn('Authorization',calls[1].args[0].headers)
        self.assertEqual(calls[2].args[0].headers['Authorization'],'Bearer new-access')
        self.assertEqual(load_protected_json(session.path)['refreshToken'],'new-refresh')
        # A second unauthorized upload never loops indefinitely.
        session,opener=self.session([unauthorized(),Response({'data':fresh}),unauthorized()])
        save_protected_json(session.path,self.tokens);session._account=load_protected_json(session.path)
        with self.probe_patch(),self.assertRaises(UploadError):session.upload(self.video)
        self.assertEqual(opener.open.call_count,3)

    def test_refresh_failure_clears_credentials_and_does_not_upload(self):
        bad=urllib.error.HTTPError(BASE_URL,401,'Unauthorized',{},io.BytesIO(b'{}'))
        session,opener=self.session([bad])
        session._account={**self.tokens,'expires':self.clock*1000-1}
        save_protected_json(session.path,session._account)
        with self.probe_patch(),self.assertRaisesRegex(UploadError,'重新登录'):session.upload(self.video)
        self.assertFalse(session.signed_in());self.assertFalse(session.path.exists())
        self.assertEqual(opener.open.call_count,1)

    def test_server_validation_detail_html_413_and_no_automatic_server_retry(self):
        for code,raw,expected in [(400,json.dumps({'message':'视频编码仅支持 H.264/AVC、H.265/HEVC'}).encode(),'视频编码'),
                                  (413,b'<html>too large</html>','200 MB'),(503,b'{}','配置异常')]:
            error=urllib.error.HTTPError(BASE_URL,code,'failure',{},io.BytesIO(raw))
            session,opener=self.session([error]);session._account=self.tokens
            with self.probe_patch(),self.assertRaisesRegex(UploadError,expected):session.upload(self.video)
            self.assertEqual(opener.open.call_count,1)

    def test_local_validation_checks_real_dimensions_codecs_and_frame_rate_before_network(self):
        with self.probe_patch():self.assertEqual(validate_video(self.video)['width'],480)
        cases=[('width',300,'总像素'),('avg_frame_rate','23/1','帧率'),('codec_name','vp9','视频编码')]
        for field,value,expected in cases:
            old=self.probe['streams'][0][field];self.probe['streams'][0][field]=value
            with self.probe_patch(),self.assertRaisesRegex(ValueError,expected):validate_video(self.video)
            self.probe['streams'][0][field]=old
        self.probe['streams'][1]['codec_name']='opus'
        session,opener=self.session([]);session._account=self.tokens
        with self.probe_patch(),self.assertRaisesRegex(ValueError,'音频编码'):session.upload(self.video)
        opener.open.assert_not_called()
        self.probe['streams'].pop()
        for duration in ('1.8','30.2'):
            self.probe['format']['duration']=duration
            with self.probe_patch():validate_video(self.video)

    def test_expired_urls_or_changed_local_files_need_reupload_but_plain_urls_stay_valid(self):
        stat=self.video.stat()
        asset=MediaAsset(self.video,'video','other','@{角色参考.mp4}',self.media['url'],5,True,
                         '2099-01-01T00:00:00Z','opaque',stat.st_size,stat.st_mtime_ns)
        self.assertFalse(needs_upload(asset))
        asset.source_expires_at='2000-01-01T00:00:00Z';self.assertTrue(needs_upload(asset))
        asset.source_expires_at='2099-01-01T00:00:00Z';self.video.write_bytes(b'changed-file')
        self.assertTrue(needs_upload(asset))
        asset.upload_id='';asset.source_expires_at='';self.assertFalse(needs_upload(asset))
        asset.source_url='';self.assertTrue(needs_upload(asset))
        asset.selected=False;self.assertFalse(needs_upload(asset))

    def test_transfer_and_waiting_timings_use_the_multipart_end_and_survive_timeout(self):
        tick=[10.0];events=[]
        session,opener=self.session([]);session._account=self.tokens
        def transmit(request,**_kwargs):
            for _chunk in request.data:tick[0]+=.25
            tick[0]+=120
            return Response({'data':self.media})
        opener.open.side_effect=transmit
        with self.probe_patch(),patch('video_upload.time.monotonic',side_effect=lambda:tick[0]):
            result=session.upload(self.video,on_event=events.append)
        trace=result['diagnostics']
        self.assertEqual(trace['connect_and_send_ms'],750)
        self.assertEqual(trace['waiting_response_ms'],120000)
        self.assertEqual(trace['elapsed_ms'],120750)
        self.assertEqual([event['phase'] for event in events],['validating','connecting_or_sending','waiting_response','reading_response'])
        def timeout(request,**_kwargs):
            for _chunk in request.data:tick[0]+=.25
            tick[0]+=300
            raise TimeoutError()
        opener.open.side_effect=timeout
        with self.probe_patch(),patch('video_upload.time.monotonic',side_effect=lambda:tick[0]),self.assertRaises(UploadError) as caught:
            session.upload(self.video)
        self.assertEqual(caught.exception.diagnostics['phase'],'waiting_response')
        self.assertEqual(caught.exception.diagnostics['waiting_response_ms'],300000)


if __name__ == '__main__': unittest.main()
