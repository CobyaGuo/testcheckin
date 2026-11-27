# -*- coding: utf-8 -*-
"""SF Symbols 风格的单色线性图标工厂（macOS 视觉语言）。

用法：icon("copy") 返回随主题着色的 QIcon（16/32px 两档，高分屏清晰）。
所有图标统一：圆头线条、2px 等效描边、视觉重心居中、无填充杂色。
"""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPen,
                           QPixmap, QBrush)

_CACHE = {}


def _stroke_color():
    app = None
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
    except Exception:
        pass
    if app is not None and app.property("theme") == "light":
        return QColor("#5a6472")
    return QColor("#aeb8cc")


def _pen(color, w):
    p = QPen(color, w)
    p.setCapStyle(Qt.RoundCap)
    p.setJoinStyle(Qt.RoundJoin)
    return p


def _paint(name, size, color):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = size / 16.0            # 设计坐标 16x16
    p.scale(s, s)
    w = 1.6                    # 16px 坐标系下的等效线宽
    p.setPen(_pen(color, w))
    p.setBrush(Qt.NoBrush)
    fn = getattr(_DRAW, "_" + name, None) or getattr(_EXTRA, "_" + name, None)
    if fn:
        fn(p)
    else:                      # 兜底：圆点
        p.drawEllipse(QPointF(8, 8), 4, 4)
    p.end()
    return pm


def icon(name):
    """按名字取 QIcon；结果缓存（颜色随主题，主题切换后由缓存键区分）。"""
    app = None
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
    except Exception:
        pass
    theme = "light" if (app is not None and app.property("theme") == "light") else "dark"
    key = (name, theme)
    if key in _CACHE:
        return _CACHE[key]
    color = _stroke_color()
    ic = QIcon()
    for sz in (16, 20, 32, 64):
        ic.addPixmap(_paint(name, sz, color))
    _CACHE[key] = ic
    return ic


def color_dot(color, size=16):
    """纯色圆形标签点（带细描边），用于标签子菜单。"""
    key = ("dot", color)
    if key in _MAC_CACHE:
        return _MAC_CACHE[key]
    ic = QIcon()
    for sz in (size, size * 2 if size <= 32 else size):
        pm = QPixmap(sz, sz)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = sz / 2.0 - 1.5
        p.setPen(QPen(QColor(0, 0, 0, 60), 1))
        p.setBrush(QColor(color))
        p.drawEllipse(sz / 2.0 - r, sz / 2.0 - r, r * 2, r * 2)
        p.end()
        ic.addPixmap(pm)
    _MAC_CACHE[key] = ic
    return ic

class _DRAW:
    """各图标的 16x16 坐标系绘制函数。"""

    @staticmethod
    def _rename(p):            # 铅笔
        p.drawLine(QPointF(3, 13), QPointF(3.5, 11), ) if False else None
        path = QPainterPath(QPointF(10.5, 2.5))
        path.lineTo(13.5, 5.5)
        path.lineTo(6, 13)
        path.lineTo(2.6, 13.4)
        path.lineTo(3, 10)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(9.2, 3.8), QPointF(12.2, 6.8))

    @staticmethod
    def _view(p):              # 眼睛
        p.drawEllipse(QPointF(8, 8), 6, 4.2)
        p.setBrush(QBrush(p.pen().color()))
        p.drawEllipse(QPointF(8, 8), 1.8, 1.8)

    @staticmethod
    def _edit(p):              # 文档 + 铅笔
        p.drawPath(_doc_path())
        p.drawLine(QPointF(9, 12.5), QPointF(14, 7.5))
        p.drawLine(QPointF(14, 7.5), QPointF(11.5, 5))
        p.drawLine(QPointF(11.5, 5), QPointF(6.5, 10))
        p.drawLine(QPointF(6.5, 10), QPointF(6, 12), )

    @staticmethod
    def _copy(p):              # 双页
        p.drawRoundedRect(QPointF(5.5, 2.5).x(), 2.5, 7, 9, 1.5, 1.5)
        p.drawRoundedRect(3.5, 4.5, 7, 9, 1.5, 1.5)

    @staticmethod
    def _move(p):              # 文件夹 + 右箭头
        p.drawPath(_folder_path(2, 4, 8, 8))
        p.drawLine(QPointF(10.5, 8), QPointF(14.5, 8))
        p.drawLine(QPointF(12.7, 6.2), QPointF(14.5, 8))
        p.drawLine(QPointF(12.7, 9.8), QPointF(14.5, 8))

    @staticmethod
    def _folder_new(p):        # 文件夹 + 加号
        p.drawPath(_folder_path(2, 3, 9, 8.5))
        p.drawLine(QPointF(12.2, 9.5), QPointF(12.2, 13.5))
        p.drawLine(QPointF(10.2, 11.5), QPointF(14.2, 11.5))

    @staticmethod
    def _trash(p):             # 垃圾桶
        p.drawRoundedRect(4.5, 4.5, 7, 9, 1.5, 1.5)
        p.drawLine(QPointF(3, 4.5), QPointF(13, 4.5))
        p.drawLine(QPointF(6.3, 4.5), QPointF(6.3, 3))
        p.drawLine(QPointF(6.3, 3), QPointF(9.7, 3))
        p.drawLine(QPointF(9.7, 3), QPointF(9.7, 4.5))
        p.drawLine(QPointF(6.8, 7), QPointF(6.8, 11))
        p.drawLine(QPointF(9.2, 7), QPointF(9.2, 11))

    @staticmethod
    def _search(p):            # 放大镜
        p.drawEllipse(QPointF(7, 7), 4.2, 4.2)
        p.drawLine(QPointF(10.2, 10.2), QPointF(13.5, 13.5))

    @staticmethod
    def _columns(p):           # 双栏布局
        p.drawRoundedRect(2.5, 3, 4.5, 10, 1.5, 1.5)
        p.drawRoundedRect(9, 3, 4.5, 10, 1.5, 1.5)

    @staticmethod
    def _tree(p):              # 目录树
        p.drawLine(QPointF(4, 2.5), QPointF(4, 13.5))
        p.drawLine(QPointF(4, 4.5), QPointF(13, 4.5))
        p.drawLine(QPointF(4, 8), QPointF(11, 8))
        p.drawLine(QPointF(4, 11.5), QPointF(13, 11.5))
        p.drawLine(QPointF(11, 8), QPointF(11, 11.5))

    @staticmethod
    def _up(p):                # 上级（箭头出托盘）
        p.drawLine(QPointF(8, 12), QPointF(8, 3.5))
        p.drawLine(QPointF(4.8, 6.7), QPointF(8, 3.5))
        p.drawLine(QPointF(11.2, 6.7), QPointF(8, 3.5))
        p.drawLine(QPointF(3, 13.5), QPointF(13, 13.5))

    @staticmethod
    def _back(p):              # 后退
        p.drawLine(QPointF(10, 3), QPointF(5, 8))
        p.drawLine(QPointF(5, 8), QPointF(10, 13))

    @staticmethod
    def _forward(p):           # 前进
        p.drawLine(QPointF(6, 3), QPointF(11, 8))
        p.drawLine(QPointF(11, 8), QPointF(6, 13))

    @staticmethod
    def _refresh(p):           # 刷新
        rect = QRectF(3.5, 3.5, 9, 9)
        path = QPainterPath()
        path.arcMoveTo(rect, 40)
        path.arcTo(rect, 40, 260)
        p.drawPath(path)
        p.drawLine(QPointF(12.2, 2.6), QPointF(12.4, 6))
        p.drawLine(QPointF(12.4, 6), QPointF(9, 5.6))

    @staticmethod
    def _terminal(p):          # 命令行
        p.drawRoundedRect(2.5, 3, 11, 10, 2, 2)
        p.drawLine(QPointF(5, 6.5), QPointF(7, 8.5))
        p.drawLine(QPointF(7, 8.5), QPointF(5, 10.5))
        p.drawLine(QPointF(8.5, 11), QPointF(11, 11))


def _doc_path():
    path = QPainterPath(QPointF(4, 2.5))
    path.lineTo(9, 2.5)
    path.lineTo(12, 5.5)
    path.lineTo(12, 13.5)
    path.lineTo(4, 13.5)
    path.closeSubpath()
    path.moveTo(9, 2.5)
    path.lineTo(9, 5.5)
    path.lineTo(12, 5.5)
    return path


def _folder_path(x, y, w, h):
    path = QPainterPath(QPointF(x, y + h))
    path.lineTo(x, y + 1.6)
    path.quadTo(x, y, x + 1.6, y)
    path.lineTo(x + w * 0.42, y)
    path.lineTo(x + w * 0.42 + 1.4, y + 1.4)
    path.lineTo(x + w - 1.6, y + 1.4)
    path.quadTo(x + w, y + 1.4, x + w, y + 3)
    path.lineTo(x + w, y + h)
    path.closeSubpath()
    return path


# ---------------------------------------------------------------------------
# macOS 风格文件/文件夹图标（替换 Windows 原生 Shell 图标）
# ---------------------------------------------------------------------------
_MAC_CACHE = {}

def mac_folder(size=16):
    """macOS 风格蓝色文件夹：浅蓝渐变前板 + 顶部后板小耳。"""
    key = ("folder", size)
    if key in _MAC_CACHE:
        return _MAC_CACHE[key]
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = size / 16.0
    p.scale(s, s)
    p.setPen(QPen(QColor("#4a8cd8"), 0.8))
    # 后板（左上小耳）
    p.setBrush(QColor("#5ea5ea"))
    p.drawRoundedRect(QRectF(1.8, 3.2, 8.4, 9.5), 1.8, 1.8)
    # 前板渐变
    from PySide6.QtGui import QLinearGradient
    g = QLinearGradient(0, 5.2, 0, 14)
    g.setColorAt(0, QColor("#7db9f2"))
    g.setColorAt(1, QColor("#559be8"))
    p.setBrush(QBrush(g))
    p.drawRoundedRect(QRectF(2.6, 5.2, 11.6, 7.6), 1.8, 1.8)
    p.end()
    ic = QIcon(pm)
    _MAC_CACHE[key] = ic
    return ic


def tagged_icon(base, colors, size=16):
    """在基础图标右下角叠加标签色点（最多 3 个）。"""
    key = ("tagged", id(base), tuple(colors), size)
    if key in _MAC_CACHE:
        return _MAC_CACHE[key]
    pm = base.pixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    x = size - 4.0
    for c in list(colors)[:3]:
        p.setPen(QPen(QColor("#ffffff"), 1.0))
        p.setBrush(QColor(c))
        p.drawEllipse(QPointF(x, size - 3.2), 2.2, 2.2)
        x -= 4.6
    p.end()
    ic = QIcon(pm)
    _MAC_CACHE[key] = ic
    return ic


class _EXTRA:
    """收藏树等专用字形（16x16 线性）。"""

    @staticmethod
    def _globe(p):             # 链接文件（地球：外圈 + 赤道 + 经线）
        p.drawEllipse(QPointF(8, 8), 6, 6)
        p.drawLine(QPointF(2.2, 8), QPointF(13.8, 8))
        p.drawEllipse(QPointF(8, 8), 2.7, 6)
        p.drawLine(QPointF(2.6, 5.5), QPointF(13.4, 5.5))
        p.drawLine(QPointF(2.6, 10.5), QPointF(13.4, 10.5))

    @staticmethod
    def _tag(p):               # 标签（右上角打孔的吊牌）
        path = QPainterPath(QPointF(2.5, 2.5))
        path.lineTo(8, 2.5)
        path.lineTo(13.5, 8)
        path.lineTo(8, 13.5)
        path.lineTo(2.5, 8)
        path.closeSubpath()
        p.drawPath(path)
        p.drawEllipse(QPointF(10.6, 5.4), 0.9, 0.9)

    @staticmethod
    def _monitor(p):           # 桌面
        p.drawRoundedRect(2.5, 3, 11, 7.5, 1.5, 1.5)
        p.drawLine(QPointF(8, 10.5), QPointF(8, 12.5))
        p.drawLine(QPointF(5.5, 13), QPointF(10.5, 13))

    @staticmethod
    def _download(p):          # 下载（箭头入托盘）
        p.drawLine(QPointF(8, 2.5), QPointF(8, 9.5))
        p.drawLine(QPointF(4.8, 6.3), QPointF(8, 9.5))
        p.drawLine(QPointF(11.2, 6.3), QPointF(8, 9.5))
        p.drawLine(QPointF(3, 12.5), QPointF(5.5, 12.5))
        p.drawLine(QPointF(10.5, 12.5), QPointF(13, 12.5))

    @staticmethod
    def _photo(p):             # 图片（山与太阳）
        p.drawRoundedRect(2.5, 3, 11, 10, 1.5, 1.5)
        p.drawEllipse(QPointF(6, 6), 1.1, 1.1)
        path = QPainterPath(QPointF(4, 11.5))
        path.lineTo(7, 8.5)
        path.lineTo(9.5, 11)
        path.lineTo(11, 9.5)
        path.lineTo(12.2, 11)
        p.drawPath(path)

    @staticmethod
    def _music(p):             # 音符
        p.drawEllipse(QPointF(5.8, 11.5), 1.8, 1.4)
        p.drawEllipse(QPointF(11.2, 10.5), 1.8, 1.4)
        p.drawLine(QPointF(7.6, 11.5), QPointF(7.6, 4))
        p.drawLine(QPointF(13, 10.5), QPointF(13, 3.5))
        p.drawLine(QPointF(7.6, 4), QPointF(13, 3.5))

    @staticmethod
    def _film(p):              # 视频（胶片框 + 播放）
        p.drawRoundedRect(2.5, 3.5, 11, 9, 1.5, 1.5)
        poly = QPainterPath(QPointF(7, 6.2))
        poly.lineTo(10.2, 8)
        poly.lineTo(7, 9.8)
        poly.closeSubpath()
        p.drawPath(poly)

    @staticmethod
    def _list(p):              # 日志（行列表）
        p.drawLine(QPointF(3.5, 4.5), QPointF(12.5, 4.5))
        p.drawLine(QPointF(3.5, 8), QPointF(12.5, 8))
        p.drawLine(QPointF(3.5, 11.5), QPointF(9.5, 11.5))
