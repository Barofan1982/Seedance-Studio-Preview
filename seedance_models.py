"""Seedance model capabilities. Discovery is availability evidence, not a routing map."""
from __future__ import annotations

from dataclasses import dataclass
import re

DEFAULT_VIDEO_MODEL = 'seedance-2.5-auto'
RATIOS = ('16:9', '4:3', '1:1', '3:4', '9:16', '21:9', 'adaptive')
MODE_NAMES = {'reference': '全模态参考', 'first_frame': '首帧图片',
              'first_last_frame': '首尾帧图片', 'text': '纯文本'}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    resolutions: tuple[str, ...]
    modes: tuple[str, ...]
    min_seconds: int
    max_seconds: int
    adaptive: bool = True
    audio: bool = True
    images: int = 0
    videos: int = 0
    audios: int = 0
    total_media: int = 0
    reference_seconds: int = 0
    audio_only: bool = False
    formats: tuple[str, ...] = ('mp4',)
    task_type_field: bool = False
    first_adaptive_only: bool = False
    seed: bool = False
    legacy: bool = False

    def ratios(self, mode):
        if self.first_adaptive_only and mode in {'first_frame', 'first_last_frame'}:
            return ('adaptive',)
        if not self.adaptive and mode == 'text':
            return RATIOS[:-1]
        return RATIOS

    def durations(self):
        return ((-1,) if self.adaptive else ()) + tuple(range(self.min_seconds, self.max_seconds + 1))


_ALL_MODES = tuple(MODE_NAMES)
_FRAME_MODES = ('first_frame', 'first_last_frame', 'text')
_RES = ('480p', '720p', '1080p')
MODEL_SPECS = {
    s.id: s for s in (
        ModelSpec(DEFAULT_VIDEO_MODEL, 'Seedance 2.5 · 中转自动档位', _RES, _ALL_MODES, 4, 30,
                  images=30, videos=10, audios=10, total_media=50, reference_seconds=30,
                  audio_only=True, formats=('mp4', 'mov'), task_type_field=True, first_adaptive_only=True),
        ModelSpec('doubao-seedance-2-5-260628', 'Seedance 2.5', _RES, _ALL_MODES, 4, 30,
                  images=30, videos=10, audios=10, total_media=50, reference_seconds=30,
                  audio_only=True, formats=('mp4', 'mov'), task_type_field=True, first_adaptive_only=True),
        ModelSpec('doubao-seedance-2-0-260128', 'Seedance 2.0', (*_RES, '4k'), _ALL_MODES, 4, 15,
                  images=9, videos=3, audios=3, total_media=15, reference_seconds=15),
        ModelSpec('doubao-seedance-2-0-fast-260128', 'Seedance 2.0 Fast', _RES[:2], _ALL_MODES, 4, 15,
                  images=9, videos=3, audios=3, total_media=15, reference_seconds=15),
        ModelSpec('doubao-seedance-2-0-mini-260615', 'Seedance 2.0 Mini', _RES[:2], _ALL_MODES, 4, 15,
                  images=9, videos=3, audios=3, total_media=15, reference_seconds=15),
        ModelSpec('doubao-seedance-1-5-pro-251215', 'Seedance 1.5 Pro · 历史版本', _RES, _FRAME_MODES,
                  4, 12, seed=True, legacy=True),
        ModelSpec('doubao-seedance-1-0-pro-250528', 'Seedance 1.0 Pro · 即将下线', _RES, _FRAME_MODES,
                  2, 12, adaptive=False, audio=False, seed=True, legacy=True),
        ModelSpec('doubao-seedance-1-0-pro-fast-251015', 'Seedance 1.0 Pro Fast · 即将下线', _RES,
                  ('first_frame', 'text'), 2, 12, adaptive=False, audio=False, seed=True, legacy=True),
    )
}
RELAY_ALIAS = re.compile(r'^seedance-2\.5-(480p-720p|1080p)-(no-video|with-video)$')
# Exact IDs returned by the configured relay on 2026-10-21. Profiles supply
# local capability checks only; resolve_model always preserves these IDs.
RELAY_MODEL_PROFILES = {
    'seedance-2.5': 'doubao-seedance-2-5-260628',
    'seedance-2.0-vip': 'doubao-seedance-2-0-260128',
    'seedance-2.0-fast-vip': 'doubao-seedance-2-0-fast-260128',
    'seedance-2.0-mini': 'doubao-seedance-2-0-mini-260615',
}
RELAY_MODEL_NAMES = {
    'seedance-2.5': 'Seedance 2.5',
    'seedance-2.0-vip': 'Seedance 2.0 VIP',
    'seedance-2.0-fast-vip': 'Seedance 2.0 Fast VIP',
    'seedance-2.0-mini': 'Seedance 2.0 Mini',
}
# The product exposes only the four models requested by the user. API catalogs
# and historical capability profiles remain separate from this menu.
VISIBLE_VIDEO_MODELS = ('seedance-2.5', 'seedance-2.0-mini', 'seedance-2.0-fast-vip', 'seedance-2.0-vip')


def model_picker_identity(model):
    if model == DEFAULT_VIDEO_MODEL or RELAY_ALIAS.fullmatch(str(model)):
        return 'seedance-2.5'
    return model if model in VISIBLE_VIDEO_MODELS else ''


def valid_model_id(value):
    value = str(value).strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', value):
        raise ValueError('模型 ID 不能为空，且只能包含字母、数字、点、横线、下划线、冒号或斜线。')
    return value


def model_spec(model=DEFAULT_VIDEO_MODEL, profile=''):
    model = valid_model_id(model)
    if model in MODEL_SPECS:
        return MODEL_SPECS[model]
    if model in RELAY_MODEL_PROFILES:
        return MODEL_SPECS[RELAY_MODEL_PROFILES[model]]
    if RELAY_ALIAS.fullmatch(model):
        return MODEL_SPECS[DEFAULT_VIDEO_MODEL]
    if profile in MODEL_SPECS:
        return MODEL_SPECS[profile]
    raise ValueError(f'模型 {model} 尚无参数配置，请在“添加模型”中选择对应的参数模板。')


def resolve_model(model, resolution, has_video):
    model = valid_model_id(model)
    if model == DEFAULT_VIDEO_MODEL:
        if resolution not in _RES:
            raise ValueError('Seedance 2.5 不支持该分辨率。')
        return f"seedance-2.5-{'1080p' if resolution == '1080p' else '480p-720p'}-{'with-video' if has_video else 'no-video'}"
    # Exact official IDs, endpoint IDs and custom relay aliases are never rewritten.
    return model


def selection_from_payload(payload, template=None):
    template = template or {}
    exact = payload.get('model', DEFAULT_VIDEO_MODEL)
    # Older records only have the resolved relay tier. Reuse their automatic tier selection.
    return template.get('generation_model') or (DEFAULT_VIDEO_MODEL if RELAY_ALIAS.fullmatch(exact) else exact)


def model_name(model):
    if model in RELAY_MODEL_NAMES:
        return RELAY_MODEL_NAMES[model]
    if model in MODEL_SPECS:
        return MODEL_SPECS[model].name
    if RELAY_ALIAS.fullmatch(str(model)):
        tier = '1080p' if '-1080p-' in str(model) else '480/720p'
        return f'Seedance 2.5 · {tier} · ' + ('有视频' if str(model).endswith('with-video') else '无视频')
    return str(model)


def state_spec(state):
    return model_spec(state.get('generation_model', DEFAULT_VIDEO_MODEL), state.get('generation_profile', ''))


def normalize_settings(state):
    """Only adjust incompatible output settings; never discard selected source assets."""
    spec = state_spec(state)
    changes = []
    allowed_modes = [MODE_NAMES[m] for m in spec.modes]
    if state['mode'] not in allowed_modes:
        state['mode'] = '纯文本' if 'text' in spec.modes else allowed_modes[0]
        changes.append('生成方式')
    mode = next(m for m, name in MODE_NAMES.items() if name == state['mode'])
    for key, values, default in (
        ('resolution', spec.resolutions, '720p'),
        ('duration', tuple(str(n) for n in spec.durations()), '5'),
        ('ratio', spec.ratios(mode), 'adaptive' if mode != 'text' or spec.adaptive else '16:9'),
        ('output_format', spec.formats, 'mp4'),
    ):
        if state.get(key) not in values:
            state[key] = default if default in values else values[0]
            changes.append({'resolution': '分辨率', 'duration': '时长', 'ratio': '比例', 'output_format': '视频格式'}[key])
    if not spec.audio and state.get('generate_audio'):
        state['generate_audio'] = False
        changes.append('声音')
    return changes


# Official online list prices, checked 2026-10-21. Estimates only, not relay receipts.
# Each pair is CNY / million output tokens: without video, with video.
OFFICIAL_PRICES = {
    'doubao-seedance-2-5-260628': {'480p': (70, 42), '720p': (70, 42), '1080p': (77, 46)},
    'doubao-seedance-2-0-260128': {'480p': (46, 28), '720p': (46, 28), '1080p': (51, 31), '4k': (26, 16)},
    'doubao-seedance-2-0-fast-260128': {'480p': (37, 22), '720p': (37, 22)},
    'doubao-seedance-2-0-mini-260615': {'480p': (23, 14), '720p': (23, 14)},
    'doubao-seedance-1-0-pro-250528': {res: (15, None) for res in _RES},
    'doubao-seedance-1-0-pro-fast-251015': {res: (4.2, None) for res in _RES},
}
PRICE_SOURCE = 'https://docs.volcengine.com/docs/ark/model-pricing?lang=zh'
