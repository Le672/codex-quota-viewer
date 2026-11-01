"""亚克力风格的 Canvas 自绘控件。

全部基于 tkinter.Canvas 手绘，不依赖任何第三方库，也不受 ttk 主题限制。
"""

from __future__ import annotations

import math
import tkinter as tk
from typing import Callable, List, Optional, Sequence, Tuple

from .theme import Palette

# --------------------------------------------------------------------------- #
# 字体
# --------------------------------------------------------------------------- #
_FONT_CACHE: dict = {}


def pick_font_family(root: Optional[tk.Misc] = None) -> str:
    """挑一个支持中文、观感现代的字体。"""
    candidates = (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "Segoe UI Variable Text",
        "Segoe UI",
        "PingFang SC",
        "Noto Sans CJK SC",
    )
    try:
        import tkinter.font as tkfont

        available = set(tkfont.families(root))
        for name in candidates:
            if name in available:
                return name
    except Exception:
        pass
    return "TkDefaultFont"


def font(root: tk.Misc, size: int = 10, weight: str = "normal") -> tuple:
    key = (size, weight)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    family = pick_font_family(root)
    spec = (family, size, weight)
    _FONT_CACHE[key] = spec
    return spec


# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #
def rounded_points(
    x1: float, y1: float, x2: float, y2: float, r: float, steps: int = 6
) -> List[float]:
    """生成圆角矩形的多边形顶点（顺时针）。"""
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts: List[float] = []
    if r <= 0.5:
        return [x1, y1, x2, y1, x2, y2, x1, y2]
    # 右上
    for i in range(steps + 1):
        a = -math.pi / 2 + (math.pi / 2) * (i / steps)
        pts.extend([x2 - r + r * math.cos(a), y1 + r + r * math.sin(a)])
    # 右下
    for i in range(steps + 1):
        a = 0 + (math.pi / 2) * (i / steps)
        pts.extend([x2 - r + r * math.cos(a), y2 - r + r * math.sin(a)])
    # 左下
    for i in range(steps + 1):
        a = math.pi / 2 + (math.pi / 2) * (i / steps)
        pts.extend([x1 + r + r * math.cos(a), y2 - r + r * math.sin(a)])
    # 左上
    for i in range(steps + 1):
        a = math.pi + (math.pi / 2) * (i / steps)
        pts.extend([x1 + r + r * math.cos(a), y1 + r + r * math.sin(a)])
    return pts


def rounded_rect(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float = 12,
    *,
    fill: Optional[str] = None,
    outline: Optional[str] = None,
    width: float = 1,
    tags: Optional[Sequence[str]] = None,
    stipple: str = "",
) -> int:
    pts = rounded_points(x1, y1, x2, y2, radius)
    return canvas.create_polygon(
        pts,
        fill=fill or "",
        outline=outline or "",
        width=width,
        smooth=False,
        joinstyle="round",
        tags=tags,
        stipple=stipple,
    )


def mix(color_a: str, color_b: str, t: float) -> str:
    """线性混色，t=0 取 a，t=1 取 b。"""
    try:
        a = _rgb(color_a)
        b = _rgb(color_b)
        t = max(0.0, min(1.0, t))
        return "#%02X%02X%02X" % tuple(
            int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3)
        )
    except Exception:
        return color_a


def _rgb(value: str) -> Tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def heat_color(palette: Palette, used_percent: float) -> str:
    """按使用率返回热度色：绿 -> 黄 -> 橙 -> 红。"""
    if used_percent >= 95:
        return palette.danger
    if used_percent >= 80:
        return mix(palette.warn, palette.danger, (used_percent - 80) / 15.0)
    if used_percent >= 50:
        return mix(palette.ok, palette.warn, (used_percent - 50) / 30.0)
    return palette.ok


# --------------------------------------------------------------------------- #
# 进度环
# --------------------------------------------------------------------------- #
class Ring:
    """带中心文字的圆环进度控件。"""

    def __init__(
        self,
        canvas: tk.Canvas,
        cx: float,
        cy: float,
        radius: float = 46,
        thickness: float = 9,
        title: str = "",
    ):
        self.canvas = canvas
        self.cx = cx
        self.cy = cy
        self.radius = radius
        self.thickness = thickness
        self.title = title
        self._items: List[int] = []

    def clear(self) -> None:
        for item in self._items:
            try:
                self.canvas.delete(item)
            except Exception:
                pass
        self._items = []

    def draw(
        self,
        palette: Palette,
        used_percent: float,
        main_text: str,
        sub_text: str = "",
        *,
        color: Optional[str] = None,
        dim: bool = False,
    ) -> None:
        self.clear()
        used = max(0.0, min(100.0, used_percent))
        r = self.radius
        box = (self.cx - r, self.cy - r, self.cx + r, self.cy + r)

        # 轨道
        self._items.append(
            self.canvas.create_oval(
                *box,
                outline=palette.ring_track,
                width=self.thickness,
            )
        )
        ring_color = color or heat_color(palette, used)
        if dim:
            ring_color = mix(palette.ring_track, ring_color, 0.45)
        # 进度（从 12 点开始顺时针）
        if used > 0.3:
            extent = -359.99 * (used / 100.0)
            self._items.append(
                self.canvas.create_arc(
                    *box,
                    start=90.0,
                    extent=extent,
                    style=tk.ARC,
                    outline=ring_color,
                    width=self.thickness,
                )
            )
        text_color = palette.text_dim if dim else palette.text
        self._items.append(
            self.canvas.create_text(
                self.cx,
                self.cy - (7 if self.title else 0),
                text=main_text,
                fill=text_color,
                font=font(self.canvas, 17 if len(main_text) <= 4 else 14, "bold"),
            )
        )
        if sub_text:
            self._items.append(
                self.canvas.create_text(
                    self.cx + (0 if self.title else 0),
                    self.cy + (12 if self.title else 17),
                    text=sub_text,
                    fill=palette.text_faint,
                    font=font(self.canvas, 8),
                )
            )
        if self.title:
            self._items.append(
                self.canvas.create_text(
                    self.cx,
                    self.cy - 26,
                    text=self.title,
                    fill=palette.text_faint,
                    font=font(self.canvas, 9),
                )
            )


        # 额度环与内容一起销毁，刷新不会叠加旧百分比和图形。
        for item in self._items:
            self.canvas.addtag_withtag("content", item)


# --------------------------------------------------------------------------- #
# 按钮
# --------------------------------------------------------------------------- #
class RoundButton:
    """圆角按钮，支持悬停/按下/禁用状态。"""

    def __init__(
        self,
        canvas: tk.Canvas,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        text: str,
        command: Callable[[], None],
        *,
        radius: float = 10,
        icon: str = "",
        accent: bool = False,
        tooltip: str = "",
        tag: str = "",
    ):
        self.canvas = canvas
        self.box = (x1, y1, x2, y2)
        self.text = text
        self.command = command
        self.radius = radius
        self.icon = icon
        self.accent = accent
        self.tooltip = tooltip
        self.tag = tag or f"btn{id(self)}"
        self.enabled = True
        self._hover = False
        self._pressed = False
        self._items: List[int] = []
        self._palette: Optional[Palette] = None

    # -- 绘制 ------------------------------------------------------------- #
    def draw(self, palette: Palette) -> None:
        self._palette = palette
        self._items = []
        x1, y1, x2, y2 = self.box
        if not self.enabled:
            bg = palette.card_bg_soft
            fg = palette.text_faint
            border = palette.card_border
        elif self.accent:
            base = palette.accent
            if self._pressed:
                bg = mix(base, "#000000", 0.25)
            elif self._hover:
                bg = mix(base, "#FFFFFF", 0.18)
            else:
                bg = base
            fg = "#FFFFFF"
            border = bg
        else:
            if self._pressed:
                bg = palette.button_active
            elif self._hover:
                bg = palette.button_hover
            else:
                bg = palette.button
            fg = palette.text
            border = palette.card_border

        self._items.append(
            rounded_rect(
                self.canvas,
                x1,
                y1,
                x2,
                y2,
                self.radius,
                fill=bg,
                outline=border,
                width=1,
                tags=(self.tag,),
            )
        )
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        label = f"{self.icon} {self.text}".strip() if self.icon else self.text
        self._items.append(
            self.canvas.create_text(
                cx,
                cy,
                text=label,
                fill=fg,
                font=font(self.canvas, 10, "bold" if self.accent else "normal"),
                tags=(self.tag,),
            )
        )
        self._bind()

    def _bind(self) -> None:
        c = self.canvas
        c.tag_bind(self.tag, "<Enter>", self._on_enter)
        c.tag_bind(self.tag, "<Leave>", self._on_leave)
        c.tag_bind(self.tag, "<ButtonPress-1>", self._on_press)
        c.tag_bind(self.tag, "<ButtonRelease-1>", self._on_release)

    def _on_enter(self, _event=None) -> None:
        if not self.enabled:
            return
        self._hover = True
        self.canvas.configure(cursor="hand2")
        self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._pressed = False
        self.canvas.configure(cursor="")
        self._redraw()

    def _on_press(self, _event=None) -> None:
        if not self.enabled:
            return
        self._pressed = True
        self._redraw()

    def _on_release(self, _event=None) -> None:
        if not self.enabled:
            return
        was = self._pressed
        self._pressed = False
        self._redraw()
        if was and self.command:
            self.command()

    def _redraw(self) -> None:
        if not self._palette or len(self._items) < 2:
            return
        # 旧版兼容控件：悬停只能原位改色；删除鼠标下的对象会无限触发 Enter。
        palette = self._palette
        if not self.enabled:
            bg, fg, border = palette.card_bg_soft, palette.text_faint, palette.card_border
        elif self.accent:
            bg = mix(palette.accent, '#000000', .25) if self._pressed else mix(palette.accent, '#FFFFFF', .18) if self._hover else palette.accent
            fg, border = '#FFFFFF', bg
        else:
            bg = palette.button_active if self._pressed else palette.button_hover if self._hover else palette.button
            fg, border = palette.text, palette.card_border
        self.canvas.itemconfigure(self._items[0], fill=bg, outline=border)
        label = f'{self.icon} {self.text}'.strip() if self.icon else self.text
        self.canvas.itemconfigure(self._items[1], fill=fg, text=label)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self._redraw()

    def set_text(self, text: str) -> None:
        self.text = text
        self._redraw()


class IconButton:
    """标题栏小图标按钮（最小化 / 关闭 / 紧凑）。"""

    def __init__(
        self,
        canvas: tk.Canvas,
        cx: float,
        cy: float,
        kind: str,
        command: Callable[[], None],
        *,
        size: float = 30,
        tooltip: str = "",
        danger: bool = False,
    ):
        self.canvas = canvas
        self.cx = cx
        self.cy = cy
        self.kind = kind
        self.command = command
        self.size = size
        self.danger = danger
        self.tooltip = tooltip
        self.tag = f"icon{id(self)}"
        self._hover = False
        self._items: List[int] = []
        self._palette: Optional[Palette] = None

    def draw(self, palette: Palette) -> None:
        self._palette = palette
        self._items = []
        c = self.canvas
        r = self.size / 2.0
        if self._hover:
            bg = palette.danger if self.danger else palette.button_hover
            fg = "#FFFFFF" if self.danger else palette.text
        else:
            bg = ""
            fg = palette.text_dim
        if bg:
            self._items.append(
                c.create_oval(
                    self.cx - r,
                    self.cy - r,
                    self.cx + r,
                    self.cy + r,
                    fill=bg,
                    outline=bg,
                    tags=(self.tag,),
                )
            )
        s = self.size * 0.22
        if self.kind == "close":
            for dx in (-1, 1):
                self._items.append(
                    c.create_line(
                        self.cx - s * 0.9,
                        self.cy + dx * s * 0.9,
                        self.cx + s * 0.9,
                        self.cy - dx * s * 0.9,
                        fill=fg,
                        width=1.6,
                        capstyle=tk.ROUND,
                        tags=(self.tag,),
                    )
                )
        elif self.kind == "min":
            self._items.append(
                c.create_line(
                    self.cx - s,
                    self.cy,
                    self.cx + s,
                    self.cy,
                    fill=fg,
                    width=1.6,
                    capstyle=tk.ROUND,
                    tags=(self.tag,),
                )
            )
        elif self.kind == "compact":
            self._items.append(
                c.create_line(
                    self.cx - s,
                    self.cy + s * 0.6,
                    self.cx + s,
                    self.cy + s * 0.6,
                    fill=fg,
                    width=1.6,
                    capstyle=tk.ROUND,
                    tags=(self.tag,),
                )
            )
            self._items.append(
                c.create_line(
                    self.cx - s,
                    self.cy - s * 0.6,
                    self.cx + s * 0.2,
                    self.cy - s * 0.6,
                    fill=fg,
                    width=1.6,
                    capstyle=tk.ROUND,
                    tags=(self.tag,),
                )
            )
        elif self.kind == "pin":
            self._items.append(
                c.create_oval(
                    self.cx - s * 0.7,
                    self.cy - s * 0.7,
                    self.cx + s * 0.7,
                    self.cy + s * 0.7,
                    outline=fg,
                    width=1.4,
                    tags=(self.tag,),
                )
            )
        self._bind()

    def _bind(self) -> None:
        self.canvas.tag_bind(self.tag, "<Enter>", self._enter)
        self.canvas.tag_bind(self.tag, "<Leave>", self._leave)
        self.canvas.tag_bind(self.tag, "<Button-1>", self._click)

    def _enter(self, _e=None) -> None:
        self._hover = True
        self.canvas.configure(cursor="hand2")
        self._refresh()

    def _leave(self, _e=None) -> None:
        self._hover = False
        self.canvas.configure(cursor="")
        self._refresh()

    def _click(self, _e=None) -> None:
        if self.command:
            self.command()

    def _refresh(self) -> None:
        if not self._palette:
            return
        for item in self._items:
            try:
                self.canvas.delete(item)
            except Exception:
                pass
        self.draw(self._palette)


# --------------------------------------------------------------------------- #
# 提示条
# --------------------------------------------------------------------------- #
class Toast:
    """在窗口底部浮出一条短提示。"""

    def __init__(self, canvas: tk.Canvas, width: int, height: int):
        self.canvas = canvas
        self.width = width
        self.height = height
        self._items: List[int] = []
        self._timer: Optional[str] = None

    def show(self, palette: Palette, text: str, seconds: float = 3.0) -> None:
        self.clear()
        c = self.canvas
        pad = 14
        try:
            # 用当前窗口高度，而不是初始化时的高度
            visible_h = max(200, c.winfo_height())
        except Exception:
            visible_h = self.height
        y2 = visible_h - 16
        y1 = y2 - 38
        x1, x2 = pad, self.width - pad
        self._items.append(
            rounded_rect(c, x1, y1, x2, y2, 12, fill=palette.card_bg, outline=palette.accent, width=1)
        )
        self._items.append(
            c.create_text(
                (x1 + x2) / 2,
                (y1 + y2) / 2,
                text=text,
                fill=palette.text,
                font=font(c, 10),
                width=int(x2 - x1 - 24),
                justify="center",
            )
        )
        root = c.winfo_toplevel()
        if self._timer:
            try:
                root.after_cancel(self._timer)
            except Exception:
                pass
        self._timer = root.after(int(seconds * 1000), self.clear)

    def clear(self) -> None:
        for item in self._items:
            try:
                self.canvas.delete(item)
            except Exception:
                pass
        self._items = []
