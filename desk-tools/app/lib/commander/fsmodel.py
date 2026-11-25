# -*- coding: utf-8 -*-
"""目录扫描与文件表模型。

性能要点：
- 用 os.scandir 在工作线程一次性取全量条目（拿 stat 缓存），UI 线程零磁盘 IO；
- QAbstractTableModel 直供 QTableView，只做字符串比较排序，万级条目不卡；
- 目录扫描带代际号（epoch），快速连续切换目录时旧结果作废。
"""
import os
import stat as stat_mod
import sys
import threading
import time
from datetime import datetime

from PySide6.QtCore import (QAbstractTableModel, QFileInfo, QModelIndex, QObject,
                            QRunnable, QSortFilterProxyModel, Qt, QThreadPool, Signal)
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFileIconProvider

# 系统图标：用 Windows 原生文件关联图标，一眼可辨；带缓存避免重复取。
_ICON_PROVIDER = None
# 文件标签：{normcase(abspath): ["#rrggbb", ...]}，由 main 启动时注入
FILE_TAGS = {}


def set_file_tags(tags):
    """更新全局标签表并清图标叠加缓存。"""
    global FILE_TAGS
    FILE_TAGS = {os.path.normcase(os.path.abspath(k)): list(v)
                 for k, v in (tags or {}).items()}
    from . import icons as _ic
    _ic._MAC_CACHE.clear()

_ICON_CACHE = {}
_ICON_CACHE_MAX = 30000


def _shell_folder_icon(path):
    """目录图标：直接问 Shell（SHGetFileInfo），拿真实文件夹图标。

    QFileIconProvider 对部分目录会回退成"电脑/驱动器"样式图标，
    这里优先用 Shell 原生结果，失败再退回 provider。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes, byref, sizeof

        class SHFILEINFOW(ctypes.Structure):
            _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int),
                        ("dwAttributes", wintypes.DWORD),
                        ("szDisplayName", wintypes.WCHAR * 260),
                        ("szTypeName", wintypes.WCHAR * 80)]

        info = SHFILEINFOW()
        SHGFI_ICON, SHGFI_SMALLICON = 0x100, 0x1
        res = ctypes.WinDLL("shell32", use_last_error=True).SHGetFileInfoW(
            str(path), 0, byref(info), sizeof(info), SHGFI_ICON | SHGFI_SMALLICON)
        if not res or not info.hIcon:
            return None
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QImage
        if not hasattr(QImage, "fromHICON"):
            return None
        img = QImage.fromHICON(info.hIcon)
        ctypes.windll.user32.DestroyIcon(info.hIcon)
        if img.isNull():
            return None
        ic = QIcon(img)
        if img.size().width() > 16:
            ic = QIcon(ic.pixmap(QSize(16, 16)))
        return ic
    except Exception:
        return None


def sys_icon(path, is_dir=False):
    """取图标：文件夹用 mac 风格蓝色文件夹；文件用 Windows 原生
    Shell 图标（不同类型/可执行文件各自真实图标，一眼可辨）。

    性能关键：文件按【扩展名】缓存（同扩展名只查一次 Shell），
    目录全局共用一份绘制图标。
    """
    global _ICON_PROVIDER
    if is_dir:
        key = "dir"
    else:
        key = "ext:" + os.path.splitext(path)[1].lower()
    ic = _ICON_CACHE.get(key)
    if ic is not None:
        return ic
    if is_dir:
        from . import icons as _ic
        ic = _ic.mac_folder()
    else:
        if _ICON_PROVIDER is None:
            _ICON_PROVIDER = QFileIconProvider()
        ic = None
        if os.name == "nt":
            ic = _shell_file_icon(path)
        if ic is None:
            try:
                ic = _ICON_PROVIDER.icon(QFileInfo(path))
            except Exception:
                ic = QIcon()
            if ic.isNull():
                ic = _ICON_PROVIDER.icon(QFileIconProvider.File)
    if len(_ICON_CACHE) >= _ICON_CACHE_MAX:
        _ICON_CACHE.clear()
    _ICON_CACHE[key] = ic
    return ic

def _shell_file_icon(path):
    """文件图标：SHGetFileInfo 按真实文件取（exe 用内嵌图标）。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes, byref, sizeof

        class SHFILEINFOW(ctypes.Structure):
            _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int),
                        ("dwAttributes", ctypes.c_uint),
                        ("szDisplayName", wintypes.WCHAR * 260),
                        ("szTypeName", wintypes.WCHAR * 80)]

        SHGFI_ICON, SHGFI_LARGEICON, SHGFI_USEFILEATTRIBUTES = 0x100, 0x0, 0x10
        info = SHFILEINFOW()
        res = ctypes.WinDLL("shell32", use_last_error=True).SHGetFileInfoW(
            str(path), 0x80, byref(info), sizeof(info),
            SHGFI_ICON | SHGFI_LARGEICON | SHGFI_USEFILEATTRIBUTES)
        if not res or not info.hIcon:
            return None
        from PySide6.QtGui import QImage
        img = QImage.fromHICON(info.hIcon)
        ctypes.windll.user32.DestroyIcon(info.hIcon)
        if img.isNull():
            return None
        from PySide6.QtGui import QPixmap
        return QIcon(QPixmap.fromImage(img))
    except Exception:
        return None

_FAV_ICONS = (("桌面", "monitor"), ("下载", "download"), ("文档", "edit"),
              ("图片", "photo"), ("音乐", "music"), ("视频", "film"),
              ("日志", "list"), ("logs", "list"))

def fav_icon(name):
    """收藏图标：按名称关键词给专属 mac 风格线性图标。"""
    from . import icons as _ic
    low = name.lower()
    for kw, glyph in _FAV_ICONS:
        if kw in low:
            return _ic.icon(glyph)
    return _ic.mac_folder()

COL_NAME, COL_SIZE, COL_TYPE, COL_MTIME = 0, 1, 2, 3
COLS = ("名称", "大小", "类型", "修改时间")

_HIDDEN_ATTR = 0x02 | 0x04  # FILE_ATTRIBUTE_HIDDEN | SYSTEM


def _fmt_size(n):
    if n is None:
        return ""
    if n < 1024:
        return "%d B" % n
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return "%.1f %s" % (n, unit)
    return "%.1f PB" % n


class Entry:
    """一条目录项的只读快照。"""
    __slots__ = ("name", "path", "is_dir", "size", "mtime", "hidden")

    def __init__(self, name, path, is_dir, size, mtime, hidden):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.size = size
        self.mtime = mtime          # float timestamp
        self.hidden = hidden

    @property
    def ext(self):
        _, e = os.path.splitext(self.name)
        return e.lower().lstrip(".")

    def type_text(self):
        if self.is_dir:
            return "文件夹"
        ext = self.ext
        return (ext.upper() + " 文件") if ext else "文件"

    def mtime_text(self):
        try:
            return datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return ""


class _ScanSignals(QObject):
    done = Signal(int, str, list)     # epoch, path, [Entry]
    failed = Signal(int, str, str)    # epoch, path, 错误


class ScanTask(QRunnable):
    """一次目录扫描；带 epoch 支持作废过期结果。"""

    def __init__(self, epoch, path, show_hidden):
        super().__init__()
        self.epoch = epoch
        self.path = path
        self.show_hidden = show_hidden
        self.sig = _ScanSignals()

    def run(self):
        entries = []
        try:
            with os.scandir(self.path) as it:
                for d in it:
                    try:
                        is_dir = d.is_dir(follow_symlinks=False)
                        st = d.stat(follow_symlinks=False)
                        hidden = False
                        if os.name == "nt":
                            attrs = getattr(d, "_attrib", st.st_file_attributes) \
                                if hasattr(st, "st_file_attributes") else st.st_file_attributes
                            hidden = bool(attrs & _HIDDEN_ATTR)
                        else:
                            hidden = d.name.startswith(".")
                        if hidden and not self.show_hidden:
                            continue
                        size = 0 if is_dir else st.st_size
                        entries.append(Entry(d.name, d.path, is_dir, size,
                                             st.st_mtime, hidden))
                    except OSError:
                        continue
        except OSError as exc:
            self.sig.failed.emit(self.epoch, self.path, str(exc))
            return
        self.sig.done.emit(self.epoch, self.path, entries)


class FsModel(QAbstractTableModel):
    """文件表数据模型。load() 触发后台扫描，结果回来后整体替换。"""

    loaded = Signal(str)          # 目录加载完成（含失败），参数为路径
    load_failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = []
        self.path = ""
        self._epoch = 0
        self._lock = threading.Lock()
        self._pool = QThreadPool.globalInstance()
        self.show_hidden = False

    # ---- 数据载入 ------------------------------------------------------
    def load(self, path):
        path = os.path.normpath(os.path.abspath(path))
        with self._lock:
            self._epoch += 1
            epoch = self._epoch
        task = ScanTask(epoch, path, self.show_hidden)
        task.sig.done.connect(self._on_done)
        task.sig.failed.connect(self._on_failed)
        self._pool.start(task)

    def _on_done(self, epoch, path, entries):
        if not self._is_current(epoch):
            return
        self.beginResetModel()
        self.path = path
        self.entries = entries
        self.endResetModel()
        self.loaded.emit(path)

    def _on_failed(self, epoch, path, err):
        if not self._is_current(epoch):
            return
        self.beginResetModel()
        self.path = path
        self.entries = []
        self.endResetModel()
        self.load_failed.emit(path, err)

    def _is_current(self, epoch):
        with self._lock:
            return epoch == self._epoch

    def refresh(self):
        if self.path:
            self.load(self.path)

    def set_show_hidden(self, on):
        self.show_hidden = on
        self.refresh()

    # ---- Qt 模型接口 ----------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):
        return 4

    def headerData(self, sec, orient, role=Qt.DisplayRole):
        if orient == Qt.Horizontal and role == Qt.DisplayRole:
            return COLS[sec]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.entries):
            return None
        e = self.entries[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == COL_NAME:
                return e.name
            if col == COL_SIZE:
                return "" if e.is_dir else _fmt_size(e.size)
            if col == COL_TYPE:
                return e.type_text()
            if col == COL_MTIME:
                return e.mtime_text()
        if role == Qt.DecorationRole:
            if col != COL_NAME:
                return None
            ic = sys_icon(e.path, e.is_dir)
            tags = FILE_TAGS.get(os.path.normcase(os.path.abspath(e.path)))
            if tags:
                from . import icons as _ic
                ic = _ic.tagged_icon(ic, tags)
            return ic
        if role == Qt.ToolTipRole:
            # 长文件名省略显示时，悬停可看全名
            return e.path if col == COL_NAME else e.name
        if role == Qt.UserRole:
            return e
        if role == Qt.TextAlignmentRole and col == COL_SIZE:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if e.hidden:
                return None
        return None

    def sort(self, col, order=Qt.AscendingOrder):
        reverse = (order == Qt.DescendingOrder)
        key_map = {
            COL_NAME: lambda e: (not e.is_dir, e.name.lower()),
            COL_SIZE: lambda e: (e.is_dir, e.size, e.name.lower()),
            COL_TYPE: lambda e: (not e.is_dir, e.type_text(), e.name.lower()),
            COL_MTIME: lambda e: e.mtime,
        }
        key = key_map.get(col, key_map[COL_NAME])
        self.layoutAboutToBeChanged.emit()
        self.entries.sort(key=key, reverse=reverse)
        self.layoutChanged.emit()

    def entry_at(self, row):
        return self.entries[row] if 0 <= row < len(self.entries) else None


class FsProxy(QSortFilterProxyModel):
    """按名称子串快速过滤（不区分大小写）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.filter_text = ""

    def set_filter(self, text):
        self.filter_text = (text or "").lower()
        self.invalidateFilter()

    @staticmethod
    def _match(term, name):
        """单条目匹配：~ 前缀为排除（NOT）。"""
        if term.startswith("~"):
            return term[1:] not in name
        return term in name

    def filterAcceptsRow(self, row, parent):
        """FreeCommander 快速过滤语法：
        a;b  → OR（含 a 或含 b）
        a&b  → AND（同时含 a 和 b）
        ~x   → NOT（不含 x）
        组合示例：pdf&inv;docx~old
        """
        if not self.filter_text:
            return True
        e = self.sourceModel().entry_at(row)
        if e is None:
            return False
        name = e.name.lower()
        for group in self.filter_text.split(";"):
            terms = [t.strip().lower() for t in group.split("&") if t.strip()]
            if not terms:
                continue
            if all(self._match(t, name) for t in terms):
                return True
        return False

    def lessThan(self, left, right):
        # 代理接管排序：按 Entry 字段比较，目录始终排前面
        m = self.sourceModel()
        a, b = m.entry_at(left.row()), m.entry_at(right.row())
        if a is None or b is None:
            return False
        col = left.column()
        if col == COL_SIZE:
            ka = (a.is_dir, a.size, a.name.lower())
            kb = (b.is_dir, b.size, b.name.lower())
        elif col == COL_MTIME:
            ka, kb = (a.is_dir, a.mtime), (b.is_dir, b.mtime)
        elif col == COL_TYPE:
            ka = (a.is_dir, a.type_text(), a.name.lower())
            kb = (b.is_dir, b.type_text(), b.name.lower())
        else:
            ka, kb = (a.name.lower(),), (b.name.lower(),)
        return ka < kb
