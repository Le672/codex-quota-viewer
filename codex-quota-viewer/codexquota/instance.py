"""Windows 单实例：再次启动时唤回已有窗口，避免重复轮询账号。"""
from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes

from .appenv import app_data_dir


def object_name(purpose: str) -> str:
    """同一登录会话共享；隔离测试可使用独立范围，不触及正在运行的查看器。"""
    scope = os.environ.get("CODEXQUOTA_INSTANCE_SCOPE", "")
    suffix = "." + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16] if scope else ""
    return f"Local\\CodexQuotaViewer.{purpose}.v1{suffix}"


def instance_exists(purpose: str) -> bool:
    if os.name != 'nt':
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenMutexW(0x00100000, False, object_name(purpose))
    if not handle:
        return ctypes.get_last_error() == 5  # 存在但当前权限不能打开。
    kernel.CloseHandle(handle)
    return True


def consume_show_request() -> bool:
    path = app_data_dir() / "show-window.request"
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


class SingleInstance:
    def __init__(self, purpose="Singleton", notify_existing=True):
        self.handle = None
        self.kernel = None
        self.purpose = purpose
        self.notify_existing = notify_existing

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateMutexW(None, False, object_name(self.purpose))
        already_running = ctypes.get_last_error() == 183
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if already_running:
            try:
                if self.notify_existing:
                    (app_data_dir() / "show-window.request").touch()
            finally:
                self.release()
            return False
        if self.notify_existing:
            consume_show_request()
        return True

    def release(self):
        if self.handle and self.kernel:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
