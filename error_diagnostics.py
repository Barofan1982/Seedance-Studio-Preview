"""Complete, credential-safe error evidence and conservative troubleshooting hints."""
from __future__ import annotations

import datetime
import json
import re
from request_records import compact_record

MAX_ERROR_BYTES = 2_000_000
TZ = datetime.timezone(datetime.timedelta(hours=8))
STAGES = {
    'import': '导入素材', 'restore_images': '恢复图片', 'upload_login': '登录上传账号',
    'upload_video': '上传参考视频', 'draft': '抽帧与提示词优化', 'refine': '成片对比优化',
    'create': '提交视频生成', 'query': '查询视频任务', 'generation': '云端生成视频',
    'download': '下载与保存成片', 'models': '读取分析平台模型', 'sign_in': '登录分析平台',
    'validation': '本地参数或文件校验', 'configure_api': '配置分析接口',
    'clear_workspace': '保存快照并清空创作', 'restore_workspace': '恢复创作快照',
}
PHASE_NAMES = {
    'opening_connection_or_sending_request': '建立连接、发送请求或等待响应头（尚不能进一步区分）',
    'reading_response': '读取接口返回', 'parsing_response': '解析接口返回',
    'checking_task_id': '核对创建响应中的任务编号', 'http_response': '接口返回 HTTP 错误',
    'business_response': '接口返回业务错误', 'waiting_response': '文件已发送，等待中转响应',
    'connecting_or_sending': '连接或发送视频', 'validating': '校验素材',
    'local_operation': '本地处理', 'cloud_generation': '任务已创建，云端生成失败',
    'stream_response': '读取 ChatGPT 分析结果',
    'writing_output': '写入本地成片文件', 'finalizing_file': '完成本地文件保存',
}


def safe_text(value, secrets=()):
    text = str(compact_record(str(value or ''), secrets))
    # URLs may also appear inside an HTML error page or an exception message.
    text = re.sub(r'https?://[^\s<>"\']+', lambda m: str(compact_record(m.group(), secrets)), text)
    text = re.sub(r'''(?i)(["']?(?:password|access_token|refresh_token|accessToken|refreshToken|api_key|authorization|cookie|id_token)["']?\s*[=:]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;<>]+)''',
                  r'\1"[已隐藏]"', text)
    return text


def safe_value(value, secrets=()):
    value = compact_record(value, secrets)
    if isinstance(value, dict): return {key: safe_value(item, secrets) for key,item in value.items()}
    if isinstance(value, list): return [safe_value(item, secrets) for item in value]
    return safe_text(value, secrets) if isinstance(value, str) else value


def error_fields(value):
    if not isinstance(value, dict):
        return '', ''
    nested = value.get('error') or value.get('Error')
    metadata = value.get('ResponseMetadata') or value.get('response_metadata')
    if not nested and isinstance(metadata, dict):
        nested = metadata.get('Error') or metadata.get('error')
    if isinstance(nested, dict):
        return (str(nested.get('code') or nested.get('Code') or nested.get('type') or ''),
                str(nested.get('message') or nested.get('Message') or nested.get('detail') or ''))
    code = value.get('code', value.get('Code', ''))
    message = value.get('message') or value.get('Message') or value.get('msg') or value.get('detail') or nested or ''
    return str(code if code is not None else ''), str(message)


def response_snapshot(raw: bytes, headers=None, status=None, *, secrets=(), complete=True):
    truncated = len(raw) > MAX_ERROR_BYTES
    text = raw[:MAX_ERROR_BYTES].decode('utf-8', errors='replace')
    try:
        parsed = json.loads(text)
    except (ValueError, UnicodeError):
        parsed = None
    clean = safe_value(parsed, secrets)
    code, message = error_fields(clean)
    ids = {name: safe_text(headers.get(name), secrets) for name in
           ('x-request-id', 'x-tt-logid', 'request-id', 'x-correlation-id', 'traceparent', 'cf-ray')
           if headers and headers.get(name)}
    if isinstance(clean, dict):
        for item in (clean, clean.get('error'), clean.get('Error'), clean.get('ResponseMetadata')):
            if isinstance(item, dict):
                for key in ('request_id', 'requestId', 'RequestId', 'RequestID', 'trace_id'):
                    if item.get(key): ids[key] = str(item[key])
    blocked = parsed is None and status in (403, 429) and bool(re.search(r'\bRequest\s+Blocked\b', text, re.I))
    if parsed is None:
        for name, pattern in (('event_id', r'Event\s+ID\s*:\s*([A-Za-z0-9_+=/-]{8,256})'),
                              ('request_id', r'Request\s+ID\s*:\s*([A-Za-z0-9_+=/-]{8,256})')):
            match = re.search(pattern, text, re.I)
            if match:
                ids.setdefault(name, safe_text(match[1], secrets))
    if blocked and not message:
        message = 'Request Blocked'
    return {
        'http_status': status, 'response_ids': ids, 'api_error_code': code, 'api_error_message': message,
        'access_blocked': blocked,
        'response_body': json.dumps(clean, ensure_ascii=False, indent=2) if parsed is not None else safe_text(text, secrets),
        'response_json': clean, 'response_content_type': headers.get('Content-Type', '') if headers else '',
        'response_body_bytes_read': min(len(raw), MAX_ERROR_BYTES),
        'response_body_complete': complete and not truncated,
        'response_body_limit_bytes': MAX_ERROR_BYTES,
        'response_body_note': '响应超过 2 MB，仅保存前 2 MB。' if truncated else
                              '响应读取中断，只保留已收到的部分。' if not complete else '',
        'retry_after': headers.get('Retry-After', '') if headers else '',
    }


def exception_diagnostics(error, operation='', *, secrets=()):
    diagnostics = getattr(error, 'diagnostics', {})
    diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
    chain, seen, current = [], set(), error
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append({'type': type(current).__name__, 'message': safe_text(current, secrets),
                      'errno': getattr(current, 'errno', None), 'winerror': getattr(current, 'winerror', None)})
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    diagnostics.setdefault('phase', 'local_operation')
    diagnostics.setdefault('error_type', type(error).__name__)
    diagnostics.setdefault('reason', safe_text(error, secrets))
    diagnostics['exception_chain'] = chain
    diagnostics['operation'] = operation
    return safe_value(diagnostics, secrets)


def possible_causes(code='', message='', status=None, phase='', *, operation='', endpoint=''):
    value = f'{code} {message}'.lower()
    if 'request blocked' in value:
        return ['平台访问防护拦截了请求；可能涉及出口 IP、访问策略或风控，不足以判定为账号额度问题。携服务器事件编号联系平台支持；不要连续重试。']
    if 'no eligible seedance accounts' in value or 'no available accounts' in value:
        return ['中转站没有可调度的 Seedance 账号；需维护人员检查分组、账号状态、Ark 能力和模型映射。']
    if 'tasktypeconstraint' in value:
        return ['请求参数与模型判定的任务类型不兼容；按返回内容中的 Issues 逐项核对。']
    if any(s in value for s in ('sensitivecontent', 'riskdetection', 'contentrejected', 'policyviolation')):
        return ['输入或生成内容未通过服务端审核；核对接口返回中具体涉及的文本、图片或视频。']
    if any(s in value for s in ('invalidimageurl', 'failed to fetch', 'failed to download', 'unable to download')):
        return ['服务端无法读取素材链接，或素材编码无效；检查地址是否过期、是否公开可访问，以及文件格式。']
    if any(s in value for s in ('unsupported', 'formatunsupported', 'invalid media')):
        return ['参数或素材格式不受当前模型支持；核对视频编码、尺寸、时长和生成方式。']
    if 'json object' in value or 'invalidparameter' in value or status in (400, 422):
        return ['请求正文或参数不符合接口要求；核对返回内容指出的字段和实际提交参数。']
    if status == 401 or any(s in value for s in ('apikey.invalid', 'invalid api key', 'unauthorized')):
        return ['鉴权未通过；核对当前接口地址对应的密钥，或上传账号登录是否过期。']
    if status == 403:
        return ['当前账号、分组或模型权限不足；也可能被服务端访问规则拒绝。']
    if 'quota' in value or 'balance' in value or status == 402:
        return ['额度或余额不足；核对对应服务的账单和可用额度。']
    if status == 404:
        return ['接口路径或任务编号不存在；核对中转站是否提供此接口及记录中的任务编号。']
    if status == 413:
        return ['请求或文件超过服务端大小限制；核对参考图片请求体或上传视频大小。']
    if status == 429 or 'rate limit' in value:
        return ['服务端并发、频率或额度限制；查看返回内容与 Retry-After，并让维护人员核对限制。']
    if status in (408, 504):
        return ['中转或上游先达到自己的等待上限；延长本地等待时间不能改变服务端超时设置。']
    if status and status >= 500:
        return ['中转站或上游服务异常；具体处理环节需用响应编号、任务编号及时间核对后台日志。']
    if any(s in value for s in ('timeout', 'timed out', '超时', '没有收到确认')):
        return ['连接、传输或服务端等待耗时超过本地上限；没有收到响应头时无法判定是网络还是中转处理。',
                '创建请求可能已被接收，需维护人员核对；不能据此判断没有扣费。']
    if any(s in value for s in ('connection reset', 'remote end closed', 'connectionreset', 'remotedisconnected')):
        if endpoint.startswith('https://api.openai.com/'):
            return ['网络、代理或 OpenAI 服务提前关闭连接；没有收到响应头，不能确定由哪一端关闭。',
                    '这条记录来自 AI 分析接口；核对代理线路及平台状态，与 Seedance 中转账号无关。',
                    '分析端可能已接收请求；确认网络稳定后可手动重新优化，可能再次消耗平台额度。工具不会自动重发。']
        return ['网络、网关或服务端提前关闭连接；需按时间和请求编号核对中转日志。']
    if operation == 'download':
        return ['核对成片地址是否有效、下载网络、目标目录权限和磁盘空间；云端生成结果可能仍然存在。']
    if phase in ('parsing_response', 'checking_task_id'):
        return ['接口返回格式不符合约定，或创建响应没有任务编号；需维护人员核对实际返回内容。']
    return ['现有信息不足以确定根因；请用完整返回内容、错误码和响应编号让维护人员核对。']


def json_value(value):
    if isinstance(value, (dict, list)): return value
    try: return json.loads(value or '{}')
    except (ValueError, TypeError): return {}


def call_issue(call):
    response = json_value(call.get('response_json'))
    return bool(call.get('status') in {'failed', 'unknown'} or call.get('error') or
                isinstance(response, dict) and (response.get('status') == 'failed' or response.get('error')))


def call_details(call):
    response = json_value(call.get('response_json'))
    response = response if isinstance(response, dict) else {}
    diag = response.get('error_diagnostics') or response.get('network_diagnostics') or response.get('api_diagnostics') or response.get('diagnostics') or {}
    diag = dict(diag) if isinstance(diag, dict) else {}
    code, message = error_fields(response)
    code = diag.get('api_error_code') or code
    message = diag.get('api_error_message') or message or call.get('error') or ''
    status = diag.get('http_status')
    if status is None:
        match = re.search(r'(?:HTTP\s*|状态码\s*)(\d{3})', call.get('error') or '', re.I)
        if match: status = int(match.group(1))
    operation = call.get('operation', '')
    cloud_failed = operation == 'query' and response.get('status') == 'failed'
    phase = 'cloud_generation' if cloud_failed else diag.get('phase', '')
    return {'stage': STAGES.get('generation' if cloud_failed else operation, operation or '未记录环节'),
            'phase': PHASE_NAMES.get(phase, phase or '旧记录未保存具体网络阶段'),
            'http_status': status, 'code': str(code or ''), 'message': str(message or ''),
            'diagnostics': diag, 'response': response, 'issue': call_issue(call),
            'causes': possible_causes(str(code or ''), str(message or '')+' '+str(diag.get('reason','')),
                                     status, phase, operation=operation, endpoint=diag.get('endpoint', ''))}


def call_report(call):
    info = call_details(call)
    created = datetime.datetime.fromtimestamp(call.get('created_at') or 0, TZ).strftime('%Y-%m-%d %H:%M:%S +08:00')
    diag = info['diagnostics']
    request = json_value(call.get('request_json'))
    request = request if isinstance(request, dict) else {}
    auth_mode = diag.get('auth_mode') or request.get('auth_mode')
    status = {'running':'进行中', 'succeeded':'调用已完成', 'failed':'调用失败', 'unknown':'提交未确认'}.get(call.get('status'), '未记录')
    if call.get('operation') == 'query' and info['response'].get('status') == 'failed':
        status = '查询已完成，云端生成失败'
    lines = [f"调用记录：#{call.get('call_id') or '未编号'}", f'时间：{created}', f"环节：{info['stage']}",
             f"阶段：{info['phase']}", f'调用结果：{status}', f"任务编号：{call.get('task_id') or '未收到'}",
             f"接口：{diag.get('method','')} {diag.get('endpoint') or '旧记录未保存接口地址'}",
             f"HTTP 状态码：{info['http_status'] if info['http_status'] is not None else '未收到 / 未记录'}",
             f"业务错误码：{info['code'] or '接口未提供 / 旧记录未保存'}",
             f"耗时：{(call.get('duration_ms') or diag.get('elapsed_ms') or 0)/1000:.2f} 秒",
             f"等待上限：{str(diag['timeout_seconds'])+' 秒' if diag.get('timeout_seconds') else '旧记录未保存'}",
             f"请求大小：{diag.get('request_body_bytes', '旧记录未保存')} 字节",
             f"服务器响应编号：{json.dumps(diag.get('response_ids') or {}, ensure_ascii=False)}",
             '', '接口或本地错误原文：', info['message'] or '本条调用没有报错。']
    context = []
    effort = diag.get('reasoning_effort', request.get('reasoning_effort'))
    if effort is not None:
        from reasoning_settings import EFFORT_LABELS
        context.append('思考强度：' + EFFORT_LABELS.get(effort, str(effort)))
    if auth_mode:
        context.append('授权方式：' + {'chatgpt_plan':'ChatGPT 会员授权', 'api_key':'接口密钥（单独计费）'}.get(auth_mode, str(auth_mode)))
    if diag.get('connection_route'):
        context.append('网络路径：' + str(diag['connection_route']) +
                       ('（' + str(diag['proxy_address']) + '）' if diag.get('proxy_address') else ''))
    if 'input_image_count' in diag:
        context.append(f"分析图片数：{diag['input_image_count']} 张（含抽帧与本地参考图）")
    lines[7:7] = context
    if info['issue']:
        lines += ['', '可能原因（排查建议，尚未确认根因）：'] + ['• '+cause for cause in info['causes']]
    if call.get('status') == 'unknown':
        lines += ['', '服务器是否接单及扣费尚未确认；不能根据超时直接重发创建请求。']
    if not diag.get('response_body'):
        lines += ['', '接口返回正文：本条记录未收到或未保存正文；不能补造 HTTP 或业务错误码。']
    elif diag.get('response_body_note'):
        lines += ['', diag['response_body_note']]
    return '\n'.join(lines)
