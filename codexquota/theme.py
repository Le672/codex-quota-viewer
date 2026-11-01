"""亚克力（Acrylic）主题与 Win32 视觉效果。

包含：
  * 系统深浅色 / 强调色探测（注册表）
  * SetWindowCompositionAttribute 亚克力模糊（Win10 1803+ / Win11）
  * DwmSetWindowAttribute 圆角 + 系统背景材质（Win11）
  * 无边框窗口的拖动 / 四边缩放命中测试
  * 托盘图标绘制（纯 ctypes，无需 Pillow）

所有 Win32 调用都做了失败降级：任何一步失败都只是"不够好看"，不会崩溃。
"""

from __future__ import annotations

import ctypes
import math
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .appenv import log

IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------------- #
# Win32 常量和函数原型
# --------------------------------------------------------------------------- #
WCA_ACCENT_POLICY = 19
ACCENT_DISABLED = 0
ACCENT_ENABLE_BLURBEHIND = 3
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
ACCENT_ENABLE_HOSTBACKDROP = 5

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWA_BORDER_COLOR = 34

DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3

DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2       # Mica
DWMSBT_TRANSIENTWINDOW = 3  # Acrylic
DWMSBT_TABBEDWINDOW = 4

GWL_EXSTYLE = -20
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080

WM_NCHITTEST = 0x0084
HTCLIENT = 1
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

RESIZE_BORDER = 6


class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_uint),
        ("AccentFlags", ctypes.c_uint),
        ("GradientColor", ctypes.c_uint),  # ABGR
        ("AnimationId", ctypes.c_uint),
    ]


class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_uint),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


class MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


_user32 = ctypes.windll.user32 if IS_WINDOWS else None
_dwmapi = ctypes.windll.dwmapi if IS_WINDOWS else None
_gdi32 = ctypes.windll.gdi32 if IS_WINDOWS else None
_shell32 = ctypes.windll.shell32 if IS_WINDOWS else None
_kernel32 = ctypes.windll.kernel32 if IS_WINDOWS else None

_set_window_composition = None
if IS_WINDOWS:
    try:
        _set_window_composition = _user32.SetWindowCompositionAttribute
        _set_window_composition.restype = wintypes.BOOL
        _set_window_composition.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(WINDOWCOMPOSITIONATTRIBDATA),
        ]
    except Exception:  # Win7 等老系统
        _set_window_composition = None


# --------------------------------------------------------------------------- #
# 调色板
# --------------------------------------------------------------------------- #
@dataclass
class Palette:
    name: str = "dark"
    # 亚克力渐变底色（ABGR，Win 把 0xAABBGGRR 解释成 tint）
    acrylic_tint: Tuple[int, int, int] = (0x22, 0x1E, 0x1B)     # RGB
    tint_alpha: int = 0xC8
    # 窗口/卡片
    window_bg: str = "#14161A"
    card_bg: str = "#22262E"
    card_bg_soft: str = "#1B1F26"
    card_border: str = "#333A45"
    text: str = "#F2F5F9"
    text_dim: str = "#9AA4B2"
    text_faint: str = "#6B7482"
    accent: str = "#4C9AFF"
    accent_soft: str = "#2B4A77"
    ok: str = "#3DD68C"
    warn: str = "#FFB454"
    danger: str = "#FF6B6B"
    track: str = "#2C323C"
    ring_track: str = "#2A303A"
    button: str = "#2A313B"
    button_hover: str = "#39424F"
    button_active: str = "#46505F"
    divider: str = "#2B313B"

    def as_dict(self) -> Dict[str, str]:
        return {
            k: v for k, v in self.__dict__.items() if isinstance(v, str)
        }


def _dark_palette(accent: Optional[str] = None) -> Palette:
    p = Palette(name="dark")
    if accent:
        p.accent = accent
    p.button_active = "#4A5464"
    return p


def _light_palette(accent: Optional[str] = None) -> Palette:
    p = Palette(
        name="light",
        acrylic_tint=(0xF2, 0xF4, 0xF8),
        tint_alpha=0xC0,
        window_bg="#F4F6FA",
        card_bg="#FFFFFF",
        card_bg_soft="#F7F9FC",
        card_border="#DCE1E9",
        text="#16181D",
        text_dim="#5C6672",
        text_faint="#8A94A2",
        accent="#0169CC",
        accent_soft="#CFE3FA",
        ok="#12A150",
        warn="#C77700",
        danger="#D93A3A",
        track="#E4E8EF",
        ring_track="#E2E7EF",
        button="#EDF1F7",
        button_hover="#E1E8F2",
        button_active="#D3DCEA",
        divider="#E4E8EF",
    )
    if accent:
        p.accent = accent
    return p


def _read_reg(path: str, name: str):
    if not IS_WINDOWS:
        return None
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except Exception:
        return None


def system_prefers_dark() -> bool:
    v = _read_reg(
        r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        "AppsUseLightTheme",
    )
    if v is None:
        return True
    try:
        return int(v) == 0
    except Exception:
        return True


def system_accent_color() -> Optional[str]:
    v = _read_reg(r"Software\Microsoft\Windows\DWM", "AccentColor")
    if v is None:
        v = _read_reg(r"Software\Microsoft\Windows\DWM", "ColorizationColor")
    if v is None:
        return None
    try:
        n = int(v) & 0xFFFFFFFF
        # 注册表里是 ABGR
        r = n & 0xFF
        g = (n >> 8) & 0xFF
        b = (n >> 16) & 0xFF
        if r + g + b < 60:  # 太暗的强调色在深色底上看不清
            return None
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        return None


def resolve_palette(theme: str) -> Palette:
    if theme == "dark":
        return _dark_palette(system_accent_color())
    if theme == "light":
        return _light_palette(system_accent_color())
    accent = system_accent_color()
    return _dark_palette(accent) if system_prefers_dark() else _light_palette(accent)


# --------------------------------------------------------------------------- #
# 亚克力 / 材质
# --------------------------------------------------------------------------- #
def _abgr(rgb: Tuple[int, int, int], alpha: int) -> int:
    r, g, b = rgb
    return ((alpha & 0xFF) << 24) | ((b & 0xFF) << 16) | ((g & 0xFF) << 8) | (r & 0xFF)


@dataclass
class EffectsReport:
    acrylic: bool = False
    mica: bool = False
    rounded: bool = False
    dark_titlebar: bool = False
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.acrylic:
            parts.append("acrylic")
        if self.mica:
            parts.append("mica")
        if self.rounded:
            parts.append("rounded")
        if self.dark_titlebar:
            parts.append("dark-titlebar")
        return "+".join(parts) or "none"


def windows_build() -> int:
    try:
        return int(getattr(sys, "getwindowsversion")().build)
    except Exception:
        return 0


def apply_acrylic(
    hwnd: int,
    palette: Palette,
    opacity: float = 0.72,
    *,
    prefer_mica: bool = False,
) -> EffectsReport:
    """给窗口套上亚克力效果，返回实际生效的效果清单。"""
    report = EffectsReport()
    if not IS_WINDOWS or not hwnd:
        report.notes.append("非 Windows 或无窗口句柄")
        return report

    alpha = int(max(0.35, min(0.95, opacity)) * 255)

    # Win11 22H2+：优先用系统背景材质（真正的 Acrylic / Mica）
    if windows_build() >= 22621 and _dwmapi is not None:
        try:
            backdrop = DWMSBT_MAINWINDOW if prefer_mica else DWMSBT_TRANSIENTWINDOW
            val = ctypes.c_int(backdrop)
            hr = _dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(DWMWA_SYSTEMBACKDROP_TYPE),
                ctypes.byref(val),
                ctypes.sizeof(val),
            )
            if hr == 0:
                if prefer_mica:
                    report.mica = True
                else:
                    report.acrylic = True
        except Exception as exc:
            report.notes.append(f"系统背景材质失败: {exc!r}")

    # Win10 1803+ / 兜底：SetWindowCompositionAttribute
    if not report.acrylic and not report.mica and _set_window_composition is not None:
        for state in (ACCENT_ENABLE_ACRYLICBLURBEHIND, ACCENT_ENABLE_BLURBEHIND):
            try:
                policy = ACCENT_POLICY()
                policy.AccentState = state
                policy.AccentFlags = 0x20 | 0x40 | 0x80 | 0x100
                policy.GradientColor = _abgr(palette.acrylic_tint, alpha)
                data = WINDOWCOMPOSITIONATTRIBDATA()
                data.Attribute = WCA_ACCENT_POLICY
                data.Data = ctypes.cast(ctypes.byref(policy), ctypes.c_void_p)
                data.SizeOfData = ctypes.sizeof(policy)
                ok = _set_window_composition(wintypes.HWND(hwnd), ctypes.byref(data))
                if ok:
                    report.acrylic = True
                    break
            except Exception as exc:
                report.notes.append(f"亚克力失败: {exc!r}")
                break
    if not report.acrylic and not report.mica:
        report.notes.append("系统不支持亚克力，已使用纯色回退")

    # 圆角（Win11）
    if windows_build() >= 22000 and _dwmapi is not None:
        try:
            pref = ctypes.c_int(DWMWCP_ROUND)
            hr = _dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
                ctypes.byref(pref),
                ctypes.sizeof(pref),
            )
            if hr == 0:
                report.rounded = True
        except Exception as exc:
            report.notes.append(f"圆角失败: {exc!r}")

    # 深色标题栏 / 边框（我们自绘标题栏，这里主要影响投影与边框）
    if _dwmapi is not None:
        try:
            dark = ctypes.c_int(1 if palette.name == "dark" else 0)
            hr = _dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(DWMWA_USE_IMMERSIVE_DARK_MODE),
                ctypes.byref(dark),
                ctypes.sizeof(dark),
            )
            if hr == 0:
                report.dark_titlebar = True
        except Exception:
            pass
    return report


def extend_frame_into_client(hwnd: int) -> bool:
    """让 DWM 阴影/圆角在无边框窗口上生效。"""
    if not IS_WINDOWS or _dwmapi is None or not hwnd:
        return False
    try:
        margins = MARGINS(1, 1, 1, 1)
        _dwmapi.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(margins))
        return True
    except Exception:
        return False


def set_tool_window(hwnd: int, enabled: bool) -> None:
    """切换 WS_EX_TOOLWINDOW（隐藏/显示任务栏按钮）。"""
    if not IS_WINDOWS or not hwnd:
        return
    try:
        style = _user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
        if enabled:
            style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        else:
            style = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
        _user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, style)
    except Exception:
        pass


def install_resize_hit_test(tk_window, border: int = RESIZE_BORDER) -> None:
    """给无边框 Tk 窗口加上四边/四角缩放与拖动命中。

    仅覆盖 Tk 默认的 WM_NCHITTEST 返回值即可，无需子类化。
    """
    if not IS_WINDOWS:
        return
    try:
        from ctypes import wintypes as wt

        hwnd = int(tk_window.winfo_id())
        # Tk 8.6 的 toplevel 需要拿到真正的顶层 HWND
        parent = _user32.GetParent(wt.HWND(hwnd))
        if parent:
            hwnd = parent
        _user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        _user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
        GWLP_WNDPROC = -4
        old_proc = _user32.GetWindowLongPtrW(wt.HWND(hwnd), GWLP_WNDPROC)

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM
        )

        def _handler(h, msg, wparam, lparam):
            if msg == WM_NCHITTEST:
                try:
                    x = ctypes.c_short(lparam & 0xFFFF).value
                    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    rect = wt.RECT()
                    _user32.GetWindowRect(wt.HWND(h), ctypes.byref(rect))
                    left = x - rect.left
                    top = y - rect.top
                    right = rect.right - x
                    bottom = rect.bottom - y
                    b = border
                    if left < b and top < b:
                        return HTTOPLEFT
                    if right < b and top < b:
                        return HTTOPRIGHT
                    if left < b and bottom < b:
                        return HTBOTTOMLEFT
                    if right < b and bottom < b:
                        return HTBOTTOMRIGHT
                    if left < b:
                        return HTLEFT
                    if right < b:
                        return HTRIGHT
                    if top < b:
                        return HTTOP
                    if bottom < b:
                        return HTBOTTOM
                except Exception:
                    return HTCLIENT
                return HTCLIENT
            return _user32.CallWindowProcW(
                ctypes.c_void_p(old_proc), h, msg, wparam, lparam
            )

        handler = WNDPROC(_handler)
        _user32.SetWindowLongPtrW(
            wt.HWND(hwnd), GWLP_WNDPROC, ctypes.cast(handler, ctypes.c_void_p)
        )
        # 保存引用，避免被 GC
        tk_window._codexquota_wndproc = handler  # type: ignore[attr-defined]
    except Exception as exc:
        log(f"安装缩放命中测试失败: {exc!r}")


def get_dpi_scale(tk_window) -> float:
    try:
        return float(tk_window.winfo_fpixels("1i")) / 96.0
    except Exception:
        return 1.0


def set_process_dpi_aware() -> None:
    """尽量开启 Per-Monitor DPI 感知，避免亚克力窗口发虚。"""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 托盘图标（ctypes 直接画，不依赖 Pillow）
# --------------------------------------------------------------------------- #
class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _create_dib(rgba: List[List[Tuple[int, int, int, int]]], size: int):
    """用 32 位 BGRA DIB 生成 HICON。rgba 为 [row][col] = (r,g,b,a)。"""
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    hdc = _user32.GetDC(None)
    try:
        bits = ctypes.c_void_p()
        hbm_color = _gdi32.CreateDIBSection(
            hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0
        )
        if not hbm_color:
            return None, None
        buf = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
        for y in range(size):
            for x in range(size):
                r, g, b, a = rgba[y][x]
                i = (y * size + x) * 4
                buf[i + 0] = b
                buf[i + 1] = g
                buf[i + 2] = r
                buf[i + 3] = a
        hbm_mask = _gdi32.CreateBitmap(size, size, 1, 1, None)
        return hbm_color, hbm_mask
    except Exception as exc:
        log(f"创建 DIB 失败: {exc!r}")
        return None, None
    finally:
        _user32.ReleaseDC(None, hdc)


def _draw_icon_pixels(size: int, palette: Palette, percent: float) -> List[List[Tuple[int, int, int, int]]]:
    """画一个圆角方块 + 进度弧的图标。"""
    accent = _hex_to_rgb(palette.accent)
    base = _hex_to_rgb(palette.card_bg)
    ring = _hex_to_rgb(palette.ring_track)
    danger = _hex_to_rgb(palette.danger)
    if percent >= 90:
        accent = danger

    cx = cy = (size - 1) / 2.0
    outer = size * 0.46
    inner = size * 0.30
    rows: List[List[Tuple[int, int, int, int]]] = []
    sweep = max(0.0, min(100.0, percent)) / 100.0 * 360.0
    for y in range(size):
        row: List[Tuple[int, int, int, int]] = []
        for x in range(size):
            dx, dy = x - cx, y - cy
            dist = (dx * dx + dy * dy) ** 0.5
            if dist > outer:
                row.append((0, 0, 0, 0))
                continue
            if dist < inner:
                # 中心：底色
                row.append((base[0], base[1], base[2], 255))
                continue
            # 角度：从 12 点方向顺时针
            ang = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
            color = accent if ang <= sweep else ring
            # 边缘抗锯齿
            alpha = 255
            if dist > outer - 1.0:
                alpha = int(255 * max(0.0, (outer - dist)))
            row.append((color[0], color[1], color[2], max(0, min(255, alpha))))
        rows.append(row)
    return rows


def _hex_to_rgb(value: str) -> Tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    try:
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except Exception:
        return (90, 160, 255)


def make_tray_hicon(size: int, palette: Palette, percent: float = 35.0) -> Optional[int]:
    """生成 HICON；失败返回 None（调用方需回退默认图标）。"""
    if not IS_WINDOWS:
        return None
    hbm_color = hbm_mask = None
    try:
        pixels = _draw_icon_pixels(size, palette, percent)
        hbm_color, hbm_mask = _create_dib(pixels, size)
        if not hbm_color or not hbm_mask:
            return None
        info = ICONINFO()
        info.fIcon = True
        info.xHotspot = 0
        info.yHotspot = 0
        info.hbmMask = hbm_mask
        info.hbmColor = hbm_color
        hicon = _user32.CreateIconIndirect(ctypes.byref(info))
        return int(hicon) if hicon else None
    except Exception as exc:
        log(f"生成托盘图标失败: {exc!r}")
        return None
    finally:
        try:
            if hbm_color:
                _gdi32.DeleteObject(hbm_color)
            if hbm_mask:
                _gdi32.DeleteObject(hbm_mask)
        except Exception:
            pass


def destroy_icon(hicon: Optional[int]) -> None:
    if IS_WINDOWS and hicon:
        try:
            _user32.DestroyIcon(wintypes.HICON(hicon))
        except Exception:
            pass


def set_window_icon(hwnd: int, hicon: Optional[int]) -> None:
    if not IS_WINDOWS or not hwnd or not hicon:
        return
    try:
        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        _user32.SendMessageW(wintypes.HWND(hwnd), WM_SETICON, ICON_BIG, hicon)
        _user32.SendMessageW(wintypes.HWND(hwnd), WM_SETICON, ICON_SMALL, hicon)
    except Exception:
        pass


def flash_taskbar(hwnd: int) -> None:
    if not IS_WINDOWS or not hwnd:
        return
    try:
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwnd", wintypes.HWND),
                ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint),
            ]

        info = FLASHWINFO(
            ctypes.sizeof(FLASHWINFO), wintypes.HWND(hwnd), 0x00000003 | 0x0000000C, 3, 0
        )
        _user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass
