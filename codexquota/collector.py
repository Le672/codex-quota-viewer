"""数据采集编排：后台线程抓取 + 结果投递到 UI 队列。"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import analytics as ana
from .appenv import Settings, log
from .events import CommunitySnapshot, fetch_community
from .token_usage import TokenReport, TokenScanner
from .providers import (
    AccountInfo,
    CodexClient,
    QuotaError,
    RateSample,
    Snapshot,
    auth_identity,
    begin_redeem,
    collect_history,
    discover_auth_files,
    end_redeem,
    extract_plan_from_usage,
    local_usage_snapshot,
    parse_credits,
    parse_usage,
)

HISTORY_TTL_SECONDS = 600.0


@dataclass
class Bundle:
    """一次刷新后的完整 UI 数据包。"""

    snapshot: Snapshot = field(default_factory=Snapshot)
    advice: ana.Advice = field(default_factory=ana.Advice)
    samples: List[RateSample] = field(default_factory=list)
    community: Optional[CommunitySnapshot] = None
    fetched_at: float = 0.0
    elapsed: float = 0.0
    error: str = ""
    source_note: str = ""
    tokens: TokenReport = field(default_factory=TokenReport)


class DataController:
    """负责抓数据、算分析，并通过 queue 把结果交给界面主线程。"""

    def __init__(self, settings: Settings, events: "queue.Queue[Dict[str, Any]]"):
        self.settings = settings
        self.events = events
        self._lock = threading.Lock()
        self._busy = False
        self._tokens_busy = False
        self._history: List[RateSample] = []
        self._history_at = 0.0
        self._client: Optional[CodexClient] = None
        self._account_key = "default"
        self._tokens = TokenScanner()
        self._last_snapshot = Snapshot()

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @property
    def busy(self) -> bool:
        return self._busy

    def refresh_async(self, reason: str = "manual") -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self.events.put({"type": "refresh_started", "reason": reason})
        thread = threading.Thread(
            target=self._run_refresh, args=(reason,), name="codexquota-refresh", daemon=True
        )
        thread.start()
        return True

    def load_history_async(self, days: float = 14.0) -> None:
        thread = threading.Thread(
            target=self._run_history, args=(days,), name="codexquota-history", daemon=True
        )
        thread.start()

    def refresh_tokens_async(self) -> bool:
        """只增量读取本机日志，不请求额度 / 预测接口。"""
        with self._lock:
            if self._tokens_busy or self._busy:
                return False
            self._tokens_busy = True
            scanner, snapshot = self._tokens, self._last_snapshot
        def work():
            try:
                report = scanner.scan(snapshot)
                if scanner is self._tokens and snapshot is self._last_snapshot:
                    self.events.put({'type': 'tokens_done', 'tokens': report})
            except Exception as exc:
                log(f'刷新本机 tokens 失败: {exc!r}')
            finally:
                with self._lock:
                    self._tokens_busy = False
        threading.Thread(target=work, name='codexquota-tokens', daemon=True).start()
        return True

    def reset_credit_async(self, credit_id: str) -> bool:
        with self._lock:
            if self._busy:
                return False
            # 必须先占位：否则 UI 的等待轮询会立刻判定"已结束"
            self._busy = True
        thread = threading.Thread(
            target=self._run_reset,
            args=(credit_id,),
            name="codexquota-reset",
            daemon=True,
        )
        thread.start()
        return True
    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _run_refresh(self, reason: str) -> None:
        started = time.time()
        try:
            bundle = self._collect(reason)
        except Exception as exc:  # 兜底：任何异常都要让 UI 恢复可用
            log(f"刷新发生未预期异常: {exc!r}\n{traceback.format_exc()}")
            bundle = Bundle(
                snapshot=Snapshot(ok=False, error=f"内部错误：{exc}"),
                advice=ana.Advice(headline="刷新失败", verdict="unknown"),
                error=str(exc),
            )
        bundle.elapsed = time.time() - started
        bundle.fetched_at = time.time()
        with self._lock:
            self._busy = False
        self.events.put({"type": "refresh_done", "bundle": bundle, "reason": reason})

    def _collect(self, reason: str) -> Bundle:
        settings = self.settings
        errors: List[str] = []
        snapshot: Optional[Snapshot] = None
        auths = discover_auth_files()

        previous_account = self._account_key
        if auths:
            auth = auths[0]
            info = auth_identity(auth)
            self._account_key = info.email or info.account_id or "default"
            if self._account_key != previous_account:
                self._history = []
                self._history_at = 0.0
                self._tokens = TokenScanner()
            if (
                self._client is None
                or self._client.auth.path != auth.path
                or self._client.auth.data != auth.data
            ):
                self._client = CodexClient(auth, info)
            client = self._client

            if settings.use_api:
                snapshot = self._fetch_api(client, info, errors)
            if snapshot is None and settings.local_fallback:
                snapshot = self._fetch_local(errors, info)
                if snapshot is not None:
                    errors = []
        else:
            errors.append(
                "没有找到 Codex 凭据（%USERPROFILE%\\.codex\\auth.json），"
                "请先执行 codex login"
            )

        if snapshot is None:
            if settings.local_fallback:
                snapshot = self._fetch_local(errors, None)
            if snapshot is None:
                snapshot = Snapshot(ok=False, error=errors[0] if errors else "无可用数据源")
                snapshot.warnings.extend(errors[1:])

        # 社区重置 / 发卡事件（用于预估下次重置时间）
        community: Optional[CommunitySnapshot] = None
        # 独立的辅助数据并行采集，避免社区网络拖慢日志扫描。
        self._last_snapshot = snapshot
        with ThreadPoolExecutor(max_workers=3) as pool:
            history_job = pool.submit(self._get_history)
            token_job = pool.submit(self._tokens.scan, snapshot)
            community_job = (pool.submit(fetch_community, ttl_hours=settings.community_ttl)
                             if settings.community_events else None)
            samples = history_job.result()
            try:
                tokens = token_job.result()
            except Exception as exc:
                tokens = TokenReport()
                log(f"采集 token 用量失败: {exc!r}")
            if community_job:
                try:
                    community = community_job.result()
                except Exception as exc:
                    log(f"获取社区事件失败: {exc!r}")

        advice = ana.build_advice(
            snapshot,
            samples,
            warn_percent=settings.warn_percent,
            expiry_warn_days=settings.expiry_warn_days,
            community=community if community and community.source != "baseline" else None,
        )
        note = ""
        if snapshot.source == "api":
            note = "实时接口"
        elif snapshot.source == "local":
            note = "本地会话日志"
        return Bundle(
            snapshot=snapshot,
            advice=advice,
            samples=samples,
            community=community,
            source_note=note,
            tokens=tokens,
        )

    def _fetch_api(
        self, client: CodexClient, info: AccountInfo, errors: List[str]
    ) -> Optional[Snapshot]:
        try:
            payload = client.fetch_usage()
        except QuotaError as exc:
            errors.append(str(exc))
            return None
        windows, meta = parse_usage(payload, source="api")
        snap = Snapshot(ok=True, source="api", raw_usage=payload)
        snap.windows = windows
        snap.account = info
        plan = extract_plan_from_usage(meta) or info.plan_type
        if plan:
            snap.account.plan_type = plan
        email = meta.get("email")
        if isinstance(email, str) and email and not snap.account.email:
            snap.account.email = email
        snap.credit_balance = _stringify(meta.get("balance"))
        snap.has_credits = bool(meta.get("has_credits"))
        snap.unlimited = bool(meta.get("unlimited"))
        if not windows:
            snap.ok = False
            snap.error = "接口未返回任何额度窗口（后端结构可能已变更）"
            snap.warnings.append("接口返回 200 但没有解析到额度窗口，可在 --json 输出中查看原始结构")

        # 重置卡
        try:
            embedded = payload.get("rate_limit_reset_credits")
            # usage 有时只给 available_count；这不能替代带到期时间的卡列表。
            has_list = isinstance(embedded, list) or (isinstance(embedded, dict) and
                       any(isinstance(embedded.get(k), list) for k in ("credits", "items", "data")))
            credits = parse_credits(embedded) if has_list else client.fetch_reset_credits()
            snap.credits = credits
            snap.raw_credits = [
                {k: c.raw.get(k) for k in list(c.raw)[:12]} if isinstance(c.raw, dict) else c.raw
                for c in credits
            ]
        except QuotaError as exc:
            snap.credits_supported = False
            snap.warnings.append(f"重置卡读取失败：{exc}")
        snap.warnings.extend(client.notes)
        client.notes = []
        return snap

    def _fetch_local(
        self, errors: List[str], info: Optional[AccountInfo]
    ) -> Optional[Snapshot]:
        windows, meta, ts = local_usage_snapshot()
        if not windows:
            return None
        from .appenv import parse_ts
        snap = Snapshot(ok=True, source="local", raw_usage={"rate_limits": meta},
                        fetched_at=float(parse_ts(ts) or time.time()))
        snap.windows = windows
        snap.account = info or AccountInfo()
        plan = extract_plan_from_usage(meta)
        if plan:
            snap.account.plan_type = plan
        snap.credit_balance = _stringify(meta.get("balance"))
        snap.has_credits = bool(meta.get("has_credits"))
        snap.unlimited = bool(meta.get("unlimited"))
        snap.credits_supported = False
        if ts:
            snap.warnings.append(f"离线快照时间：{ts}")
        snap.warnings.extend(errors)
        if time.time() - snap.fetched_at > 600:
            snap.warnings.append("本地快照已超过 10 分钟，剩余额度仅供参考")
        snap.warnings.append("离线模式：重置卡列表需要联网接口，暂不可用")
        return snap

    def _get_history(self) -> List[RateSample]:
        now = time.time()
        if self._history_at and now - self._history_at < HISTORY_TTL_SECONDS:
            return self._history
        try:
            self._history = collect_history(days=14.0)
            self._history_at = now
        except Exception as exc:
            log(f"采集历史失败: {exc!r}")
        return self._history

    def _run_history(self, days: float) -> None:
        try:
            samples = collect_history(days=days)
            tokens = self._tokens.scan(self._last_snapshot)
            with self._lock:
                self._history = samples
                self._history_at = time.time()
            self.events.put({"type": "history_done", "samples": samples, "tokens": tokens})
        except Exception as exc:
            log(f"重扫历史失败: {exc!r}")
            self.events.put({"type": "history_failed", "error": str(exc)})

    def _run_reset(self, credit_id: str) -> None:
        try:
            if self._client is None:
                auths = discover_auth_files()
                if not auths:
                    raise QuotaError("没有可用的 Codex 凭据", kind="auth")
                auth = auths[0]
                self._client = CodexClient(auth, auth_identity(auth))
                self._account_key = self._client.info.email or self._client.info.account_id or "default"
            redeem_id, reused = begin_redeem(self._account_key, credit_id)
            self.events.put(
                {
                    "type": "reset_note",
                    "text": "复用未完成的请求 id（幂等重试）" if reused else "正在消耗重置卡…",
                }
            )
            ok, message = self._client.consume_credit(credit_id, redeem_id)
            # 明确成功或明确被拒都视为"已解决"，超时/5xx 保留记录以便幂等重试
            resolved = ok or message.startswith("HTTP 4")
            end_redeem(self._account_key, resolved)
            self.events.put(
                {"type": "reset_done", "ok": ok, "message": message, "credit_id": credit_id}
            )
        except QuotaError as exc:
            self.events.put(
                {"type": "reset_done", "ok": False, "message": str(exc), "credit_id": credit_id}
            )
        except Exception as exc:
            log(f"重置失败: {exc!r}\n{traceback.format_exc()}")
            self.events.put(
                {
                    "type": "reset_done",
                    "ok": False,
                    "message": f"内部错误：{exc}",
                    "credit_id": credit_id,
                }
            )
        finally:
            with self._lock:
                self._busy = False


def _stringify(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    return str(value)
