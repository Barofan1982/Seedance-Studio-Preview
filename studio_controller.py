"""Independent Studio state and jobs, using the existing Seedance/ChatGPT clients."""
from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
import traceback
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from chatgpt_auth import ChatGPTSession
from history_store import HistoryStore
from media_assets import MediaAsset, add_local_asset, add_url_asset, audio_data_url, compiled_mentions, image_data_url, video_metadata
from openai_prompt import DEFAULT_MODEL, OpenAIPromptClient
from agent_platforms import PLATFORMS, PlatformAccounts
from agent_prompt import AgentPromptClient
from request_records import compact_record
from secure_store import load_key, load_openai_key, save_key, save_openai_key, load_protected_json, save_protected_json
from seedance_client import DEFAULT_BASE_URL, CREATE_TIMEOUT_SECONDS, SeedanceClient, SubmissionUncertain, build_payload, download_video, model_alias
from error_diagnostics import exception_diagnostics
from video_analysis import analysis_assets, analysis_signature, choose_keyframes, frame_catalog, keyframe_assets, needs_video_analysis, reference_videos
from ui_language import error_text
from billing_cny import DEFAULT_CNY_RATES, estimate_cny
from generation_status import STATUS_NAMES
from video_upload import UploadSession, needs_upload, source_expired
from upload_library import UploadLibrary, record_status, local_copy
from image_library import ImageLibrary, image_copy, image_status
from generation_queue import GenerationQueue, ACTIVE as QUEUE_ACTIVE
from reasoning_settings import EFFORT_LABELS, supported_efforts, selected_effort
from prompt_policy import cleanup_reference, frames_for_generation, strip_analysis_references, enforce_edit_requirements, edit_requirements
from workspace_snapshots import WorkspaceSnapshots, CONTENT_DEFAULTS, SNAPSHOT_KEYS, has_content

from app_paths import APP_DIR, DEFAULT_OUTPUT_DIR, open_error_log
MODES = {"全模态参考": "reference", "首帧图片": "first_frame", "首尾帧图片": "first_last_frame", "纯文本": "text"}
TASK_TYPES = {"参考生视频": "reference"}
PURPOSE = {"competitor": "视频", "character": "角色", "scene": "场景", "first": "首帧", "last": "尾帧", "audio": "音频", "other": "参考", "keyframe": "关键帧"}
DEFAULTS = {
    "base_url": DEFAULT_BASE_URL, "mode": "全模态参考", "task_type": "参考生视频",
    "resolution": "720p", "duration": "5", "ratio": "16:9", "generate_audio": True,
    "watermark": False, "return_last_frame": False, "output_format": "mp4",
    "download_dir": str(DEFAULT_OUTPUT_DIR), "openai_model": DEFAULT_MODEL,
    "ai_provider": "chatgpt", "ai_models": {}, "ai_reasoning_efforts": {},
    "transcribe_audio": False, "manual_prompt": False, "auto_analyze": False, "allow_http_for": "",
    "brief": "", "ai_original_text": "",
    "prompt": "", "analysis": "", "prompt_draft": {}, "task_id": "",
    "iteration_task_id": "", "review_output": False,
    "frame_catalog": [], "generated_keyframe_binding": "", "use_keyframes_for_generation": False,
    "cny_rates": DEFAULT_CNY_RATES,
    "removed_assets": [],
}


def manifest(assets: list[MediaAsset]) -> list[dict]:
    upload_fields = {'source_expires_at','upload_id','upload_file_size','upload_file_mtime_ns'}
    return [{**{key:value for key,value in asdict(a).items() if key not in upload_fields or value},
             "path": str(a.path) if a.path else ""} for a in assets]


def neutral_video_text(text: str) -> str:
    """Use neutral wording in displayed progress, errors and cached analysis."""
    legacy = "\u7ade\u54c1"
    return str(text).replace(legacy + "短视频", "参考视频").replace(legacy + "视频", "参考视频").replace(legacy + "素材", "视频素材").replace(legacy, "参考视频")


def load_assets(values: list[dict]) -> list[MediaAsset]:
    return [MediaAsset(Path(a["path"]) if a.get("path") else None, a["kind"], a["purpose"],
                       a["handle"], a.get("source_url", ""), a.get("duration"), a.get("selected", True),
                       a.get('source_expires_at',''), a.get('upload_id',''),
                       a.get('upload_file_size',0), a.get('upload_file_mtime_ns',0))
            for a in values]


def generation_settings(state: dict, assets: list[MediaAsset]) -> dict:
    return {
        "model": model_alias(state["resolution"], any(a.selected and a.kind == "video" and a.purpose != "competitor" for a in assets)),
        "mode": MODES[state["mode"]], "task_type": "reference",
        **{key: state[key] for key in ("resolution", "ratio", "generate_audio", "watermark", "output_format", "return_last_frame")},
        "duration": int(state["duration"]),
    }


def prompt_for_submission(state: dict) -> str:
    return str(state.get("prompt", "")).strip() or str(state.get("brief", "")).strip()


def review_summary(state: dict, assets: list[MediaAsset]) -> str:
    draft = state.get("prompt_draft") or {}
    if not draft:
        return "智能优化后，这里只显示保留内容、角色对应关系和需要确认的问题。"
    if (draft.get("prompt", "").strip() != prompt_for_submission(state)
            or draft.get("source_assets", manifest(analysis_assets(assets))) != manifest(analysis_assets(assets))):
        return "创作内容或素材已修改，上次核对结果已过期。需要重新核对时，点击智能优化。"
    analysis = neutral_video_text(str(draft.get("analysis", "")).strip())
    if len(analysis) > 180:
        analysis = analysis[:177].rstrip() + "…"
    questions = [neutral_video_text(str(item).strip()) for item in draft.get("uncertainties", [])[:3]]
    questions = [item if len(item) <= 100 else item[:97].rstrip() + "…" for item in questions if item]
    receipt = f"已按每秒一帧分析 {draft['frame_count']} 张画面。\n\n" if draft.get("frame_count") else ""
    return receipt + analysis + ("\n\n需确认：\n" + "\n".join("• " + item for item in questions) if questions else "")


def collect_payload(state: dict, assets: list[MediaAsset]) -> dict:
    settings = generation_settings(state, assets)
    original = prompt_for_submission(state)
    if not frames_for_generation(state):
        original = strip_analysis_references(original, state.get('frame_catalog', []), assets,
                                             state.get('generated_keyframe_binding', ''))
        assets = analysis_assets(assets)
    competitor = next((a for a in assets if a.purpose == "competitor"), None)
    if any(needs_upload(a) for a in assets):
        raise ValueError("本地视频需要上传，或参考地址已过期、文件已变化；点击生成将先上传，也可在素材菜单选择上传视频。")
    if needs_video_analysis(state, assets):
        draft = state.get("prompt_draft") or {}
        if draft.get("source_assets") is not None and draft["source_assets"] != manifest(analysis_assets(assets)):
            raise ValueError("已开启生成前自动分析，素材已变化，请重新抽帧并优化，或在更多参数关闭自动分析。")
        raise ValueError("已开启自动分析，先抽帧并优化提示词；也可在更多参数关闭自动分析后直接生成。")
    if competitor and competitor.handle in original:
        raise ValueError("此视频当前仅用于分析；请在素材菜单选择“上传并用于生成”，再引用视频文件名。")
    selected = [a for a in assets if a.selected and a.purpose != "competitor"]
    mode = settings["mode"]
    if mode == "reference" and any(a.purpose in {"first", "last"} for a in selected):
        raise ValueError("全模态参考不能混入首帧或尾帧，请修改素材用途。")
    if mode in {"first_frame", "first_last_frame"} and any(a.purpose not in {"first", "last"} for a in selected):
        raise ValueError("首帧方式只能选中首帧、尾帧图片。")
    if mode == "text" and selected:
        raise ValueError("文生视频不能勾选参考素材。")
    if mode in {"first_frame", "first_last_frame"}:
        selected.sort(key=lambda a: 0 if a.purpose == "first" else 1)
    compiled, images, videos, audios = compiled_mentions(original, selected)
    if any(not a.source_url for a in videos):
        raise ValueError("参考视频尚未上传，请在素材菜单上传视频或关联视频网址。")
    if sum(a.duration or 0 for a in videos) > 30 or sum(a.duration or 0 for a in audios) > 30:
        raise ValueError("参考视频或音频的总时长不能超过 30 秒。")
    first = [a for a in images if a.purpose == "first"]
    last = [a for a in images if a.purpose == "last"]
    if mode == "first_frame" and (len(first) != 1 or last):
        raise ValueError("首帧方式需要一张首帧图片。")
    if mode == "first_last_frame" and (len(first) != 1 or len(last) != 1):
        raise ValueError("首尾帧方式需要各一张首帧、尾帧图片。")
    source = lambda a: image_data_url(a.path) if a.path else a.source_url
    payload = build_payload(
        prompt=compiled, **{k: settings[k] for k in ("duration", "resolution", "ratio", "mode", "task_type",
                                                     "generate_audio", "watermark", "output_format", "return_last_frame")},
        image_sources=[source(a) for a in images if a.purpose not in {"first", "last"}],
        video_sources=[a.source_url for a in videos],
        audio_sources=[audio_data_url(a.path) if a.path else a.source_url for a in audios],
        first_frame=source(first[0]) if first else "", last_frame=source(last[0]) if last else "",
    )
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 64 * 1024 * 1024:
        raise ValueError("参考素材超过请求容量，请减少素材。")
    return payload


def structured_prompt(original: str, assets: list[MediaAsset], state: dict) -> tuple[str, int]:
    bindings = "\n".join(f"{PURPOSE.get(a.purpose, '参考')}：{a.handle}" for a in assets if a.selected and a.kind == "image")
    sections = [
        ("视频目标", "描述要完成的玩法、叙事或广告动作。"), ("角色与场景", bindings or "描述主角与场景。"),
        ("全局风格", "光线、色调、材质与构图。"),
        ("时间轴分镜", "按自适应时长安排镜头。" if str(state["duration"]) == "-1" else f"按 {state['duration']} 秒安排镜头与动作。"),
        ("运镜与动作", "镜头运动、动作顺序与转场。"),
        ("声音与台词", "配乐、音效与台词。" if state["generate_audio"] else "无声视频。"),
        ("连续性约束", "保持角色、玩法布局和动作衔接一致。"),
    ]
    headings = re.findall(r"(?m)^\s*【([^】]+)】", original)
    missing = [(t, b) for t, b in sections if not any(h.startswith(t) for h in headings)]
    if not missing:
        return original, 0
    if len(missing) == len(sections):
        if original.strip():
            missing[0] = ("视频目标", original.strip())
        return "\n\n".join(f"【{t}】\n{b}" for t, b in missing), len(missing)
    return original.rstrip() + "\n\n" + "\n\n".join(f"【{t}】\n{b}" for t, b in missing), len(missing)


class StudioController(QObject):
    changed = Signal(str)
    status_changed = Signal(str)
    busy_changed = Signal(bool)
    failed = Signal(str)
    result_ready = Signal(dict)
    _finished = Signal(object)
    _progress = Signal(str)
    _upload_event = Signal(dict)

    def __init__(self, directory: Path = APP_DIR, *, auth=None, history=None, upload_session=None):
        super().__init__()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_file = self.directory / "state.json"
        try:
            saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.state = {**DEFAULTS, **saved}
        self.state['ai_models'] = deepcopy({provider: model for provider, model in (saved.get('ai_models') or {}).items()
                                           if provider in PLATFORMS and provider != 'chatgpt'})
        preferences = saved.get('ai_reasoning_efforts') or {}
        self.state['ai_reasoning_efforts'] = {
            provider: {model: effort for model, effort in values.items()
                       if isinstance(model, str) and isinstance(effort, str) and effort in EFFORT_LABELS}
            for provider, values in (preferences.items() if isinstance(preferences, dict) else [])
            if provider in PLATFORMS and isinstance(values, dict)}
        if self.state['ai_provider'] not in PLATFORMS:
            self.state['ai_provider'] = 'chatgpt'
        self.state["task_type"] = "参考生视频"
        self.state["duration"] = str(self.state["duration"])
        self.assets = load_assets(saved.get("assets", []))
        self.sync_keyframe_policy()
        self.api_key = load_key()
        self.openai_key = load_openai_key()
        self.auth = auth or ChatGPTSession(self.directory / "chatgpt_accounts.bin")
        self.platform_accounts = PlatformAccounts(self.directory)
        self.model_cache = {}
        self.upload_auth = upload_session or UploadSession(self.directory / 'video_upload_account.bin')
        self.upload_cache_file = self.directory / 'video_upload_cache.bin'
        self.upload_cache = load_protected_json(self.upload_cache_file)
        for asset in self.assets:
            if asset.path and asset.upload_id:
                key = self._video_cache_key(asset.path)
                cached = self.upload_cache.get(key)
                if not cached or source_expired(load_assets([cached])[0]):
                    self.upload_cache[key] = manifest([asset])[0]
        if self.upload_cache:
            save_protected_json(self.upload_cache_file,self.upload_cache)
        self.upload_library = UploadLibrary(self.directory / 'video_upload_library.bin')
        for uploaded in self.upload_cache.values():
            self.upload_library.remember(uploaded)
        for uploaded in manifest(self.assets):
            self.upload_library.remember(uploaded)
        self.upload_library.save()
        self.history = history or HistoryStore(self.directory / "history.sqlite3")
        self.workspace_snapshots = WorkspaceSnapshots(self.directory)
        self.queue = GenerationQueue(self.directory / 'generation_queue.sqlite3')
        self.queue.recover(self.history)
        self.queue_enabled = False
        self.queue_auto_submission = False
        self.queue_reason = '重新打开后，请点击继续队列。' if self.queue.rows() else ''
        self.image_library = ImageLibrary(self.directory)
        if not self.image_library.history_imported:
            for image, created_at in self.history.image_snapshots():
                self.image_library.remember(image, added_at=created_at)
            self.image_library.history_imported = True
        for removed in self.state.get('removed_assets', []):
            self.image_library.remember(removed['asset'], added_at=removed.get('removed_at'))
        for image in manifest(self.assets):
            self.image_library.remember(image)
        self.image_library.save()
        self.models: list[dict] = []
        self.busy = False
        self.query_busy = False
        self.closed = False
        self.active_call: int | None = None
        self.query_call: int | None = None
        self.status = "准备就绪"
        self._query_failures = 0
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self.save)
        self._poll = QTimer(self)
        self._poll.setSingleShot(True)
        self._poll.timeout.connect(self.query)
        self._queue_timer = QTimer(self)
        self._queue_timer.setSingleShot(True)
        self._queue_timer.timeout.connect(self.dispatch_queue)
        self._finished.connect(self._job_finished)
        self._progress.connect(self.set_status)
        self.upload_feedback = {'active':False}
        self._upload_event.connect(self._receive_upload_event)
        self._upload_timer = QTimer(self)
        self._upload_timer.setInterval(1000)
        self._upload_timer.timeout.connect(self.refresh_upload_feedback)
        self._upload_recorded_at = 0
        self._expiry_snapshot = self.upload_availability()
        self._expiry_timer = QTimer(self)
        self._expiry_timer.setInterval(1000)
        self._expiry_timer.timeout.connect(self.refresh_upload_expiry)
        self._expiry_timer.start()

    def set_status(self, value: str):
        self.status = neutral_video_text(compact_record(value, (self.api_key, self.openai_key)))
        self.status_changed.emit(self.status)

    def set_value(self, name: str, value):
        if name == "task_type":
            value = "参考生视频"
        if self.state.get(name) == value:
            return
        self.state[name] = value
        if name == "mode":
            mode = MODES[value]
            if mode != "reference":
                self.state["task_type"] = "参考生视频"
            if mode in {"first_frame", "first_last_frame"}:
                self.state["ratio"] = "adaptive"
            for a in self.assets:
                if a.purpose == "competitor":
                    continue
                allowed = mode == "reference" and a.purpose not in {"first", "last"} or mode == "first_frame" and a.purpose == "first" or mode == "first_last_frame" and a.purpose in {"first", "last"}
                if not allowed:
                    a.selected = False
            self.changed.emit("assets")
        self._save_timer.start(450)
        self.changed.emit("parameters" if name not in {"brief", "prompt"} else "text")

    def save(self):
        self._save_timer.stop()
        data = {**self.state, "assets": manifest(self.assets)}
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_file)
        self.changed.emit("saved")

    def start_job(self, operation, request, action, success, *, task_id="", failure=None, background=False, on_started=None, extra_secrets=()):
        if self.busy or self.closed or background and self.query_busy:
            return False
        secrets = (self.api_key, self.openai_key, self.platform_accounts.sessions['deepseek'].record.get('api_key', ''), *extra_secrets)
        try:
            call_id = self.history.begin_call(operation, request, task_id, secrets)
            if on_started:
                on_started(call_id)
        except Exception as error:
            if 'call_id' in locals():
                self.history.finish_call(call_id,error=f'本地提交记录初始化失败，请求未发送：{error}',secrets=secrets)
            self.failed.emit(f"调用记录保存失败，请求未发送：{error}")
            return False
        if background:
            self.query_call = call_id
            self.query_busy = True
        else:
            self.active_call = call_id
            self.last_error_call_id = None
            self.busy = True
            self.busy_changed.emit(True)

        def worker():
            value = None
            error = None
            try:
                value = action()
            except Exception as caught:
                error = caught
            try:
                response = {"id": value} if operation == "create" and isinstance(value, str) else value
                if error:
                    diagnostics = exception_diagnostics(error, operation, secrets=secrets)
                    key = 'network_diagnostics' if isinstance(error, SubmissionUncertain) else 'error_diagnostics'
                    response = {key: diagnostics}
                    self.write_error(f"调用 #{call_id} {operation} 网络诊断：" + json.dumps(response, ensure_ascii=False))
                audit_error = str(error) if error else (value.get('error','') if operation == 'upload_video' and isinstance(value,dict) else '')
                self.history.finish_call(call_id, response=response, error=audit_error,
                                         uncertain=isinstance(error, SubmissionUncertain), secrets=secrets)
            except Exception:
                self.write_error(traceback.format_exc())
            if not self.closed:
                self._finished.emit((call_id, background, success, failure, value, error))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def workspace_has_content(self):
        return has_content(self.state, self.assets)

    def _workspace_materials(self):
        assets = manifest(self.assets)
        for asset in assets:
            if asset['kind'] == 'image' and asset.get('path') and not Path(asset['path']).is_file():
                cached = self.image_library.find_asset(asset)
                if cached and image_copy(cached):
                    asset['path'] = str(image_copy(cached))
        return assets

    def _replace_workspace(self, state, assets, *, analysis_current=False):
        old_state, old_assets = deepcopy(self.state), self.assets
        try:
            self.state.update(deepcopy(CONTENT_DEFAULTS))
            self.state.update({key: deepcopy(value) for key, value in state.items() if key in SNAPSHOT_KEYS})
            self.state['removed_assets'] = []
            self.assets = load_assets(assets)
            self.sync_keyframe_policy()
            if self.state.get('prompt_draft') and analysis_current:
                self.state['prompt_draft']['source_assets'] = manifest(analysis_assets(self.assets))
                self.state['prompt_draft']['analysis_signature'] = analysis_signature(self.assets)
            self.save()
        except Exception:
            self.state, self.assets = old_state, old_assets
            raise
        self.changed.emit('all')
        self.changed.emit('workspace_snapshots')

    def clear_workspace(self):
        """Called only after the UI confirms; never mutate before backup succeeds."""
        if self.busy or self.closed:
            raise ValueError('请等待当前导入、上传或分析操作完成，再清空创作。')
        if not self.workspace_has_content():
            raise ValueError('当前项目为空，无需清空。')
        state, assets = deepcopy(self.state), self._workspace_materials()
        analysis_current = (state.get('prompt_draft') or {}).get('analysis_signature') == analysis_signature(self.assets)

        def action():
            identity = self.workspace_snapshots.save(state, assets, progress=self._progress.emit, analysis_current=analysis_current)
            return {'snapshot_id': identity}

        def received(result):
            try:
                self._replace_workspace({}, [])
            except Exception as error:
                self.set_status('清空未完成，当前创作仍保留，恢复快照已保存。')
                raise ValueError('清空未完成，当前创作仍保留；快照已保存：' + str(error)) from error
            self.set_status('已保存创作快照并清空，可在“历史记录 → 创作快照”恢复。')

        def failure(error):
            self.set_status('保存未完成，当前创作未清空。')
            self.failed.emit('保存未完成，当前创作未清空：' + str(error))

        self.set_status('正在保存创作快照，保存成功后清空…')
        return self.start_job('clear_workspace', {'asset_count': len(assets)}, action, received, failure=failure)

    def restore_workspace(self, identity, success=None):
        if self.busy or self.closed:
            raise ValueError('请等待当前操作完成，再恢复创作。')
        state, assets = deepcopy(self.state), self._workspace_materials()
        analysis_current = (state.get('prompt_draft') or {}).get('analysis_signature') == analysis_signature(self.assets)

        def action():
            record = self.workspace_snapshots.get(identity)
            warnings = self.workspace_snapshots.warnings(record, verify=True)
            if has_content(state, assets):
                self.workspace_snapshots.save(state, assets, reason='恢复前保存', progress=self._progress.emit, analysis_current=analysis_current)
            return {'record': record, 'warnings': warnings}

        def received(result):
            record = result['record']
            self._replace_workspace(record['state'], record['assets'], analysis_current=record.get('analysis_current', False))
            self.set_status('已恢复创作快照。' + ('部分素材需重新关联或上传，请查看快照提醒。' if result['warnings'] else '可继续编辑；尚未提交生成。'))
            if success:
                success()

        self.set_status('正在核对并恢复创作快照…')
        return self.start_job('restore_workspace', {'snapshot_id': identity}, action, received)

    @Slot(object)
    def _job_finished(self, packet):
        if self.closed:
            return
        call_id, background, success, failure, value, error = packet
        if background:
            self.query_busy = False
            self.query_call = None
        else:
            if self.upload_feedback.get('active'):
                self._upload_timer.stop()
                self.upload_feedback['active'] = False
                self.changed.emit('upload_progress')
            self.busy = False
            self.active_call = None
            self.busy_changed.emit(False)
        if error or isinstance(value, dict) and (value.get('error') or value.get('status') == 'failed'):
            self.last_error_call_id = call_id
        self.changed.emit("history")
        if error:
            detail = str(compact_record(str(error), (self.api_key, self.openai_key)))
            self.write_error(detail)
            if failure:
                failure(error)
            else:
                self.set_status(error_text(detail))
                self.failed.emit(detail)
            self.schedule_queue()
            return
        try:
            success(value)
        except Exception as caught:
            self.write_error(traceback.format_exc())
            self.failed.emit(str(caught))
        self.sync_queue()
        self.schedule_queue()

    def write_error(self, text):
        output, _path = open_error_log(self.directory)
        if output is None:
            return
        try:
            with output:
                output.write(str(compact_record(text, (self.api_key, self.openai_key))) + "\n")
        except OSError:
            pass

    def import_files(self, paths: list[str], purpose: str | None = None):
        existing = [replace(a) for a in self.assets]

        def action():
            additions = []
            errors = []
            for path in paths:
                suffix = Path(path).suffix.lower()
                role = purpose or ("competitor" if suffix in {".mp4", ".mov", ".m4v", ".webm"} else "audio" if suffix in {".wav", ".mp3"} else "character" if not any(a.kind == "image" for a in existing + additions) else "other")
                try:
                    asset = add_local_asset(path, role, existing + additions)
                    if asset.kind == 'video':
                        asset.purpose, asset.selected = 'other', True
                        self.restore_uploaded_url(asset)
                    additions.append(asset)
                except Exception as error:
                    errors.append(f"{Path(path).name}：{error}")
            return {"assets": manifest(additions), "errors": errors}

        def received(result):
            additions = load_assets(result["assets"])
            self.assets.extend(additions)
            self.remember_images(additions)
            self.save()
            self.changed.emit("assets")
            self.set_status(f"已导入 {len(additions)} 份素材")
            if result["errors"]:
                self.failed.emit("\n".join(result["errors"]))

        self.set_status("正在导入素材…")
        self.start_job("import", {"files": paths, "purpose": purpose}, action, received)

    def add_url(self, url, kind, duration=None):
        self.assets.append(add_url_asset(url, kind, self.assets, duration=duration))
        self.remember_images([self.assets[-1]])
        self.save()
        self.changed.emit("assets")

    def update_asset(self, index: int, *, selected=None, purpose=None, remove=False):
        asset = self.assets[index]
        if remove:
            self.remember_images([asset])
            self.state['removed_assets'] = (self.state.get('removed_assets',[]) +
                [{'asset':manifest([asset])[0],'removed_at':time.time()}])[-20:]
        if asset.purpose == "keyframe" and (remove or selected is not None) and not purpose:
            handles = [a.handle for a in self.assets if a.purpose == "keyframe" and a.selected and a is not asset]
            if selected and not remove:
                handles.append(asset.handle)
            self.apply_keyframes(handles)
            return
        if remove:
            self.assets.pop(index)
        else:
            asset = self.assets[index]
            if selected is not None:
                asset.selected = bool(selected)
            if purpose:
                asset.purpose = purpose
                self.remember_images([asset])
        self.save()
        self.changed.emit("assets")

    @staticmethod
    def _video_cache_key(path):
        return os.path.normcase(os.path.abspath(path))

    def restore_uploaded_url(self, asset):
        if asset.kind != 'video' or not asset.path or not asset.path.is_file(): return
        saved = self.upload_cache.get(self._video_cache_key(asset.path))
        if not saved: return
        stat = asset.path.stat()
        if [stat.st_size,stat.st_mtime_ns] != [saved.get('upload_file_size'),saved.get('upload_file_mtime_ns')]: return
        candidate = load_assets([saved])[0]
        if source_expired(candidate): return
        for key in ('source_url','source_expires_at','upload_id','upload_file_size','upload_file_mtime_ns'):
            setattr(asset,key,getattr(candidate,key))

    def restore_removed_asset(self):
        if self.busy or self.closed: return
        removed = list(self.state.get('removed_assets',[]))
        if not removed:
            raise ValueError('没有可恢复的移除记录；可以重新导入本地文件。')
        asset = load_assets([removed[-1]['asset']])[0]
        if asset.kind == 'image' and asset.path and not asset.path.is_file():
            cached = self.image_library.find_asset(removed[-1]['asset'])
            if cached and image_copy(cached): asset.path = image_copy(cached)
        if asset.path and not asset.path.is_file() and not asset.source_url:
            raise ValueError('本地素材文件不存在，请重新选择文件。')
        self.restore_uploaded_url(asset)
        self.state['removed_assets'] = removed[:-1]
        if not any(existing.handle == asset.handle for existing in self.assets):
            mode = MODES[self.state['mode']]
            allowed = ((mode == 'reference' and asset.purpose not in {'first','last'})
                or (mode == 'first_frame' and asset.purpose == 'first')
                or (mode == 'first_last_frame' and asset.purpose in {'first','last'}))
            asset.selected = asset.selected and allowed
            self.assets.append(asset)
        if asset.purpose == 'keyframe' and self.state.get('frame_catalog'):
            self.apply_keyframes([a.handle for a in self.assets if a.purpose == 'keyframe' and a.selected],notify=False)
        self.save()
        self.changed.emit('assets')
        self.set_status('已恢复素材：' + asset.filename + ('，已复用上传地址' if asset.upload_id and not needs_upload(asset) else ''))

    def remember_images(self, assets):
        images = [asset for asset in assets if asset.kind == 'image']
        if not images: return
        for image in manifest(images): self.image_library.remember(image)
        self.image_library.save()
        self.changed.emit('image_library')

    def add_library_images(self, identities, success=None):
        if self.busy or self.closed: return
        records = [self.image_library.get(identity) for identity in dict.fromkeys(identities)]
        existing = [replace(asset) for asset in self.assets]
        existing_ids = set()
        for asset in manifest(existing):
            if asset['kind'] != 'image': continue
            saved = self.image_library.find_asset(asset)
            if saved and self.image_library.matches_current(asset, saved): existing_ids.add(saved['library_id'])
        def prepare():
            additions, errors = [], []
            for record in records:
                name = record.get('filename', '图片')
                try:
                    if image_status(record)[0] != 'available':
                        raise ValueError('原文件和本地副本均不存在，请重新导入。')
                    saved = image_copy(record)
                    # Do not add a second attachment for the same saved image.
                    if record['library_id'] in existing_ids or any(a.kind == 'image' and (a.path == saved if saved else a.source_url == record.get('source_url'))
                           for a in existing + additions): continue
                    role = record.get('purpose', 'other')
                    if role == 'keyframe': role = 'other'
                    if saved:
                        asset = add_local_asset(saved, role, existing + additions)
                    else:
                        asset = add_url_asset(record['source_url'], 'image', existing + additions)
                        asset.purpose = role
                    preferred = record.get('handle', asset.handle)
                    if not any(a.handle == preferred for a in existing + additions): asset.handle = preferred
                    additions.append(asset)
                except Exception as error: errors.append(name + '：' + str(error))
            return {'assets': manifest(additions), 'errors': errors}
        def received(result):
            additions = load_assets(result['assets'])
            mode = MODES[self.state['mode']]
            selected_images = sum(a.selected and a.kind == 'image' for a in self.assets)
            first_taken = any(a.selected and a.purpose == 'first' for a in self.assets)
            last_taken = any(a.selected and a.purpose == 'last' for a in self.assets)
            for image in additions:
                image.selected = ((mode == 'reference' and image.purpose not in {'first','last'} and selected_images < 30)
                    or (mode in {'first_frame','first_last_frame'} and image.purpose == 'first' and not first_taken)
                    or (mode == 'first_last_frame' and image.purpose == 'last' and not last_taken))
                selected_images += int(image.selected)
                first_taken |= image.selected and image.purpose == 'first'
                last_taken |= image.selected and image.purpose == 'last'
            self.assets.extend(additions)
            self.save()
            self.changed.emit('assets')
            unselected = sum(not a.selected for a in additions)
            self.set_status(f'已从素材库添加 {len(additions)} 张图片' + (f'，其中 {unselected} 张未勾选' if unselected else ''))
            if result['errors']: self.failed.emit('\n'.join(result['errors']))
            if success: success()
        self.start_job('restore_images', {'library_ids': list(dict.fromkeys(identities))}, prepare, received)

    def upload_availability(self):
        return tuple((row['upload_id'], record_status(row)[0]) for row in self.upload_library.rows())

    def refresh_upload_expiry(self):
        current = self.upload_availability()
        if current != self._expiry_snapshot:
            self._expiry_snapshot = current
            self.changed.emit('uploaded_library')
            self.changed.emit('assets')

    def add_uploaded_asset(self, identity, *, renew=False):
        if self.busy or self.closed:
            raise ValueError('请等待当前操作完成。')
        record = self.upload_library.get(identity)
        if not renew and record_status(record)[0] != 'available':
            raise ValueError('此视频地址已过期或即将到期，不能调用；请先重新上传。')
        if MODES[self.state['mode']] != 'reference':
            raise ValueError('视频参考需要全模态参考方式，请先修改生成方式。')
        path = local_copy(record)
        if renew:
            path = Path(record['path']) if record.get('path') else None
            if not path or not path.is_file():
                raise ValueError('本地视频已移动或删除，请重新导入视频后上传。')
        current = next((a for a in self.assets if a.upload_id == identity or (path and a.path == path)), None)
        selected = [a for a in self.assets if a is not current and a.selected and a.kind == 'video' and a.purpose != 'competitor']
        if len(selected) >= 10 or sum(a.duration or 0 for a in selected) + (record.get('duration') or 0) > 30:
            raise ValueError('参考视频最多 10 个，总时长不能超过 30 秒；请先取消勾选多余视频。')
        if current is None:
            candidate = load_assets([record])[0]
            candidate.path = path
            candidate.purpose, candidate.selected = 'other', True
            original = candidate.handle
            number = 2
            while any(a.handle == candidate.handle for a in self.assets):
                name = original.removeprefix('@{').removesuffix('}')
                candidate.handle = f'@{{{Path(name).stem} ({number}){Path(name).suffix}}}'
                number += 1
            self.assets.append(candidate)
            current = candidate
        else:
            for key in ('source_url','source_expires_at','upload_id','upload_file_size','upload_file_mtime_ns','duration'):
                setattr(current,key,record.get(key))
            current.path, current.purpose, current.selected = path, 'other', True
        self.save()
        self.changed.emit('assets')
        self.set_status('已从素材库添加：' + current.filename)
        return self.assets.index(current)

    def link_local_video(self, index, path):
        asset = self.assets[index]
        candidate = add_local_asset(path, "competitor", [])
        if asset.path != candidate.path:
            asset.source_url, asset.source_expires_at, asset.upload_id = '', '', ''
            asset.upload_file_size = asset.upload_file_mtime_ns = 0
        asset.path, asset.duration = candidate.path, candidate.duration
        self.save()
        self.changed.emit("assets")
        self.set_status("已关联本地视频，智能分析将使用本地文件")

    def link_video_url(self, index, url):
        asset = self.assets[index]
        candidate = add_url_asset(url, "video", [], duration=asset.duration)
        asset.source_url = candidate.source_url
        asset.source_expires_at, asset.upload_id = '', ''
        asset.upload_file_size = asset.upload_file_mtime_ns = 0
        asset.purpose, asset.selected = "other", True
        self.save()
        self.changed.emit("assets")

    def sign_in_upload(self, username, password, success=None):
        def received(account):
            self.changed.emit('upload_account')
            self.set_status('中转账号已登录，可上传本地视频')
            if success: success()
        self.set_status('正在登录视频上传账号…')
        # Credentials only live in the action closure and the HTTPS request.
        self.start_job('upload_login', {'service':'视频上传中转'},
                       lambda: self.upload_auth.login(username,password),received)

    def sign_out_upload(self):
        if self.busy: return
        self.upload_auth.logout()
        self.changed.emit('upload_account')
        self.set_status('视频上传账号已退出')

    def pending_video_uploads(self):
        return [index for index,asset in enumerate(self.assets) if needs_upload(asset)]

    def upload_videos(self, indices, success=None):
        if self.busy or self.closed: return
        targets = [(self.assets[index], replace(self.assets[index])) for index in dict.fromkeys(indices)]
        if not targets: return
        videos = [asset for asset in self.assets if asset.kind == 'video' and asset.selected and asset.purpose != 'competitor']
        promoted = [asset for asset,_snapshot in targets if asset.purpose == 'competitor' or not asset.selected]
        if len(videos) + len(promoted) > 10 or sum(asset.duration or 0 for asset in videos + promoted) > 30:
            raise ValueError('参考视频最多 10 个，总时长不能超过 30 秒；请先取消勾选多余视频。')
        for _asset, snapshot in targets:
            if snapshot.kind != 'video' or not snapshot.path or not snapshot.path.is_file():
                raise ValueError(f'{snapshot.filename} 的本地视频不存在；请重新关联文件后上传。')
        if not self.upload_auth.signed_in():
            raise ValueError('请先在设置中登录视频上传账号。')
        draft = self.state.get('prompt_draft') or {}
        analysis_valid = (draft.get('analysis_signature') == analysis_signature(self.assets)
                          and bool(draft.get('frame_count')) and bool(self.state.get('frame_catalog'))
                          and all(Path(frame['path']).is_file() for frame in self.state['frame_catalog']))

        def action():
            results = []
            for index, (_asset, snapshot) in enumerate(targets,1):
                stat = snapshot.path.stat()
                def progress(sent,total):
                    percent = 100 if sent == total else min(99,int(sent/total*100)) if total else 0
                    self._upload_event.emit({'index':index,'count':len(targets),'filename':snapshot.filename,
                                             'sent_bytes':sent,'total_bytes':total,'percent':percent})
                def event(value):
                    self._upload_event.emit({**value,'index':index,'count':len(targets),'filename':snapshot.filename})
                try:
                    uploaded = self.upload_auth.upload(snapshot.path,progress,on_event=event)
                    results.append({'media':uploaded,'file_size':stat.st_size,'file_mtime_ns':stat.st_mtime_ns})
                except Exception as error:
                    # Preserve earlier successful uploads if a later file fails.
                    return {'uploaded':results,'error':str(error),'network_diagnostics':getattr(error,'diagnostics',{})}
            return {'uploaded':results,'error':''}

        def received(result):
            still_valid = analysis_valid and draft.get('analysis_signature') == analysis_signature(self.assets)
            for (asset, snapshot), entry in zip(targets,result['uploaded']):
                if not any(asset is existing for existing in self.assets): continue
                media = entry['media']
                asset.source_url, asset.source_expires_at, asset.upload_id = media['url'],media['expires_at'],media['id']
                asset.upload_file_size, asset.upload_file_mtime_ns = entry['file_size'],entry['file_mtime_ns']
                asset.duration, asset.purpose, asset.selected = media['duration'],'other',True
                self.upload_cache[self._video_cache_key(asset.path)] = manifest([asset])[0]
                self.upload_library.remember(manifest([asset])[0], uploaded_at=time.time())
            if result['uploaded']:
                save_protected_json(self.upload_cache_file,self.upload_cache)
                self.upload_library.save()
                self.changed.emit('uploaded_library')
            if still_valid and self.state.get('prompt_draft'):
                # Transport URL rotation does not change locally analysed footage.
                self.state['prompt_draft'].update(source_assets=manifest(analysis_assets(self.assets)),
                                                analysis_signature=analysis_signature(self.assets))
            self.save()
            self.changed.emit('assets')
            self.changed.emit('upload_account')
            if result['error']:
                self.set_status('视频上传未完成，已保留成功上传的地址')
                self.failed.emit(result['error'])
                return
            self.set_status(f'已上传 {len(result["uploaded"])} 个视频，参考地址有效期 7 天')
            if success: success()

        self.set_status('正在校验并上传本地视频…')
        self.upload_feedback = {'active':True,'phase':'validating','started_at':time.time(),'phase_started_at':time.time(),
                                'index':1,'count':len(targets),'filename':targets[0][1].filename,'percent':0}
        if self.start_job('upload_video', {'files':[str(snapshot.path) for _,snapshot in targets]},action,received):
            self._upload_timer.start()
            self.changed.emit('upload_progress')
        else:
            self.upload_feedback['active'] = False

    @Slot(dict)
    def _receive_upload_event(self, value):
        if self.closed or not self.upload_feedback.get('active'): return
        changed_phase = ('phase' in value and value['phase'] != self.upload_feedback.get('phase')) or value.get('index') != self.upload_feedback.get('index')
        self.upload_feedback.update(value)
        if changed_phase:
            self.upload_feedback['phase_started_at'] = time.time()
            self._upload_recorded_at = 0
        self.refresh_upload_feedback()

    def refresh_upload_feedback(self):
        feedback = self.upload_feedback
        if not feedback.get('active') or self.closed: return
        elapsed = max(0,int(time.time()-feedback['started_at']))
        waiting = max(0,int(time.time()-feedback['phase_started_at']))
        feedback.update(elapsed_seconds=elapsed,phase_seconds=waiting)
        phase = feedback.get('phase')
        stage = {'validating':'校验本地视频','refreshing_account':'续期登录', 'connecting_or_sending':f'发送视频 {feedback.get("percent",0)}%',
                 'waiting_response':f'等待中转响应 {waiting} 秒','reading_response':'读取上传结果'}.get(phase,'准备上传')
        self.set_status(f'上传 {feedback["index"]}/{feedback["count"]} · {feedback["filename"]} · {stage} · 总计 {elapsed} 秒')
        if self.active_call and time.time()-self._upload_recorded_at >= 15:
            try:
                self.history.upload_progress(self.active_call,feedback)
                self._upload_recorded_at = time.time()
            except Exception: pass
        self.changed.emit('upload_progress')

    def sync_keyframe_policy(self):
        if frames_for_generation(self.state):
            return
        for name in ('prompt', 'brief'):
            self.state[name] = strip_analysis_references(self.state.get(name, ''), self.state.get('frame_catalog', []),
                                                        self.assets, self.state.get('generated_keyframe_binding', ''))
        self.assets = analysis_assets(self.assets)
        self.state.update(use_keyframes_for_generation=False, generated_keyframe_binding='')
        if self.state.get('prompt_draft'):
            self.state['prompt_draft'].update(prompt=prompt_for_submission(self.state), keyframe_handles=[])

    def apply_keyframes(self, handles, *, notify=True):
        if handles and cleanup_reference(prompt_for_submission(self.state)):
            raise ValueError('Logo 替换与文字清理任务的关键帧仅供 AI 分析，不附加到视频生成。')
        catalog = self.state["frame_catalog"]
        available = {f["handle"] for f in catalog}
        handles = list(dict.fromkeys(handles))
        if any(h not in available for h in handles):
            raise ValueError("关键帧记录已变化，请重新抽帧分析。")
        existing = analysis_assets(self.assets)
        if sum(a.selected and a.kind == "image" for a in existing) + len(handles) > 30:
            raise ValueError("角色图、场景图和关键帧合计最多 30 张。")
        if any(not Path(f["path"]).is_file() for f in catalog if f["handle"] in handles):
            raise ValueError("关键帧文件已移动，请重新抽帧分析。")
        self.assets = existing + keyframe_assets(catalog, handles)
        self.remember_images([a for a in self.assets if a.purpose == 'keyframe'])
        prompt = prompt_for_submission(self.state)
        old = self.state.get("generated_keyframe_binding", "")
        if old:
            prompt = prompt.replace(old, "").rstrip()
        binding = ("\n\n【关键帧参考】\n" + "、".join(handles)
                   + " 仅用于构图、动作与镜头顺序；替换角色的外观以角色图为准。") if handles else ""
        self.state.update(prompt=prompt + binding, brief=prompt + binding, generated_keyframe_binding=binding,
                          use_keyframes_for_generation=bool(handles))
        if self.state.get("prompt_draft"):
            self.state["prompt_draft"].update(prompt=prompt + binding, keyframe_handles=handles)
        if notify:
            self.save()
            self.changed.emit("draft")
            self.set_status(f"已选择 {len(handles)} 张关键帧用于生成" if handles else "关键帧仅供分析，未附加到视频生成")

    def analysis_account(self):
        return self.auth.active_account() if self.state['ai_provider'] == 'chatgpt' else self.platform_accounts.sessions[self.state['ai_provider']].active_account()

    def analysis_label(self):
        return self.auth.label() if self.state['ai_provider'] == 'chatgpt' else self.platform_accounts.sessions[self.state['ai_provider']].label()

    def analysis_model(self):
        return self.state['openai_model'] if self.state['ai_provider'] == 'chatgpt' else self.state['ai_models'].get('deepseek', 'deepseek-flash')

    def set_analysis_model(self, model):
        if self.state['ai_provider'] == 'chatgpt':
            self.state['openai_model'] = model
        else:
            self.state['ai_models'][self.state['ai_provider']] = model

    def reasoning_model(self, model=None):
        return next((item for item in self.models if item.get('slug') == (model or self.analysis_model())), {})

    def reasoning_options(self, model=None):
        return supported_efforts(self.state['ai_provider'], self.reasoning_model(model))

    def analysis_reasoning_effort(self, model=None):
        provider, slug = self.state['ai_provider'], model or self.analysis_model()
        saved = self.state['ai_reasoning_efforts'].get(provider, {}).get(slug)
        return selected_effort(provider, self.reasoning_model(slug), saved)

    def set_analysis_reasoning_effort(self, effort):
        if self.busy:
            return
        if effort not in self.reasoning_options():
            raise ValueError('当前模型不支持该思考强度，请刷新模型后重新选择。')
        self.state['ai_reasoning_efforts'].setdefault(self.state['ai_provider'], {})[self.analysis_model()] = effort
        self.save()
        self.changed.emit('ai_preferences')

    def select_ai_provider(self, provider):
        if self.busy or provider not in PLATFORMS or provider == self.state['ai_provider']:
            return
        self.model_cache[self.state['ai_provider']] = self.models
        self.state['ai_provider'] = provider
        self.models = self.model_cache.get(provider, [])
        self.save()
        self.changed.emit('account')
        self.changed.emit('models')
        if self.analysis_account():
            self.refresh_models()

    def refresh_models(self, success=None):
        if not self.analysis_account():
            return
        provider = self.state['ai_provider']
        session = self.auth if provider == 'chatgpt' else self.platform_accounts.sessions[provider]
        def action():
            models = session.models()
            if not models:
                raise ValueError('分析平台没有返回可用模型，请在连接设置中刷新；仍可直接生成视频。')
            return models
        def received(models):
            self.models = models
            self.model_cache[provider] = models
            if models and self.analysis_model() not in {m['slug'] for m in models}:
                self.set_analysis_model(models[0]['slug'])
                self.save()
            self.changed.emit('models')
            self.changed.emit('account')
            self.set_status('准备就绪')
            if callable(success):
                success()
        self.set_status(f'正在连接 {PLATFORMS[provider]}…')
        return self.start_job('models', {'provider': provider, 'auth_mode': 'chatgpt_plan' if provider == 'chatgpt' else 'api_key'},
                              action, received, failure=self._analysis_error)

    def sign_in(self):
        if self.busy:
            return
        if self.state['ai_provider'] != 'chatgpt':
            self.failed.emit('DeepSeek 使用 API Key，请在连接窗口填写接口密钥。')
            return
        def action():
            return {'label': self.auth.sign_in(), 'models': self.auth.models()}
        def received(result):
            self.models = result['models']
            self.model_cache['chatgpt'] = self.models
            if self.models and self.analysis_model() not in {m['slug'] for m in self.models}:
                self.set_analysis_model(self.models[0]['slug'])
            self.save()
            self.changed.emit('account'); self.changed.emit('models')
            self.set_status('ChatGPT 已连接')
        self.set_status('在浏览器中完成 ChatGPT 登录…')
        self.start_job('sign_in', {'provider': 'chatgpt', 'auth_mode': 'chatgpt_plan'}, action, received, failure=self._analysis_error)

    def configure_deepseek_api(self, key):
        if self.busy or self.state['ai_provider'] != 'deepseek':
            return
        session = self.platform_accounts.sessions['deepseek']
        def received(models):
            self.models = models; self.model_cache['deepseek'] = models
            if self.analysis_model() not in {m['slug'] for m in models}:
                self.set_analysis_model(models[0]['slug'])
            self.save()
            self.changed.emit('account'); self.changed.emit('models')
            self.set_status('DeepSeek API 已连接；分析按接口用量计费。')
        self.set_status('正在校验 DeepSeek 接口密钥（不调用推理）…')
        self.start_job('configure_api', {'provider': 'deepseek', 'auth_mode': 'api_key', 'endpoint': 'https://api.deepseek.com/models'},
                       lambda: session.configure_key(key), received, failure=self._analysis_error, extra_secrets=(key,))

    def cancel_ai_login(self):
        if self.state['ai_provider'] == 'deepseek':
            self.platform_accounts.sessions['deepseek'].cancel_pending()

    def sign_out_ai(self):
        if self.busy or self.state['ai_provider'] != 'deepseek':
            return
        self.platform_accounts.sessions['deepseek'].clear_key()
        self.models = []; self.model_cache.pop('deepseek', None)
        self.changed.emit('account'); self.changed.emit('models')
        self.set_status('已清除 DeepSeek 接口密钥')

    def _analysis_error(self, error):
        self.changed.emit('account')
        self.set_status(str(error))
        self.failed.emit(str(error))

    def analyze(self):
        provider = self.state['ai_provider']
        if not self.analysis_account():
            raise ValueError("先在右上角登录 ChatGPT。" if provider == "chatgpt" else "先在连接窗口配置 DeepSeek API Key。")
        model = self.analysis_model()
        if model not in {m["slug"] for m in self.models}:
            # Fetch capabilities only when the user requests analysis; continue
            # that request once, without another click or a startup dependency.
            return self.refresh_models(success=self.analyze)
        effort = self.analysis_reasoning_effort()
        assets = [replace(a) for a in analysis_assets(self.assets)]
        selected_model = next(m for m in self.models if m['slug'] == model)
        if provider != 'chatgpt' and selected_model.get('vision') is not True and any(a.kind in {'video','image'} for a in assets):
            raise ValueError('当前分析模型只支持文本，请在平台登录窗口选择支持图片的模型。')
        competitor = next((a for a in assets if a.purpose == "competitor"), None)
        brief = prompt_for_submission(self.state)
        if self.state.get("generated_keyframe_binding"):
            brief = brief.replace(self.state["generated_keyframe_binding"], "").rstrip()
        previous = self.state["prompt"].strip()
        attach_frames = frames_for_generation(self.state)
        settings = generation_settings(self.state, assets)
        parent = self.state["iteration_task_id"]
        generated = None
        if self.state["review_output"] and parent:
            row = self.row(parent)
            if not row or not row["local_path"] or not Path(row["local_path"]).is_file():
                raise ValueError("成片文件不在本机，请从作品库重新下载。")
            generated = Path(row["local_path"])
        transcribe = self.state["transcribe_audio"]
        transcription_key = self.openai_key if transcribe else ""
        if transcribe and not transcription_key:
            raise ValueError("语音转写需要单独的 OpenAI 接口密钥，或关闭语音转写。")
        self.state["ai_original_text"] = brief
        self.save()

        def action():
            catalog = frame_catalog(reference_videos(assets), assets, self.directory / "reference_frames", self._progress.emit)
            client = (OpenAIPromptClient(self.auth.access_token(), model, chatgpt_plan=True, transcription_api_key=transcription_key,
                                        reasoning_effort=effort)
                      if provider == 'chatgpt' else AgentPromptClient(self.platform_accounts.sessions[provider], model,
                                                                    transcription_api_key=transcription_key, reasoning_effort=effort))
            draft = client.draft(competitor, assets, brief, transcribe_audio=transcribe, generation_settings=settings,
                                 generated_video=generated, previous_prompt=previous, on_progress=self._progress.emit,
                                 reference_frames=catalog, use_keyframes_for_generation=attach_frames)
            return {"draft": asdict(draft), "frames": catalog, "sources": manifest(assets), "provider":provider,
                    "ai_usage":self.platform_accounts.sessions[provider].usage if provider != 'chatgpt' else {}}

        def received(result):
            data = result["draft"]
            # The probe updates duration; record the same source metadata the next submit will validate.
            for current in self.assets:
                source = next((a for a in assets if a.handle == current.handle), None)
                if source:
                    current.duration = source.duration
            data["source_assets"] = manifest(analysis_assets(self.assets))
            data["source_brief"] = brief
            data["analysis_signature"] = analysis_signature(self.assets)
            data["frame_count"] = len(result["frames"])
            data['ai_provider'] = provider
            data['ai_model'] = model
            data['ai_reasoning_effort'] = effort
            data['ai_usage'] = result.get('ai_usage', {})
            if not attach_frames:
                data['prompt'] = strip_analysis_references(data['prompt'], result['frames'], self.assets,
                                                          self.state.get('generated_keyframe_binding', ''))
                data['keyframe_handles'] = []
            data['prompt'] = enforce_edit_requirements(data['prompt'], brief, assets)
            data['use_keyframes_for_generation'] = attach_frames
            if edit_requirements(brief)['watermarks']:
                self.state['watermark'] = False
            self.state["prompt_draft"] = data
            self.state["manual_prompt"] = False
            self.state["prompt"] = data["prompt"]
            self.state["generated_keyframe_binding"] = ""
            self.state["frame_catalog"] = result["frames"]
            self.state["analysis"] = data["analysis"]
            capacity = 30 - sum(a.selected and a.kind == "image" for a in analysis_assets(self.assets))
            mentioned = [f["handle"] for f in result["frames"] if f["handle"] in data["prompt"]]
            chosen = choose_keyframes(result["frames"], mentioned + data.get("keyframe_handles", []), capacity) if attach_frames else []
            if any(h not in chosen for h in mentioned):
                raise ValueError("智能分析引用了过多关键帧，请减少其他图片后重新分析。")
            self.apply_keyframes(chosen, notify=False)
            self.save()
            self.changed.emit("draft")
            self.set_status(f"已分析 {len(result['frames'])} 张画面；" + (f"附加 {len(chosen)} 张关键帧" if attach_frames else '关键帧仅供分析，生成使用勾选的视频与素材'))

        self.set_status("正在智能分析素材…" if not generated else "正在智能对比参考视频与成片…")
        self.start_job("refine" if generated else "draft", {
            "model": model, "provider":provider, "reasoning_effort": effort,
            "use_keyframes_for_generation": attach_frames,
            "auth_mode": "chatgpt_plan" if provider == 'chatgpt' else 'api_key',
            "billing_source": "ChatGPT 会员额度" if provider == 'chatgpt' else 'DeepSeek API 余额（接口单独计费）',
            "audio_transcription": {"enabled": transcribe, "billing_source": "OpenAI 接口单独计费" if transcribe else "未启用"},
            "generation_settings": settings, "creative_brief": brief, "assets": manifest(assets),
            "previous_prompt": previous, "generated_video": str(generated) if generated else "",
        }, action, received, task_id=parent, failure=self._analysis_error)

    def http_allowed(self):
        address = self.state["base_url"].strip().rstrip("/")
        return not address.startswith("http://") or self.state.get("allow_http_for") == address

    def seedance_client(self):
        return SeedanceClient(self.state["base_url"], self.api_key, allow_http=self.http_allowed())

    def generation_snapshot(self, payload=None):
        payload = deepcopy(payload if payload is not None else collect_payload(self.state, self.assets))
        keys = ('mode','task_type','resolution','duration','ratio','generate_audio','watermark',
                'return_last_frame','output_format','download_dir','brief','prompt','analysis',
                'prompt_draft','frame_catalog','generated_keyframe_binding','ai_original_text',
                'manual_prompt','auto_analyze','iteration_task_id','review_output','use_keyframes_for_generation')
        return {'payload': payload, 'assets': manifest(self.assets), 'mode': MODES[self.state['mode']],
                'original_prompt': prompt_for_submission(self.state), 'parent_task_id': self.state['iteration_task_id'],
                'template': deepcopy({key:self.state[key] for key in ('prompt_draft','frame_catalog',
                                    'generated_keyframe_binding','ai_original_text','manual_prompt','auto_analyze','use_keyframes_for_generation')}),
                'settings': generation_settings(self.state, self.assets),
                'state': deepcopy({key:self.state[key] for key in keys}),
                'base_url': self.state['base_url'].strip().rstrip('/'),
                'allow_http_for': self.state.get('allow_http_for','')}

    def enqueue_generation(self, payload=None):
        if self.busy or self.closed:
            return None
        # A click authorizes this exact payload, including media bytes, for later submission.
        snapshot = self.generation_snapshot(payload)
        self.seedance_client()
        first = not any(r['status'] in QUEUE_ACTIVE | {'waiting','unknown'} for r in self.queue.rows())
        identity = self.queue.add(snapshot)
        if first:
            self.queue_enabled = True
            self.queue_reason = ''
        self.changed.emit('queue')
        self.set_status(f'已加入生成队列 #{identity}；'+('将按顺序自动提交' if self.queue_enabled else '队列已暂停'))
        self.schedule_queue()
        if not self.busy and not self.query_busy:
            self.dispatch_queue(automatic=False)
        return identity

    def schedule_queue(self):
        if not self.closed:
            self._queue_timer.start(200)

    def pause_queue(self):
        self.queue_enabled = False
        self.queue_reason = '已暂停后续提交，当前任务继续生成和保存。'
        self.changed.emit('queue')

    def start_queue(self):
        if any(r['status'] == 'unknown' for r in self.queue.rows()):
            raise ValueError('队列中有提交未确认的任务，请先核对并关联任务编号，或确认服务器未接单。')
        self.queue_enabled = True
        self.queue_reason = ''
        self.changed.emit('queue')
        if self._query_failures > 3:
            self._query_failures = 0
            self.resume()
        self.schedule_queue()

    def sync_queue(self):
        tasks = {r['task_id']:r for r in self.history.rows()}
        changed = False
        for item in self.queue.rows():
            if item['status'] not in QUEUE_ACTIVE or not item['task_id']:
                continue
            row = tasks.get(item['task_id'])
            if not row:
                continue
            if row['status'] == 'succeeded':
                status = 'succeeded' if row['local_path'] and Path(row['local_path']).is_file() else 'saving'
            elif row['status'] in {'failed','cancelled','expired'}:
                status = 'failed'
            else:
                status = 'queued' if row['status']=='queued' else 'running'
            if status != item['status']:
                self.queue.update(item['queue_id'], status=status, error=row.get('error',''))
                changed = True
                if status == 'failed':
                    self.queue_enabled = False
                    self.queue_reason = f"任务 #{item['queue_id']} 失败，后续提交已暂停。"
        if changed:
            self.changed.emit('queue')

    def queue_failure(self, identity, error, *, submission=False):
        receipt_known = submission and bool(self.queue.get(identity)['task_id'])
        uncertain = isinstance(error, SubmissionUncertain) or receipt_known
        self.queue.fail(identity, error, uncertain=uncertain,
                        secrets=(self.api_key,self.openai_key))
        self.queue_enabled = False
        self.queue_reason = f'任务 #{identity} '+('已接单，保存任务记录失败，请核对。' if receipt_known else
                                               '提交未确认，请先核对。' if uncertain else '失败，后续提交已暂停。')
        self.changed.emit('queue')
        self.set_status(self.queue_reason)
        self.failed.emit(str(error))

    def dispatch_queue(self, *, automatic=True):
        if self.closed:
            return
        self.sync_queue()
        entries = self.queue.rows()
        if any(r['status'] == 'unknown' for r in entries):
            self.queue_enabled = False
            return
        if any(r['status'] in QUEUE_ACTIVE for r in entries) or self.pending_generation() or not self.queue_enabled:
            return
        item = next((r for r in entries if r['status'] == 'waiting'), None)
        if not item:
            self.queue_enabled = False
            self.queue_reason = '队列已完成。'
            self.changed.emit('queue')
            return
        if self.busy or self.query_busy:
            self._queue_timer.start(500)
            return
        try:
            snapshot = self.queue.get(item['queue_id'])['snapshot']
            if snapshot['base_url'] != self.state['base_url'].strip().rstrip('/'):
                raise ValueError('中转连接已更改，请恢复原连接或移出任务后重新加入队列。')
            if any(a.selected and a.upload_id and source_expired(a) for a in load_assets(snapshot['assets'])):
                raise ValueError('队列中的参考视频地址已过期，请移出并编辑，重新上传后加入队列。')
            if not self.queue.claim(item['queue_id']):
                return
            self.changed.emit('queue')
            self.queue_auto_submission = automatic
            if not self.generate(snapshot=snapshot, queue_id=item['queue_id']):
                raise ValueError('无法初始化提交记录，请求未发送，队列已暂停。')
        except Exception as error:
            self.queue_failure(item['queue_id'], error)
        finally:
            self.queue_auto_submission = False

    def remove_queue_item(self, identity):
        self.queue.cancel(identity)
        self.changed.emit('queue')
        self.schedule_queue()

    def edit_queue_item(self, identity):
        if self.busy:
            raise ValueError('当前操作完成后可载入队列任务。')
        item = self.queue.get(identity)
        self.queue.cancel(identity)
        self.state.update(deepcopy(item['snapshot']['state']))
        self.state['auto_analyze'] = item['snapshot']['state'].get('auto_analyze',False)
        self.assets = load_assets(item['snapshot']['assets'])
        self.sync_keyframe_policy()
        self.save()
        self.changed.emit('all')
        self.changed.emit('queue')
        self.set_status('已从队列移出并载入编辑；修改后可重新加入。')

    def move_queue_item(self, identity, direction):
        self.queue.move(identity,direction)
        self.changed.emit('queue')

    def clear_unconfirmed_queue_item(self, identity):
        item = self.queue.get(identity)
        if item['status'] != 'unknown':
            raise ValueError('此任务已不处于待核对状态。')
        self.queue.update(identity,status='cancelled',error='用户核对服务器未接单后移出；没有重新提交。')
        self.queue_reason = '已移出核对项，可继续队列。'
        self.changed.emit('queue')

    def attach_queue_task(self, identity, task_id):
        if self.busy or self.query_busy:
            raise ValueError('当前请求完成后可关联任务。')
        item = self.queue.get(identity)
        if item['status'] != 'unknown':
            raise ValueError('只有提交未确认的任务需要关联编号。')
        task_id = task_id.strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{3,128}',task_id):
            raise ValueError('任务编号格式不正确。')
        if self.pending_generation():
            raise ValueError('已有生成任务，请等待完成后关联。')
        snapshot = item['snapshot']
        client = self.seedance_client()
        def received(task):
            if task.get('id') != task_id or task.get('status') not in {'queued','running','succeeded','failed','cancelled','expired'}:
                raise ValueError('接口未确认此任务编号，请核对中转站记录。')
            self.history.add(task_id,snapshot['payload'],snapshot['mode'],original_prompt=snapshot['original_prompt'],
                             assets=snapshot['assets'],parent_task_id=snapshot['parent_task_id'],template=snapshot['template'])
            self.history.update(task)
            self.queue.update(identity,task_id=task_id,status='running',error='')
            self.state['task_id'] = task_id
            self.save()
            self.sync_queue()
            self.changed.emit('history')
            self.changed.emit('queue')
            self.resume()
        self.start_job('query',{'task_id':task_id,'queue_id':identity,'purpose':'关联已接单的任务'},
                       lambda:client.get_task(task_id),received,task_id=task_id)

    def generate(self, payload=None, *, snapshot=None, queue_id=None):
        if self.busy or self.closed:
            return
        pending = self.pending_generation()
        if pending:
            raise ValueError('已有视频任务正在排队或生成，请等待完成后再提交；当前工具不会自动排队下一条。')
        snapshot = snapshot or self.generation_snapshot(payload)
        if any(asset.selected and asset.upload_id and source_expired(asset) for asset in load_assets(snapshot['assets'])):
            raise ValueError('参考视频地址已过期或即将到期，不能提交，请先重新上传。')
        payload = deepcopy(snapshot['payload'])
        client = self.seedance_client()
        original, assets = snapshot['original_prompt'], snapshot['assets']
        mode, parent, template = snapshot['mode'], snapshot['parent_task_id'], snapshot['template']
        self._poll.stop()
        self.save()

        def action():
            task_id = client.create_task(payload)
            if queue_id:
                self.queue.update(queue_id,task_id=task_id,status='queued')
            self.history.add(task_id, payload, mode, original_prompt=original, assets=assets, parent_task_id=parent,
                             template=template)
            return task_id

        def received(task_id):
            self.state["task_id"] = task_id
            self.save()
            self.changed.emit("history")
            self.changed.emit('queue')
            self.set_status("任务已提交")
            self.query()

        self.set_status("正在提交生成…")
        return self.start_job("create", {"payload": payload, "assets": assets, "original_prompt": original, "parent_task_id": parent,
                                  "timeout_seconds": CREATE_TIMEOUT_SECONDS, "queue_id":queue_id},
                       action, received,
                       on_started=(lambda call_id:self.queue.update(queue_id,call_id=call_id)) if queue_id else None,
                       failure=(lambda error:self.queue_failure(queue_id,error,submission=True)) if queue_id else None)

    def row(self, task_id=None):
        task_id = task_id or self.state["task_id"]
        return next((row for row in self.history.rows() if row["task_id"] == task_id), None)

    def pending_generation(self):
        return next((row for row in self.history.rows() if row['status'] in {'queued', 'running'}), None)

    def query(self, task_id=None):
        if self.closed:
            return
        if task_id:
            pending = self.pending_generation()
            if pending and pending['task_id'] != task_id:
                raise ValueError('已有任务正在生成，请先等待完成，避免切换查询丢失当前进度。')
            self.state["task_id"] = task_id
            self._query_failures = 0
            self.save()
        task_id = self.state["task_id"]
        if not task_id:
            return
        if self.busy or self.query_busy:
            self._poll.start(1000)
            return
        try:
            client = self.seedance_client()
        except ValueError as error:
            self.set_status(str(error))
            return

        def received(task):
            safe_task = dict(task)
            if safe_task.get("error"):
                safe_task["error"] = compact_record(safe_task["error"], (self.api_key, self.openai_key))
            self.history.update(safe_task)
            self._query_failures = 0
            self.changed.emit("history")
            if self.state["task_id"] != task_id:
                return
            status = task.get("status", "unknown")
            if not self.busy:
                self.set_status({"queued": "排队中", "running": "正在生成视频…", "succeeded": "视频生成完成",
                                 "failed": "生成失败", "cancelled": "任务已取消", "expired": "任务已过期"}.get(status, "状态待核对"))
            if status in {"running", "queued"}:
                self._poll.start(5000)
            elif status == "succeeded":
                row = self.row(task_id)
                url = (task.get("content") or {}).get("video_url") or (row["video_url"] if row else "")
                if url:
                    if self.busy:
                        self._poll.start(1000)
                    else:
                        self.download(task_id, url)
                else:
                    queued = next((r for r in self.queue.rows() if r['task_id']==task_id and r['status'] in QUEUE_ACTIVE),None)
                    if queued:
                        self.queue_failure(queued['queue_id'],ValueError('生成状态成功，但接口未返回视频地址，队列已暂停。'))
            elif status == "failed":
                self.failed.emit(str(task.get("error") or "上游生成失败，请查看调用记录。"))

        def failure(error):
            self._query_failures += 1
            if self._query_failures <= 3:
                if not self.busy:
                    self.set_status(f"查询暂未成功，稍后重试（{self._query_failures}/3）")
                self._poll.start(5000 * self._query_failures)
            else:
                if not self.busy:
                    self.set_status("查询暂停，可在成片或历史记录中继续查询")
                self.failed.emit(str(error))
        def query_action():
            task = client.get_task(task_id)
            trace = getattr(client, 'last_response_diagnostics', None)
            if isinstance(trace, dict):
                # Retain transport evidence alongside the original server fields.
                task = {**task, 'api_diagnostics': trace if task.get('status') == 'failed' else
                        {k:v for k,v in trace.items() if k not in {'response_body','response_json'}}}
            return task
        self.start_job("query", {"task_id": task_id}, query_action, received, task_id=task_id, failure=failure, background=True)

    def resume(self):
        if self.closed:
            return
        if self.busy or self.query_busy:
            self._poll.start(1000)
            return
        self.sync_queue()
        queued = next((r for r in self.queue.rows() if r['status'] in QUEUE_ACTIVE and r['task_id']), None)
        if queued:
            self.state['task_id'] = queued['task_id']
            self.save()
        row = self.row()
        if not row:
            return
        if row["status"] == "succeeded" and row["local_path"] and Path(row["local_path"]).is_file():
            self.result_ready.emit(row)
            return
        if row["status"] in {"queued", "running", "succeeded"} and self.api_key and self.http_allowed():
            self.query()

    def download(self, task_id, url=None, destination: Path | None = None):
        row = self.row(task_id)
        if not row:
            raise ValueError("找不到任务记录。")
        if destination is None and row["local_path"] and Path(row["local_path"]).is_file():
            self.result_ready.emit(row)
            self.set_status("成片已保存在本机")
            return
        suffix = ".mov" if row["output_format"] == "mov" else ".mp4"
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", task_id)[:80]
        queued = next((r for r in self.queue.rows() if r['task_id'] == task_id),None)
        download_dir = self.queue.get(queued['queue_id'])['snapshot']['state']['download_dir'] if queued else self.state['download_dir']
        path = destination or Path(download_dir) / f"seedance_{safe_id}{suffix}"
        url = url or row["video_url"]
        if not url:
            self.query(task_id)
            return

        def action():
            path.parent.mkdir(parents=True, exist_ok=True)
            if destination is not None or not path.is_file() or path.stat().st_size == 0:
                download_video(url, path)
            try:
                info = video_metadata(path)
            except Exception:
                info = {}
            self.history.set_local_path(task_id, path)
            self.history.set_media_info(task_id, info)
            return {"path": str(path), "media_info": info}

        def received(result):
            self.changed.emit("history")
            self.result_ready.emit(self.row(task_id))
            self.set_status("视频已自动保存")
        self.set_status("正在保存成片…")
        self.start_job("download", {"video_url": url, "local_path": str(path)}, action, received, task_id=task_id,
                       failure=(lambda error:self.queue_failure(queued['queue_id'],error)) if queued else None)

    def reuse(self, row, refine=False):
        payload = json.loads(row["request_json"])
        template = json.loads(row.get("template_json") or '{}')
        prompt = row.get("original_prompt") or row.get("prompt", "")
        binding = template.get('generated_keyframe_binding','')
        if not binding and any(a.get('purpose')=='keyframe' for a in json.loads(row['assets_json'])):
            match=re.search(r'\n\n【关键帧参考】\n[^\n]*仅用于构图、动作与镜头顺序；替换角色的外观以角色图为准。$',prompt)
            binding=match.group(0) if match else ''
        self.state.update(
            mode=next((name for name, code in MODES.items() if code == row["mode"]), "全模态参考"),
            task_type="参考生视频",
            duration=str(row["requested_duration"] or DEFAULTS['duration']), prompt=prompt, brief=prompt,
            iteration_task_id=row["task_id"], review_output=refine,
            prompt_draft=template.get('prompt_draft',{}), manual_prompt=template.get('manual_prompt',True) and not refine,
            auto_analyze=template.get('auto_analyze',False),
            use_keyframes_for_generation=template.get('use_keyframes_for_generation',False),
            frame_catalog=template.get('frame_catalog',[]), generated_keyframe_binding=binding,
            ai_original_text=template.get('ai_original_text',prompt), analysis=(template.get('prompt_draft') or {}).get('analysis',''),
        )
        for key in ("resolution", "ratio", "output_format"):
            self.state[key] = row[key]
        for key in ("watermark", "return_last_frame", "generate_audio"):
            self.state[key] = payload.get(key, DEFAULTS[key])
        self.assets = load_assets(json.loads(row["assets_json"]))
        self.sync_keyframe_policy()
        self.remember_images(self.assets)
        self.save()
        self.changed.emit("all")
        self.set_status("已载入成片，填写改进要求后可用智能对比优化" if refine else "已载入历史模板，可修改提示词、素材和参数后再次生成")

    def reuse_prompt(self, record):
        settings=record['settings']
        self.assets=load_assets(record['assets'])
        self.remember_images(self.assets)
        self.state.update(mode=next((name for name,code in MODES.items() if code==settings.get('mode')),'全模态参考'),
                          task_type='参考生视频', prompt=record['original_prompt'], brief=record['original_prompt'],
                          ai_original_text=record.get('source_brief',''), iteration_task_id=record.get('parent_task_id',''),
                          review_output=False, manual_prompt=True, frame_catalog=record.get('frames',[]),
                          generated_keyframe_binding='', prompt_draft=record['draft'], analysis=record['draft'].get('analysis',''),
                          use_keyframes_for_generation=record['draft'].get('use_keyframes_for_generation',False))
        for key in ('resolution','ratio','output_format','generate_audio','watermark','return_last_frame'):
            self.state[key]=settings.get(key,DEFAULTS[key])
        self.state['duration']=str(settings.get('duration',DEFAULTS['duration']))
        if frames_for_generation(self.state) and self.state['frame_catalog'] and all(Path(f['path']).is_file() for f in self.state['frame_catalog']):
            capacity=30-sum(a.selected and a.kind=='image' for a in analysis_assets(self.assets))
            self.apply_keyframes(choose_keyframes(self.state['frame_catalog'],record['draft'].get('keyframe_handles',[]),capacity),notify=False)
        self.sync_keyframe_policy()
        self.save()
        self.changed.emit('all')
        self.set_status('已载入提示词版本，可继续修改后生成')

    def export_csv(self, path):
        from ui_language import choice_text
        fields = ("任务编号", "状态", "分辨率", "画面比例", "设定时长（秒）", "产出时长（秒）",
                  "生成计费单位", "总计费单位", "参考费用（人民币元）", "费用状态", "本地视频位置")
        with Path(path).open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fields)
            writer.writeheader()
            for row in self.history.rows():
                amount = estimate_cny(row, self.state['cny_rates'])
                values = dict(zip(fields, (row['task_id'], STATUS_NAMES.get(row['status'],'待核对'), row['resolution'],
                                         choice_text(row['ratio']), '自适应' if row['requested_duration']==-1 else row['requested_duration'],
                                         row['actual_duration'], row['completion_tokens'], row['total_tokens'],
                                         format(amount,'.2f') if amount is not None else '',
                                         '按当前单价估算' if amount is not None else '未估算', row['local_path'])))
                safe = {k: ("'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v) for k, v in values.items()}
                writer.writerow(safe)

    def close(self):
        self.closed = True
        self.platform_accounts.cancel_pending()
        self._save_timer.stop()
        self._poll.stop()
        self._queue_timer.stop()
        self._upload_timer.stop()
        self._expiry_timer.stop()
        self.save()
        if self.active_call is not None:
            self.history.interrupt_call(self.active_call, "试用界面已关闭；服务端结果尚未确认。")
        if self.query_call is not None:
            self.history.interrupt_call(self.query_call, "试用界面已关闭；查询结果尚未确认。")
