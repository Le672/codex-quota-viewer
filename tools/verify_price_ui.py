"""隔离后台验证价格显示、尺寸、设置保存 / 取消，不读取账户。"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

stage = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(stage))
with tempfile.TemporaryDirectory(prefix='quota-price-ui-') as directory:
    os.environ['APPDATA'] = directory
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from codexquota.appenv import Settings, APP_VERSION
    from codexquota.ui import QuotaApp
    from codexquota.collector import Bundle
    from codexquota.providers import Snapshot, AccountInfo, QuotaWindow, ResetCredit
    from codexquota.analytics import Advice
    from codexquota.community_api import parse_api
    from codexquota.token_usage import TokenEstimate, TokenReport, ModelUsage, TokenCounts
    import time

    class FakeController:
        busy = False
        def __init__(self, *args):
            pass
        def refresh_async(self, *args):
            return False
        def load_history_async(self):
            pass

    with patch('codexquota.ui.DataController', FakeController):
        app = QuotaApp(Settings(refresh_seconds=0, notify_on_expiry=False, theme='light'))
    now = int(time.time())
    estimates = {kind: TokenEstimate(kind=kind, observed_tokens=1_100_000,
        estimated_used=257_800_000, estimated_remaining=2_600_000, input_tokens=1_000_000,
        cached_input_tokens=800_000, output_tokens=100_000,
        model_usage=[ModelUsage('gpt-6.1-sol', TokenCounts(1_100_000, 1_000_000, 800_000, 100_000), False, 1)])
        for kind in ('weekly', '5h')}
    app.bundle = Bundle(snapshot=Snapshot(ok=True, source='api', account=AccountInfo(email='sample@example.invalid', plan_type='plus'),
        windows=[QuotaWindow(kind='weekly', used_percent=99, window_minutes=10080, resets_at=now+150000),
                 QuotaWindow(kind='5h', used_percent=44, window_minutes=300, resets_at=now+5000)],
        credits=[ResetCredit(credit_id=f'test-card-{i}', expires_at=now+86400*(i+10)) for i in range(5)]),
        tokens=TokenReport(windows=estimates), community=parse_api(json.loads((stage/'tests/fixtures/aihot-sample.json').read_text(encoding='utf-8')), now),
        advice=Advice(headline='建议保留，先用当前额度'))
    app._render()
    app.qt.processEvents()
    QTest.qWait(150)
    checks = []
    def check(value, description):
        assert value, description
        checks.append(description)
    try:
        text = app.window_widgets['weekly']['tokens'].text()
        check('257.8M' in text and '≈$346.86' in text and '2.6M' in text and '≈$3.50' in text, '已用 / 剩余 tokens 分别显示按分类折算的美元等值')
        check(app.root.size().width() == 390 and app.root.height() == 460, '完整面板尺寸保持 390×460')
        layout = dict(full_scroll=app.scroll.verticalScrollBar().maximum())
        check(app.scroll.verticalScrollBar().maximum() == 0, '新增价格后完整面板首屏没有额外滚动')
        check(app.credits_card.mapTo(app.scroll.viewport(), app.credits_card.rect().bottomRight()).y() < app.scroll.viewport().height(), '重置卡与用卡建议首屏完整显示')
        for kind in ('weekly', '5h'):
            label = app.window_widgets[kind]['tokens']
            check(label.fontMetrics().boundingRect(0, 0, 2000, 2000, Qt.TextFlag.TextExpandTabs, label.text()).width() <= label.width(), '完整面板价格文字没有横向裁切')
        app.root.grab().save(str(stage/'price-preview-full.png'))
        app.toggle_compact()
        app.qt.processEvents()
        QTest.qWait(150)
        check(app.root.width() == 390 and app.root.height() == 170, 'HUD 尺寸保持 390×170')
        check('$' in app.hud_widgets['weekly']['tokens'].text(), 'HUD 同样显示已用和剩余价值')
        check(app.status.mapTo(app.root, app.status.rect().bottomRight()).y() < app.root.height(), 'HUD 状态栏完整显示')
        app.root.grab().save(str(stage/'price-preview-hud.png'))
        app.show_settings()
        dialog = app._dialogs['settings']
        app.qt.processEvents()
        check(dialog.tabs.count() == 3 and dialog.tabs.tabText(2) == '价格', '设置提供独立价格页')
        dialog.tabs.setCurrentIndex(2)
        dialog.pricing_combo.setCurrentIndex(dialog.pricing_combo.findData('gpt-5.6-sol'))
        check('$4' in dialog.price_details.text() and '$20' in dialog.price_details.text(), '价格页显示所选模型的官方输入 / 输出单价')
        dialog.grab().save(str(stage/'price-preview-settings.png'))
        QTest.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
        check(app.settings.pricing_model == 'auto', '取消价格设置保留原偏好')
        app.show_settings()
        dialog = app._dialogs['settings']
        dialog.pricing_combo.setCurrentIndex(dialog.pricing_combo.findData('gpt-5.6-sol'))
        QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
        check(app.settings.pricing_model == 'gpt-5.6-sol' and '≈$731.21' in app.hud_widgets['weekly']['tokens'].text(), '保存模型后即时重算价值')
        app.show_settings()
        dialog = app._dialogs['settings']
        dialog.value_check.setChecked(False)
        QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
        check('$' not in app.hud_widgets['weekly']['tokens'].text() and not Settings.load().show_token_value, '价格显示开关可保存')
        output = dict(ok=True, checks=checks, layout=layout)
    except Exception as error:
        output = dict(ok=False, checks=checks, error=repr(error),
                      scroll=app.scroll.verticalScrollBar().maximum(), root_size=[app.root.width(), app.root.height()])
        app.root.grab().save(str(stage/'price-preview-failed.png'))
        raise
    finally:
        (stage/f'v{APP_VERSION}-price-ui-validation.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
        app.quit_app()
    print(json.dumps(output, ensure_ascii=False))
