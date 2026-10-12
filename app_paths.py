"""Keep portable runtime data beside the executable; source uses its old profile."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path


def data_directory(*, frozen=None, executable=None, appdata=None):
    if frozen is None:
        frozen = bool(getattr(sys, 'frozen', False))
    if frozen:
        return Path(executable or sys.executable).resolve().parent / 'data'
    return Path(appdata or os.environ.get('APPDATA', Path.home())) / 'SeedanceVideoStudioPreview'


def output_directory(*, frozen=None, executable=None):
    if frozen is None:
        frozen = bool(getattr(sys, 'frozen', False))
    if frozen:
        return Path(executable or sys.executable).resolve().parent / '成片'
    return Path.home() / 'Downloads' / 'Seedance Studio'


APP_DIR = data_directory()
DEFAULT_OUTPUT_DIR = output_directory()
TECHNICAL_CONTACT = '不知道即梦 API Token 或中转站登录账号？请联系吴仕敏索取文档和密钥。'


def open_error_log(directory=APP_DIR):
    """A blocked diagnostic file must not stop an otherwise writable application."""
    log_dir = Path(directory) / 'logs'
    unique_name = f'error-{os.getpid()}-{time.time_ns()}.log'
    for target in (log_dir / 'error.log', log_dir / unique_name,
                   Path(tempfile.gettempdir()) / 'SeedanceStudio' / unique_name):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            return target.open('a', encoding='utf-8'), target
        except OSError:
            continue
    return None, None
