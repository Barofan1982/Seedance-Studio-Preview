"""Durable FIFO receipts; full media payloads are encrypted for this Windows user."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from secure_store import encode_protected_json, decode_protected_json
from request_records import compact_record
from error_diagnostics import safe_text

ACTIVE = {'submitting', 'submitted', 'queued', 'running', 'saving'}
STATUS_NAMES = {'waiting': '待提交', 'submitting': '提交中', 'queued': '服务端排队中', 'running': '生成中', 'saving': '保存中',
                'submitted':'已接单 · 待查询状态',
                'succeeded': '已保存', 'failed': '失败 · 已留档', 'unknown': '未收到回执 · 已留档',
                'cancelled': '已移出'}


class GenerationQueue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS entries (
                queue_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
                position INTEGER NOT NULL, status TEXT NOT NULL,
                summary TEXT NOT NULL, settings_json TEXT NOT NULL, snapshot BLOB NOT NULL,
                task_id TEXT NOT NULL DEFAULT '', call_id INTEGER,
                error TEXT NOT NULL DEFAULT '')''')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def add(self, snapshot):
        encoded = encode_protected_json(snapshot)
        with self.connect() as c:
            position = c.execute('SELECT COALESCE(MAX(position),0)+1 FROM entries').fetchone()[0]
            row = c.execute('''INSERT INTO entries
                (created_at,position,status,summary,settings_json,snapshot) VALUES (?,?,'waiting',?,?,?)''',
                (time.time(), position, ' '.join(safe_text(snapshot['original_prompt']).split())[:90],
                 json.dumps(snapshot['settings'], ensure_ascii=False), encoded))
            return row.lastrowid

    def rows(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('''SELECT queue_id,created_at,position,status,summary,
                settings_json,task_id,call_id,error FROM entries ORDER BY position,queue_id''')]

    def get(self, identity):
        with self.connect() as c:
            row = c.execute('SELECT * FROM entries WHERE queue_id=?', (identity,)).fetchone()
        if not row:
            raise ValueError('队列记录不存在，请刷新清单。')
        row = dict(row)
        row['snapshot'] = decode_protected_json(bytes(row['snapshot']))
        return row

    def update(self, identity, **fields):
        allowed = {'status','task_id','call_id','error'}
        if not fields or set(fields) - allowed:
            raise ValueError('队列更新字段无效。')
        with self.connect() as c:
            c.execute('UPDATE entries SET ' + ','.join(f'{key}=?' for key in fields) + ' WHERE queue_id=?',
                      [*fields.values(), identity])

    def claim(self, identity):
        with self.connect() as c:
            cursor = c.execute("UPDATE entries SET status='submitting',error='' WHERE queue_id=? AND status='waiting'",
                               (identity,))
            return cursor.rowcount == 1

    def move(self, identity, direction):
        with self.connect() as c:
            waiting = list(c.execute("SELECT queue_id,position FROM entries WHERE status='waiting' ORDER BY position,queue_id"))
            index = next((i for i, r in enumerate(waiting) if r['queue_id'] == identity), None)
            if index is None:
                raise ValueError('只能调整待提交任务的顺序。')
            other = index + (-1 if direction < 0 else 1)
            if 0 <= other < len(waiting):
                a, b = waiting[index], waiting[other]
                c.execute('UPDATE entries SET position=? WHERE queue_id=?', (b['position'], a['queue_id']))
                c.execute('UPDATE entries SET position=? WHERE queue_id=?', (a['position'], b['queue_id']))

    def cancel(self, identity):
        with self.connect() as c:
            cursor = c.execute("UPDATE entries SET status='cancelled' WHERE queue_id=? AND status='waiting'", (identity,))
            if cursor.rowcount != 1:
                raise ValueError('只能取消尚未提交的任务；其他记录自动保留在历史中。')

    def recover(self, history):
        """Never replay a POST after a close or crash; use saved receipts where possible."""
        calls = {c['call_id']: c for c in history.calls(limit=None) if c['operation'] == 'create'}
        for row in self.rows():
            if row['status'] not in ACTIVE:
                continue
            task_id = row['task_id']
            call = calls.get(row['call_id'])
            if not task_id and call and call['status'] == 'succeeded':
                response = json.loads(call['response_json'] or '{}')
                task_id = response.get('id', '') if isinstance(response, dict) else ''
            if task_id:
                snapshot = self.get(row['queue_id'])['snapshot']
                history.add(task_id, snapshot['payload'], snapshot['mode'],
                            original_prompt=snapshot['original_prompt'], assets=snapshot['assets'],
                            parent_task_id=snapshot['parent_task_id'], template=snapshot['template'], initial_status='submitted')
                self.update(row['queue_id'], task_id=task_id, status='submitted')
            elif call and call['status'] == 'failed':
                self.update(row['queue_id'], status='failed', error=call['error'])
            else:
                self.update(row['queue_id'], status='unknown',
                            error='程序关闭前未保存创建回执，服务器可能已接单；请核对后关联任务编号或确认未接单。')

    def fail(self, identity, error, *, uncertain=False, secrets=()):
        self.update(identity, status='unknown' if uncertain else 'failed',
                    error=str(compact_record(str(error), secrets)))
