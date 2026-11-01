"""社区重置事件与「预估重置 / 发卡时间」。

数据来源：https://aihot.news/codex-reset （AIHOT 整理的 Tibo 重置监控，非 OpenAI 官方页面）

它跟踪的是**全体用户**的额度重置与重置卡发放事件，和个人账号的自然重置
（`resets_at`）是两件事，所以这里单独建模：

  * `EventKind.RESET`      —— 官方给所有付费用户重置额度（社区重置）
  * `EventKind.CARD_GRANT` —— 官方发放重置卡
  * `EventKind.CONFIRM`    —— 只是确认帖，不重复计入事件

预测规则（同该站的公开说明）：
  1. 有原话就按原话（公告里给了时间窗就直用）；
  2. 没写时间就按习惯 —— 重置多落在太平洋时间 16:30–21:30，即北京时间
     次日 07:30–12:30；
  3. 用近期事件间隔的中位数外推下一个事件时刻。

所有时间以 epoch 秒存储，按本机时区显示（该站页面本身是北京时间）
"""

from __future__ import annotations

import html as html_mod
import json
import re
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .appenv import APP_VERSION, app_data_dir, log, parse_ts
from .providers import http_request

SOURCE_URL = "https://aihot.news/codex-reset"
API_URL = "https://aihot.news/api/v1/codex-resets"
RECENT_API_URL = API_URL + "/recent"
POLL_SECONDS = 300
CACHE_TTL = 6 * 3600          # 6 小时内直接用缓存
CACHE_MAX_AGE = 4 * 86400     # 超过 4 天的缓存视为不可靠
FETCH_TIMEOUT = 20

RESET = "reset"
CARD_GRANT = "card_grant"
CONFIRM = "confirm"

# 常见重置时刻（太平洋时间 16:30–21:30 → 北京时间次日）
WINDOW_START_HOUR = 7.5
WINDOW_END_HOUR = 12.5
NOMINAL_HOUR = 10.0           # 只用日期时假定的时刻（北京时间）
NOMINAL_UTC_OFFSET = 8 * 3600  # 该站的"北京时间"基准

# 内置基线（来自该站 2026-06 ~ 2026-09 的日历，离线时使用）
BASELINE_RESET_DATES = (
    "2026-06-29", "2026-07-10", "2026-07-11", "2026-07-15", "2026-07-18",
    "2026-07-22", "2026-07-26", "2026-07-28", "2026-07-29", "2026-08-01",
    "2026-08-09", "2026-08-11", "2026-08-24", "2026-08-28", "2026-08-30",
    "2026-08-31", "2026-09-08", "2026-09-27",
)
BASELINE_CARD_DATES = (
    "2026-06-30", "2026-07-02", "2026-07-13", "2026-07-14",
    "2026-08-22", "2026-09-04", "2026-09-05", "2026-09-10", "2026-09-23",
)

_COMBINED_RE = re.compile(
    r"(?:(?P<y>20\d{2})\s*年\s*(?P<m>\d{1,2})\s*月)|"
    r'(?:href="/codex-reset/history/(?P<date>\d{4}-\d{2}-\d{2})"[^>]*>\s*'
    r"(?P<day>\d{1,2})(?P<tail>(?:额度重置|重置确认|发重置卡|\+\d+|\s|待生效)*))"
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_WINDOW_RE = re.compile(
    r"预计\s*([^\n]{0,40}?)\s*[–\-~—]\s*([^\n]{0,40}?)(?:\n|模型推算|$)"
)
_MEDIAN_RE = re.compile(r"重置间隔中位数\s*([\d.]+)\s*天")
_LAST_RESET_RE = re.compile(r"上次额度重置\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_COUNT90_RE = re.compile(r"近\s*90\s*天额度重置\s*(\d+)\s*次")
_CARD90_RE = re.compile(r"近\s*90\s*天发重置卡\s*(\d+)\s*次")
_UPDATED_RE = re.compile(r"最近完整核验\s*(\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2})")


@dataclass
class ResetEvent:
    """一次社区事件。"""

    date: str = ""                      # YYYY-MM-DD（页面所在时区，北京时间）
    kind: str = RESET
    ts: Optional[int] = None            # 仅"当天"精度
    label: str = ""
    scope: str = ""
    confirmed: bool = False
    delta: int = 0                      # 该日 +N 条额外记录
    source: str = "aihot"
    event_id: str = ""
    time_precision: str = "date"
    url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Announcement:
    """已宣布但尚未生效的重置。"""

    window_text: str = ""
    window_start: Optional[int] = None
    window_end: Optional[int] = None
    basis: str = ""
    scope: str = ""
    quote: str = ""
    posted: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Forecast:
    """预估结果。"""

    next_event_ts: Optional[int] = None
    window_start: Optional[int] = None
    window_end: Optional[int] = None
    interval_days: Optional[float] = None
    typical_hour: Optional[float] = None
    confidence: float = 0.0
    bases: List[str] = field(default_factory=list)
    source: str = "baseline"        # live | cache | baseline | announcement

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CommunitySnapshot:
    ok: bool = False
    source: str = "baseline"        # live | cache | baseline | error
    fetched_at: float = 0.0
    events: List[ResetEvent] = field(default_factory=list)
    announcement: Optional[Announcement] = None
    reset_count_90d: int = 0
    card_count_90d: int = 0
    median_interval_days: Optional[float] = None
    last_reset_date: str = ""
    updated_text: str = ""
    next_reset: Optional[Forecast] = None
    next_card: Optional[Forecast] = None
    warnings: List[str] = field(default_factory=list)
    raw_dates: List[str] = field(default_factory=list)
    api_checked_at: Optional[int] = None
    monitor_status: str = "unknown"
    pending_labels: List[str] = field(default_factory=list)

    # -- 便捷访问 ------------------------------------------------------- #
    def resets(self) -> List[ResetEvent]:
        return [e for e in self.events if e.kind == RESET]

    def cards(self) -> List[ResetEvent]:
        return [e for e in self.events if e.kind == CARD_GRANT]

    def hours_to_next_reset(self, now: Optional[float] = None) -> Optional[float]:
        now = time.time() if now is None else now
        if self.announcement and self.announcement.window_start:
            return (self.announcement.window_start - now) / 3600.0
        if self.next_reset and self.next_reset.next_event_ts:
            return (self.next_reset.next_event_ts - now) / 3600.0
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "reset_count_90d": self.reset_count_90d,
            "card_count_90d": self.card_count_90d,
            "median_interval_days": self.median_interval_days,
            "last_reset_date": self.last_reset_date,
            "updated_text": self.updated_text,
            "announcement": self.announcement.to_dict() if self.announcement else None,
            "next_reset": self.next_reset.to_dict() if self.next_reset else None,
            "next_card": self.next_card.to_dict() if self.next_card else None,
            "events": [e.to_dict() for e in self.events],
            "warnings": list(self.warnings),
            "source_url": SOURCE_URL,
            "api_url": API_URL,
            "api_checked_at": self.api_checked_at,
            "monitor_status": self.monitor_status,
            "pending_labels": self.pending_labels,
        }


# --------------------------------------------------------------------------- #
# 抓取与解析
# --------------------------------------------------------------------------- #
def cache_path():
    return app_data_dir() / "community-reset.json"


def _plain_text(raw: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = _TAG_RE.sub("\n", text)
    text = html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text


def _nominal_ts(date_str: str, hour: float = NOMINAL_HOUR) -> Optional[int]:
    """把 YYYY-MM-DD（北京时间）换算成 epoch 秒。"""
    try:
        y, m, d = (int(part) for part in date_str.split("-"))
        import calendar

        base = calendar.timegm((y, m, d, 0, 0, 0, 0, 0, 0)) - NOMINAL_UTC_OFFSET
        return int(base + hour * 3600)
    except Exception:
        return None


def parse_page(raw: str, *, now: Optional[float] = None) -> CommunitySnapshot:
    """从页面 HTML 解析出事件、统计与公告。"""
    now = time.time() if now is None else now
    snap = CommunitySnapshot(ok=True, fetched_at=now)
    if not raw:
        snap.ok = False
        snap.source = "error"
        snap.warnings.append("页面内容为空")
        return snap

    text = _plain_text(raw)

    # ---- 统计 ----
    match = _MEDIAN_RE.search(text)
    if match:
        try:
            snap.median_interval_days = float(match.group(1))
        except Exception:
            pass
    match = _COUNT90_RE.search(text)
    if match:
        snap.reset_count_90d = int(match.group(1))
    match = _CARD90_RE.search(text)
    if match:
        snap.card_count_90d = int(match.group(1))
    match = _UPDATED_RE.search(text)
    if match:
        snap.updated_text = match.group(1).strip()

    # ---- 日历事件：按"当前月份标签"归属日期 ----
    events: Dict[Tuple[str, str], ResetEvent] = {}
    raw_dates: List[str] = []
    for match in _COMBINED_RE.finditer(raw):
        year, month = match.group("y"), match.group("m")
        if year and month:  # 月份标签
            continue
        date_str = match.group("date")
        if not date_str:
            continue
        raw_dates.append(date_str)
        tail = (match.group("tail") or "").replace(" ", "")
        if not tail:
            continue
        kinds: List[str] = []
        if "额度重置" in tail:
            kinds.append(RESET)
        if "重置确认" in tail:
            kinds.append(CONFIRM)
        if "发重置卡" in tail:
            kinds.append(CARD_GRANT)
        delta = 0
        delta_match = re.search(r"\+(\d+)", tail)
        if delta_match:
            try:
                delta = int(delta_match.group(1))
            except Exception:
                delta = 0
        for kind in kinds:
            key = (date_str, kind)
            if key in events:
                events[key].delta = max(events[key].delta, delta)
                continue
            events[key] = ResetEvent(
                date=date_str,
                kind=kind,
                ts=_nominal_ts(date_str),
                label={
                    RESET: "额度重置",
                    CARD_GRANT: "发重置卡",
                    CONFIRM: "重置确认",
                }.get(kind, kind),
                confirmed=(kind != CONFIRM),
                delta=delta,
            )

    # 去掉明显越界的日期（解析噪声）
    sane: List[ResetEvent] = []
    for event in events.values():
        if not event.ts:
            continue
        if event.ts < now - 200 * 86400 or event.ts > now + 60 * 86400:
            continue
        sane.append(event)
    sane.sort(key=lambda e: (e.ts or 0))
    snap.events = sane
    snap.raw_dates = sorted(set(raw_dates))

    # ---- 上次额度重置 ----
    resets = [e for e in sane if e.kind == RESET]
    if resets:
        snap.last_reset_date = resets[-1].date
    else:
        match = _LAST_RESET_RE.search(text)
        if match:
            year = time.gmtime(now + NOMINAL_UTC_OFFSET).tm_year
            snap.last_reset_date = f"{year:04d}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"

    # ---- 公告（等待生效） ----
    if "等待额度重置生效" in text:
        ann = Announcement()
        window = _WINDOW_RE.search(text)
        if window:
            ann.window_text = f"{window.group(1).strip()} – {window.group(2).strip()}"
            ann.window_start, ann.window_end = _parse_window(
                window.group(1).strip(), window.group(2).strip(), now
            )
        basis = re.search(r"模型推算：([^\n]{0,120})", text)
        if basis:
            ann.basis = basis.group(1).strip()
        scope = re.search(r"适用范围：([^\n]{0,60})", text)
        if scope:
            ann.scope = scope.group(1).strip()
        posted = re.search(r"(\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})", text)
        if posted:
            ann.posted = posted.group(1)
        snap.announcement = ann

    snap.next_reset = forecast(snap, RESET, now)
    snap.next_card = forecast(snap, CARD_GRANT, now)
    if not sane:
        snap.warnings.append("未能从页面解析出事件（页面结构可能已变更）")
    return snap


def _parse_window(start_text: str, end_text: str, now: float) -> Tuple[Optional[int], Optional[int]]:
    """解析"今天 03:00 – 明天 03:00"这类相对时间窗。"""
    local_now = time.localtime(now)
    today = time.mktime(
        (local_now.tm_year, local_now.tm_mon, local_now.tm_mday, 0, 0, 0, 0, 0, -1)
    )

    def one(text: str, default_day: int) -> Optional[int]:
        text = text.strip()
        if not text:
            return None
        day_offset = default_day
        if text.startswith("明天"):
            day_offset = 1
            text = text[2:]
        elif text.startswith("今天"):
            day_offset = 0
            text = text[2:]
        elif text.startswith("后天"):
            day_offset = 2
            text = text[2:]
        match = re.search(r"(\d{1,2}):(\d{2})", text)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
        else:
            match = re.search(r"(\d{1,2})\s*[点時时]", text)
            if not match:
                return None
            hour, minute = int(match.group(1)), 0
        if hour > 23:
            return None
        return int(today + day_offset * 86400 + hour * 3600 + minute * 60)

    start = one(start_text, 0)
    end = one(end_text, 1)
    if start and end and end < start:
        end += 86400
    return start, end


# --------------------------------------------------------------------------- #
# 预测
# --------------------------------------------------------------------------- #
def _event_timestamps(events: List[ResetEvent], kind: str) -> List[int]:
    out: List[int] = []
    for event in events:
        if event.kind != kind or not event.ts:
            continue
        # 同一天多条记录只算一次
        if out and abs(event.ts - out[-1]) < 12 * 3600:
            continue
        out.append(event.ts)
    return sorted(out)


def _median_interval(timestamps: List[int]) -> Optional[float]:
    if len(timestamps) < 3:
        return None
    gaps = []
    for prev, cur in zip(timestamps, timestamps[1:]):
        days = (cur - prev) / 86400.0
        if 0.15 <= days <= 45:
            gaps.append(days)
    if len(gaps) < 2:
        return None
    try:
        return float(statistics.median(gaps))
    except Exception:
        return None


def _typical_hour(timestamps: List[int]) -> Optional[float]:
    """事件落在北京时间一天中的哪个时刻（取近期样本的中位数，按 UTC+8 计）。"""
    if not timestamps:
        return None
    hours = []
    for ts in timestamps[-12:]:
        local = time.gmtime(ts + NOMINAL_UTC_OFFSET)
        hours.append(local.tm_hour + local.tm_min / 60.0)
    if not hours:
        return None
    try:
        return float(statistics.median(hours))
    except Exception:
        return None


def _snap_to_window(ts: int) -> Tuple[int, int, int]:
    """把预测时刻吸附到常见时段的窗口，返回 (锚点, 窗口起, 窗口止)。"""
    local = time.localtime(ts)
    day_start = time.mktime(
        (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)
    )
    start = int(day_start + WINDOW_START_HOUR * 3600)
    end = int(day_start + WINDOW_END_HOUR * 3600)
    if ts < start:
        anchor = start
    elif ts > end:
        anchor = end
    else:
        anchor = ts
    return anchor, start, end


def forecast(
    snap: CommunitySnapshot, kind: str, now: Optional[float] = None
) -> Optional[Forecast]:
    """预估下一次事件（额度重置 / 发重置卡）。"""
    now = time.time() if now is None else now
    out = Forecast(source=snap.source)

    # 1) 官方已宣布 -> 直接用公告窗口
    if kind == RESET and snap.announcement and snap.announcement.window_start:
        out.window_start = snap.announcement.window_start
        out.window_end = snap.announcement.window_end or snap.announcement.window_start + 86400
        out.next_event_ts = out.window_start
        out.confidence = 0.9
        out.source = "announcement"
        out.bases.append("官方已宣布（AIHOT 监控到的预告）")
        if snap.announcement.basis:
            out.bases.append(snap.announcement.basis)
        return out

    # 2) 用历史间隔外推
    timestamps = _event_timestamps(snap.events, kind)
    if not timestamps:
        out.bases.append("没有可用的事件历史")
        return out

    interval = _median_interval(timestamps)
    if interval is None:
        interval = snap.median_interval_days or (2.1 if kind == RESET else 14.0)
        out.bases.append("样本不足，使用站点给出的中位数间隔")
    else:
        out.bases.append(f"按近 {len(timestamps)} 次事件的间隔中位数 {interval:.1f} 天外推")
    out.interval_days = interval

    typical_hour = _typical_hour(timestamps)
    out.typical_hour = typical_hour

    predicted = timestamps[-1] + int(interval * 86400)
    anchor, start, end = _snap_to_window(predicted)
    # 常见时段（北京时间 07:30–12:30）
    if start < now <= end + 12 * 3600 and predicted <= now + 12 * 3600:
        anchor = max(int(now), start)
        out.bases.append("当前正处在常见重置时段内")
    out.next_event_ts = int(anchor)
    out.window_start, out.window_end = start, end
    if typical_hour is not None:
        out.bases.append(
            f"历史时刻集中在北京时间 {int(WINDOW_START_HOUR):02d}:30–"
            f"{int(WINDOW_END_HOUR):02d}:30（中位 {typical_hour:.1f} 时）"
        )

    # 置信度
    count = len(timestamps)
    spread = 0.0
    gaps = [
        (cur - prev) / 86400.0
        for prev, cur in zip(timestamps, timestamps[1:])
        if 0.15 <= (cur - prev) / 86400.0 <= 45
    ]
    if len(gaps) >= 2:
        mean = sum(gaps) / len(gaps)
        spread = statistics.pstdev(gaps) / mean if mean else 0.0
    if count >= 8 and spread < 0.9:
        out.confidence = 0.7
    elif count >= 5:
        out.confidence = 0.5
    else:
        out.confidence = 0.3
    out.bases.append(f"样本 {count} 次，间隔波动 {spread * 100:.0f}%")
    return out


# --------------------------------------------------------------------------- #
# 缓存 + 联网刷新
# --------------------------------------------------------------------------- #
def load_cache() -> Optional[CommunitySnapshot]:
    path = cache_path()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        events = [
            ResetEvent(**item)
            for item in data.get("events", [])
            if isinstance(item, dict)
        ]
        snap = CommunitySnapshot(
            ok=bool(data.get("ok")),
            source="cache",
            fetched_at=float(data.get("fetched_at") or 0),
            events=events,
            reset_count_90d=int(data.get("reset_count_90d") or 0),
            card_count_90d=int(data.get("card_count_90d") or 0),
            median_interval_days=data.get("median_interval_days"),
            last_reset_date=str(data.get("last_reset_date") or ""),
            updated_text=str(data.get("updated_text") or ""),
        )
        ann = data.get("announcement")
        if isinstance(ann, dict):
            snap.announcement = Announcement(**ann)
        return snap
    except Exception as exc:
        log(f"读取社区事件缓存失败: {exc!r}")
        return None


def save_cache(snap: CommunitySnapshot) -> None:
    try:
        cache_path().write_text(
            json.dumps(snap.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        log(f"写入社区事件缓存失败: {exc!r}")


def clear_cache() -> None:
    try:
        path = cache_path()
        if path.is_file():
            path.unlink()
    except Exception:
        pass


def baseline_snapshot(now: Optional[float] = None) -> CommunitySnapshot:
    """内置基线：联网不可用时的兜底，保证「预估时间」始终有输出。"""
    now = time.time() if now is None else now
    snap = CommunitySnapshot(ok=True, source="baseline", fetched_at=now)
    snap.warnings.append("使用内置基线数据（未能联网获取最新事件）")
    for date_str in BASELINE_RESET_DATES:
        snap.events.append(
            ResetEvent(date=date_str, kind=RESET, ts=_nominal_ts(date_str), label="额度重置")
        )
    for date_str in BASELINE_CARD_DATES:
        snap.events.append(
            ResetEvent(
                date=date_str, kind=CARD_GRANT, ts=_nominal_ts(date_str), label="发重置卡"
            )
        )
    snap.events.sort(key=lambda e: e.ts or 0)
    resets = snap.resets()
    if resets:
        snap.last_reset_date = resets[-1].date
    snap.median_interval_days = _median_interval(_event_timestamps(snap.events, RESET))
    snap.next_reset = forecast(snap, RESET, now)
    snap.next_card = forecast(snap, CARD_GRANT, now)
    return snap


def fetch_community(*, force: bool = False, now: Optional[float] = None,
                    ttl_hours: float = 6) -> CommunitySnapshot:
    """生产环境只使用公开 JSON API；HTML 与基线保留给离线解析自检。"""
    from .community_api import fetch_api
    return fetch_api(force=force, now=now, ttl_hours=ttl_hours)

def _merge(base: Optional[CommunitySnapshot], extra: CommunitySnapshot) -> CommunitySnapshot:
    if base is None:
        return extra
    seen = {(e.date, e.kind) for e in base.events}
    for event in extra.events:
        key = (event.date, event.kind)
        if key in seen:
            continue
        seen.add(key)
        base.events.append(event)
    base.events.sort(key=lambda e: e.ts or 0)
    base.reset_count_90d = max(base.reset_count_90d, extra.reset_count_90d)
    base.card_count_90d = max(base.card_count_90d, extra.card_count_90d)
    if extra.median_interval_days:
        base.median_interval_days = extra.median_interval_days
    if extra.last_reset_date and extra.last_reset_date > base.last_reset_date:
        base.last_reset_date = extra.last_reset_date
    if extra.announcement and not base.announcement:
        base.announcement = extra.announcement
    if extra.updated_text:
        base.updated_text = extra.updated_text
    base.raw_dates = sorted(set(base.raw_dates + extra.raw_dates))
    return base


# --------------------------------------------------------------------------- #
# 展示辅助
# --------------------------------------------------------------------------- #
def fmt_local_window(start: Optional[int], end: Optional[int], now: Optional[float] = None) -> str:
    """把时间窗格式化成"今天 07:30–12:30"这种本地读法。"""
    if not start:
        return "—"
    now = time.time() if now is None else now
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    tomorrow = time.strftime("%Y-%m-%d", time.localtime(now + 86400))
    day_after = time.strftime("%Y-%m-%d", time.localtime(now + 2 * 86400))

    def day_label(ts: int) -> str:
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if day == today:
            return "今天"
        if day == tomorrow:
            return "明天"
        if day == day_after:
            return "后天"
        return time.strftime("%m-%d", time.localtime(ts))

    if end and time.strftime("%Y-%m-%d", time.localtime(end)) == time.strftime(
        "%Y-%m-%d", time.localtime(start)
    ):
        return f"{day_label(start)} {time.strftime('%H:%M', time.localtime(start))}–{time.strftime('%H:%M', time.localtime(end))}"
    text = f"{day_label(start)} {time.strftime('%H:%M', time.localtime(start))}"
    if end:
        text += f" – {day_label(end)} {time.strftime('%H:%M', time.localtime(end))}"
    return text


def fmt_countdown(ts: Optional[int], now: Optional[float] = None) -> str:
    if not ts:
        return "—"
    now = time.time() if now is None else now
    delta = ts - now
    if delta <= 0:
        return "已进入预计时段"
    hours = delta / 3600.0
    if hours < 1:
        return f"约 {int(delta / 60)} 分钟后"
    if hours < 48:
        return f"约 {hours:.1f} 小时后"
    return f"约 {hours / 24:.1f} 天后"


def describe_event(event: ResetEvent, now: Optional[float] = None) -> str:
    """把一次社区事件描述成"今天 10:00 · 重置额度"这种本地读法。"""
    if not event.ts:
        return f"{event.date} · {event.label}"
    now = time.time() if now is None else now
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    tomorrow = time.strftime("%Y-%m-%d", time.localtime(now + 86400))
    day = time.strftime("%Y-%m-%d", time.localtime(event.ts))
    if day == today:
        prefix = "今天 "
    elif day == tomorrow:
        prefix = "明天 "
    else:
        prefix = time.strftime("%m-%d ", time.localtime(event.ts))
    delta = int(now - event.ts)
    if delta < 0:
        suffix = fmt_countdown(event.ts, now)
    elif delta < 3600:
        suffix = f"{max(1, delta // 60)} 分钟前"
    elif delta < 86400:
        suffix = f"{delta // 3600} 小时前"
    else:
        suffix = f"{delta // 86400} 天前"
    return f"{prefix}{suffix} · {event.label}"


def confidence_label(confidence: float) -> str:
    if confidence >= 0.85:
        return "官方已宣布"
    if confidence >= 0.65:
        return "较高"
    if confidence >= 0.45:
        return "中等"
    return "偏低"

