"""Simplified Chinese presentation; API identifiers and original diagnostics stay intact."""
from __future__ import annotations

import re

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator


CHOICE_NAMES = {'adaptive': '自适应', 'mp4': 'MP4', 'mov': 'MOV'}

FIELD_NAMES = {
    'reasoning':'思考配置', 'effort':'思考强度', 'reasoning_effort':'思考强度',
    'ai_reasoning_effort':'分析思考强度', 'ai_reasoning_efforts':'各模型思考强度', 'thinking':'思考模式',
    'connection_route':'网络路径', 'proxy_address':'代理地址', 'input_image_count':'分析图片数量',
    'request':'提交请求', 'payload':'请求正文', 'model':'模型', 'content':'输入内容',
    'type':'内容类型', 'text':'文字', 'image_url':'图片链接', 'video_url':'视频链接', 'audio_url':'音频链接',
    'url':'网址', 'role':'素材用途', 'resolution':'分辨率', 'ratio':'画面比例', 'duration':'时长（秒）',
    'seed':'随机种子', 'camera_fixed':'固定镜头', 'generate_audio':'生成声音', 'watermark':'添加水印',
    'return_last_frame':'返回尾帧', 'output_format':'输出格式', 'omni_reference_task_type':'参考任务类型',
    'id':'任务编号', 'task_id':'任务编号', 'parent_task_id':'来源任务编号', 'call_id':'调用编号',
    'status':'状态', 'error':'报错原因', 'code':'错误代码', 'message':'说明', 'mode':'生成方式',
    'assets':'素材', 'source_assets':'源素材', 'path':'本地文件', 'local_path':'本地视频位置',
    'source_url':'公开素材网址', 'handle':'素材引用', 'kind':'素材类型', 'purpose':'用途', 'selected':'已选择',
    'original_prompt':'原始提示词', 'prompt':'提示词', 'creative_brief':'创作要求', 'brief':'创作要求',
    'draft':'创作方案', 'prompt_draft':'创作方案', 'analysis':'内容核对', 'shots':'分镜',
    'uncertainties':'待确认问题', 'keyframe_handles':'关键帧引用', 'frame_count':'分析画面数量',
    'frames':'画面', 'frame_catalog':'抽帧列表', 'source_handle':'来源素材', 'timestamp':'画面时间（秒）',
    'generation_settings':'生成设置', 'previous_prompt':'上一版提示词', 'generated_video':'已生成视频',
    'usage':'用量', 'completion_tokens':'生成计费单位', 'total_tokens':'总计费单位',
    'requested_duration':'设定时长（秒）', 'actual_duration':'产出时长（秒）',
    'estimated_cost_usd':'旧版美元估算', 'created_at':'创建时间', 'updated_at':'更新时间',
    'operation':'操作', 'duration_ms':'耗时（毫秒）', 'response':'返回结果', 'network_diagnostics':'网络诊断',
    'auth_mode':'连接方式', 'billing_source':'用量来源', 'audio_transcription':'语音转写', 'enabled':'已启用',
    'width':'宽度（像素）', 'height':'高度（像素）', 'has_audio':'包含声音', 'media_info':'成片信息',
    'models':'模型列表', 'display_name':'显示名称', 'slug':'模型标识', 'visibility':'显示方式',
    'request_json':'原始请求参数', 'response_json':'原始返回参数', 'assets_json':'素材清单',
    'media_json':'成片信息', 'template_json':'模板信息', 'seedance_prompt':'生成提示词',
    'method':'请求方式', 'endpoint':'接口地址', 'phase':'请求阶段', 'http_status':'响应状态码',
    'response_ids':'服务器响应编号', 'timeout_seconds':'等待上限（秒）', 'elapsed_ms':'已耗时（毫秒）',
    'request_body_bytes':'请求大小（字节）', 'error_type':'错误类型', 'reason_type':'原因类型',
    'reason':'原因', 'errno':'错误编号', 'winerror':'系统错误编号',
    'record_type':'记录类型', 'size':'大小（字节）', 'sha256':'校验值', 'bytes':'字节数',
    'base64_chars':'编码字符数', 'mime_type':'媒体类型', 'framespersecond':'帧率',
    'service_tier':'服务等级', 'execution_expires_after':'任务有效期（秒）', 'priority':'优先级',
    'manual_prompt':'使用手写提示词', 'auto_analyze':'生成前自动分析', 'review_output':'对比成片', 'ai_original_text':'优化前原文',
    'generated_keyframe_binding':'关键帧素材绑定', 'analysis_signature':'分析校验值',
    'source_expires_at':'参考地址到期时间', 'upload_id':'上传媒体编号', 'upload_file_size':'上传文件大小',
    'upload_file_mtime_ns':'上传文件版本', 'expires_at':'参考地址到期时间', 'size_bytes':'文件大小（字节）',
    'filename':'文件名', 'uploaded':'已上传视频', 'file_size':'文件大小（字节）', 'file_mtime_ns':'文件版本',
    'service':'服务',
    'diagnostics':'上传耗时诊断', 'connect_and_send_ms':'连接与发送耗时（毫秒）',
    'waiting_response_ms':'等待中转响应耗时（毫秒）', 'upload_progress':'上传进度',
    'sent_bytes':'已发送大小（字节）', 'total_bytes':'视频大小（字节）', 'percent':'发送进度（百分比）',
    'elapsed_seconds':'总耗时（秒）', 'phase_seconds':'当前阶段耗时（秒）', 'index':'当前文件序号', 'count':'文件数量',
    'started_at':'开始时间', 'phase_started_at':'阶段开始时间', 'removed_assets':'已移除素材', 'removed_at':'移除时间',
    'library_id':'素材库编号', 'library_ids':'素材库编号列表', 'snapshot_path':'本地备份文件',
    'original_file_size':'原文件大小', 'original_file_mtime_ns':'原文件版本', 'added_at':'加入素材库时间',
    'error_diagnostics':'错误诊断', 'api_diagnostics':'接口诊断', 'api_error_code':'业务错误码',
    'api_error_message':'接口错误原文', 'response_body':'完整响应正文', 'response_ids':'服务器响应编号',
    'response_content_type':'响应格式', 'response_body_complete':'响应是否完整',
    'response_body_bytes_read':'已保存响应字节数', 'response_body_limit_bytes':'响应记录大小上限',
    'response_body_note':'响应记录说明', 'retry_after':'建议等待时间', 'exception_chain':'异常来源',
}

ENUM_NAMES = {
    'api_key': '接口密钥',
    'reference':'参考生视频', 'first_frame':'首帧', 'first_last_frame':'首尾帧', 'last_frame':'尾帧',
    'reference_image':'参考图片', 'reference_video':'参考视频', 'reference_audio':'参考音频',
    'edit':'编辑视频', 'extend':'延长视频', 'text':'文字', 'image':'图片', 'video':'视频', 'audio':'音频',
    'character':'角色图', 'scene':'场景图', 'competitor':'视频', 'other':'参考素材', 'keyframe':'关键帧',
    'queued':'排队中', 'running':'进行中', 'succeeded':'已完成', 'failed':'失败', 'unknown':'未确认',
    'cancelled':'已取消', 'expired':'已过期', 'submitting':'提交中', 'draft':'提示词版本',
    'create':'提交生成', 'query':'查询结果', 'download':'保存成片', 'refine':'对比优化',
    'models':'读取模型列表', 'sign_in':'连接账号', 'chatgpt_plan':'ChatGPT 会员登录',
    'upload_login':'登录上传账号', 'upload_video':'上传视频', 'restore_images':'从素材库恢复图片',
    'clear_workspace':'保存快照并清空创作', 'restore_workspace':'恢复创作快照',
    'list':'显示', 'default':'默认', 'GET':'读取', 'POST':'提交',
    'opening_connection_or_sending_request':'建立连接或发送请求', 'reading_response':'读取响应',
    'connecting_or_sending':'连接或发送视频', 'waiting_response':'等待中转响应',
    'validating':'校验视频', 'refreshing_account':'续期上传账号',
}


def choice_text(value):
    return CHOICE_NAMES.get(str(value).lower(), str(value))


def parameter_value(value, key=''):
    if value is None:
        return '未提供'
    if isinstance(value, bool):
        return '是' if value else '否'
    if key in {'duration', 'requested_duration'} and value in {-1, '-1'}:
        return '自适应'
    text = str(value)
    if key in {'effort', 'reasoning_effort', 'ai_reasoning_effort'}:
        from reasoning_settings import EFFORT_LABELS
        return EFFORT_LABELS.get(text, text)
    if key == 'billing_source':
        return text.replace('OpenAI API 单独计费', 'OpenAI 接口单独计费')
    if key in {'error', 'reason'} and text:
        return error_text(text)
    if key == 'model' and text.startswith('seedance-2.5-'):
        return 'Seedance 2.5 · ' + ('有视频参考' if 'with-video' in text else '无视频参考')
    if key in {'type', 'role', 'mode', 'purpose', 'kind', 'status', 'operation', 'auth_mode', 'phase', 'visibility', 'service_tier', 'method'}:
        return ENUM_NAMES.get(text, choice_text(text))
    if key in {'ratio', 'output_format'}:
        return choice_text(value)
    return text or '（空）'


def install_chinese_translation(application):
    """Translate Qt's standard buttons, input dialogs and text context menus."""
    QLocale.setDefault(QLocale('zh_CN'))
    if hasattr(application, '_studio_chinese_translators'):
        return
    directory = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    translators = []
    for catalog in ('qtbase_zh_CN', 'qtmultimedia_zh_CN'):
        translator = QTranslator(application)
        if translator.load(catalog, directory):
            application.installTranslator(translator)
            translators.append(translator)
    application._studio_chinese_translators = translators


def error_text(value):
    """Show a Chinese explanation, leaving the original available in diagnostic details."""
    text = str(value or '').strip()
    status = re.search(r'(?:HTTP\s*|状态码\s*)(\d{3})', text, re.IGNORECASE)
    prefix = f'状态码 {status.group(1)}：' if status else ''
    explanations = (
        (r'request body must be a JSON object', '请求正文必须是结构化对象，请核对提交参数。'),
        (r'no eligible seedance accounts|no available accounts|no accounts', '中转站没有可调度的视频生成账号，请联系维护人员核对账号状态。'),
        (r'invalid api key|incorrect api key|unauthorized|invalid authentication credentials', '接口密钥无效或鉴权未通过，请检查连接设置。'),
        (r'reference video format is not supported|unsupported (?:video|media|file) format', '不支持该参考文件的格式，请检查视频格式和编码。'),
        (r'failed to (?:fetch|download)|unable to download', '无法读取参考文件，请检查链接是否可公开访问。'),
        (r'timed?\s*out|timeout', '等待响应超时，请核对原任务状态。'),
        (r'connection reset|forcibly closed|remote end closed connection', '连接被服务器中断，请核对原任务状态。'),
        (r'connection refused', '无法连接服务器，请检查服务地址和网络连接。'),
        (r'invalid data found when processing input|invalid media|could not open file', '无法解析素材文件，请检查文件是否完整以及格式是否受支持。'),
        (r'permission denied|access is denied', '无法访问文件或目录，请检查访问权限。'),
        (r'no space left on device|disk full', '磁盘空间不足，无法保存文件。'),
        (r'no such file or directory|file not found', '找不到指定文件，请检查原文件是否已移动。'),
        (r'rate limit|too many requests', '请求过于频繁，请稍后重试。'),
        (r'insufficient quota|quota exceeded', '可用额度不足，请检查对应服务的用量。'),
    )
    for pattern, message in explanations:
        if re.search(pattern, text, re.IGNORECASE):
            return prefix + message
    # Chinese messages can contain filenames, URLs and product names. Preserve them.
    # Untranslated exception tails after a colon belong in original diagnostics.
    for separator in ('：', ': '):
        if separator in text:
            heading, tail = text.split(separator, 1)
            if re.search(r'[\u4e00-\u9fff]', heading) and re.search(r'[A-Za-z]{3,}', tail) and not re.search(r'[\u4e00-\u9fff]', tail):
                return heading + '，详细原因请查看原始报错。'
    if re.search(r'[\u4e00-\u9fff]', text):
        return text
    return prefix + '操作未完成，详细原因请查看原始报错。'
