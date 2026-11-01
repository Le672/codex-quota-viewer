"""主面板只负责显示；独立、按按钮形状裁剪的窗口接收按钮输入。

Windows layered + WS_EX_TRANSPARENT 可穿透到其他进程。仅返回
HTTRANSPARENT 不保证跨线程穿透，因此不使用该方式或鼠标全局钩子。
"""
from PySide6.QtCore import Qt, QEvent, QPoint, QPointF, QTimer
from PySide6.QtGui import QColor, QEnterEvent, QMouseEvent, QPainter, QRegion
from PySide6.QtWidgets import QApplication, QPushButton, QWidget


class ButtonInputLayer(QWidget):
    def __init__(self, owner):
        super().__init__(owner.root, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowDoesNotAcceptFocus)
        self.owner = owner
        self._hover = self._pressed = None
        self._mask_key = None
        self.setWindowTitle('Codex 额度 · 按钮输入')
        self.setWindowIcon(owner.icon)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync)
        self.timer.start(25)

    def buttons(self):
        root = self.owner.root
        return [button for button in root.findChildren(QPushButton)
                if button.window() == root and button.isVisible()]

    def button_rect(self, button):
        return button.rect().translated(button.mapTo(self.owner.root, QPoint()))

    def sync(self):
        owner = self.owner
        if (owner._closed or not owner.click_through_active or not owner.root.isVisible() or
                any(dialog.isVisible() for dialog in owner._dialogs.values()) or
                QApplication.activePopupWidget() is not None):
            self._set_hover(None)
            if self._pressed:
                self._pressed.setDown(False)
                self._pressed = None
            self.hide()
            return
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus
        if owner.settings.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        if self.windowFlags() != flags:
            self.setWindowFlags(flags)
            self._mask_key = None
        if self.geometry() != owner.root.geometry():
            self.setGeometry(owner.root.geometry())
        rects = [self.button_rect(button) for button in self.buttons()]
        if not rects:
            self.hide()
            return
        key = (self.devicePixelRatioF(), tuple((r.x(), r.y(), r.width(), r.height()) for r in rects))
        if key != self._mask_key:
            region = QRegion()
            for rect in rects:
                region |= QRegion(rect)
            self.setMask(region)
            self._mask_key = key
            self.update()
        if not self.isVisible():
            self.show()
            self.raise_()

    def paintEvent(self, event):
        # 最小 alpha 使按钮区域进入 Windows 命中测试；实际按钮仍由主面板绘制。
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
        painter.end()

    def _button_at(self, point):
        return next((button for button in self.buttons() if self.button_rect(button).contains(point)), None)

    def _set_hover(self, button):
        if button == self._hover:
            return
        if self._hover:
            self._hover.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
            QApplication.sendEvent(self._hover, QEvent(QEvent.Type.Leave))
            self._hover.update()
        self._hover = button
        self.setToolTip(button.toolTip() if button else '')
        if button:
            button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
            QApplication.sendEvent(button, QEnterEvent(QPointF(), QPointF(), QPointF(self.mapToGlobal(QPoint()))))
            button.update()

    def _send_mouse(self, button, event):
        local = event.position() - QPointF(button.mapTo(self.owner.root, QPoint()))
        forwarded = QMouseEvent(event.type(), local, event.position(), event.globalPosition(),
                                event.button(), event.buttons(), event.modifiers())
        QApplication.sendEvent(button, forwarded)
        event.accept()

    def mousePressEvent(self, event):
        button = self._button_at(event.position().toPoint())
        self._set_hover(button)
        if button and button.isEnabled():
            self._pressed = button
            self._send_mouse(button, event)

    def mouseMoveEvent(self, event):
        self._set_hover(self._button_at(event.position().toPoint()))
        if self._pressed:
            self._send_mouse(self._pressed, event)

    def mouseReleaseEvent(self, event):
        button, self._pressed = self._pressed, None
        if button:
            self._send_mouse(button, event)
        self.sync()

    def leaveEvent(self, event):
        self._set_hover(None)
        super().leaveEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.owner.on_close()
