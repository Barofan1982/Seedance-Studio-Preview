"""Sign in with ChatGPT for an open-source local Windows client."""

from __future__ import annotations

import base64
import hashlib
import json
import os
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

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from secure_store import load_protected_json, save_protected_json


APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "SeedanceVideoStudioPreview"
AUTH_FILE = APP_DIR / "chatgpt_accounts.bin"
AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
CALLBACK_PATH = "/auth/callback"


class ChatGPTAuthError(Exception):
    pass


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _json_request(url: str, fields: dict | None = None) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8") if fields is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Accept": "application/json"}, method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.build_opener(_NoRedirects()).open(request, timeout=30) as response:
            raw = response.read(2_000_001)
    except urllib.error.HTTPError as error:
        raw = error.read(4096)
        try:
            detail = json.loads(raw).get("error_description") or json.loads(raw).get("error") or "请求失败"
        except (ValueError, AttributeError):
            detail = "请求失败"
        raise ChatGPTAuthError(f"ChatGPT 登录返回 HTTP {error.code}：{str(detail)[:180]}") from None
    except (OSError, urllib.error.URLError) as error:
        raise ChatGPTAuthError(f"连接 ChatGPT 登录服务失败：{error}") from error
    if len(raw) > 2_000_000:
        raise ChatGPTAuthError("登录服务响应过大。")
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ChatGPTAuthError("登录服务返回的数据无效。") from error
    if not isinstance(value, dict):
        raise ChatGPTAuthError("登录服务返回格式无效。")
    return value


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _validate_id_token(token: str, client_id: str, nonce: str) -> dict:
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        header = json.loads(_base64url_decode(encoded_header))
        claims = json.loads(_base64url_decode(encoded_payload))
        signature = _base64url_decode(encoded_signature)
    except (ValueError, TypeError) as error:
        raise ChatGPTAuthError("ChatGPT 身份令牌格式无效。") from error
    discovery = _json_request(DISCOVERY_URL)
    if discovery.get("issuer") != "https://auth.openai.com":
        raise ChatGPTAuthError("ChatGPT 身份提供方不匹配。")
    jwks_uri = discovery.get("jwks_uri", "")
    parsed = urllib.parse.urlsplit(jwks_uri)
    if parsed.scheme != "https" or parsed.hostname != "auth.openai.com":
        raise ChatGPTAuthError("ChatGPT 公钥地址无效。")
    keys = _json_request(jwks_uri).get("keys", [])
    key = next((item for item in keys if item.get("kid") == header.get("kid") and item.get("use", "sig") == "sig"), None)
    if not key:
        raise ChatGPTAuthError("无法找到 ChatGPT 身份令牌的验证公钥。")
    message = f"{encoded_header}.{encoded_payload}".encode("ascii")
    try:
        if header.get("alg") == "RS256" and key.get("kty") == "RSA":
            public = rsa.RSAPublicNumbers(
                int.from_bytes(_base64url_decode(key["e"]), "big"),
                int.from_bytes(_base64url_decode(key["n"]), "big"),
            ).public_key()
            public.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        elif header.get("alg") == "ES256" and key.get("kty") == "EC" and key.get("crv") == "P-256":
            from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

            x = int.from_bytes(_base64url_decode(key["x"]), "big")
            y = int.from_bytes(_base64url_decode(key["y"]), "big")
            public = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
            if len(signature) != 64:
                raise ValueError("invalid ECDSA signature length")
            public.verify(
                encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")),
                message, ec.ECDSA(hashes.SHA256()),
            )
        else:
            raise ChatGPTAuthError("不支持的 ChatGPT 身份签名算法。")
    except (InvalidSignature, ValueError, KeyError) as error:
        raise ChatGPTAuthError("ChatGPT 身份令牌签名验证失败。") from error
    audience = claims.get("aud")
    if isinstance(audience, str):
        audience = [audience]
    now = time.time()
    if (
        claims.get("iss") != "https://auth.openai.com"
        or not isinstance(audience, list) or client_id not in audience
        or claims.get("nonce") != nonce
        or not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= now
        or not isinstance(claims.get("sub"), str) or not claims["sub"]
    ):
        raise ChatGPTAuthError("ChatGPT 身份令牌校验失败。")
    return claims


def _listen_for_code(expected_state: str) -> tuple[HTTPServer, dict[str, str]]:
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            values = urllib.parse.parse_qs(parsed.query)
            if parsed.path != CALLBACK_PATH or values.get("state", [""])[0] != expected_state:
                self.send_error(400, "Invalid callback")
                return
            result["code"] = values.get("code", [""])[0]
            result["error"] = values.get("error", [""])[0]
            result["client_id"] = values.get("client_id", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<html><body><h3>登录已完成，可返回视频工具。</h3></body></html>".encode("utf-8"))

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 5
    return server, result


class ChatGPTSession:
    def __init__(self, auth_file: Path = AUTH_FILE):
        self.auth_file = auth_file
        self._lock = threading.RLock()
        self.data = load_protected_json(auth_file)
        if auth_file.exists() and not self.data:
            raise ChatGPTAuthError("本机保存的 ChatGPT 登录信息无法解密。请检查 Windows 用户账号或备份该文件。")
        if "host_id" not in self.data:
            self.data["host_id"] = "urn:uuid:" + str(uuid.uuid4())
        self.data.setdefault("accounts", {})
        self.data.setdefault("active_id", "")
        self._save()

    def _save(self) -> None:
        save_protected_json(self.auth_file, self.data)

    def active_account(self) -> dict | None:
        return self.data["accounts"].get(self.data.get("active_id", ""))

    def label(self) -> str:
        account = self.active_account()
        if not account:
            return "尚未登录 ChatGPT"
        return f"已登录：{account.get('email') or account.get('subject', '')} · 使用 ChatGPT 计划"

    def sign_in(self, *, new_account: bool = False) -> str:
        current = None if new_account else self.active_account()
        selected_id = current.get("client_id") if current else None
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        server, callback = _listen_for_code(state)
        redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        parameters = {
            "client_id": selected_id or "dynamic_agent_client",
            "ext_agent_host_id": self.data["host_id"],
            "response_type": "code", "redirect_uri": redirect_uri,
            "scope": SCOPE, "resource": RESOURCE, "state": state, "nonce": nonce,
            "code_challenge_method": "S256", "code_challenge": challenge,
        }
        if current and current.get("id_token"):
            parameters["id_token_hint"] = current["id_token"]
        elif not current:
            parameters["agent_name_hint"] = "Seedance Video Workflow"
        url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(parameters)
        try:
            if not webbrowser.open(url):
                raise ChatGPTAuthError("无法打开浏览器，请检查默认浏览器设置。")
            deadline = time.monotonic() + 180
            while not callback and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if not callback:
            raise ChatGPTAuthError("等待 ChatGPT 登录超时，请重试。")
        if callback.get("error"):
            raise ChatGPTAuthError("ChatGPT 授权未完成。")
        client_id = selected_id or callback.get("client_id")
        if not client_id or client_id == "dynamic_agent_client" or (selected_id and callback.get("client_id") not in ("", selected_id)):
            raise ChatGPTAuthError("ChatGPT 客户端注册信息不匹配。")
        token = _json_request(TOKEN_URL, {
            "grant_type": "authorization_code", "client_id": client_id, "code": callback.get("code", ""),
            "code_verifier": verifier, "redirect_uri": redirect_uri, "resource": RESOURCE,
        })
        claims = _validate_id_token(str(token.get("id_token", "")), client_id, nonce)
        if current and claims["sub"] != current.get("subject"):
            raise ChatGPTAuthError("登录的 ChatGPT 账号与原账号不一致。")
        scopes = str(token.get("scope", "")).split()
        if "chatgpt.tokens.use.direct" not in scopes:
            raise ChatGPTAuthError("尚未授权使用 ChatGPT 计划。")
        if not token.get("access_token") or not token.get("refresh_token"):
            raise ChatGPTAuthError("ChatGPT 登录凭据不完整。")
        account = {
            "client_id": client_id, "subject": claims["sub"], "email": claims.get("email", ""),
            "id_token": token["id_token"], "access_token": token["access_token"],
            "refresh_token": token["refresh_token"], "scopes": scopes,
            "expires_at": time.time() + float(token.get("expires_in", 3600)),
        }
        with self._lock:
            self.data["accounts"][client_id] = account
            self.data["active_id"] = client_id
            self._save()
        return self.label()

    def access_token(self) -> str:
        with self._lock:
            account = self.active_account()
            if not account:
                raise ChatGPTAuthError("请先点击“使用 ChatGPT 登录”。")
            if account.get("expires_at", 0) > time.time() + 120:
                return account["access_token"]
            if not account.get("refresh_token"):
                raise ChatGPTAuthError("ChatGPT 登录已失效，请重新登录。")
            refreshed = _json_request(TOKEN_URL, {
                "grant_type": "refresh_token", "client_id": account["client_id"],
                "refresh_token": account["refresh_token"], "resource": RESOURCE,
            })
            if not refreshed.get("access_token") or not refreshed.get("refresh_token"):
                raise ChatGPTAuthError("ChatGPT 登录续期失败，请重新登录。")
            if "chatgpt.tokens.use.direct" not in str(refreshed.get("scope", "")).split():
                raise ChatGPTAuthError("ChatGPT 计划授权已失效，请重新登录。")
            account.update({
                "access_token": refreshed["access_token"], "refresh_token": refreshed["refresh_token"],
                "expires_at": time.time() + float(refreshed.get("expires_in", 3600)),
            })
            self._save()
            return account["access_token"]

    def models(self) -> list[dict]:
        token = self.access_token()
        request = urllib.request.Request(
            RESOURCE + "/models", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.build_opener(_NoRedirects()).open(request, timeout=30) as response:
                data = json.load(response)
        except (OSError, ValueError) as error:
            raise ChatGPTAuthError(f"读取 ChatGPT 模型列表失败：{error}") from error
        models = data.get("models", []) if isinstance(data, dict) else []
        return [item for item in models if isinstance(item, dict) and item.get("visibility") == "list" and item.get("slug")]
