"""Store an optional API key encrypted for the current Windows account."""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path


KEY_FILE = Path(os.environ.get("APPDATA", Path.home())) / "SeedanceVideoStudioPreview" / "api_key.bin"
OPENAI_KEY_FILE = KEY_FILE.with_name("openai_api_key.bin")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


if os.name == "nt":
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _transform(data: bytes, encrypt: bool) -> bytes:
    if os.name != "nt":
        raise OSError("此功能只支持 Windows。")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _DataBlob(len(data), buffer)
    target = _DataBlob()
    function = _crypt32.CryptProtectData if encrypt else _crypt32.CryptUnprotectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError(ctypes.get_last_error(), "Windows 密钥加密失败")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        _kernel32.LocalFree(target.pbData)


def _load_key(path: Path) -> str:
    try:
        return _transform(path.read_bytes(), encrypt=False).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _save_key(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(_transform(key.encode("utf-8"), encrypt=True))
    temporary.replace(path)


def load_key() -> str:
    return _load_key(KEY_FILE)


def save_key(key: str) -> None:
    _save_key(KEY_FILE, key)


def remove_key() -> None:
    KEY_FILE.unlink(missing_ok=True)


def load_openai_key() -> str:
    return _load_key(OPENAI_KEY_FILE)


def save_openai_key(key: str) -> None:
    _save_key(OPENAI_KEY_FILE, key)


def remove_openai_key() -> None:
    OPENAI_KEY_FILE.unlink(missing_ok=True)


def load_protected_json(path: Path) -> dict:
    try:
        value = json.loads(_transform(path.read_bytes(), encrypt=False))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def encode_protected_json(value: dict) -> bytes:
    return _transform(json.dumps(value, ensure_ascii=False).encode('utf-8'), encrypt=True)


def decode_protected_json(value: bytes) -> dict:
    result = json.loads(_transform(value, encrypt=False))
    if not isinstance(result, dict):
        raise ValueError('本地队列快照损坏，不能提交。')
    return result


def save_protected_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(_transform(json.dumps(value, ensure_ascii=False).encode("utf-8"), encrypt=True))
    temporary.replace(path)
