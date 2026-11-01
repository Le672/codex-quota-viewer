"""Windows 亚克力背景 + Qt 玻璃表面；只绘制材质，不改变控件或事件。"""
from __future__ import annotations
import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QRadialGradient, QPainter, QPainterPath, QPen, QRegion
from PySide6.QtWidgets import QFrame, QDialog
from .appenv import log

@dataclass
class BackdropResult:
    applied: bool = False
    method: str = 'solid'
    detail: str = ''

def set_backdrop(hwnd: int, *, enabled: bool, dark: bool, tint=.62) -> BackdropResult:
    """只修改本应用 HWND。保留明确的 ABI，失败时使用实色玻璃表面。"""
    if os.name != 'nt':
        return BackdropResult(detail='系统未提供 Windows 亚克力')
    try:
        dwm = ctypes.WinDLL('dwmapi', use_last_error=True)
        setter = dwm.DwmSetWindowAttribute
        setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        setter.restype = ctypes.c_long
        def attribute(number, value):
            data = ctypes.c_int(value)
            return setter(hwnd, number, ctypes.byref(data), ctypes.sizeof(data))
        attribute(20, int(dark))  # DWMWA_USE_IMMERSIVE_DARK_MODE
        attribute(33, 2)         # DWMWCP_ROUND
        attribute(34, 0xFFFFFFFE)  # 禁用系统边框，由 Qt 绘制玻璃光边
        user = ctypes.WinDLL('user32', use_last_error=True)
        get_style = user.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.GetWindowLongW
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        layered = bool(get_style(hwnd, -20) & 0x00080000)
        # Qt 的逐像素 alpha 窗口使用 WS_EX_LAYERED：系统 backdrop 不能可靠透过，
        # 使用同一 DWM 的 accent 兼容路径。普通 Win32 窗口保留文档化的 backdrop。
        if layered:
            attribute(38, 1)
            return _layered_acrylic(user, hwnd, enabled=enabled, dark=dark, tint=tint)
        result = attribute(38, 3 if enabled else 1)  # Desktop Acrylic / None
        if not enabled:
            return BackdropResult()
        if result < 0:
            return BackdropResult(detail=f'DWM HRESULT 0x{result & 0xffffffff:08X}')
        class Margins(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int) for name in ('left', 'right', 'top', 'bottom')]
        extend = dwm.DwmExtendFrameIntoClientArea
        extend.argtypes = [wintypes.HWND, ctypes.POINTER(Margins)]
        extend.restype = ctypes.c_long
        margins = Margins(-1, -1, -1, -1)
        extend(hwnd, ctypes.byref(margins))
        return BackdropResult(True, 'dwm-acrylic', 'DWMSBT_TRANSIENTWINDOW')
    except (OSError, AttributeError, ValueError) as error:
        return BackdropResult(detail=str(error))

def _layered_acrylic(user, hwnd, *, enabled, dark, tint):
    class Accent(ctypes.Structure):
        _fields_ = [('state',ctypes.c_int),('flags',ctypes.c_int),('color',ctypes.c_uint32),('animation',ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [('attribute',ctypes.c_int),('data',ctypes.c_void_p),('size',ctypes.c_size_t)]
    function = user.SetWindowCompositionAttribute
    function.argtypes = [wintypes.HWND, ctypes.POINTER(Data)]
    function.restype = wintypes.BOOL
    alpha = int(48 + max(.35, min(.95, tint))*40)
    red, green, blue = (17, 27, 40) if dark else (242, 247, 255)
    color = (alpha << 24) | (blue << 16) | (green << 8) | red
    accent = Accent(4 if enabled else 0, 2, color, 0)
    data = Data(19, ctypes.cast(ctypes.pointer(accent),ctypes.c_void_p), ctypes.sizeof(accent))
    if not function(hwnd, ctypes.byref(data)):
        return BackdropResult(detail=f'Accent Win32 {ctypes.get_last_error()}')
    if not enabled:
        return BackdropResult()
    verified = '设置成功'
    try:
        getter = user.GetWindowCompositionAttribute
        getter.argtypes = [wintypes.HWND, ctypes.POINTER(Data)]
        getter.restype = wintypes.BOOL
        state = Accent()
        query = Data(19, ctypes.cast(ctypes.pointer(state),ctypes.c_void_p), ctypes.sizeof(state))
        if getter(hwnd, ctypes.byref(query)):
            verified = f'读取状态={state.state}'
            if state.state != 4:
                return BackdropResult(detail=verified)
    except AttributeError:
        pass
    return BackdropResult(True, 'accent-acrylic', f'Qt alpha 兼容 · {verified}')

def rounded_mask(widget, radius=25, inset=0):
    path = QPainterPath()
    path.addRoundedRect(QRectF(widget.rect()).adjusted(inset, inset, -inset, -inset), radius, radius)
    widget.setMask(QRegion(path.toFillPolygon().toPolygon()))

def paint_surface(widget, owner, *, shell=False, native=False):
    """边缘折光、上沿高光与两层柔光；背景模糊交给 DWM，不逐帧采样桌面。"""
    colors = owner.colors
    dark = owner.dark_theme
    appearance = owner.appearance
    glass = appearance.material == 'glass'
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(widget.rect()).adjusted(.8, .8, -.8, -.8)
    radius = 24 if shell else 18
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.setClipPath(path)
    painter.setPen(Qt.PenStyle.NoPen)
    if appearance.material == 'transparent':
        # 1/255 的无色 alpha 仅保留 Windows layered 窗口的鼠标命中区域。
        # 背景只在最外层绘制一次，避免卡片重叠改变用户设定的浓度。
        # 不调用 setWindowOpacity，文字和按钮独立保持清晰。
        background = QColor(colors['bg']) if shell and appearance.transparent_opacity else QColor(0, 0, 0)
        background.setAlpha(max(1, round(appearance.transparent_opacity * 255)) if shell else 1)
        painter.fillPath(path, background)
        rim = QColor(colors['text'])
        rim.setAlpha(65 if shell else 36)
        painter.setPen(QPen(rim, .8))
        painter.drawPath(path)
        painter.end()
        return
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    if shell:
        density = appearance.opacity
        alpha = int(132 + density * 67) if glass and native else 255
        start = QColor(15, 23, 35, alpha) if dark else QColor(243, 248, 255, min(255, alpha+22))
        end = QColor(23, 34, 42, alpha) if dark else QColor(221, 233, 246, min(255, alpha+22))
    elif glass:
        start = QColor(236, 246, 255, 21 if dark else 100)
        end = QColor(226, 241, 255, 7 if dark else 35)
    else:
        start = end = QColor(colors['card'])
    gradient.setColorAt(0, start)
    gradient.setColorAt(1, end)
    painter.fillPath(path, gradient)
    if glass:
        glow = QRadialGradient(rect.width()*.06, -rect.height()*.09, max(160., rect.width()*.94))
        glow.setColorAt(0, QColor(192, 233, 255, 34 if dark else 90))
        glow.setColorAt(.5, QColor(180, 223, 255, 8))
        glow.setColorAt(1, QColor(180, 223, 255, 0))
        painter.fillPath(path, glow)
        if shell:
            glow = QRadialGradient(rect.right(), rect.bottom(), rect.width()*.82)
            glow.setColorAt(0, QColor(91, 225, 192, 18 if dark else 35))
            glow.setColorAt(1, QColor(91, 225, 192, 0))
            painter.fillPath(path, glow)
        rim = QLinearGradient(rect.topLeft(), rect.bottomRight())
        rim.setColorAt(0, QColor(245, 252, 255, 112 if dark else 230))
        rim.setColorAt(.32, QColor(239, 250, 255, 23 if dark else 100))
        rim.setColorAt(.67, QColor(203, 247, 244, 12 if dark else 85))
        rim.setColorAt(1, QColor(241, 253, 255, 66 if dark else 190))
        painter.setClipping(False)
        painter.setPen(QPen(rim, 1.1))
        painter.drawPath(path)
        inner = rect.adjusted(1.3, 1.3, -1.3, -1.3)
        painter.setPen(QPen(QColor(255, 255, 255, 14 if dark else 85), .7))
        painter.drawRoundedRect(inner, radius-1, radius-1)
    else:
        painter.setPen(QPen(QColor(colors['border']), 1))
        painter.drawPath(path)
    painter.end()

class GlassSurface(QFrame):
    def __init__(self, owner, shell=False):
        super().__init__()
        self.owner, self.shell = owner, shell
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    def paintEvent(self, event):
        paint_surface(self, self.owner, shell=self.shell,
                      native=self.owner.root.backdrop.applied)

class GlassDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner.root)
        self.owner = owner
        self.backdrop = BackdropResult()
        self._material_key = None
        self.drag_offset = None
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.apply_material)
    def apply_material(self):
        if not self.isVisible():
            return
        appearance = self.owner.appearance
        key = (int(self.winId()), appearance.material, self.owner.dark_theme, appearance.opacity)
        self.update()
        if key == self._material_key:
            return
        self._material_key = key
        self.backdrop = set_backdrop(int(self.winId()), enabled=appearance.material == 'glass',
                                    dark=self.owner.dark_theme, tint=appearance.opacity)
        if appearance.material == 'transparent':
            self.backdrop.method = 'transparent'
    def resizeEvent(self, event):
        super().resizeEvent(event)
        rounded_mask(self, 24)
    def paintEvent(self, event):
        paint_surface(self, self.owner, shell=True, native=self.backdrop.applied)
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 56:
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        if self.drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
        else:
            super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        self.drag_offset = None
        super().mouseReleaseEvent(event)
