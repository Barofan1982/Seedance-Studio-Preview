"""Account login adapters. Platform application credentials never enter the desktop UI.

DeepSeek follows the published dsh-deepseek-account-platform and Messages protocols.
WorkBuddy / Doubao Work use a developer-operated authorization service; that service
owns application secrets and refresh tokens. No browser cookie extraction or API-key fallback.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import platform
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from error_diagnostics import response_snapshot, safe_text
from secure_store import load_protected_json, save_protected_json

PLATFORMS = {"chatgpt": "ChatGPT", "deepseek": "DeepSeek", "workbuddy": "WorkBuddy", "doubao_work": "豆包工作"}
PLATFORM_ORIGIN = "https://platform.deepseek.com"
INFERENCE_ORIGIN = "https://api.deepseek.com"
# Advisory catalog from the official Harness source, not a remotely verified entitlement.
DEEPSEEK_MODELS = [
    {"slug": "deepseek-flash", "display_name": "DeepSeek-V4.1-Flash · 支持图片", "vision": True, "catalog_source": "官方预置，调用时验证"},
    {"slug": "deepseek-v4-pro", "display_name": "DeepSeek-V4-Pro · 纯文本", "vision": False, "catalog_source": "官方预置，调用时验证"},
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
                                    headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})})
    started = time.monotonic()
    try:
        return urllib.request.build_opener(NoRedirects()).open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        try:
            raw = error.read(2_000_001)
            trace = response_snapshot(raw, error.headers, error.code, secrets=secret_values)
        finally:
            error.close()
        trace.update(endpoint=url, method=request.get_method(), phase="http_response", elapsed_ms=round((time.monotonic()-started)*1000))
        hint = "登录已失效，请重新登录。" if error.code == 401 else "账号额度不足，请在平台查看余额。" if error.code == 402 else trace.get("api_error_message") or "平台未接受请求。"
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


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def validated_browser_url(url, origin, path=None):
    if not isinstance(url, str) or not url:
        raise PlatformError('平台返回的登录地址不完整。')
    actual, allowed = urllib.parse.urlsplit(url), urllib.parse.urlsplit(origin)
    if (actual.scheme, actual.netloc) != (allowed.scheme, allowed.netloc) or actual.username or actual.password or actual.fragment or path and actual.path != path:
        raise PlatformError("平台返回了不受信任的登录地址，已停止登录。")
    return url


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


def read_messages_stream(response):
    """Only settled text blocks count as output; reasoning and truncated streams do not."""
    blocks, usage, reason, started = {}, {}, None, False
    event_lines, total = [], 0
    for raw in response:
        total += len(raw)
        if total > MAX_RESPONSE or len(raw) > 1_000_000:
            raise PlatformError("AI 返回的数据过大。")
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise PlatformError('AI 返回的流式编码不正确。') from None
        if line.startswith("data:"):
            event_lines.append(line[5:].lstrip())
        elif not line and event_lines:
            try:
                event = json.loads("\n".join(event_lines))
            except ValueError:
                raise PlatformError("AI 返回的流式数据无法解析。") from None
            event_lines = []
            kind = event.get("type")
            if kind == "error":
                code = event.get("error", {}).get("type", "")
                raise PlatformError("AI 分析失败，请检查账号权限或额度。", diagnostics={"api_error_code": code, "phase": "stream_response"})
            if kind == "message_start":
                if started:
                    raise PlatformError("AI 返回了重复的响应。")
                started = True
                usage.update(event.get("message", {}).get("usage", {}))
            elif kind == "content_block_start":
                index, block = event.get("index"), event.get("content_block", {})
                if not started or index in blocks or reason is not None or block.get("type") not in {"text", "thinking"}:
                    raise PlatformError("AI 返回了不支持的内容。")
                blocks[index] = {"kind": block["type"], "text": block.get("text", ""), "closed": False}
            elif kind in {"content_block_delta", "content_block_stop"}:
                block = blocks.get(event.get("index"))
                if block is None or block["closed"]:
                    raise PlatformError("AI 返回的内容顺序不正确。")
                if kind == "content_block_stop":
                    block["closed"] = True
                elif event.get("delta", {}).get("type") == "text_delta" and block["kind"] == "text":
                    block["text"] += event["delta"]["text"]
            elif kind == "message_delta":
                reason = event.get("delta", {}).get("stop_reason") or reason
                usage.update(event.get("usage", {}))
            elif kind == "message_stop":
                if not started or reason not in {"end_turn", "stop_sequence"} or any(not block["closed"] for block in blocks.values()):
                    raise PlatformError("AI 响应未完成，原提示词已保留。")
                return "".join(block["text"] for block in blocks.values() if block["kind"] == "text"), usage
    raise PlatformError("AI 连接结束但未返回完整结果，原提示词已保留。")


class DeepSeekSession:
    def __init__(self, file):
        self.file = Path(file)
        self.record = load_protected_json(self.file)
        self.cancelled = threading.Event()
        self.lock = threading.RLock()
        self.usage = {}

    def active_account(self):
        with self.lock:
            return {"label": self.record.get("label") or "DeepSeek 账号"} if self.record.get("token") and self.record.get("issuer") == PLATFORM_ORIGIN else None

    def label(self):
        return (self.active_account() or {}).get("label", "尚未登录")

    def models(self):
        if not self.active_account():
            return []
        self.refresh_profile()
        return [dict(model) for model in DEEPSEEK_MODELS]

    def headers(self):
        return {"x-client-platform": "web", "x-client-bundle-id": "", "x-client-version": "seedance-studio-preview",
                "x-client-locale": "zh_CN", "x-client-timezone-offset": "28800"}

    def _platform(self, path, *, body=None, token=""):
        value = json_request(PLATFORM_ORIGIN + path, body=body,
                             headers={**self.headers(), **({"x-dsh-auth-token": token} if token else {})},
                             secret_values=(token, *[str(v) for v in (body or {}).values()]))
        if value.get("code") == 40003:
            self.reject_token(token)
            raise PlatformError("DeepSeek 登录已失效，请重新登录。")
        data = value.get("data")
        if value.get("code") != 0 or not isinstance(data, dict) or data.get("biz_code") != 0:
            code = data.get('biz_code', value.get('code')) if isinstance(data, dict) else value.get('code')
            secret_values = (token, *[str(v) for v in (body or {}).values()])
            detail = safe_text((data.get('biz_msg') or data.get('biz_message') or '') if isinstance(data, dict) else '', secret_values) or safe_text(value.get('msg') or value.get('message') or '请求未完成', secret_values)
            trace = response_snapshot(json.dumps(value).encode(), secrets=secret_values)
            trace.update(endpoint=PLATFORM_ORIGIN+path, phase='business_response', api_error_code=str(code), api_error_message=detail)
            raise PlatformError(f'DeepSeek 返回错误 {code}：{detail}', diagnostics=trace)
        return data.get("biz_data")

    def token(self):
        with self.lock:
            if not self.active_account() or str(self.record["token"]).startswith("dsh_mock_"):
                raise PlatformError("请先登录 DeepSeek。")
            return self.record["token"]

    def reject_token(self, token):
        with self.lock:
            if token and self.record.get("token") == token:
                self.record = {"device_id": self.record.get("device_id", str(uuid.uuid4()))}
                save_protected_json(self.file, self.record)

    def refresh_profile(self):
        token = self.token()
        try:
            user = self._platform("/auth-api/v0/users/current", token=token)
        except PlatformError as error:
            if error.diagnostics.get("http_status") == 401:
                self.reject_token(token)
            raise
        with self.lock:
            if self.record.get("token") == token:
                self.record["label"] = self._label(user)
                save_protected_json(self.file, self.record)

    @staticmethod
    def _label(user):
        if not isinstance(user, dict):
            return "DeepSeek 账号"
        identity = user.get("id_profile") or {}
        return str(identity.get("name") or user.get("mobile") or user.get("mobile_number") or user.get("email") or "DeepSeek 账号")[:150]

    def cancel_sign_in(self):
        with self.lock:
            self.cancelled.set()

    def sign_in(self, *, progress=None, browser=webbrowser.open, timeout=600):
        self.cancelled.clear()
        verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        callback = {}
        owner = self

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
                valid = (url.path == "/oauth/callback" and len(query.get("state", [])) == 1 and
                         len(query.get("code", [])) == 1 and query["code"][0] and
                         hmac.compare_digest(query["state"][0].encode(), state.encode()) and not callback and not owner.cancelled.is_set())
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                self.end_headers()
                if valid:
                    callback["code"] = query["code"][0]
                self.wfile.write(("<!doctype html><meta charset=utf-8><p>授权已回传，请回到 Seedance Studio 查看连接结果。</p>" if valid else "无效的登录回调。").encode())

        deadline, authorize_id, committed = time.monotonic() + timeout, "", False
        with HTTPServer(("127.0.0.1", 0), Callback) as server:
            server.timeout = 0.25
            redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth/callback"
            try:
                init = self._platform("/auth-api/v0/dsh/auth_init", body={"code_challenge": challenge(verifier), "code_challenge_method": "S256",
                      "state": state, "redirect_uri": redirect_uri, "locale": "zh_CN", "login_source": "web"})
                if not isinstance(init, dict) or not isinstance(init.get("expires_in"), (int, float)) or init["expires_in"] <= 0 or not init.get("authorize_id"):
                    raise PlatformError("DeepSeek 登录初始化返回不完整。")
                authorize_id = init["authorize_id"]
                login_url = validated_browser_url(init.get("authorize_url", ""), PLATFORM_ORIGIN, "/dsh/authorize")
                deadline = min(deadline, time.monotonic()+init["expires_in"])
                if self.cancelled.is_set():
                    raise PlatformError("已取消登录。")
                if progress:
                    progress("请在浏览器中完成 DeepSeek 登录授权…")
                if not browser(login_url):
                    raise PlatformError("无法打开浏览器，请检查默认浏览器设置。")
                while not callback and not self.cancelled.is_set() and time.monotonic() < deadline:
                    server.handle_request()
                if not callback or self.cancelled.is_set() or time.monotonic() >= deadline:
                    raise PlatformError("已取消登录。" if self.cancelled.is_set() else "等待 DeepSeek 登录超时，请重试。")
                device = self.record.get("device_id") or str(uuid.uuid4())
                result = self._platform("/auth-api/v0/dsh/auth_exchange", body={"code": callback["code"], "code_verifier": verifier,
                    "redirect_uri": redirect_uri, "device_id": device, "device_model": f"{platform.system().lower()}-{platform.machine()}",
                    "os_version": f"{platform.system()} {platform.release()}"})
                if not isinstance(result, dict) or not isinstance(result.get("token"), str) or not re.fullmatch(r"[!-~]+", result["token"]):
                    raise PlatformError("DeepSeek 登录凭据不完整。")
                validated_browser_url(result.get("authorized_url", ""), PLATFORM_ORIGIN, "/dsh/authorized")
                with self.lock:
                    if self.cancelled.is_set() or time.monotonic() >= deadline:
                        raise PlatformError("登录已取消或超时，请重试。")
                    record = {"token": result["token"], "issuer": PLATFORM_ORIGIN, "device_id": device, "label": self._label(result.get("user"))}
                    save_protected_json(self.file, record)
                    self.record = record
                    committed = True
                return self.label()
            finally:
                if authorize_id and not committed:
                    try:
                        self._platform("/auth-api/v0/dsh/auth_cancel", body={"authorize_id": authorize_id, "code_verifier": verifier})
                    except PlatformError:
                        pass

    def sign_out(self):
        self.cancel_sign_in()
        token = self.record.get("token", "")
        self.reject_token(token)
        # Revocation is best effort; local credentials are removed immediately.
        if token:
            def revoke():
                try:
                    self._platform("/auth-api/v0/users/logout", body={}, token=token)
                except PlatformError:
                    pass
            threading.Thread(target=revoke, daemon=True).start()

    def complete(self, request):
        token, model = self.token(), request["model"]
        entry = next((item for item in DEEPSEEK_MODELS if item["slug"] == model), None)
        if entry is None:
            raise PlatformError("请选择 DeepSeek 官方预置模型。")
        content, image_bytes, image_count = [], 0, 0
        for message in request.get("input", []):
            for block in message.get("content", []):
                if block.get("type") == "input_text":
                    content.append({"type": "text", "text": block["text"]})
                elif block.get("type") == "input_image":
                    if not entry["vision"]:
                        raise PlatformError("当前 DeepSeek 模型只支持文本，请选择支持图片的模型。")
                    match = re.fullmatch(r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)", block["image_url"])
                    if not match:
                        raise PlatformError("DeepSeek 图片编码不正确。")
                    image_count += 1
                    image_bytes += len(match[2])
                    content.append({"type": "image", "source": {"type": "base64", "media_type": match[1], "data": match[2]}})
        if image_count > 600 or image_bytes > 20*1024*1024:
            raise PlatformError("分析画面过多，请减少参考视频或图片后重试。")
        schema = request["text"]["format"]["schema"]
        body = {"model": model, "stream": True, "max_tokens": 8000, "thinking": {"type": "enabled"}, "output_config": {"effort": "high"},
                "system": request["instructions"] + "\n只输出符合以下结构的 JSON，不加说明或代码围栏：\n" + json.dumps(schema, ensure_ascii=False),
                "messages": [{"role": "user", "content": content}]}
        try:
            with open_request(INFERENCE_ORIGIN+"/v1/messages", body=body, timeout=300,
                              headers={"x-dsh-auth-token": token, "anthropic-version": "2023-06-01", "Accept": "text/event-stream",
                                       "x-deepseek-harness-user-id": self.record.get('device_id') or str(uuid.uuid4()),
                                       "x-deepseek-harness-session-id": str(uuid.uuid4())}, secret_values=(token,)) as response:
                text, self.usage = read_messages_stream(response)
        except PlatformError as error:
            if error.diagnostics.get("http_status") == 401:
                self.reject_token(token)
            raise
        return parse_draft(text)


class ServiceSession:
    """OAuth broker client; sessions are bound to the publisher's exact service URL."""
    def __init__(self, provider, file, config):
        self.provider, self.file, self.config = provider, Path(file), config
        self.record = load_protected_json(self.file)
        self.cancelled = threading.Event()
        self.lock = threading.RLock()
        self.catalog = []
        self.usage = {}

    @property
    def origin(self):
        address = str(self.config.get("service_url", "")).strip().rstrip("/")
        url = urllib.parse.urlsplit(address)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise PlatformError(f"{PLATFORMS[self.provider]} 登录暂未开通，请等待工具更新。")
        return address

    def configured(self):
        try:
            return bool(self.origin)
        except PlatformError:
            return False

    def active_account(self):
        return {"label": self.record.get("label") or PLATFORMS[self.provider]} if self.configured() and self.record.get("service_url") == self.origin and self.record.get("session_token") else None

    def label(self):
        return (self.active_account() or {}).get("label", "尚未登录" if self.configured() else "暂未开通")

    def _request(self, path, *, body=None, authorized=False, timeout=30):
        token = self.record.get("session_token", "") if authorized else ""
        if authorized and not self.active_account():
            raise PlatformError(f"请先登录 {PLATFORMS[self.provider]}。")
        try:
            result = json_request(self.origin + f"/v1/platforms/{self.provider}/" + path, body=body,
                                  headers={"Authorization": "Bearer "+token} if token else {}, timeout=timeout,
                                  secret_values=(token, *[str(v) for v in (body or {}).values() if isinstance(v, str)]))
        except PlatformError as error:
            if token and error.diagnostics.get("http_status") == 401 and self.record.get("session_token") == token:
                self.sign_out(remote=False)
            raise
        if result.get("error"):
            raise PlatformError("授权服务未完成请求。", diagnostics={"api_error_code": str(result.get("error_code", "")), "phase": "business_response"})
        return result

    def metadata(self):
        value = self._request("metadata")
        if value.get("provider") != self.provider or value.get("configured") is not True:
            raise PlatformError(f"{PLATFORMS[self.provider]} 登录暂未开通，请等待工具更新。")
        models = value.get("models")
        if not isinstance(models, list) or not models or any(not isinstance(m, dict) or not isinstance(m.get("slug"), str) or not m["slug"] for m in models):
            raise PlatformError("授权服务未提供可用模型。")
        self.catalog = [{"slug": m["slug"], "display_name": str(m.get("display_name") or m["slug"]), "vision": m.get("vision") is True} for m in models]
        return value

    def models(self):
        if not self.active_account():
            return []
        self.metadata()
        self._request("account", authorized=True)
        return list(self.catalog)

    def cancel_sign_in(self):
        with self.lock:
            self.cancelled.set()

    def sign_in(self, *, progress=None, browser=webbrowser.open, timeout=600):
        self.cancelled.clear()
        self.metadata()
        verifier = secrets.token_urlsafe(32)
        value = self._request("auth/start", body={"code_challenge": challenge(verifier), "code_challenge_method": "S256"})
        # The broker owns the OAuth state and callback. Browser navigation starts only on its origin.
        login_url = validated_browser_url(value.get("authorize_url", ""), self.origin)
        attempt = value.get("attempt_id")
        ttl = value.get("expires_in")
        if not isinstance(attempt, str) or not attempt or not isinstance(ttl, (int, float)) or ttl <= 0:
            raise PlatformError("登录初始化返回不完整。")
        deadline = time.monotonic()+min(timeout, ttl)
        if progress:
            progress(f"请在浏览器中完成 {PLATFORMS[self.provider]} 登录授权…")
        if not browser(login_url):
            raise PlatformError("无法打开浏览器，请检查默认浏览器设置。")
        committed = False
        try:
            while not self.cancelled.is_set() and time.monotonic() < deadline:
                self.cancelled.wait(2)
                if self.cancelled.is_set():
                    break
                result = self._request("auth/poll", body={"attempt_id": attempt, "code_verifier": verifier})
                if result.get("status") == "pending":
                    continue
                if result.get("status") != "authorized" or not isinstance(result.get("session_token"), str) or not result["session_token"]:
                    raise PlatformError("本次授权未完成，请重新登录。")
                with self.lock:
                    if self.cancelled.is_set() or time.monotonic() >= deadline:
                        break
                    record = {"service_url": self.origin, "session_token": result["session_token"], "label": str(result.get("label") or PLATFORMS[self.provider])[:150]}
                    save_protected_json(self.file, record)
                    self.record, committed = record, True
                return self.label()
            raise PlatformError("已取消登录。" if self.cancelled.is_set() else "等待登录超时，请重试。")
        finally:
            if not committed:
                try:
                    self._request("auth/cancel", body={"attempt_id": attempt, "code_verifier": verifier})
                except PlatformError:
                    pass

    def sign_out(self, *, remote=True):
        self.cancel_sign_in()
        token = self.record.get("session_token", "")
        origin = self.record.get("service_url", "")
        self.record = {}
        save_protected_json(self.file, {})
        if remote and token and self.configured() and origin == self.origin:
            def revoke():
                try:
                    json_request(origin+f"/v1/platforms/{self.provider}/auth/revoke", body={}, headers={"Authorization": "Bearer "+token}, secret_values=(token,))
                except PlatformError:
                    pass
            threading.Thread(target=revoke, daemon=True).start()

    def complete(self, request):
        entry = next((item for item in self.catalog if item["slug"] == request["model"]), None)
        if entry is None:
            raise PlatformError("请先刷新平台模型列表。")
        if not entry["vision"] and any(block.get("type") == "input_image" for m in request["input"] for block in m["content"]):
            raise PlatformError("当前平台模型不支持图片分析，请选择支持图片的模型。")
        # All frame bytes and the exact draft schema are forwarded; no text-only silent downgrade.
        result = self._request("analyze", body={"request_id": str(uuid.uuid4()), "request": request}, authorized=True, timeout=300)
        self.usage = result.get("usage") or {}
        return parse_draft(result.get("draft"))


class PlatformAccounts:
    def __init__(self, directory, distribution_file=None):
        self.directory = Path(directory)
        file = Path(distribution_file) if distribution_file else Path(__file__).with_name("platform_defaults.json")
        try:
            config = json.loads(file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            config = {}
        self.sessions = {"deepseek": DeepSeekSession(self.directory/"deepseek_account.bin")}
        for provider in ("workbuddy", "doubao_work"):
            # A distribution file may contain only public connection information.
            public = config.get(provider, {}) if isinstance(config, dict) else {}
            self.sessions[provider] = ServiceSession(provider, self.directory/(provider+"_account.bin"),
                                                     {"service_url": public.get("service_url", "")} if isinstance(public, dict) else {})

    def cancel_sign_in(self):
        for session in self.sessions.values():
            session.cancel_sign_in()
