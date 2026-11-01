"""只查询或移动本程序的窗口，使用显示器 DPI 映射 Qt 逻辑坐标。"""
import ctypes
from ctypes import wintypes

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication


class MonitorInfo(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                ('work', wintypes.RECT), ('flags', wintypes.DWORD), ('device', wintypes.WCHAR * 32)]


class NativePosition:
    def __init__(self):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        self.qt = QApplication.instance() or QApplication([])
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user.MonitorFromWindow.restype = wintypes.HANDLE
        self.user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        self.user.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.user.GetDpiForWindow.restype = wintypes.UINT
        self.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]

    def find(self, pids, title, visible_only=True):
        result = []
        @self.callback_type
        def collect(hwnd, unused):
            pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and (not visible_only or self.user.IsWindowVisible(hwnd)):
                text = ctypes.create_unicode_buffer(256)
                self.user.GetWindowTextW(hwnd, text, len(text))
                if text.value == title:
                    result.append(int(hwnd))
            return True
        self.user.EnumWindows(collect, 0)
        return result[0] if result else None

    def mapping(self, hwnd):
        monitor = MonitorInfo()
        monitor.size = ctypes.sizeof(monitor)
        assert self.user.GetMonitorInfoW(self.user.MonitorFromWindow(hwnd, 2), ctypes.byref(monitor))
        screen = next((item for item in self.qt.screens() if item.name() == monitor.device),
                      self.qt.primaryScreen())
        dpi_scale = self.user.GetDpiForWindow(hwnd) / 96
        return monitor, screen, dpi_scale

    def geometry(self, hwnd):
        rect = wintypes.RECT()
        assert self.user.GetWindowRect(hwnd, ctypes.byref(rect))
        monitor, screen, scale = self.mapping(hwnd)
        area = screen.geometry()
        return (area.x() + round((rect.left - monitor.monitor.left) / scale),
                area.y() + round((rect.top - monitor.monitor.top) / scale),
                round((rect.right - rect.left) / scale), round((rect.bottom - rect.top) / scale))

    def move(self, hwnd, position):
        monitor, screen, scale = self.mapping(hwnd)
        x, y = position
        area = screen.geometry()
        physical_x = monitor.monitor.left + round((x - area.x()) * scale)
        physical_y = monitor.monitor.top + round((y - area.y()) * scale)
        assert self.user.SetWindowPos(hwnd, None, physical_x, physical_y, 0, 0, 0x15)

    def close(self, hwnd):
        assert self.user.PostMessageW(hwnd, 0x10, 0, 0)
