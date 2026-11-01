"""OpenAI Standard API 等值估算；定价来源、核验日期与未知项明确显示。"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Optional

from .token_usage import ModelUsage, TokenCounts

PRICING_URL = 'https://developers.openai.com/api/docs/pricing'
PRICING_CHECKED = '2026-10-03'


@dataclass(frozen=True)
class ModelPrice:
    name: str
    input: float
    cached_input: float
    output: float
    cache_write: Optional[float] = None
    long_context: bool = True

    @property
    def source(self):
        return f'https://developers.openai.com/api/docs/models/{self.name}'

    def value(self, counts, long_context=False):
        inputs = counts.input * (2 if long_context and self.long_context else 1)
        cached = counts.cached_input * (2 if long_context and self.long_context else 1)
        writes = (counts.cache_write or 0) * (2 if long_context and self.long_context else 1)
        outputs = counts.output * (1.5 if long_context and self.long_context else 1)
        # 缓存命中、缓存写入都是输入的子集，不能再加到普通输入中。
        write_rate = self.cache_write if self.cache_write is not None else self.input
        return ((inputs-cached-writes)*self.input + cached*self.cached_input +
                writes*write_rate + outputs*self.output)/1_000_000


# USD / 1M tokens。逐个核对官方模型页，旧模型仍按自己的价格计值。
PRICES = {price.name: price for price in (
    ModelPrice('gpt-6.1-sol', 2, .10, 10, 2.50),
    ModelPrice('gpt-6-sol', 2, .20, 10, 2.50),
    ModelPrice('gpt-6-astra', 10, 1, 50, 12.50),
    ModelPrice('gpt-6-luna', .10, .01, .50, .125),
    ModelPrice('gpt-5.6-sol', 4, .40, 20, 5),
    ModelPrice('gpt-5.6-terra', 2, .20, 12, 2.50),
    ModelPrice('gpt-5.6-luna', .20, .02, 1.20, .25),
    ModelPrice('gpt-5.5', 5, .50, 30),
    ModelPrice('gpt-5.4', 2.50, .25, 15),
    ModelPrice('gpt-5.3-codex', 1.75, .175, 14, long_context=False),
)}


def normalize_model(value):
    name = str(value or '').strip().lower()
    if name == 'gpt-5.6':
        name = 'gpt-5.6-sol'  # 官方模型页明确记录的别名。
    if name not in PRICES:
        name = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', name)
    return name


def money(value, *, compact=False):
    if value is None or not math.isfinite(value) or value < 0:
        return '—'
    if compact and value >= 10000:
        return f'${value/1000:.1f}K'
    if 0 < value < .001:
        return '<$0.001'
    if 0 < value < .01:
        return f'${value:.3f}'
    return f'${value:,.2f}'


@dataclass
class ValueEstimate:
    observed_usd: Optional[float] = None
    used_usd: Optional[float] = None
    remaining_usd: Optional[float] = None
    usd_per_million: Optional[float] = None
    priced_tokens: int = 0
    unpriced_tokens: int = 0
    used_is_lower_bound: bool = False
    basis: str = ''
    note: str = ''
    currency: str = 'USD'
    pricing_checked: str = PRICING_CHECKED
    pricing_url: str = PRICING_URL

    def to_dict(self):
        return asdict(self)


def estimate_value(estimate, model='auto'):
    result = ValueEstimate(note='API 等值估算，非订阅账单；采用 Standard 单价，不含工具费用、Fast 和地区附加费。')
    if estimate is None or not estimate.observed_tokens:
        result.note += ' 尚无本机输入 / 输出样本，无法估算价值。'
        return result
    groups = estimate.model_usage or [ModelUsage(counts=TokenCounts(
        estimate.observed_tokens, estimate.input_tokens, estimate.cached_input_tokens, estimate.output_tokens))]
    override = normalize_model(model) if model != 'auto' else None
    unknown, models, details = set(), set(), []
    amount = 0.
    for group in groups:
        name = override or normalize_model(group.model)
        price = PRICES.get(name)
        if price is None:
            unknown.add(name or '未记录模型')
            result.unpriced_tokens += group.counts.total
            continue
        counts = group.counts
        if (counts.input+counts.output != counts.total or min(counts.total, counts.input,
                counts.cached_input, counts.output, counts.cache_write or 0) < 0 or
                counts.cached_input+(counts.cache_write or 0) > counts.input):
            result.note += ' token 分类数据不完整，无法估算价值。'
            return result
        cost = price.value(counts, group.long_context is True)
        amount += cost
        result.priced_tokens += counts.total
        models.add(name)
        details.append(f'{name}: 输入 ${price.input:g} / 缓存 ${price.cached_input:g} / 输出 ${price.output:g} 每百万 tokens')
        if group.long_context is True and price.long_context:
            details.append('该组请求超过 272K 输入：输入与缓存 ×2，输出 ×1.5。')
        elif group.long_context is None and price.long_context:
            details.append('缺少单次请求输入长度，按短上下文基准估算。')
        if price.cache_write is not None and counts.cache_write is None:
            details.append('日志没有缓存写入分类，未命中缓存的输入按普通输入估算。')
    result.basis = ('指定模型 ' if override else '日志模型 ') + ', '.join(sorted(models))
    if unknown:
        result.note += (' 无已核验价格：'+', '.join(sorted(unknown))+
                       f'（{result.unpriced_tokens:,} tokens）。本机观测价值仅为可计价部分；'
                       '已用 / 剩余预测采用其余已识别模型的比例；可在设置 → 价格指定折算模型。')
    if not result.priced_tokens:
        return result
    if sum(group.counts.total for group in groups) != estimate.observed_tokens:
        result.note += ' 模型分组与总 tokens 不一致，无法估算价值。'
        return result
    result.observed_usd = amount
    unit = amount/result.priced_tokens
    result.usd_per_million = unit*1_000_000
    if estimate.estimated_used is not None:
        result.used_usd = estimate.estimated_used*unit
    else:
        result.used_usd = amount
        result.used_is_lower_bound = bool(result.unpriced_tokens)
    if estimate.estimated_remaining is not None:
        result.remaining_usd = estimate.estimated_remaining*unit
    result.note += (' 已用 / 剩余预测沿用本机观测的模型和输入 / 缓存 / 输出比例；'
                    '同一 tokens 会出现在 5h 和每周两个窗口，不应把两组价值相加。\n')
    result.note += '\n'.join(dict.fromkeys(details)) + f'\n官方价格核验：{PRICING_CHECKED} · {PRICING_URL}'
    return result
