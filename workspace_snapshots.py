"""Recoverable local workspaces, with encrypted records and durable media copies."""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
import uuid
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path

from secure_store import encode_protected_json, decode_protected_json
from video_upload import expiry_timestamp

CONTENT_DEFAULTS = {
    'prompt': '', 'brief': '', 'analysis': '', 'prompt_draft': {}, 'ai_original_text': '',
    'iteration_task_id': '', 'review_output': False, 'frame_catalog': [],
    'generated_keyframe_binding': '', 'use_keyframes_for_generation': False, 'manual_prompt': False,
}
SETTING_KEYS = ('mode', 'task_type', 'resolution', 'duration', 'ratio', 'generate_audio',
                'watermark', 'return_last_frame', 'output_format', 'auto_analyze', 'transcribe_audio')
SNAPSHOT_KEYS = tuple(CONTENT_DEFAULTS) + SETTING_KEYS


def has_content(state, assets):
    return bool(assets or str(state.get('prompt', '')).strip() or str(state.get('brief', '')).strip())


def rewrite_paths(value, replacements):
    if isinstance(value, dict):
        return {key: item if key == 'original_path' else rewrite_paths(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_paths(item, replacements) for item in value]
    return replacements.get(value, value) if isinstance(value, str) else value


class WorkspaceSnapshots:
    def __init__(self, directory):
        self.directory = Path(directory) / 'workspace_snapshots'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'records.sqlite3'
        with self.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY, created_at REAL NOT NULL,
                reason TEXT NOT NULL, contents BLOB NOT NULL)''')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _copy_media(self, source, progress):
        source = Path(source)
        progress('正在保存素材副本：' + source.name)
        media = self.directory / 'media'
        media.mkdir(exist_ok=True)
        temporary = media / (uuid.uuid4().hex + '.tmp')
        try:
            before = source.stat()
            digest = hashlib.sha256()
            with source.open('rb') as incoming, temporary.open('xb') as outgoing:
                for block in iter(lambda: incoming.read(1024 * 1024), b''):
                    digest.update(block)
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('素材正在变化，未清空。请等待文件保存完成：' + source.name)
            destination = media / digest.hexdigest() / source.name
            destination.parent.mkdir(exist_ok=True)
            if not destination.is_file():
                shutil.copystat(source, temporary)
                temporary.replace(destination)
            # Check existing copies as well; a damaged backup cannot justify clearing.
            with destination.open('rb') as saved:
                if hashlib.file_digest(saved, 'sha256').hexdigest() != digest.hexdigest():
                    raise ValueError('素材副本校验失败，未清空：' + source.name)
            return destination, digest.hexdigest(), before
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, state, assets, *, reason='清空前保存', progress=None, analysis_current=False):
        if not has_content(state, assets):
            raise ValueError('当前项目为空，无需清空。')
        progress = progress or (lambda _text: None)
        snapshot = {'state': {key: deepcopy(state[key]) for key in SNAPSHOT_KEYS if key in state},
                    'assets': deepcopy(assets), 'version': 1, 'analysis_current': bool(analysis_current)}
        replacements = {}
        for asset in snapshot['assets']:
            source = Path(asset['path']) if asset.get('path') else None
            if source and source.is_file():
                destination, digest, stat = self._copy_media(source, progress)
                replacements[str(source)] = str(destination)
                asset.update(original_path=str(source), sha256=digest)
                if asset.get('upload_id'):
                    if (stat.st_size, stat.st_mtime_ns) == (asset.get('upload_file_size'), asset.get('upload_file_mtime_ns')):
                        asset['upload_file_mtime_ns'] = destination.stat().st_mtime_ns
                    else:
                        # A URL for an older version must not be paired with a newer local copy.
                        asset.update(source_url='', source_expires_at='', upload_id='', upload_file_size=0, upload_file_mtime_ns=0)
            elif asset.get('source_url'):
                expires = expiry_timestamp(asset.get('source_expires_at'))
                if expires and expires <= time.time() + 60:
                    raise ValueError('素材地址已过期且没有本地副本，未清空。请先重新关联文件：' + asset.get('handle', ''))
            else:
                raise ValueError('素材文件缺失，无法保存可恢复副本，未清空：' + asset.get('handle', ''))
        # Analysis frames are also retained, but a missing analysis image does not
        # prevent saving the original media and prompt for later re-analysis.
        for frame in snapshot['state'].get('frame_catalog', []):
            source = frame.get('path')
            if source and source not in replacements and Path(source).is_file():
                replacements[source] = str(self._copy_media(source, progress)[0])
        snapshot = rewrite_paths(snapshot, replacements)
        identity = uuid.uuid4().hex
        encoded = encode_protected_json(snapshot)
        with self.connect() as connection:
            connection.execute('INSERT INTO snapshots VALUES (?,?,?,?)', (identity, time.time(), reason, encoded))
        # Re-read and decrypt the committed record before the caller may clear.
        self.get(identity)
        return identity

    def get(self, identity):
        with self.connect() as connection:
            row = connection.execute('SELECT created_at,reason,contents FROM snapshots WHERE snapshot_id=?', (identity,)).fetchone()
        if not row:
            raise ValueError('创作快照不存在，请刷新。')
        try:
            value = decode_protected_json(row[2])
            if value.get('version') != 1 or not isinstance(value.get('assets'), list) or not isinstance(value.get('state'), dict):
                raise ValueError('不支持的快照内容')
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            raise ValueError('创作快照无法读取或解密，未更改当前内容。') from error
        return {**value, 'snapshot_id': identity, 'created_at': row[0], 'reason': row[1]}

    def rows(self):
        with self.connect() as connection:
            identities = [row[0] for row in connection.execute('SELECT snapshot_id FROM snapshots ORDER BY created_at DESC')]
        records = []
        for identity in identities:
            try:
                records.append(self.get(identity))
            except ValueError as error:
                records.append({'snapshot_id': identity, 'error': str(error), 'state': {}, 'assets': []})
        return records

    def warnings(self, record, *, verify=False):
        warnings = []
        for asset in record['assets']:
            path = Path(asset['path']) if asset.get('path') else None
            name = asset.get('handle', '素材')
            if path and not path.is_file():
                warnings.append(name + '：本地副本缺失，需重新关联')
            elif path and verify and asset.get('sha256'):
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != asset['sha256']:
                        raise ValueError('快照素材副本已变化，未恢复：' + name)
            expires = expiry_timestamp(asset.get('source_expires_at'))
            if expires and expires <= time.time() + 60:
                warnings.append(name + ('：链接已过期，将使用本地副本重新上传' if path and path.is_file() else '：链接已过期，需重新关联文件'))
        return warnings
