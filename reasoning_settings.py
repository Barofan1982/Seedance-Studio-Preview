"""Model-aware effort choices and provider-specific request parameters."""

EFFORT_LABELS = {
    'none': '关闭思考', 'minimal': '极低', 'low': '低', 'medium': '中',
    'high': '高', 'xhigh': '超高', 'max': '最高', 'ultra': '极限',
}
DEEPSEEK_EFFORTS = ('none', 'low', 'high', 'max')
DEEPSEEK_MODEL_IDS = ('deepseek-flash', 'deepseek-v4-pro')


def supported_efforts(provider, model):
    if not isinstance(model, dict):
        return []
    if provider == 'deepseek':
        return list(DEEPSEEK_EFFORTS) if model.get('slug') in DEEPSEEK_MODEL_IDS else []
    levels = model.get('supported_reasoning_levels', [])
    if not isinstance(levels, list):
        return []
    available = set()
    for item in levels:
        effort = item.get('effort') if isinstance(item, dict) else item
        if isinstance(effort, str):
            available.add(effort)
    return [effort for effort in EFFORT_LABELS if effort in available]


def selected_effort(provider, model, saved=None):
    options = supported_efforts(provider, model)
    if not options:
        return None
    if saved in options:
        return saved
    if 'high' in options:
        return 'high'
    default = model.get('default_reasoning_level')
    return default if default in options else options[0]


def deepseek_reasoning(effort='high'):
    if effort not in DEEPSEEK_EFFORTS:
        raise ValueError('当前 DeepSeek 模型不支持该思考强度。')
    if effort == 'none':
        return {'thinking': {'type': 'disabled'}}
    return {'thinking': {'type': 'enabled'}, 'reasoning_effort': effort}
