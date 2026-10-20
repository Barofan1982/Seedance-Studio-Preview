"""Validate image-to-shot bindings and the fixed-duration script timeline."""
import re


def validate_storyboard(data, images, settings):
    shots = data.get('shots', [])
    if not isinstance(shots, list) or any(not isinstance(shot, dict) for shot in shots):
        raise ValueError('AI 返回的分镜格式不完整，当前提示词未替换。')
    prompt = str(data.get('seedance_prompt', ''))
    visuals = '\n'.join(str(shot.get('visual', '')) for shot in shots)
    missing = [asset.handle for asset in images if asset.handle not in prompt or asset.handle not in visuals]
    if missing:
        raise ValueError('AI 分镜未完整引用图片，当前提示词未替换：' + '、'.join(missing))
    duration = settings.get('duration', -1)
    if duration in {-1, '-1'}:
        return
    cursor = 0.0
    prompt_cursor = 0
    pattern = r'\s*(\d+(?:\.\d+)?)\s*(?:秒|s)?\s*[-–—~～至到]\s*(\d+(?:\.\d+)?)\s*(?:秒|s)?\s*'
    for shot in shots:
        match = re.fullmatch(pattern, str(shot.get('time', '')), re.I)
        if not match:
            raise ValueError('AI 分镜时间格式不完整，请重新优化；需使用“0-3秒”这样的起止格式。')
        start, end = map(float, match.groups())
        if abs(start - cursor) > .01 or end <= start or end > float(duration) + .01:
            raise ValueError('AI 分镜时间有空档、重叠或超出目标时长，当前提示词未替换，请调整分镜要求。')
        # Validate the actual submitted text too, not only the auxiliary shots list.
        ranges = list(re.finditer(r'(\d+(?:\.\d+)?)\s*(?:秒|s)?\s*[-–—~～至到]\s*(\d+(?:\.\d+)?)\s*(?:秒|s)?', prompt[prompt_cursor:], re.I))
        found = next((value for value in ranges if abs(float(value[1])-start) <= .01 and abs(float(value[2])-end) <= .01), None)
        if not found:
            raise ValueError('AI 提示词未包含与分镜一致的完整时间轴，当前提示词未替换，请重新优化。')
        prompt_cursor += found.end()
        cursor = end
    if not shots or abs(cursor - float(duration)) > .01:
        raise ValueError('AI 分镜总时长与生成设置不一致，当前提示词未替换，请调整分镜要求。')
