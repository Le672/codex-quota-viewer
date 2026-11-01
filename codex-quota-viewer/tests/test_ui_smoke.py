"""真实 Qt 事件循环与 QTest 按钮输入；隔离后台，不接触账号、不消费卡片。"""

def main():
    import json
    import os
    import sys
    import tempfile
    import threading
    import time
    from pathlib import Path
    from unittest.mock import patch
    from PySide6.QtCore import Qt, QTimer, QEvent, QPointF
    from PySide6.QtGui import QEnterEvent, QColor, QPalette
    from PySide6.QtWidgets import QPushButton, QApplication, QSystemTrayIcon
    from PySide6.QtTest import QTest

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    test_data = tempfile.TemporaryDirectory(prefix='quota-qt-')
    os.environ['APPDATA'] = test_data.name
    from codexquota.appenv import Settings, APP_VERSION
    from codexquota.analytics import Advice
    from codexquota.collector import Bundle
    from codexquota.providers import Snapshot, QuotaWindow, ResetCredit, AccountInfo
    from codexquota.community_api import parse_api
    from codexquota.ui import QuotaApp
    from codexquota.token_usage import TokenEstimate, TokenReport

    stage = Path(__file__).resolve().parents[1]
    now = int(time.time())
    payload = json.loads((stage/'tests'/'fixtures'/'aihot-sample.json').read_text(encoding='utf-8'))
    community = parse_api(payload, now)
    bundle = Bundle(snapshot=Snapshot(ok=True, source='api', account=AccountInfo(email='test@example.invalid', plan_type='plus'),
        windows=[QuotaWindow(kind='5h', used_percent=37, window_minutes=300, resets_at=now+5000),
                 QuotaWindow(kind='weekly', used_percent=70, window_minutes=10080, resets_at=now+150000)],
        credits=[ResetCredit(credit_id=f'test-card-{i}', scope='codexRateLimits', expires_at=now+86400*(i+10)) for i in range(5)]),
        community=community, advice=Advice(headline='建议保留，先用当前额度', lines=['当前每周额度已用 70%', '测试卡片还有 10 天到期']), source_note='隔离测试',
        tokens=TokenReport(windows={
            '5h': TokenEstimate(kind='5h', observed_tokens=120000, estimated_used=200000, estimated_remaining=340000, note='本机样本粗略估算'),
            'weekly': TokenEstimate(kind='weekly', observed_tokens=500000, estimated_used=700000, estimated_remaining=300000, note='本机样本粗略估算')}))

    class FakeController:
        def __init__(self, settings, events):
            self.events, self.busy, self.reset_calls = events, False, []
        def refresh_async(self, reason):
            if self.busy:
                return False
            self.busy = True
            self.events.put(dict(type='refresh_started'))
            def work():
                time.sleep(1.2)
                self.busy = False
                self.events.put(dict(type='refresh_done', bundle=bundle))
            threading.Thread(target=work, daemon=True).start()
            return True
        def load_history_async(self):
            threading.Timer(.15, lambda: self.events.put(dict(type='history_done', samples=[]))).start()
        def reset_credit_async(self, card):
            self.reset_calls.append(card)
            threading.Timer(.15, lambda: self.events.put(dict(type='reset_done', ok=True, message='模拟提交成功'))).start()
            return True

    with patch('codexquota.ui.DataController', FakeController):
        app = QuotaApp(Settings(refresh_seconds=0, community_events=True, notify_on_expiry=False, minimize_to_tray=False, theme='dark'))
    result = {'ok': False, 'checks': []}
    beats = [0]
    timer = QTimer(app.root)
    timer.timeout.connect(lambda: beats.__setitem__(0, beats[0]+1))
    timer.start(10)

    def click(button):
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)

    def check(condition, message):
        assert condition, message
        result['checks'].append(message)

    def clear_fraction():
        frame = app.root.grab().toImage()
        samples = [frame.pixelColor(x, y).alpha() for y in range(10, frame.height(), 13) for x in range(10, frame.width(), 13)]
        return sum(alpha <= 3 for alpha in samples)/len(samples)

    def alpha_fraction(expected):
        frame = app.root.grab().toImage()
        samples = [frame.pixelColor(x, y).alpha() for y in range(20, frame.height()-20, 13)
                   for x in range(20, frame.width()-20, 13)]
        return sum(abs(alpha-expected) <= 4 for alpha in samples)/len(samples)

    def native_style():
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        getter = user.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.GetWindowLongW
        getter.argtypes = [wintypes.HWND, ctypes.c_int]
        getter.restype = ctypes.c_ssize_t
        return getter(int(app.root.winId()), -20)

    def step(callback, delay=100):
        def run():
            try:
                callback()
            except Exception as error:
                result['error'] = repr(error)
                result['layout'] = dict(scroll=app.scroll.verticalScrollBar().maximum(), viewport=app.scroll.viewport().height(),
                    overview=app.ring.parentWidget().height(), prediction=app.prediction_card.height(), credits=app.credits_card.height())
                app.root.grab().save(str(stage/'qt-preview-failed.png'))
                app.quit_app()
        QTimer.singleShot(delay, run)

    def start():
        if not app.bundle.snapshot.ok or app.controller.busy:
            step(start, 100)
            return
        check(app.bundle.snapshot.ok and not app.controller.busy, '后台刷新完成')
        check((app.root.width(), app.root.height()) == (390, 460), '完整面板采用 390×460 紧凑尺寸')
        check(app.scroll.verticalScrollBar().maximum() == 0 and
              app.credits_card.mapTo(app.scroll.viewport(), app.credits_card.rect().bottomRight()).y() < app.scroll.viewport().height(),
              '额度、两项预测和重置卡在首屏完整显示')
        check(app.root.windowOpacity() == 1.0, '文字不跟随整窗透明度变淡')
        check(not app.root.backdrop.applied and app.root.backdrop.method == 'transparent', '默认纯透明模式关闭原生模糊')
        check(clear_fraction() > .60, '主面板大部分像素透明且没有渐变底色')
        check('已用≈' in app.window_widgets['weekly']['tokens'].text() and '余≈' in app.window_widgets['5h']['tokens'].text(), '两组额度显示已用和剩余 tokens 预测')
        for button in (app.refresh_button, app.history_button, app.settings_button, app.reset_button):
            check(isinstance(button, QPushButton), '操作使用原生 Qt 按钮')
            identity = id(button)
            for _ in range(1000):
                QApplication.sendEvent(button, QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1)))
                QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
            check(id(button) == identity, '悬停不重建按钮')
        check(app.forecast_widgets['reset']['value'].text() != '暂无可用预测', '主界面显示重置预测')
        check(app.forecast_widgets['card_grant']['value'].text() != '暂无可用预测', '主界面显示发卡预测')
        check(app.reset_button.mapTo(app.root, app.reset_button.rect().bottomRight()).y() < app.root.height(), '固定操作栏完整显示')
        app.root.grab().save(str(stage/'qt-preview-full.png'))
        click(app.mode_button)
        check(app.settings.compact and not app.scroll.isVisible() and app.hud.isVisible(), 'HUD 切换立即生效')
        check(not app.root.backdrop.applied, 'HUD 保留纯透明模式')
        step(hud)

    def hud():
        check((app.root.width(), app.root.height()) == (390, 170), 'HUD 采用 390×170 紧凑尺寸')
        check(app.hud_widgets['5h']['tokens'].isVisible() and '余≈' in app.hud_widgets['5h']['tokens'].text() and
              app.status.mapTo(app.root, app.status.rect().bottomRight()).y() < app.root.height(), 'HUD 显示 tokens 预测且状态栏没有裁切')
        check(app.hud_note.isVisible() and '重置 ' in app.hud_note.text() and 'AIHOT' not in app.hud_note.text(), 'HUD 保留在线预测摘要并去除提供方字样')
        app.root.grab().save(str(stage/'qt-preview-hud.png'))
        click(app.pin_button)
        step(pinned, 150)

    def pinned():
        check(app.settings.always_on_top and not app.root.backdrop.applied and
              app.root._material_key[0] == int(app.root.winId()), '置顶后重新绑定当前窗口的透明模式')
        click(app.pin_button)
        step(unpinned, 150)

    def unpinned():
        check(not app.settings.always_on_top and not app.root.backdrop.applied, '取消置顶后透明模式仍正常')
        click(app.mode_button)
        check(not app.settings.compact and app.scroll.isVisible(), '展开恢复完整面板')
        check((app.root.width(), app.root.height()) == (390, 460), 'HUD 展开恢复缩小后的完整尺寸')
        click(app.settings_button)
        dialog = app._dialogs['settings']
        check(dialog.isVisible(), '设置点击打开非阻塞弹窗')
        dialog.interval_input.setText('bad')
        click(dialog.save_button)
        check(dialog.isVisible() and dialog.error_label.text(), '无效设置保留弹窗并显示错误')
        dialog.interval_input.setText('0')
        dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData('light'))
        dialog.material_combo.setCurrentIndex(dialog.material_combo.findData('solid'))
        dialog.tint_slider.setValue(75)
        click(dialog.save_button)
        check(app.settings.theme == 'light' and not dialog.isVisible(), '主题保存成功且关闭弹窗')
        check(app.settings.material == 'solid' and app.settings.opacity == .75, '材质和玻璃浓度设置保存')
        step(history, 1500)

    def history():
        check(app.scroll.verticalScrollBar().maximum() == 0 and
              app.credits_card.mapTo(app.scroll.viewport(), app.credits_card.rect().bottomRight()).y() < app.scroll.viewport().height(),
              '浅色主题下核心内容仍在首屏完整显示')
        check(not app.root.backdrop.applied, '实色模式关闭原生亚克力')
        click(app.settings_button)
        setting_dialog = app._dialogs['settings']
        setting_dialog.material_combo.setCurrentIndex(setting_dialog.material_combo.findData('glass'))
        click(setting_dialog.save_button)
        step(open_history, 1400)

    def open_history():
        check(app.root.backdrop.applied, '重新启用玻璃不重建按钮')
        click(app.history_button)
        dialog = app._dialogs['history']
        check('在线预测' in dialog.text.toPlainText() and 'aihot' not in dialog.text.toPlainText().lower(), '记录包含在线预测详情并去除提供方字样')
        check('Tokens 用量与预测' in dialog.text.toPlainText() and '本机样本' in dialog.text.toPlainText(), '记录包含 tokens 观测和预测依据')
        click(dialog.scan_button)
        check(app._history_busy, '记录扫描后台启动')
        step(history_done, 350)

    def history_done():
        dialog = app._dialogs['history']
        check(dialog.scan_button.isVisible() and dialog.scan_button.isEnabled() and not app._history_busy, '记录扫描结束恢复操作栏')
        app._history_busy = True
        app._handle_event(dict(type='history_failed', error='模拟错误'))
        check(dialog.scan_button.isEnabled(), '扫描失败恢复按钮')
        dialog.close()
        click(app.reset_button)
        dialog = app._dialogs['reset']
        check(dialog.isVisible() and not app.controller.reset_calls, '重置弹窗打开不提交卡片')
        check(dialog.tree.topLevelItemCount() == 5, '卡片弹窗包含全部卡片')
        click(dialog.cancel_button)
        check(not dialog.isVisible() and not app.controller.reset_calls, '取消不消费卡片')
        click(app.reset_button)
        click(app._dialogs['reset'].confirm_button)
        check(app.controller.reset_calls == ['test-card-0'], '仅确认后向隔离桩提交选中卡片')
        step(after_reset, 1700)

    def after_reset():
        app._dialogs['reset'].close()
        check(not app.controller.busy, '模拟提交后刷新完成')
        click(app.refresh_button)
        before = beats[0]
        click(app.settings_button)
        check(app._dialogs['settings'].isVisible() and app.controller.busy, '慢刷新期间设置立即打开')
        app._dialogs['settings'].close()
        step(lambda: finish(before), 1500)

    def finish(before):
        check(beats[0]-before >= 40, '慢刷新期间主线程心跳持续')
        check(app.refresh_button.isEnabled(), '刷新结束恢复按钮')
        app._handle_event(dict(type='refresh_done', bundle=Bundle(snapshot=Snapshot(ok=False, error='模拟失败'))))
        check(app.reset_button.isEnabled(), '失败刷新保留数据并恢复卡片按钮')
        app.hide_to_tray()
        check(not app.root.isVisible(), '收进托盘')
        app.show_window()
        check(app.root.isVisible(), '从托盘恢复')
        click(app.settings_button)
        dialog = app._dialogs['settings']
        dialog.material_combo.setCurrentIndex(dialog.material_combo.findData('transparent'))
        check(not dialog.tint_slider.isEnabled(), '纯透明模式禁用玻璃底色浓度')
        click(dialog.save_button)
        step(final_transparent, 1500)

    def final_transparent():
        check(app.settings.material == 'transparent' and not app.root.backdrop.applied and clear_fraction() > .60, '亚克力切回纯透明后清除模糊与填充')
        check(app.root.windowOpacity() == 1.0 and app.scroll.verticalScrollBar().maximum() == 0, '透明模式文字保持不透明且 tokens 不扩大窗口')
        app.root.grab().save(str(stage/'qt-preview-transparent-light.png'))
        click(app.settings_button)
        dialog = app._dialogs['settings']
        check(dialog.transparent_slider.isEnabled() and dialog.transparent_slider.minimum() == 0 and
              dialog.transparent_slider.maximum() == 100, '透明背景支持 0～100% 独立不透明度')
        dialog.transparent_slider.setValue(60)
        field = dialog.color_inputs['muted']
        field.setText('#345678')
        field.textEdited.emit(field.text())
        check(app.appearance.transparent_opacity == .60 and app.settings.transparent_opacity == 0,
              '实时预览与已保存偏好分离')
        check(app.colors['muted'] == '#345678', '辅助文字配色实时预览')
        step(cancel_preview, 150)

    def cancel_preview():
        check(alpha_fraction(153) > .60 and app.root.windowOpacity() == 1., '透明背景浓度生效且整窗透明度保持 100%')
        click(app._dialogs['settings'].cancel_button)
        step(restore_preview, 150)

    def restore_preview():
        check(app.appearance.transparent_opacity == 0 and not app.settings.custom_colors and clear_fraction() > .60,
              '取消恢复原始透明度与颜色')
        click(app.settings_button)
        dialog = app._dialogs['settings']
        field = dialog.color_inputs['text']
        field.setText('bad')
        click(dialog.save_button)
        check(dialog.isVisible() and '#RRGGBB' in dialog.error_label.text(), '无效颜色保留设置并阻止保存')
        click(dialog.reset_colors_button)
        click(dialog.color_buttons['title'])
        check(dialog.color_picker.isVisible(), '颜色选择器使用非阻塞窗口')
        dialog.color_picker.setCurrentColor(QColor('#336699'))
        dialog.color_picker.accept()
        check(dialog.color_inputs['title'].text() == '#336699' and app.colors['title'] == '#336699', '选色更新标题并实时预览')
        for role, value in (('text', '#223344'), ('muted', '#445566'), ('weekly', '#CC3300'),
                            ('five_hour', '#5533AA'), ('accent', '#116644'), ('warn', '#AA6611'), ('danger', '#AA2233')):
            field = dialog.color_inputs[role]
            field.setText(value)
            field.textEdited.emit(value)
        dialog.transparent_slider.setValue(40)
        dialog.variables['always_on_top'].setChecked(True)
        dialog.variables['hide_taskbar'].setChecked(True)
        result['saved_position'] = [app.root.x(), app.root.y()]
        click(dialog.save_button)
        step(tray_only, 200)

    def tray_only():
        style = native_style()
        check(app.root.isVisible() and app.tray.isVisible() and style & 0x80 and not style & 0x40000 and style & 8,
              'Win32 确认前台置顶且无任务栏按钮，托盘入口保留')
        check([app.root.x(), app.root.y()] == result['saved_position'], '修改任务栏模式保持窗口位置')
        restored = Settings.load()
        check(restored.hide_taskbar and restored.always_on_top and restored.transparent_opacity == .40 and
              len(restored.custom_colors) == 8, '悬浮模式、不透明度及全部配色持久化')
        check(app.window_widgets['weekly']['bar'].color == '#CC3300' and
              app.hud_widgets['5h']['bar'].color == '#5533AA' and
              app.window_widgets['5h']['tokens'].palette().color(QPalette.ColorRole.WindowText).name() == '#445566',
              '完整面板与 HUD 的额度和 tokens 颜色一致')
        weekly = app.bundle.snapshot.window('weekly')
        weekly.used_percent = 97
        app._render()
        check(app.pace_label.text() == '接近耗尽' and '#AA2233' in app.pace_label.styleSheet(),
              '告急配色应用到接近耗尽文字')
        weekly.used_percent = 70
        app._render()
        check(alpha_fraction(102) > .60, '已保存的背景不透明度应用到主面板')
        frame = app.root.grab().toImage()
        dpr = frame.devicePixelRatio()
        point = app.mode_button.mapTo(app.root, app.mode_button.rect().topLeft())
        check(frame.pixelColor(round((point.x()+5)*dpr), round((point.y()+app.mode_button.height()/2)*dpr)).alpha() < 110,
              '透明按钮无不透明黑色底块')
        app.root.grab().save(str(stage/'qt-preview-custom-colors.png'))
        app.show_settings()
        dialog = app._dialogs['settings']
        dialog.transparent_slider.setValue(100)
        check(alpha_fraction(255) > .80, '100% 不透明度实际填充完整背景')
        dialog.transparent_slider.setValue(0)
        check(clear_fraction() > .60, '0% 不透明度恢复纯透明背景')
        dialog.transparent_slider.setValue(40)
        dialog.grab().save(str(stage/'qt-preview-settings.png'))
        check(dialog.isVisible() and dialog.parent() == app.root, '仅托盘模式仍可打开设置')
        dialog.reject()
        click(app.pin_button)
        step(unpin_tray_only, 150)

    def unpin_tray_only():
        style = native_style()
        check(app.root.isVisible() and style & 0x80 and not style & 0x40000 and not style & 8,
              '取消置顶不恢复任务栏图标')
        click(app.pin_button)
        app.hide_to_tray()
        app.show_window()
        step(restore_tray_only, 150)

    def restore_tray_only():
        check(app.root.isVisible() and native_style() & 0x80 and native_style() & 8,
              '托盘隐藏和恢复保留置顶及无任务栏模式')
        click(app.mode_button)
        check(app.settings.compact and app.hud.isVisible() and app.root.width() == 390,
              '仅托盘悬浮模式可切换 HUD')
        app.tray_only_action.trigger()
        step(normal_taskbar, 150)

    def normal_taskbar():
        check(app.root.isVisible() and not native_style() & 0x80 and not app.settings.hide_taskbar,
              '托盘菜单可恢复普通任务栏窗口')
        with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=False):
            app.settings.hide_taskbar = True
            app._apply_window_mode()
            check(not native_style() & 0x80, '系统托盘不可用时保留可恢复的普通窗口')
        result.update(ok=True, heartbeats=beats[0], hover_events=8000, checks_count=len(result['checks']))
        (stage/f'v{APP_VERSION}-qt-validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        app.quit_app()

    step(start, 1700)
    QTimer.singleShot(30000, lambda: (result.update(error='GUI 测试超时'), app.quit_app()))
    app.run()
    print(json.dumps(result, ensure_ascii=False))
    test_data.cleanup()
    raise SystemExit(0 if result['ok'] else 1)

if __name__ == "__main__":
    main()
