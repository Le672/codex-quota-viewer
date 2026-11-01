"""真实 Win32 输入，验证跨进程穿透和按钮响应；只点击两套隔离测试窗口。"""
def main():
    import ctypes
    import json
    import os
    import subprocess
    import sys
    import tempfile
    import time
    from ctypes import wintypes
    from pathlib import Path
    from unittest.mock import patch
    from PySide6.QtCore import Qt, QTimer, QPoint
    from PySide6.QtWidgets import QApplication

    stage = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(stage))
    test_data = tempfile.TemporaryDirectory(prefix='quota-passthrough-')
    os.environ['APPDATA'] = test_data.name
    from codexquota.appenv import Settings, APP_VERSION
    from codexquota.ui import QuotaApp
    from codexquota.collector import Bundle
    from codexquota.providers import Snapshot, QuotaWindow

    class FakeController:
        def __init__(self, settings, events):
            self.busy, self.refreshes = False, 0
        def refresh_async(self, reason):
            self.refreshes += 1
            return False
        def load_history_async(self):
            pass

    with patch('codexquota.ui.DataController', FakeController):
        app = QuotaApp(Settings(refresh_seconds=0, always_on_top=True, hide_taskbar=True,
                                click_through=True, notify_on_expiry=False, transparent_opacity=.3))
    app.bundle = Bundle(snapshot=Snapshot(ok=True, source='api', windows=[
        QuotaWindow(kind='weekly', used_percent=40, resets_at=int(time.time())+300000),
        QuotaWindow(kind='5h', used_percent=20, resets_at=int(time.time())+5000)]))
    app._render()
    area = app.qt.primaryScreen().availableGeometry()
    app.root.move(area.x()+30, area.y()+60)
    geometry = app.root.geometry()
    receiver_file = Path(test_data.name)/'receiver.json'
    title = 'Quota input test receiver '+str(os.getpid())
    peer = subprocess.Popen([sys.executable, str(stage/'tests'/'click_receiver.py'), str(receiver_file), title,
                             str(geometry.x()-5), str(geometry.y()-5), '420', '510'],
                            creationflags=subprocess.CREATE_NO_WINDOW)

    user = ctypes.WinDLL('user32', use_last_error=True)
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    user.WindowFromPoint.argtypes = [wintypes.POINT]
    user.WindowFromPoint.restype = wintypes.HWND
    user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.GetSystemMetrics.argtypes = [ctypes.c_int]
    getter = user.GetWindowLongPtrW
    getter.argtypes = [wintypes.HWND, ctypes.c_int]
    getter.restype = ctypes.c_ssize_t
    previous_cursor = wintypes.POINT()
    user.GetCursorPos(ctypes.byref(previous_cursor))
    class Mouse(ctypes.Structure):
        _fields_ = [('dx',wintypes.LONG),('dy',wintypes.LONG),('data',wintypes.DWORD),
                    ('flags',wintypes.DWORD),('time',wintypes.DWORD),('extra',ctypes.c_size_t)]
    class Key(ctypes.Structure):
        _fields_ = [('vk',wintypes.WORD),('scan',wintypes.WORD),('flags',wintypes.DWORD),
                    ('time',wintypes.DWORD),('extra',ctypes.c_size_t)]
    class InputUnion(ctypes.Union):
        _fields_ = [('mouse',Mouse),('key',Key)]
    class Input(ctypes.Structure):
        _fields_ = [('type',wintypes.DWORD),('value',InputUnion)]
    user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    user.SendInput.restype = wintypes.UINT
    result = dict(ok=False, checks=[])
    def check(condition, description):
        assert condition, description
        result['checks'].append(description)
    def step(callback, delay=250):
        def run():
            try:
                callback()
            except Exception as error:
                result['error'] = repr(error)
                app.quit_app()
        QTimer.singleShot(delay, run)
    def point(widget, position=None):
        position = position or widget.rect().center()
        dpr = widget.devicePixelRatioF()
        native = wintypes.POINT(round(position.x()*dpr), round(position.y()*dpr))
        user.ClientToScreen(int(widget.window().winId()), ctypes.byref(native)) if widget.isWindow() else None
        if not widget.isWindow():
            origin = widget.mapTo(widget.window(), position)
            native = wintypes.POINT(round(origin.x()*dpr), round(origin.y()*dpr))
            user.ClientToScreen(int(widget.window().winId()), ctypes.byref(native))
        return native
    def inject(widget, position=None, wheel=False, right=False):
        native = point(widget, position)
        hit = user.WindowFromPoint(native)
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hit, ctypes.byref(pid))
        assert pid.value in (os.getpid(), peer.pid), '输入目标不是隔离测试窗口，停止发送'
        assert user.SetCursorPos(native.x, native.y), '无法定位测试鼠标'
        flags = [0x800] if wheel else [0x8, 0x10] if right else [0x2, 0x4]
        origin_x, origin_y = user.GetSystemMetrics(76), user.GetSystemMetrics(77)
        width, height = user.GetSystemMetrics(78), user.GetSystemMetrics(79)
        absolute_x = round((native.x-origin_x)*65535/(width-1))
        absolute_y = round((native.y-origin_y)*65535/(height-1))
        inputs = (Input*(len(flags)+1))()
        for index, flag in enumerate([0xC001, *flags]):
            inputs[index].type = 0
            inputs[index].value.mouse = Mouse(absolute_x if index == 0 else 0, absolute_y if index == 0 else 0,
                                              120 if wheel and index > 0 else 0, flag, 0, 0)
        assert user.SendInput(len(inputs), inputs, ctypes.sizeof(Input)) == len(inputs), 'Win32 输入未成功发送'
        result['last_input'] = dict(point=[native.x,native.y], target_pid=pid.value, input_size=ctypes.sizeof(Input))
        return hit
    def state():
        return json.loads(receiver_file.read_text(encoding='utf-8'))
    def start():
        if not receiver_file.exists() or not user.FindWindowW(None, title):
            step(start)
            return
        app.input_layer.sync()
        user.SetForegroundWindow(user.FindWindowW(None, title))
        root_style = getter(int(app.root.winId()), -20)
        layer_style = getter(int(app.input_layer.winId()), -20)
        check(bool(root_style & 0x20) and bool(root_style & 0x80000), '显示窗口使用 Windows layered + 鼠标穿透样式')
        check(not layer_style & 0x20 and bool(layer_style & 0x80), '按钮输入窗口可命中且不进入任务栏')
        result['peer_pid'] = peer.pid
        result['body_clicks_before'] = state()['clicks']
        check(inject(app.ring) == user.FindWindowW(None, title), '不透明额度数字处命中下方另一进程')
        step(body_clicked)
    def body_clicked():
        result['body_state'] = state()
        check(state()['clicks'] == result['body_clicks_before']+1, '下方进程实际收到额度图表处的鼠标点击')
        inject(app.prediction_card, QPoint(30, 25), wheel=True)
        step(wheel_received)
    def wheel_received():
        check(state()['wheel'] == 120, '下方进程实际收到正文区域的滚轮')
        result['right_before'] = state()['right_clicks']
        inject(app.account_label, right=True)
        step(right_received)
    def right_received():
        check(state()['right_clicks'] == result['right_before']+1, '账号文字区域右键穿透')
        result['peer_before_button'] = state()['clicks']
        check(inject(app.mode_button) == int(app.input_layer.winId()), '顶部按钮命中独立按钮输入区域')
        step(hud_clicked)
    def hud_clicked():
        check(app.settings.compact and app.root.height() == 170, '真实 Windows 点击顶部按钮切换 HUD')
        check(state()['clicks'] == result['peer_before_button'], '按钮操作没有点击下方软件')
        result['hud_before'] = state()['clicks']
        inject(app.hud_widgets['5h']['percent'])
        step(hud_body_clicked)
    def hud_body_clicked():
        check(state()['clicks'] == result['hud_before']+1, 'HUD 数字区域也穿透到另一进程')
        inject(app.mode_button)
        step(open_settings)
    def open_settings():
        check(not app.settings.compact, '真实点击恢复完整面板')
        inject(app.settings_button)
        step(settings_open)
    def settings_open():
        dialog = app._dialogs['settings']
        check(dialog.isVisible() and not app.input_layer.isVisible(), '底部设置按钮有效且弹窗期间停用按钮输入层')
        check(not getter(int(dialog.winId()), -20) & 0x20, '设置弹窗不穿透')
        dialog.reject()
        step(opaque)
    def opaque():
        app.settings.material = 'solid'
        app._choose_theme()
        step(opaque_click)
    def opaque_click():
        result['opaque_before'] = state()['clicks']
        inject(app.ring)
        step(opaque_received)
    def opaque_received():
        check(state()['clicks'] == result['opaque_before']+1, '背景改为实色仍可跨进程穿透')
        app.toggle_move()
        check(not getter(int(app.root.winId()), -20) & 0x20 and not app.input_layer.isVisible(), '临时移动模式恢复正常输入')
        app.escape_window()
        step(moved)
    def moved():
        check(app.click_through_active and app.input_layer.isVisible(), '结束移动恢复仅按钮可点击')
        app.hide_to_tray()
        check(not app.root.isVisible() and not app.input_layer.isVisible(), '收进托盘同时隐藏输入层')
        app.show_window()
        step(restored)
    def restored():
        check(app.input_layer.isVisible() and getter(int(app.root.winId()), -20) & 0x20, '托盘恢复保留穿透')
        app.toggle_click_through()
        check(not app.click_through_active and not app.input_layer.isVisible(), '可关闭穿透恢复原有交互')
        result.update(ok=True, checks_count=len(result['checks']))
        app.quit_app()

    step(start, 1800)
    QTimer.singleShot(16000, lambda: (result.update(error='跨进程穿透测试超时'), app.quit_app()))
    try:
        app.run()
    finally:
        peer.terminate()
        peer.wait(timeout=5)
        user.SetCursorPos(previous_cursor.x, previous_cursor.y)
        test_data.cleanup()
    (stage/f'v{APP_VERSION}-passthrough-validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['ok'] else 1)

if __name__ == '__main__':
    main()
