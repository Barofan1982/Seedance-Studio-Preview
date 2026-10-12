"""Analyze a local competitor video and draft a Seedance prompt with OpenAI."""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from media_assets import MediaAsset, _run_media_tool, extract_audio, sample_video_frames
from error_diagnostics import MAX_ERROR_BYTES, response_snapshot, safe_text


RESPONSES_URL = "https://api.openai.com/v1/responses"
TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
DEFAULT_MODEL = "gpt-6-astra"


@dataclass
class PromptDraft:
    analysis: str
    shots: list[dict]
    prompt: str
    uncertainties: list[str]
    transcript: str = ""
    keyframe_handles: list[str] = field(default_factory=list)


class OpenAIError(Exception):
    def __init__(self, message, *, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": {"type": "string"},
                    "visual": {"type": "string"},
                    "camera": {"type": "string"},
                    "motion": {"type": "string"},
                },
                "required": ["time", "visual", "camera", "motion"],
                "additionalProperties": False,
            },
        },
        "seedance_prompt": {"type": "string"},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "keyframe_handles": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["analysis", "shots", "seedance_prompt", "uncertainties", "keyframe_handles"],
    "additionalProperties": False,
}


def _scaled_jpeg_data_url(path: Path, temporary_dir: Path, name: str) -> str:
    image = temporary_dir / f"{name}.jpg"
    _run_media_tool([
        "ffmpeg", "-y", "-v", "error", "-i", str(path), "-frames:v", "1",
        "-vf", "scale=768:-2:force_original_aspect_ratio=decrease", "-q:v", "5", str(image)
    ])
    return "data:image/jpeg;base64," + base64.b64encode(image.read_bytes()).decode("ascii")


def _response_text(data: dict) -> str:
    fragments = []
    for item in data.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                value = content.get("text", "")
                if isinstance(value, str) and value.strip():
                    fragments.append(value)
    if fragments:
        return "\n".join(fragments)
    raise OpenAIError("ChatGPT 没有返回可读取的提示词；请检查模型响应状态。")


def _read_response_stream(response) -> dict:
    """Keep streamed text: the terminal event need not repeat message content."""
    completed = None
    text_parts: dict[tuple[int, int], str] = {}
    finished_items: dict[int, dict] = {}
    data_lines: list[str] = []
    consumed = 0

    def consume_event() -> None:
        nonlocal completed
        if not data_lines or data_lines == ["[DONE]"]:
            data_lines.clear()
            return
        try:
            event = json.loads("\n".join(data_lines))
        except ValueError as error:
            raise OpenAIError("ChatGPT 流式响应无效。") from error
        data_lines.clear()
        if not isinstance(event, dict):
            raise OpenAIError("ChatGPT 流式响应格式不正确。")
        kind = event.get("type")
        if kind in ("response.failed", "error"):
            detail = (event.get("response") or {}).get("error") or event.get("error") or event
            code = detail.get("code", "")
            if code in ("subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"):
                raise OpenAIError("ChatGPT 会员可共享额度已用完或暂不可用；请在 ChatGPT 中检查此工具的计划用量设置，或稍后重试。",
                                  diagnostics={**response_snapshot(json.dumps(event).encode()), 'phase':'stream_response',
                                               'api_error_code':code, 'api_error_message':detail.get('message','')})
            raise OpenAIError(f"ChatGPT 计划请求失败：{detail.get('message') or code or '未知错误'}",
                              diagnostics={**response_snapshot(json.dumps(event).encode()), 'phase':'stream_response',
                                           'api_error_code':code, 'api_error_message':detail.get('message','')})
        if kind == "response.incomplete":
            raise OpenAIError("AI 响应未完成，请减少素材或重试。")
        if kind == "response.completed":
            completed = event.get("response")
        elif kind in ("response.output_text.delta", "response.output_text.done"):
            index = (event.get("output_index", 0), event.get("content_index", 0))
            value = event.get("text" if kind.endswith(".done") else "delta", "")
            if isinstance(value, str):
                text_parts[index] = value if kind.endswith(".done") else text_parts.get(index, "") + value
        elif kind == "response.output_item.done" and isinstance(event.get("item"), dict):
            finished_items[event.get("output_index", 0)] = event["item"]

    for raw_line in response:
        consumed += len(raw_line)
        if consumed > 16_000_000:
            raise OpenAIError("ChatGPT 流式响应过大。")
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line:
            consume_event()
    consume_event()
    if not isinstance(completed, dict):
        raise OpenAIError("ChatGPT 连接结束，但没有完成响应。")
    try:
        _response_text(completed)
        return completed
    except OpenAIError:
        pass
    # Preserve completed metadata/usage, and fill missing text from stream events.
    result = dict(completed)
    if finished_items:
        result["output"] = [finished_items[index] for index in sorted(finished_items)]
        try:
            _response_text(result)
            return result
        except OpenAIError:
            pass
    if text_parts:
        result["output"] = [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": text_parts[index]} for index in sorted(text_parts)
        ]}]
    return result


class OpenAIPromptClient:
    def __init__(
        self, api_key: str, model: str = DEFAULT_MODEL, *,
        chatgpt_plan: bool = False, transcription_api_key: str = "",
    ):
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.chatgpt_plan = chatgpt_plan
        self.transcription_api_key = (transcription_api_key or (api_key if not chatgpt_plan else "")).strip()
        if not self.api_key:
            raise ValueError("请先登录 ChatGPT。" if chatgpt_plan else "请填写 OpenAI 接口密钥。")
        if not self.model:
            raise ValueError("请填写 ChatGPT 模型名称。")

    def _post(self, url: str, data: bytes, content_type: str, timeout: int = 180, credential: str | None = None) -> dict:
        credential = credential or self.api_key
        started = time.monotonic()
        phase, http_status, response_headers = 'opening_connection_or_sending_request', None, {}
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.build_opener(_NoRedirects()).open(request, timeout=timeout) as response:
                phase, http_status, response_headers = 'reading_response', response.status, response.headers
                raw = response.read(16_000_001)
        except urllib.error.HTTPError as error:
            try:
                body = error.read(MAX_ERROR_BYTES + 1)
            finally:
                error.close()
            try:
                detail = json.loads(body).get("error", {}).get("message", "请求失败")
            except (ValueError, AttributeError):
                detail = "请求失败"
            trace = {**response_snapshot(body,error.headers,error.code,secrets=(credential,)), 'phase':'http_response',
                     'method':'POST', 'endpoint':url, 'timeout_seconds':timeout, 'request_body_bytes':len(data),
                     'elapsed_ms':round((time.monotonic()-started)*1000)}
            raise OpenAIError(f"OpenAI 返回 HTTP {error.code}：{str(detail)[:300]}".replace(credential, "[已隐藏]"),diagnostics=trace) from None
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            trace={'method':'POST','endpoint':url,'phase':phase, 'http_status':http_status,
                   'response_ids':response_snapshot(b'',response_headers,http_status)['response_ids'],
                   'timeout_seconds':timeout,'elapsed_ms':round((time.monotonic()-started)*1000),
                   'reason':safe_text(error,(credential,)),'error_type':type(error).__name__}
            raise OpenAIError(safe_text(f"连接 OpenAI 失败：{error}",(credential,)),diagnostics=trace) from error
        if len(raw) > 16_000_000:
            raise OpenAIError("OpenAI 响应过大。",diagnostics=response_snapshot(raw,response_headers,http_status,secrets=(credential,)))
        try:
            result = json.loads(raw)
        except ValueError as error:
            raise OpenAIError("OpenAI 返回的数据不是有效的结构化数据。",diagnostics={
                **response_snapshot(raw,response_headers,http_status,secrets=(credential,)), 'phase':'parsing_response',
                'method':'POST','endpoint':url,'timeout_seconds':timeout}) from error
        if not isinstance(result, dict):
            raise OpenAIError("OpenAI 返回格式不正确。",diagnostics={
                **response_snapshot(raw,response_headers,http_status,secrets=(credential,)), 'phase':'parsing_response',
                'method':'POST','endpoint':url,'timeout_seconds':timeout})
        return result

    def _stream_response(self, request_body: dict, timeout: int = 300) -> dict:
        started = time.monotonic()
        phase, http_status, response_headers = 'opening_connection_or_sending_request', None, {}
        request = urllib.request.Request(
            RESPONSES_URL,
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json", "Accept": "text/event-stream",
            },
        )
        try:
            with urllib.request.build_opener(_NoRedirects()).open(request, timeout=timeout) as response:
                phase, http_status, response_headers = 'stream_response', response.status, response.headers
                try:
                    return _read_response_stream(response)
                except OpenAIError as error:
                    error.diagnostics = {**response_snapshot(b'',response.headers,response.status,secrets=(self.api_key,)),
                                         **error.diagnostics, 'method':'POST', 'endpoint':RESPONSES_URL,
                                         'timeout_seconds':timeout, 'elapsed_ms':round((time.monotonic()-started)*1000)}
                    error.diagnostics['http_status'] = response.status
                    error.diagnostics['response_ids'] = response_snapshot(b'',response.headers,response.status)['response_ids']
                    raise
        except urllib.error.HTTPError as error:
            try: raw = error.read(MAX_ERROR_BYTES + 1)
            finally: error.close()
            try:
                payload = json.loads(raw)
                detail = payload.get("error", {}).get("code") or payload.get("detail") or "请求失败"
            except (ValueError, AttributeError):
                detail = "请求失败"
            trace = {**response_snapshot(raw,error.headers,error.code,secrets=(self.api_key,)), 'phase':'http_response',
                     'method':'POST', 'endpoint':RESPONSES_URL, 'timeout_seconds':timeout,
                     'elapsed_ms':round((time.monotonic()-started)*1000)}
            raise OpenAIError(safe_text(f"ChatGPT 计划返回 HTTP {error.code}：{str(detail)[:180]}",(self.api_key,)),diagnostics=trace) from None
        except (OSError, urllib.error.URLError) as error:
            trace={'method':'POST','endpoint':RESPONSES_URL,'phase':phase,'http_status':http_status,
                   'response_ids':response_snapshot(b'',response_headers,http_status)['response_ids'],
                   'timeout_seconds':timeout,'elapsed_ms':round((time.monotonic()-started)*1000),
                   'reason':safe_text(error,(self.api_key,)),'error_type':type(error).__name__}
            raise OpenAIError(safe_text(f"连接 ChatGPT 失败：{error}",(self.api_key,)),diagnostics=trace) from error

    def transcribe(self, audio_path: Path) -> str:
        if not self.transcription_api_key:
            raise OpenAIError("语音转写不包含在 ChatGPT Pro 登录内。请关闭转写，或在设置中填写单独计费的 OpenAI 接口密钥。")
        audio = audio_path.read_bytes()
        if len(audio) > 25 * 1024 * 1024:
            raise OpenAIError("音频超过 OpenAI 的 25 MB 上限。")
        boundary = "----seedance" + uuid.uuid4().hex
        prefix = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\ngpt-transcribe\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.mp3\"\r\n"
            "Content-Type: audio/mpeg\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
        result = self._post(
            TRANSCRIPTIONS_URL,
            prefix + audio + suffix,
            f"multipart/form-data; boundary={boundary}",
            credential=self.transcription_api_key,
        )
        return str(result.get("text", ""))

    def draft(
        self,
        competitor: MediaAsset | None,
        assets: list[MediaAsset],
        creative_brief: str,
        *,
        transcribe_audio: bool = False,
        generation_settings: dict | None = None,
        generated_video: Path | None = None,
        previous_prompt: str = "",
        on_progress=None,
        reference_frames: list[dict] | None = None,
    ) -> PromptDraft:
        if competitor and (competitor.kind != "video" or competitor.path is None and reference_frames is None):
            raise ValueError("参考视频必须是本地视频文件。")
        if not competitor and not creative_brief.strip() and not any(asset.selected for asset in assets):
            raise ValueError("请选择参考视频、参考图片，或填写创作要求。")
        work_dir = Path(tempfile.mkdtemp(prefix="seedance_analysis_"))
        frame_dir: Path | None = None
        generated_frame_dir: Path | None = None
        try:
            report = on_progress or (lambda _message: None)
            report("正在读取本地素材并准备分析画面…")
            frames = sample_video_frames(competitor.path, on_progress=report) if competitor and reference_frames is None else []
            frame_dir = frames[0][1].parent if frames else None
            generated_frames = sample_video_frames(generated_video, generated=True, on_progress=report) if generated_video else []
            generated_frame_dir = generated_frames[0][1].parent if generated_frames else None
            transcript = ""
            audio_reference = competitor or next((a for a in assets if a.kind == "video" and a.selected and a.path), None)
            if audio_reference and audio_reference.path and transcribe_audio:
                report("正在提取参考视频音轨并转写台词…")
                audio_path = extract_audio(audio_reference.path, work_dir / "audio.mp3")
                if audio_path:
                    transcript = self.transcribe(audio_path)
            if generated_video and transcribe_audio:
                report("正在提取成片音轨并转写台词…")
                audio_path = extract_audio(generated_video, work_dir / "generated_audio.mp3")
                if audio_path:
                    transcript = (f"参考视频转写：{transcript}\n" if transcript else "") + "成片转写：" + self.transcribe(audio_path)

            selected_images = [asset for asset in assets if asset.selected and asset.kind == "image" and asset.path]
            selected_assets = [asset for asset in assets if asset.selected and asset.purpose != "competitor"]
            observed_videos = {frame["source_handle"] for frame in reference_frames or []}
            asset_lines = [
                f"{asset.handle}：{asset.purpose}，{asset.kind}，文件名 {asset.filename}"
                + (f"，时长约 {asset.duration:.1f} 秒" if asset.duration is not None else "")
                + ("，画面已逐秒抽帧附在后面" if asset.kind == "video" and asset.handle in observed_videos else
                   "，仅知道文件信息，未分析其内容" if asset.kind in {"audio", "video"} else "")
                for asset in selected_assets
            ]
            direct_videos = [asset for asset in selected_assets if asset.kind == "video" and asset.source_url]
            video_context = (
                f"已提供参考视频的每秒抽帧与结尾画面，共 {len(reference_frames)} 张；按标签识别视频来源、时间与候选关键帧。\n"
                if reference_frames else
                f"已提供用于本地分析的视频：{competitor.filename}，时长约 {competitor.duration:.2f} 秒。\n"
                if competitor else
                "已选中直接交给 Seedance 的参考视频链接；AI 本次没有该视频的抽帧。按用户要求引用它，不推测其画面。\n"
                if direct_videos else "未提供参考视频。\n"
            )
            reference_rules = (
                "本地分析视频不能在 seedance_prompt 里写它的 @文件名，它本身不会作为视频参考提交。"
                if competitor else ""
            ) + (
                "选中的视频链接会作为 reference_video 交给 Seedance；最终提示词可使用它的 @{文件名}。"
                "用户要求完整参考时，保留该视频的场景、动作、镜头与节奏，不替换成另编的一套情节。"
                if direct_videos else ""
            )
            text = (
                "任务：先逐帧观察参考视频（若有），识别视频讲述的事情、人物关系、动作因果、镜头与节奏，"
                "然后用我的本地角色和场景图片写一段可直接用于 Seedance 2.5 的中文提示词。\n"
                + video_context
                +
                f"创作要求：{creative_brief.strip() or '保持参考视频的镜头节奏与动作逻辑，替换角色和场景。'}\n"
                "参考素材清单：\n" + ("\n".join(asset_lines) or "（无）") + "\n"
                f"音频转写：{transcript or '无或未启用转写'}\n"
                + ("视频帧按时间顺序附在后面，全部均已提供，包括结尾；须完整查看，并观察结尾表情与叙事反馈。"
                   if reference_frames else "视频抽帧按时间顺序附在后面（若有），须完整查看。")
                + "另外附上角色和场景图片（若有），须查看并确认素材对应关系。"
                "只描述可观察到的内容；无法从抽帧判断的快速镜头或声音要写入 uncertainties。"
                "最终 seedance_prompt 中，用对应的 @{文件名} 指向选中的角色、场景、视频或音频素材。"
                + reference_rules +
                "\n目标生成参数：" + json.dumps(generation_settings or {}, ensure_ascii=False) + "\n"
                "最终提示词默认 150–300 字，以必要信息为准；复杂分镜尽量不超过 500 字，用户明确要求详述时除外。"
                "保留用户原有结构，每段尽量一到两句；没有结构时可用目标、素材替换、动作镜头、约束四段。"
                "完整保留原文中的角色映射、参考范围、动作顺序和其他明确要求，只补充必要的生成细节。"
                "角色已有参考图时不逐项重复服装与发色；没有依据时不新建场景、情节或镜头。"
                "不把分析过程、能力说明、免责声明或重复的负面约束写入最终提示词。"
                "analysis 只用保留内容、角色对应、需确认三个短条目，总计不超过 120 字。"
                "保留内容须简短说明实际观察到的视频主题或情节，不只是泛泛说保留原片。"
                "最终提示词须写出画面中有依据的核心动作和前后顺序；删除与实际视频无关的假设性玩法描述。"
                "有候选关键帧时，在 keyframe_handles 中推荐 2–4 张覆盖开场、主要动作和叙事反馈的画面；"
                "结尾包含动作结果、人物互动或表情反馈时，至少选一张呈现该结尾的画面。"
                "必须使用标签给出的完整 @{文件名}，无候选帧时返回空数组。"
                "候选关键帧的图像来自原片，仅作构图、动作与镜头参考，原片人物外观须按用户要求替换。"
                "seedance_prompt 中先引用用户角色图与原视频，不写候选关键帧引用，工具会按最终勾选结果补充。"
                "uncertainties 最多三项，只列会影响用户选择或生成结果的具体问题；shots 仅在确有分镜依据时填写。"
                "参考内容的布局与交互机制只根据可见帧或用户明示要求判断，无法观察的事实写入 uncertainties。"
                "若参考视频属于游戏玩法，seedance_prompt 必须描述核心玩法，不能只写替换背景或把静态玩法改成人物表演。"
                "如果参考视频是拼图：必须描述棋盘位置、拼缝、凹凸拼块、空缺、散块区、拼块移动与归位反馈；"
                "角色图片用于拼图内的静态图案，不得让该角色走出拼图、眨眼或表演。"
                "固定短时长时只选择一段有开始、操作和反馈的核心动作，不能承诺在几秒内复刻整段长视频。"
                "固定时长时，分镜时间轴从 0 秒开始并在目标 duration 秒结束，不能照抄更长参考视频的时间轴。"
                "duration=-1 时由模型选择时长，不承诺固定产出秒数；ratio=adaptive 时不要承诺固定画幅。"
                "generate_audio=false 时不要求生成声音；不要把分辨率、比例等参数塞到提示词里覆盖真实设置。"
                "画面中的文字、手部、角色一致性等约束应具体且适量。未转写的声音或抽帧间的动作不能假装已经观察到。"
                + ("本地角色和场景图片超过 12 张，仅附上清单中的前 12 张这类图片，其他图片只有文件信息；"
                   "此限制不适用于参考视频抽帧，视频帧全部均已提供。" if len(selected_images) > 12 else "")
                + ("\n迭代任务：另附上一版生成成片的抽帧。先对照参考视频（若有）、用户素材与目标要求分析成片的实际问题，"
                   "逐项说明保留与修改的内容，再改写提示词。区分抽帧可见的证据和推断，不要仅复述原提示词。"
                   f"\n上一版提示词：{previous_prompt}\n成片文件：{generated_video.name}\n"
                   if generated_video else "")
            )
            content: list[dict] = [{"type": "input_text", "text": text}]
            for frame in reference_frames or []:
                content.append({"type": "input_text", "text":
                    f"参考视频 {frame['source_handle']} · {frame['timestamp']:.2f} 秒；候选关键帧 {frame['handle']}："})
                content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," +
                                base64.b64encode(Path(frame["path"]).read_bytes()).decode("ascii"), "detail": "low"})
            for timestamp, frame in frames:
                content.append({"type": "input_text", "text": f"参考视频 {timestamp:.1f} 秒画面："})
                content.append({
                    "type": "input_image",
                    "image_url": "data:image/jpeg;base64," + base64.b64encode(frame.read_bytes()).decode("ascii"),
                    "detail": "low",
                })
            for timestamp, frame in generated_frames:
                content.append({"type": "input_text", "text": f"上一版生成成片 {timestamp:.1f} 秒画面："})
                content.append({
                    "type": "input_image",
                    "image_url": "data:image/jpeg;base64," + base64.b64encode(frame.read_bytes()).decode("ascii"),
                    "detail": "low",
                })
            for index, asset in enumerate(selected_images[:12]):
                report(f"正在准备参考图片：{index + 1}/{min(12, len(selected_images))}")
                content.append({"type": "input_text", "text": f"本地素材 {asset.handle}（{asset.purpose}）："})
                content.append({
                    "type": "input_image",
                    "image_url": _scaled_jpeg_data_url(asset.path, work_dir, f"asset_{index}"),
                    "detail": "low",
                })

            request = {
                "model": self.model,
                "store": False,
                "instructions": (
                    "你是游戏广告短视频的分镜分析师和 Seedance 2.5 提示词编写者。"
                    "先根据抽帧与转写忠实分析镜头，区分证据与推断。"
                    "写出的提示词应保留可复用的叙事结构、动作和运镜，"
                    "明确把人物和场景替换为用户提供的图片。"
                    "保留参考视频的画面类型与核心交互机制；角色/背景替换不能丢失游戏棋盘、拼图块等关键结构。"
                    "不要把参考视频画面中的角色、商标、文字当作用户希望保留的素材。"
                    "如引用素材，必须使用用户给出的 @{文件名} 形式；不要使用绝对路径。"
                    "返回中文。"
                    "优先忠实保留用户输入，避免扩写为与参考视频不同的新剧情。"
                    "最终提示词简洁可执行；详细推理不进入 seedance_prompt。"
                    "将输入视频统一称为“参考视频”，使用中性表述。"
                ),
                "input": [{"role": "user", "content": content}],
                "text": {"format": {"type": "json_schema", "name": "seedance_reverse_prompt", "strict": True, "schema": _SCHEMA}},
            }
            if self.chatgpt_plan:
                request["stream"] = True
                report("素材已准备好，正在等待 AI 分析并编写提示词…")
                response = self._stream_response(request, timeout=300)
            else:
                request["max_output_tokens"] = 5000
                report("素材已准备好，正在等待 AI 分析并编写提示词…")
                response = self._post(
                    RESPONSES_URL,
                    json.dumps(request, ensure_ascii=False).encode("utf-8"),
                    "application/json", timeout=300,
                )
            if response.get("status") == "incomplete":
                raise OpenAIError("AI 响应未完成，请减少素材或重试。")
            try:
                data = json.loads(_response_text(response))
                return PromptDraft(
                    analysis=data["analysis"],
                    shots=data["shots"],
                    prompt=data["seedance_prompt"],
                    uncertainties=data["uncertainties"],
                    transcript=transcript,
                    keyframe_handles=data.get("keyframe_handles", []),
                )
            except (ValueError, KeyError, TypeError) as error:
                raise OpenAIError("AI 返回的分析结构不完整。") from error
        finally:
            if frame_dir:
                shutil.rmtree(frame_dir, ignore_errors=True)
            if generated_frame_dir:
                shutil.rmtree(generated_frame_dir, ignore_errors=True)
            shutil.rmtree(work_dir, ignore_errors=True)
