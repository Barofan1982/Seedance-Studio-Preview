"""Model-specific estimates and separately reported relay charges. USD:CNY is 1:7."""
from __future__ import annotations

import math
import json
from decimal import Decimal
from copy import deepcopy
from seedance_models import DEFAULT_VIDEO_MODEL, RELAY_ALIAS, RELAY_MODEL_PROFILES, OFFICIAL_PRICES

DEFAULT_CNY_RATES = {'without_video': 70.0, 'with_video': 42.0}
USD_CNY = Decimal(7)


def parse_cny_rates(values):
    rates = {}
    for kind in DEFAULT_CNY_RATES:
        text = str(values.get(kind, '')).strip()
        if not text:
            continue
        try:
            price = float(text)
        except (TypeError, ValueError):
            raise ValueError('人民币单价请填写有效的非负数字，或留空表示未设置。') from None
        if not math.isfinite(price) or price < 0:
            raise ValueError('人民币单价必须是有效的非负数字。')
        rates[kind] = price
    return rates


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def pricing_snapshot(rates):
    """Freeze configured and reference prices when the user enqueues this request."""
    return {**deepcopy(rates), '_official_prices': deepcopy(OFFICIAL_PRICES), '_usd_cny': 7}


def billing_details(record, rates):
    model = record.get('model', '')
    payload = _object(record.get('request_json'))
    snapshot = _object(record.get('billing_rates_json'))
    prices = snapshot or rates
    relay = bool(RELAY_ALIAS.fullmatch(str(model)))
    content = payload.get('content')
    known_input = relay or isinstance(content, list)
    has_video = model.endswith('-with-video') if relay else any(
        item.get('type') == 'video_url' for item in (content if isinstance(content, list) else []) if isinstance(item, dict))
    kind = 'with_video' if has_video else 'without_video'
    price_model = DEFAULT_VIDEO_MODEL if relay else model
    model_rates = prices.get('models', {}).get(price_model, {})
    resolution = record.get('actual_resolution') or payload.get('resolution') or record.get('resolution')
    if not resolution and relay and '480p-720p' in model:
        resolution = '720p'  # These two tiers share the same rate; do not invent an output size.
    official_id = RELAY_MODEL_PROFILES.get(model, model)
    if relay or model == DEFAULT_VIDEO_MODEL:
        official_id = 'doubao-seedance-2-5-260628'
    official = prices.get('_official_prices', OFFICIAL_PRICES).get(official_id, {}).get(resolution)
    reference = official[1 if has_video else 0] if official and known_input else None
    price, source = None, '未设置单价'
    if resolution in model_rates:
        price = model_rates[resolution].get(kind)
        source = '自定义单价' if _number(price) else '未设置单价'
    elif relay and resolution in {'480p', '720p'} and _number(prices.get(kind)):
        price, source = prices[kind], '已确认中转单价'
    elif prices.get('official_reference', True) and _number(reference):
        price, source = reference, '官方参考价'
    tokens = record.get('completion_tokens')
    amount = (Decimal(str(tokens)) * Decimal(str(price)) / Decimal(1_000_000)
              if record.get('status') == 'succeeded' and known_input and _number(price) and _number(tokens) else None)
    seconds = record.get('actual_duration')
    if amount is not None:
        reason = ('提交时保存的' if snapshot else '当前') + source + '估算'
    elif record.get('status') != 'succeeded':
        reason = '任务未成功，暂不估算'
    elif not _number(tokens):
        reason = '未返回计费 tokens'
    elif not known_input:
        reason = '视频输入类型未记录'
    else:
        reason = '该模型或档位未设置单价'
    return {'amount': amount, 'usd': amount / USD_CNY if amount is not None else None,
            'per_second': amount / Decimal(str(seconds)) if amount is not None and _number(seconds) and seconds > 0 else None,
            'rate': Decimal(str(price)) if _number(price) and known_input else None,
            'source': source, 'reason': reason, 'kind': kind if known_input else '', 'tokens': tokens,
            'model': model, 'actual_model': record.get('actual_model') or '', 'resolution': resolution,
            'snapshot': bool(snapshot)}


def estimate_cny(record, rates):
    return billing_details(record, rates)['amount']


def billing_text(record, rates):
    detail = billing_details(record, rates)
    if detail['amount'] is None:
        return detail['reason']
    text = f"费用估算：¥{detail['amount']:.2f} / ${detail['usd']:.2f}"
    if detail['per_second'] is not None:
        text += f" · 每秒 ¥{detail['per_second']:.2f}"
    text += (f"\n{detail['tokens']:,} tokens × ¥{detail['rate']:.2f} / 百万"
             f" · {'含视频输入' if detail['kind'] == 'with_video' else '无视频输入'} · {detail['reason']}")
    if detail['actual_model']:
        text += '\n接口返回模型：' + detail['actual_model']
    return text


def relay_usage_rows(response):
    """Aggregate receipts are not per-task receipts or a quote for future requests."""
    items = response.get('model_stats', []) if isinstance(response, dict) else []
    if not isinstance(items, list):
        return []
    rows = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('model'), str):
            continue
        value = item.get('actual_cost')
        usd = Decimal(str(value)) if _number(value) else None
        tokens, count = item.get('output_tokens'), item.get('requests')
        rows.append({'model': item['model'], 'requests': count if _number(count) else None,
                     'tokens': tokens if _number(tokens) else None, 'usd': usd,
                     'cny': usd * USD_CNY if usd is not None else None,
                     'effective_rate_usd': usd * 1_000_000 / Decimal(str(tokens))
                     if usd is not None and _number(tokens) and tokens > 0 else None})
    return rows


def cny_summary(records, rates):
    completed = [record for record in records if record.get('status') == 'succeeded']
    amounts = [estimate_cny(record, rates) for record in completed]
    known = [amount for amount in amounts if amount is not None]
    return {'amount': sum(known, Decimal(0)) if known or not completed else None,
            'priced': len(known), 'unpriced': len(completed) - len(known)}
