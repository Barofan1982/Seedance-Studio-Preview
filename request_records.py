"""Compact JSON records: retain prompt/parameters, omit credentials and media bytes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SECRET_FIELDS = {"authorization", "api_key", "apikey", "access_token", "refresh_token", "id_token", "accesstoken", "refreshtoken", "password", "key", "cookie", "set-cookie"}
SECRET_FIELDS.update({'token','session_token','client_secret','code_verifier','x-dsh-auth-token','authorize_id'})


def compact_record(value, secrets: tuple[str, ...] = ()):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): "[已隐藏]" if str(key).lower() in SECRET_FIELDS else compact_record(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [compact_record(item, secrets) for item in value]
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value:
            header = value.split(",", 1)[0]
            if re.fullmatch(re.escape(header) + r",\[本地媒体编码已折叠；字符数 \d+；sha256 [0-9a-f]{64}\]", value):
                return value
            return f"{header},[本地媒体编码已折叠；字符数 {len(value)}；sha256 {hashlib.sha256(value.encode()).hexdigest()}]"
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[已隐藏]")
        value = re.sub(r"sk-[A-Za-z0-9_-]{16,}", "[已隐藏]", value)
        value = re.sub(r"Bearer\s+\S+", "Bearer [已隐藏]", value, flags=re.I)
        if value.startswith(("https://", "http://")):
            parsed = urlsplit(value)
            query = [(key, "[已隐藏]" if any(word in key.lower() for word in ("token", "key", "signature", "credential")) else item)
                     for key, item in parse_qsl(parsed.query, keep_blank_values=True)]
            value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # This representation is only for logs/previews. Actual API payloads remain
    # untouched, so unexpected diagnostic objects cannot prevent saving a call.
    return {"record_type": f"{type(value).__module__}.{type(value).__qualname__}",
            "record_note": "非 JSON 对象，仅记录类型"}
