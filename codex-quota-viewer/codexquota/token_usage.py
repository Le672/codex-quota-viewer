"""本机 token 观测与额度标定。订阅百分比并非固定 token 上限。"""
from __future__ import annotations

import hashlib
import copy
import json
import math
import time
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .providers import QuotaWindow, Snapshot, newest_session_files, parse_usage


@dataclass(frozen=True)
class TokenCounts:
    total: int = 0
    input: int = 0
    cached_input: int = 0
    output: int = 0
    cache_write: Optional[int] = None


@dataclass
class ModelUsage:
    model: str = ''
    counts: TokenCounts = field(default_factory=TokenCounts)
    long_context: Optional[bool] = None
    requests: int = 0


@dataclass
class TokenEvent:
    ts: float
    identity: str
    counts: TokenCounts
    windows: list[QuotaWindow]
    plan: str = ''
    model: str = ''
    request_input_tokens: Optional[int] = None


@dataclass
class TokenEstimate:
    kind: str = ''
    observed_tokens: Optional[int] = None
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    estimated_used: Optional[int] = None
    estimated_remaining: Optional[int] = None
    tokens_per_percent: Optional[float] = None
    calibration_points: int = 0
    calibration_gain: float = 0
    observed_calls: int = 0
    observed_since: Optional[float] = None
    note: str = '尚无本机 token 样本'
    model_usage: list[ModelUsage] = field(default_factory=list)

    @property
    def uncached_input_tokens(self):
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def cache_hit_rate(self):
        return self.cached_input_tokens / self.input_tokens if self.input_tokens else None

    def to_dict(self):
        return dict(asdict(self), uncached_input_tokens=self.uncached_input_tokens,
                    cache_hit_rate=self.cache_hit_rate)


@dataclass
class TokenReport:
    windows: dict[str, TokenEstimate] = field(default_factory=dict)
    scanned_files: int = 0
    scanned_at: float = 0
    session: Optional[TokenEstimate] = None
    session_id: str = ''
    session_activity_at: float = 0

    def to_dict(self):
        return dict(windows={key: value.to_dict() for key, value in self.windows.items()},
                    scanned_files=self.scanned_files, scanned_at=self.scanned_at,
                    session=self.session.to_dict() if self.session else None,
                    session_id=self.session_id, session_activity_at=self.session_activity_at)


def compact_tokens(value):
    if value is None:
        return '—'
    if value >= 1_000_000:
        return f'{value/1_000_000:.1f}M'
    if value >= 1000:
        return f'{value/1000:.1f}K'
    return str(round(value))


def _timestamp(value):
    try:
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            result = float(value)
        else:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result = parsed.timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _counts(value):
    if not isinstance(value, dict):
        return None
    def integer(key):
        number = value.get(key, 0)
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or number < 0:
            raise ValueError(key)
        return int(number)
    try:
        inputs, outputs = integer('input_tokens'), integer('output_tokens')
        total = inputs + outputs if 'input_tokens' in value or 'output_tokens' in value else integer('total_tokens')
        # cached_input 是 input 的子集；reasoning_output 已包含在 output 中。
        cached = min(inputs, integer('cached_input_tokens'))
        writes = min(inputs-cached, integer('cache_write_tokens')) if 'cache_write_tokens' in value else None
        return TokenCounts(total, inputs, cached, outputs, writes)
    except (ValueError, OverflowError):
        return None


def _accumulate(result, event):
    """累计观测值；缓存输入是输入子集，不再重复加到总量。"""
    counts = event.counts
    result.observed_tokens = (result.observed_tokens or 0) + counts.total
    result.input_tokens += counts.input
    result.cached_input_tokens += counts.cached_input
    result.output_tokens += counts.output
    result.observed_calls += 1
    long_context = event.request_input_tokens > 272000 if event.request_input_tokens is not None else None
    group = next((item for item in result.model_usage if
                  (item.model, item.long_context, item.counts.cache_write is not None) ==
                  (event.model, long_context, counts.cache_write is not None)), None)
    if group is None:
        group = ModelUsage(model=event.model, long_context=long_context,
                           counts=TokenCounts(cache_write=0 if counts.cache_write is not None else None))
        result.model_usage.append(group)
    old = group.counts
    writes = (old.cache_write or 0) + counts.cache_write if counts.cache_write is not None else None
    group.counts = TokenCounts(old.total + counts.total, old.input + counts.input,
                              old.cached_input + counts.cached_input, old.output + counts.output, writes)
    group.requests += 1


@dataclass
class _FileState:
    offset: int = 0
    mtime_ns: int = 0
    previous: Optional[TokenCounts] = None
    provider: str = ''
    model: str = ''
    events: list[TokenEvent] = field(default_factory=list)
    session_id: str = ''
    started_at: float = 0
    activity_at: float = 0
    session: TokenEstimate = field(default_factory=lambda: TokenEstimate(kind='session',
        note='最近活动的本机会话；累计本会话可观测请求，包含缓存输入；继承历史不重复累计。'))
    session_seen: set[str] = field(default_factory=set)


class TokenScanner:
    """只解析元数据及 token_count；后续刷新只读取新增内容，忽略半行。"""
    def __init__(self):
        self._files: dict[Path, _FileState] = {}
        self._lock = threading.Lock()

    def scan(self, snapshot: Snapshot, *, paths=None, now=None):
        with self._lock:
            return self._scan(snapshot, paths=paths, now=now)

    def _scan(self, snapshot, *, paths=None, now=None):
        now = time.time() if now is None else now
        cutoff = now - 14*86400
        selected = list(paths) if paths is not None else newest_session_files(240)
        active = set()
        combined = []
        for path in selected:
            path = Path(path)
            try:
                stat = path.stat()
                if stat.st_mtime < cutoff and paths is None:
                    continue
                active.add(path)
                state = self._files.setdefault(path, _FileState())
                # 文件被截断 / 就地重写时重新读取，不能沿用旧累加器。
                if stat.st_size < state.offset or (stat.st_size == state.offset and stat.st_mtime_ns != state.mtime_ns):
                    state = self._files[path] = _FileState()
                if stat.st_size > state.offset:
                    self._read(path, state, cutoff, now)
                state.mtime_ns = stat.st_mtime_ns
                state.events = [event for event in state.events if event.ts >= cutoff]
                combined.extend(state.events)
            except (OSError, ValueError, TypeError):
                continue
        self._files = {path: state for path, state in self._files.items() if path in active}
        report = estimate_tokens(snapshot, combined, now=now, scanned_files=len(active))
        sessions = [(path, state) for path, state in self._files.items()
                    if state.provider == 'openai' and 0 < state.activity_at <= now]
        if sessions:
            path, state = max(sessions, key=lambda item: (item[1].activity_at, str(item[0])))
            report.session = copy.deepcopy(state.session)
            report.session_id = state.session_id or path.stem
            report.session_activity_at = state.activity_at
        return report

    def _read(self, path, state, cutoff, now):
        with path.open('rb') as stream:
            stream.seek(state.offset)
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line or not line.endswith(b'\n'):
                    state.offset = offset
                    break
                state.offset = stream.tell()
                if not any(marker in line for marker in (b'"session_meta"', b'"turn_context"', b'"token_count"')):
                    continue
                try:
                    record = json.loads(line)
                    payload = record.get('payload') or {}
                    if record.get('type') == 'session_meta':
                        state.provider = payload.get('model_provider', '')
                        state.model = str(payload.get('model') or '')
                        state.session_id = str(payload.get('id') or '')
                        state.started_at = _timestamp(payload.get('timestamp') or record.get('timestamp')) or 0
                        if state.started_at <= now:
                            state.activity_at = max(state.activity_at, state.started_at)
                        state.session.observed_since = state.started_at or None
                        continue
                    if record.get('type') == 'turn_context':
                        state.model = str(payload.get('model') or '')
                        activity = _timestamp(record.get('timestamp')) or 0
                        if activity <= now:
                            state.activity_at = max(state.activity_at, activity)
                        continue
                    if record.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                        continue
                    # 自定义 provider / API-key 的 token 不与 ChatGPT 订阅额度混算。
                    if state.provider != 'openai':
                        continue
                    info = payload.get('info') or {}
                    cumulative = _counts(info.get('total_token_usage'))
                    last = _counts(info.get('last_token_usage'))
                    timestamp = _timestamp(record.get('timestamp'))
                    if timestamp is None or timestamp > now:
                        continue
                    delta = TokenCounts()
                    if cumulative:
                        previous = state.previous
                        if previous and cumulative.total >= previous.total and cumulative.input >= previous.input and cumulative.output >= previous.output:
                            inputs = cumulative.input-previous.input
                            writes = (max(0, cumulative.cache_write-previous.cache_write)
                                      if cumulative.cache_write is not None and previous.cache_write is not None else None)
                            delta = TokenCounts(cumulative.total-previous.total, inputs,
                                                min(inputs, max(0, cumulative.cached_input-previous.cached_input)),
                                                cumulative.output-previous.output, writes)
                        elif last:
                            # 首条 / 累计计数重启，只记本次请求，不把继承的累计历史算成新消耗。
                            delta = last
                        state.previous = cumulative
                    elif last:
                        delta = last
                    rate = payload.get('rate_limits')
                    windows, meta = parse_usage({'rate_limits': rate}) if isinstance(rate, dict) else ([], {})
                    # fork 复制的历史事件有相同原始时间与计数，跨文件去重。
                    identity_data = [record.get('timestamp'), info.get('total_token_usage'), info.get('last_token_usage'), rate]
                    identity = hashlib.sha256(json.dumps(identity_data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    event = TokenEvent(timestamp, identity, delta, windows,
                        str(meta.get('plan_type') or rate.get('plan_type') or ''), state.model,
                        last.input if last else None) if isinstance(rate, dict) else TokenEvent(
                            timestamp, identity, delta, [], model=state.model,
                            request_input_tokens=last.input if last else None)
                    state.activity_at = max(state.activity_at, timestamp)
                    if timestamp >= state.started_at and delta.total > 0 and identity not in state.session_seen:
                        state.session_seen.add(identity)
                        _accumulate(state.session, event)
                        if state.session.observed_since is None:
                            state.session.observed_since = timestamp
                    if timestamp >= cutoff and windows:
                        state.events.append(event)
                except (ValueError, TypeError, AttributeError):
                    continue


def _matches(first, second):
    return (first.limit_id, first.slot, first.window_minutes) == (second.limit_id, second.slot, second.window_minutes)


def estimate_tokens(snapshot, events, *, now=None, scanned_files=0):
    now = time.time() if now is None else now
    # 只保留真实已经发生的记录；fork 历史及完全重复事件只计一次。
    unique = {}
    for event in events:
        if event.ts <= now and (event.identity not in unique or
                (event.counts.total, bool(event.model)) >
                (unique[event.identity].counts.total, bool(unique[event.identity].model))):
            unique[event.identity] = event
    ordered = sorted(unique.values(), key=lambda event: event.ts)
    report = TokenReport(scanned_files=scanned_files, scanned_at=now)
    for window in snapshot.windows:
        result = TokenEstimate(kind=window.kind)
        report.windows[window.kind] = result
        if not window.resets_at or not window.window_minutes or window.resets_at <= now:
            result.note = '额度窗口时间不可用或已到期，等待刷新'
            continue
        start = window.resets_at-window.window_minutes*60
        eligible = []
        for event in ordered:
            if event.ts < start or event.ts > now:
                continue
            if event.plan and snapshot.account.plan_type and event.plan.lower() != snapshot.account.plan_type.lower():
                continue
            local = next((local for local in event.windows if _matches(local, window) and
                          local.resets_at and abs(local.resets_at-window.resets_at) <= 120), None)
            if local:
                eligible.append((event, local))
        if not eligible:
            continue
        # 同一自然周期中消费卡片也会清零；只标定最后一个连续增长段。
        boundary = 0
        for index in range(1, len(eligible)):
            if eligible[index-1][1].used_percent-eligible[index][1].used_percent >= 2:
                boundary = index
        if boundary:
            start = eligible[boundary][0].ts
            eligible = eligible[boundary:]
        measured = [event for event, local in eligible if event.counts.total > 0 and (not boundary or event.ts > start)]
        result.observed_calls = len(measured)
        result.observed_since = start
        if measured:
            result.observed_tokens = sum(event.counts.total for event in measured)
            result.input_tokens = sum(event.counts.input for event in measured)
            result.cached_input_tokens = sum(event.counts.cached_input for event in measured)
            result.output_tokens = sum(event.counts.output for event in measured)
            groups = {}
            for event in measured:
                long_context = event.request_input_tokens > 272000 if event.request_input_tokens is not None else None
                key = (event.model, long_context, event.counts.cache_write is not None)
                group = groups.setdefault(key, ModelUsage(model=event.model, long_context=long_context))
                old, new = group.counts, event.counts
                writes = ((old.cache_write or 0)+new.cache_write) if new.cache_write is not None else None
                group.counts = TokenCounts(old.total+new.total, old.input+new.input,
                                          old.cached_input+new.cached_input, old.output+new.output, writes)
                group.requests += 1
            result.model_usage = list(groups.values())
        else:
            result.note = '只有额度记录，尚无可累加的本机 token 样本'
            continue
        result.note = '本机记录的输入 + 输出 tokens（缓存输入已计入输入，推理已计入输出）；其他设备和云端未统计'
        # 避免整数百分比抖动放大预测：至少 3 个有效额度点、5 个百分点、60 秒。
        points = []
        for event, local in eligible:
            if not points or local.used_percent > points[-1][1]+.01:
                points.append((event.ts, local.used_percent))
        if len(points) < 3:
            result.note += '；标定样本不足，预计剩余暂不可用'
            continue
        first, last = points[0], points[-1]
        gain = last[1]-first[1]
        if gain < 5 or last[0]-first[0] < 60 or now-last[0] > 1800 or window.used_percent+1 < last[1]:
            result.note += '；额度变化不足、样本过旧或额度发生重置，预计剩余暂不可用'
            continue
        # 首个额度点之前的请求不参与两点之间的消耗，避免偏移一个请求。
        token_gain = sum(event.counts.total for event, local in eligible if first[0] < event.ts <= last[0])
        if token_gain <= 0:
            result.note += '；没有可标定的 token 消耗'
            continue
        rate = token_gain/gain
        result.tokens_per_percent = rate
        result.calibration_points = len(points)
        result.calibration_gain = gain
        result.estimated_used = round(max(0, min(100, window.used_percent))*rate)
        result.estimated_remaining = round(window.remaining_percent*rate)
        result.note += (f'；根据同一窗口 {len(points)} 个额度点、{gain:.0f} 个百分点估算；'
                        '沿用近期模型与缓存组合，属于粗略预测，不是官方 token 上限')
        if window.source == 'local':
            result.note += '；当前额度为离线快照'
    return report
