"""用 Qt 逻辑坐标恢复主窗口位置，兼容负坐标和显示器变更。"""


def startup_position(saved_geometry, size, work_areas):
    """保留仍在屏幕上的坐标；旧屏幕不存在时就近移回可见区域。"""
    width, height = size
    areas = [area for area in work_areas if area[2] > 0 and area[3] > 0]
    if not areas:
        return tuple(saved_geometry[:2]) if saved_geometry else (0, 0)
    if saved_geometry is None:
        left, top, area_width, area_height = areas[0]
        return (left + max(0, (area_width - width) // 2),
                top + max(0, (area_height - height) // 2))
    x, y = saved_geometry[:2]
    for left, top, area_width, area_height in areas:
        if left <= x < left + area_width and top <= y < top + area_height:
            return x, y

    def distance(area):
        left, top, area_width, area_height = area
        nearest_x = max(left, min(left + area_width - 1, x))
        nearest_y = max(top, min(top + area_height - 1, y))
        return (x - nearest_x) ** 2 + (y - nearest_y) ** 2

    left, top, area_width, area_height = min(areas, key=distance)
    return (max(left, min(left + max(0, area_width - width), x)),
            max(top, min(top + max(0, area_height - height), y)))
