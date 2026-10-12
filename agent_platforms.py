"""DeepSeek API key storage, model validation and image/text analysis."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from error_diagnostics import response_snapshot, safe_text
from secure_store import load_protected_json, save_protected_json

PLATFORMS = {"chatgpt": "ChatGPT", "deepseek": "DeepSeek"}
INFERENCE_ORIGIN = "https://api.deepseek.com"
DEEPSEEK_MODELS = [
    {"slug": "deepseek-flash", "display_name": "DeepSeek Flash · 支持图片", "vision": True},
    {"slug": "deepseek-v4-pro", "display_name": "DeepSeek Pro · 纯文本", "vision": False},
]
MAX_RESPONSE = 8_000_000


class PlatformError(Exception):
    def __init__(self, message, *, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def open_request(url, *, body=None, headers=None, timeout=30, secret_values=()):
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                                    headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "SeedanceStudio/0.1", **(headers or {})})
    started = time.monotonic()
    try:
        return urllib.request.build_opener(NoRedirects()).open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        try:
            raw = error.read(2_000_001)
            trace = response_snapshot(raw, error.headers, error.code, secrets=secret_values)
        finally:
            error.close()
        trace.update(endpoint=url, method=request.get_method(), phase="http_response", elapsed_ms=round((time.monotonic()-started)*1000),
                     timeout_seconds=timeout, request_body_bytes=len(request.data or b''))
        hint = "接口密钥无效或已失效，请重新配置。" if error.code == 401 else "API 余额不足，请在平台查看余额。" if error.code == 402 else trace.get("api_error_message") or "平台未接受请求。"
        if trace.get('access_blocked'):
            stage = "请求被平台访问防护拦截"
            event = trace['response_ids'].get('event_id') or trace['response_ids'].get('request_id')
            hint = stage + "。请勿连续重试；可携事件编号联系 DeepSeek 支持。" + (f"事件编号：{event}" if event else "")
        elif error.code == 429:
            hint = "平台限制了本次请求，请查看返回内容与 Retry-After 后再操作。"
        raise PlatformError(safe_text(f"平台返回 HTTP {error.code}：{hint}", secret_values), diagnostics=trace) from None
    except (OSError, urllib.error.URLError) as error:
        raise PlatformError("连接平台失败，请检查网络后重试。", diagnostics={
            "endpoint": url, "method": request.get_method(), "phase": "opening_connection_or_sending_request",
            "reason": safe_text(error, secret_values), "timeout_seconds": timeout,
        }) from None


def json_request(url, **kwargs):
    with open_request(url, **kwargs) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise PlatformError("平台返回的数据过大。")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise PlatformError("平台返回的数据无法解析。") from None
    if not isinstance(value, dict):
        raise PlatformError("平台返回的格式不正确。")
    return value


def parse_draft(value):
    """Reject incomplete or invented response shapes before touching the user's draft."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        try:
            value = json.loads(text)
        except ValueError:
            raise PlatformError("AI 返回的结果不是完整方案，请保留原提示词后重试。") from None
    if not isinstance(value, dict) or not isinstance(value.get("analysis"), str) or not isinstance(value.get("seedance_prompt"), str) or not value["seedance_prompt"].strip():
        raise PlatformError("AI 返回的方案缺少提示词。")
    for field in ("uncertainties", "keyframe_handles"):
        if not isinstance(value.get(field), list) or any(not isinstance(item, str) for item in value[field]):
            raise PlatformError("AI 返回的方案结构不完整。")
    if not isinstance(value.get("shots"), list) or any(not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in ("time", "visual", "camera", "motion")) for item in value["shots"]):
        raise PlatformError("AI 返回的镜头结构不完整。")
    return {key: value[key] for key in ("analysis", "shots", "seedance_prompt", "uncertainties", "keyframe_handles")}


class DeepSeekApiSession:
    def __init__(self, file):
        self.file = Path(file)
        self.record = load_protected_json(self.file)
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.usage = {}

    def active_account(self):
        with self.lock:
            return {"label": "DeepSeek API"} if self.record.get('api_key') and self.record.get('verified') is True else None

    def label(self):
        return '已配置接口密钥' if self.active_account() else '未配置接口密钥'

    def _list_models(self, key):
        value = json_request(INFERENCE_ORIGIN + '/models', headers={'Authorization': 'Bearer ' + key}, secret_values=(key,))
        data = value.get('data')
        if not isinstance(data, list) or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in data):
            raise PlatformError('DeepSeek 模型列表返回格式不正确。')
        available = {item['id'] for item in data}
        models = [dict(model) for model in DEEPSEEK_MODELS if model['slug'] in available]
        if not models:
            raise PlatformError('接口没有返回可用的分析模型，请检查账号权限。')
        return models

    def configure_key(self, key):
        key = str(key).strip()
        if not key or len(key) > 1024 or not re.fullmatch(r'[!-~]+', key):
            raise PlatformError('请填写有效的 DeepSeek API Key。')
        self.cancelled.clear()
        models = self._list_models(key)
        with self.lock:
            if self.cancelled.is_set():
                raise PlatformError('已取消接口校验。')
            record = {'api_key': key, 'verified': True}
            save_protected_json(self.file, record)
            self.record = record
        return models

    def key(self):
        with self.lock:
            if not self.active_account():
                raise PlatformError('请先配置 DeepSeek API Key。')
            return self.record['api_key']

    def reject_key(self, key):
        with self.lock:
            if key and self.record.get('api_key') == key:
                self.record = {}
                save_protected_json(self.file, {})

    def models(self):
        if not self.active_account():
            return []
        key = self.key()
        try:
            return self._list_models(key)
        except PlatformError as error:
            if error.diagnostics.get('http_status') == 401:
                self.reject_key(key)
            raise

    def cancel_pending(self):
        with self.lock:
            self.cancelled.set()

    def clear_key(self):
        self.cancel_pending()
        self.reject_key(self.record.get('api_key', ''))

    def complete(self, request):
        key, model = self.key(), request['model']
        entry = next((item for item in DEEPSEEK_MODELS if item['slug'] == model), None)
        if entry is None:
            raise PlatformError('请选择受支持的 DeepSeek 分析模型。')
        content = []
        for message in request.get('input', []):
            for block in message.get('content', []):
                if block.get('type') == 'input_text':
                    content.append({'type': 'text', 'text': block['text']})
                elif block.get('type') == 'input_image':
                    if not entry['vision']:
                        raise PlatformError('当前 DeepSeek 模型只支持文本，请选择支持图片的模型。')
                    url = block.get('image_url', '')
                    if not isinstance(url, str) or not re.fullmatch(r'data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/=]+', url):
                        raise PlatformError('DeepSeek 图片编码不正确。')
                    content.append({'type': 'image_url', 'image_url': {'url': url}})
        if sum(block['type'] == 'image_url' for block in content) > 600:
            raise PlatformError('分析画面过多，请减少参考视频或图片。')
        schema = request['text']['format']['schema']
        system = request['instructions'] + '\n只输出符合以下结构的 JSON，不加说明或代码围栏：\n' + json.dumps(schema, ensure_ascii=False)
        body = {'model': model, 'stream': False, 'max_tokens': 8000, 'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': content}]}
        if len(json.dumps(body, ensure_ascii=False).encode('utf-8')) > 48 * 1024 * 1024:
            raise PlatformError('分析请求超过 48 MiB，请减少参考图片或抽帧数量。')
        try:
            result = json_request(INFERENCE_ORIGIN + '/chat/completions', body=body,
                                  headers={'Authorization': 'Bearer ' + key}, timeout=300, secret_values=(key,))
        except PlatformError as error:
            if error.diagnostics.get('http_status') == 401:
                self.reject_key(key)
            raise
        self.usage = result.get('usage') or {}
        choices = result.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or choices[0].get('finish_reason') != 'stop':
            raise PlatformError('DeepSeek 未返回完整结果，原提示词已保留。')
        message = choices[0].get('message') or {}
        if not isinstance(message, dict):
            raise PlatformError('DeepSeek 返回的消息结构不正确，原提示词已保留。')
        return parse_draft(message.get('content'))


class PlatformAccounts:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.sessions = {'deepseek': DeepSeekApiSession(self.directory / 'deepseek_api_key.bin')}

    def cancel_pending(self):
        for session in self.sessions.values():
            session.cancel_pending()
