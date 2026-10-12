"""Yuan estimates from provider token rates supplied by the user."""
from __future__ import annotations

import math
from decimal import Decimal

DEFAULT_CNY_RATES = {'without_video': 70.0, 'with_video': 42.0}


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


def estimate_cny(record, rates):
    if record.get('status') != 'succeeded':
        return None
    model = record.get('model', '')
    if not model.startswith('seedance-2.5-'):
        return None
    if model.endswith('-with-video'):
        kind = 'with_video'
    elif model.endswith('-no-video'):
        kind = 'without_video'
    else:
        return None
    price = rates.get(kind)
    tokens = record.get('completion_tokens')
    if (not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(price) or price < 0
            or not isinstance(tokens, (int, float)) or isinstance(tokens, bool) or not math.isfinite(tokens) or tokens < 0):
        return None
    return Decimal(str(tokens)) * Decimal(str(price)) / Decimal(1_000_000)


def cny_summary(records, rates):
    completed = [record for record in records if record.get('status') == 'succeeded']
    amounts = [estimate_cny(record, rates) for record in completed]
    known = [amount for amount in amounts if amount is not None]
    return {'amount': sum(known, Decimal(0)) if known or not completed else None,
            'priced': len(known), 'unpriced': len(completed) - len(known)}
