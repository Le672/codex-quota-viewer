"""隔离账户，验证会话、缓存明细入口和后台更新；导出示例界面。"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
with tempfile.TemporaryDirectory(prefix='quota-details-') as directory:
    os.environ['APPDATA'] = directory
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from codexquota.appenv import Settings, APP_VERSION
    from codexquota.collector import Bundle
    from codexquota.providers import Snapshot, QuotaWindow, AccountInfo, ResetCredit
    from codexquota.token_usage import TokenEstimate, TokenReport, TokenCounts, ModelUsage
    from codexquota.community_api import parse_api
    from codexquota.ui import QuotaApp

    class FakeController:
        busy = False
        def __init__(self, *args): pass
        def refresh_async(self, *args): return False
        def load_history_async(self): pass

    with patch('codexquota.ui.DataController', FakeController):
        app = QuotaApp(Settings(refresh_seconds=0, theme='dark', material='solid',
                                notify_on_expiry=False, click_through=True))
    now = int(time.time())
    counts = TokenCounts(1100000, 1000000, 800000, 100000)
    estimate = TokenEstimate(kind='session', observed_tokens=counts.total, input_tokens=counts.input,
        cached_input_tokens=counts.cached_input, output_tokens=counts.output, observed_calls=12,
        model_usage=[ModelUsage('gpt-6.1-sol', counts, False, 12)], note='示例数据；总输入包含命中输入，推理已包含在输出中。')
    report = TokenReport(windows={'5h': estimate, 'weekly': estimate}, session=estimate,
                         session_id='example-session', session_activity_at=now)
    app.bundle = Bundle(snapshot=Snapshot(ok=True, source='api', account=AccountInfo(plan_type='plus'),
        windows=[QuotaWindow(kind=k, used_percent=30, window_minutes=300 if k=='5h' else 10080,
                             resets_at=now+4000) for k in ('5h', 'weekly')],
        credits=[ResetCredit(credit_id='example-card', expires_at=now+864000)]), tokens=report,
        community=parse_api(json.loads((root/'tests/fixtures/aihot-sample.json').read_text(encoding='utf-8')), now))
    checks = []
    def check(value, label):
        assert value, label
        checks.append(label)
    try:
        app._render()
        QTest.qWait(200)
        check('1.1M' in app.session_label.text() and '80.0%' in app.session_label.text(), '主面板显示本会话和缓存命中率')
        check(app.scroll.verticalScrollBar().maximum() == 0, '完整小窗口首屏无额外滚动')
        check(app.root.width() == 390 and app.root.height() == 460, '完整面板尺寸保持 390×460')
        QTest.mouseClick(app.usage_button, Qt.MouseButton.LeftButton)
        app.qt.processEvents()
        dialog = app._dialogs['usage']
        check(dialog.isVisible() and dialog.tabs.count() == 3, '用量按钮打开会话、5 小时、每周三个分页')
        text = dialog.pages['session'].toPlainText()
        check(all(value in text for value in ('1,100,000', '1,000,000', '800,000', '200,000', '100,000', '80.0%')), '明细包含准确总量、输入、命中、未命中、输出与命中率')
        check('example-session' in text and '最近活动' in text, '会话识别依据明确可查')
        check('API 等值估算' in text and '$' in text, '会话明细沿用价格折算')
        (root/'previews').mkdir(exist_ok=True)
        dialog.grab().save(str(root/'previews/v1.10-token-details.png'))
        dialog.close()
        app.root.grab().save(str(root/'previews/v1.10-full.png'))
        app.toggle_compact()
        QTest.qWait(150)
        check(app.root.width() == 390 and app.root.height() == 170, 'HUD 尺寸保持 390×170')
        check('1.1M' in app.hud_note.text() and '80.0%' in app.hud_note.text() and '重置 ' in app.hud_note.text(), 'HUD 同时显示会话、命中率与在线预测')
        check(app.usage_button.isVisible(), '穿透 HUD 保留用量按钮')
        check(app.status.mapTo(app.root, app.status.rect().bottomRight()).y() < app.root.height(), 'HUD 状态栏没有被挤出')
        app.root.grab().save(str(root/'previews/v1.10-hud.png'))
        app.show_token_usage()
        dialog = app._dialogs['usage']
        report.session = TokenEstimate(kind='session', observed_tokens=200, input_tokens=100, output_tokens=100)
        app._handle_event({'type':'tokens_done', 'tokens':report})
        check('200 tokens' in dialog.pages['session'].toPlainText() and '0.0%' in app.hud_note.text(), '后台 tokens 事件即时更新 HUD 与已打开的明细')
        report.session = TokenEstimate(kind='session')
        app._handle_event({'type':'tokens_done', 'tokens':report})
        check('命中 —' in app.hud_note.text(), '无输入样本时不虚构命中率')
        result = dict(ok=True, version=APP_VERSION, checks=checks)
        (root/f'v{APP_VERSION}-token-ui-validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=True))
    finally:
        app.quit_app()
