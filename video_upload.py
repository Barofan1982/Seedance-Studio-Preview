"""Authenticated video uploads to the documented middle platform, with no Seedance credentials."""
from __future__ import annotations

import datetime
import json
import math
import threading
import time
import urllib.error
import urllib.request
import uuid
from fractions import Fraction
from pathlib import Path

from media_assets import _run_media_tool
from secure_store import load_protected_json, save_protected_json
from seedance_client import _public_https_url
from error_diagnostics import MAX_ERROR_BYTES, response_snapshot, safe_text

BASE_URL = 'https://prod-mpapi.qzzgame.com/api'
MAX_VIDEO_BYTES = 200 * 1024 * 1024


class UploadError(ValueError):
    def __init__(self, message, status=None, diagnostics=None):
        super().__init__(message)
        self.status = status
        self.diagnostics = diagnostics or {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def expiry_timestamp(value):
    try:
        date = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if date.tzinfo is None:
            return 0
        return date.timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


def source_expired(asset, now=None):
    return bool(asset.source_expires_at and expiry_timestamp(asset.source_expires_at) <= (time.time() if now is None else now) + 60)


def needs_upload(asset):
    if asset.kind != 'video' or not asset.selected or asset.purpose == 'competitor':
        return False
    if not asset.source_url or source_expired(asset):
        return True
    if asset.upload_id and asset.path:
        try:
            stat = asset.path.stat()
            return [stat.st_size, stat.st_mtime_ns] != [asset.upload_file_size, asset.upload_file_mtime_ns]
        except OSError:
            # The already-uploaded copy can still be used until its URL expires.
            return False
    return False


def validate_video(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError('本地视频不存在，请在素材菜单重新关联文件。')
    if path.suffix.lower() not in {'.mp4', '.mov'}:
        raise ValueError('上传视频只支持 MP4、MOV，请先转换格式。')
    size = path.stat().st_size
    if not 0 < size <= MAX_VIDEO_BYTES:
        raise ValueError('单个视频不能为空，且不能超过 200 MB。')
    probe = _run_media_tool(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration,format_name:stream=codec_type,codec_name,width,height,avg_frame_rate', '-of', 'json', str(path)])
    try:
        data = json.loads(probe.stdout)
        streams = data['streams']
        video = next(s for s in streams if s.get('codec_type') == 'video')
        container = str(data['format']['format_name']).split(',')
        duration = float(data['format']['duration'])
        width, height = int(video['width']), int(video['height'])
        fps = float(Fraction(video['avg_frame_rate']))
    except (KeyError, ValueError, TypeError, StopIteration, ZeroDivisionError):
        raise ValueError('无法读取视频的真实格式、尺寸、时长或帧率，请检查文件。') from None
    errors = []
    if not {'mov', 'mp4'} & set(container): errors.append('容器格式必须是 MP4 或 MOV')
    if video.get('codec_name') not in {'h264', 'hevc'}: errors.append('视频编码仅支持 H.264/AVC、H.265/HEVC')
    if any(s.get('codec_name') not in {'aac', 'mp3'} for s in streams if s.get('codec_type') == 'audio'):
        errors.append('音频编码仅支持 AAC、MP3，也可移除音轨')
    if not math.isfinite(duration) or not 1.8 <= duration <= 30.2: errors.append('视频时长须为 1.8～30.2 秒')
    if not 300 <= width <= 6000 or not 300 <= height <= 6000: errors.append('宽度和高度均须为 300～6000 像素')
    if height <= 0 or not .4 <= width / height <= 2.5: errors.append('宽高比须在 0.4～2.5 之间')
    if not 409600 <= width * height <= 8295044: errors.append('总像素须为 409,600～8,295,044，不能只看分辨率名称')
    if not 24 <= fps <= 60: errors.append('帧率须为 24～60 帧/秒')
    if errors: raise ValueError(path.name + '：\n' + '\n'.join('• ' + error for error in errors))
    return {'size_bytes': size, 'duration': duration, 'width': width, 'height': height, 'fps': fps,
            'video_codec': video['codec_name'], 'has_audio': any(s.get('codec_type') == 'audio' for s in streams)}


class MultipartVideo:
    """Stream the file with a generated boundary and a known Content-Length."""
    def __init__(self, path, on_progress=None):
        self.path = Path(path)
        self.size = self.path.stat().st_size
        self.boundary = 'SeedanceStudio' + uuid.uuid4().hex
        name = self.path.name.replace('"', '_').replace('\r', '_').replace('\n', '_').replace('\\', '_')
        media_type = 'video/quicktime' if self.path.suffix.lower() == '.mov' else 'video/mp4'
        self.head = (f'--{self.boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                     f'Content-Type: {media_type}\r\n\r\n').encode('utf-8')
        self.tail = f'\r\n--{self.boundary}--\r\n'.encode('ascii')
        self.on_progress = on_progress or (lambda _sent, _total: None)
        self.on_sent = lambda: None

    def __len__(self): return len(self.head) + self.size + len(self.tail)

    def __iter__(self):
        self.on_progress(0, self.size)
        yield self.head
        sent = 0
        with self.path.open('rb') as source:
            while chunk := source.read(256 * 1024):
                sent += len(chunk)
                yield chunk
                self.on_progress(sent, self.size)
        yield self.tail
        self.on_sent()


class UploadSession:
    def __init__(self, path, *, opener=None, clock=time.time):
        self.path = Path(path)
        from direct_network import direct_handlers
        self.opener = opener or urllib.request.build_opener(*direct_handlers(), _NoRedirect())
        self.clock = clock
        self._lock = threading.RLock()
        self._account = load_protected_json(self.path)

    def account(self):
        account = self._account
        return {'username': account.get('username', ''), 'nickname': account.get('nickname', '')}

    def signed_in(self):
        return bool(self._account.get('refreshToken'))

    def label(self):
        if not self.signed_in(): return '未登录中转账号'
        account = self.account()
        username = account['username']
        if username.isdigit() and len(username) == 11: username = username[:3] + '****' + username[-4:]
        return '已登录：' + (username or account['nickname'] or '中转账号')

    def _request(self, endpoint, *, value=None, body=None, token='', timeout=30, on_event=None):
        started = time.monotonic()
        sent_at = None
        headers_at = None
        phase = 'connecting_or_sending'
        report = on_event or (lambda _event: None)
        def diagnostic():
            now = time.monotonic()
            return {'phase':phase, 'method':'POST', 'endpoint':BASE_URL + endpoint, 'elapsed_ms':round((now-started)*1000),
                    'connect_and_send_ms':round(((sent_at if sent_at is not None else now)-started)*1000),
                    'waiting_response_ms':round(((headers_at or now)-sent_at)*1000) if sent_at is not None else None,
                    'timeout_seconds':timeout, 'connection_route':'直连（不使用系统代理）', 'connect_timeout_seconds':30}
        def sent():
            nonlocal sent_at, phase
            sent_at, phase = time.monotonic(), 'waiting_response'
            report({'phase':phase, 'diagnostics':diagnostic()})
        headers = {'Accept': 'application/json', 'User-Agent': 'SeedanceStudio/1.0'}
        if value is not None:
            body = json.dumps(value, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        elif isinstance(body, MultipartVideo):
            headers['Content-Type'] = 'multipart/form-data; boundary=' + body.boundary
            headers['Content-Length'] = str(len(body))
            body.on_sent = sent
        if token: headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(BASE_URL + endpoint, data=body, headers=headers, method='POST')
        secrets = (token, *(str(v) for v in (value or {}).values()),
                   self._account.get('accessToken',''), self._account.get('refreshToken',''))
        report({'phase':phase})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                headers_at, phase = time.monotonic(), 'reading_response'
                status = response.status
                response_headers = response.headers
                content_type = response.headers.get('Content-Type','').lower()
                raw = response.read(MAX_ERROR_BYTES + 1)
        except urllib.error.HTTPError as error:
            headers_at, phase = time.monotonic(), 'reading_response'
            status = error.code
            response_headers = error.headers
            content_type = (error.headers or {}).get('Content-Type','').lower()
            try: raw = error.read(MAX_ERROR_BYTES + 1)
            finally: error.close()
        except (TimeoutError, OSError, urllib.error.URLError) as error:
            stage = '等待中转响应' if sent_at is not None else '连接或发送视频'
            reason = getattr(error, 'reason', error)
            trace = {**diagnostic(), 'error_type':type(error).__name__, 'reason_type':type(reason).__name__,
                     'reason':safe_text(reason, secrets), 'errno':getattr(reason,'errno',None), 'winerror':getattr(reason,'winerror',None)}
            raise UploadError(f'{stage}未完成，请检查网络或联系中转维护人员；本地视频已保留。',diagnostics=trace) from error
        trace = {**diagnostic(),**response_snapshot(raw,response_headers,status,secrets=secrets)}
        report({'phase':phase,'diagnostics':trace})
        if len(raw) > MAX_ERROR_BYTES:
            raise UploadError('中转响应超过 2 MB，无法完整读取；请查看已保存的响应部分。',status,trace)
        try:
            parsed = json.loads(raw) if not content_type or 'json' in content_type else {}
        except (ValueError, UnicodeError):
            parsed = {}
        if not 200 <= status < 300:
            messages = {401:'中转登录已失效，请重新登录。',413:'视频超过上传大小限制，单个文件不得超过 200 MB。',
                429:'上传过于频繁，每分钟最多 10 次，请稍后重试。',502:'中转暂未能保存视频，请稍后重试。',
                503:'中转上传服务配置异常，请联系维护人员处理。'}
            detail = parsed.get('message') if isinstance(parsed, dict) else None
            if isinstance(detail, list): detail = '；'.join(str(item) for item in detail)
            # Do not echo credentials even if an upstream error mirrors its input.
            detail = str(detail or messages.get(status, '中转请求未完成，请查看接口服务状态。'))
            for secret in secrets:
                if secret: detail = detail.replace(secret, '[已隐藏]')
            raise UploadError(f'中转返回状态码 {status}：{detail}', status, trace)
        if not isinstance(parsed, dict) or not isinstance(parsed.get('data'), dict):
            raise UploadError('中转返回的数据不完整，请联系维护人员核对接口响应。',diagnostics=trace)
        if isinstance(body, MultipartVideo):
            self._last_upload_trace = trace
        return parsed['data']

    def _save_tokens(self, data):
        try:
            access = data['accessToken']; refresh = data['refreshToken']; expires = float(data['expires'])
            if not isinstance(access,str) or not access or not isinstance(refresh,str) or not refresh or not math.isfinite(expires) or expires <= self.clock() * 1000:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise UploadError('中转返回的登录凭据或到期时间无效，请重新登录。') from None
        account = {**self._account, **{k:data[k] for k in ('username','nickname') if k in data},
                   'accessToken':access, 'refreshToken':refresh, 'expires':expires}
        save_protected_json(self.path, account)
        self._account = account

    def login(self, username, password):
        if not str(username).strip() or not password: raise ValueError('请填写中转账号和密码。')
        with self._lock:
            data = self._request('/auth/login', value={'username':str(username).strip(),'password':password})
            self._save_tokens({**data,'username':data.get('username') or str(username).strip()})
            return self.account()

    def logout(self):
        with self._lock:
            self.path.unlink(missing_ok=True)
            self._account = {}

    def _refresh(self):
        refresh = self._account.get('refreshToken')
        if not refresh: raise UploadError('请先登录中转账号，再上传本地视频。',401)
        try:
            self._save_tokens(self._request('/auth/refreshToken', value={'refreshToken':refresh}))
        except (UploadError, OSError):
            self.logout()
            raise UploadError('中转登录已失效，刷新未成功，请重新登录后上传。',401) from None

    def upload(self, path, on_progress=None, on_event=None):
        path = Path(path)
        report = on_event or (lambda _event: None)
        report({'phase':'validating'})
        info = validate_video(path)
        with self._lock:
            if not self.signed_in(): raise UploadError('请先登录中转账号，再上传本地视频。',401)
            if self._account.get('expires',0) <= (self.clock()+60)*1000:
                report({'phase':'refreshing_account'})
                self._refresh()
            for attempt in range(2):
                try:
                    data = self._request('/media-upload/videos', body=MultipartVideo(path,on_progress),
                                         token=self._account['accessToken'],timeout=300,on_event=report)
                    break
                except UploadError as error:
                    if error.status != 401: raise
                    if attempt:
                        self.logout()
                        raise UploadError('中转刷新后仍未通过验证，请重新登录后上传。',401) from None
                    report({'phase':'refreshing_account'})
                    self._refresh()
        url = data.get('url')
        if not isinstance(url,str): raise UploadError('上传响应缺少视频地址，请联系维护人员。')
        _public_https_url(url,'中转视频地址')
        if expiry_timestamp(data.get('expires_at')) <= self.clock()+60:
            raise UploadError('上传接口返回的视频地址已过期或缺少有效期，请联系维护人员。')
        if not isinstance(data.get('id'),str) or not data['id']:
            raise UploadError('上传响应缺少媒体编号，请联系维护人员。')
        # Return only media metadata, never account tokens or password.
        return {'id':data['id'],'url':url,'expires_at':data['expires_at'],
                'filename':path.name,'size_bytes':info['size_bytes'],'duration':info['duration'],
                'diagnostics':getattr(self,'_last_upload_trace',{})}
