"""AIHOT v1 快照：完整历史初始化、5 分钟 ETag 轮询、无认证信息。"""
from __future__ import annotations
import gzip
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from . import events as ev
from .appenv import APP_VERSION, app_data_dir, parse_ts, log

def parse_api(payload: dict, now=None) -> ev.CommunitySnapshot:
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or payload.get('schemaVersion') != 1 or not isinstance(payload.get('events'), list):
        raise ValueError('AIHOT API 数据结构不可读')
    monitor = payload.get('monitor') or {}
    snap = ev.CommunitySnapshot(ok=True, source='live', fetched_at=now,
        api_checked_at=parse_ts(payload.get('checkedAt')),
        monitor_status=str(monitor.get('status', 'unknown')),
        updated_text=str(payload.get('checkedAt') or ''))
    pending = {}
    seen = set()
    for item in payload['events']:
        if not isinstance(item, dict):
            continue
        kind = {'direct_reset': ev.RESET, 'reset_credit': ev.CARD_GRANT}.get(item.get('type'))
        if not kind:
            continue
        presentation = item.get('presentation') or {}
        state = presentation.get('status') or item.get('status')
        products = presentation.get('productsZh') or ''
        # 明确仅属于 ChatGPT 的事件不作为 Codex 发卡历史。
        if products and 'Codex' not in products:
            continue
        if state == 'confirmed':
            date = item.get('occurredOn')
            # confirmedAt 是确认帖时间，绝不当成精确到账时间。
            stamp = ev._nominal_ts(date) if date else parse_ts(item.get('confirmedAt'))
            if not stamp:
                continue
            date = date or time.strftime('%Y-%m-%d', time.gmtime(stamp + 8*3600))
            key = (date, kind)
            if key in seen:
                continue
            seen.add(key)
            snap.events.append(ev.ResetEvent(date=date, kind=kind, ts=stamp, confirmed=True,
                label='额度重置' if kind == ev.RESET else '发重置卡',
                scope=item.get('scope') or presentation.get('scopeLabel') or '',
                event_id=str(item.get('id', '')), time_precision='date' if item.get('occurredOn') else 'confirmation',
                url=item.get('url') or ev.SOURCE_URL))
        elif state not in ('likely_completed', 'withdrawn', 'cancelled', 'completed'):
            estimate, schedule = item.get('estimate') or {}, item.get('schedule') or {}
            window = estimate or schedule
            start, end = parse_ts(window.get('from')), parse_ts(window.get('through'))
            # 历史预告不能伪装成尚未生效的新公告。
            if end and end < now:
                continue
            label = ('额度重置' if kind == ev.RESET else '发重置卡')
            snap.pending_labels.append(f'{label}已预告' + ('，尚未给出时间' if not start else ''))
            if not start:
                continue
            source = 'aihot_estimate' if estimate else 'announcement'
            forecast = ev.Forecast(next_event_ts=start, window_start=start, window_end=end or start,
                confidence=.5 if estimate else .9, source=source,
                bases=[window.get('reason') or 'AIHOT 收录的原帖预告', window.get('label') or ''])
            if kind not in pending or start < pending[kind].window_start:
                pending[kind] = forecast
                if kind == ev.RESET and not estimate:
                    snap.announcement = ev.Announcement(window_text=window.get('label') or '',
                        window_start=start, window_end=end or start, basis='原帖明确时间',
                        scope=item.get('scope') or presentation.get('scopeLabel') or '')
    snap.events.sort(key=lambda event: event.ts or 0)
    resets, cards = snap.resets(), snap.cards()
    snap.last_reset_date = resets[-1].date if resets else ''
    snap.reset_count_90d = sum((event.ts or 0) >= now-90*86400 for event in resets)
    snap.card_count_90d = sum((event.ts or 0) >= now-90*86400 for event in cards)
    snap.median_interval_days = ev._median_interval(ev._event_timestamps(snap.events, ev.RESET))
    snap.next_reset = pending.get(ev.RESET) or ev.forecast(snap, ev.RESET, now)
    snap.next_card = pending.get(ev.CARD_GRANT) or ev.forecast(snap, ev.CARD_GRANT, now)
    if snap.monitor_status not in ('healthy', 'unknown'):
        snap.warnings.append('AIHOT 监控状态：' + snap.monitor_status)
    if not snap.api_checked_at or now-snap.api_checked_at > 24*3600:
        snap.warnings.append('AIHOT 核验时间已超过 24 小时或不可读')
    return snap

def request_api(url, etag=''):
    headers = {'User-Agent': f'codex-quota-viewer/{APP_VERSION}', 'Accept': 'application/json', 'Accept-Encoding': 'gzip'}
    if etag:
        headers['If-None-Match'] = etag
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read(4*1024*1024)
            if response.headers.get('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
            return response.status, json.loads(raw.decode('utf-8')), response.headers.get('ETag', '')
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return 304, None, error.headers.get('ETag', etag)
        raise RuntimeError(f'AIHOT HTTP {error.code}') from error

def cache_path():
    return app_data_dir() / 'community-api-v1.json'

def read_state():
    try:
        data = json.loads(cache_path().read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def save_state(state):
    temporary = cache_path().with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, cache_path())
    except OSError as error:
        log(f'AIHOT 缓存写入失败：{error}')
    finally:
        if temporary.exists():
            temporary.unlink()

def merge_recent(full, recent):
    """Recent 是窗口快照：替换窗口内旧记录，能正确应用撤回和更正。"""
    merged = dict(full)
    threshold = parse_ts(recent.get('historyFrom')) or 0
    ids = {event.get('id') for event in recent.get('events', [])}
    old = [event for event in full.get('events', []) if event.get('id') not in ids and
           (parse_ts(event.get('updatedAt')) or parse_ts(event.get('createdAt')) or 0) < threshold]
    merged.update(recent)
    merged['events'] = old + recent.get('events', [])
    merged['count'] = len(merged['events'])
    return merged

def fetch_api(*, force=False, now=None, ttl_hours=6):
    now = time.time() if now is None else now
    state = read_state()
    changed = False
    try:
        full = state.get('full')
        if not full or now-state.get('full_at', 0) >= ttl_hours*3600:
            status, data, etag = request_api(ev.API_URL, state.get('full_etag', ''))
            if status == 200:
                parse_api(data, now)
                state.update(full=data, full_etag=etag)
            if not state.get('full'):
                raise ValueError('AIHOT 未返回历史快照')
            state['full_at'] = now
            changed = True
        if force or not state.get('recent') or now-state.get('recent_at', 0) >= ev.POLL_SECONDS:
            status, data, etag = request_api(ev.RECENT_API_URL, state.get('recent_etag', ''))
            if status == 200:
                parse_api(data, now)
                state.update(recent=data, recent_etag=etag)
            if not state.get('recent'):
                raise ValueError('AIHOT 未返回轮询快照')
            state['recent_at'] = now
            changed = True
        payload = merge_recent(state['full'], state['recent'])
        snap = parse_api(payload, now)
        snap.fetched_at = state['recent_at']
        snap.source = 'live' if changed else 'cache'
        if changed:
            save_state(state)
        return snap
    except Exception as error:
        log(f'AIHOT API 获取失败：{error}')
        full = state.get('full')
        if full and now-state.get('recent_at', state.get('full_at', 0)) < ev.CACHE_MAX_AGE:
            try:
                snap = parse_api(merge_recent(full, state['recent']) if state.get('recent') else full, now)
                snap.source = 'cache'
                snap.fetched_at = state.get('recent_at', state.get('full_at', 0))
                snap.warnings.append(f'联网失败，显示缓存：{error}')
                return snap
            except ValueError:
                pass
        return ev.CommunitySnapshot(ok=False, source='error', warnings=[f'AIHOT API 不可用：{error}'])
