"""自检：不联网即可验证解析、分析与鉴权发现的正确性。

用法：
    python run.py --selftest          # 离线自检
    python run.py --selftest --live   # 额外真实调用一次官方接口
"""

from __future__ import annotations

import json
import copy
import sys
import time
from typing import Any, Dict, List, Tuple

from . import analytics as ana
from . import events as ev
from .appenv import APP_VERSION, codex_home, platform_summary
from .collector import DataController
from .providers import (
    RateSample,
    ResetCredit,
    Snapshot,
    WINDOW_5H,
    WINDOW_WEEKLY,
    auth_identity,
    decode_jwt,
    discover_auth_files,
    parse_credits,
    parse_ts,
    parse_usage,
)

# 与官方 /wham/usage 形状一致的样例
USAGE_FIXTURE: Dict[str, Any] = {
    "plan_type": "plus",
    "rate_limits": {
        "limit_id": "codex",
        "limit_name": None,
        "primary": {"used_percent": 62.0, "window_minutes": 300, "resets_at": 1790697295},
        "secondary": {"used_percent": 41.0, "window_minutes": 10080, "resets_at": 1791091046},
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    },
    "rate_limits_by_limit_id": {
        "codex": {
            "limit_id": "codex",
            "primary": {"used_percent": 62.0, "window_minutes": 300, "resets_at": 1790697295},
            "secondary": {
                "used_percent": 41.0,
                "window_minutes": 10080,
                "resets_at": 1791091046,
            },
        }
    },
}

# 与官方 reset-credits 形状一致的样例（含另一种命名风格）
CREDITS_FIXTURE: Dict[str, Any] = {
    "credits": [
        {
            "credit_id": "b4f53a61d614aaaa",
            "granted_at": "2026-06-12T00:00:00Z",
            "expires_at": "2026-07-12T00:00:00Z",
            "scope": "weekly",
            "status": "available",
        },
        {
            "id": "2815139a8ea0bbbb",
            "grantedAt": 1781000000,
            "expiresAt": 1784600000000,  # 毫秒
            "reset_scope": "five_hour",
        },
        {"credit_id": "alreadyused01", "status": "consumed", "expires_at": 1790000000},
    ]
}

LOCAL_FIXTURE: Dict[str, Any] = {
    "rate_limits": {
        "limit_id": "codex",
        "primary": {"used_percent": 73.0, "window_minutes": 300, "resets_at": 1790660802},
        "secondary": {"used_percent": 43.0, "window_minutes": 10080, "resets_at": 1791091046},
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        "plan_type": "plus",
    }
}


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.lines: List[str] = []

    def check(self, condition: bool, name: str, detail: str = "") -> None:
        if condition:
            self.passed += 1
            self.lines.append(f"  [PASS] {name}")
        else:
            self.failed += 1
            self.lines.append(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(f"[{title}]")

    def report(self) -> str:
        head = [
            f"Codex 额度查看器 v{APP_VERSION} 自检",
            f"环境: {platform_summary()}",
            f"Codex Home: {codex_home()}",
            "",
        ]
        tail = [
            "",
            f"结果: {self.passed} 通过 / {self.failed} 失败",
        ]
        return "\n".join(head + self.lines + tail)


SAMPLE_HTML = """<html><body>
<p>全部为北京时间 · UTC+8</p>
<p>近 90 天额度重置</p><p>23 次</p>
<p>近 90 天发重置卡</p><p>8 次</p>
<p>重置间隔中位数</p><p>2.1 天</p>
<p>上次额度重置</p><p>9月27日</p>
<h2>等待额度重置生效</h2>
<p>预计 今天 03:00 - 明天 03:00</p>
<p>模型推算：仅称"下周"，未给具体日期；参考其常在太平洋时间下午至晚间完成重置。</p>
<li>适用范围：Tibo 未说明适用范围</li>
<h3>2026 年 9 月</h3>
<a href="/codex-reset/history/2026-08-31">31额度重置</a>
<a href="/codex-reset/history/2026-09-08">8额度重置</a>
<a href="/codex-reset/history/2026-09-04">4发重置卡</a>
<a href="/codex-reset/history/2026-09-12">12重置确认</a>
<a href="/codex-reset/history/2026-09-27">27额度重置</a>
<h3>2026 年 8 月</h3>
<a href="/codex-reset/history/2026-08-09">9额度重置</a>
<a href="/codex-reset/history/2026-08-11">11额度重置</a>
<a href="/codex-reset/history/2026-08-24">24额度重置</a>
<a href="/codex-reset/history/2026-08-28">28额度重置</a>
<a href="/codex-reset/history/2026-08-30">30额度重置</a>
<a href="/codex-reset/history/2026-08-22">22发重置卡</a>
<h3>2026 年 7 月</h3>
<a href="/codex-reset/history/2026-07-10">10额度重置</a>
<a href="/codex-reset/history/2026-07-11">11额度重置 +1</a>
<a href="/codex-reset/history/2026-07-15">15额度重置</a>
<a href="/codex-reset/history/2026-07-18">18额度重置</a>
<a href="/codex-reset/history/2026-07-22">22额度重置</a>
<a href="/codex-reset/history/2026-07-26">26额度重置</a>
<a href="/codex-reset/history/2026-07-28">28额度重置</a>
<p>监控正常 · 最近检查 9/29 21:50</p>
</body></html>"""


def _offline_controller_check(c: Checker) -> None:
    """用 DataController 完整跑一遍采集链路。

    刻意关掉接口（use_api=False），这样自检不依赖网络、也不消耗任何额度，
    只验证「本地会话日志 → 解析 → 分析」这条链路。
    """
    import queue as _queue

    from .appenv import Settings

    settings = Settings()
    settings.use_api = False
    settings.local_fallback = True
    settings.community_events = False

    events: "_queue.Queue[dict]" = _queue.Queue()
    controller = DataController(settings, events)
    if not controller.refresh_async("selftest"):
        c.check(False, "启动采集任务")
        return
    bundle = None
    for _ in range(600):  # 最多等 60 秒
        try:
            event = events.get(timeout=0.1)
        except _queue.Empty:
            continue
        if event.get("type") == "refresh_done":
            bundle = event["bundle"]
            break
    c.check(bundle is not None, "采集任务在超时前完成")
    if bundle is None:
        return
    snap = bundle.snapshot
    c.lines.append(
        f"  [INFO] 数据源={snap.source or 'none'} 窗口={len(snap.windows)} "
        f"重置卡={len(snap.credits)} 采样={len(bundle.samples)}"
    )
    c.check(bool(snap.error) or snap.ok, "失败时带可读错误信息", snap.error)
    c.check(bool(bundle.advice.headline), "生成了分析结论", bundle.advice.headline)
    if snap.windows:
        c.check(all(0 <= w.used_percent <= 100 for w in snap.windows), "百分比取值范围正确")
    if snap.ok:
        c.check(
            bool(snap.windows) or snap.source == "local",
            "成功时至少有一个额度窗口",
        )
    for credit in snap.credits:
        c.check(
            credit.expires_at is None or credit.expires_at > 1_000_000_000,
            "重置卡到期时间为合法时间戳",
            str(credit.expires_at),
        )


def run_selftest(live: bool = False) -> int:
    c = Checker()

    # ---------------- 时间解析 ----------------
    c.section("时间解析")
    c.check(parse_ts(1790697295) == 1790697295, "epoch 秒")
    c.check(parse_ts(1790697295000) == 1790697295, "epoch 毫秒自动降级")
    c.check(parse_ts("1790697295") == 1790697295, "字符串数字")
    c.check(parse_ts("2026-06-12T00:00:00Z") == 1781222400, "ISO8601 UTC")
    c.check(parse_ts("") is None and parse_ts(None) is None, "空值安全")

    # ---------------- 用量解析 ----------------
    c.section("额度解析")
    windows, meta = parse_usage(USAGE_FIXTURE)
    kinds = sorted(w.kind for w in windows)
    c.check(len(windows) >= 2, "至少解析出 2 个窗口", f"得到 {len(windows)}")
    c.check(WINDOW_5H in kinds, "识别 5 小时窗口")
    c.check(WINDOW_WEEKLY in kinds, "识别每周窗口")
    five = next((w for w in windows if w.kind == WINDOW_5H), None)
    weekly = next((w for w in windows if w.kind == WINDOW_WEEKLY), None)
    c.check(five is not None and abs(five.used_percent - 62.0) < 0.01, "5 小时已用百分比")
    c.check(five is not None and five.resets_at == 1790697295, "5 小时重置时间")
    c.check(weekly is not None and weekly.window_minutes == 10080, "每周窗口长度")
    c.check(abs((five.remaining_percent if five else 0) - 38.0) < 0.01, "剩余百分比换算")
    c.check(meta.get("plan_type") == "plus", "套餐字段提取")

    # 重复窗口去重（rate_limits 与 rate_limits_by_limit_id 同时存在）
    keys = [(w.limit_id, w.slot, w.window_minutes) for w in windows]
    c.check(len(keys) == len(set(keys)), "重复窗口已去重")

    # ---------------- 本地快照 ----------------
    c.section("本地会话日志回退")
    lw, lm = parse_usage(LOCAL_FIXTURE, source="local")
    c.check(len(lw) >= 2, "本地快照解析窗口")
    c.check(all(w.source == "local" for w in lw), "来源标记为 local")
    c.check(lm.get("plan_type") == "plus", "本地套餐提取")

    # ---------------- 重置卡解析 ----------------
    c.section("重置卡解析")
    credits = parse_credits(CREDITS_FIXTURE, now=1780000000)
    c.check(len(credits) == 2, "过滤掉已消耗的卡", f"得到 {len(credits)}")
    c.check(
        credits[0].expires_at is not None and credits[0].expires_at < (credits[1].expires_at or 0),
        "按到期时间升序",
    )
    c.check(credits[0].credit_id.startswith("b4f53a61"), "credit_id 提取")
    c.check(credits[1].credit_id.startswith("2815139"), "备用 id 字段提取")
    c.check(credits[1].expires_at == 1784600000, "毫秒时间戳归一化")
    c.check(credits[0].scope == "weekly", "scope 字段提取")
    c.check(parse_credits([]) == [], "空列表安全")
    c.check(parse_credits({"credits": "oops"}) == [], "异常结构安全")
    c.check(len(parse_credits([{"expires_at": 1790000000}], now=1780000000)) == 1, "单对象兜底")

    # ---------------- 分析引擎 ----------------
    c.section("分析引擎")
    now = 1790000000.0  # 固定"现在"，保证结果可复现
    snap = Snapshot(ok=True, source="api")
    snap.windows = windows
    snap.credits = [
        ResetCredit(credit_id="soon00000001", expires_at=int(now) + 86400),        # 1 天后到期
        ResetCredit(credit_id="later0000002", expires_at=int(now) + 20 * 86400),   # 20 天后到期
    ]

    samples: List[RateSample] = []
    base = int(now) - 6 * 3600
    for i in range(60):
        ts = base + i * 360
        samples.append(RateSample(ts=ts, limit_id="codex", slot="primary",
                                  window_minutes=300, used_percent=min(99.0, 20 + i * 1.2),
                                  resets_at=1790697295))
        samples.append(RateSample(ts=ts, limit_id="codex", slot="secondary",
                                  window_minutes=10080, used_percent=min(99.0, 30 + i * 0.3),
                                  resets_at=1791091046))
    trend = ana.analyze_trends(samples, 300)
    c.check(trend.per_hour is not None and trend.per_hour > 5, "消耗速度估算",
            f"per_hour={trend.per_hour}")
    c.check(trend.span_hours > 5.0, "采样跨度计算")

    advice = ana.build_advice(snap, samples, now=now)
    c.check(bool(advice.headline), "给出结论标题", advice.headline)
    c.check(advice.verdict in ("use_now", "use_soon", "wait", "none"), "结论类型合法",
            advice.verdict)
    c.check(len(advice.credit_advices) == 2, "每张卡都有建议")
    c.check(
        advice.credit_advices[0].verdict in ("use_now", "use_soon"),
        "临期卡（1 天）被判为尽快使用",
        advice.credit_advices[0].verdict,
    )
    c.check(
        advice.credit_advices[1].verdict == "wait",
        "远期货（20 天）被判为继续保留",
        advice.credit_advices[1].verdict,
    )
    c.check(
        advice.credit_advices[1].action_time is not None
        and advice.credit_advices[1].action_time > now,
        "保留策略给出未来的建议时间",
    )

    # 额度很空 + 卡很久到期 -> 应该建议等待
    empty = Snapshot(ok=True, source="api")
    empty.windows = [
        copy.deepcopy(w) for w in windows if w.kind == WINDOW_5H
    ]
    if empty.windows:
        empty.windows[0].used_percent = 5.0
    empty.credits = [ResetCredit(credit_id="fresh0000001", expires_at=int(now) + 30 * 86400)]
    advice2 = ana.build_advice(empty, [], now=now)
    c.check(advice2.credit_advices[0].verdict == "wait", "额度充足时建议等待",
            advice2.credit_advices[0].verdict)

    # 额度打满 -> 立即使用
    full = Snapshot(ok=True, source="api")
    full.windows = [copy.deepcopy(w) for w in windows if w.kind == WINDOW_5H]
    if full.windows:
        full.windows[0].used_percent = 99.0
    full.credits = [ResetCredit(credit_id="full00000001", expires_at=int(now) + 30 * 86400)]
    advice3 = ana.build_advice(full, [], now=now)
    c.check(advice3.credit_advices[0].verdict == "use_now", "额度打满时立即使用",
            advice3.credit_advices[0].verdict)

    c.check(ana.fmt_duration(3661).startswith("1 小时"), "时长格式化")
    c.check(ana.fmt_duration(None) == "—", "空时长安全")

    # 社区重置临近时，远期卡应把"社区重置"纳入理由
    community = ev.parse_page(SAMPLE_HTML, now=now)
    # 明确构造 6 小时内的公告，避免样例日期与固定 now 不一致。
    community.announcement.window_start = int(now + 6 * 3600)
    community.announcement.window_end = int(now + 8 * 3600)
    advice4 = ana.build_advice(snap, samples, now=now, community=community)
    c.check(bool(advice4.community_note), "分析里带上社区重置结论",
            advice4.community_note)
    c.check(advice4.community_ts is not None, "分析里带上社区重置时间")
    c.check(len(advice4.credit_advices) == 2, "社区信息不影响卡片建议数量")
    c.check(
        all(
            item.verdict in ("use_now", "use_soon", "wait")
            for item in advice4.credit_advices
        ),
        "结论类型合法",
        str([item.verdict for item in advice4.credit_advices]),
    )
    c.check(
        all(
            item.action_time is None or item.action_time > now - 60
            for item in advice4.credit_advices
        ),
        "建议时间不在过去",
    )
    c.check(
        any(
            "社区重置" in reason
            for item in advice4.credit_advices
            for reason in item.reasons
        ),
        "至少一张卡的理由里提到社区重置",
        str([item.reasons for item in advice4.credit_advices]),
    )

    # ---------------- 鉴权发现 ----------------
    c.section("鉴权发现")
    auths = discover_auth_files()
    c.check(bool(auths), "发现 auth.json", f"codex_home={codex_home()}")
    if auths:
        info = auth_identity(auths[0])
        c.check(info.auth_mode == "chatgpt", "auth_mode = chatgpt", info.auth_mode)
        c.check(bool(info.account_id), "解析出 account_id")
        claims = decode_jwt(auths[0].access_token)
        c.check(bool(claims), "access_token 可解码")
        exp = parse_ts(claims.get("exp"))
        c.check(exp is not None, "解析出 token 过期时间")
        if exp:
            left = (exp - time.time()) / 3600.0
            c.lines.append(f"  [INFO] access_token 剩余有效期约 {left:.1f} 小时")
        c.lines.append(
            f"  [INFO] 账号 {info.email or '未知'} / 套餐 {info.plan_type or '未知'}"
        )
    else:
        c.lines.append("  [INFO] 未找到凭据：离线模式下只能读本地会话日志")

    # ---------------- 社区重置事件 / 预估 ----------------
    c.section("社区重置预估")
    try:
        evn = ev.parse_page(SAMPLE_HTML, now=1789000000.0)
        c.check(evn.ok, "页面解析成功")
        c.check(evn.median_interval_days == 2.1, "解析出间隔中位数",
                str(evn.median_interval_days))
        c.check(evn.reset_count_90d == 23, "解析出 90 天重置次数",
                str(evn.reset_count_90d))
        c.check(evn.card_count_90d == 8, "解析出 90 天发卡次数",
                str(evn.card_count_90d))
        c.check(len(evn.resets()) >= 15, "解析出足够多的重置事件",
                str(len(evn.resets())))
        c.check(len(evn.cards()) == 2, "准确解析出样例中的 2 次发卡事件", str(len(evn.cards())))
        c.check(evn.last_reset_date == "2026-09-27", "上次重置日期",
                evn.last_reset_date)
        c.check(evn.announcement is not None, "解析出已宣布的重置")
        if evn.announcement:
            c.check(evn.announcement.window_start is not None,
                    "公告时间窗可解析为时间戳")
            c.check(bool(evn.announcement.scope), "公告适用范围",
                    evn.announcement.scope)
        c.check(evn.next_reset is not None
                and evn.next_reset.next_event_ts is not None, "给出下次重置预估")
        if evn.next_reset:
            c.check(evn.next_reset.source == "announcement",
                    "已宣布时优先使用公告窗口", evn.next_reset.source)
            c.check(evn.next_reset.confidence >= 0.8, "公告置信度较高")
        c.check(evn.next_card is not None
                and evn.next_card.next_event_ts is not None, "给出下次发卡预估")
        if evn.next_card:
            c.check(evn.next_card.source != "announcement",
                    "发卡走统计外推", evn.next_card.source)

        # 无公告时应走统计外推
        evn2 = ev.parse_page(SAMPLE_HTML.replace("等待额度重置生效", "本期已结束"),
                             now=1789000000.0)
        c.check(evn2.announcement is None, "无公告时不构造公告")
        c.check(evn2.next_reset is not None, "无公告时仍能外推")
        if evn2.next_reset:
            c.check(evn2.next_reset.interval_days is not None
                    and 1.0 <= evn2.next_reset.interval_days <= 5.0,
                    "外推间隔落在合理区间", str(evn2.next_reset.interval_days))
            c.check(0.2 <= evn2.next_reset.confidence <= 0.9, "置信度落在合理区间",
                    str(evn2.next_reset.confidence))

        base = ev.baseline_snapshot(now=1789000000.0)
        c.check(len(base.resets()) >= 10, "内置基线可用", str(len(base.resets())))
        c.check(base.next_reset is not None, "基线也能给出预估")
        c.check(bool(ev.fmt_countdown(1789000000 + 7200, 1789000000.0)),
                "倒计时文案")
        c.check(ev.fmt_local_window(None, None) == "—", "空时间窗安全")
        c.check(bool(ev.confidence_label(0.9)), "置信度文案")
        c.check(bool(ev.describe_event(base.resets()[-1], 1789000000.0)),
                "事件描述文案")
        c.check(ev.parse_page("", now=1789000000.0).ok is False, "空页面安全")
    except Exception as exc:
        c.check(False, "社区预估模块无异常", repr(exc))

    # ---------------- 全链路（离线） ----------------
    c.section("采集链路")
    try:
        _offline_controller_check(c)
    except Exception as exc:
        c.check(False, "采集链路无异常", repr(exc))

    if live:
        c.section("实时接口")
        code = _live_check(c)
        if code:
            return code

    text = c.report()
    _emit(text)
    return 0 if c.failed == 0 else 1


def _live_check(c: Checker) -> int:
    from .providers import CodexClient, QuotaError

    auths = discover_auth_files()
    if not auths:
        c.check(False, "实时调用需要有效凭据")
        _emit(c.report())
        return 1
    client = CodexClient(auths[0])
    try:
        payload = client.fetch_usage()
        windows, meta = parse_usage(payload)
        c.check(True, "GET /wham/usage 成功")
        c.check(len(windows) > 0, "接口返回了额度窗口", f"{len(windows)} 个")
        for window in windows:
            c.lines.append(
                f"  [INFO] {window.title()}（{window.window_minutes} 分钟）"
                f" 已用 {window.used_percent:.0f}%  剩余 {window.remaining_percent:.0f}%"
            )
    except QuotaError as exc:
        c.check(False, "GET /wham/usage", str(exc))
    try:
        credits = client.fetch_reset_credits()
        c.check(True, "GET /wham/rate-limit-reset-credits 成功")
        c.lines.append(f"  [INFO] 重置卡 {len(credits)} 张")
        for credit in credits[:5]:
            c.lines.append(
                f"    #{credit.short_id()} 发放 {ana.fmt_full_clock(credit.granted_at)}"
                f" 到期 {ana.fmt_full_clock(credit.expires_at)}"
            )
        if not credits:
            c.lines.append("  [INFO] 该账号当前没有重置卡（正常情况）")
    except QuotaError as exc:
        c.check(False, "GET /wham/rate-limit-reset-credits", str(exc))
    _emit(c.report())
    return 0 if c.failed == 0 else 1


def _emit(text: str) -> None:
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    try:
        from .appenv import log

        log("自检输出已写入日志")
        for line in text.splitlines():
            log("  " + line)
    except Exception:
        pass
