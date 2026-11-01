"""Codex 数据后端。

数据来源（按优先级）：
  1. 官方接口：
       GET https://chatgpt.com/backend-api/wham/usage
       GET https://chatgpt.com/backend-api/wham/rate-limit-reset-credits
       POST .../wham/rate-limit-reset-credits/consume   （消耗一张重置卡）
     凭据来自 %USERPROFILE%\\.codex\\auth.json（或 accounts/*.auth.json），
     与 codex-auth / 官方 CLI 的文件存储格式一致；access_token 过期时用
     refresh_token 走 https://auth.openai.com/oauth/token 自动续期。

  2. 本地会话日志回退：%USERPROFILE%\\.codex\\sessions\\**\\rollout-*.jsonl
     里每次 token_count 事件都带一份 rate_limits 快照，可离线读取。

所有解析都写得很宽容（字段名多变、层级可能不同），并且在解析不到时会给出
明确的诊断信息，便于在 UI 里排查。
"""

from __future__ import annotations

import base64
import json
import math
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .appenv import APP_VERSION, codex_home, log, parse_ts, pending_dir

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
CHATGPT_BASE = os.environ.get("CODEX_RESET_BASE_URL", "https://chatgpt.com/backend-api")
AUTH_TOKEN_URL = os.environ.get(
    "CODEX_REFRESH_TOKEN_URL_OVERRIDE", "https://auth.openai.com/oauth/token"
)
# 与官方 Codex 客户端一致的 OAuth client id
DEFAULT_CLIENT_ID = os.environ.get(
    "CODEX_APP_SERVER_LOGIN_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann"
)
DEFAULT_SCOPES = ("openid profile email offline_access", "openid profile email", "")

FIVE_HOUR_MINUTES = 300
WEEKLY_MINUTES = 10080
WINDOW_5H = "5h"
WINDOW_WEEKLY = "weekly"
WINDOW_OTHER = "other"


class QuotaError(Exception):
    """可预期的错误（网络/鉴权/接口变更），UI 直接把 message 显示给用户。"""

    def __init__(self, message: str, *, kind: str = "error", detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 简单转发
        return self.args[0] if self.args else self.kind


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class QuotaWindow:
    """一个额度窗口（5 小时 / 每周 / 其它）。"""

    limit_id: str = "codex"
    limit_name: str = ""
    slot: str = "primary"
    kind: str = WINDOW_OTHER
    used_percent: float = 0.0
    window_minutes: int = 0
    resets_at: Optional[int] = None
    source: str = "api"

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))

    def seconds_until_reset(self, now: Optional[float] = None) -> Optional[float]:
        if not self.resets_at:
            return None
        return self.resets_at - (now if now is not None else time.time())

    def title(self) -> str:
        if self.kind == WINDOW_5H:
            return "5 小时额度"
        if self.kind == WINDOW_WEEKLY:
            return "每周额度"
        return _minutes_label(self.window_minutes) or "额度窗口"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "limit_id": self.limit_id,
            "limit_name": self.limit_name,
            "slot": self.slot,
            "kind": self.kind,
            "used_percent": self.used_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
            "source": self.source,
        }


@dataclass
class ResetCredit:
    """一张「重置卡」（rate-limit reset credit）。"""

    credit_id: str = ""
    granted_at: Optional[int] = None
    expires_at: Optional[int] = None
    scope: str = ""
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def days_left(self, now: Optional[float] = None) -> Optional[float]:
        if not self.expires_at:
            return None
        return (self.expires_at - (now if now is not None else time.time())) / 86400.0

    def short_id(self) -> str:
        cid = self.credit_id or ""
        if len(cid) > 10:
            return cid[:10]
        return cid or "(无编号)"


@dataclass
class AccountInfo:
    email: str = ""
    plan_type: str = ""
    account_id: str = ""
    fedramp: bool = False
    auth_path: str = ""
    auth_mode: str = "chatgpt"


@dataclass
class Snapshot:
    """一次采集的完整结果。"""

    ok: bool = False
    source: str = ""            # api | local | none
    account: AccountInfo = field(default_factory=AccountInfo)
    windows: List[QuotaWindow] = field(default_factory=list)
    credits: List[ResetCredit] = field(default_factory=list)
    credit_balance: Optional[str] = None
    has_credits: bool = False
    unlimited: bool = False
    credits_supported: bool = True
    fetched_at: float = field(default_factory=time.time)
    warnings: List[str] = field(default_factory=list)
    error: str = ""
    raw_usage: Dict[str, Any] = field(default_factory=dict)
    raw_credits: Any = None

    def window(self, kind: str) -> Optional[QuotaWindow]:
        for w in self.windows:
            if w.kind == kind:
                return w
        return None

    def other_windows(self) -> List[QuotaWindow]:
        return [w for w in self.windows if w.kind == WINDOW_OTHER]

    def sorted_credits(self) -> List[ResetCredit]:
        """按到期时间升序（无到期时间的排最后）。"""
        return sorted(
            self.credits,
            key=lambda c: (c.expires_at is None, c.expires_at or 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "account": {
                "email": self.account.email,
                "plan_type": self.account.plan_type,
                "account_id": self.account.account_id,
                "auth_path": self.account.auth_path,
                "auth_mode": self.account.auth_mode,
            },
            "windows": [w.to_dict() for w in self.windows],
            "credits": [
                {
                    "credit_id": c.credit_id,
                    "granted_at": c.granted_at,
                    "expires_at": c.expires_at,
                    "scope": c.scope,
                    "status": c.status,
                }
                for c in self.sorted_credits()
            ],
            "credit_balance": self.credit_balance,
            "has_credits": self.has_credits,
            "unlimited": self.unlimited,
            "credits_supported": self.credits_supported,
            "warnings": list(self.warnings),
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _minutes_label(minutes: int) -> str:
    if not minutes or minutes <= 0:
        return ""
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} 天"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} 小时"
    return f"{minutes} 分钟"


def classify_window(minutes: int) -> str:
    if minutes == FIVE_HOUR_MINUTES:
        return WINDOW_5H
    if minutes == WEEKLY_MINUTES:
        return WINDOW_WEEKLY
    # 兜底：接近 5 小时/7 天也能识别
    if 240 <= minutes <= 360:
        return WINDOW_5H
    if 6 * 1440 <= minutes <= 8 * 1440:
        return WINDOW_WEEKLY
    return WINDOW_OTHER


def _first_key(data: Dict[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    # 大小写不敏感 + 忽略下划线
    flat = {str(k).lower().replace("_", ""): v for k, v in data.items()}
    for name in names:
        key = name.lower().replace("_", "")
        if key in flat and flat[key] is not None:
            return flat[key]
    return None


def _iter_nodes(node: Any, depth: int = 0) -> Iterable[Any]:
    """深度优先遍历 dict/list（限制深度，避免异常结构卡死）。"""
    if depth > 6:
        return
    yield node
    if isinstance(node, dict):
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _iter_nodes(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                yield from _iter_nodes(value, depth + 1)


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
def decode_jwt(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# 凭据发现与刷新
# --------------------------------------------------------------------------- #
@dataclass
class AuthFile:
    path: Path
    data: Dict[str, Any]

    @property
    def tokens(self) -> Dict[str, Any]:
        t = self.data.get("tokens")
        return t if isinstance(t, dict) else {}

    @property
    def auth_mode(self) -> str:
        mode = self.data.get("auth_mode")
        if isinstance(mode, str) and mode:
            return mode
        return "chatgpt" if self.tokens else "unknown"

    @property
    def access_token(self) -> str:
        return str(self.tokens.get("access_token") or "")

    @property
    def refresh_token(self) -> str:
        return str(self.tokens.get("refresh_token") or "")

    @property
    def id_token(self) -> str:
        return str(self.tokens.get("id_token") or "")

    @property
    def account_id(self) -> str:
        return str(self.tokens.get("account_id") or "")


def discover_auth_files() -> List[AuthFile]:
    """按 codex-reset / codex-auth 的顺序找凭据文件。"""
    found: List[AuthFile] = []
    home = codex_home()
    candidates: List[Path] = [home / "auth.json"]

    accounts_dir = home / "accounts"
    if accounts_dir.is_dir():
        try:
            registry = accounts_dir / "registry.json"
            order: List[str] = []
            if registry.is_file():
                try:
                    reg = json.loads(registry.read_text(encoding="utf-8"))
                    order = _registry_order(reg)
                except Exception:
                    order = []
            files = sorted(accounts_dir.glob("*.auth.json"))
            if order:
                files.sort(
                    key=lambda p: (
                        order.index(p.name) if p.name in order else len(order),
                        p.name,
                    )
                )
            candidates.extend(files)
        except Exception:
            pass

    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            af = AuthFile(path=path, data=data)
            if af.auth_mode in ("apikey", "agentIdentity", "bedrockApiKey"):
                log(f"跳过无 ChatGPT 额度的凭据: {path} ({af.auth_mode})")
                continue
            if not af.access_token and not af.refresh_token:
                continue
            found.append(af)
        except Exception as exc:
            log(f"读取凭据失败 {path}: {exc!r}")
    return found


def _registry_order(reg: Any) -> List[str]:
    """从 codex-auth 的 registry.json 里取出账号文件名顺序。"""
    out: List[str] = []
    try:
        items = reg
        if isinstance(reg, dict):
            for key in ("accounts", "items", "entries", "order"):
                if isinstance(reg.get(key), list):
                    items = reg[key]
                    break
        if isinstance(items, dict):
            items = list(items.values())
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str):
                    out.append(item if item.endswith(".json") else f"{item}.auth.json")
                elif isinstance(item, dict):
                    for key in ("file", "filename", "path", "auth_file", "name"):
                        v = item.get(key)
                        if isinstance(v, str) and v:
                            out.append(
                                v if v.endswith(".json") else f"{v}.auth.json"
                            )
                            break
    except Exception:
        pass
    return out


def auth_identity(af: AuthFile) -> AccountInfo:
    """从 id_token / access_token 里解析账号信息。"""
    info = AccountInfo(auth_path=str(af.path), auth_mode=af.auth_mode)
    claims: Dict[str, Any] = {}
    for token in (af.id_token, af.access_token):
        if token:
            claims = decode_jwt(token)
            if claims:
                break
    auth_ns = claims.get("https://api.openai.com/auth")
    if isinstance(auth_ns, dict):
        info.account_id = str(auth_ns.get("chatgpt_account_id") or "")
        info.plan_type = str(auth_ns.get("chatgpt_plan_type") or "")
        info.fedramp = bool(auth_ns.get("chatgpt_account_is_fedramp"))
    profile = claims.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        info.email = str(profile.get("email") or "")
    info.email = info.email or str(claims.get("email") or "")
    info.account_id = info.account_id or af.account_id
    try:
        if info.fedramp is False:
            flag = af.data.get("chatgpt_account_is_fedramp")
            info.fedramp = bool(flag)
    except Exception:
        pass
    return info


def token_expiry(token: str) -> Optional[int]:
    claims = decode_jwt(token)
    return parse_ts(claims.get("exp"))


def _patch_auth_file(af: AuthFile, *, access_token: str, refresh_token: str,
                     id_token: str = "") -> None:
    """把刷新后的 token 原子写回 auth.json（与官方 CLI 行为一致）。"""
    data = dict(af.data)
    tokens = dict(af.tokens)
    tokens["access_token"] = access_token
    if refresh_token:
        tokens["refresh_token"] = refresh_token
    if id_token:
        tokens["id_token"] = id_token
    data["tokens"] = tokens
    data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000000Z"
    tmp = af.path.with_suffix(af.path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, af.path)
        af.data = data
        log(f"已刷新并写回 token: {af.path}")
    except Exception as exc:
        log(f"写回 token 失败（不影响本次使用）: {exc!r}")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def refresh_access_token(af: AuthFile, *, force: bool = False) -> str:
    """用 refresh_token 换新的 access_token，返回可用的 access_token。"""
    access = af.access_token
    exp = token_expiry(access)
    now = int(time.time())
    if not force and access and (exp is None or exp - now > 120):
        return access
    refresh = af.refresh_token
    if not refresh:
        if access:
            return access
        raise QuotaError("凭据里没有 refresh_token，请重新执行 codex login", kind="auth")

    client_id = DEFAULT_CLIENT_ID
    claims = decode_jwt(af.id_token) if af.id_token else {}
    aud = claims.get("aud")
    if isinstance(aud, list) and aud and isinstance(aud[0], str):
        client_id = aud[0]

    last_error = ""
    for scope in DEFAULT_SCOPES:
        body = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "scope": scope,
        }
        try:
            status, payload = http_request(
                AUTH_TOKEN_URL,
                method="POST",
                data=urllib.parse.urlencode(body).encode("ascii"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=20,
            )
        except QuotaError as exc:
            last_error = str(exc)
            continue
        if status == 200 and isinstance(payload, dict) and payload.get("access_token"):
            new_access = str(payload["access_token"])
            new_refresh = str(payload.get("refresh_token") or "")
            new_id = str(payload.get("id_token") or "")
            _patch_auth_file(
                af, access_token=new_access, refresh_token=new_refresh, id_token=new_id
            )
            return new_access
        if isinstance(payload, dict):
            err = payload.get("error")
            desc = payload.get("error_description") or payload.get("message")
            last_error = f"{err or status}: {desc or ''}".strip()
        else:
            last_error = f"HTTP {status}"
        if status in (400, 401) and "invalid_grant" in str(last_error):
            break
    if access and (exp is None or exp - now > 0):
        log(f"刷新失败但旧 token 仍在有效期内，继续使用: {last_error}")
        return access
    raise QuotaError(
        f"access_token 已过期且刷新失败（{last_error or '未知原因'}），请重新执行 codex login",
        kind="auth",
    )


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: int = 20,
) -> Tuple[int, Any]:
    """返回 (status, json_or_text)。4xx/5xx 也返回状态码，不抛异常。"""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        if value is not None:
            req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            body = resp.read()
            status = int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        status = int(exc.code)
    except urllib.error.URLError as exc:
        raise QuotaError(f"网络请求失败：{exc.reason}", kind="network") from exc
    except TimeoutError as exc:
        raise QuotaError("网络请求超时", kind="network") from exc
    except Exception as exc:
        raise QuotaError(f"网络请求异常：{exc!r}", kind="network") from exc

    text = body.decode("utf-8", errors="replace") if body else ""
    if not text:
        return status, None
    try:
        return status, json.loads(text)
    except Exception:
        return status, text


class CodexClient:
    """带自动续期的 Codex 后端客户端。"""

    def __init__(self, auth: AuthFile, info: Optional[AccountInfo] = None):
        self.auth = auth
        self.info = info or auth_identity(auth)
        self._token = ""
        self._token_checked_at = 0.0
        self.notes: List[str] = []

    # -- 基础 ------------------------------------------------------------- #
    def token(self, force_refresh: bool = False) -> str:
        now = time.time()
        if (
            not force_refresh
            and self._token
            and now - self._token_checked_at < 60
        ):
            return self._token
        self._token = refresh_access_token(self.auth, force=force_refresh)
        self._token_checked_at = now
        return self._token

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token()}",
            "User-Agent": f"codex-quota-viewer/{APP_VERSION}",
            "originator": "codex_cli_rs",
        }
        account_id = self.info.account_id or self.auth.account_id
        if account_id:
            headers["chatgpt-account-id"] = account_id
        if self.info.fedramp:
            headers["X-OpenAI-Fedramp"] = "true"
        return headers

    def _get_json(self, path: str) -> Tuple[int, Any]:
        url = f"{CHATGPT_BASE.rstrip('/')}{path}"
        status, payload = http_request(url, headers=self._headers())
        if status == 401:
            # token 可能被服务端提前失效：强制刷新一次再试
            self.token(force_refresh=True)
            status, payload = http_request(url, headers=self._headers())
        return status, payload

    def _post_json(self, path: str, body: Dict[str, Any]) -> Tuple[int, Any]:
        url = f"{CHATGPT_BASE.rstrip('/')}{path}"
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        raw = json.dumps(body).encode("utf-8")
        status, payload = http_request(url, method="POST", headers=headers, data=raw)
        if status == 401:
            self.token(force_refresh=True)
            headers = self._headers()
            headers["Content-Type"] = "application/json"
            status, payload = http_request(url, method="POST", headers=headers, data=raw)
        return status, payload

    # -- 接口 ------------------------------------------------------------- #
    def fetch_usage(self) -> Dict[str, Any]:
        status, payload = self._get_json("/wham/usage")
        if status != 200:
            detail = ""
            if isinstance(payload, dict):
                detail = str(
                    payload.get("detail")
                    or payload.get("error")
                    or payload.get("message")
                    or ""
                )
            elif isinstance(payload, str):
                detail = payload[:400]
            raise QuotaError(
                f"获取额度失败（HTTP {status}）{('：' + detail) if detail else ''}",
                kind="auth" if status in (401, 403) else "api",
                detail=detail,
            )
        if not isinstance(payload, dict):
            raise QuotaError("额度接口返回了非预期格式", kind="api")
        return payload

    def fetch_reset_credits(self) -> List[ResetCredit]:
        status, payload = self._get_json("/wham/rate-limit-reset-credits")
        if status == 404:
            raise QuotaError("该账号/后端未提供重置卡接口（404）", kind="unsupported")
        if status != 200:
            raise QuotaError(f"读取重置卡失败（HTTP {status}）", kind="api")
        return parse_credits(payload)

    def consume_credit(self, credit_id: str, redeem_id: str) -> Tuple[bool, str]:
        """消耗一张重置卡。返回 (是否成功, 说明)。"""
        body: Dict[str, Any] = {"redeem_request_id": redeem_id}
        if credit_id:
            body["credit_id"] = credit_id
        status, payload = self._post_json(
            "/wham/rate-limit-reset-credits/consume", body
        )
        text = ""
        if isinstance(payload, dict):
            text = str(
                payload.get("result")
                or payload.get("status")
                or payload.get("code")
                or payload.get("detail")
                or payload
            )
        elif isinstance(payload, str):
            text = payload[:300]
        if 200 <= status < 300:
            return True, text or "ok"
        return False, f"HTTP {status} {text}".strip()


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def parse_usage(payload: Dict[str, Any], source: str = "api") -> Tuple[List[QuotaWindow], Dict[str, Any]]:
    """从 /wham/usage 响应里提取额度窗口。

    响应结构在不同版本间有过变化（rate_limits -> rate_limits_by_limit_id 等），
    所以这里用"找特征字段"的方式兜底解析。
    """
    windows: List[QuotaWindow] = []
    meta: Dict[str, Any] = {}

    rate_limits = payload.get("rate_limits") or payload.get("rate_limit")
    if rate_limits is None:
        rate_limits = payload.get("rateLimits")
    by_id = payload.get("rate_limits_by_limit_id") or payload.get("rateLimitsByLimitId")

    containers: List[Tuple[str, Any]] = []
    if isinstance(by_id, dict):
        for limit_id, value in by_id.items():
            containers.append((str(limit_id), value))
    elif isinstance(rate_limits, dict):
        containers.append((str(rate_limits.get("limit_id") or "codex"), rate_limits))
    elif isinstance(rate_limits, list):
        for idx, value in enumerate(rate_limits):
            if isinstance(value, dict):
                containers.append((str(value.get("limit_id") or f"limit-{idx}"), value))

    if not containers:
        # 直接窗口和包装过的 rateLimit 对象。
        block = payload.get("rateLimit") or payload
        containers.append(("codex", block))

    seen: set = set()
    for limit_id, block in containers:
        if not isinstance(block, dict):
            continue
        limit_name = str(block.get("limit_name") or block.get("limitName") or "")
        for slot in ("primary", "secondary", "tertiary"):
            slot_data = (block.get(slot) or block.get(slot + "_window")
                         or block.get(slot + "Window"))
            if not isinstance(slot_data, dict):
                continue
            used = _first_key(slot_data, ("used_percent", "usedPercent"))
            minutes = _first_key(slot_data, ("window_minutes", "windowMinutes",
                                             "window_duration_mins", "windowDurationMins"))
            if minutes is None:
                seconds = _first_key(slot_data, ("limit_window_seconds", "limitWindowSeconds"))
                try:
                    minutes = float(seconds) / 60
                except (TypeError, ValueError):
                    pass
            resets = _first_key(slot_data, ("resets_at", "resetsAt", "reset_at"))
            if used is None or minutes is None or isinstance(used, bool):
                continue
            try:
                used_f = float(used)
                minutes_i = int(minutes)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(used_f) or minutes_i <= 0:
                continue
            key = (limit_id, slot, minutes_i)
            if key in seen:
                continue
            seen.add(key)
            windows.append(
                QuotaWindow(
                    limit_id=limit_id,
                    limit_name=limit_name,
                    slot=slot,
                    kind=classify_window(minutes_i),
                    used_percent=max(0.0, min(100.0, used_f)),
                    window_minutes=minutes_i,
                    resets_at=parse_ts(resets),
                    source=source,
                )
            )
        # 有些后端把窗口直接放在 block 上
        if not any(w.limit_id == limit_id for w in windows):
            used = _first_key(block, ("used_percent", "usedPercent"))
            minutes = _first_key(block, ("window_minutes", "windowMinutes",
                                         "window_duration_mins", "windowDurationMins"))
            if used is not None and minutes is not None and not isinstance(used, bool):
                try:
                    if not math.isfinite(float(used)) or int(minutes) <= 0:
                        continue
                    windows.append(
                        QuotaWindow(
                            limit_id=limit_id,
                            limit_name=limit_name,
                            slot="primary",
                            kind=classify_window(int(minutes)),
                            used_percent=max(0.0, min(100.0, float(used))),
                            window_minutes=int(minutes),
                            resets_at=parse_ts(
                                _first_key(block, ("resets_at", "resetsAt"))
                            ),
                            source=source,
                        )
                    )
                except Exception:
                    pass

    # 元信息
    for key in (
        "plan_type",
        "planType",
        "email",
        "account_id",
        "has_credits",
        "unlimited",
        "credits",
        "spend_control_reached",
        "rate_limit_reached_type",
    ):
        if key in payload and payload[key] is not None:
            meta[key] = payload[key]

    # credits 块可能嵌在 rate_limits 里
    for node in _iter_nodes(payload):
        if isinstance(node, dict) and ("has_credits" in node or "hasCredits" in node):
            meta.setdefault("has_credits", _first_key(node, ("has_credits", "hasCredits")))
            meta.setdefault("unlimited", node.get("unlimited"))
            if node.get("balance") is not None:
                meta.setdefault("balance", node.get("balance"))
            break

    windows.sort(key=lambda w: (w.kind == WINDOW_OTHER, w.window_minutes))
    if "plan_type" not in meta:
        for node in _iter_nodes(payload):
            if isinstance(node, dict):
                plan = _first_key(node, ("plan_type", "planType"))
                if plan:
                    meta["plan_type"] = plan
                    break
    return windows, meta


def parse_credits(payload: Any, *, now: Optional[float] = None) -> List[ResetCredit]:
    """从 reset-credits 响应里提取每张重置卡。"""
    now = time.time() if now is None else now
    items: List[Any] = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("credits", "items", "data", "results", "rate_limit_reset_credits", "rateLimitResetCredits",
                    "rateLimitResetCredits", "available"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
            if isinstance(value, dict):
                for sub in ("credits", "items", "data"):
                    if isinstance(value.get(sub), list):
                        items = value[sub]
                        break
                if items:
                    break
        if not items:
            # 单个对象也接受
            if any(k in payload for k in ("expires_at", "expiresAt", "expiry")):
                items = [payload]

    credits: List[ResetCredit] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        credited = ResetCredit(
            credit_id=str(
                _first_key(item, ("credit_id", "creditId", "id", "uuid")) or ""
            ),
            granted_at=parse_ts(
                _first_key(item, ("granted_at", "grantedAt", "grant_date", "created_at",
                                  "createdAt", "issued_at", "issuedAt"))
            ),
            expires_at=parse_ts(
                _first_key(item, ("expires_at", "expiresAt", "expiry", "expiration",
                                  "expires", "valid_until", "validUntil", "expire_at"))
            ),
            scope=str(
                _first_key(item, ("scope", "reset_scope", "resetScope", "window",
                                  "applies_to", "kind", "type", "resetType")) or ""
            ),
            status=str(_first_key(item, ("status", "state")) or ""),
            raw=item,
        )
        if credited.status.lower() in ("used", "consumed", "redeemed", "expired"):
            continue
        if credited.expires_at is not None and credited.expires_at <= now:
            continue
        if credited.credit_id and any(c.credit_id == credited.credit_id for c in credits):
            continue
        credits.append(credited)
    credits.sort(key=lambda c: (c.expires_at is None, c.expires_at or 0))
    return credits


def extract_plan_from_usage(meta: Dict[str, Any]) -> str:
    for key in ("plan_type", "planType"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


# --------------------------------------------------------------------------- #
# 本地会话日志回退
# --------------------------------------------------------------------------- #
def _read_tail_lines(path: Path, max_bytes: int = 262144) -> List[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # 丢弃半行
            data = fh.read()
        return data.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return []


def newest_session_files(limit: int = 40) -> List[Path]:
    root = codex_home() / "sessions"
    if not root.is_dir():
        return []
    files: List[Tuple[float, Path]] = []
    try:
        for path in root.rglob("rollout-*.jsonl"):
            try:
                files.append((path.stat().st_mtime, path))
            except Exception:
                continue
    except Exception as exc:
        log(f"扫描会话目录失败: {exc!r}")
        return []
    files.sort(key=lambda item: item[0], reverse=True)
    return [p for _, p in files[:limit]]


def latest_local_usage() -> Optional[Tuple[Dict[str, Any], str, float]]:
    """返回最近一条本地 rate_limits 快照：(rate_limits, 时间戳ISO, mtime)。"""
    latest = None
    latest_time = -1.0
    for path in newest_session_files(limit=25):
        lines = _read_tail_lines(path)
        for line in reversed(lines):
            line = line.strip()
            if not line or "rate_limits" not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            payload = obj.get("payload") if isinstance(obj, dict) else None
            if not isinstance(payload, dict):
                continue
            rate = payload.get("rate_limits")
            if isinstance(rate, dict) and parse_usage({"rate_limits": rate})[0]:
                ts = str(obj.get("timestamp") or "")
                try:
                    mtime = path.stat().st_mtime
                except Exception:
                    mtime = 0.0
                event_time = parse_ts(ts) or mtime
                if event_time > latest_time:
                    latest = (rate, ts, mtime)
                    latest_time = event_time
                break
    return latest


def local_usage_snapshot() -> Tuple[List[QuotaWindow], Dict[str, Any], str]:
    """把本地快照包装成与接口一致的结构。"""
    found = latest_local_usage()
    if not found:
        return [], {}, ""
    rate, ts, _mtime = found
    windows, meta = parse_usage({"rate_limits": rate}, source="local")
    credits_meta = rate.get("credits") if isinstance(rate.get("credits"), dict) else {}
    meta["has_credits"] = credits_meta.get("has_credits")
    meta["unlimited"] = credits_meta.get("unlimited")
    meta["balance"] = credits_meta.get("balance")
    if rate.get("plan_type"):
        meta["plan_type"] = rate.get("plan_type")
    return windows, meta, ts


# --------------------------------------------------------------------------- #
# 本地历史采样（用于趋势分析）
# --------------------------------------------------------------------------- #
@dataclass
class RateSample:
    ts: int
    limit_id: str
    slot: str
    window_minutes: int
    used_percent: float
    resets_at: Optional[int]


def collect_history(days: float = 14.0, max_files: int = 60) -> List[RateSample]:
    """扫描会话日志，取出所有 rate_limits 采样点（按时间升序）。"""
    cutoff = time.time() - days * 86400.0
    samples: List[RateSample] = []
    for path in newest_session_files(limit=max_files):
        try:
            if path.stat().st_mtime < cutoff - 86400:
                continue
        except Exception:
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    payload = obj.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    rate = payload.get("rate_limits")
                    if not isinstance(rate, dict):
                        continue
                    ts = parse_ts(obj.get("timestamp"))
                    if ts is None or ts < cutoff:
                        continue
                    for window in parse_usage({"rate_limits": rate})[0]:
                        samples.append(RateSample(ts=ts, limit_id=window.limit_id,
                                                  slot=window.slot,
                                                  window_minutes=window.window_minutes,
                                                  used_percent=window.used_percent,
                                                  resets_at=window.resets_at))
        except Exception as exc:
            log(f"读取会话文件失败 {path}: {exc!r}")
    unique = {(s.ts, s.limit_id, s.slot, s.window_minutes, s.used_percent, s.resets_at): s
              for s in samples}
    return sorted(unique.values(), key=lambda s: s.ts)


def find_reset_events(samples: Sequence[RateSample]) -> List[Tuple[int, str, float, int]]:
    """从采样里找出"窗口被重置"的时刻（额度大幅回落）。

    返回 [(ts, kind, 之前百分比, window_minutes)]。
    """
    events: List[Tuple[int, str, float, int]] = []
    last_by_key: Dict[Tuple[str, str, int], RateSample] = {}
    for sample in samples:
        key = (sample.limit_id, sample.slot, sample.window_minutes)
        prev = last_by_key.get(key)
        if prev is not None:
            dropped = prev.used_percent - sample.used_percent
            reset_moved = (
                sample.resets_at is not None
                and prev.resets_at is not None
                and sample.resets_at > prev.resets_at + 60
            )
            if dropped >= 25.0 or (reset_moved and dropped >= 5.0):
                events.append(
                    (sample.ts, classify_window(sample.window_minutes),
                     prev.used_percent, sample.window_minutes)
                )
        last_by_key[key] = sample
    return events


# --------------------------------------------------------------------------- #
# 消耗重置卡（带幂等保护）
# --------------------------------------------------------------------------- #
def _pending_record_path(account_key: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_@." else "_" for ch in account_key)
    return pending_dir() / f"pending-redeem.{safe or 'default'}.json"


def begin_redeem(account_key: str, credit_id: str) -> Tuple[str, bool]:
    """返回 (redeem_request_id, 是否为上一次未完成请求的复用)。

    与 codex-reset 相同的幂等策略：请求 id 先落盘，成功/明确失败后再清除；
    24 小时内的同一张卡重试复用原 id，避免服务端重复扣卡。
    """
    path = _pending_record_path(account_key)
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(data, dict)
                and data.get("credit_id") == credit_id
                and time.time() - float(data.get("ts") or 0) < 24 * 3600
                and data.get("redeem_request_id")
            ):
                return str(data["redeem_request_id"]), True
    except Exception:
        pass
    rid = str(uuid.uuid4())
    try:
        path.write_text(
            json.dumps(
                {"credit_id": credit_id, "redeem_request_id": rid, "ts": time.time()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        log(f"写入幂等记录失败: {exc!r}")
    return rid, False


def end_redeem(account_key: str, resolved: bool) -> None:
    if not resolved:
        return
    path = _pending_record_path(account_key)
    try:
        if path.is_file():
            path.unlink()
    except Exception:
        pass
