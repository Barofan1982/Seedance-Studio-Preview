"""Honest stage feedback and locally measured waiting estimates."""
from __future__ import annotations

import math
import statistics
import time
from ui_language import error_text
from seedance_client import CREATE_TIMEOUT_SECONDS

PHASES = ("提交", "排队", "生成", "保存")
STATUS_NAMES = {"submitting":"提交中", "queued":"排队中", "running":"生成中", "succeeded":"已完成",
                "failed":"失败", "unknown":"提交未确认", "cancelled":"已取消", "expired":"已过期", "draft":"提示词版本"}


def clock_text(seconds):
    value=max(0,int(seconds))
    return f"{value//60:02d}:{value%60:02d}"


def waiting_estimate(samples, settings, elapsed):
    similar=[r['seconds'] for r in samples if r['resolution']==settings.get('resolution')
             and r['model']==settings.get('model')
             and r['requested_duration']==settings.get('duration')]
    chosen=similar or [r['seconds'] for r in samples if r['resolution']==settings.get('resolution')]
    if not chosen:
        return "等待预估：暂无历史样本", "完成几次任务后，将按本机实际耗时估算。"
    median=statistics.median(chosen)
    lower=min(min(chosen),median*.8)
    upper=max(max(chosen),median*1.4)
    source=f"基于本机 {len(chosen)} 次{'同类' if similar else '同分辨率'}任务耗时估算，排队情况变化会影响时间。"
    if elapsed>=upper:
        return "已超过近期预估，仍在等待结果",source
    low=max(0,math.floor((lower-elapsed)/60))
    high=max(1,math.ceil((upper-elapsed)/60))
    return (f"预计还需 {low}–{high} 分钟" if low and low!=high else f"预计还需约 {high} 分钟以内"),source


def generation_feedback(call, row, timing, samples, settings, *, operation="", paused=False, now=None):
    now=time.time() if now is None else now
    result={"phase":-1,"active":False,"title":"成片将在这里显示","elapsed":"","estimate":"",
            "estimate_source":"","detail":"生成完成后自动保存到本机。","task_id":"","tone":"idle"}
    if not call and not row:
        return result
    status=call['status'] if call and call['status'] in {'running','unknown','failed'} else (row or {}).get('status','unknown')
    start=(timing or {}).get('submitted_at') or (call or {}).get('created_at') or (row or {}).get('created_at') or now
    finish=(timing or {}).get('finished_at')
    if call and status in {'unknown','failed'} and call.get('duration_ms') is not None:
        finish=call['created_at']+call['duration_ms']/1000
    elapsed=max(0,(finish or now)-start)
    result['elapsed']=f"{'耗时' if finish else '已等待'} {clock_text(elapsed)}"
    result['task_id']=(row or {}).get('task_id','')
    if call and call['status']=='running':
        result.update(phase=0,active=True,title="正在提交请求",tone="busy",task_id="",detail="等待服务器返回任务编号，请勿重复提交。")
    elif status in {'unknown','failed'} and (not row or call and call['status'] in {'unknown','failed'}):
        unknown=status=='unknown'
        result.update(title="提交未确认" if unknown else "提交失败",tone="warning",task_id="" if call and call['status'] in {'unknown','failed'} else result['task_id'],
                      detail="服务器可能已接单；请勿直接重试，先核对调用记录。" if unknown else error_text((call or {}).get('error') or (row or {}).get('error') or '请查看调用记录。')[:300])
    elif status in {'queued','running'}:
        result.update(phase=1 if status=='queued' else 2,active=True,tone="busy",
                      title="正在排队" if status=='queued' else "正在生成视频",
                      detail="完成后将自动保存到本机。" if not paused else "自动查询已暂停，点击“继续查询”更新任务状态。")
        result['estimate'],result['estimate_source']=waiting_estimate(samples,settings,elapsed)
        if paused:
            result['title']+=" · 查询暂停"
            result['active']=False
    elif status=='succeeded':
        available=(row or {}).get('local_available',False)
        result.update(phase=4 if available else 3,active=operation=='download',tone='good' if available else 'busy',
                      title="视频已保存" if available else "正在保存到本机" if operation=='download' else "生成完成 · 待保存",
                      detail="可播放成片，或载入模板继续修改。" if available else "服务端已生成；视频保存到本机后即可播放。")
    else:
        result.update(title=STATUS_NAMES.get(status,'状态待核对'),tone='warning',detail=error_text((row or {}).get('error') or '请查看调用记录。')[:300])
    if result['phase']==0:
        result['estimate']=f"提交响应等待上限约 {CREATE_TIMEOUT_SECONDS // 60} 分钟"
    return result
