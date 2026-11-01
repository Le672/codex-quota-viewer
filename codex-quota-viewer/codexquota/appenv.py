"""运行环境、路径、设置与日志。

只依赖 Python 标准库，便于 PyInstaller 单文件打包。
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

APP_NAME = "CodexQuotaViewer"
APP_DISPLAY_NAME = "Codex 额度查看器"
APP_VERSION = "1.10.0"
APP_ID = "codex-quota-viewer"

# Windows 下避免弹出「无控制台」相关的黑框（onefile 子进程会继承）
IS_FROZEN = bool(getattr(sys, "frozen", False))


# --------------------------------------------------------------------------- #
# 目录与路径
# --------------------------------------------------------------------------- #
def _executable_dir() -> Path:
    if IS_FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _shared_data_dir(base: Path | None) -> Path | None:
    """安装版使用用户目录，避开继承 Codex 包身份后的 AppData 重定向。"""
    profile = os.environ.get('USERPROFILE')
    if os.name != 'nt' or not profile or not base or os.environ.get('CODEXQUOTA_INSTANCE_SCOPE'):
        return None
    # APPDATA 被测试、便携运行或用户覆盖时，保留其显式选择。
    expected = Path(profile) / 'AppData' / 'Roaming'
    if os.path.normcase(os.path.abspath(base)) != os.path.normcase(os.path.abspath(expected)):
        return None
    directory = Path(profile) / '.codex-quota-viewer'
    return directory if (directory / 'storage.json').is_file() else None


def _migrate_legacy_data(legacy: Path, destination: Path) -> None:
    """独立任务首次打开共享目录时迁移自己的偏好；已有偏好不被覆盖。"""
    if (destination / 'settings.json').exists():
        return
    # 只迁移本程序的设置、事件缓存和日志，不复制账号或 Codex 凭据。
    for name in ('community-api-v1.json', 'community-reset.json', 'viewer.log', 'viewer.log.1'):
        source, target = legacy / name, destination / name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
    pending = legacy / 'pending'
    if pending.is_dir() and not (destination / 'pending').exists():
        shutil.copytree(pending, destination / 'pending')
    source = legacy / 'settings.json'
    if source.is_file():
        # 最后迁移偏好，使存在该文件意味着缓存迁移已经完成。
        shutil.copy2(source, destination / 'settings.json')


def app_data_dir() -> Path:
    """安装版共享用户目录；未安装及显式覆盖保留 APPDATA，失败退回临时目录。"""
    base: Optional[Path] = None
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata)
    elif os.name == "nt":
        up = os.environ.get("USERPROFILE")
        if up:
            base = Path(up) / "AppData" / "Roaming"
    else:
        base = Path.home() / ".config"
    legacy = (base / APP_NAME) if base else (Path(tempfile.gettempdir()) / APP_NAME)
    shared = _shared_data_dir(base)
    candidate = shared or legacy
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        if shared:
            _migrate_legacy_data(legacy, shared)
        return candidate
    except OSError:
        fallback = Path(tempfile.gettempdir()) / APP_NAME
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def log_path() -> Path:
    return app_data_dir() / "viewer.log"


def pending_dir() -> Path:
    d = app_data_dir() / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def codex_home() -> Path:
    """定位 Codex home（与官方 CLI / codex-auth 一致的解析顺序）。"""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        up = os.environ.get("USERPROFILE")
        if up:
            return Path(up) / ".codex"
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".codex"
    return Path.home() / ".codex"


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
_log_lock = threading.Lock()
_LOG_MAX_BYTES = 512 * 1024


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    if os.environ.get("CODEXQUOTA_DEBUG") or not IS_FROZEN:
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass
    try:
        with _log_lock:
            p = log_path()
            if p.exists() and p.stat().st_size > _LOG_MAX_BYTES:
                p.replace(p.with_suffix(".log.1"))
            with p.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    refresh_seconds: int = 180          # 自动刷新间隔（秒），0 = 关闭
    minimize_to_tray: bool = True       # 关闭窗口时收进托盘
    start_minimized: bool = False       # 启动即最小化到托盘
    always_on_top: bool = False
    hide_taskbar: bool = False          # 前台悬浮显示，但只保留系统托盘入口
    click_through: bool = True          # 主窗口只有按钮接收输入，其余区域穿透
    compact: bool = False               # 紧凑模式（只显示额度）
    theme: str = "dark"                 # auto | dark | light
    material: str = "transparent"       # transparent | glass | solid
    use_api: bool = True                # 优先使用官方接口
    local_fallback: bool = True         # 接口不可用时回退读取本地会话日志
    notify_on_expiry: bool = True       # 重置卡临期 / 额度告急时气泡提醒
    community_events: bool = True       # 拉取社区重置/发卡事件并预估下次时间
    community_ttl: int = 6              # 社区事件缓存小时数
    opacity: float = 0.62               # 玻璃底色浓度；不改变文字或整个窗口的透明度
    transparent_opacity: float = 0.0    # 透明模式背景不透明度，0～1；文字保持清晰
    custom_colors: dict = field(default_factory=dict)
    show_token_value: bool = True       # tokens 后显示 Standard API 美元等值
    pricing_model: str = 'auto'         # auto 按日志模型分别折算，或指定模型
    geometry: str = ""                  # "x,y,w,h"
    warn_percent: float = 85.0          # 额度告警阈值
    expiry_warn_days: float = 7.0       # 重置卡到期提醒阈值（天）

    @classmethod
    def load(cls) -> "Settings":
        st = cls()
        try:
            p = settings_path()
            if p.exists():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key, value in raw.items():
                        if key in cls.__dataclass_fields__:
                            try:
                                setattr(st, key, value)
                            except Exception:
                                pass
        except Exception as exc:  # 损坏的设置不应阻止启动
            log(f"读取设置失败，使用默认值: {exc!r}")
        st.clamp()
        return st

    def clamp(self) -> None:
        try:
            self.refresh_seconds = int(self.refresh_seconds)
        except Exception:
            self.refresh_seconds = 180
        if self.refresh_seconds and self.refresh_seconds < 30:
            self.refresh_seconds = 30
        if self.refresh_seconds > 3600:
            self.refresh_seconds = 3600
        for name, default, low, high in (("opacity", .62, .35, .95),
                                         ("transparent_opacity", 0., 0., 1.)):
            try:
                value = float(getattr(self, name))
                if not math.isfinite(value):
                    raise ValueError("non-finite")
                setattr(self, name, max(low, min(high, value)))
            except (TypeError, ValueError, OverflowError):
                setattr(self, name, default)
        roles = ("title", "text", "muted", "weekly", "five_hour", "accent", "warn", "danger")
        raw_colors = self.custom_colors if isinstance(self.custom_colors, dict) else {}
        self.custom_colors = {
            role: value.upper() for role, value in raw_colors.items()
            if role in roles and isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value)
        }
        if self.theme not in ("auto", "dark", "light"):
            self.theme = "auto"
        if self.material not in ("transparent", "glass", "solid"):
            self.material = "transparent"
        from .pricing import PRICES
        if not isinstance(self.pricing_model, str) or self.pricing_model not in ('auto', *PRICES):
            self.pricing_model = 'auto'
        for name, default, low, high in (("warn_percent", 85.0, 1, 100),
                                         ("expiry_warn_days", 7.0, 0, 90),
                                         ("community_ttl", 6, 1, 24)):
            try:
                value = float(getattr(self, name))
                if not math.isfinite(value):
                    raise ValueError("non-finite")
                setattr(self, name, max(low, min(high, value)))
            except (TypeError, ValueError, OverflowError):
                setattr(self, name, default)
        for name in ("minimize_to_tray", "start_minimized", "always_on_top", "hide_taskbar", "click_through", "compact",
                     "use_api", "local_fallback", "notify_on_expiry", "community_events", "show_token_value"):
            value = getattr(self, name)
            if isinstance(value, str):
                value = value.strip().lower() in ("true", "1", "yes")
            setattr(self, name, bool(value))
        if not isinstance(self.geometry, str):
            self.geometry = ""

    def save(self) -> None:
        try:
            self.clamp()
            destination = settings_path()
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, destination)
        except Exception as exc:
            log(f"保存设置失败: {exc!r}")

    def geometry_tuple(self) -> Optional[tuple]:
        txt = (self.geometry or "").strip()
        if not txt:
            return None
        try:
            parts = [int(float(v)) for v in txt.split(",")]
            if len(parts) != 4:
                return None
            x, y, w, h = parts
            # 主窗口只恢复坐标；HUD 高度为 170，不能按旧完整面板的高度拒绝。
            if w <= 0 or h <= 0 or any(not -2147483648 <= part <= 2147483647 for part in parts):
                return None
            return x, y, w, h
        except Exception:
            return None


def platform_summary() -> str:
    try:
        return (
            f"{platform.system()} {platform.release()} ({platform.version()}) "
            f"python {platform.python_version()} frozen={IS_FROZEN}"
        )
    except Exception:
        return "unknown"


def parse_ts(value: Any) -> Optional[int]:
    """把各种时间表示统一成 epoch 秒（秒/毫秒/ISO8601 字符串）。

    放在 appenv 里是为了让 providers / events 都能用，且不产生循环导入。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n <= 0:
            return None
        while n > 1e11:
            n /= 1000.0
        return int(n)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return parse_ts(int(text))
        try:
            import datetime as _dt

            iso = text.replace("Z", "+00:00")
            dt = _dt.datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return None
    return None
