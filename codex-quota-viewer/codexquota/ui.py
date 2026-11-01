"""Qt 桌面 widget：圆角暗色面板、紧凑 HUD、原生按钮和独立在线预测。"""
from __future__ import annotations
import os
import queue
import re
import sys
import time
import traceback
from dataclasses import replace
from PySide6.QtCore import Qt, QTimer, QRectF, QPoint
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QShortcut, QKeySequence
from PySide6.QtWidgets import (QApplication, QWidget, QFrame, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QScrollArea, QDialog, QLineEdit, QComboBox, QCheckBox,
    QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QSystemTrayIcon, QMenu, QSlider,
    QColorDialog, QTabWidget)
from . import analytics as ana, events as ev
from .appenv import APP_DISPLAY_NAME, APP_VERSION, Settings, app_data_dir, log
from .collector import Bundle, DataController
from .instance import consume_show_request
from .lifecycle import CodexExitSignal
from .providers import WINDOW_5H, WINDOW_WEEKLY
from .glass import GlassSurface, GlassDialog, BackdropResult, set_backdrop, rounded_mask
from .token_usage import compact_tokens
from .pricing import PRICES, PRICING_CHECKED, PRICING_URL, estimate_value, money
from .icons import application_icon
from .input_layer import ButtonInputLayer
from .placement import startup_position

DARK = dict(bg='#111C2B', card='#23313E', text='#F5F9FF', muted='#BACADB', border='#465667',
            accent='#73E4BF', accent_text='#0B3025', danger='#FF8D8C', warn='#F0CC8E', soft='#2C3C4B', end='#182B31')
LIGHT = dict(bg='#EFF5FF', card='#F6FAFF', text='#172B41', muted='#475D75', border='#C8D8E8',
             accent='#24865E', accent_text='#FFFFFF', danger='#C24D45', warn='#986112', soft='#E3EDF7', end='#E0EDF4')

COLOR_ROLES = (('title', '窗口标题'), ('text', '正文 / 按钮'), ('muted', '辅助文字 / tokens'),
               ('weekly', '每周额度'), ('five_hour', '5 小时额度'), ('accent', '正常状态 / 节奏'),
               ('warn', '警告文字'), ('danger', '告急文字'))

def theme_colors(theme):
    if theme == 'auto':
        from .theme import system_prefers_dark
        theme = 'dark' if system_prefers_dark() else 'light'
    colors = dict(DARK if theme == 'dark' else LIGHT)
    colors.update(title=colors['text'], weekly='#FF8179', five_hour='#B694F5')
    return colors, theme == 'dark'

def prediction_text(text):
    """预测提供方的名字不进入界面，原始数据与接口地址保持不变。"""
    return re.sub(r'AIHOT\s*', '', str(text or ''), flags=re.IGNORECASE)

class Meter(QWidget):
    """纯展示图层，不绑定鼠标事件、不在悬停时删除对象。"""
    def __init__(self, parent=None, ring=False):
        super().__init__(parent)
        self.ring, self.value, self.ideal = ring, 0., None
        self.color, self.track, self.text = '#64DF93', '#35413A', '#F0F6F2'
        self.setFixedSize(76, 76) if ring else self.setFixedHeight(8)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    def set_value(self, value, ideal=None, color=None):
        self.value = max(0., min(100., value))
        self.ideal = max(0., min(100., ideal)) if ideal is not None else None
        if color:
            self.color = color
        self.update()
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.ring:
            scale = self.width()/112
            inset = 8*scale
            rect = QRectF(inset, inset, self.width()-2*inset, self.height()-2*inset)
            painter.setPen(QPen(QColor(self.track), 8*scale))
            painter.drawEllipse(rect)
            pen = QPen(QColor(self.color), 8*scale)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawArc(rect, 90*16, -int(360*16*self.value/100))
            painter.setPen(QColor(self.text))
            painter.setFont(QFont('Segoe UI', round(24*scale), QFont.Weight.Bold))
            painter.drawText(QRectF(0, 30*scale, self.width(), 40*scale), Qt.AlignmentFlag.AlignCenter, f'{self.value:.0f}%')
            painter.setFont(QFont('Microsoft YaHei UI', 7))
            painter.drawText(QRectF(0, 72*scale, self.width(), 20*scale), Qt.AlignmentFlag.AlignCenter, '剩余')
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.track))
            painter.drawRoundedRect(QRectF(0, 1, self.width(), 6), 3, 3)
            if self.value:
                painter.setBrush(QColor(self.color))
                painter.drawRoundedRect(QRectF(0, 1, self.width()*self.value/100, 6), 3, 3)
            if self.ideal is not None:
                x = max(2., min(self.width()-2., self.width()*self.ideal/100))
                painter.setPen(QPen(QColor(self.text), 1.4))
                painter.drawLine(QPoint(int(x), 0), QPoint(int(x), 8))
        painter.end()

class WidgetWindow(QWidget):
    def __init__(self, app):
        super().__init__()
        self.owner = app
        self.drag_offset = None
        self.backdrop = BackdropResult()
        self._material_key = None
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.apply_material)
    def resizeEvent(self, event):
        super().resizeEvent(event)
        rounded_mask(self, 24, 5)
        self.owner._schedule_position_save()
    def moveEvent(self, event):
        super().moveEvent(event)
        self.owner._schedule_position_save()
    def apply_material(self):
        if not self.isVisible():
            return
        appearance = self.owner.appearance
        key = (int(self.winId()), appearance.material, self.owner.dark_theme, appearance.opacity)
        if key == self._material_key:
            return
        self._material_key = key
        self.backdrop = set_backdrop(key[0], enabled=key[1] == 'glass', dark=key[2], tint=key[3])
        if key[1] == 'transparent':
            self.backdrop.method = 'transparent'
        log(f'玻璃材质: {self.backdrop.method} · {self.backdrop.detail}')
        for surface in self.findChildren(GlassSurface):
            surface.update()
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 72:
            self.drag_offset = event.globalPosition().toPoint()-self.frameGeometry().topLeft()
            event.accept()
    def mouseMoveEvent(self, event):
        if self.drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint()-self.drag_offset)
    def mouseReleaseEvent(self, event):
        self.drag_offset = None
        self.owner._save_position()
    def closeEvent(self, event):
        if self.owner._closed:
            event.accept()
        else:
            event.ignore()
            self.owner.on_close()

class QuotaApp:
    def __init__(self, settings: Settings, start_minimized=None):
        self.settings = settings
        self._position_ready = False
        self.codex_exit_signal = CodexExitSignal()
        self._move_mode = False
        self._appearance_preview = None
        self.events = queue.Queue()
        self.controller = DataController(settings, self.events)
        self.bundle, self.samples = Bundle(), []
        self._closed, self._history_busy = False, False
        self._dialogs, self._notified = {}, set()
        self._next_refresh = time.monotonic() + (settings.refresh_seconds or 180)
        self._next_token_refresh = time.monotonic() + 15
        self.qt = QApplication.instance() or QApplication(sys.argv[:1])
        self.qt.setQuitOnLastWindowClosed(False)
        self.qt.setApplicationName(APP_DISPLAY_NAME)
        self.icon = application_icon()
        self.qt.setWindowIcon(self.icon)
        self.root = WidgetWindow(self)
        self.position_timer = QTimer(self.root)
        self.position_timer.setSingleShot(True)
        self.position_timer.setInterval(250)
        self.position_timer.timeout.connect(self._save_position)
        self.root.setWindowIcon(self.icon)
        self.root.setWindowTitle(f'{APP_DISPLAY_NAME} · v{APP_VERSION}')
        self._choose_theme()
        self._build()
        self.input_layer = ButtonInputLayer(self)
        self._install_tray()
        self._apply_window_mode()
        QShortcut(QKeySequence('Ctrl+R'), self.root, activated=lambda: self.refresh('hotkey'))
        QShortcut(QKeySequence('Escape'), self.root, activated=self.escape_window)
        self.pump_timer = QTimer(self.root)
        self.pump_timer.timeout.connect(self._pump)
        self.pump_timer.start(50)
        self.tick_timer = QTimer(self.root)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start(1000)
        self._apply_mode()
        screens = [self.qt.primaryScreen()] + [screen for screen in self.qt.screens()
                                              if screen != self.qt.primaryScreen()]
        areas = [screen.availableGeometry().getRect() for screen in screens if screen]
        position = startup_position(settings.geometry_tuple(),
                                    (self.root.width(), self.root.height()), areas)
        self.root.move(*position)
        self._position_ready = True
        self.qt.aboutToQuit.connect(self._save_position)
        QTimer.singleShot(150, lambda: self.refresh('startup'))
        initial_hidden = settings.start_minimized if start_minimized is None else start_minimized
        QTimer.singleShot(100, self.hide_to_tray if initial_hidden else self.show_window)

    def _schedule_position_save(self):
        if self._position_ready and not self._closed and not self.root.isMinimized():
            self.position_timer.start()

    def _save_position(self):
        if not self._position_ready or self._closed or self.root.isMinimized():
            return
        self.position_timer.stop()
        geometry = ','.join(str(value) for value in self.root.geometry().getRect())
        if geometry != self.settings.geometry:
            self.settings.geometry = geometry
            self.settings.save()

    @property
    def appearance(self):
        return self._appearance_preview or self.settings

    @property
    def click_through_active(self):
        return self.settings.click_through and not self._move_mode

    def _choose_theme(self):
        appearance = self.appearance
        c, self.dark_theme = theme_colors(appearance.theme)
        c.update(appearance.custom_colors)
        previous_colors = getattr(self, 'colors', None)
        self.colors = c
        glass = appearance.material == 'glass'
        transparent = appearance.material == 'transparent'
        button_bg = ('qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 rgba(240,250,255,35),stop:1 rgba(215,235,255,12))'
                     if self.dark_theme else 'qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 rgba(255,255,255,210),stop:1 rgba(237,247,255,115))') if glass else c['soft']
        button_border = 'rgba(238,250,255,55)' if glass and self.dark_theme else 'rgba(255,255,255,215)' if glass else c['border']
        hover_bg = 'rgba(233,249,255,48)' if glass and self.dark_theme else 'rgba(255,255,255,228)' if glass else c['card']
        if transparent:
            button_bg = 'transparent'
            button_border = 'rgba(185,200,215,110)'
            hover_bg = 'rgba(140,160,180,24)'
        primary_bg = ('transparent' if transparent else
                      f'qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {c["accent"]},stop:1 {"#44BB9B" if self.dark_theme else "#26754F"})')
        input_bg = 'transparent' if transparent else c['card']
        if not getattr(self, '_font_applied', False):
            self.qt.setFont(QFont('Microsoft YaHei UI', 9))
            self._font_applied = True
        stylesheet = f'''
            QWidget {{ color: {c['text']}; font-family: "Microsoft YaHei UI"; font-size: 12px; }}
            QWidget#shell, QFrame#card {{ background: transparent; border: none; }}
            QLabel {{ background: transparent; border: none; }}
            QLabel#title {{ color: {c['title']}; font-size: 20px; font-weight: 700; }}
            QLabel#heading {{ font-size: 13px; font-weight: 600; }}
            QLabel#muted {{ color: {c['muted']}; font-size: 11px; }}
            QLabel#percent {{ font-family: "Segoe UI"; font-size: 23px; font-weight: 700; }}
            QPushButton {{ background: {button_bg}; border: 1px solid {button_border}; border-radius: 13px;
                padding: 7px 12px; color: {c['text']}; font-size: 12px; }}
            QPushButton:hover {{ border-color: {c['accent']}; background: {hover_bg}; }}
            QPushButton:pressed {{ background: {c['border']}; }}
            QPushButton:disabled {{ color: {c['muted']}; }}
            QPushButton#primary {{ background: {primary_bg};
                color: {c['accent'] if transparent else c['accent_text']}; font-weight: 700; border-color: {c['accent']}; }}
            QPushButton#primary:hover {{ background: {hover_bg if transparent else c['accent']}; border-color: {c['text']}; }}
            QPushButton#primary:disabled {{ background: {button_bg if transparent else c['soft']}; color: {c['muted']}; }}
            QDialog {{ background: transparent; }}
            QTabWidget::pane {{ border: none; background: transparent; }}
            QTabBar::tab {{ color: {c['muted']}; padding: 7px 18px; border-bottom: 2px solid transparent; }}
            QTabBar::tab:selected {{ color: {c['text']}; border-bottom-color: {c['accent']}; }}
            QLineEdit, QComboBox, QPlainTextEdit, QTreeWidget {{ background: {input_bg}; color: {c['text']};
                border: 1px solid {c['border']}; border-radius: 7px; padding: 8px; selection-background-color: {c['soft']}; }}
            QHeaderView::section {{ background: {c['soft']}; color: {c['muted']}; padding: 8px; border: none; }}
            QTreeWidget::item {{ padding: 8px; }}
            QCheckBox {{ spacing: 9px; padding: 4px 0; }}
            QSlider::groove:horizontal {{ height: 5px; background: {c['border']}; border-radius: 2px; }}
            QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
            QSlider::handle:horizontal {{ background: {c['text']}; width: 14px; margin: -5px 0; border-radius: 7px; }}
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ background: transparent; width: 5px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: {c['border']}; min-height: 20px; border-radius: 2px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QMenu {{ background: {c['card']}; color: {c['text']}; border: 1px solid {c['border']}; }}
            QMenu::item {{ padding: 8px 24px; }} QMenu::item:selected {{ background: {c['soft']}; }}
            QToolTip {{ background: {c['card']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 6px; }}
            QWidget#shell QLabel {{ font-size: 11px; }}
            QWidget#shell QLabel#title {{ font-size: 16px; }}
            QWidget#shell QLabel#heading {{ font-size: 12px; }}
            QWidget#shell QLabel#percent {{ font-size: 19px; }}
            QWidget#shell QLabel[tokenValue="true"] {{ font-size: 10px; }}
            QWidget#shell QPushButton {{ padding: 4px 9px; font-size: 11px; border-radius: 10px; }}
        '''
        if self.qt.styleSheet() != stylesheet:
            self.qt.setStyleSheet(stylesheet)
        if hasattr(self, 'ring') and c != previous_colors:
            self._apply_colors()
        self.root.update()
        for surface in self.root.findChildren(GlassSurface):
            surface.update()
        if self.root.isVisible():
            QTimer.singleShot(0, self.root.apply_material)
        for dialog in self._dialogs.values():
            if dialog.isVisible():
                QTimer.singleShot(0, dialog.apply_material)

    def _apply_colors(self):
        c = self.colors
        self.ring.track, self.ring.text = c['border'], c['text']
        minimum = self.ring.value
        self.ring.set_value(minimum, color=c['danger'] if minimum < 10 else c['warn'] if minimum < 30 else c['accent'])
        for group in (self.window_widgets, self.hud_widgets):
            for kind, widgets in group.items():
                color = c['weekly' if kind == WINDOW_WEEKLY else 'five_hour']
                widgets['color'] = color
                widgets['percent'].setStyleSheet(f'color: {color};')
                bar = widgets['bar']
                bar.track, bar.text = c['border'], c['text']
                bar.set_value(bar.value, bar.ideal, color)
        self._update_pace_color()
        for label in (self.banner, self.community_warning):
            label.setStyleSheet(f'color: {c["warn"]}; font-size: 11px;')
        self.status.setStyleSheet(f'color: {c.get(getattr(self, "_status_kind", "muted"), c["muted"])}; font-size: 11px;')

    def _update_pace_color(self):
        state = self.pace_label.text()
        role = 'danger' if state == '接近耗尽' else 'warn' if state == '建议放缓' else 'accent'
        self.pace_label.setStyleSheet(f'color: {self.colors[role]}; font-weight: 700;')

    def _apply_window_mode(self):
        """Tool 窗口不进入任务栏；修改模式时保留位置、大小和可见状态。"""
        only_tray = self.settings.hide_taskbar and QSystemTrayIcon.isSystemTrayAvailable()
        flags = (Qt.WindowType.Tool if only_tray else Qt.WindowType.Window) | Qt.WindowType.FramelessWindowHint
        if self.settings.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        if self.click_through_active:
            flags |= Qt.WindowType.WindowTransparentForInput
        if self.root.windowFlags() != flags:
            self.input_layer.hide()
            visible = self.root.isVisible()
            geometry = self.root.geometry()
            dialogs = [dialog for dialog in self._dialogs.values() if dialog.isVisible()]
            self.root.setWindowFlags(flags)
            self.root.setGeometry(geometry)
            self.root.setWindowIcon(self.icon)
            self.root._material_key = None
            if visible:
                self.root.show()
            for dialog in dialogs:
                dialog.show()
                dialog.raise_()
        if hasattr(self, 'tray_only_action'):
            self.tray_only_action.setChecked(self.settings.hide_taskbar)
        if hasattr(self, 'pin_button'):
            self.pin_button.setText('已置顶' if self.settings.always_on_top else '置顶')
        if hasattr(self, 'click_through_action'):
            self.click_through_action.setChecked(self.settings.click_through)
            self.move_action.setText('锁定并恢复穿透' if self._move_mode else '移动窗口')
        self.input_layer.sync()

    def _label(self, text='', muted=False, heading=False, wrap=False):
        label = QLabel(text)
        label.setObjectName('muted' if muted else 'heading' if heading else '')
        label.setWordWrap(wrap)
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    def _button(self, text, command, primary=False, tooltip=''):
        button = QPushButton(text)
        if primary:
            button.setObjectName('primary')
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda checked=False: self._safe(command))
        button.setToolTip(tooltip)
        return button

    def _safe(self, callback):
        try:
            callback()
        except Exception as error:
            log('界面操作失败：' + traceback.format_exc())
            self._set_banner(f'操作失败：{error}')

    def _card(self):
        card = GlassSurface(self)
        card.setObjectName('card')
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(3)
        return card, layout

    def _build(self):
        outer = QVBoxLayout(self.root)
        outer.setContentsMargins(5, 5, 5, 5)
        self.shell = GlassSurface(self, shell=True)
        self.shell.setObjectName('shell')
        outer.addWidget(self.shell)
        page = QVBoxLayout(self.shell)
        page.setContentsMargins(12, 10, 12, 9)
        page.setSpacing(5)
        header = QHBoxLayout()
        header.setSpacing(5)
        title = self._label('Codex 额度')
        title.setObjectName('title')
        header.addWidget(title)
        header.addStretch()
        self.usage_button = self._button('用量', self.show_token_usage, tooltip='本会话、5 小时、每周的输入 / 输出与缓存明细')
        header.addWidget(self.usage_button)
        self.mode_button = self._button('HUD', self.toggle_compact, tooltip='切换紧凑 HUD / 完整面板')
        header.addWidget(self.mode_button)
        self.pin_button = self._button('置顶', self.toggle_topmost)
        header.addWidget(self.pin_button)
        self.header_refresh = self._button('↻', lambda: self.refresh('hud'), tooltip='刷新额度与在线预测')
        self.header_refresh.setFixedWidth(28)
        header.addWidget(self.header_refresh)
        close_button = self._button('×', self.on_close, tooltip='收进托盘')
        close_button.setFixedWidth(28)
        header.addWidget(close_button)
        page.addLayout(header)
        self.account_label = self._label('正在读取当前账号…', muted=True)
        page.addWidget(self.account_label)
        self.banner = self._label('', wrap=True)
        self.banner.setStyleSheet(f'color: {self.colors["warn"]}; font-size: 11px;')
        self.banner.hide()
        page.addWidget(self.banner)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName('body')
        self.body.setStyleSheet('QWidget#body { background: transparent; }')
        self.scroll.viewport().setObjectName('scrollViewport')
        self.scroll.viewport().setStyleSheet('QWidget#scrollViewport { background: transparent; }')
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 3, 0)
        body.setSpacing(5)
        self.scroll.setWidget(self.body)
        page.addWidget(self.scroll, 1)
        overview, layout = self._card()
        content = QHBoxLayout()
        self.ring = Meter(ring=True)
        self.ring.setToolTip('两组额度中较低的剩余比例')
        self.ring.track, self.ring.text = self.colors['border'], self.colors['text']
        ring_column = QVBoxLayout()
        ring_column.setSpacing(1)
        ring_column.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignCenter)
        content.addLayout(ring_column)
        readings = QVBoxLayout()
        readings.setSpacing(3)
        self.window_widgets = {}
        for kind, title, color in ((WINDOW_WEEKLY, '7d · 每周额度', self.colors['weekly']), (WINDOW_5H, '5h · 5 小时额度', self.colors['five_hour'])):
            group = QVBoxLayout()
            group.setSpacing(1)
            row = QHBoxLayout()
            window_title = self._label(title, muted=True)
            row.addWidget(window_title)
            percent = self._label('—')
            percent.setObjectName('percent')
            percent.setStyleSheet(f'color: {color};')
            row.addStretch()
            row.addWidget(percent)
            group.addLayout(row)
            bar = Meter()
            bar.setToolTip('彩色：实际剩余；白线：按窗口剩余时间计算的理想余量')
            bar.track, bar.text = self.colors['border'], self.colors['text']
            group.addWidget(bar)
            tokens = self._label('等待 token 样本', muted=True)
            tokens.setProperty('tokenValue', True)
            group.addWidget(tokens)
            readings.addLayout(group)
            self.window_widgets[kind] = dict(percent=percent, bar=bar, title=window_title, tokens=tokens, color=color)
        content.addLayout(readings, 1)
        layout.addLayout(content)
        body.addWidget(overview)
        self.pace_label = self._label('等待同步')
        self.pace_label.setStyleSheet(f'color: {self.colors["accent"]}; font-weight: 700;')
        ring_column.addWidget(self.pace_label, 0, Qt.AlignmentFlag.AlignCenter)
        self.session_label = self._label('本会话 —\n命中 —', muted=True)
        self.session_label.setProperty('tokenValue', True)
        self.session_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ring_column.addWidget(self.session_label, 0, Qt.AlignmentFlag.AlignCenter)
        self.pace_details = self._label('', muted=True, wrap=True)
        self.pace_details.setParent(overview)
        self.pace_details.hide()
        self.prediction_card, pred_layout = self._card()
        pred_header = QHBoxLayout()
        pred_header.addWidget(self._label('在线预测', heading=True))
        pred_header.addStretch()
        self.prediction_status = self._label('等待同步', muted=True)
        pred_header.addWidget(self.prediction_status)
        pred_layout.addLayout(pred_header)
        self.forecast_widgets = {}
        for kind, title in ((ev.RESET, '重置'), (ev.CARD_GRANT, '发卡')):
            label = self._label(title, muted=True)
            row = QHBoxLayout()
            row.setSpacing(7)
            row.addWidget(label)
            value = self._label('正在获取…', heading=True, wrap=True)
            row.addWidget(value, 1)
            badge = self._label('历史推算', muted=True)
            row.addWidget(badge)
            pred_layout.addLayout(row)
            note = self._label('', muted=True, wrap=True)
            pred_layout.addWidget(note)
            self.forecast_widgets[kind] = dict(value=value, note=note, badge=badge)
        self.community_stats = self._label('', muted=True, wrap=True)
        pred_layout.addWidget(self.community_stats)
        self.community_latest = self._label('', muted=True, wrap=True)
        self.community_latest.setParent(self.prediction_card)
        self.community_latest.hide()
        self.community_warning = self._label('', muted=True, wrap=True)
        self.community_warning.setStyleSheet(f'color: {self.colors["warn"]}; font-size: 11px;')
        pred_layout.addWidget(self.community_warning)
        body.addWidget(self.prediction_card)
        self.credits_card, credits_layout = self._card()
        credits_header = QHBoxLayout()
        credits_header.addWidget(self._label('重置卡', heading=True))
        credits_header.addStretch()
        self.credit_count = self._label('等待同步', muted=True)
        credits_header.addWidget(self.credit_count)
        credits_layout.addLayout(credits_header)
        self.credit_rows = QVBoxLayout()
        self.credit_rows.setSpacing(1)
        credits_layout.addLayout(self.credit_rows)
        body.addWidget(self.credits_card)
        self.advice_title = self._label('同步后生成用卡建议', muted=True, wrap=True)
        credits_layout.addWidget(self.advice_title)
        self.advice_details = self._label('', muted=True, wrap=True)
        self.advice_details.setParent(self.credits_card)
        self.advice_details.hide()
        body.addStretch()
        self.hud = QWidget()
        hud_layout = QHBoxLayout(self.hud)
        hud_layout.setContentsMargins(0, 0, 0, 0)
        hud_layout.setSpacing(18)
        self.hud_widgets = {}
        for kind, title, color in ((WINDOW_WEEKLY, '7d', self.colors['weekly']), (WINDOW_5H, '5h', self.colors['five_hour'])):
            group = QVBoxLayout()
            group.setSpacing(2)
            row = QHBoxLayout()
            row.addWidget(self._label(title, heading=True))
            percent = self._label('—')
            percent.setObjectName('percent')
            percent.setStyleSheet(f'color:{color};')
            row.addStretch()
            row.addWidget(percent)
            bar = Meter()
            bar.track, bar.text = self.colors['border'], self.colors['text']
            group.addLayout(row)
            group.addWidget(bar)
            tokens = self._label('等待 token 样本', muted=True)
            tokens.setProperty('tokenValue', True)
            group.addWidget(tokens)
            hud_layout.addLayout(group, 1)
            self.hud_widgets[kind] = dict(percent=percent, bar=bar, tokens=tokens, color=color)
        page.addWidget(self.hud)
        self.hud_note = self._label('', muted=True)
        page.addWidget(self.hud_note)
        footer = QHBoxLayout()
        self.refresh_button = self._button('刷新', lambda: self.refresh('button'))
        self.history_button = self._button('记录', self.show_history)
        self.settings_button = self._button('设置', self.show_settings)
        self.reset_button = self._button('使用重置卡', self.reset_credit, primary=True)
        self.reset_button.setEnabled(False)
        for button in (self.refresh_button, self.history_button, self.settings_button):
            footer.addWidget(button)
        footer.addStretch()
        footer.addWidget(self.reset_button)
        self.footer = QWidget()
        self.footer.setLayout(footer)
        footer.setContentsMargins(0, 0, 0, 0)
        page.addWidget(self.footer)
        self.status = self._label('● 准备同步', muted=True)
        page.addWidget(self.status)

    def _apply_mode(self):
        compact = self.settings.compact
        self.scroll.setVisible(not compact)
        self.footer.setVisible(not compact)
        self.account_label.setVisible(not compact)
        self.hud.setVisible(compact)
        self.hud_note.setVisible(compact)
        self.mode_button.setText('展开' if compact else 'HUD')
        self.pin_button.setText('已置顶' if self.settings.always_on_top else '置顶')
        self.root.setMinimumSize(360, 165 if compact else 380)
        height = 170 if compact else min(460, self.qt.primaryScreen().availableGeometry().height()-40)
        self.root.resize(390, height)
        self._update_countdowns()
        QTimer.singleShot(0, self.input_layer.sync)

    def toggle_compact(self):
        self.settings.compact = not self.settings.compact
        self.settings.save()
        self._apply_mode()

    def toggle_topmost(self):
        self.settings.always_on_top = not self.settings.always_on_top
        self.settings.save()
        self._apply_window_mode()

    def toggle_taskbar(self):
        self.settings.hide_taskbar = not self.settings.hide_taskbar
        self.settings.save()
        self._apply_window_mode()

    def toggle_click_through(self):
        self.settings.click_through = not self.settings.click_through
        self._move_mode = False
        self.settings.save()
        self._apply_window_mode()

    def toggle_move(self):
        self._move_mode = not self._move_mode
        self._apply_window_mode()
        self.show_window()

    def escape_window(self):
        self.toggle_move() if self._move_mode else self.hide_to_tray()

    def _status(self, text, kind='muted'):
        self._status_kind = kind
        self.status.setText('● ' + text)
        self.status.setStyleSheet(f'color: {self.colors.get(kind, self.colors["muted"])}; font-size: 11px;')

    def _set_banner(self, text=''):
        self.banner.setText(text)
        self.banner.setVisible(bool(text))

    def _render(self):
        snap = self.bundle.snapshot
        self.account_label.setText(f'{snap.account.email or "当前 Codex 账号"}  ·  {(snap.account.plan_type or "未知套餐").capitalize()}')
        values = []
        for kind, widgets in self.window_widgets.items():
            window = snap.window(kind)
            percent = window.remaining_percent if window else 0
            ideal = self._ideal(window)
            widgets['percent'].setText(f'{percent:.0f}%' if window else '—')
            widgets['bar'].set_value(percent, ideal, widgets['color'])
            hud = self.hud_widgets[kind]
            hud['percent'].setText(widgets['percent'].text())
            hud['bar'].set_value(percent, ideal, hud['color'])
            estimate = self.bundle.tokens.windows.get(kind)
            token_text = self._token_text(estimate)
            widgets['tokens'].setText(token_text)
            hud['tokens'].setText(self._token_text(estimate, hud=True))
            detail = self._token_detail(estimate)
            widgets['tokens'].setToolTip(detail)
            hud['percent'].setToolTip(token_text+'\n'+detail)
            hud['tokens'].setToolTip(token_text+'\n'+detail)
            if window:
                values.append(percent)
        minimum = min(values) if values else 0
        self.ring.set_value(minimum, color=self.colors['danger'] if minimum < 10 else self.colors['warn'] if minimum < 30 else self.colors['accent'])
        while self.credit_rows.count():
            child = self.credit_rows.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self.credit_count.setText(f'{len(snap.credits)} 张可用' if snap.credits_supported else '暂不可读')
        for credit in snap.sorted_credits()[:2]:
            days = credit.days_left()
            row = self._label(f'#{credit.credit_id[-8:]}   {time.strftime("%m-%d", time.localtime(credit.expires_at)) if credit.expires_at else "未标注"} 到期'
                              + (f' · {max(0, days):.1f} 天' if days is not None else ''), muted=True)
            row.setToolTip(f'卡片 {credit.credit_id}\n到期：{ana.fmt_full_clock(credit.expires_at)}')
            self.credit_rows.addWidget(row)
        if not snap.credits:
            self.credit_rows.addWidget(self._label('没有可用卡片' if snap.credits_supported else '卡片列表需要实时接口', muted=True))
        self.credit_count.setToolTip('显示到期最早的两张，点击使用重置卡查看全部卡片')
        self.advice_title.setText(self.bundle.advice.headline or '等待同步')
        lines = [line for line in self.bundle.advice.lines if not line.startswith(('决策对象', '预估下次社区', '官方已宣布'))]
        self.advice_details.setText('\n'.join(lines[:2]))
        self.advice_title.setToolTip('\n'.join([self.bundle.advice.headline, *lines]))
        weekly = snap.window(WINDOW_WEEKLY)
        ideal = self._ideal(weekly)
        if weekly and ideal is not None:
            delta = weekly.remaining_percent-ideal
            state = '余量充足' if delta > 12 else '建议放缓' if delta < -12 else '节奏正常'
            if minimum < 10:
                state = '接近耗尽'
            self.pace_label.setText(state)
            self.pace_details.setText(f'每周实际剩余 {weekly.remaining_percent:.0f}%  ·  理想 {ideal:.0f}%  ·  偏差 {delta:+.0f}%')
        else:
            self.pace_label.setText('无法判断')
            self.pace_details.setText('等待窗口长度与重置时间')
        self.pace_label.setToolTip(self.pace_details.text())
        self._update_pace_color()
        self.reset_button.setEnabled(bool(snap.ok and snap.source == 'api' and snap.credits and not self.controller.busy))
        self._render_community()
        self._update_countdowns()
        self._set_banner(snap.error or ('本地快照，剩余额度可能已变化' if snap.source == 'local' else ''))
        session = self.bundle.tokens.session
        self.session_label.setText('本会话 '+compact_tokens(session.observed_tokens if session else None)+
                                   '\n命中 '+self._cache_rate(session))
        self.session_label.setToolTip(self._session_detail())
        self._update_token_usage()

    def _ideal(self, window):
        if not window or not window.resets_at:
            return None
        minutes = window.window_minutes or (300 if window.kind == WINDOW_5H else 10080)
        return max(0., min(100., (window.resets_at-time.time())/(minutes*60)*100))

    def _token_detail(self, estimate):
        if estimate is None:
            return '本机日志尚无可用 token 样本，额度百分比无法直接换算成固定 tokens'
        total = f'{estimate.observed_tokens:,}' if estimate.observed_tokens is not None else '—'
        detail = (f'本机已用：{total} tokens / {estimate.observed_calls:,} 条用量记录\n'
                  f'总输入：{estimate.input_tokens:,}\n'
                  f'  缓存命中输入：{estimate.cached_input_tokens:,}\n'
                  f'  缓存未命中输入：{estimate.uncached_input_tokens:,}\n'
                  f'输出：{estimate.output_tokens:,}\n'
                  f'缓存命中率：{self._cache_rate(estimate)}（命中输入 ÷ 总输入）\n'
                  '合计 = 总输入 + 输出；命中与未命中合计为总输入。\n'+estimate.note)
        if self.settings.show_token_value:
            value = estimate_value(estimate, self.settings.pricing_model)
            detail += (f'\n\nAPI 等值估算 · USD\n{value.basis}\n'
                       f'本机可计价 {"≥" if value.unpriced_tokens else "≈"}{money(value.observed_usd)} · 预计已用 {money(value.used_usd)}'
                       f' · 预计剩余 {money(value.remaining_usd)}\n'+value.note)
        return detail

    @staticmethod
    def _cache_rate(estimate):
        rate = estimate.cache_hit_rate if estimate else None
        return f'{rate:.1%}' if rate is not None else '—'

    def _session_detail(self):
        report = self.bundle.tokens
        return ('本会话 = 最近活动的本机 Codex 会话（按日志中的活动时间选择）\n'
                f'会话 ID：{report.session_id or "尚未发现"}\n'
                f'最近活动：{ana.fmt_full_clock(report.session_activity_at or None)}\n\n'+
                self._token_detail(report.session))

    def show_token_usage(self):
        dialog, fresh = self._dialog('usage', 'Tokens 用量明细', 580, 510)
        if not fresh:
            self._update_token_usage()
            return
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addWidget(self._label('Tokens 用量与缓存', heading=True))
        dialog.tabs = QTabWidget()
        dialog.pages = {}
        for key, title in (('session', '本会话'), (WINDOW_5H, '5 小时'), (WINDOW_WEEKLY, '每周')):
            page = QPlainTextEdit()
            page.setReadOnly(True)
            dialog.tabs.addTab(page, title)
            dialog.pages[key] = page
        layout.addWidget(dialog.tabs, 1)
        row = QHBoxLayout()
        dialog.scan_button = self._button('更新本机统计', self._scan_history)
        row.addWidget(dialog.scan_button)
        row.addStretch()
        row.addWidget(self._button('关闭', dialog.close))
        layout.addLayout(row)
        dialog.show()
        self._update_token_usage()

    def _update_token_usage(self):
        dialog = self._dialogs.get('usage')
        if not dialog or not dialog.isVisible():
            return
        dialog.pages['session'].setPlainText(self._session_detail())
        for kind in (WINDOW_5H, WINDOW_WEEKLY):
            estimate = self.bundle.tokens.windows.get(kind)
            dialog.pages[kind].setPlainText(self._token_text(estimate).replace('\n', ' / ')+
                                          '\n\n'+self._token_detail(estimate))
        dialog.scan_button.setEnabled(not self._history_busy)
        dialog.scan_button.setText('扫描中…' if self._history_busy else '更新本机统计')

    def _token_text(self, estimate, *, hud=False):
        predicted = estimate is not None and estimate.estimated_remaining is not None
        observed = estimate.observed_tokens if estimate else None
        used = estimate.estimated_used if predicted else observed
        remaining = estimate.estimated_remaining if predicted else None
        used_label = ('用≈' if hud else '已用≈') if predicted else ('本机 ' if hud else '本机已用 ')
        if used is None:
            used_label = '已用 —' if not hud else '用 —'
            first = used_label
        else:
            first = used_label+compact_tokens(used)
        second = ('余≈' if predicted else '余 ')+compact_tokens(remaining)
        if self.settings.show_token_value:
            value = estimate_value(estimate, self.settings.pricing_model)
            first += (' · ≥' if value.used_is_lower_bound else ' · ≈')+money(value.used_usd, compact=True) if value.used_usd is not None else ' · $—'
            second += ' · ≈'+money(value.remaining_usd, compact=True) if value.remaining_usd is not None else ' · $—'
            return first+'\n'+second
        return first+' · '+second+('' if hud else ' tokens')

    def _render_community(self):
        snap = self.bundle.community
        enabled = self.settings.community_events
        usable = bool(enabled and snap and snap.ok and snap.source != 'baseline')
        self.prediction_status.setText('已关闭' if not enabled else '实时 API' if usable and snap.source == 'live' else '缓存' if usable else '暂不可用')
        for kind, widgets in self.forecast_widgets.items():
            forecast = (snap.next_reset if kind == ev.RESET else snap.next_card) if usable else None
            if not forecast or not forecast.next_event_ts:
                widgets['value'].setText('设置中启用在线预测' if not enabled else '暂无可用预测')
                widgets['note'].setText('已预告，尚未给出时间' if usable and snap.pending_labels else '等待预测数据')
                widgets['badge'].setText('—')
                continue
            source = forecast.source
            widgets['badge'].setText('模型预测' if source == 'aihot_estimate' else '原帖预告' if source == 'announcement' else '历史推算')
            widgets['value'].setText(ev.fmt_local_window(forecast.window_start, forecast.window_end))
            overdue = bool(forecast.window_end and forecast.window_end < time.time())
            widgets['note'].setText('预测时段已过，等待新公告' if overdue else
                ev.fmt_countdown(forecast.next_event_ts) + (' · 预告时间' if source == 'announcement' else ' · 统计参考，不保证发生' if source != 'aihot_estimate' else ' · 模型估计时段'))
        if usable:
            verified = time.strftime('%m-%d %H:%M', time.localtime(snap.api_checked_at)) if snap.api_checked_at else '未知'
            self.community_stats.setText(f'90天 重置{snap.reset_count_90d} / 发卡{snap.card_count_90d} · {verified}')
            latest = sorted(snap.events, key=lambda event: event.ts or 0, reverse=True)
            self.community_latest.setText(prediction_text('最近：' + '  /  '.join(f'{event.date[5:]} {event.label}' for event in latest[:2])))
            self.community_warning.setText(prediction_text('；'.join(snap.warnings[:2])))
        else:
            self.community_stats.setText('社区监控，独立于个人额度自然重置')
            self.community_latest.setText('')
            self.community_warning.setText(prediction_text('；'.join(snap.warnings[:1])) if snap and enabled else '')
        warning = self.community_warning.text()
        self.community_warning.hide()
        if warning:
            stale = bool(snap and snap.api_checked_at and time.time()-snap.api_checked_at > 86400)
            self.prediction_status.setText(self.prediction_status.text()+(' · 较旧' if stale else ' · 提示'))
        self.prediction_status.setStyleSheet(f'color: {self.colors["warn" if warning else "muted"]};')
        self.prediction_status.setToolTip(warning)
        self.prediction_card.setToolTip(self.community_latest.text()+'\n'+warning)
        self._update_hud_note()

    def _update_hud_note(self):
        snap = self.bundle.community
        forecast = snap.next_reset if snap and snap.ok else None
        text = ev.fmt_local_window(forecast.window_start, forecast.window_end) if forecast and forecast.next_event_ts else '等待在线预测'
        session = self.bundle.tokens.session
        summary = ('会话 '+compact_tokens(session.observed_tokens if session else None)+
                   ' · 命中 '+self._cache_rate(session)+'  |  重置 '+text)
        self.hud_note.setText(self.hud_note.fontMetrics().elidedText(
            summary, Qt.TextElideMode.ElideRight, max(200, self.root.width()-32)))
        self.hud_note.setToolTip(summary+'\n\n'+self._session_detail())

    def _update_countdowns(self):
        for kind, widgets in self.window_widgets.items():
            window = self.bundle.snapshot.window(kind)
            delta = window.seconds_until_reset() if window else None
            prefix = '7d' if kind == WINDOW_WEEKLY else '5h'
            widgets['title'].setText(prefix+' · '+('等待同步' if not window else '重置时间未知' if delta is None else
                '等待重置' if delta <= 0 else ana.fmt_duration(delta).replace(' ', '')+'后重置'))
            widgets['title'].setToolTip(('每周额度' if kind == WINDOW_WEEKLY else '5 小时额度')+'\n自然重置：'+ana.fmt_full_clock(window.resets_at if window else None))

    def _pump(self):
        if self._closed:
            return
        for _ in range(50):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._safe(lambda event=event: self._handle_event(event))

    def _handle_event(self, event):
        kind = event.get('type')
        if kind == 'tokens_done':
            self.bundle.tokens = event['tokens']
            self._render()
            self._update_history()
        elif kind == 'refresh_started':
            self._status('正在同步…', 'accent')
        elif kind == 'refresh_done':
            bundle = event['bundle']
            if bundle.snapshot.ok or not self.bundle.snapshot.ok:
                self.bundle, self.samples = bundle, bundle.samples
            elif bundle.community:
                self.bundle.community = bundle.community
            self._render()
            self.refresh_button.setEnabled(True)
            self.header_refresh.setEnabled(True)
            self.refresh_button.setText('刷新')
            self._status(('实时额度' if bundle.snapshot.source == 'api' and bundle.snapshot.ok else
                          '本地快照' if bundle.snapshot.ok else '同步失败') + ' · ' + time.strftime('%H:%M:%S'),
                         'accent' if bundle.snapshot.ok else 'warn')
            if not bundle.snapshot.ok:
                self._set_banner(bundle.snapshot.error or '同步失败，保留上次数据')
            self._update_tray()
            self._notify_warnings()
        elif kind in ('history_done', 'history_failed'):
            self._history_busy = False
            if kind == 'history_done':
                self.samples = event.get('samples', [])
                if event.get('tokens') is not None:
                    self.bundle.tokens = event['tokens']
                    self._render()
            else:
                self._set_banner('扫描失败：' + event.get('error', ''))
            self._update_history()
            self._update_token_usage()
        elif kind == 'reset_note':
            self._status(event.get('text', ''), 'accent')
        elif kind == 'reset_done':
            dialog = self._dialogs.get('reset')
            if dialog and dialog.isVisible():
                dialog.result_label.setText(('成功：' if event.get('ok') else '失败：') + event.get('message', ''))
                dialog.confirm_button.setEnabled(False)
            QTimer.singleShot(150, lambda: self.refresh('after-reset'))

    def _tick(self):
        if self._closed:
            return
        if self.codex_exit_signal.requested():
            log('收到 Codex 退出信号，保存偏好并退出')
            self.quit_app()
            return
        if consume_show_request():
            self.show_window()
        self._update_countdowns()
        if time.monotonic() >= self._next_token_refresh:
            self._next_token_refresh = time.monotonic()+15
            refresh_tokens = getattr(self.controller, 'refresh_tokens_async', None)
            if refresh_tokens:
                refresh_tokens()
        if self.settings.refresh_seconds and time.monotonic() >= self._next_refresh:
            self._next_refresh = time.monotonic()+self.settings.refresh_seconds
            self.refresh('auto')

    def refresh(self, reason='manual'):
        if not self._closed and self.controller.refresh_async(reason):
            self.refresh_button.setEnabled(False)
            self.header_refresh.setEnabled(False)
            self.refresh_button.setText('同步中')
            self.reset_button.setEnabled(False)

    def _dialog(self, key, title, width, height):
        old = self._dialogs.get(key)
        if old and old.isVisible():
            old.raise_()
            old.activateWindow()
            return old, False
        if old:
            old.deleteLater()
        dialog = GlassDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(width, height)
        area = self.qt.primaryScreen().availableGeometry()
        dialog.move(max(area.x(), min(area.right()-width, self.root.x()+(self.root.width()-width)//2)),
                    max(area.y(), min(area.bottom()-height, self.root.y()+(self.root.height()-height)//2)))
        self._dialogs[key] = dialog
        return dialog, True

    def show_settings(self):
        dialog, fresh = self._dialog('settings', '设置 · Codex 额度', 420, 640)
        if not fresh:
            return
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(8)
        layout.addWidget(self._label('设置', heading=True))
        dialog.tabs = QTabWidget()
        layout.addWidget(dialog.tabs, 1)
        def tab_page(title):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.viewport().setStyleSheet('background: transparent;')
            page = QWidget()
            page.setStyleSheet('background: transparent;')
            scroll.setWidget(page)
            body = QVBoxLayout(page)
            body.setContentsMargins(0, 8, 7, 0)
            body.setSpacing(7)
            dialog.tabs.addTab(scroll, title)
            return body
        appearance_layout = tab_page('外观')
        general_layout = tab_page('常规')
        pricing_layout = tab_page('价格')
        dialog.value_check = QCheckBox('在已用 / 剩余 tokens 后显示美元等值')
        dialog.value_check.setChecked(self.settings.show_token_value)
        pricing_layout.addWidget(dialog.value_check)
        pricing_layout.addWidget(self._label('折算模型', heading=True))
        dialog.pricing_combo = QComboBox()
        dialog.pricing_combo.addItem('自动 · 按日志模型分别计价', 'auto')
        for model in PRICES:
            dialog.pricing_combo.addItem(model, model)
        dialog.pricing_combo.setCurrentIndex(dialog.pricing_combo.findData(self.settings.pricing_model))
        pricing_layout.addWidget(dialog.pricing_combo)
        dialog.price_details = self._label('', muted=True, wrap=True)
        pricing_layout.addWidget(dialog.price_details)
        def update_price_details():
            model = dialog.pricing_combo.currentData()
            price = PRICES.get(model)
            text = (f'{model}\n每百万 tokens：输入 ${price.input:g} · 缓存输入 ${price.cached_input:g}'
                    f' · 输出 ${price.output:g}\n' if price else '自动读取本机日志的模型，混用模型分别计值。\n')
            if price and price.cache_write is not None:
                text += f'缓存写入 ${price.cache_write:g} / 百万 tokens。\n'
            text += ('美元 USD · Standard API 价格。\n价值属于 API 等值估算，不是订阅账单。\n'
                     '已用与剩余预测沿用本机输入、缓存、输出比例；'
                     '没有可定价样本时显示 $—；有部分未知模型时，预测沿用已识别模型的输入 / 缓存 / 输出比例，'
                     '本机观测价值用 ≥ 标明可计价部分。\n'
                     '超过 272K 输入的请求按已核验的长上下文倍率计算。'
                     '日志缺少缓存写入分类时按普通输入估算；不含 Fast、地区附加费和工具费用。\n'
                     f'官方价格核验：{PRICING_CHECKED}\n{PRICING_URL}')
            dialog.price_details.setText(text)
        dialog.pricing_combo.currentIndexChanged.connect(update_price_details)
        update_price_details()
        pricing_layout.addStretch()
        def combo_row(label, combo):
            row = QHBoxLayout()
            row.addWidget(self._label(label, muted=True))
            row.addWidget(combo, 1)
            appearance_layout.addLayout(row)
        dialog.theme_combo = QComboBox()
        for name, value in (('深色', 'dark'), ('浅色', 'light'), ('跟随系统', 'auto')):
            dialog.theme_combo.addItem(name, value)
        dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData(self.settings.theme))
        combo_row('主题', dialog.theme_combo)
        dialog.material_combo = QComboBox()
        dialog.material_combo.addItem('透明面板（可调不透明度）', 'transparent')
        dialog.material_combo.addItem('液态玻璃 / 亚克力', 'glass')
        dialog.material_combo.addItem('实色面板', 'solid')
        dialog.material_combo.setCurrentIndex(dialog.material_combo.findData(self.settings.material))
        combo_row('材质', dialog.material_combo)
        dialog.transparent_label = self._label('', muted=True)
        appearance_layout.addWidget(dialog.transparent_label)
        dialog.transparent_slider = QSlider(Qt.Orientation.Horizontal)
        dialog.transparent_slider.setRange(0, 100)
        dialog.transparent_slider.setValue(round(self.settings.transparent_opacity * 100))
        appearance_layout.addWidget(dialog.transparent_slider)
        dialog.tint_label = self._label('', muted=True)
        appearance_layout.addWidget(dialog.tint_label)
        dialog.tint_slider = QSlider(Qt.Orientation.Horizontal)
        dialog.tint_slider.setRange(35, 95)
        dialog.tint_slider.setValue(round(self.settings.opacity*100))
        appearance_layout.addWidget(dialog.tint_slider)
        appearance_layout.addWidget(self._label('文字配色 · 选色或输入 #RRGGBB', heading=True))
        dialog.preview = replace(self.settings, custom_colors=dict(self.settings.custom_colors))
        dialog.color_overrides = dict(self.settings.custom_colors)
        dialog.color_inputs, dialog.color_buttons = {}, {}
        color_grid = QGridLayout()
        color_grid.setHorizontalSpacing(10)
        color_grid.setVerticalSpacing(5)
        appearance_layout.addLayout(color_grid)
        def preview_appearance():
            dialog.preview.theme = dialog.theme_combo.currentData()
            dialog.preview.material = dialog.material_combo.currentData()
            dialog.preview.opacity = dialog.tint_slider.value() / 100
            dialog.preview.transparent_opacity = dialog.transparent_slider.value() / 100
            dialog.preview.custom_colors = dict(dialog.color_overrides)
            self._appearance_preview = dialog.preview
            self._choose_theme()
            material = dialog.preview.material
            dialog.transparent_label.setText(f'背景不透明度：{dialog.transparent_slider.value()}%（0% 纯透明，100% 实色）')
            dialog.tint_label.setText(f'玻璃底色浓度：{dialog.tint_slider.value()}%')
            for widget in (dialog.transparent_label, dialog.transparent_slider):
                widget.setEnabled(material == 'transparent')
            for widget in (dialog.tint_label, dialog.tint_slider):
                widget.setEnabled(material == 'glass')
        def update_swatch(role, value):
            dialog.color_buttons[role].setStyleSheet(f'background: {value}; border: 1px solid {self.colors["border"]}; border-radius: 7px;')
        def edit_color(role, value):
            if re.fullmatch(r'#[0-9a-fA-F]{6}', value):
                dialog.color_overrides[role] = value.upper()
                update_swatch(role, value)
                preview_appearance()
        def choose_color(role):
            old = getattr(dialog, 'color_picker', None)
            if old and old.isVisible():
                old.raise_()
                old.activateWindow()
                return
            picker = dialog.color_picker = QColorDialog(QColor(self.colors[role]), dialog)
            picker.setWindowTitle('选择文字颜色')
            picker.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
            picker.setStyleSheet(f'QColorDialog {{ background: {self.colors["card"]}; }}')
            def selected(color):
                value = color.name().upper()
                dialog.color_inputs[role].setText(value)
                edit_color(role, value)
            picker.colorSelected.connect(selected)
            picker.open()
        for index, (role, label) in enumerate(COLOR_ROLES):
            cell = QVBoxLayout()
            cell.setSpacing(2)
            cell.addWidget(self._label(label, muted=True))
            row = QHBoxLayout()
            row.setSpacing(4)
            field = QLineEdit(self.colors[role])
            field.setMaxLength(7)
            field.setStyleSheet('padding: 4px 6px; font-size: 11px;')
            field.textEdited.connect(lambda value, role=role: edit_color(role, value))
            dialog.color_inputs[role] = field
            row.addWidget(field, 1)
            button = self._button('', lambda role=role: choose_color(role), tooltip='打开颜色选择器')
            button.setFixedSize(26, 26)
            dialog.color_buttons[role] = button
            update_swatch(role, self.colors[role])
            row.addWidget(button)
            cell.addLayout(row)
            color_grid.addLayout(cell, index // 2, index % 2)
        def refresh_default_colors():
            defaults, _ = theme_colors(dialog.theme_combo.currentData())
            for role, _ in COLOR_ROLES:
                value = dialog.color_overrides.get(role, defaults[role])
                dialog.color_inputs[role].setText(value)
                update_swatch(role, value)
            preview_appearance()
        def reset_colors():
            dialog.color_overrides.clear()
            refresh_default_colors()
        dialog.reset_colors_button = self._button('恢复主题默认配色', reset_colors)
        appearance_layout.addWidget(dialog.reset_colors_button)
        appearance_layout.addWidget(self._label('实时预览；保存后记住设置，取消恢复原样。\n不透明度只改变背景，文字保持清晰。', muted=True))
        appearance_layout.addStretch()
        dialog.theme_combo.currentIndexChanged.connect(refresh_default_colors)
        dialog.material_combo.currentIndexChanged.connect(preview_appearance)
        dialog.transparent_slider.valueChanged.connect(preview_appearance)
        dialog.tint_slider.valueChanged.connect(preview_appearance)
        preview_appearance()
        def end_preview(result):
            self._appearance_preview = None
            self._choose_theme()
        dialog.finished.connect(end_preview)

        general_layout.addWidget(self._label('自动刷新秒数：30～3600，0 关闭', muted=True))
        dialog.interval_input = QLineEdit(str(self.settings.refresh_seconds))
        general_layout.addWidget(dialog.interval_input)
        dialog.variables = {}
        for name, label in (('always_on_top', '窗口置顶'), ('hide_taskbar', '悬浮显示，仅留托盘（隐藏任务栏图标）'),
                ('click_through', '仅按钮可点击，其他区域穿透到下方软件'),
                ('minimize_to_tray', '关闭窗口时收进托盘'), ('start_minimized', '手动启动时直接进入托盘'),
                ('compact', '默认使用紧凑 HUD'), ('use_api', '读取 Codex 实时额度'),
                ('local_fallback', '联网失败时使用本地快照'), ('community_events', '在线预测（5 分钟轮询缓存）'),
                ('notify_on_expiry', '额度不足与卡片临期时提醒')):
            check = QCheckBox(label)
            check.setChecked(getattr(self.settings, name))
            dialog.variables[name] = check
            general_layout.addWidget(check)
        general_layout.addWidget(self._label('仅留托盘时窗口仍可显示和置顶。\n单击托盘图标恢复，右键可打开设置或退出。', muted=True))
        general_layout.addStretch()
        dialog.error_label = self._label('', wrap=True)
        dialog.error_label.setStyleSheet(f'color: {self.colors["warn"]};')
        layout.addWidget(dialog.error_label)
        def save():
            try:
                interval = int(dialog.interval_input.text())
                if interval != 0 and not 30 <= interval <= 3600:
                    raise ValueError()
            except ValueError:
                dialog.tabs.setCurrentIndex(1)
                dialog.error_label.setText('请输入 30～3600 的整数，或 0')
                return
            for role, label in COLOR_ROLES:
                value = dialog.color_inputs[role].text()
                if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
                    dialog.tabs.setCurrentIndex(0)
                    dialog.error_label.setText(f'{label}请输入 #RRGGBB 格式的颜色')
                    dialog.color_inputs[role].setFocus()
                    return
                default = theme_colors(dialog.theme_combo.currentData())[0][role]
                if role in dialog.color_overrides or value.upper() != default.upper():
                    dialog.color_overrides[role] = value.upper()
            needs_refresh = (interval != self.settings.refresh_seconds or any(
                dialog.variables[name].isChecked() != getattr(self.settings, name)
                for name in ('use_api', 'local_fallback', 'community_events')))
            self.settings.refresh_seconds = interval
            self.settings.theme = dialog.theme_combo.currentData()
            self.settings.material = dialog.material_combo.currentData()
            self.settings.opacity = dialog.tint_slider.value()/100
            self.settings.transparent_opacity = dialog.transparent_slider.value()/100
            self.settings.custom_colors = dict(dialog.color_overrides)
            self.settings.show_token_value = dialog.value_check.isChecked()
            self.settings.pricing_model = dialog.pricing_combo.currentData()
            for name, check in dialog.variables.items():
                setattr(self.settings, name, check.isChecked())
            self.settings.save()
            self._next_refresh = time.monotonic()+(interval or 180)
            dialog.accept()
            self._apply_window_mode()
            self._apply_mode()
            self._render()
            self.show_window()
            if needs_refresh:
                self.refresh('settings')
        row = QHBoxLayout()
        row.addWidget(self._button('数据目录', self._open_data_dir))
        row.addStretch()
        dialog.cancel_button = self._button('取消', dialog.reject)
        row.addWidget(dialog.cancel_button)
        dialog.save_button = self._button('保存', save, primary=True)
        row.addWidget(dialog.save_button)
        layout.addLayout(row)
        dialog.show()

    def show_history(self):
        dialog, fresh = self._dialog('history', '记录 · Codex 额度', 640, 540)
        if not fresh:
            return
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(self._label('额度记录与在线预测详情', heading=True))
        dialog.text = QPlainTextEdit()
        dialog.text.setReadOnly(True)
        layout.addWidget(dialog.text, 1)
        row = QHBoxLayout()
        dialog.scan_button = self._button('重新扫描记录', self._scan_history)
        row.addWidget(dialog.scan_button)
        row.addStretch()
        row.addWidget(self._button('关闭', dialog.close))
        layout.addLayout(row)
        dialog.show()
        self._update_history()

    def _update_history(self):
        dialog = self._dialogs.get('history')
        if not dialog or not dialog.isVisible():
            return
        snap = self.bundle.snapshot
        lines = [f'当前数据：{self.bundle.source_note}', f'近 14 天采样：{len(self.samples)} 条', '']
        for window in snap.windows:
            lines.extend([f'{window.title()}：剩余 {window.remaining_percent:.0f}%', f'自然重置：{ana.fmt_full_clock(window.resets_at)}', ''])
        lines.extend(['Tokens 用量与预测', self._session_detail(), ''])
        for window in snap.windows:
            estimate = self.bundle.tokens.windows.get(window.kind)
            lines.append(window.title()+'：'+self._token_text(estimate).replace('\n', ' / '))
            lines.extend([self._token_detail(estimate), ''])
        for credit in snap.sorted_credits():
            lines.append(f'#{credit.credit_id[-8:]} · 发放 {ana.fmt_full_clock(credit.granted_at)} · 到期 {ana.fmt_full_clock(credit.expires_at)}')
        lines.extend(['', '用卡建议', self.bundle.advice.headline, *self.bundle.advice.lines, '', '在线预测'])
        community = self.bundle.community
        if community:
            lines.extend([prediction_text(f'数据状态：{community.source}'), f'核验时间：{community.updated_text}',
                          prediction_text(f'监控状态：{community.monitor_status}'), *map(prediction_text, community.warnings)])
            for title, forecast in (('全员重置', community.next_reset), ('发重置卡', community.next_card)):
                if forecast:
                    lines.extend([title + '：' + ev.fmt_local_window(forecast.window_start, forecast.window_end), *map(prediction_text, forecast.bases)])
            lines.extend(['', '最近事件（时间为确认时间或核验日期，非精确到账时间）'])
            lines.extend(prediction_text(f'{event.date} · {event.label} · {event.scope or "范围未明确"}') for event in reversed(community.events[-10:]))
        lines.extend(['', '诊断信息', *snap.warnings])
        dialog.text.setPlainText('\n'.join(lines))
        dialog.scan_button.setText('扫描中…' if self._history_busy else '重新扫描记录')
        dialog.scan_button.setEnabled(not self._history_busy)

    def _scan_history(self):
        if not self._history_busy:
            self._history_busy = True
            self._update_history()
            self._update_token_usage()
            self.controller.load_history_async()

    def reset_credit(self):
        credits = self.bundle.snapshot.sorted_credits()
        if not credits:
            self._set_banner('当前没有可用重置卡')
            return
        dialog, fresh = self._dialog('reset', '使用重置卡 · Codex 额度', 580, 380)
        if not fresh:
            return
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(self._label('选择一张重置卡', heading=True))
        layout.addWidget(self._label('只有点击“确认使用”才会消耗卡片', muted=True))
        tree = QTreeWidget()
        tree.setHeaderLabels(['卡片', '到期时间', '剩余'])
        tree.setRootIsDecorated(False)
        for credit in credits:
            days = credit.days_left()
            item = QTreeWidgetItem(['#'+credit.credit_id[-8:], ana.fmt_full_clock(credit.expires_at), f'{days:.1f} 天' if days is not None else '未知'])
            item.setData(0, Qt.ItemDataRole.UserRole, credit.credit_id)
            tree.addTopLevelItem(item)
        tree.setCurrentItem(tree.topLevelItem(0))
        tree.setColumnWidth(0, 130)
        tree.setColumnWidth(1, 230)
        dialog.tree = tree
        layout.addWidget(tree, 1)
        dialog.result_label = self._label('', muted=True, wrap=True)
        layout.addWidget(dialog.result_label)
        def confirm():
            current = tree.currentItem()
            if current and self.controller.reset_credit_async(current.data(0, Qt.ItemDataRole.UserRole)):
                dialog.confirm_button.setEnabled(False)
                dialog.result_label.setText('正在提交…')
            else:
                dialog.result_label.setText('正在同步，请稍后重试')
        row = QHBoxLayout()
        row.addStretch()
        dialog.cancel_button = self._button('取消', dialog.close)
        row.addWidget(dialog.cancel_button)
        dialog.confirm_button = self._button('确认使用', confirm, primary=True)
        dialog.confirm_button.setEnabled(not self.controller.busy)
        row.addWidget(dialog.confirm_button)
        layout.addLayout(row)
        dialog.show()

    def _install_tray(self):
        self.tray = QSystemTrayIcon(self.icon, self.root)
        menu = QMenu()
        for label, callback in (('显示窗口', self.show_window), ('刷新', lambda: self.refresh('tray')), ('切换 HUD', self.toggle_compact)):
            menu.addAction(label, lambda checked=False, callback=callback: self._safe(callback))
        self.tray_only_action = menu.addAction('仅留托盘，隐藏任务栏')
        self.tray_only_action.setCheckable(True)
        self.tray_only_action.setChecked(self.settings.hide_taskbar)
        self.tray_only_action.triggered.connect(lambda checked=False: self._safe(self.toggle_taskbar))
        self.click_through_action = menu.addAction('仅按钮可点击，其余穿透')
        self.click_through_action.setCheckable(True)
        self.click_through_action.setChecked(self.settings.click_through)
        self.click_through_action.triggered.connect(lambda checked=False: self._safe(self.toggle_click_through))
        self.move_action = menu.addAction('移动窗口', lambda checked=False: self._safe(self.toggle_move))
        for label, callback in (('设置', self.show_settings), ('退出', self.quit_app)):
            menu.addAction(label, lambda checked=False, callback=callback: self._safe(callback))
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_window() if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
        self.tray.show()

    def _update_tray(self):
        self.tray.setToolTip(APP_DISPLAY_NAME + '\n' + ' · '.join(f'{window.title()}剩余 {window.remaining_percent:.0f}%' for window in self.bundle.snapshot.windows[:2]))

    def _notify_warnings(self):
        if not self.settings.notify_on_expiry or self.bundle.snapshot.source != 'api':
            return
        current, messages = set(), []
        for window in self.bundle.snapshot.windows:
            if window.used_percent >= self.settings.warn_percent:
                key = ('quota', window.kind, window.resets_at)
                current.add(key)
                if key not in self._notified:
                    messages.append(f'{window.title()}剩余 {window.remaining_percent:.0f}%')
        for credit in self.bundle.snapshot.credits:
            days = credit.days_left()
            if days is not None and 0 < days <= self.settings.expiry_warn_days:
                key = ('credit', credit.credit_id, credit.expires_at)
                current.add(key)
                if key not in self._notified:
                    messages.append(f'重置卡将在 {days:.1f} 天后到期')
        self._notified.intersection_update(current)
        self._notified.update(current)
        if messages:
            self.tray.showMessage(APP_DISPLAY_NAME, '\n'.join(messages), QSystemTrayIcon.MessageIcon.Warning)

    def hide_to_tray(self):
        self._save_position()
        self.input_layer.hide()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.root.hide()
        else:
            self.root.showMinimized()

    def show_window(self):
        if not self._closed:
            self.root.showNormal()
            self.root.raise_()
            if not self.click_through_active:
                self.root.activateWindow()
            QTimer.singleShot(0, self.input_layer.sync)

    def on_close(self):
        self.hide_to_tray() if self.settings.minimize_to_tray and QSystemTrayIcon.isSystemTrayAvailable() else self.quit_app()

    def _open_data_dir(self):
        if os.name == 'nt':
            os.startfile(str(app_data_dir()))

    def quit_app(self):
        if self._closed:
            return
        self._save_position()
        self.position_timer.stop()
        self._closed = True
        self.codex_exit_signal.close()
        self.settings.save()
        self.pump_timer.stop()
        self.tick_timer.stop()
        self.tray.hide()
        self.input_layer.timer.stop()
        self.input_layer.hide()
        for dialog in self._dialogs.values():
            dialog.close()
        self.root.close()
        self.qt.quit()

    def run(self):
        self.qt.exec()
