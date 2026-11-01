"""重置时机分析引擎。

核心问题：手上这些「重置卡」什么时候用最划算？

结论建立在三条原则上：
  1. 重置卡会过期 —— 过期作废是纯损失，所以临期的卡必须优先消化；
  2. 重置会把对应窗口的已用额度清零 —— 在额度所剩无几时用，价值最大；
  3. 用得太早等于浪费 —— 两次自然重置之间重复重置同一窗口，收益会打折。

因此推荐时间 = max(现在, 预计触顶时间) 与 min(到期前 24 小时) 的交集：
  * 卡快过期（<= 2 天）        -> 立刻用（不用就没了）
  * 当前窗口已接近触顶（>= 85%）-> 立刻用（可以马上继续干活）
  * 窗口用得很空且卡还很久到期 -> 等（给出建议等待到的时间点）
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .providers import (
    ResetCredit,
    RateSample,
    Snapshot,
    QuotaWindow,
    WINDOW_5H,
    WINDOW_OTHER,
    WINDOW_WEEKLY,
    classify_window,
    find_reset_events,
)
from . import events as ev

# 阈值（可在设置里微调 warn_percent）
CRITICAL_PERCENT = 95.0
HIGH_PERCENT = 85.0
LOW_PERCENT = 40.0
EXPIRY_URGENT_DAYS = 2.0
EXPIRY_SOON_DAYS = 7.0


def fmt_duration(seconds: Optional[float], *, short: bool = False) -> str:
    if seconds is None:
        return "—"
    if seconds < 0:
        seconds = 0
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        text = f"{days} 天 {hours} 小时" if hours else f"{days} 天"
    elif hours:
        text = f"{hours} 小时 {minutes} 分" if minutes and not short else f"{hours} 小时"
    elif minutes:
        text = f"{minutes} 分钟"
    else:
        text = f"{total} 秒"
    return text


def fmt_clock(ts: Optional[int], now: Optional[float] = None) -> str:
    if not ts:
        return "—"
    now = time.time() if now is None else now
    base = time.localtime(ts)
    stamp = time.strftime("%m-%d %H:%M", base)
    delta = ts - now
    if 0 <= delta < 86400 * 2 and time.strftime("%Y-%m-%d", base) != time.strftime(
        "%Y-%m-%d", time.localtime(now)
    ):
        stamp = f"{'明天' if delta < 86400 * 2 else ''} {time.strftime('%H:%M', base)}".strip()
    return stamp


def fmt_full_clock(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# --------------------------------------------------------------------------- #
# 窗口分析
# --------------------------------------------------------------------------- #
@dataclass
class WindowTrend:
    window_minutes: int = 0
    kind: str = WINDOW_OTHER
    per_hour: Optional[float] = None       # 每小时消耗的百分点
    span_hours: float = 0.0                # 采样覆盖时长
    sample_count: int = 0
    observed_max: float = 0.0
    resets_seen: int = 0
    exhausted_events: int = 0

    def hours_to_full(self, used_percent: float) -> Optional[float]:
        if not self.per_hour or self.per_hour <= 0.01:
            return None
        remaining = max(0.0, 100.0 - used_percent)
        return remaining / self.per_hour


@dataclass
class WindowAdvice:
    window: QuotaWindow
    remaining_hours: Optional[float] = None   # 预计多少小时后触顶
    exhaust_at: Optional[int] = None
    reset_in_text: str = "—"
    trend: WindowTrend = field(default_factory=WindowTrend)
    status: str = "ok"          # ok | high | critical | exhausted
    headline: str = ""
    details: List[str] = field(default_factory=list)


@dataclass
class CreditAdvice:
    credit: ResetCredit
    verdict: str = "wait"        # use_now | use_soon | wait | keep
    action_time: Optional[int] = None
    reasons: List[str] = field(default_factory=list)
    target: str = ""             # 建议作用在哪个窗口
    urgency: float = 0.0


@dataclass
class Advice:
    headline: str = ""
    verdict: str = "wait"
    action_time: Optional[int] = None
    lines: List[str] = field(default_factory=list)
    balance_line: str = ""
    window_advices: List[WindowAdvice] = field(default_factory=list)
    credit_advices: List[CreditAdvice] = field(default_factory=list)
    used_samples: int = 0
    history_days: float = 0.0
    community_note: str = ""            # 社区重置预估的一句话结论
    community_ts: Optional[int] = None  # 预估的社区重置时刻

    def to_dict(self) -> Dict[str, Any]:
        return {
            "headline": self.headline,
            "verdict": self.verdict,
            "action_time": self.action_time,
            "action_time_text": fmt_full_clock(self.action_time),
            "lines": list(self.lines),
            "balance_line": self.balance_line,
            "used_samples": self.used_samples,
            "history_days": round(self.history_days, 3),
            "community_note": self.community_note,
            "community_ts": self.community_ts,
            "windows": [
                {
                    "title": item.window.title(),
                    "kind": item.window.kind,
                    "used_percent": item.window.used_percent,
                    "remaining_percent": item.window.remaining_percent,
                    "resets_at": item.window.resets_at,
                    "status": item.status,
                    "headline": item.headline,
                    "per_hour": item.trend.per_hour,
                    "hours_to_full": item.remaining_hours,
                    "details": list(item.details),
                }
                for item in self.window_advices
            ],
            "credits": [
                {
                    "credit_id": item.credit.credit_id,
                    "expires_at": item.credit.expires_at,
                    "granted_at": item.credit.granted_at,
                    "verdict": item.verdict,
                    "action_time": item.action_time,
                    "urgency": item.urgency,
                    "target": item.target,
                    "reasons": list(item.reasons),
                }
                for item in self.credit_advices
            ],
        }


# --------------------------------------------------------------------------- #
# 趋势
# --------------------------------------------------------------------------- #
def analyze_trends(
    samples: Sequence[RateSample], window_minutes: Optional[int] = None
) -> WindowTrend:
    trend = WindowTrend(window_minutes=window_minutes or 0)
    if window_minutes:
        trend.kind = classify_window(window_minutes)
        series = [s for s in samples if s.window_minutes == window_minutes]
    else:
        series = list(samples)
    if len(series) < 2:
        return trend

    trend.sample_count = len(series)
    trend.span_hours = max(0.0, (series[-1].ts - series[0].ts) / 3600.0)
    trend.observed_max = max(s.used_percent for s in series)

    # 只在"同一个自然窗口内"统计增速：跨越 resets_at 的段要断开
    total_gain = 0.0
    total_hours = 0.0
    for prev, cur in zip(series, series[1:]):
        if prev.resets_at and cur.resets_at and cur.resets_at > prev.resets_at + 60:
            continue  # 中间发生了重置
        if cur.used_percent + 0.5 < prev.used_percent:
            continue  # 额度回落，非自然消耗
        dt_hours = (cur.ts - prev.ts) / 3600.0
        if dt_hours <= 0 or dt_hours > 6:
            continue
        total_gain += cur.used_percent - prev.used_percent
        total_hours += dt_hours
    if total_hours > 0.05 and total_gain > 0:
        trend.per_hour = total_gain / total_hours

    events = find_reset_events(series)
    trend.resets_seen = len(events)
    trend.exhausted_events = sum(1 for s in series if s.used_percent >= 99.5)
    if window_minutes:
        kind = classify_window(window_minutes)
        trend.resets_seen = sum(1 for _, k, _, _ in events if k == kind)
    return trend


def window_advice(
    window: QuotaWindow,
    samples: Sequence[RateSample],
    now: Optional[float] = None,
    warn_percent: float = HIGH_PERCENT,
) -> WindowAdvice:
    now = time.time() if now is None else now
    advice = WindowAdvice(window=window)
    advice.trend = analyze_trends([s for s in samples if s.limit_id == window.limit_id
                                  and s.slot == window.slot], window.window_minutes)

    if window.used_percent >= CRITICAL_PERCENT:
        advice.status = "exhausted" if window.used_percent >= 99.5 else "critical"
    elif window.used_percent >= warn_percent:
        advice.status = "high"

    secs = window.seconds_until_reset(now)
    advice.reset_in_text = fmt_duration(secs) if secs is not None else "未知"

    hours = advice.trend.hours_to_full(window.used_percent)
    if hours is not None:
        advice.remaining_hours = hours
        advice.exhaust_at = int(now + hours * 3600)
        if secs is not None and hours * 3600 > secs:
            advice.headline = f"按当前速度，额度会在自然重置前耗尽"
        else:
            advice.headline = f"约 {fmt_duration(hours * 3600)} 后触顶"
    elif window.used_percent >= CRITICAL_PERCENT:
        advice.headline = "额度已接近用尽"
    else:
        advice.headline = "暂无足够历史数据估算速度"

    if advice.trend.per_hour:
        advice.details.append(
            f"近 {advice.trend.span_hours:.1f} 小时消耗约 {advice.trend.per_hour:.1f}%/小时"
            f"（{advice.trend.sample_count} 个采样点）"
        )
    else:
        advice.details.append("历史采样不足，速度估算不可用")
    if advice.trend.resets_seen:
        advice.details.append(f"历史记录中出现过 {advice.trend.resets_seen} 次窗口重置")
    if window.resets_at:
        advice.details.append(
            f"自然重置：{fmt_full_clock(window.resets_at)}（{advice.reset_in_text}后）"
        )
    else:
        advice.details.append("后端未给出自然重置时间")
    return advice


# --------------------------------------------------------------------------- #
# 重置卡建议
# --------------------------------------------------------------------------- #
def credit_advice(
    credit: ResetCredit,
    windows: Sequence[QuotaWindow],
    advices: Sequence[WindowAdvice],
    now: Optional[float] = None,
    *,
    credits_total: int = 1,
    community_ts: Optional[int] = None,
) -> CreditAdvice:
    now = time.time() if now is None else now
    out = CreditAdvice(credit=credit)

    days_left = credit.days_left(now)

    # 选最合适的窗口：已用百分比最高的那个
    target_window: Optional[QuotaWindow] = None
    target_advice: Optional[WindowAdvice] = None
    if windows:
        paired = list(zip(windows, list(advices) + [None] * len(windows)))
        paired.sort(key=lambda item: item[0].used_percent, reverse=True)
        target_window, target_advice = paired[0][0], paired[0][1]  # type: ignore[assignment]

    if target_window is not None:
        out.target = target_window.title()
        used = target_window.used_percent
        out.reasons.append(f"当前 {out.target} 已用 {used:.0f}%")
    else:
        used = 0.0
        out.reasons.append("没有读到额度窗口")
    if days_left is not None:
        out.reasons.append(f"该卡 {fmt_duration(days_left * 86400)} 后到期")

    # 社区重置（官方给所有人重置）临近时，这张卡的价值会被稀释
    natural_reset_ts = target_window.resets_at if target_window else None
    community_covers = False
    community_hours: Optional[float] = None
    if community_ts:
        community_hours = (community_ts - now) / 3600.0
        if 0 <= community_hours <= 24.0 and credit.expires_at:
            card_left = (credit.expires_at - now) / 3600.0
            if card_left > community_hours + 24.0:
                if not natural_reset_ts or community_ts <= natural_reset_ts + 3600:
                    community_covers = True
                    out.reasons.append(
                        f"社区重置预计 {fmt_duration(community_hours * 3600)}后到达，"
                        "会把额度整体恢复"
                    )

    # 判断
    if days_left is not None and days_left <= 0:
        out.verdict = "wait"
        out.reasons.append("已过期，接口仍列出但通常无法使用")
        out.action_time = None
        return out

    urgent_expiry = days_left is not None and days_left <= EXPIRY_URGENT_DAYS
    soon_expiry = days_left is not None and days_left <= EXPIRY_SOON_DAYS
    expiring_last = credits_total <= 1 and soon_expiry

    if community_covers and used < HIGH_PERCENT:
        # 先用社区重置顶上，卡留到之后
        out.verdict = "wait"
        out.action_time = int((community_ts or now) + 6 * 3600)
        out.reasons.append("先别用卡：等社区重置生效后仍有大量额度，卡留到真正需要时再用")
        out.urgency = 20.0
        if len(windows) > 1 and out.target:
            others = "、".join(w.title() for w in windows if w.title() != out.target)
            if others:
                out.reasons.append(f"注意：重置通常作用于{out.target}（同时存在 {others}）")
        return out

    if target_window is not None and used >= HIGH_PERCENT:
        out.verdict = "use_now"
        out.action_time = int(now)
        out.reasons.append(
            f"{out.target} 已用 {used:.0f}%，现在重置可以立刻恢复产出"
        )
        out.urgency = 90.0
    elif urgent_expiry and target_window is not None and used > LOW_PERCENT:
        out.verdict = "use_now"
        out.action_time = int(now)
        out.reasons.append("卡即将过期，且当前额度已有消耗，先用掉避免作废")
        out.urgency = 85.0
    elif urgent_expiry and target_window is not None and used <= LOW_PERCENT:
        out.verdict = "use_soon"
        out.action_time = int(min(now + 6 * 3600, (credit.expires_at or now) - 3600))
        out.reasons.append("卡即将过期但额度很空：建议先压一压使用量，临近到期前用掉")
        out.urgency = 70.0
    elif expiring_last and target_window is not None and used > 60:
        out.verdict = "use_soon"
        out.action_time = int(min(now + 24 * 3600, (credit.expires_at or now) - 12 * 3600))
        out.reasons.append("即将到期且额度已用较多，建议 1 天内安排重置")
        out.urgency = 60.0
    else:
        # 等待：等到触顶前、社区重置之后、或到期前
        candidates: List[float] = []
        if target_advice is not None and target_advice.remaining_hours:
            # 触顶时间的 85% 处动用，兼顾"不浪费"和"不断供"
            candidates.append(now + target_advice.remaining_hours * 3600 * 0.85)
        if community_covers and community_ts:
            candidates.append(community_ts + 6 * 3600)
        if credit.expires_at:
            candidates.append(credit.expires_at - 24 * 3600)
        if candidates:
            when = max(now, min(candidates))
        else:
            when = now + 3 * 86400
        out.verdict = "wait"
        out.action_time = int(when)
        if target_advice is not None and target_advice.remaining_hours:
            out.reasons.append(
                f"按当前速度约 {fmt_duration(target_advice.remaining_hours * 3600)} 后触顶，"
                "现在重置会浪费掉本次自然重置前的余量"
            )
        else:
            out.reasons.append("额度充足，先留着这张卡以应对突发需求")
        out.urgency = 30.0

    if len(windows) > 1 and out.target:
        others = "、".join(w.title() for w in windows if w.title() != out.target)
        if others:
            out.reasons.append(f"注意：重置通常作用于{out.target}（同时存在 {others}）")
    return out


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def build_advice(
    snapshot: Snapshot,
    samples: Sequence[RateSample],
    now: Optional[float] = None,
    warn_percent: float = HIGH_PERCENT,
    expiry_warn_days: float = EXPIRY_SOON_DAYS,
    community: Optional["ev.CommunitySnapshot"] = None,
) -> Advice:
    now = time.time() if now is None else now
    result = Advice()
    result.used_samples = len(samples)
    if samples:
        result.history_days = max(0.0, (samples[-1].ts - samples[0].ts) / 86400.0)

    # ---- 社区重置预估 ----
    community_ts: Optional[int] = None
    community_note = ""
    if community is not None and community.ok:
        forecast = community.next_reset
        if community.announcement and community.announcement.window_start:
            community_ts = community.announcement.window_start
            window = ev.fmt_local_window(
                community.announcement.window_start, community.announcement.window_end, now
            )
            community_note = f"官方已宣布：{window} 生效（{ev.fmt_countdown(community_ts, now)}）"
        elif forecast and forecast.next_event_ts:
            window = ev.fmt_local_window(forecast.window_start, forecast.window_end, now)
            community_note = (
                f"预估下次社区重置 {window}"
                f"（{ev.fmt_countdown(forecast.next_event_ts, now)}，把握 {ev.confidence_label(forecast.confidence)}）"
            )
            # 统计预测可以展示，但不据此让用户等待或消费卡片。
            if forecast.source in ('announcement', 'aihot_estimate') and (forecast.window_end or 0) >= now:
                community_ts = forecast.next_event_ts
    result.community_ts = community_ts
    result.community_note = community_note

    result.window_advices = [
        window_advice(w, samples, now, warn_percent) for w in snapshot.windows
    ]

    credits = snapshot.sorted_credits()
    result.credit_advices = [
        credit_advice(
            c,
            snapshot.windows,
            result.window_advices,
            now,
            credits_total=len(credits),
            community_ts=community_ts,
        )
        for c in credits
    ]

    # 余额行
    bits: List[str] = []
    bits.append(f"重置卡 {len(credits)} 张")
    if snapshot.credit_balance not in (None, ""):
        bits.append(f"积分余额 {snapshot.credit_balance}")
    if snapshot.unlimited:
        bits.append("账号为无限额度")
    result.balance_line = " · ".join(bits)

    # 总结论
    lines: List[str] = []
    if community_note:
        lines.append(community_note)
    if not snapshot.ok:
        result.headline = "暂时读不到额度数据"
        result.verdict = "unknown"
        lines.append(snapshot.error or "请检查网络或重新登录 Codex")
        result.lines = lines
        return result

    verdict_rank = {"use_now": 0, "use_soon": 1, "wait": 2, "keep": 3}
    top_credit: Optional[CreditAdvice] = None
    for item in result.credit_advices:
        if top_credit is None or verdict_rank.get(item.verdict, 9) < verdict_rank.get(
            top_credit.verdict, 9
        ):
            top_credit = item

    # 窗口风险
    worst: Optional[WindowAdvice] = None
    for item in result.window_advices:
        if worst is None or item.window.used_percent > worst.window.used_percent:
            worst = item

    if top_credit is None:
        result.headline = ("没有可用的重置卡" if snapshot.credits_supported
                           else "重置卡暂不可读，先参考额度水位")
        result.verdict = "none"
        if worst is not None:
            lines.append(
                f"{worst.window.title()}剩余 {worst.window.remaining_percent:.0f}%，"
                f"{worst.reset_in_text}后自然重置"
            )
            if worst.exhaust_at and worst.window.seconds_until_reset(now):
                if worst.exhaust_at < (worst.window.resets_at or 0):
                    lines.append("按当前速度会在自然重置前耗尽，注意控制用量或考虑升级/加购")
    else:
        credit = top_credit.credit
        if top_credit.verdict == "use_now":
            result.headline = "现在就用掉 1 张重置卡"
        elif top_credit.verdict == "use_soon":
            result.headline = "今天内安排一次重置"
        else:
            when = fmt_full_clock(top_credit.action_time)
            result.headline = f"建议保留，最早 {when} 再重置"
        result.verdict = top_credit.verdict
        result.action_time = top_credit.action_time
        lines.append(f"决策对象：{credit.short_id()}（到期 {fmt_full_clock(credit.expires_at)}）")
        lines.extend(f"· {reason}" for reason in top_credit.reasons[:4])

    if worst is not None:
        lines.append(
            f"额度水位：{worst.window.title()} 已用 {worst.window.used_percent:.0f}%，"
            f"剩余 {worst.window.remaining_percent:.0f}%，{worst.reset_in_text}后自然重置"
        )
    if len(credits) > 1:
        soon = [
            c for c in credits
            if c.days_left(now) is not None and (c.days_left(now) or 99) <= expiry_warn_days
        ]
        if soon:
            lines.append(f"其中 {len(soon)} 张将在 {expiry_warn_days:.0f} 天内到期，优先消化")
    if result.history_days >= 0.2:
        lines.append(f"分析基于最近 {result.history_days:.1f} 天的本地使用记录（{len(samples)} 个采样）")
    else:
        lines.append("本地历史样本较少，建议用一会儿再看分析会更准")

    result.lines = lines
    return result


def expiry_watchlist(
    snapshot: Snapshot, now: Optional[float] = None, days: float = 3.0
) -> List[ResetCredit]:
    now = time.time() if now is None else now
    out: List[ResetCredit] = []
    for credit in snapshot.credits:
        left = credit.days_left(now)
        if left is not None and 0 <= left <= days:
            out.append(credit)
    return out
