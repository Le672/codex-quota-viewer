"""系统托盘常驻（纯 ctypes 实现，不依赖 pystray/pywin32）。

做法：注册一个 message-only 窗口接收 Shell_NotifyIcon 的回调消息，再弹出
原生右键菜单。窗口建在 Tk 主线程上，Tk 的事件循环本身就在派发线程消息，
所以不需要另起消息泵。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Tuple

from .appenv import APP_DISPLAY_NAME, log

IS_WINDOWS = os.name == "nt"

WM_APP = 0x8000
WM_TRAYICON = WM_APP + 20
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_TASKBARCREATED = 0

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIF_SHOWTIP = 0x00000080
NIF_GUID = 0x00000020

NOTIFYICON_VERSION_4 = 4

NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
MF_UNCHECKED = 0x00000000
MF_GRAYED = 0x00000001
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

WM_NULL = 0x0000

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


def _configure_win32() -> None:
    """显式声明指针大小，防止 64 位 Windows 的句柄被截成 32 位。"""
    if not IS_WINDOWS:
        return
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]
    user32.RegisterClassExW.restype = wintypes.WORD
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreatePopupMenu.argtypes = []
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
    user32.TrackPopupMenu.restype = ctypes.c_uint
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.DestroyMenu.restype = wintypes.BOOL
    ctypes.windll.shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
    ctypes.windll.shell32.Shell_NotifyIconW.restype = wintypes.BOOL


_configure_win32()


class TrayIcon:
    """托盘图标 + 原生菜单。"""

    _class_registered = False
    _class_name = "CodexQuotaViewerTrayWnd"

    def __init__(
        self,
        hicon: Optional[int],
        tooltip: str = APP_DISPLAY_NAME,
        *,
        on_show: Optional[Callable[[], None]] = None,
        on_refresh: Optional[Callable[[], None]] = None,
        on_toggle_tray: Optional[Callable[[], None]] = None,
        on_settings: Optional[Callable[[], None]] = None,
        on_exit: Optional[Callable[[], None]] = None,
    ):
        self.hicon = hicon
        self.tooltip = tooltip[:127]
        self.on_show = on_show
        self.on_refresh = on_refresh
        self.on_toggle_tray = on_toggle_tray
        self.on_settings = on_settings
        self.on_exit = on_exit
        self.hwnd: Optional[int] = None
        self.available = False
        self.minimize_to_tray = True
        self._instance = None
        self._wndproc = None
        self._menu_ids: Dict[int, Callable[[], None]] = {}
        self._checks: Dict[int, bool] = {}
        self._taskbar_msg = 0
        self._added = False
        self._destroyed = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def install(self) -> bool:
        if not IS_WINDOWS:
            log("托盘：非 Windows 平台，跳过")
            return False
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            shell32 = ctypes.windll.shell32

            user32.SetProcessDPIAware()
            self._instance = kernel32.GetModuleHandleW(None)

            self._taskbar_msg = user32.RegisterWindowMessageW("TaskbarCreated")

            self._wndproc = WNDPROC(self._handle_message)
            wc = WNDCLASSEX()
            wc.cbSize = ctypes.sizeof(WNDCLASSEX)
            wc.style = 0
            wc.lpfnWndProc = self._wndproc
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = self._instance
            wc.hIcon = self.hicon or 0
            wc.hCursor = 0
            wc.hbrBackground = 0
            wc.lpszMenuName = None
            wc.lpszClassName = self._class_name
            wc.hIconSm = self.hicon or 0

            atom = user32.RegisterClassExW(ctypes.byref(wc))
            if not atom and not TrayIcon._class_registered:
                err = kernel32.GetLastError()
                if err not in (1410,):  # ERROR_CLASS_ALREADY_EXISTS
                    log(f"托盘：注册窗口类失败 err={err}")
            TrayIcon._class_registered = True

            HWND_MESSAGE = wintypes.HWND(-3)
            hwnd = user32.CreateWindowExW(
                0,
                self._class_name,
                "CodexQuotaViewerTray",
                0,
                0,
                0,
                0,
                0,
                HWND_MESSAGE,
                0,
                self._instance,
                None,
            )
            if not hwnd:
                log(f"托盘：创建消息窗口失败 err={kernel32.GetLastError()}")
                return False
            self.hwnd = int(hwnd)

            data = self._build_data()
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
                log("托盘：Shell_NotifyIcon(NIM_ADD) 失败")
                return False
            self._added = True

            data.uVersion = NOTIFYICON_VERSION_4
            shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
            self.available = True
            log("托盘：已就绪")
            return True
        except Exception as exc:
            log(f"托盘：初始化异常 {exc!r}")
            return False

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        hwnd, self.hwnd = self.hwnd, None
        try:
            if self._added and hwnd:
                data = self._build_data_for(hwnd)
                ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
                self._added = False
        except Exception:
            pass
        try:
            if hwnd:
                ctypes.windll.user32.DestroyWindow(wintypes.HWND(hwnd))
        except Exception:
            pass
        self.available = False

    # ------------------------------------------------------------------ #
    # 通知
    # ------------------------------------------------------------------ #
    def _build_data(self) -> NOTIFYICONDATA:
        return self._build_data_for(self.hwnd or 0)

    def _build_data_for(self, hwnd: int) -> NOTIFYICONDATA:
        data = NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        data.hWnd = wintypes.HWND(hwnd)
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        data.uCallbackMessage = WM_TRAYICON
        data.hIcon = self.hicon or 0
        data.szTip = self.tooltip
        return data

    def update(self, tooltip: Optional[str] = None, hicon: Optional[int] = None) -> None:
        if not self.available or not self.hwnd:
            return
        if tooltip is not None:
            self.tooltip = tooltip[:127]
        if hicon:
            self.hicon = hicon
        try:
            data = self._build_data()
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        except Exception as exc:
            log(f"托盘：更新失败 {exc!r}")

    def notify(self, title: str, text: str, *, warning: bool = False) -> None:
        if not self.available or not self.hwnd:
            return
        try:
            data = self._build_data()
            data.uFlags = NIF_INFO | NIF_ICON | NIF_TIP | NIF_MESSAGE
            data.szInfoTitle = title[:63]
            data.szInfo = text[:255]
            data.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        except Exception as exc:
            log(f"托盘：气泡通知失败 {exc!r}")

    # ------------------------------------------------------------------ #
    # 菜单
    # ------------------------------------------------------------------ #
    def _menu_items(self) -> List[Tuple[str, Callable[[], None], bool, bool]]:
        """返回 [(文本, 回调, 是否勾选, 是否启用)]，空文本=分隔线。"""
        items: List[Tuple[str, Callable[[], None], bool, bool]] = [
            (f"显示{APP_DISPLAY_NAME}", self.on_show or (lambda: None), False, True),
            ("立即刷新", self.on_refresh or (lambda: None), False, True),
            ("", lambda: None, False, False),
            (
                "关闭时最小化到托盘",
                self.on_toggle_tray or (lambda: None),
                self.minimize_to_tray,
                True,
            ),
            ("设置…", self.on_settings or (lambda: None), False, True),
            ("", lambda: None, False, False),
            ("退出", self.on_exit or (lambda: None), False, True),
        ]
        return items

    def _show_menu(self) -> None:
        if not self.hwnd:
            return
        try:
            user32 = ctypes.windll.user32
            menu = user32.CreatePopupMenu()
            if not menu:
                return
            self._menu_ids = {}
            self._checks = {}
            cmd_id = 1000
            for text, callback, checked, enabled in self._menu_items():
                if not text:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                flags = MF_STRING
                if checked:
                    flags |= MF_CHECKED
                if not enabled:
                    flags |= MF_GRAYED
                user32.AppendMenuW(menu, flags, cmd_id, text)
                self._menu_ids[cmd_id] = callback
                self._checks[cmd_id] = checked
                cmd_id += 1

            user32.SetForegroundWindow(wintypes.HWND(self.hwnd))
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            choice = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                pt.x,
                pt.y,
                0,
                wintypes.HWND(self.hwnd),
                None,
            )
            user32.PostMessageW(wintypes.HWND(self.hwnd), WM_NULL, 0, 0)
            user32.DestroyMenu(menu)
            callback = self._menu_ids.get(int(choice))
            if callback:
                try:
                    callback()
                except Exception as exc:
                    log(f"托盘菜单回调异常: {exc!r}")
        except Exception as exc:
            log(f"托盘：弹出菜单失败 {exc!r}")

    # ------------------------------------------------------------------ #
    # 消息处理
    # ------------------------------------------------------------------ #
    def _handle_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAYICON:
                # NOTIFYICON_VERSION_4: LOWORD(lparam) 是事件；wparam 是坐标。
                event = int(lparam) & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, 0x0400, 0x0401):
                    if self.on_show:
                        self.on_show()
                elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                return 0
            if msg == WM_COMMAND:
                callback = self._menu_ids.get(int(wparam) & 0xFFFF)
                if callback:
                    callback()
                return 0
            if self._taskbar_msg and msg == self._taskbar_msg:
                # 资源管理器重启，重新添加托盘图标
                log("托盘：检测到任务栏重建，重新注册")
                self._added = False
                data = self._build_data()
                if ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
                    self._added = True
                    data.uVersion = NOTIFYICON_VERSION_4
                    ctypes.windll.shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
                return 0
            if msg == WM_DESTROY:
                self.destroy()
                return 0
        except Exception as exc:
            log(f"托盘消息处理异常: {exc!r}")
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
