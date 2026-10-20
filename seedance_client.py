"""Small standard-library client for Sub2API's Seedance video tasks."""

from __future__ import annotations

import ipaddress
import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from request_records import compact_record
from error_diagnostics import MAX_ERROR_BYTES, error_fields, response_snapshot
from direct_network import direct_handlers, BeforeSendConnectionError, CONNECT_TIMEOUT_SECONDS


DEFAULT_BASE_URL = "http://43.153.91.218:8088/api/v3"
CREATE_TIMEOUT_SECONDS = 180
RESOLUTIONS = ("480p", "720p", "1080p")
RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive")
MODES = ("reference", "first_frame", "first_last_frame", "text")
TASK_TYPES = ("reference", "edit", "extend")
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
MODEL_PRICES_USD_PER_MILLION = {
    "seedance-2.5-480p-720p-no-video": 10.0,
    "seedance-2.5-480p-720p-with-video": 6.0,
    "seedance-2.5-1080p-no-video": 11.0,
    "seedance-2.5-1080p-with-video": 46.0 / 7.0,
}


class SeedanceError(Exception):
    """An error safe to display to the user."""

    def __init__(self, message: str, *, diagnostics: dict | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class SubmissionUncertain(SeedanceError):
    """The create request may have reached the server; do not resubmit blindly."""

    def __init__(self, message: str, *, diagnostics: dict | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class _NoApiRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class _HttpsVideoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        newurl = urllib.parse.urljoin(request.full_url, newurl)
        _public_https_url(newurl, "重定向后的视频链接")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def model_alias(resolution: str, has_video_reference: bool) -> str:
    if resolution not in RESOLUTIONS:
        raise ValueError("分辨率只能选择 480p、720p 或 1080p。")
    tier = "1080p" if resolution == "1080p" else "480p-720p"
    reference = "with-video" if has_video_reference else "no-video"
    return f"seedance-2.5-{tier}-{reference}"


def _public_https_url(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label}必须是可公开访问的安全网址（以 https:// 开头）。")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError(f"{label}不能指向本机。")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError(f"{label}不能指向内网地址。")
    return value


def _image_source(value: str) -> str:
    if value.startswith("data:image/"):
        if not re.match(r"^data:image/(png|jpeg|webp|bmp|tiff|gif);base64,[A-Za-z0-9+/=]+$", value):
            raise ValueError("本地图片编码格式不正确。")
        return value
    return _public_https_url(value, "参考图片")


def _video_source(value: str) -> str:
    return _public_https_url(value, "参考视频")


def _audio_source(value: str) -> str:
    if value.startswith("data:audio/"):
        if not re.fullmatch(r"data:audio/(wav|mpeg|mp3);base64,[A-Za-z0-9+/=]+", value):
            raise ValueError("本地音频编码格式不正确，仅支持 WAV/MP3。")
        return value
    return _public_https_url(value, "参考音频")


def build_payload(
    *,
    prompt: str,
    duration: int,
    resolution: str,
    ratio: str,
    image_url: str = "",
    video_url: str = "",
    image_sources: list[str] | None = None,
    video_sources: list[str] | None = None,
    audio_sources: list[str] | None = None,
    mode: str = "reference",
    task_type: str = "reference",
    first_frame: str = "",
    last_frame: str = "",
    generate_audio: bool = True,
    watermark: bool = False,
    output_format: str = "mp4",
    return_last_frame: bool = False,
    seed: int | None = None,
    camera_fixed: bool | None = None,
) -> dict:
    prompt = prompt.strip()
    if mode not in MODES:
        raise ValueError("生成方式无效。")
    if task_type not in TASK_TYPES:
        raise ValueError("全模态任务类型无效。")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration != -1 and not 4 <= duration <= 30:
        raise ValueError("Seedance 2.5 时长必须是 4–30 秒，或选择自适应时长（传入 -1）。")
    if ratio not in RATIOS:
        raise ValueError("请选择有效的画面比例。")
    if not isinstance(generate_audio, bool) or not isinstance(watermark, bool) or not isinstance(return_last_frame, bool):
        raise ValueError("声音、水印和返回尾帧参数必须是开关值。")
    if output_format not in ("mp4", "mov"):
        raise ValueError("输出格式只能是 MP4 或 MOV。")
    if seed is not None or camera_fixed is not None:
        raise ValueError("随机种子和固定镜头仅支持 Seedance 1.0，Seedance 2.5 不接受这两个参数。")
    images = list(image_sources or [])
    videos = list(video_sources or [])
    audios = list(audio_sources or [])
    if image_url.strip():
        images.append(image_url)
    if video_url.strip():
        videos.append(video_url)
    images = [_image_source(source) for source in images]
    videos = [_video_source(source) for source in videos]
    audios = [_audio_source(source) for source in audios]
    if len(images) > 30 or len(videos) > 10 or len(audios) > 10 or len(images) + len(videos) + len(audios) > 50:
        raise ValueError("Seedance 2.5 最多支持 30 张图片、10 段视频、10 段音频，总计 50 个参考素材。")
    if mode != "reference" and task_type != "reference":
        raise ValueError("视频编辑和延长仅适用于全模态参考方式。")
    if mode in ("first_frame", "first_last_frame"):
        if images or videos or audios:
            raise ValueError("首帧/首尾帧方式不能混入全模态参考图片、视频或音频。")
        if not first_frame:
            raise ValueError("请选择首帧图片。")
        if mode == "first_last_frame" and not last_frame:
            raise ValueError("首尾帧方式还需选择尾帧图片。")
        if mode == "first_frame" and last_frame:
            raise ValueError("首帧方式不能包含尾帧图片。")
        if ratio != "adaptive":
            raise ValueError("首帧/首尾帧方式的画面比例必须选择自适应。")
    elif first_frame or last_frame:
        raise ValueError("首帧与尾帧图片只用于首帧/首尾帧方式。")
    if mode == "text" and (images or videos or audios):
        raise ValueError("纯文本方式不能附带参考素材。")
    if mode == "text" and not prompt:
        raise ValueError("文生视频必须填写提示词。")
    if mode == "reference" and not (prompt or images or videos or audios):
        raise ValueError("全模态参考方式至少需要提示词或一份参考素材。")
    if task_type in ("edit", "extend"):
        if not videos:
            raise ValueError("视频编辑/延长需要至少一个可访问的视频网址。")
        if ratio != "adaptive":
            raise ValueError("视频编辑/延长的画面比例必须选择自适应。")
    if task_type == "edit" and duration != -1:
        raise ValueError("Seedance 2.5 视频编辑的时长必须是 -1。")
    content: list[dict] = []
    if prompt:
        content.append({"type": "text", "text": prompt})
    if mode == "reference":
        for source in images:
            content.append({"type": "image_url", "image_url": {"url": source}, "role": "reference_image"})
        for source in videos:
            content.append({"type": "video_url", "video_url": {"url": source}, "role": "reference_video"})
        for source in audios:
            content.append({"type": "audio_url", "audio_url": {"url": source}, "role": "reference_audio"})
    elif mode in ("first_frame", "first_last_frame"):
        content.append({"type": "image_url", "image_url": {"url": _image_source(first_frame)}, "role": "first_frame"})
        if last_frame:
            content.append({"type": "image_url", "image_url": {"url": _image_source(last_frame)}, "role": "last_frame"})
    result = {
        "model": model_alias(resolution, bool(videos)),
        "content": content,
        "duration": duration,
        "resolution": resolution,
        "ratio": ratio,
        "generate_audio": generate_audio,
        "watermark": watermark,
        "output_format": output_format,
        "return_last_frame": return_last_frame,
    }
    if mode == "reference" and (images or videos or audios):
        result["omni_reference_task_type"] = task_type
    return result


class SeedanceClient:
    def __init__(self, base_url: str, api_key: str, *, allow_http: bool = False):
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        parsed = urllib.parse.urlsplit(self.base_url)
        if not self.api_key:
            raise ValueError("请填写接口密钥。")
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("接口地址必须是有效的网络地址。")
        if parsed.path.rstrip("/") != "/api/v3" or parsed.query or parsed.fragment:
            raise ValueError("接口地址应以 /api/v3 结尾。")
        if parsed.scheme == "http" and not allow_http:
            raise ValueError("当前连接会明文传输密钥；请先确认继续使用该地址。")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if data is not None and len(data) > 64 * 1024 * 1024:
            raise ValueError("请求体超过 64 MB，请减少或压缩参考图片。")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        started = time.monotonic()
        phase = "opening_connection_or_sending_request"
        http_status = None
        response_ids = {}
        timeout = CREATE_TIMEOUT_SECONDS if method == "POST" else 30
        raw = b''
        def diagnostic(**extra):
            return compact_record({
                'method': method, 'endpoint': self.base_url + path, 'phase': phase,
                'http_status': http_status, 'response_ids': response_ids, 'timeout_seconds': timeout,
                'elapsed_ms': round((time.monotonic() - started) * 1000),
                'request_body_bytes': len(data) if data is not None else 0, **extra,
                'connection_route': '直连（不使用系统代理）', 'connect_timeout_seconds': CONNECT_TIMEOUT_SECONDS,
            }, (self.api_key,))
        try:
            # The API key and JSON body must reach the configured gateway unchanged.
            # urllib otherwise inherits machine HTTP proxies, some of which rewrite POST bodies.
            with urllib.request.build_opener(*direct_handlers(), _NoApiRedirects()).open(request, timeout=timeout) as response:
                phase = "reading_response"
                http_status = response.status
                response_ids = {name: response.headers[name] for name in ("x-request-id", "x-tt-logid", "request-id")
                                if response.headers.get(name)}
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as error:
            http_status = error.code
            phase = 'http_response'
            complete = True
            try:
                raw = error.read(MAX_ERROR_BYTES + 1)
            except (OSError, http.client.HTTPException) as read_error:
                raw = getattr(read_error, 'partial', b'')
                complete = False
            finally:
                error.close()
            snapshot = response_snapshot(raw, error.headers, http_status, secrets=(self.api_key,), complete=complete)
            trace = diagnostic(**snapshot)
            detail = _error_detail(raw)
            if error.code == 503 and "No eligible Seedance accounts" in detail:
                requested_model = str(payload.get("model", "")) if isinstance(payload, dict) else ""
                detail += (
                    "。中转站没有可调度的 Seedance 上游账号。请在 Sub2API 后台检查："
                    "当前 Key 所属分组是否关联 Seedance 账号、账号是否启用且可调度、"
                    "是否开启 Seedance (Ark) 端点能力，以及模型别名映射"
                    + (f"（本次：{requested_model}）" if requested_model else "")
                    + "。需由中转站管理员修复。"
                )
            message = f"接口返回 HTTP {error.code}：{detail}".replace(self.api_key, "[已隐藏]")
            if method == 'POST' and error.code in {408, 504}:
                raise SubmissionUncertain(message + '。中转返回超时，是否接单尚未确认。', diagnostics=trace) from None
            raise SeedanceError(message, diagnostics=trace) from None
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError, http.client.HTTPException) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            trace = diagnostic(
                error_type=type(error).__name__, reason_type=type(reason).__name__, reason=str(reason),
                errno=getattr(reason, 'errno', None), winerror=getattr(reason, 'winerror', None),
            )
            if isinstance(reason, BeforeSendConnectionError):
                trace.update(phase='connecting', request_sent=False, connect_attempts=2)
                raise SeedanceError('直连建立连接失败，已重试一次；生成请求尚未发送，可以重试。', diagnostics=trace) from error
            if getattr(error, 'partial', b''):
                trace.update(response_snapshot(error.partial, status=http_status, secrets=(self.api_key,), complete=False))
                trace['response_ids'] = response_ids
            if method == "POST":
                raise SubmissionUncertain(
                    "创建请求没有收到确认，服务器可能已经接单。请勿直接重复提交，以免重复收费。",
                    diagnostics=trace,
                ) from error
            raise SeedanceError(f"网络请求失败：{reason}".replace(self.api_key, '[已隐藏]'), diagnostics=trace) from error
        snapshot = response_snapshot(raw, response.headers, http_status, secrets=(self.api_key,))
        phase = 'parsing_response'
        trace = diagnostic(**snapshot)
        self.last_response_diagnostics = trace
        def bad_response(message):
            error_class = SubmissionUncertain if method == 'POST' else SeedanceError
            return error_class(message, diagnostics=trace)
        if len(raw) > 2_000_000:
            raise bad_response("接口响应超过 2 MB，无法完整读取；请查看已保存的响应部分。")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise bad_response("接口返回的内容不是有效的结构化数据。") from error
        if not isinstance(result, dict):
            raise bad_response("接口返回格式不正确。")
        code, message = error_fields(result)
        if not (method == 'GET' and result.get('status') == 'failed') and (result.get('error') or code and code.lower() not in {'0', '200', 'ok', 'success'}):
            # HTTP success can still carry a business rejection.
            trace['phase'] = 'business_response'
            raise SeedanceError(f'接口返回业务错误 {code}：{message}'.replace(self.api_key, '[已隐藏]'), diagnostics=trace)
        self.last_response_diagnostics = trace
        return result

    def create_task(self, payload: dict) -> str:
        result = self._request("POST", "/contents/generations/tasks", payload)
        task_id = result.get("id")
        if not isinstance(task_id, str) or not task_id:
            trace = {**getattr(self, 'last_response_diagnostics', {}), 'phase': 'checking_task_id'}
            raise SubmissionUncertain("创建响应缺少任务编号，是否接单尚未确认。", diagnostics=trace)
        return task_id

    def get_task(self, task_id: str) -> dict:
        task_id = task_id.strip()
        if not task_id or "/" in task_id or "?" in task_id or "#" in task_id:
            raise ValueError("任务编号无效。")
        quoted = urllib.parse.quote(task_id, safe="")
        return self._request("GET", f"/contents/generations/tasks/{quoted}")


def _error_detail(raw: bytes) -> str:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return "请求失败，请检查配置和额度"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "请求失败")[:300]
        if isinstance(error, str):
            return error[:300]
        return str(data.get("message") or data.get("msg") or data.get("detail") or "请求失败")[:300]
    return "请求失败"


def estimated_cost_usd(task: dict, model: str) -> float | None:
    tokens = task.get("usage", {}).get("completion_tokens") if isinstance(task.get("usage"), dict) else None
    price = MODEL_PRICES_USD_PER_MILLION.get(model)
    if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and price is not None:
        return tokens / 1_000_000 * price
    return None


def download_video(url: str, destination: Path) -> None:
    url = _public_https_url(url, "生成的视频链接")
    destination = Path(destination)
    temporary = destination.with_name(destination.name + ".part")
    started = time.monotonic()
    phase = 'opening_connection_or_sending_request'
    http_status = None
    response_headers = {}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "SeedanceVideoGenerator/1.0"})
        with urllib.request.build_opener(_HttpsVideoRedirects()).open(request, timeout=60) as response:
            http_status, response_headers, phase = response.status, response.headers, 'reading_response'
            _public_https_url(response.geturl(), "重定向后的视频链接")
            phase = 'writing_output'
            with temporary.open("wb") as output:
                while True:
                    phase = 'reading_response'
                    chunk = response.read(1024 * 1024)
                    if not chunk: break
                    phase = 'writing_output'
                    output.write(chunk)
        phase = 'finalizing_file'
        temporary.replace(destination)
    except Exception as error:
        temporary.unlink(missing_ok=True)
        if isinstance(error, ValueError): raise
        trace = {'method':'GET', 'endpoint':compact_record(url), 'phase':phase, 'http_status':http_status,
                 'timeout_seconds':60, 'elapsed_ms':round((time.monotonic()-started)*1000),
                 'error_type':type(error).__name__, 'reason':str(error), 'errno':getattr(error,'errno',None)}
        if isinstance(error, urllib.error.HTTPError):
            try: raw = error.read(MAX_ERROR_BYTES + 1)
            except (OSError, http.client.HTTPException): raw = b''
            finally: error.close()
            trace.update(response_snapshot(raw,error.headers,error.code))
        else:
            trace['response_ids'] = response_snapshot(b'',response_headers,http_status)['response_ids']
        raise SeedanceError(f'下载或保存成片失败：{error}', diagnostics=trace) from error
