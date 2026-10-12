"""Describe recorded generation outcomes without changing server task status."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HistoryOutcome:
    title: str
    tone: str
    category: str
    description: str = ''
    error: str = ''
    file_available: bool = False


def record_outcome(record: dict) -> HistoryOutcome:
    kind = record.get('record_kind', 'task')
    status = record.get('status', '')
    error = str(record.get('error') or '')
    if kind == 'prompt':
        return HistoryOutcome('仅提示词 · 非成片', 'neutral', 'prompt',
                              '此条保存智能优化后的提示词版本；视频结果请查看视频生成记录。')
    if status == 'submitting':
        return HistoryOutcome('提交中 · 等待确认', 'info', 'progress', '正在等待服务器返回任务编号。')
    if status == 'unknown' or (kind == 'submission' and status == 'succeeded'):
        description = ('未收到任务编号，是否接单及生成结果需后台核对；请先核对原提交，再决定是否重新生成。'
                       if not record.get('task_id') else '已有任务编号，但最终结果尚未确认，需要继续查询。')
        return HistoryOutcome('提交未确认' if not record.get('task_id') else '结果待核对',
                              'warning', 'uncertain', description, error)
    if status == 'failed':
        return HistoryOutcome('提交失败 · 无成片' if kind == 'submission' else '生成失败 · 无成片',
                              'error', 'failed',
                              '这次提交返回错误，未获得任务编号和成片。' if kind == 'submission'
                              else '服务端报告生成失败，未获得成片。',
                              error or '没有记录具体错误原因。')
    if status in {'cancelled', 'expired'}:
        return HistoryOutcome('已取消 · 无成片' if status == 'cancelled' else '已过期 · 无成片',
                              'neutral', 'closed', error=error)
    if status in {'queued', 'running'}:
        title = '排队中' if status == 'queued' else '生成中'
        if record.get('query_status') in {'failed', 'unknown'}:
            return HistoryOutcome(title + ' · 查询异常', 'warning', 'progress',
                                  '上次查询未成功，以上为最后确认状态；尚未取得最终结果。',
                                  str(record.get('query_error') or '查询结果未确认。'))
        return HistoryOutcome(title + ' · 尚无成片', 'info', 'progress', '任务尚未结束，正在等待结果。')
    if status == 'succeeded':
        path = Path(record['local_path']) if record.get('local_path') else None
        file_issue = ''
        if path:
            try:
                if path.is_file():
                    if path.stat().st_size > 0:
                        return HistoryOutcome('有成片 · 已保存', 'good', 'saved', file_available=True)
                    file_issue = '本地视频为空文件，无法播放。'
                else:
                    file_issue = '原保存位置找不到视频文件。'
            except OSError:
                file_issue = '目前无法读取原保存位置的视频文件。'
        if record.get('download_status') in {'failed', 'unknown'}:
            return HistoryOutcome('生成成功 · 保存失败', 'warning', 'unavailable',
                                  '服务端已生成视频，但保存到本机未成功。',
                                  str(record.get('download_error') or '没有记录具体保存错误。'))
        if record.get('download_status') == 'running':
            return HistoryOutcome('生成成功 · 正在保存', 'info', 'progress', '正在下载成片，保存完成后即可播放。')
        if file_issue:
            return HistoryOutcome('生成成功 · 文件异常' if '空文件' in file_issue else '生成成功 · 本地缺失',
                                  'warning', 'unavailable', file_issue)
        if not record.get('video_url'):
            return HistoryOutcome('生成成功 · 未返回文件', 'warning', 'unavailable',
                                  '服务器报告生成成功，但记录中没有视频地址和本地文件，需要核对返回结果。')
        return HistoryOutcome('生成成功 · 待保存', 'warning', 'unavailable', '服务端已生成视频，尚未保存到本机。')
    return HistoryOutcome('状态待核对', 'warning', 'uncertain',
                          '记录没有明确的最终状态，需要核对原任务。', error)
