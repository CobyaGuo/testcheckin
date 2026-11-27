# -*- coding: utf-8 -*-
"""文件面板：路径栏 + 工具栏 + 目录树(可选) + 文件表/图标视图 + 驱动器栏。"""
import os
import string
import sys
from datetime import datetime

from PySide6.QtCore import (QDir, QMetaObject, QModelIndex, QPoint, Q_ARG,
                            QRunnable, QSize, Qt, QThreadPool, QTimer, Signal, Slot)
from PySide6.QtGui import (QAction, QGuiApplication, QColor, QPainter, QPen,
                           QStandardItem)
from PySide6.QtWidgets import QListView
from . import icons
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QCompleter, QFileDialog,
                               QFileSystemModel, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit,
                               QListView, QMenu, QPushButton, QSizePolicy, QSplitter,
                               QStackedWidget, QTableView, QToolButton,
                               QTreeView, QVBoxLayout, QWidget)

from .fsmodel import (COL_NAME, COL_SIZE, COL_TYPE, COL_MTIME, FsModel,
                      FsProxy, sys_icon)

VIEW_DETAIL, VIEW_ICONS = "detail", "icons"


def drive_letters():
    """枚举驱动器：用 GetLogicalDrives 位掩码，零磁盘 IO（os.path.exists
    对断开的网络盘/软驱可能阻塞数秒，曾导致启动卡死）。"""
    if os.name != "nt":
        return ["/"]
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        mask = 0
    return [ch + ":\\" for i, ch in enumerate(string.ascii_uppercase) if mask >> i & 1]


def icon_for(is_dir, ext):
    """轻量图标：用文字符号代替资源文件，避免依赖图标包。"""
    if is_dir:
        return "📁"
    if ext in ("py", "js", "ts", "c", "cpp", "h", "java", "rs", "go", "sh", "bat", "ps1", "cmd"):
        return "📜"
    if ext in ("png", "jpg", "jpeg", "gif", "bmp", "webp", "ico", "svg"):
        return "🖼"
    if ext in ("zip", "rar", "7z", "tar", "gz"):
        return "🗜"
    if ext in ("mp3", "wav", "flac", "ogg", "m4a"):
        return "🎵"
    if ext in ("mp4", "mkv", "avi", "mov", "webm"):
        return "🎬"
    if ext in ("doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "md", "txt"):
        return "📄"
    if ext in ("exe", "msi", "lnk"):
        return "⚙"
    return "📄"


def _fill_drive_tips_async(buttons):
    """后台线程算各驱动器容量后回填 tooltip，避免启动/建面板时卡 UI。"""
    import threading as _th

    def work():
        for b, d in buttons:
            try:
                import shutil as _sh
                u = _sh.disk_usage(d)
                tip = "%s  可用 %.1f GB / 共 %.1f GB" % (
                    d, u.free / 1073741824.0, u.total / 1073741824.0)
            except OSError:
                tip = d
            QMetaObject.invokeMethod(b, "setToolTip",
                                     Qt.QueuedConnection, Q_ARG(str, tip))
    _th.Thread(target=work, daemon=True).start()


def drive_tip(d):
    """驱动器按钮提示：总容量 + 可用空间。"""
    try:
        import shutil as _sh
        u = _sh.disk_usage(d)
        return "%s  可用 %.1f GB / 共 %.1f GB" % (
            d, u.free / 1073741824.0, u.total / 1073741824.0)
    except OSError:
        return d


class CrumbBar(QWidget):
    """面包屑地址栏（FC UiAddressBar）：路径分段按钮，点击跳转；
    每段带 ▾ 下拉列出其子文件夹；点击空白区进入路径编辑。

    长路径保护：路径深/段名长时若按内容自然伸展，会把 QSplitter 的左右
    面板撑到窗口之外（实测 1280 窗口里 pane 被撑到 1989px），收藏树/文件树
    被压成窄条。这里 ① sizeHint 不参与宽度决策（Ignored + minimumWidth 0）；
    ② 超过 5 段折叠中间为「…」菜单；③ 每段按可用宽度 elide，超长段不再撑开。
    """
    crumb_clicked = Signal(str)
    edit_requested = Signal()

    MAX_SEGMENTS = 5          # 超过则折叠中间段（保留盘符 + 末 3 段）
    MAX_SEGMENT_PX = 132      # 单段按钮最大宽度，超出 elide

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("crumbBar")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self._path = ""
        self.h = QHBoxLayout(self)
        self.h.setContentsMargins(0, 0, 0, 0)
        self.h.setSpacing(1)

    def set_path(self, path):
        if path == self._path:
            return
        self._path = path
        self._rebuild()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # 栏宽变化（拖动 splitter / 切换单双栏）后按新宽度重算可见段数与 elide。
        if abs(ev.size().width() - getattr(self, "_built_width", -1)) > 12:
            self._rebuild()

    def _rebuild(self):
        self._built_width = self.width()
        while self.h.count():
            it = self.h.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        path = self._path
        if not path:
            self.h.addStretch(1)
            return
        drive, rest = os.path.splitdrive(path)
        parts = []
        if drive:
            parts.append((drive + "\\", drive + "\\"))
        # 关键：必须以 "D:\\" 为起点，不能用 "D:"——os.path.join("D:", seg)
        # 得到的是盘符相对路径 "D:seg"（isdir/scandir 全部失败）。
        cur = drive + "\\" if drive else ""
        for seg in [s for s in rest.replace("/", "\\").split("\\") if s]:
            cur = os.path.normpath(os.path.join(cur, seg))
            parts.append((seg, cur))

        # 按当前宽度决定能放几段：每段成本 ≈ 文本宽 + ▾ 14px + spacing。
        budget = max(self.width(), 120)
        n = min(len(parts), self.MAX_SEGMENTS)
        while n > 2 and (budget - n * 16) // n < 34:
            n -= 1
        seg_px = max(28, min(self.MAX_SEGMENT_PX, (budget - n * 16) // n))

        hidden = []
        if n < len(parts):
            tail = max(1, n - 2)               # 盘符/首段 + … + 末 N
            hidden = parts[1:len(parts) - tail]
            shown = [(parts[0][0], parts[0][1], False),
                     ("…", "", True)] + \
                    [(l, t, False) for l, t in parts[-tail:]]
        else:
            shown = [(l, t, False) for l, t in parts]

        fm = self.fontMetrics()
        for label, target, collapsed in shown:
            if collapsed:
                b = QToolButton()
                b.setText("…")
                b.setAutoRaise(True)
                b.setFixedWidth(22)
                b.setFocusPolicy(Qt.NoFocus)
                m = QMenu(b)
                for hlabel, htarget in hidden:
                    act = m.addAction("📁 " + hlabel)
                    act.triggered.connect(
                        lambda _=False, pp=htarget: self.crumb_clicked.emit(pp))
                m.addSeparator()
                act = m.addAction("编辑完整路径…")
                act.triggered.connect(lambda _=False: self.edit_requested.emit())
                b.setMenu(m)
                b.setPopupMode(QToolButton.InstantPopup)
                b.setToolTip("折叠了 %d 级目录（点击展开）" % len(hidden))
                self.h.addWidget(b, 0, Qt.AlignLeft)
                continue
            elided = fm.elidedText(label, Qt.ElideMiddle, seg_px)
            b = QToolButton()
            b.setText(elided)
            b.setToolTip(target)
            b.setAutoRaise(True)
            b.setFocusPolicy(Qt.NoFocus)
            b.setMinimumWidth(0)                # 极端窄栏时允许被压缩，不溢出
            b.clicked.connect(lambda _=False, t=target: self.crumb_clicked.emit(t))
            self.h.addWidget(b, 0, Qt.AlignLeft)
            d = QToolButton()
            d.setText("▾")
            d.setAutoRaise(True)
            d.setFixedWidth(14)
            d.setFocusPolicy(Qt.NoFocus)
            d.setPopupMode(QToolButton.InstantPopup)
            m = QMenu(d)
            m.aboutToShow.connect(lambda mm=m, t=target: self._fill_menu(mm, t))
            d.setMenu(m)
            self.h.addWidget(d, 0, Qt.AlignLeft)
        self.h.addStretch(1)

    def _fill_menu(self, menu, target):
        menu.clear()
        try:
            with os.scandir(target) as it:
                dirs = sorted((d.name, d.path) for d in it if d.is_dir())
        except OSError:
            dirs = []
        for name, p in dirs[:200]:
            act = menu.addAction("📁 " + name)
            act.triggered.connect(lambda _=False, pp=p: self.crumb_clicked.emit(pp))
        if not dirs:
            menu.addAction("（无子文件夹）").setEnabled(False)

    def mousePressEvent(self, ev):
        self.edit_requested.emit()
        super().mousePressEvent(ev)


class EntryIconModel(FsProxy):
    """图标视图的数据装饰：DisplayRole 渲染成"图标 名称"。"""

    def data(self, index, role=Qt.DisplayRole):
        e = self.sourceModel().entry_at(index.row()) if index.isValid() else None
        if role == Qt.DisplayRole and e is not None:
            return e.name
        if role == Qt.UserRole:
            return e
        return super().data(index, role)


class FilePane(QWidget):
    """单侧文件面板：可选目录树 + 详细/图标两种视图。"""
    path_changed = Signal(str)
    status_changed = Signal()
    activate = Signal(object)
    focus_requested = Signal(object)
    quick_searched = Signal(str)     # 快速搜索关键字变化（状态栏显示）
    smart_done = Signal(int, object, bool, bool)  # 智能搜索回传（线程→UI，流式分批；final 收尾；是否截断）

    filter_both = Signal(str)        # Ctrl+Enter：把过滤条件应用到所有面板

    def __init__(self, parent=None, strip_side="left"):
        super().__init__(parent)
        # 普通 QWidget 子类默认不绘制 QSS border/background，必须显式开启
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._make_glow()
        self._history = []
        self.tab_lock = ""      # ""=未锁定 nav=允许导航 sub=仅子文件夹 hard=禁止导航
        self.lock_root = "" 
        self._hist_pos = -1
        self._view_mode = VIEW_DETAIL
        self._tree_visible = False
        self._strip_side = strip_side
        # 刷新前后的视图状态保存/恢复（选中单条目 + 顶部可见条目路径）
        self._nav_snapshot = None
        self._selection_touched = False

        # ---- 模型 ----
        self.model = FsModel(self)
        self.proxy = FsProxy(self)
        self.proxy.setSourceModel(self.model)
        self.model.loaded.connect(self._on_loaded)
        self.model.load_failed.connect(self._on_load_failed)
        self.smart_done.connect(self._on_smart_results)
        # 区分「模型重置导致的选中清空」与「用户主动改选」
        self._model_resetting = False
        self.model.modelAboutToBeReset.connect(self._on_model_reset_begin)
        self.model.modelReset.connect(self._on_model_reset_end)

        # ---- 路径栏 ----
        self.path_edit = QComboBox()
        self.path_edit.setEditable(True)
        self.path_edit.setObjectName("pathBar")
        self.path_edit.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.path_edit.setMinimumContentsLength(30)
        # 30 字符的 minimumContentsLength 会作为 minimumSizeHint 参与 QSplitter
        # 分配：长路径编辑时把左右面板撑变形。设为 0，实际宽度由 layout stretch
        # 决定，超长文本靠 lineEdit 横向滚动。
        self.path_edit.setMinimumWidth(0)
        self.path_edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.path_edit.lineEdit().returnPressed.connect(self._path_submitted)
        # 地址栏自动完成（FC UiAddressBar）：输入时弹出路径建议。
        # 保留自动完成，但 activated 与 returnPressed 都统一走 _path_submitted：
        # 回车时 QComboBox 会连发 activated + returnPressed，且 setCurrentText 还
        # 会延迟派发带旧 index 的 activated；若 activated 读 itemText(i) 与
        # returnPressed 的输入框新值竞争，会触发多次 navigate 互相作废扫描 epoch。
        self._path_cmpl_model = QFileSystemModel(self)
        self._path_cmpl_model.setRootPath("")
        self._path_cmpl_model.setFilter(QDir.Dirs | QDir.Drives | QDir.NoDotAndDotDot)
        _c = QCompleter(self._path_cmpl_model, self)
        _c.setCompletionMode(QCompleter.PopupCompletion)
        _c.setCaseSensitivity(Qt.CaseInsensitive)
        self.path_edit.setCompleter(_c)
        self._path_submitting = False
        self.path_edit.activated.connect(lambda i: self._path_submitted())

        # 面包屑地址栏（UiAddressBar）：分段按钮 + 子文件夹下拉
        self.crumb = CrumbBar()
        self.crumb.crumb_clicked.connect(self.navigate)
        self.crumb.edit_requested.connect(self._edit_path)

        # 地址栏动作工具栏（Address bar - actions toolbar）
        def abar_btn(ic, tip, fn):
            b = QToolButton()
            b.setIcon(ic)
            b.setIconSize(QSize(16, 16))
            b.setAutoRaise(True)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            return b
        self.addr_bar = QHBoxLayout()
        self.addr_bar.setSpacing(2)
        self.addr_bar.addStretch(1)
        from . import icons as _icons
        self.addr_bar.addWidget(abar_btn(_icons.icon("rename"), "编辑路径 (Alt+G)", self._edit_path))
        self.addr_bar.addWidget(abar_btn(_icons.icon("folder_new"), "浏览文件夹…", self._browse_path))
        self.addr_bar.addWidget(abar_btn(_icons.icon("copy"), "复制完整路径 (Ctrl+Alt+I)", self._copy_path))
        self.addr_bar.addWidget(abar_btn(_icons.icon("up"), "上级目录", self.go_up))

        # ---- 工具栏 ----
        from . import icons as _icons
        self.btn_tree = QPushButton(_icons.icon("tree"), "树")
        self.btn_view = QPushButton(_icons.icon("view"), "视图")
        btn_up = QPushButton(_icons.icon("up"), "上级")
        btn_back = QPushButton(_icons.icon("back"), "后退")
        btn_fwd = QPushButton(_icons.icon("forward"), "前进")
        btn_refresh = QPushButton(_icons.icon("refresh"), "刷新")
        for b in (self.btn_tree, self.btn_view, btn_up, btn_back, btn_fwd, btn_refresh):
            b.setFlat(True)
            b.setIconSize(QSize(16, 16))
        btn_up.clicked.connect(self.go_up)
        btn_back.clicked.connect(self.go_back)
        btn_fwd.clicked.connect(self.go_forward)
        btn_refresh.clicked.connect(self.refresh)
        self.btn_tree.clicked.connect(self.toggle_tree)  # 转发主窗口共享树
        self.btn_view.clicked.connect(self._view_menu)

        # 终端按钮：在当前路径打开系统默认终端
        self.btn_term = QPushButton(icons.icon("terminal"), "终端")
        self.btn_term.setFlat(True)
        self.btn_term.setIconSize(QSize(16, 16))
        self.btn_term.setToolTip("在当前目录打开系统默认终端")
        self.btn_term.clicked.connect(self.open_terminal)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("🔍 过滤（a&b 与 / a;b 或 / ~x 排除）")
        self.filter_edit.setToolTip("支持 FreeCommander 运算符：\n"
                                    "a&b 同时包含\na;b 任一包含\n~x 排除\n"
                                    "Ctrl+Enter 应用到两个面板")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMaximumWidth(220)
        self.filter_edit.textChanged.connect(self.proxy.set_filter)
        self.filter_edit.installEventFilter(self)

        tools = QHBoxLayout()
        tools.setContentsMargins(4, 4, 4, 0)
        tools.setSpacing(4)
        for w in (btn_back, btn_fwd, btn_up, btn_refresh, self.btn_tree, self.btn_view,
                  self.btn_term):
            tools.addWidget(w)
        tools.addStretch(1)
        tools.addWidget(self.filter_edit)

        # ---- 详细视图（文件表）----
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        hh = self.table.horizontalHeader()
        # 性能关键：不用 ResizeToContents/resizeColumnsToContents——
        # 那会对全部行×列逐个测量，万级条目直接卡死；改固定宽度。
        hh.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        for col, width in ((COL_SIZE, 100), (COL_TYPE, 110), (COL_MTIME, 150)):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)
            hh.resizeSection(col, width)
        hh.setStretchLastSection(False)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_NAME, Qt.AscendingOrder)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(self._on_double)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.status_changed.emit())

        # ---- 图标视图 ----
        self.icon_proxy = EntryIconModel(self)
        self.icon_proxy.setSourceModel(self.model)
        self.icons = QListView()
        self.icons.setViewMode(QListView.IconMode)
        self.icons.setResizeMode(QListView.Adjust)
        self.icons.setGridSize(QSize(92, 72))
        self.icons.setSelectionMode(QListView.ExtendedSelection)
        self.icons.setEditTriggers(QListView.NoEditTriggers)
        self.icons.setContextMenuPolicy(Qt.CustomContextMenu)
        self.icons.customContextMenuRequested.connect(self._context_menu)
        self.icons.doubleClicked.connect(self._on_double)
        self.icons.setModel(self.icon_proxy)
        self.icons.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.icons.selectionModel().selectionChanged.connect(
            lambda *_: self.status_changed.emit())

        # 快速搜索（Quick search）：面板内直接打字即定位；Esc 取消
        self._qs_text = ""
        self.table.viewport().installEventFilter(self)
        self.icons.viewport().installEventFilter(self)
        self.table.installEventFilter(self)
        self.icons.installEventFilter(self)

        # 智能文件夹搜索结果视图（页 2）
        from PySide6.QtGui import QStandardItemModel, QStandardItem
        self.results_model = QStandardItemModel(0, 1, self)
        self.results_list = QListView()
        self.results_list.setModel(self.results_model)
        self.results_list.setFrameShape(QListView.NoFrame)
        self.results_list.setEditTriggers(QListView.NoEditTriggers)
        self.results_list.setUniformItemSizes(True)
        self.results_list.doubleClicked.connect(self._result_activated)
        self.results_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results_list.customContextMenuRequested.connect(self._results_menu)
        self._smart_epoch = 0
        self._smart_info = ("", "", "")

        self.stack = QStackedWidget()
        self.stack.addWidget(self.table)   # 0 detail
        self.stack.addWidget(self.icons)   # 1 icons
        self.stack.addWidget(self.results_list)  # 2 smart results
        self.stack.currentChanged.connect(lambda *_: self.status_changed.emit())

        # ---- 目录树（每面板可选，QFileSystemModel 自带线程化 IO）----
        # 目录树全局唯一（主窗口左侧），面板内不再各带一棵

        # ---- 驱动器栏 ----
        self.drive_bar = QHBoxLayout()
        self.drive_bar.setContentsMargins(4, 4, 4, 0)
        self.drive_bar.setSpacing(3)
        self._drive_btns = []
        for d in drive_letters():
            b = QPushButton(d)
            b.setFlat(True)
            b.setIcon(sys_icon(d, is_dir=True))
            b.setToolTip(d)   # 容量提示由后台线程补充（disk_usage 可能很慢）
            b.clicked.connect(lambda _=False, dd=d: self.navigate(dd))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(
                lambda pos, dd=d, bb=b: self._drive_menu(dd, bb))
            self._drive_btns.append((b, d))
            self.drive_bar.addWidget(b)
        _fill_drive_tips_async(self._drive_btns)
        self.drive_bar.addStretch(1)

        # 驱动器占用空间信息栏（Drive occupied space info）
        self.drive_info = QLabel("")
        self.drive_info.setObjectName("driveInfo")
        self.drive_info.setFixedHeight(16)

        # Go up strips：面板外沿的上级目录条带（点一次进一级）
        self.up_strip = QToolButton()
        self.up_strip.setObjectName("upStrip")
        self.up_strip.setText("▲\n上\n级")
        self.up_strip.setToolTip("打开上级文件夹")
        self.up_strip.setFixedWidth(14)
        self.up_strip.clicked.connect(self.go_up)

        list_row = QHBoxLayout()
        list_row.setContentsMargins(0, 0, 0, 0)
        list_row.setSpacing(0)
        if self._strip_side == "left":
            list_row.addWidget(self.up_strip)
        list_row.addWidget(self.stack, 1)
        if self._strip_side == "right":
            list_row.addWidget(self.up_strip)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        path_row = QHBoxLayout()
        path_row.setContentsMargins(4, 4, 4, 0)
        path_row.setSpacing(4)
        path_row.addWidget(self.crumb, 1)
        path_row.addWidget(self.path_edit, 1)
        self.path_edit.setVisible(False)   # 面包屑为主显示，✏/Alt+G 才出现编辑框
        path_row.addLayout(self.addr_bar)
        v.addLayout(path_row)
        v.addLayout(tools)
        v.addLayout(list_row, 1)
        v.addLayout(self.drive_bar)
        v.addWidget(self.drive_info)

    def _drive_menu(self, d, btn):
        """驱动器按钮右键菜单（FC Drivebar 上下文菜单精简版）。"""
        m = QMenu(self)
        a_open = m.addAction("打开 " + d)
        a_open.triggered.connect(lambda: self.navigate(d))
        a_exp = m.addAction("在资源管理器中打开")
        a_exp.triggered.connect(lambda: os.startfile(d))
        m.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    # ---- 地址栏动作 ----
    def _edit_path(self):
        self.path_edit.setVisible(True)
        self.path_edit.setFocus()
        self.path_edit.lineEdit().selectAll()

    def _browse_path(self):
        d = QFileDialog.getExistingDirectory(self, "浏览文件夹", self.model.path or "")
        if d:
            self.navigate(d)

    def _copy_path(self):
        QGuiApplication.clipboard().setText(self.model.path)

    # ---- 视图模式 --------------------------------------------------------
    def set_view_mode(self, mode):
        self._view_mode = mode
        self.stack.setCurrentIndex(0 if mode == VIEW_DETAIL else 1)

    def _view_menu(self):
        m = QMenu(self)
        a1 = m.addAction("详细列表")
        a2 = m.addAction("大图标")
        a1.triggered.connect(lambda: self.set_view_mode(VIEW_DETAIL))
        a2.triggered.connect(lambda: self.set_view_mode(VIEW_ICONS))
        m.exec(self.btn_view.mapToGlobal(self.btn_view.rect().bottomLeft()))

    # ---- 目录树 ----------------------------------------------------------
    def toggle_tree(self, on=None):
        # 树是共享的：转发给主窗口
        if hasattr(self, "main") and self.main:
            self.main.toggle_tree()

    # ---- 导航 ------------------------------------------------------------
    def navigate(self, path, push_history=True):
        path = path.strip()
        if not path:
            return
        path = os.path.normpath(os.path.expandvars(path))
        if not os.path.isdir(path):
            self._set_path_text(self.model.path)
            return
        # 目录导航必须回到文件列表视图：否则从智能文件夹结果页（页 2）
        # 点收藏/树/面包屑后，model 已加载但界面还停在结果页，看起来"无响应"。
        if self.stack.currentIndex() == 2:
            self.stack.setCurrentIndex(0 if self._view_mode == VIEW_DETAIL else 1)
        lock = getattr(self, "tab_lock", "")
        if lock == "hard":
            self._set_path_text(self.model.path)
            return
        if lock == "sub":
            root = os.path.normcase(getattr(self, "lock_root", "") or self.model.path)
            new = os.path.normcase(path)
            if new != root and not new.startswith(root.rstrip("\\") + "\\"):
                self._set_path_text(self.model.path)
                return
        if push_history and self._hist_pos >= 0 and path == self._history[self._hist_pos]:
            # 不能用 model.refresh()：它读 model.path，而异步扫描可能还没完成，
            # model.path 仍是上一个目录，会把刚提交的导航"回退"到旧目录。
            # 直接按请求路径重新扫描，重复 navigate 同路径也是幂等的。
            self.model.load(path)
            return
        if push_history:
            self._history = self._history[:self._hist_pos + 1]
            self._history.append(path)
            if len(self._history) > 100:
                self._history.pop(0)
            self._hist_pos = len(self._history) - 1
        self._set_path_text(path)
        self._update_drive_info(path)
        self.model.load(path)

    def _set_path_text(self, path):
        self.path_edit.blockSignals(True)
        self.path_edit.setCurrentText(path)
        if self.path_edit.findText(path) < 0:
            self.path_edit.addItem(path)
            while self.path_edit.count() > 15:   # 历史封顶，下拉不无限膨胀
                self.path_edit.removeItem(0)
        self.path_edit.blockSignals(False)
        self.path_edit.setToolTip(path)
        self.crumb.set_path(path)

    def _path_submitted(self):
        # 同一次回车可能同时触发 activated / returnPressed / setCurrentText
        # 延迟派发的 activated；只认第一次，避免多次 navigate 互相作废扫描。
        if self._path_submitting:
            return
        self._path_submitting = True
        try:
            # 直接读输入框文本：currentText() 在下拉项含相同前缀时可能返回被
            # 自动匹配的历史项，而非用户刚粘贴/输入的内容。
            text = self.path_edit.lineEdit().text().strip()
            self.navigate(text)
            self.path_edit.setVisible(False)
        finally:
            # 延迟派发的 activated 可能在事件循环下一轮才到，延后解锁
            QTimer.singleShot(0, self._clear_path_submitting)

    def _clear_path_submitting(self):
        self._path_submitting = False

    def go_up(self):
        p = self.model.path
        parent = os.path.dirname(p) if p else None
        if parent and parent != p:
            self.navigate(parent)

    def go_back(self):
        if self._hist_pos > 0:
            self._hist_pos -= 1
            self.navigate(self._history[self._hist_pos], push_history=False)

    def go_forward(self):
        if self._hist_pos < len(self._history) - 1:
            self._hist_pos += 1
            self.navigate(self._history[self._hist_pos], push_history=False)

    def refresh(self):
        """刷新当前目录：异步扫描前记录视图位置，扫描完成后恢复。"""
        self._snapshot_nav_state()
        self.model.refresh()

    def current_path(self):
        return self.model.path

    # ---- 刷新前后的视图状态保持 ----------------------------------------
    def _snapshot_nav_state(self):
        """在刷新发起的瞬间记录状态；只有刷新完成仍是同一目录才会恢复。"""
        self._selection_touched = False
        path = self.model.path
        if not path or self.stack.currentIndex() == 2:
            self._nav_snapshot = None       # 智能搜索结果页不参与
            return
        view = self._cur_view()
        cur = self.current_entry()
        top = self._first_visible_entry(view)
        self._nav_snapshot = {
            "path": path,
            "view": 0 if view is self.table else 1,
            "current": cur.path if cur else None,
            "top": top.path if top else None,
        }

    def _first_visible_entry(self, view):
        """当前视图最上面可见行的条目（用于把滚动位置还原到同一条目）。"""
        try:
            idx = view.indexAt(QPoint(0, 0))
            if not idx.isValid():
                return None
            proxy = self.proxy if view is self.table else self.icon_proxy
            src = proxy.mapToSource(proxy.index(idx.row(), 0))
            return src.data(Qt.UserRole)
        except Exception:
            return None

    def _restore_nav_state(self, path):
        """加载完成后恢复选中与滚动；换目录/用户已改选/条目失效则相应跳过。"""
        snap = self._nav_snapshot
        self._nav_snapshot = None
        if not snap or snap.get("path") != path:
            return                          # 导航到新目录：不恢复旧目录的位置
        view = self.table if snap["view"] == 0 else self.icons
        proxy = self.proxy if snap["view"] == 0 else self.icon_proxy

        # 选中：快照里的 current 在用户改选时已被 _on_selection_changed 更新，
        # 因此这里恢复的始终是用户最新的选择。
        if snap.get("current"):
            target = None
            for i, e in enumerate(self.model.entries):
                if e.path == snap["current"]:
                    target = i
                    break
            if target is not None:
                pr = proxy.mapFromSource(self.model.index(target, 0)).row()
                if pr >= 0:
                    idx = proxy.index(pr, 0)
                    view.setCurrentIndex(idx)
                    if view is self.table:
                        view.selectRow(pr)

        # 滚动：按顶部可见条目的新行号定位（不保存像素偏移）
        if snap.get("top"):
            for i, e in enumerate(self.model.entries):
                if e.path == snap["top"]:
                    pr = proxy.mapFromSource(self.model.index(i, 0)).row()
                    if pr >= 0:
                        view.scrollTo(proxy.index(pr, 0),
                                      QAbstractItemView.PositionAtTop)
                    break

    def _on_loaded(self, path):
        self._set_path_text(path)
        self.path_changed.emit(path)
        # 等代理模型按新数据重排完成（layoutChanged）后再恢复位置
        QTimer.singleShot(0, lambda p=path: self._restore_nav_state(p))

    _drive_info_cache = {}   # 盘符 -> (时间戳, 文本, 百分比)，避免频繁 disk_usage

    def _update_drive_info(self, path):
        """刷新驱动器占用信息条（蓝色=已用，剩余=可用）。60 秒缓存。"""
        try:
            import shutil as _sh
            import time as _time
            drv = os.path.splitdrive(os.path.abspath(path or os.getcwd()))[0].upper()
            cached = self._drive_info_cache.get(drv)
            if cached and _time.time() - cached[0] < 60:
                _, text, pct = cached
                self.drive_info.setText(text)
                self._apply_drive_style(pct)
                return
            u = _sh.disk_usage(path or os.getcwd())
            pct = int(u.used * 100 / u.total) if u.total else 0
            text = "%s 已用 %d%%    可用 %.1f GB / 共 %.1f GB" % (
                os.path.splitdrive(os.path.abspath(path or os.getcwd()))[0],
                pct, u.free / 1073741824.0, u.total / 1073741824.0)
            self.drive_info.setText(text)
            self._apply_drive_style(pct)
            self._drive_info_cache[drv] = (_time.time(), text, pct)
        except OSError:
            self.drive_info.setText("")

    def _apply_drive_style(self, pct):
        """占用条配色随主题：深色用压暗警示色，浅色用正常强度色。"""
        from PySide6.QtWidgets import QApplication
        light = QApplication.instance().property("theme") == "light"
        if light:
            color = "#2563eb" if pct < 80 else ("#b45309" if pct < 92 else "#b91c1c")
            bg, fg = "#dde2ec", "#3a3f4d"
        else:
            color = "#3b82f6" if pct < 80 else ("#a17c26" if pct < 92 else "#a8483c")
            bg, fg = "#242836", "#aab1c2"
        p = min(max(pct, 0), 99) / 100.0
        self.drive_info.setStyleSheet(
            "QLabel#driveInfo{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 %s, stop:%f %s, stop:%f %s);border-radius:4px;"
            "color:%s;font-size:9.5px;padding:0 6px;}"
            % (color, p, color, p + 0.001, bg, fg))

    def _on_load_failed(self, path, err):
        self.path_changed.emit(path)
        self.status_changed.emit()

    # ---- 选择（两种视图通用）---------------------------------------------
    def _on_model_reset_begin(self):
        self._model_resetting = True

    def _on_model_reset_end(self):
        # 让本次 reset 连锁触发的 selectionChanged 全部走完再解除标记
        QTimer.singleShot(0, lambda: setattr(self, "_model_resetting", False))

    def _on_selection_changed(self, *_):
        if self._model_resetting or self._nav_snapshot is None:
            return
        # 刷新发起后用户若改选，记下新选择：刷新完成后按它恢复，
        # 而不是恢复发起瞬间保存的旧选择（模型 reset 会清空当前选中）。
        self._selection_touched = True
        cur = self.current_entry()
        self._nav_snapshot["current"] = cur.path if cur else None

    def _cur_view(self):
        return self.table if self.stack.currentIndex() == 0 else self.icons

    def selected_entries(self):
        view = self._cur_view()
        rows = sorted({i.row() for i in view.selectionModel().selectedRows()
                       if i.isValid()})
        return [self.proxy.mapToSource(self.proxy.index(r, 0)).data(Qt.UserRole)
                for r in rows]

    def current_entry(self):
        view = self._cur_view()
        idx = view.currentIndex()
        if not idx.isValid():
            return None
        e = self.proxy.mapToSource(self.proxy.index(idx.row(), 0)).data(Qt.UserRole)
        return e

    def select_single(self, entry):
        if entry is None:
            return
        for r, e in enumerate(self.model.entries):
            if e.path == entry.path:
                pr = self.proxy.mapFromSource(self.model.index(r, 0)).row()
                self._cur_view().selectRow(pr) if hasattr(self._cur_view(), "selectRow") \
                    else self._cur_view().setCurrentIndex(self.proxy.index(pr, 0))
                return

    # ---- 交互 ------------------------------------------------------------
    def _on_double(self, index):
        e = self.proxy.mapToSource(self.proxy.index(index.row(), 0)).data(Qt.UserRole)
        if e:
            self.activate.emit(e)

    def _context_menu(self, pos: QPoint):
        e = self.current_entry()
        menu = QMenu(self)
        if e:
            a_open = menu.addAction("打开" if e.is_dir else "打开（系统默认程序）")
            a_open.triggered.connect(lambda: self.activate.emit(e))
            menu.addSeparator()
            a_copy_name = menu.addAction("复制文件名")
            a_copy_name.triggered.connect(lambda: QGuiApplication.clipboard().setText(e.name))
            a_copy_path = menu.addAction("复制完整路径")
            a_copy_path.triggered.connect(lambda: QGuiApplication.clipboard().setText(e.path))
            a_rename = menu.addAction("重命名 (F2)")
            a_rename.triggered.connect(self.main.rename_selected)
            a_batch = menu.addAction("批量重命名…")
            a_batch.triggered.connect(self.main.batch_rename)
            menu.addSeparator()
            if not e.is_dir and e.ext == "zip":
                a_unzip = menu.addAction("解压到此处")
                a_unzip.triggered.connect(self.main.unpack_here)
            else:
                a_zip = menu.addAction("压缩为 ZIP…")
                a_zip.triggered.connect(self.main.zip_selected)
            a_md5 = menu.addAction("计算 MD5")
            a_md5.triggered.connect(self.main.calc_md5)
            # 标签：彩色圆点叠加在图标上，持久化到 cfg
            tag_menu = menu.addMenu(icons.icon("tag"), "标签")
            names = {"#ff453a": "红色", "#ff9f0a": "橙色", "#ffd60a": "黄色",
                     "#34c759": "绿色", "#0a84ff": "蓝色", "#5e6ad2": "靛蓝",
                     "#af52de": "紫色", "#98a0ac": "灰色"}
            for c in names:
                act_c = tag_menu.addAction(icons.color_dot(c), names[c])
                act_c.triggered.connect(lambda _=False, cc=c: self.main.tag_entry(cc))
            a_untag = tag_menu.addAction("移除标签")
            a_untag.triggered.connect(lambda: self.main.tag_entry(None))
            menu.addSeparator()
            a_del = menu.addAction("删除到回收站 (Delete / F8)")
            a_del.triggered.connect(self.main.delete_selected)
            a_purge = menu.addAction("永久删除 (Shift+Delete)")
            a_purge.triggered.connect(self.main.purge_selected)
        a_paste = menu.addAction("粘贴到此处 (Ctrl+V)")
        a_paste.triggered.connect(self.main.paste_into_pane)
        menu.addSeparator()
        a_newdir = menu.addAction("新建文件夹 (F7)")
        a_newdir.triggered.connect(self.main.new_folder)
        a_open_shell = menu.addAction("在此处打开终端")
        a_open_shell.triggered.connect(self.open_terminal)
        a_props = menu.addAction("刷新")
        a_props.triggered.connect(self.refresh)
        menu.exec(self._cur_view().viewport().mapToGlobal(pos))

    def open_terminal(self):
        """在当前活动面板路径打开系统默认终端（优先 Windows Terminal）。"""
        import shutil, subprocess
        path = self.model.path or None
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            if shutil.which("wt"):
                subprocess.Popen(["wt.exe", "-d", path or "."], creationflags=flags)
            else:
                subprocess.Popen(["cmd", "/K", f'cd /d "{path}"'], cwd=path,
                                 creationflags=flags)
        except Exception:
            try:
                subprocess.Popen(["powershell"], cwd=path, creationflags=flags)
            except Exception:
                pass

    # ---- 智能文件夹（递归搜索结果视图）--------------------------------
    def show_smart(self, name, scope, query):
        """打开智能文件夹：在 scope 内递归搜索 query，结果展示在当前面板。"""
        scope = os.path.abspath(os.path.expanduser(scope or "."))
        if not os.path.isdir(scope):
            self._smart_epoch += 1
            self._smart_info = (name, scope, query)
            self.results_model.removeRows(0, self.results_model.rowCount())
            tip = QStandardItem(f"（无效的搜索范围：{scope}）")
            tip.setFlags(Qt.NoItemFlags)
            self.results_model.invisibleRootItem().appendRow(tip)
            self.stack.setCurrentWidget(self.results_list)
            return
        self._smart_epoch += 1
        self._smart_info = (name, scope, query)
        self._smart_truncated = False
        self.results_model.removeRows(0, self.results_model.rowCount())
        root = self.results_model.invisibleRootItem()
        tip = QStandardItem(f"⌛ 正在搜索：{query or '*'} （范围：{scope}）")
        tip.setFlags(Qt.NoItemFlags)
        root.appendRow(tip)
        self.stack.setCurrentWidget(self.results_list)
        task = _SmartSearch(self, self._smart_epoch, scope, query)
        QThreadPool.globalInstance().start(task)

    @Slot(int, object, bool, bool)
    def _smart_results(self, epoch, batch, final, truncated):
        self._on_smart_results(epoch, batch, final, truncated)

    def _on_smart_results(self, epoch, batch, final, truncated=False):
        if epoch != self._smart_epoch:
            return
        if final:
            self._smart_truncated = bool(truncated)
        root = self.results_model.invisibleRootItem()
        # 第一批到达时清掉"⌛ 正在搜索"提示行（提示行无 UserRole 数据）
        for i in range(root.rowCount() - 1, -1, -1):
            if root.child(i).data(Qt.UserRole) is None:
                root.removeRow(i)
        for e in batch:
            it = QStandardItem(e.name)
            it.setIcon(sys_icon(e.path, e.is_dir))
            it.setData(e, Qt.UserRole)
            it.setToolTip(e.path)
            root.appendRow(it)
        self.status_changed.emit()
        if final:
            name, scope, query = self._smart_info
            if self.results_model.rowCount() == 0:
                tip = QStandardItem(f"（无结果：{query or '*'} ／ {scope}）")
                tip.setFlags(Qt.NoItemFlags)
                root.appendRow(tip)
            elif truncated:
                tip = QStandardItem("⚠ 结果已达上限 %d 条，仅显示前 %d 条；请缩小范围或细化条件"
                                    % (_SmartSearch.MAX_RESULTS, self.results_model.rowCount()))
                tip.setFlags(Qt.NoItemFlags)
                root.appendRow(tip)

    def _results_menu(self, pos):
        name, scope, query = self._smart_info
        if not scope:
            return
        m = QMenu(self)
        a_save = m.addAction("保存为智能文件夹…")
        a_cp = m.addAction("复制全部结果路径")
        act = m.exec(self.results_list.viewport().mapToGlobal(pos))
        if act is a_save and getattr(self, "main", None):
            self.main.fav_tree._smart_dialog(
                prefill={"name": name, "scope": scope, "query": query})
        elif act is a_cp:
            paths = []
            for r in range(self.results_model.rowCount()):
                e = self.results_model.item(r).data(Qt.UserRole)
                if e:
                    paths.append(e.path)
            QGuiApplication.clipboard().setText("\n".join(paths))

    def _result_activated(self, index):
        e = index.data(Qt.UserRole)
        if e is not None:
            self.activate.emit(e)

    def smart_status(self):
        """智能结果视图下的状态栏文本。"""
        name, scope, query = self._smart_info
        suffix = "（已截断，仅前 %d 条）" % _SmartSearch.MAX_RESULTS \
            if getattr(self, "_smart_truncated", False) else ""
        return f"智能文件夹 [{name}]：{self.results_model.rowCount()} 个结果{suffix}"

    def eventFilter(self, obj, ev):
        """快速搜索：可打印字符累积匹配，Backspace 回退，Esc/失焦清除。"""
        from PySide6.QtCore import QEvent
        if obj is self.filter_edit and ev.type() == QEvent.KeyPress \
                and ev.key() in (Qt.Key_Return, Qt.Key_Enter) \
                and ev.modifiers() & Qt.ControlModifier:
            self.filter_both.emit(self.filter_edit.text())
            return True
        if ev.type() == QEvent.KeyPress and obj in (self.table, self.icons,
                                                    self.table.viewport(),
                                                    self.icons.viewport()):
            if self._qs_text and ev.key() in (Qt.Key_Return, Qt.Key_Enter):
                idx = self._cur_view().currentIndex()
                if idx.isValid():
                    e = self.model.entry_at(self.proxy.mapToSource(idx).row())
                    if e is not None:
                        self.activate.emit(e)
                        self._qs_text = ""
                        self.quick_searched.emit("")
                return True
            if self._qs_text and ev.key() in (Qt.Key_Down, Qt.Key_Up):
                self._quick_search(backward=(ev.key() == Qt.Key_Up), step=True)
                self.quick_searched.emit(self._qs_text)
                return True
            t = ev.text()
            if t and t.isprintable() and not t.isspace() and not ev.modifiers() & ~Qt.ShiftModifier:
                self._qs_text += t
                if self._quick_search():
                    self.quick_searched.emit(self._qs_text)
                    return True
                self._qs_text = self._qs_text[:-1]
            elif ev.key() == Qt.Key_Backspace and self._qs_text:
                self._qs_text = self._qs_text[:-1]
                self._quick_search()
                self.quick_searched.emit(self._qs_text)
                return True
            elif ev.key() == Qt.Key_Escape and self._qs_text:
                self._qs_text = ""
                self.quick_searched.emit("")
                return True
        elif ev.type() == QEvent.FocusOut:
            self._qs_text = ""
            self.quick_searched.emit("")
        return super().eventFilter(obj, ev)

    def _quick_search(self, backward=False, step=False):
        """找匹配项；step=True 时 ↓/↑ 从下一/上一行开始循环。"""
        if not self._qs_text:
            return False
        low = self._qs_text.lower()
        n = self.proxy.rowCount()
        if not n:
            return False
        cur = self._cur_view().currentIndex().row()
        if step:
            cur += -1 if backward else 1
        if backward:
            order = list(range(cur, -1, -1)) + list(range(n - 1, cur, -1))
        else:
            order = list(range(cur, n)) + list(range(0, cur))
        for r in order:
            if not 0 <= r < n:
                continue
            name = self.proxy.index(r, 0).data(Qt.DisplayRole) or ""
            if low in name.lower():
                self._cur_view().setCurrentIndex(self.proxy.index(r, 0))
                return True
        return False

    def focusInEvent(self, ev):
        self.focus_requested.emit(self)
        super().focusInEvent(ev)

    def _make_glow(self):
        """活动高亮覆盖层：置顶绘制整圈描边，不被子控件遮挡。"""
        self._glow = _ActiveGlow(self)
        self._glow.hide()

    def set_active(self, on):
        """活动面板描边高亮（顶层覆盖层绘制，保证盖在子控件之上）。"""
        if self.property("active") == on:
            return
        self.setProperty("active", on)
        self.style().unpolish(self)
        self.style().polish(self)
        self._glow.setVisible(bool(on))
        if on:
            self._glow.raise_()
            self._glow.setGeometry(self.rect())
            self._glow.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_glow"):
            self._glow.setGeometry(self.rect())


class _ActiveGlow(QWidget):
    """活动面板高亮覆盖层（不拦截鼠标）。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        dark = (QApplication.instance().property("theme") != "light")
        color = QColor("#46608c") if dark else QColor("#b9c6dc")
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(color, 2))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), 6, 6)
        p.end()


class _SmartSearch(QRunnable):
    """智能文件夹后台递归搜索：空格 AND，分号 OR；结果流式回传。

    条件语法（可与名称子串混用）：
      ext:py,js          扩展名
      size:>10mb / <1k   大小（b/k/m/g）
      date:>2025-01-01   修改时间（YYYY[-MM[-DD]]）
      type:dir|file
      tag:红 / #ff453a   文件标签（颜色名或十六进制）
      /正则/             文件名正则
    """
    SKIP_DIRS = {".git", "__pycache__", "$RECYCLE.BIN",
                 "System Volume Information", "node_modules", ".svn"}
    MAX_RESULTS = 2000
    BATCH = 200

    _TAG_NAMES = {"红": "#ff453a", "橙": "#ff9f0a", "黄": "#ffd60a", "绿": "#34c759",
                  "蓝": "#0a84ff", "靛": "#5e6ad2", "紫": "#af52de", "灰": "#98a0ac"}
    _UNITS = {"": 1, "b": 1, "k": 1024, "m": 1048576, "g": 1073741824}

    class _Sig:
        pass

    def __init__(self, pane, epoch, scope, query):
        super().__init__()
        self.pane, self.epoch, self.scope = pane, epoch, scope
        ors = [t.strip() for t in (query or "").split(";") if t.strip()]
        # 空格与 & 均为 AND（与快速过滤语法一致），分号为 OR
        self.or_groups = [[self._mk_pred(t) for t in o.replace("&", " ").split()] or [None]
                          for o in ors] or [[None]]

    # ---- 条件解析 --------------------------------------------------------
    def _mk_pred(self, t):
        t = t.strip()
        if not t:
            return None
        low = t.lower()
        if low.startswith("/"):
            import re
            body = t[1:-1] if len(t) > 1 and t.endswith("/") else t[1:]
            try:
                rx = re.compile(body, re.I)
            except re.error:
                rx = re.compile(re.escape(body), re.I)
            return lambda n, p, st, d: rx.search(n) is not None
        if ":" in t:
            k, v = t.split(":", 1)
            k = k.lower()
            v = v.strip()
            if k == "ext":
                exts = {"." + x.lower().lstrip(".") for x in v.split(",") if x.strip()}
                return lambda n, p, st, d: (not d) and                     os.path.splitext(n)[1].lower() in exts
            if k == "size":
                return self._size_pred(v)
            if k == "date":
                return self._date_pred(v)
            if k == "type":
                if v.lower().startswith("d"):
                    return lambda n, p, st, d: d
                if v.lower().startswith("f"):
                    return lambda n, p, st, d: not d
            elif k == "tag":
                return self._tag_pred(v)
        return lambda n, p, st, d, _t=t.lower(): _t in n.lower()

    def _size_pred(self, v):
        import operator
        import re
        m = re.match(r"(>=|<=|>|<)?\s*([\d.]+)\s*([bkmg]?)", v.lower())
        if not m:
            return None
        f = {">": operator.gt, "<": operator.lt,
             ">=": operator.ge, "<=": operator.le}[m.group(1) or ">"]
        val = float(m.group(2)) * self._UNITS[m.group(3) or "b"]
        return lambda n, p, st, d: (not d) and st is not None and f(st.st_size, val)

    def _date_pred(self, v):
        import re
        m = re.match(r"(>=|<=|>|<)?\s*(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", v)
        if not m:
            return None
        try:
            ts = datetime(int(m.group(2)), int(m.group(3) or 1),
                          int(m.group(4) or 1)).timestamp()
        except ValueError:
            return None
        before = (m.group(1) or ">=")[0] == "<"
        if before:
            return lambda n, p, st, d: st is not None and st.st_mtime < ts
        return lambda n, p, st, d: st is not None and st.st_mtime >= ts

    def _tag_pred(self, v):
        vv = v.lower().lstrip("#")
        want = set()
        if len(vv) == 6:
            try:
                int(vv, 16)
                want.add("#" + vv)
            except ValueError:
                pass
        for cn, cv in self._TAG_NAMES.items():
            if cn in vv or vv in cn:
                want.add(cv)
        if not want:
            return None
        from .fsmodel import FILE_TAGS
        def pred(n, p, st, d):
            tags = FILE_TAGS.get(os.path.normcase(os.path.abspath(p)))
            return bool(tags) and any(c.lower() in want for c in tags)
        return pred

    # ---- 执行 ------------------------------------------------------------
    def run(self):
        from .fsmodel import Entry
        total = 0
        batch = []
        done = False
        for root, dirs, files in os.walk(os.path.abspath(self.scope)):
            dirs[:] = sorted(d for d in dirs if d not in self.SKIP_DIRS)
            for n in dirs + files:
                p = os.path.join(root, n)
                is_dir = n in dirs
                try:
                    st = os.stat(p)
                except OSError:
                    continue   # 跳过无法访问的项，不产生无效条目
                if any(all(pred(n, p, st, is_dir) for pred in grp if pred)
                       for grp in self.or_groups):
                    batch.append(Entry(n, p, is_dir, st.st_size, st.st_mtime, False))
                    total += 1
                    if len(batch) >= self.BATCH:
                        self.pane.smart_done.emit(self.epoch, batch, False, False)
                        batch = []
                    if total >= self.MAX_RESULTS:
                        done = True
                        break
            if done:
                break
        if batch:
            self.pane.smart_done.emit(self.epoch, batch, False, False)
        self.pane.smart_done.emit(self.epoch, [], True, total >= self.MAX_RESULTS)

class FileSystemTree(QWidget):
    """全局唯一的文件系统树面板（FC 的 File system tree panel）。

    左右两栏共用：点击节点在"活动面板"中打开；活动面板路径变化时同步高亮。
    """
    navigate = Signal(str)
    new_tab_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("fsTree")
        self.tree_model = QFileSystemModel(self)
        self.tree_model.setRootPath("")
        self.tree_model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot | QDir.Drives)
        self.tree = _FsTree()
        self.tree.setModel(self.tree_model)
        for c in range(1, 4):
            self.tree.hideColumn(c)
        self.tree.setHeaderHidden(True)
        self.tree.clicked.connect(self._clicked)
        self.tree.doubleClicked.connect(self._clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.tree)

    def _clicked(self, index):
        path = self.tree_model.filePath(index)
        if path and os.path.isdir(path):
            self.navigate.emit(path)

    def sync(self, path):
        """面板切换目录后同步选中节点。"""
        if path:
            self.tree.setCurrentIndex(self.tree_model.index(path))

    def _menu(self, pos):
        idx = self.tree.indexAt(pos)
        path = self.tree_model.filePath(idx) if idx.isValid() else ""
        m = QMenu(self)
        a_open = m.addAction("在活动面板打开")
        a_new = m.addAction("在新标签打开")
        m.addSeparator()
        a_copy = m.addAction("复制路径")
        act = m.exec(self.tree.viewport().mapToGlobal(pos))
        if act is a_open and path and os.path.isdir(path):
            self.navigate.emit(path)
        elif act is a_new and path and os.path.isdir(path):
            self.new_tab_requested.emit(path)
        elif act is a_copy and path:
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.clipboard().setText(path)


class _FsTree(QTreeView):
    """文件系统树：支持 +/-/*（含小键盘）展开、折叠、递归展开。"""

    def keyPressEvent(self, ev):
        k = ev.key()
        if k in (Qt.Key_Plus, Qt.Key_Minus, Qt.Key_Asterisk):
            idx = self.currentIndex()
            if idx.isValid():
                if k == Qt.Key_Plus:
                    self.expand(idx)
                elif k == Qt.Key_Minus:
                    self.collapse(idx)
                else:
                    self.expandRecursively(idx)
                return
        super().keyPressEvent(ev)
