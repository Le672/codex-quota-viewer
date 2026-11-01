"""从矢量源生成多尺寸 ICO 与小尺寸审阅图：python tools/make_icon.py。"""
from __future__ import annotations
import struct
import sys
from pathlib import Path
from PySide6.QtCore import Qt, QRectF, QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QColor, QFont
from PySide6.QtSvg import QSvgRenderer

SIZES = (16, 20, 24, 28, 32, 40, 48, 64, 96, 128, 256)
ROOT = Path(__file__).resolve().parents[1]


def render(renderer, size):
    image = QImage(size*4, size*4, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, image.width(), image.height()))
    painter.end()
    return image.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)


def png_bytes(image):
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    return bytes(buffer.data())


def main():
    app = QGuiApplication.instance() or QGuiApplication([])
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT/'icon.ico'
    renderer = QSvgRenderer(str(ROOT/'assets'/'icon.svg'))
    assert renderer.isValid(), '图标矢量源不可读'
    images = {size: render(renderer, size) for size in SIZES}
    offset = 6+16*len(SIZES)
    entries, data = [], []
    for size, image in images.items():
        png = png_bytes(image)
        dim = size if size < 256 else 0
        entries.append(struct.pack('<BBBBHHII', dim, dim, 0, 0, 1, 32, len(png), offset))
        data.append(png)
        offset += len(png)
        image.save(str(ROOT/'assets'/f'icon-{size}.png'))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(struct.pack('<HHH', 0, 1, len(SIZES))+b''.join(entries)+b''.join(data))
    review = QImage(800, 390, QImage.Format.Format_ARGB32_Premultiplied)
    review.fill(QColor('#F3F5FA'))
    painter = QPainter(review)
    painter.fillRect(0, 195, 800, 195, QColor('#141923'))
    for row in range(2):
        top = row*195
        painter.setPen(QColor('#23304C' if row == 0 else '#D5E0F4'))
        painter.setFont(QFont('Segoe UI', 11))
        painter.drawImage(28, top+22, render(renderer, 144))
        painter.drawText(QRectF(28, top+166, 144, 20), Qt.AlignmentFlag.AlignCenter, 'Codex Quota')
        x = 205
        for size in (16, 20, 24, 32, 48, 64):
            painter.drawImage(x+(72-size)//2, top+80-size//2, images[size])
            painter.drawText(QRectF(x, top+130, 72, 24), Qt.AlignmentFlag.AlignCenter, str(size)+' px')
            x += 94
    painter.end()
    preview = ROOT/'previews'
    preview.mkdir(exist_ok=True)
    review.save(str(preview/'icon-review.png'))
    print(f'ICO: {output}, {len(SIZES)} sizes, {output.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
