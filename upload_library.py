"""Persistent uploaded videos; availability follows the server's signed URL expiry."""
from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path

from secure_store import load_protected_json, save_protected_json
from video_upload import expiry_timestamp


def record_status(record, now=None):
    now = time.time() if now is None else now
    expires = expiry_timestamp(record.get('source_expires_at'))
    if not record.get('source_url') or not expires:
        return 'unknown', '有效期未知'
    if expires <= now:
        return 'expired', '已过期'
    if expires <= now + 60:
        return 'expiring', '即将到期'
    return 'available', '可使用'


def local_copy(record):
    """Only use a local copy for analysis if it still matches the uploaded file."""
    if not record.get('path'):
        return None
    path = Path(record['path'])
    try:
        stat = path.stat()
        if (path.is_file() and stat.st_size == record.get('upload_file_size')
                and stat.st_mtime_ns == record.get('upload_file_mtime_ns')):
            return path
    except OSError:
        pass
    return None


class UploadLibrary:
    def __init__(self, path):
        self.path = Path(path)
        value = load_protected_json(self.path)
        self.records = value.get('records', {})

    def remember(self, asset, *, uploaded_at=None):
        identity = asset.get('upload_id')
        if not identity or not asset.get('source_url'):
            return
        previous = self.records.get(identity, {})
        expires = expiry_timestamp(asset.get('source_expires_at'))
        self.records[identity] = {
            **deepcopy(asset),
            'uploaded_at': previous.get('uploaded_at') or uploaded_at or (expires - 7*86400 if expires else 0),
        }

    def save(self):
        save_protected_json(self.path, {'records': self.records})

    def rows(self):
        return sorted(self.records.values(), key=lambda row: row.get('uploaded_at', 0), reverse=True)

    def get(self, identity):
        try:
            return deepcopy(self.records[identity])
        except KeyError:
            raise ValueError('上传记录不存在，请刷新素材库。') from None
