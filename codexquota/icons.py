"""应用、窗口、托盘与安装快捷方式使用同一份多尺寸图标。"""
import sys
from pathlib import Path
from PySide6.QtGui import QIcon


def icon_path():
    root = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    return root/'icon.ico'


def application_icon():
    return QIcon(str(icon_path()))
