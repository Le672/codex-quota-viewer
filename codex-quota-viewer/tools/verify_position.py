"""使用隔离设置验证位置保存 / 恢复；可同时验证真实打包程序。"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / 'tools'))
from codexquota.appenv import APP_VERSION, Settings, settings_path
from codexquota.lifecycle import CodexExitSignal


def preferences(settings):
    return {key: value for key, value in asdict(settings).items() if key != 'geometry'}


def isolate(directory):
    os.environ['APPDATA'] = str(directory)
    os.environ['CODEX_HOME'] = str(directory / 'empty-codex')
    os.environ['CODEXQUOTA_INSTANCE_SCOPE'] = str(directory)


def source_checks():
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QSystemTrayIcon
    from codexquota.ui import QuotaApp
    checks, apps = [], []
    def check(condition, description):
        assert condition, description
        checks.append(description)
    class OfflineController:
        def __init__(self, settings, events):
            self.busy = False
        def refresh_async(self, reason):
            return False
    with tempfile.TemporaryDirectory(prefix='quota-position-source-') as temporary:
        isolate(Path(temporary))
        settings = Settings(compact=False, start_minimized=True, minimize_to_tray=True,
                            hide_taskbar=True, click_through=True, theme='light', material='transparent',
                            refresh_seconds=0, community_events=False, use_api=False,
                            local_fallback=False, notify_on_expiry=False)
        settings.save()
        try:
            with patch('codexquota.ui.DataController', OfflineController):
                app = QuotaApp(settings, start_minimized=False)
                apps.append(app)
                QTest.qWait(180)
                area = app.qt.primaryScreen().availableGeometry()
                target = (area.x() + 80, area.y() + 96)
                app.root.move(*target)
                QTest.qWait(320)
                check(Settings.load().geometry_tuple()[:2] == target, '移动后自动持久化实际坐标')
                for x_offset in (16, 32, 48):
                    app.root.move(target[0] + x_offset, target[1] + 32)
                target = (target[0] + 48, target[1] + 32)
                QTest.qWait(320)
                check(Settings.load().geometry_tuple()[:2] == target, '连续移动保留最后位置')
                app.toggle_compact()
                QTest.qWait(320)
                check(app.root.pos().toTuple() == target and Settings.load().geometry_tuple()[3] == 170,
                      'HUD 尺寸可保存，切换模式保留位置')
                app.toggle_topmost()
                app.toggle_taskbar()
                QTest.qWait(320)
                check(app.root.pos().toTuple() == target, '置顶和任务栏模式重建窗口后保留位置')
                target = (target[0] + 16, target[1] + 16)
                app.root.move(*target)
                if QSystemTrayIcon.isSystemTrayAvailable():
                    app.root.close()
                    check(not app.root.isVisible() and Settings.load().geometry_tuple()[:2] == target,
                          '关闭到托盘立即保存，无需等待移动定时器')
                    app.show_window()
                    QTest.qWait(100)
                    check(app.root.pos().toTuple() == target, '托盘恢复沿用原位置')
                before = preferences(app.settings)
                app.quit_app()
                check(Settings.load().geometry_tuple()[:2] == target, '退出查看器保存最后位置')
                app = QuotaApp(Settings.load(), start_minimized=False)
                apps.append(app)
                QTest.qWait(180)
                check(app.root.pos().toTuple() == target, '下次启动读取关闭位置，不再居中')
                check(preferences(app.settings) == before, '恢复位置不改变其余偏好')
                with_signal = CodexExitSignal(owner=True)
                try:
                    target = (target[0] + 32, target[1] + 32)
                    app.root.move(*target)
                    with_signal.set_absent(True)
                    app._tick()
                    check(app._closed and Settings.load().geometry_tuple()[:2] == target,
                          'Codex 退出信号路径同样立即保存位置')
                finally:
                    with_signal.close()
                app = QuotaApp(Settings.load())
                apps.append(app)
                QTest.qWait(180)
                check(app.root.pos().toTuple() == target, '托盘启动先恢复关闭位置')
                app.show_window()
                QTest.qWait(100)
                check(app.root.pos().toTuple() == target, '托盘启动后的首次显示不再居中')
                app.quit_app()
        finally:
            for app in apps:
                if not app._closed:
                    app.quit_app()
    return checks


def compiled_checks(executable):
    import psutil
    from native_position import NativePosition
    native, processes, checks = NativePosition(), [], []
    def check(condition, description):
        assert condition, description
        checks.append(description)
    def visible(process):
        try:
            pids = {process.pid} | {item.pid for item in psutil.Process(process.pid).children(recursive=True)}
        except psutil.NoSuchProcess:
            return None
        return native.find(pids, f'Codex 额度查看器 · v{APP_VERSION}')
    def launch():
        environment = os.environ.copy()
        environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        process = subprocess.Popen([str(executable), '--follow-codex'], env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        processes.append(process)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            hwnd = visible(process)
            if hwnd:
                return process, hwnd
            time.sleep(.1)
        raise AssertionError('隔离 EXE 窗口未显示')
    with tempfile.TemporaryDirectory(prefix='quota-position-exe-') as temporary:
        isolate(Path(temporary))
        area = native.qt.primaryScreen().availableGeometry()
        initial = (area.x() + 80, area.y() + 96)
        settings = Settings(compact=True, start_minimized=True, minimize_to_tray=False,
                            hide_taskbar=True, click_through=True, theme='light', material='transparent',
                            refresh_seconds=0, community_events=False, use_api=False,
                            local_fallback=False, notify_on_expiry=False,
                            geometry=f'{initial[0]},{initial[1]},390,170')
        settings.save()
        expected_preferences = preferences(Settings.load())
        owner = CodexExitSignal(owner=True)
        owner.set_absent(False)
        try:
            process, hwnd = launch()
            check(native.geometry(hwnd)[:2] == initial, '实际 EXE 首次显示恢复 HUD 保存位置')
            target = (initial[0] + 160, initial[1] + 128)
            native.move(hwnd, target)
            time.sleep(.6)
            check(Settings.load().geometry_tuple()[:2] == target, '实际原生窗口移动自动保存逻辑坐标')
            native.close(hwnd)
            check(process.wait(12) == 0, '真实 EXE 关闭后正常退出')
            check(Settings.load().geometry_tuple()[:2] == target, '关闭前保存实际移动位置')
            process, hwnd = launch()
            check(native.geometry(hwnd)[:2] == target, '打包程序重启后原位恢复')
            target = (target[0] + 32, target[1] + 32)
            native.move(hwnd, target)
            owner.set_absent(True)
            check(process.wait(12) == 0, '打包程序响应 Codex 退出信号并正常保存')
            check(Settings.load().geometry_tuple()[:2] == target, 'Codex 退出路径保留最后坐标')
            owner.set_absent(False)
            process, hwnd = launch()
            check(native.geometry(hwnd)[:2] == target, '下次联动启动恢复 Codex 关闭位置')
            native.close(hwnd)
            check(process.wait(12) == 0, '恢复后的程序再次正常退出')
            check(preferences(Settings.load()) == expected_preferences, '启停只更新位置，保留所有其他偏好')
        finally:
            owner.set_absent(True)
            for process in processes:
                if process.poll() is None:
                    try:
                        process.wait(5)
                    except subprocess.TimeoutExpired:
                        for child in psutil.Process(process.pid).children(recursive=True):
                            child.terminate()
                        process.terminate()
                        process.wait(5)
            owner.close()
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--compiled', type=Path)
    args = parser.parse_args()
    old = {key: os.environ.get(key) for key in ('APPDATA', 'CODEX_HOME', 'CODEXQUOTA_INSTANCE_SCOPE')}
    try:
        checks = compiled_checks(args.compiled) if args.compiled else source_checks()
        kind = 'position-compiled' if args.compiled else 'position-source'
        report = dict(ok=True, version=APP_VERSION, checks=checks)
        (root / f'v{APP_VERSION}-{kind}-validation.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=True))
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == '__main__':
    main()
