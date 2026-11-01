"""跟随 Codex 桌面主进程的轻量后台联动，不导入 Qt 或读取账号。"""
from __future__ import annotations

import ctypes
import json
import ntpath
import os
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .appenv import APP_VERSION, app_data_dir, log
from .instance import SingleInstance, object_name, instance_exists

POLL_SECONDS = 1.0
EXIT_GRACE_SECONDS = 2.0
FOLLOW_TASK_NAME = 'CodexQuotaViewer-FollowCodex'
_TOOL_WINDOW_HOSTS: set[HostProcess] = set()


def ensure_registered_follower() -> bool:
    """手动启动可恢复计划任务；由服务创建进程，避免继承 Codex 的 Job。"""
    if os.name != 'nt' or os.environ.get('CODEXQUOTA_INSTANCE_SCOPE'):
        return False
    if not (app_data_dir() / 'codex-follow-task.json').is_file():
        return False
    if instance_exists('CodexFollower'):
        return True
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'schtasks.exe'
    try:
        completed = subprocess.run([str(executable), '/Run', '/TN', FOLLOW_TASK_NAME],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        log(f'手动启动恢复独立联动任务: {completed.returncode}')
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f'恢复独立联动任务失败: {type(exc).__name__}')
        return False


def is_codex_desktop_path(executable: str) -> bool:
    """按包名和主程序位置识别，避开 ChatGPT、CLI 和 sandbox service。"""
    parts = ntpath.normpath(executable).casefold().split("\\")
    if len(parts) < 4 or parts[-1] not in ("chatgpt.exe", "codex.exe"):
        return False
    return (parts[-4] == "windowsapps" and parts[-3].startswith("openai.codex_")
            and parts[-2] == "app")


def is_desktop_main(command_line: list[str]) -> bool:
    """Electron 渲染器、GPU、utility、crashpad 均带 --type 参数。"""
    if not command_line:
        return False
    return not any(arg.casefold() == "--type" or arg.casefold().startswith("--type=")
                   for arg in command_line[1:])


@dataclass(frozen=True, order=True)
class HostProcess:
    pid: int
    created: float


def window_is_open(rows: list[dict], prefer_tool: bool) -> bool:
    """工具主窗口关闭后，最小化的任务栏代理窗口不能维持联动。"""
    return any(row['visible'] and (row['tool'] or not prefer_tool) for row in rows)


def open_desktop_pids(hosts: tuple[HostProcess, ...]) -> set[int] | None:
    if os.name != 'nt':
        return None
    if not hosts:
        _TOOL_WINDOW_HOSTS.clear()
        return set()
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetWindow.restype = wintypes.HWND
    user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    frames = {host.pid: [] for host in hosts}
    @callback_type
    def collect(hwnd, unused):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in frames:
            return True
        name = ctypes.create_unicode_buffer(128)
        user.GetClassNameW(hwnd, name, len(name))
        if name.value == 'Chrome_WidgetWin_1' and not user.GetWindow(hwnd, 4):
            frames[pid.value].append(dict(visible=bool(user.IsWindowVisible(hwnd)),
                                          tool=bool(user.GetWindowLongPtrW(hwnd, -20) & 0x80)))
        return True
    if not user.EnumWindows(collect, 0):
        return None
    _TOOL_WINDOW_HOSTS.intersection_update(hosts)
    opened = set()
    for host in hosts:
        rows = frames[host.pid]
        if any(row['tool'] for row in rows):
            _TOOL_WINDOW_HOSTS.add(host)
        if window_is_open(rows, host in _TOOL_WINDOW_HOSTS):
            opened.add(host.pid)
    return opened


def find_codex_desktops() -> tuple[HostProcess, ...] | None:
    """只读取候选桌面程序的路径、启动角色和创建时间；失败时保留当前状态。"""
    import psutil

    hosts = []
    uncertain = False
    try:
        for process in psutil.process_iter(attrs=["name"]):
            if str(process.info.get("name", "")).casefold() not in ("chatgpt.exe", "codex.exe"):
                continue
            try:
                if not is_codex_desktop_path(process.exe()):
                    continue
                command_line = process.cmdline()
                if not command_line:
                    uncertain = True
                    continue
                if not is_desktop_main(command_line):
                    continue
                if process.is_running():
                    hosts.append(HostProcess(process.pid, process.create_time()))
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError):
                uncertain = True
    except (psutil.Error, OSError):
        return None
    # 已确认至少一个桌面主进程时可启动；无法确认退出时不误关查看器。
    candidates = tuple(sorted(hosts))
    opened = open_desktop_pids(candidates)
    if candidates:
        return tuple(host for host in candidates if opened is None or host.pid in opened)
    return () if not uncertain else None


class FollowState:
    """只在运行 / 退出转换时操作；用户主动退出查看器后本次 Codex 会话不重开。"""
    def __init__(self, grace: float = EXIT_GRACE_SECONDS):
        self.grace = grace
        self.active = False
        self.initialized = False
        self.absent_since: float | None = None

    def step(self, hosts: tuple[HostProcess, ...] | None, now: float) -> str | None:
        if hosts is None:
            self.absent_since = None
            return None
        first = not self.initialized
        self.initialized = True
        if hosts:
            self.absent_since = None
            if not self.active:
                self.active = True
                return "start"
        elif not self.active:
            if first:
                return "stop"
        else:
            if self.absent_since is None:
                self.absent_since = now
            if now - self.absent_since >= self.grace:
                self.active = False
                self.absent_since = None
                return "stop"
        return None


class CodexExitSignal:
    """Windows 命名事件：联动器发信号，Qt 主线程自行退出并保存偏好。"""
    def __init__(self, owner=False):
        self.handle = None
        self.kernel = None
        self.owner = owner
        if os.name == "nt":
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel.CreateEventW.restype = wintypes.HANDLE
            self.kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel.OpenEventW.restype = wintypes.HANDLE
            for name in ("SetEvent", "ResetEvent", "CloseHandle"):
                function = getattr(self.kernel, name)
                function.argtypes = [wintypes.HANDLE]
                function.restype = wintypes.BOOL
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            if owner:
                self.handle = self.kernel.CreateEventW(None, True, False, object_name("CodexExit"))
                if not self.handle:
                    raise ctypes.WinError(ctypes.get_last_error())

    def set_absent(self, absent: bool):
        if self.kernel and self.handle and self.owner:
            function = self.kernel.SetEvent if absent else self.kernel.ResetEvent
            if not function(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())

    def requested(self) -> bool:
        if self.kernel and not self.handle:
            self.handle = self.kernel.OpenEventW(0x00100000, False, object_name("CodexExit"))
        return bool(self.kernel and self.handle and self.kernel.WaitForSingleObject(self.handle, 0) == 0)

    def close(self):
        if self.kernel and self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def viewer_command() -> list[str]:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        if executable.name.casefold() == "codex额度联动.exe":
            executable = executable.with_name("Codex额度查看器.exe")
        return [str(executable), "--follow-codex"]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "run.py"), "--follow-codex"]


def launch_viewer():
    environment = os.environ.copy()
    if getattr(sys, "frozen", False):
        # onefile 的联动器长期存在；窗口使用独立解压目录并清理自己的运行库。
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return subprocess.Popen(viewer_command(), env=environment,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


class CodexFollower:
    def __init__(self, detector: Callable = find_codex_desktops, launcher: Callable = launch_viewer,
                 signal=None, status_path: Path | None = None, clock: Callable = time.monotonic):
        self.detector, self.launcher, self.clock = detector, launcher, clock
        self.signal = signal if signal is not None else CodexExitSignal(owner=True)
        self.status_path = status_path if status_path is not None else app_data_dir() / "codex-follow.json"
        self.state = FollowState()
        self.retry_at: float | None = None
        self.retry_count = 0
        self.started_at = time.time()
        self.last_hosts = None
        self.viewer_process = None
        self.last_status_at = 0.0

    def _status(self, hosts):
        payload = dict(version=APP_VERSION, watcher_pid=os.getpid(), started_at=self.started_at,
                       updated_at=time.time(),
                       state="running" if self.state.active else "waiting",
                       desktop_pids=[host.pid for host in hosts] if hosts else [],
                       poll_seconds=POLL_SECONDS, exit_grace_seconds=EXIT_GRACE_SECONDS)
        try:
            temporary = self.status_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.status_path)
        except OSError as exc:
            log(f"联动状态写入失败: {type(exc).__name__}")

    def _launch(self, now):
        try:
            self.viewer_process = self.launcher()
            self.retry_at = None
            self.retry_count = 0
            log("Codex 桌面已启动，打开额度查看器")
        except OSError as exc:
            self.retry_count += 1
            self.retry_at = now + 5 if self.retry_count < 3 else None
            log(f"联动启动失败: {type(exc).__name__}，尝试 {self.retry_count}/3")

    def tick(self):
        if self.viewer_process is not None and self.viewer_process.poll() is not None:
            self.viewer_process = None
        hosts, now = self.detector(), self.clock()
        action = self.state.step(hosts, now)
        if action == "start":
            self.signal.set_absent(False)
            self._launch(now)
        elif action == "stop":
            self.retry_at = None
            self.retry_count = 0
            self.signal.set_absent(True)
            log("Codex 桌面已退出，关闭额度查看器")
        elif self.retry_at is not None and now >= self.retry_at and hosts:
            self._launch(now)
        if action or hosts != self.last_hosts or now - self.last_status_at >= 30:
            self._status(hosts)
            self.last_status_at = now
        self.last_hosts = hosts
        return action

    def close(self):
        self.signal.close()


def watch_codex() -> int:
    if os.name != "nt":
        log("Codex 桌面联动目前仅支持 Windows")
        return 3
    guard = SingleInstance("CodexFollower", notify_existing=False)
    if not guard.acquire():
        return 0
    follower = None
    try:
        follower = CodexFollower()
        log("Codex 桌面联动已就绪")
        while True:
            follower.tick()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return 0
    finally:
        if follower is not None:
            follower.close()
        guard.release()
