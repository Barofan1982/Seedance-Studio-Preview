"""Durable local image copies, independent of the current attachment list."""
from __future__ import annotations

import hashlib
import os
import time
from copy import deepcopy
from pathlib import Path

from media_assets import MAX_IMAGE_BYTES
from secure_store import load_protected_json, save_protected_json


def image_copy(record):
    backup = Path(record['snapshot_path']) if record.get('snapshot_path') else None
    if backup and backup.is_file():
        return backup
    original = Path(record['path']) if record.get('path') else None
    try:
        if original and original.is_file():
            stat = original.stat()
            if (stat.st_size == record.get('original_file_size')
                    and stat.st_mtime_ns == record.get('original_file_mtime_ns')):
                return original
    except OSError:
        pass
    return None


def image_status(record):
    if image_copy(record):
        return 'available', '本地已保存'
    if not record.get('path') and record.get('source_url'):
        return 'available', '网址素材'
    return 'missing', '文件缺失'


class ImageLibrary:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'image_library.bin'
        value = load_protected_json(self.path)
        self.records = value.get('records', {})
        self.sources = value.get('sources', {})
        self.history_imported = value.get('history_imported', False)

    @staticmethod
    def source_key(path):
        return os.path.normcase(os.path.abspath(path))

    def remember(self, asset, *, added_at=None):
        if asset.get('kind') != 'image': return
        record = deepcopy(asset)
        path = Path(record['path']) if record.get('path') else None
        name = record.get('handle', '').removeprefix('@{').removesuffix('}') or (path.name if path else '图片')
        source = self.source_key(path) if path else record.get('source_url', '')
        if not source: return
        stat = None
        try:
            if path and path.is_file(): stat = path.stat()
        except OSError:
            pass
        missing_id = 'missing:' + hashlib.sha256(source.encode('utf-8')).hexdigest()
        cached = self.sources.get(source, {})
        cached_record = self.records.get(cached.get('library_id'), {})
        if not stat and image_copy(cached_record):
            return cached_record['library_id']
        if stat:
            cached = self.sources.get(source, {})
            identity = cached.get('library_id')
            old = self.records.get(identity, {})
            if ([stat.st_size, stat.st_mtime_ns] == cached.get('version') and image_copy(old)):
                old.update(purpose=record.get('purpose', 'other'), selected=record.get('selected', True))
                return identity
            if not 0 < stat.st_size < MAX_IMAGE_BYTES:
                stat = None
            else:
                try:
                    data = path.read_bytes()
                    current = path.stat()
                    if len(data) >= MAX_IMAGE_BYTES or [stat.st_size, stat.st_mtime_ns] != [current.st_size, current.st_mtime_ns]:
                        raise OSError('图片正在变化，稍后重新导入。')
                    digest = hashlib.sha256(data).hexdigest()
                    identity = digest + ':' + hashlib.sha256(path.name.encode('utf-8')).hexdigest()[:16]
                    destination = self.directory / 'image_library_files' / digest / path.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.is_file():
                        temporary = destination.with_name(destination.name + '.tmp')
                        temporary.write_bytes(data)
                        temporary.replace(destination)
                    record.update(snapshot_path=str(destination), original_file_size=stat.st_size,
                                  original_file_mtime_ns=stat.st_mtime_ns, file_size=len(data), sha256=digest)
                    self.sources[source] = {'library_id': identity, 'version': [stat.st_size, stat.st_mtime_ns]}
                    self.records.pop(missing_id, None)
                except OSError:
                    stat = None
        if not stat:
            if image_copy(cached_record): return cached_record['library_id']
            identity = missing_id if path else 'url:' + hashlib.sha256(source.encode('utf-8')).hexdigest()
        previous = self.records.get(identity, {})
        record.update(library_id=identity, filename=name,
                      added_at=previous.get('added_at') or added_at or time.time())
        self.records[identity] = record
        return identity

    def find_asset(self, asset):
        """Find the latest saved version for an undo entry with a missing original."""
        source = self.source_key(asset['path']) if asset.get('path') else asset.get('source_url', '')
        identity = self.sources.get(source, {}).get('library_id')
        return self.records.get(identity)

    def matches_current(self, asset, record):
        if not asset.get('path'): return asset.get('source_url') == record.get('source_url')
        if record.get('snapshot_path') == asset['path']: return True
        try:
            stat = Path(asset['path']).stat()
        except OSError: return False
        saved = self.sources.get(self.source_key(asset['path']), {})
        return saved.get('library_id') == record['library_id'] and saved.get('version') == [stat.st_size, stat.st_mtime_ns]

    def rows(self):
        return sorted(self.records.values(), key=lambda row: row.get('added_at', 0), reverse=True)

    def get(self, identity):
        try: return deepcopy(self.records[identity])
        except KeyError: raise ValueError('图片记录不存在，请刷新素材库。') from None

    def save(self):
        save_protected_json(self.path, {'records': self.records, 'sources': self.sources,
                                       'history_imported': self.history_imported})
