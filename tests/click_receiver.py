"""独立进程的鼠标接收窗口，只记录本窗口收到的真实输入。"""
import json
import sys
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QApplication, QWidget

destination = Path(sys.argv[1])
state = dict(clicks=0, right_clicks=0, wheel=0, releases=0)

def save():
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(state), encoding='utf-8')
    temporary.replace(destination)

class Receiver(QWidget):
    def mousePressEvent(self, event):
        key = 'clicks' if event.button() == Qt.MouseButton.LeftButton else 'right_clicks'
        state[key] += 1
        save()
        event.accept()
    def wheelEvent(self, event):
        state['wheel'] += event.angleDelta().y()
        save()
        event.accept()
    def mouseReleaseEvent(self, event):
        state['releases'] += 1
        save()
        event.accept()
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#E5EAF0'))
        painter.drawText(15, 25, 'Quota click-through test: independent receiver')

app = QApplication([])
window = Receiver()
window.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
window.setWindowTitle(sys.argv[2])
window.setGeometry(*map(int, sys.argv[3:7]))
window.show()
save()
app.exec()
