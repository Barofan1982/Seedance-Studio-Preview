"""Local task history and token/seconds audit for Seedance generation."""

from __future__ import annotations

import os
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from seedance_client import estimated_cost_usd
from request_records import compact_record


from app_paths import APP_DIR

HISTORY_FILE = APP_DIR / "history.sqlite3"


class HistoryStore:
    def __init__(self, path: Path = HISTORY_FILE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    ratio TEXT NOT NULL,
                    output_format TEXT NOT NULL DEFAULT 'mp4',
                    requested_duration INTEGER,
                    actual_duration REAL,
                    status TEXT NOT NULL,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    estimated_cost_usd REAL,
                    video_url TEXT NOT NULL DEFAULT '',
                    local_path TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                )
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "local_path" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN local_path TEXT NOT NULL DEFAULT ''")
            if "output_format" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN output_format TEXT NOT NULL DEFAULT 'mp4'")
            for name, default in (("request_json", "{}"), ("assets_json", "[]"), ("original_prompt", ""), ("parent_task_id", "")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
            if "media_json" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN media_json TEXT NOT NULL DEFAULT '{}'")
            if "template_json" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN template_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS calls (
                    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL, operation TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, duration_ms INTEGER, request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT ''
                )
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def add(self, task_id: str, payload: dict, mode: str = "reference", *,
            original_prompt: str = "", assets: list[dict] | None = None, parent_task_id: str = "",
            template: dict | None = None, initial_status: str = 'queued') -> None:
        prompt = next((item.get("text", "") for item in payload.get("content", []) if item.get("type") == "text"), "")
        with self._connect() as connection:
            connection.execute("""
                INSERT OR IGNORE INTO tasks
                (task_id, created_at, model, mode, task_type, resolution, ratio, output_format,
                 requested_duration, status, prompt, request_json, assets_json, original_prompt, parent_task_id, template_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task_id, int(time.time()), payload["model"], mode,
                payload.get("omni_reference_task_type", "reference"), payload["resolution"], payload["ratio"],
                payload.get("output_format", "mp4"),
                payload.get("duration"), initial_status, prompt,
                json.dumps(compact_record(payload), ensure_ascii=False),
                json.dumps(assets or [], ensure_ascii=False), original_prompt or prompt, parent_task_id,
                json.dumps(compact_record(template or {}), ensure_ascii=False),
            ))

    def begin_call(self, operation: str, request: dict, task_id: str = "", secrets: tuple[str, ...] = ()) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO calls (created_at, operation, task_id, status, request_json) VALUES (?, ?, ?, 'running', ?)",
                (time.time(), operation, task_id, json.dumps(compact_record(request, secrets), ensure_ascii=False)),
            )
            return cursor.lastrowid

    def finish_call(self, call_id: int, *, response=None, error: str = "", uncertain: bool = False,
                    secrets: tuple[str, ...] = ()) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT created_at FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if not row:
                return
            connection.execute(
                "UPDATE calls SET status=?, duration_ms=?, response_json=?, error=?, task_id=CASE WHEN ? != '' THEN ? ELSE task_id END WHERE call_id=?",
                ("unknown" if uncertain else "failed" if error else "succeeded",
                 max(0, round((time.time() - row["created_at"]) * 1000)),
                 json.dumps(compact_record(response, secrets), ensure_ascii=False),
                 compact_record(error, secrets),
                 str(response.get("id", "")) if isinstance(response, dict) else "",
                 str(response.get("id", "")) if isinstance(response, dict) else "", call_id),
            )

    def calls(self, limit: int | None = 500, task_id: str = "") -> list[dict]:
        with self._connect() as connection:
            query = "SELECT * FROM calls"
            arguments = []
            if task_id:
                query += " WHERE task_id=?"
                arguments.append(task_id)
            query += " ORDER BY call_id DESC"
            if limit is not None:
                query += " LIMIT ?"
                arguments.append(limit)
            return [dict(row) for row in connection.execute(query, arguments)]

    def image_snapshots(self):
        """Recover image references from every saved task and call, including imports."""
        def walk(value, created_at):
            if isinstance(value, dict):
                if value.get('kind') == 'image' and value.get('path'):
                    yield value, created_at
                else:
                    for child in value.values(): yield from walk(child, created_at)
            elif isinstance(value, list):
                for child in value: yield from walk(child, created_at)
        with self._connect() as connection:
            rows = connection.execute('''
                SELECT created_at, assets_json AS value FROM tasks
                UNION ALL SELECT created_at, template_json FROM tasks
                UNION ALL SELECT created_at, request_json FROM calls
                UNION ALL SELECT created_at, response_json FROM calls
                ORDER BY created_at
            ''').fetchall()
        for row in rows:
            try: value = json.loads(row['value'])
            except (TypeError, ValueError): continue
            yield from walk(value, row['created_at'])

    def upload_progress(self, call_id, progress):
        """Keep a small live upload receipt; the final response replaces it."""
        with self._connect() as connection:
            connection.execute("UPDATE calls SET response_json=? WHERE call_id=? AND status='running' AND operation='upload_video'",
                (json.dumps(compact_record({'upload_progress':progress}),ensure_ascii=False),call_id))

    def latest_submission(self) -> dict | None:
        """Read submission feedback without loading media bodies or model catalogs."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT call_id, created_at, task_id, status, duration_ms, error,
                CASE WHEN json_valid(response_json) THEN json_object('network_diagnostics', json_object(
                    'reason', substr(json_extract(response_json, '$.network_diagnostics.reason'), 1, 500),
                    'reason_type', json_extract(response_json, '$.network_diagnostics.reason_type'),
                    'error_type', json_extract(response_json, '$.network_diagnostics.error_type'),
                    'errno', json_extract(response_json, '$.network_diagnostics.errno'),
                    'winerror', json_extract(response_json, '$.network_diagnostics.winerror'),
                    'timeout_seconds', json_extract(response_json, '$.network_diagnostics.timeout_seconds'),
                    'elapsed_ms', json_extract(response_json, '$.network_diagnostics.elapsed_ms')
                )) ELSE '{}' END AS response_json FROM calls """
                "WHERE operation='create' ORDER BY call_id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def call_summary(self, call_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT call_id, created_at, operation, task_id, status, duration_ms, error "
                                     "FROM calls WHERE call_id=?", (call_id,)).fetchone()
            return dict(row) if row else None

    def submission_for_task(self, task_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT call_id, created_at, task_id, status, duration_ms, error FROM calls "
                                     "WHERE operation='create' AND task_id=? ORDER BY call_id DESC LIMIT 1", (task_id,)).fetchone()
            return dict(row) if row else None

    def completion_times(self) -> list[dict]:
        """Measured submit-to-success times; polling delays are included in these local observations."""
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT t.model, t.resolution, t.requested_duration,
                       MIN(q.created_at + COALESCE(q.duration_ms, 0)/1000.0) -
                       COALESCE((SELECT MIN(c.created_at) FROM calls c WHERE c.operation='create' AND c.task_id=t.task_id),
                                t.created_at) AS seconds
                FROM tasks t JOIN calls q ON q.task_id=t.task_id AND q.operation='query'
                WHERE t.status='succeeded' AND json_valid(q.response_json)
                      AND json_extract(q.response_json, '$.status')='succeeded'
                GROUP BY t.task_id ORDER BY t.created_at DESC LIMIT 30
            """).fetchall()
            return [dict(row) for row in rows if 0 < row['seconds'] < 7200]

    def task_timing(self, task_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute("""
                SELECT MIN(CASE WHEN operation='create' THEN created_at END) AS submitted_at,
                       MIN(CASE WHEN operation='query' AND json_valid(response_json)
                                    AND json_extract(response_json,'$.status') IN ('succeeded','failed','cancelled','expired')
                                THEN created_at+COALESCE(duration_ms,0)/1000.0 END) AS finished_at,
                       MAX(CASE WHEN operation='query' AND status='succeeded'
                                THEN created_at+COALESCE(duration_ms,0)/1000.0 END) AS checked_at
                FROM calls WHERE task_id=?
            """,(task_id,)).fetchone()
            return dict(row)

    def generation_records(self) -> list[dict]:
        rows = [{**row, "record_kind": "task"} for row in self.rows()]
        with self._connect() as connection:
            latest_operations = connection.execute("""
                SELECT c.task_id, c.operation, c.status, c.error
                FROM calls c JOIN (
                    SELECT MAX(call_id) AS call_id FROM calls
                    WHERE operation IN ('query','download') AND task_id!=''
                    GROUP BY task_id, operation
                ) latest ON c.call_id=latest.call_id
            """).fetchall()
            calls = connection.execute("SELECT * FROM calls WHERE operation='create' AND task_id='' ORDER BY call_id DESC").fetchall()
        operations = {}
        for call in latest_operations:
            operations.setdefault(call['task_id'], {}).update({
                call['operation'] + '_status': call['status'],
                call['operation'] + '_error': call['error'],
            })
        for row in rows:
            row.update(operations.get(row['task_id'], {}))
        for call in calls:
            request = json.loads(call['request_json'])
            payload = request.get('payload') or {}
            roles={item.get('role') for item in payload.get('content',[])}
            mode=('first_last_frame' if 'first_frame' in roles and 'last_frame' in roles else
                  'first_frame' if 'first_frame' in roles else
                  'reference' if any(item.get('type') in {'image_url','video_url','audio_url'} for item in payload.get('content',[])) else 'text')
            rows.append({
                "record_kind": "submission", "call_id": call['call_id'], "task_id": "", "created_at": call['created_at'],
                "status": 'submitting' if call['status']=='running' else call['status'], "error": call['error'],
                "resolution": payload.get('resolution',''), "ratio": payload.get('ratio',''),
                "requested_duration": payload.get('duration'), "local_path": "", "actual_duration": None,
                "original_prompt": request.get('original_prompt') or next((i.get('text','') for i in payload.get('content',[]) if i.get('type')=='text'),''),
                "assets_json": json.dumps(request.get('assets',[]),ensure_ascii=False),
                "request_json": json.dumps(payload,ensure_ascii=False), "parent_task_id": request.get('parent_task_id',''),
                "mode": mode, "template_json": '{}',
            })
        return sorted(rows,key=lambda row: row['created_at'],reverse=True)

    def prompt_records(self) -> list[dict]:
        with self._connect() as connection:
            calls = connection.execute("SELECT * FROM calls WHERE operation IN ('draft','refine') "
                                       "AND status='succeeded' ORDER BY call_id DESC").fetchall()
        records=[]
        for call in calls:
            response=json.loads(call['response_json'])
            if not isinstance(response,dict):
                continue
            draft=response.get('draft',response)
            if not isinstance(draft,dict) or not draft.get('prompt'):
                continue
            request=json.loads(call['request_json'])
            records.append({"record_kind":"prompt", "call_id":call['call_id'], "task_id":call['task_id'],
                            "parent_task_id":call['task_id'], "created_at":call['created_at'], "status":"draft",
                            "original_prompt":draft['prompt'], "source_brief":request.get('creative_brief',''),
                            "settings":request.get('generation_settings',{}), "assets":request.get('assets',[]),
                            "draft":draft, "frames":response.get('frames',[]), "local_path":""})
        return records

    def interrupt_call(self, call_id: int, message: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE calls SET status='unknown', error=? WHERE call_id=? AND status='running'",
                               (compact_record(message), call_id))

    def update(self, task: dict) -> None:
        task_id = str(task.get("id", ""))
        if not task_id:
            return
        usage = task.get("usage") if isinstance(task.get("usage"), dict) else {}
        completion = usage.get("completion_tokens")
        total = usage.get("total_tokens")
        content = task.get("content") if isinstance(task.get("content"), dict) else {}
        with self._connect() as connection:
            row = connection.execute("SELECT model FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return
            cost = estimated_cost_usd(task, row["model"])
            actual_duration = task.get("duration")
            if not isinstance(actual_duration, (int, float)) or isinstance(actual_duration, bool) or actual_duration < 0:
                actual_duration = None
            connection.execute("""
                UPDATE tasks SET status=?, actual_duration=COALESCE(?, actual_duration),
                    completion_tokens=COALESCE(?, completion_tokens), total_tokens=COALESCE(?, total_tokens),
                    estimated_cost_usd=COALESCE(?, estimated_cost_usd),
                    video_url=CASE WHEN ? != '' THEN ? ELSE video_url END, error=?
                WHERE task_id=?
            """, (
                str(task.get("status", "unknown")), actual_duration,
                completion if isinstance(completion, int) else None,
                total if isinstance(total, int) else None,
                cost, str(content.get("video_url", "")), str(content.get("video_url", "")),
                json.dumps(compact_record(task['error']), ensure_ascii=False) if isinstance(task.get('error'), (dict,list)) else
                str(compact_record(task.get('error') or '')), task_id,
            ))

    def rows(self) -> list[dict]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM tasks ORDER BY created_at DESC, task_id DESC")]

    def set_local_path(self, task_id: str, path: Path) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE tasks SET local_path=? WHERE task_id=?", (str(path), task_id))

    def set_media_info(self, task_id: str, info: dict) -> None:
        duration = info.get("duration")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
            duration = None
        with self._connect() as connection:
            connection.execute("UPDATE tasks SET media_json=?, actual_duration=COALESCE(actual_duration, ?) WHERE task_id=?",
                               (json.dumps(info, ensure_ascii=False), duration, task_id))

    def summary(self) -> dict:
        with self._connect() as connection:
            row = connection.execute("""
                SELECT COUNT(*) AS task_count,
                    SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS video_count,
                    SUM(CASE WHEN status='succeeded' THEN COALESCE(actual_duration, 0) ELSE 0 END) AS seconds,
                    SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,
                    SUM(COALESCE(total_tokens, 0)) AS total_tokens,
                    SUM(COALESCE(estimated_cost_usd, 0)) AS estimated_cost_usd
                FROM tasks
            """).fetchone()
            return dict(row)
