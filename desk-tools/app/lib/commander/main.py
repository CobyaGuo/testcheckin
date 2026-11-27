# -*- coding: utf-8 -*-
"""commander 主窗口：双栏文件管理器（FreeCommander 风格）。

- 双栏 + 每栏多标签页；
- F2 重命名 / F3 查看 / F4 编辑 / F5 复制 / F6 移动 / F7 新建 / F8 删除 / F9 搜索；
- Delete 删除到回收站 / Shift+Delete 永久删除（二次强确认，不可恢复）；
- Ctrl+C / X / V 剪贴板复制、剪切、粘贴；Ctrl+H 切换单双栏；Ctrl+. 切换隐藏文件；
- 底部命令行：输入 cd、常用命令（以 ! 前缀直接执行任意命令）。
纯本地：无网络、无鉴权、无远程能力。
"""
import os
import shutil
import subprocess
import sys
import threading

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QPushButton, QSplitter, QStatusBar, QTabBar,
                               QTabWidget, QToolBar, QVBoxLayout, QWidget)
from PySide6.QtGui import QColor, QIcon, QPen
from . import icons

from . import config
from .dialogs import BatchRenameDialog, Md5Dialog
from .operations import OpQueue, OpRunner
from .pane import (VIEW_DETAIL, VIEW_ICONS, FilePane, FileSystemTree)
from .queuebar import FavoritesTree, QueueBar
from .theme import apply_theme
from .viewer import QuickViewerPanel, SearchDialog, ViewerDialog

_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


TAB_COLORS = [("", "默认"), ("#3b82f6", "蓝"), ("#34d399", "绿"), ("#f59e0b", "橙"),
              ("#f87171", "红"), ("#a78bfa", "紫"), ("#22d3ee", "青")]


def _color_icon(color):
    from PySide6.QtGui import QPixmap
    pm = QPixmap(12, 12)
    pm.fill(QColor(color))
    return QIcon(pm)


class ColoredTabBar(QTabBar):
    """支持按标签设置背景色的标签条。

    颜色以【面板对象】为键（拖动换位后仍正确）；
    文字颜色按背景亮度自动选黑/白，保证可读。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bg = {}          # pane -> QColor

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if not self.bg:
            return
        from PySide6.QtGui import QPainter, QPainterPath
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        fm = self.fontMetrics()
        for i in range(self.count()):
            color = self.bg.get(self.parent().widget(i))
            if color is None:
                continue
            r = self.tabRect(i).adjusted(0, 1, -1, 0)
            path = QPainterPath()
            path.addRoundedRect(r, 7, 7)   # 圆角胶囊，与 QSS 样式一致
            p.fillPath(path, color)
            if i == self.currentIndex():
                p.setPen(QPen(QColor("#7cb0ff"), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 6, 6)
            fg = QColor("#1f2430") if (0.299 * color.red() + 0.587 * color.green()
                                       + 0.114 * color.blue()) > 150 else QColor("#ffffff")
            p.setPen(fg)
            text = fm.elidedText(self.tabText(i), Qt.ElideRight,
                                 max(r.width() - 18, 24))
            p.drawText(r.adjusted(9, 0, -12, 0), Qt.AlignVCenter | Qt.AlignLeft, text)


class PaneTabs(QTabWidget):
    """每侧独立的标签页容器：可新建 / 重命名 / 着色，全部持久化到 cfg。"""

    def __init__(self, main, parent=None, strip_side="left"):
        super().__init__(parent)
        self.main = main
        self._strip_side = strip_side
        self.setTabBar(ColoredTabBar(self))
        self.tabBar().installEventFilter(self)
        self.setDocumentMode(True)
        self.setTabsClosable(True)
        self.setMovable(True)
        self.tabCloseRequested.connect(self._close_tab)
        plus = QPushButton("＋")
        plus.setFlat(True)
        plus.setToolTip("新建标签 (Ctrl+T)")
        plus.clicked.connect(self.new_tab)
        self.setCornerWidget(plus, Qt.TopRightCorner)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._tab_menu)
        self.tabBar().setElideMode(Qt.ElideRight)
        self.tabBar().setUsesScrollButtons(True)
        self.currentChanged.connect(self._on_tab_switched)

    def eventFilter(self, obj, ev):
        """双击标签栏空白处新建标签。"""
        from PySide6.QtCore import QEvent
        if obj is self.tabBar() and ev.type() == QEvent.MouseButtonDblClick \
                and self.tabBar().tabAt(ev.pos()) < 0:
            self.new_tab()
            return True
        return super().eventFilter(obj, ev)

    def other_tabs(self):
        return self.main.panes[1] if self.main.panes[0] is self else self.main.panes[0]

    def transfer_tab(self, idx, copy=False):
        """把标签移动/复制到对侧（FC Tab area 功能）。"""
        other = self.other_tabs()
        w = self.widget(idx)
        path = w.current_path()
        title = self.tabText(idx)
        color = getattr(w, "tab_color", "")
        base = os.path.basename(path) or path
        custom = "" if title == base else title
        if not copy:
            if self.count() <= 1:
                return
            self.removeTab(idx)
            w.deleteLater()
        other.add_pane(path, custom, color)
        self.main.save_tabs()

    def _tab_menu(self, pos):
        idx = self.tabBar().tabAt(pos)
        if idx < 0:
            pane = self.currentWidget()
            if pane:
                pane._context_menu(pos)
            return
        menu = QMenu(self)
        a_new = menu.addAction("新建标签 (Ctrl+T)")
        a_ren = menu.addAction("重命名标签…")
        m_col = menu.addMenu("标签颜色")
        for color, cname in TAB_COLORS:
            act = m_col.addAction("● " + cname)
            if color:
                act.setIcon(_color_icon(color))
            act.triggered.connect(lambda _=False, c=color, i=idx: self.set_tab_color(i, c))
        a_close = menu.addAction("关闭标签 (Ctrl+W)")
        m_move = menu.addMenu("移动 / 复制到对侧")
        a_mv = m_move.addAction("移动标签到对侧")
        a_cp = m_move.addAction("复制标签到对侧")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is a_new:
            self.new_tab()
        elif chosen is a_ren:
            self.rename_tab(idx)
        elif chosen is a_close:
            self._close_tab(idx)
        elif chosen is a_mv:
            self.transfer_tab(idx, copy=False)
        elif chosen is a_cp:
            self.transfer_tab(idx, copy=True)

    def rename_tab(self, idx):
        pane = self.widget(idx)
        new, ok = QInputDialog.getText(self, "重命名标签", "标签名（留空恢复为目录名）：",
                                       text=self.tabText(idx))
        if not ok:
            return
        pane.custom_title = new.strip()
        self.setTabText(idx, new.strip() or os.path.basename(pane.current_path())
                        or pane.current_path())
        self.main.save_tabs()

    def set_tab_color(self, idx, color):
        self.widget(idx).tab_color = color
        bar = self.tabBar()
        if color:
            bar.bg[self.widget(idx)] = QColor(color)
        else:
            bar.bg.pop(self.widget(idx), None)
        bar.update()
        self.main.save_tabs()

    def add_pane(self, path, title="", color=""):
        pane = FilePane(strip_side=self._strip_side)
        pane.main = self.main
        pane.custom_title = title
        pane.tab_color = color
        pane.activate.connect(lambda e, tp=pane: self.main.on_activate(e, tp))
        pane.status_changed.connect(self.main.update_status)
        pane.quick_searched.connect(lambda t: self.main.quick_search_display(t))
        pane.path_changed.connect(lambda p, tp=pane: self._retitle(pane, p))
        pane.path_changed.connect(lambda p, tp=pane: self.main._pane_path_changed(tp, p))
        pane.focus_requested.connect(lambda tp=pane: self.main._pane_focused(tp))
        pane.filter_both.connect(lambda t, tp=pane: self.main.apply_filter_both(t))
        idx = self.addTab(pane, title or os.path.basename(path) or path)
        self.setTabToolTip(idx, path)
        if color:
            self.tabBar().bg[pane] = QColor(color)
        self.setCurrentIndex(idx)
        pane.navigate(path)
        return pane

    def new_tab(self):
        self.add_pane(os.path.expanduser("~"))
        self.main.save_tabs()

    def _retitle(self, pane, path):
        if getattr(pane, "custom_title", ""):
            return
        self.setTabText(self.indexOf(pane), os.path.basename(path) or path or "…")
        self.setTabToolTip(self.indexOf(pane), path)

    def _close_tab(self, idx):
        if self.count() <= 1:
            return
        w = self.widget(idx)
        self.removeTab(idx)
        w.deleteLater()
        self.main.save_tabs()

    def current_pane(self):
        return self.currentWidget()

    def _on_tab_switched(self, idx):
        pane = self.widget(idx)
        if pane and self.main:
            self.main._pane_focused(pane)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Commander — 文件管理")
        self.cfg = config.load()
        self.clip = []        # 剪贴板路径
        self.clip_mode = None # "copy" | "cut"
        self.op_queue = OpQueue()
        self.op = OpRunner(self, self.op_queue)
        self.panes = []
        self._active_idx = self.cfg.get("active_pane", 0)

        # ---- 图标（打包后同样以相对路径定位，便携可用）----
        icon_path = os.path.join(_ASSETS, "commander.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # ---- 收藏树面板（左，Alt+F 开关）----
        # 文件标签：启动时注入模型
        from .fsmodel import set_file_tags
        set_file_tags(self.cfg.get("file_tags", {}))

        self.fav_tree = FavoritesTree(self.cfg)
        self.fav_tree.navigate.connect(self.navigate_favorite)
        self.fav_tree.smart_requested.connect(self.smart_open)
        self.fav_tree.changed.connect(self._rebuild_fav_bar)
        self.fav_tree.changed.connect(self._sync_fav_menu)

        # ---- 文件系统树：全局唯一，左右两栏共用，跟活动面板走 ----
        self.fs_tree = FileSystemTree()
        self.fs_tree.navigate.connect(self._tree_nav)
        self.fs_tree.new_tab_requested.connect(self._tree_new_tab)
        self.fs_tree.setMaximumWidth(300)   # 显隐统一延迟到布局装配完成后

        # ---- 中央：双栏标签页 ----
        self.pane_l = PaneTabs(self)
        self.pane_r = PaneTabs(self, strip_side="right")
        self.panes = [self.pane_l, self.pane_r]
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self.pane_l)
        self.split.addWidget(self._build_mid_ops())
        self.split.addWidget(self.pane_r)
        self.split.setSizes([580, 44, 580])
        self.split.widget(1).setFixedWidth(44)
        self.split.setCollapsible(1, False)

        # ---- 快速查看面板（Ctrl+Q 开关，跟随活动面板选中项）----
        self.quick_viewer = QuickViewerPanel()
        self.quick_viewer.hide()
        self.quick_viewer.setMinimumHeight(180)

        # ---- 操作队列栏（底部，有后台任务时出现）----
        self.queue_bar = QueueBar(self.op_queue)

        # ---- 收藏栏 ----
        self.fav_bar = QHBoxLayout()
        self.fav_bar.setContentsMargins(6, 4, 6, 0)
        self.fav_bar.setSpacing(3)
        self._rebuild_fav_bar()

        # ---- F 键功能栏 + 命令行 ----（按钮保留，完整功能走主菜单）
        fbar = QHBoxLayout()
        fbar.setContentsMargins(6, 4, 6, 4)
        fbar.setSpacing(4)
        for key, text, ic, fn in (("F2", "重命名", "rename", self.rename_selected),
                                  ("F3", "查看", "view", self.view_selected),
                                  ("F4", "编辑", "edit", self.edit_selected),
                                  ("F5", "复制", "copy", self.copy_selected),
                                  ("F6", "移动", "move", self.move_selected),
                                  ("F7", "新建", "folder_new", self.new_folder),
                                  ("F8", "删除", "trash", self.delete_selected),
                                  ("F9", "搜索", "search", self.search_here),
                                  ("Ctrl+H", "单/双栏", "columns", self.toggle_layout)):
            b = QPushButton(icons.icon(ic), text)
            b.setToolTip(key)
            b.setFlat(True)
            b.setIconSize(QSize(16, 16))
            b.clicked.connect(fn)
            fbar.addWidget(b)
        fbar.addStretch(1)
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("命令行：cd … / !任意命令（如 !git status）")
        self.cmd.setMaximumWidth(520)
        self.cmd.returnPressed.connect(self._run_cmd)
        self.cmd.installEventFilter(self)
        self._cmd_history = []
        self._cmd_hpos = -1
        fbar.addWidget(self.cmd)

        self.func_bar = QWidget()
        self.func_bar.setLayout(fbar)
        bottom = QVBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(0)
        bottom.addWidget(self.func_bar)

        self.center_split = QSplitter(Qt.Horizontal)
        self.fav_tree.setMaximumWidth(240)
        # 防止 QSplitter 在面板最小宽度约束下把收藏树压成 40px 窄条
        self.fav_tree.setMinimumWidth(180)
        self.center_split.addWidget(self.fav_tree)
        self.center_split.addWidget(self.fs_tree)
        self.center_split.addWidget(self.split)
        self.center_split.setStretchFactor(1, 1)

        center = QWidget()
        cv = QVBoxLayout(center)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addLayout(self.fav_bar)
        cv.addWidget(self.center_split, 1)
        cv.addWidget(self.quick_viewer)
        cv.addWidget(self.queue_bar)
        cv.addLayout(bottom)
        self.setCentralWidget(center)

        self._build_actions_toolbar()
        # 延迟到布局装配完成后再按配置显隐子面板：
        # 无 parent 的 setVisible(True) 会把面板变成独立顶层窗口（即启动时闪出的多个空白窗）
        self.fav_tree.setVisible(self.cfg.get("fav_tree", False))
        self.fs_tree.setVisible(self.cfg.get("fs_tree", False))
        self.quick_viewer.setVisible(False)
        QApplication.instance().focusChanged.connect(self._on_focus_changed)

        # 应用级点击过滤：点在哪个面板内，该面板立即成为活动面板
        # （焦点事件在某些场景不触发，用点击兜底，保证"选中哪个操作哪个"）
        from PySide6.QtCore import QObject, QEvent

        class _ClickFocus(QObject):
            def eventFilter(selff, obj, ev):
                if ev.type() == QEvent.MouseButtonPress:
                    w = obj
                    while w is not None and not isinstance(w, FilePane):
                        w = w.parentWidget()
                    if w is not None:
                        self._pane_focused(w)
                return False
        self._click_focus = _ClickFocus(self)
        QApplication.instance().installEventFilter(self._click_focus)

        # ---- 状态栏（只在初始化时创建一次）----
        self.st_label = QLabel("就绪")
        self.st_count = QLabel("")
        sb = self.statusBar()
        sb.addWidget(self.st_label, 1)
        sb.addPermanentWidget(self.st_count)

        self._build_menus()
        self._shortcuts()
        self._restore()
        self._show_config_warnings()

    def _show_config_warnings(self):
        """把配置读写/容错告警集中到状态栏，完整内容放 tooltip。"""
        warns = config.get_warnings()
        if not warns:
            return
        msg = "配置告警（%d）：%s" % (len(warns), "；".join(warns))
        self.st_label.setText(msg)
        self.st_label.setToolTip(msg)
        self.st_label.setStyleSheet("color: #e6a23c;")

    def _on_focus_changed(self, _old, now):
        """焦点落在哪个面板内，树/状态栏就跟随哪个面板。"""
        if now is None:
            return
        w = now
        while w is not None and not isinstance(w, FilePane):
            w = w.parentWidget()
        if w is not None:
            self._pane_focused(w)

    # ---- 主菜单（FreeCommander 界面总览里的 main menu）--------------------
    def _build_menus(self):
        mb = self.menuBar()

        m_file = mb.addMenu("文件")
        m_file.addAction("新建文件夹\tF7", self.new_folder)
        m_file.addSeparator()
        m_file.addAction("新建智能文件夹…", self.add_smart_folder)
        m_file.addAction("压缩为 ZIP…", self.zip_selected)
        m_file.addAction("解压到此处", self.unpack_here)
        m_file.addSeparator()
        m_file.addAction("在此处打开终端", lambda: self.active_pane()._open_shell_here())
        m_file.addAction("退出\tAlt+F4", self.close)

        m_edit = mb.addMenu("编辑")
        m_edit.addAction("复制\tCtrl+C", self.clipboard_copy)
        m_edit.addAction("剪切\tCtrl+X", self.clipboard_cut)
        m_edit.addAction("粘贴\tCtrl+V", self.paste_active)
        m_edit.addSeparator()
        m_edit.addAction("重命名\tF2", self.rename_selected)
        m_edit.addAction("批量重命名…", self.batch_rename)
        m_edit.addAction("删除到回收站\tF8", self.delete_selected)
        m_edit.addSeparator()
        m_edit.addAction("全选\tCtrl+A", self.select_all)
        m_edit.addAction("反选\tCtrl+I", self.invert_selection)

        m_view = mb.addMenu("视图")
        g = m_view.addMenu("文件视图")
        g.addAction("详细列表", lambda: self.set_view_mode(VIEW_DETAIL))
        g.addAction("大图标", lambda: self.set_view_mode(VIEW_ICONS))
        m_view.addAction("目录树开关", self.toggle_tree)
        m_view.addAction("收藏树 (Alt+F)", self.toggle_fav_tree)
        m_view.addAction("快速查看 (Ctrl+Q)", self.toggle_quick_viewer)
        m_view.addAction("功能栏", self.toggle_functions_bar)
        m_view.addAction("命令行", self.toggle_command_line)
        m_view.addSeparator()
        m_theme = m_view.addMenu("主题")
        m_theme.addAction("深色", lambda: self.set_theme("dark"))
        m_theme.addAction("浅色（米灰）", lambda: self.set_theme("light"))
        m_view.addSeparator()
        m_view.addAction("单栏 / 双栏\tF10", self.toggle_layout)
        m_view.addSeparator()
        m_view.addAction("对侧打开同一目录\tCtrl+E", self.same_folder)
        m_view.addAction("交换两侧目录\tCtrl+U", self.swap_panes)
        m_view.addAction("水平 / 垂直排列", self.toggle_orientation)
        m_view.addAction("显示隐藏文件\tCtrl+.", self.toggle_hidden)

        m_fav = mb.addMenu("收藏")
        m_fav.addAction("将当前目录加入收藏…", self.add_favorite)
        self.m_fav_list = m_fav

        m_tools = mb.addMenu("工具")
        m_tools.addAction("搜索\tF9", self.search_here)
        m_tools.addAction("计算 MD5", self.calc_md5)
        m_tools.addSeparator()
        m_tools.addAction("刷新\tCtrl+R", self.refresh_all)

    def _sync_fav_menu(self):
        if not hasattr(self, "m_fav_list"):
            return
        self.m_fav_list.clear()
        self.m_fav_list.addAction("将当前目录加入收藏…", self.add_favorite)
        self.m_fav_list.addSeparator()
        for fav in self.cfg.get("favorites", []):
            if not isinstance(fav, dict) or not fav.get("name") or not fav.get("path"):
                continue
            self.m_fav_list.addAction("★ " + fav["name"],
                                      lambda p=fav["path"], n=fav["name"]: self.navigate_favorite(p, n))

    # ---- 初始化辅助 ------------------------------------------------------
    def _rebuild_fav_bar(self):
        while self.fav_bar.count():
            it = self.fav_bar.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        from .fsmodel import fav_icon
        from .queuebar import _emoji_icon  # noqa: F401 （保留兼容）
        for fav in self.cfg.get("favorites", []):
            if not isinstance(fav, dict) or not fav.get("name") or not fav.get("path"):
                continue
            b = QPushButton(" " + fav["name"])
            b.setFlat(True)
            b.setIcon(fav_icon(fav["name"]))
            b.setToolTip(fav["path"])
            b.clicked.connect(lambda _=False, p=fav["path"], n=fav["name"]: self.navigate_favorite(p, n))
            self.fav_bar.addWidget(b)
        add = QPushButton("＋ 添加收藏")
        add.setFlat(True)
        add.clicked.connect(self.add_favorite)
        self.fav_bar.addWidget(add)
        self.fav_bar.addStretch(1)

    def _shortcuts(self):
        sc = [
            ("F2", self.rename_selected), ("F3", self.view_selected),
            ("F4", self.edit_selected), ("F5", self.copy_selected),
            ("F6", self.move_selected), ("F7", self.new_folder),
            ("F8", self.delete_selected), ("F9", self.search_here),
            # Windows 通用约定：Delete 到回收站，Shift+Delete 永久删除。
            ("Delete", self.delete_selected),
            ("Shift+Delete", self.purge_selected),
            ("Ctrl+C", self.clipboard_copy), ("Ctrl+X", self.clipboard_cut),
            ("Ctrl+V", self.paste_active), ("Ctrl+R", self.refresh_all),
            ("Ctrl+.", self.toggle_hidden), ("Ctrl+T", self.new_tab),
            ("Ctrl+W", self.close_tab), ("Ctrl+H", self.toggle_layout),
            ("Alt+1", lambda: self._focus_pane(0)), ("Alt+2", lambda: self._focus_pane(1)),
            ("Ctrl+Q", self.toggle_quick_viewer), ("Alt+F", self.toggle_fav_tree),
            ("Shift+Tab", self._cycle_focus), ("Ctrl+Y", self._focus_filter),
            ("F10", self.toggle_layout), ("Ctrl+E", self.same_folder),
            ("Ctrl+U", self.swap_panes),
            ("Ctrl+Tab", lambda: self._cycle_tab(1)),
            ("Ctrl+Shift+Tab", lambda: self._cycle_tab(-1)),
        ]
        for key, fn in sc:
            s = QShortcut(QKeySequence(key), self)
            s.activated.connect(fn)

    def _restore(self):
        panes_cfg = self.cfg.get("panes", config.load()["panes"])
        for i, pc in enumerate(panes_cfg[:2]):
            for t in pc.get("tabs", [{}]):
                self.panes[i].add_pane(t.get("path", os.path.expanduser("~")))
        single = self.cfg.get("single_pane", False)
        self.pane_r.setVisible(not single)
        if self.cfg.get("view_mode", VIEW_DETAIL) == VIEW_ICONS:
            self.set_view_mode(VIEW_ICONS)
        apply_theme(QApplication.instance(), self.cfg.get("theme", "dark"))
        # 恢复标签自定义名与颜色
        panes_cfg = self.cfg.get("panes", [])
        for pi, pc in enumerate(panes_cfg[:2]):
            pt = self.panes[pi]
            for i, t in enumerate(pc.get("tabs", [])):
                if i < pt.count():
                    wd = pt.widget(i)
                    wd.custom_title = t.get("title", "")
                    wd.tab_color = t.get("color", "")
                    if wd.custom_title:
                        pt.setTabText(i, wd.custom_title)
                    if wd.tab_color:
                        pt.tabBar().bg[wd] = QColor(wd.tab_color)
        self.fs_tree.setVisible(self.cfg.get("fs_tree", False))
        self._sync_fav_menu()
        w = self.cfg.get("window", {})
        self.resize(w.get("w", 1280), w.get("h", 800))
        if w.get("maximized"):
            self.showMaximized()
        # 恢复分割条比例（左右栏 / 收藏树-树-面板）
        cs = self.cfg.get("split_sizes")
        if isinstance(cs, list) and len(cs) == 3 and all(isinstance(x, int) and x > 0 for x in cs):
            try:
                self.center_split.setSizes(cs[:3])
            except Exception:
                pass
        else:
            # 首次运行无历史尺寸：记录合理默认值。收藏树默认隐藏，QSplitter
            # 会在显示时把隐藏 widget 压到极小（约 40px），需在 toggle_fav_tree
            # 显示时重新应用，见 _ensure_fav_tree_width。
            total = max(self.center_split.width(), 900)
            self._default_center_sizes = [180, 220, total - 400]
        ps = self.cfg.get("pane_sizes")
        if ps and len(ps) == 3:
            try:
                self.split.setSizes(ps)
            except Exception:
                pass

    def closeEvent(self, ev):
        self.save_tabs()
        self.cfg["single_pane"] = not self.pane_r.isVisible()
        self.cfg["window"] = {"w": self.width(), "h": self.height(),
                              "maximized": self.isMaximized()}
        self.cfg["split_sizes"] = list(self.center_split.sizes())
        self.cfg["pane_sizes"] = list(self.split.sizes())
        config.save(self.cfg)
        super().closeEvent(ev)

    # ---- 面板定位 --------------------------------------------------------
    def _pane_tabs(self, pane):
        """页面控件的 parent 是 QTabWidget 内部的堆栈，需向上遍历找所属标签容器。"""
        w = pane.parentWidget() if pane else None
        while w is not None and not isinstance(w, PaneTabs):
            w = w.parentWidget()
        return w

    def active_pane(self):
        """活动面板：跟踪最后一次点选的面板（按钮/菜单操作也指向它）。"""
        pt = self.panes[self._active_idx]
        if not pt.isVisible():
            pt = self.panes[0]
        return pt.current_pane()

    def inactive_pane(self):
        act = self.active_pane()
        for pt in self.panes:
            if pt.isVisible():
                pane = pt.current_pane()
                if pane is not act:
                    return pane
        return None

    def _focus_pane(self, i):
        pt = self.panes[i]
        if pt.isVisible():
            pt.current_pane().table.setFocus()

    def _cycle_focus(self):
        """Shift+Tab：文件列表 → 目录树 → 命令行循环切焦点。"""
        pane = self.active_pane()
        if self.fs_tree.isVisible() and self.fs_tree.tree.hasFocus():
            self.cmd.setFocus()
        elif pane._cur_view().hasFocus() and self.fs_tree.isVisible():
            self.fs_tree.tree.setFocus()
        else:
            pane._cur_view().setFocus()

    def _focus_filter(self):
        self.active_pane().filter_edit.setFocus()

    def _cycle_tab(self, delta):
        act = self.active_pane()
        pt = self._pane_tabs(act)
        if pt and pt.count() > 1:
            pt.setCurrentIndex((pt.currentIndex() + delta) % pt.count())

    def eventFilter(self, obj, ev):
        """命令行历史：↑/↓ 翻历史。"""
        from PySide6.QtCore import QEvent
        if obj is self.cmd and ev.type() == QEvent.KeyPress:
            if ev.key() == Qt.Key_Up and self._cmd_history:
                self._cmd_hpos = max(0, (self._cmd_hpos if self._cmd_hpos >= 0
                                         else len(self._cmd_history)) - 1)
                self.cmd.setText(self._cmd_history[self._cmd_hpos])
                return True
            if ev.key() == Qt.Key_Down and self._cmd_history:
                self._cmd_hpos = min(len(self._cmd_history), self._cmd_hpos + 1)
                self.cmd.setText(self._cmd_history[self._cmd_hpos]
                                 if self._cmd_hpos < len(self._cmd_history) else "")
                return True
        return super().eventFilter(obj, ev)

    # ---- 操作入口（F 键 / 菜单 / 右键共用）---------------------------------
    def on_activate(self, entry, pane=None):
        """打开条目：优先在【来源面板】导航（pane 由信号携带），
        避免双击瞬间焦点未切换时打开到对侧。"""
        pane = pane or self.active_pane()
        if entry.is_dir:
            pane.navigate(entry.path)
        else:
            self._shell_open(entry.path)

    def _shell_open(self, path):
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            QMessageBox.warning(self, "打开失败", str(exc))

    def copy_selected(self):
        pane = self.active_pane()
        dst = self.inactive_pane()
        sel = pane.selected_entries()
        if not sel or not dst or not dst.current_path():
            return
        srcs = [e.path for e in sel]
        task = self.op.run_copy(srcs, dst.current_path(), mode="copy")
        if task is not None:
            task.sig.finished.connect(lambda *_: (pane.refresh(), dst.refresh()))

    def move_selected(self):
        pane = self.active_pane()
        dst = self.inactive_pane()
        sel = pane.selected_entries()
        if not sel or not dst or not dst.current_path():
            return
        srcs = [e.path for e in sel]
        task = self.op.run_copy(srcs, dst.current_path(), mode="move")
        if task is not None:
            task.sig.finished.connect(lambda *_: (pane.refresh(), dst.refresh()))

    def _delete_focus_ok(self):
        """Delete/Shift+Delete 是窗口级快捷键：焦点在文本输入框时应让
        Delete 正常删字符，而不是删文件。仅在文件视图有焦点时执行文件删除。"""
        try:
            w = QApplication.focusWidget()
        except Exception:
            return True
        if w is None:
            return True
        # 文本框/命令行/过滤框/路径框里按 Delete 属于文本编辑
        if isinstance(w, QLineEdit):
            return False
        parent = w.parent() if hasattr(w, "parent") else None
        if parent is not None and isinstance(parent, QLineEdit):
            return False
        return True

    def delete_selected(self):
        if not self._delete_focus_ok():
            return
        pane = self.active_pane()
        sel = pane.selected_entries()
        if not sel:
            return
        names = "、".join(e.name for e in sel[:5])
        more = "" if len(sel) <= 5 else " 等 %d 项" % len(sel)
        box = QMessageBox(self)
        box.setWindowTitle("删除确认")
        box.setText("把 %s%s 移入回收站？\n（Shift+Delete 可永久删除，不进回收站）" % (names, more))
        yes = box.addButton("移入回收站", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not yes:
            return
        task = self.op.run_delete([e.path for e in sel])
        task.sig.finished.connect(lambda *_: pane.refresh())

    def purge_selected(self):
        """Shift+Delete：永久删除（不可恢复）。必须二次强确认。"""
        if not self._delete_focus_ok():
            return
        pane = self.active_pane()
        sel = pane.selected_entries()
        if not sel:
            return
        names = "、".join(e.name for e in sel[:5])
        more = "" if len(sel) <= 5 else " 等 %d 项" % len(sel)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("永久删除（不可恢复）")
        box.setText("永久删除 %s%s？" % (names, more))
        box.setInformativeText("此操作不进回收站，无法撤销！确定要继续吗？")
        yes = box.addButton("永久删除", QMessageBox.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not yes:
            return
        task = self.op.run_purge([e.path for e in sel])
        task.sig.finished.connect(lambda *_: pane.refresh())

    def rename_selected(self):
        pane = self.active_pane()
        e = pane.current_entry()
        if not e:
            return
        new, ok = QInputDialog.getText(self, "重命名", "新名称：", text=e.name)
        if not ok or not new.strip() or new == e.name:
            return
        try:
            os.rename(e.path, os.path.join(os.path.dirname(e.path), new.strip()))
        except OSError as exc:
            QMessageBox.warning(self, "重命名失败", str(exc))
        pane.refresh()

    def new_folder(self):
        pane = self.active_pane()
        base = pane.current_path()
        if not base:
            return
        name, ok = QInputDialog.getText(self, "新建文件夹", "名称：", text="新建文件夹")
        if not ok or not name.strip():
            return
        try:
            os.mkdir(os.path.join(base, name.strip()))
        except OSError as exc:
            QMessageBox.warning(self, "新建失败", str(exc))
        pane.refresh()

    def view_selected(self):
        e = self.active_pane().current_entry()
        if e and not e.is_dir:
            ViewerDialog(e.path, self).show()

    def edit_selected(self):
        e = self.active_pane().current_entry()
        if e and not e.is_dir:
            self._shell_open(e.path)   # 用系统默认编辑器（记事本/VSCode 等关联）

    def search_here(self):
        pane = self.active_pane()
        root = pane.current_path()
        if not root:
            return
        dlg = SearchDialog(root, pane.model.show_hidden, self)
        if dlg.exec() and dlg.result_path:
            self._reveal(dlg.result_path)

    def _reveal(self, path):
        """在活动面板中定位到搜索结果：文件则进入其目录并选中。"""
        pane = self.active_pane()
        if os.path.isdir(path):
            pane.navigate(path)
            return
        pane.navigate(os.path.dirname(path))

        def select():
            for r, e in enumerate(pane.model.entries):
                if e.path == path:
                    pane.table.selectRow(pane.proxy.mapFromSource(
                        pane.model.index(r, 0)).row())
                    break
        QTimer.singleShot(300, select)

    # ---- 剪贴板（程序内，非系统剪贴板）--------------------------------------
    def clipboard_copy(self):
        sel = self.active_pane().selected_entries()
        if sel:
            self.clip = [e.path for e in sel]
            self.clip_mode = "copy"
            self.st_label.setText("已复制 %d 项（Ctrl+V 粘贴到对侧）" % len(self.clip))

    def clipboard_cut(self):
        sel = self.active_pane().selected_entries()
        if sel:
            self.clip = [e.path for e in sel]
            self.clip_mode = "cut"
            self.st_label.setText("已剪切 %d 项（Ctrl+V 粘贴到对侧）" % len(self.clip))

    def paste_active(self):
        self.paste_into_pane(self.active_pane())

    def paste_into_pane(self, pane=None):
        pane = pane or self.active_pane()
        if not self.clip or not pane.current_path():
            return
        task = self.op.run_copy(list(self.clip), pane.current_path(),
                                mode="move" if self.clip_mode == "cut" else "copy")
        if self.clip_mode == "cut":
            self.clip = []
        if task is not None:
            task.sig.finished.connect(lambda *_: pane.refresh())

    # ---- 布局 / 视图 ------------------------------------------------------
    def toggle_layout(self):
        vis = not self.pane_r.isVisible()
        self.pane_r.setVisible(vis)
        self.cfg["single_pane"] = not vis

    def toggle_hidden(self):
        on = not self.cfg.get("show_hidden", False)
        self.cfg["show_hidden"] = on
        for pt in self.panes:
            for i in range(pt.count()):
                pt.widget(i).model.set_show_hidden(on)
        self.st_label.setText("已%s显示隐藏文件" % ("" if on else "取消"))

    def refresh_all(self):
        for pt in self.panes:
            for i in range(pt.count()):
                pt.widget(i).refresh()

    def new_tab(self):
        act = self.active_pane()
        pt = self._pane_tabs(act)
        if pt:
            pt.new_tab()

    def close_tab(self):
        for pt in self.panes:
            if pt.hasFocus():
                pt._close_tab(pt.currentIndex())

    def add_favorite(self):
        pane = self.active_pane()
        path = pane.current_path()
        if not path:
            return
        name, ok = QInputDialog.getText(self, "添加收藏", "名称：", text=os.path.basename(path))
        if not ok or not name.strip():
            return
        self.cfg.setdefault("favorites", []).append({"name": name.strip(), "path": path})
        config.save(self.cfg)
        # 新增收藏必须同步刷新收藏树：fav_tree 内部维护自己的 QTreeWidgetItem，
        # 只重建收藏栏/菜单时树看不到新项（新增只在收藏栏出现，树里始终没有）。
        self.fav_tree.reload()
        self._rebuild_fav_bar()
        self._sync_fav_menu()

    # ---- 收藏导航统一入口 ---------------------------------------------------
    def navigate_favorite(self, path, name=None):
        """收藏栏/收藏树点击的统一导航：先激活目标面板（描边高亮），再切目录。

        原来直接 active_pane().navigate(path)：数据确实切了，但活动面板的
        「激活边框」只在点面板内部时才更新，从收藏点击进去时边框还停在旧面板，
        用户看着没边框的那一栏，以为"点了没反应"。
        """
        raw = str(path or "").strip()
        if not raw:
            return
        if not name:
            # 收藏树只发路径信号：按路径反查名称，供下面判定特殊目录兜底
            for fav in self.cfg.get("favorites", []):
                if isinstance(fav, dict) and fav.get("path") == raw:
                    name = fav.get("name")
                    break
        path = os.path.normpath(os.path.expandvars(os.path.expanduser(raw)))
        # 用户换机器/换账户后配置里的 home 路径可能失效：已知的特殊目录
        # 按收藏项名称用当前系统的真实位置兜底，避免"收藏点了完全没反应"。
        if not os.path.isdir(path) and name:
            for label, env in (("桌面", "Desktop"), ("下载", "Downloads"), ("文档", "Documents"),
                               ("图片", "Pictures"), ("音乐", "Music"), ("视频", "Videos")):
                if label in str(name):
                    alt = os.path.normpath(os.path.expanduser("~/" + env))
                    if os.path.isdir(alt):
                        path = alt
                        break
        pane = self.active_pane()
        if not os.path.isdir(path):
            self.st_label.setText("收藏路径不存在：%s" % raw)
            self.st_label.setToolTip(raw)
            return
        self._pane_focused(pane)     # 描边高亮 + 共享树同步到目标面板
        pane.navigate(path)

    # ---- 动作工具栏（Actions toolbar）与面板开关 ----------------------------
    def _build_actions_toolbar(self):
        tb = QToolBar("动作")
        tb.setObjectName("actionsBar")
        tb.setMovable(False)
        for text, tip, fn in (
                ("📁", "查看/打开选中项", self.on_activate_current),
                ("📁+", "新建文件夹 (F7)", self.new_folder),
                ("🗑", "删除到回收站 (F8)", self.delete_selected),
                ("🔍", "搜索 (F9)", self.search_here),
                ("⇧", "视图切换", lambda: self.set_view_mode(
                    VIEW_ICONS if self.cfg.get("view_mode", VIEW_DETAIL) == VIEW_DETAIL
                    else VIEW_DETAIL)),
                ("⟳", "刷新 (Ctrl+R)", self.refresh_all)):
            b = QPushButton(text)
            b.setFlat(True)
            b.setToolTip(tip)
            b.setFixedWidth(30)
            b.clicked.connect(fn)
            tb.addWidget(b)
        self.addToolBar(tb)

    def on_activate_current(self):
        e = self.active_pane().current_entry()
        if e:
            self.on_activate(e)

    def set_theme(self, name):
        self.cfg["theme"] = name
        config.save(self.cfg)
        apply_theme(QApplication.instance(), name)

    def _build_mid_ops(self):
        """双栏中间的操作栏（FreeCommander 需求：两栏间直接操作）。"""
        from .fsmodel import sys_icon
        w = QWidget()
        w.setObjectName("midOps")
        w.setContextMenuPolicy(Qt.CustomContextMenu)
        w.customContextMenuRequested.connect(lambda *_: self._splitter_menu())
        v = QVBoxLayout(w)
        v.setContentsMargins(2, 8, 2, 8)
        v.setSpacing(4)
        v.addStretch(1)
        # 只保留双边操作：复制到对侧 / 移动到对侧
        for tip, ic, fn in (("复制到对侧 (F5)", "copy", self.copy_selected),
                            ("移动到对侧 (F6)", "move", self.move_selected)):
            b = QPushButton(icons.icon(ic), "")
            b.setToolTip(tip)
            b.setFixedSize(36, 30)
            b.setIconSize(QSize(18, 18))
            b.clicked.connect(fn)
            v.addWidget(b)
        v.addStretch(1)
        return w

    # ---- 标签与智能文件夹 ------------------------------------------------
    def tag_entry(self, color):
        """给活动面板当前选中项加/移除标签（色点叠加在图标上）。"""
        from . import config, fsmodel
        pane = self.active_pane()
        e = pane.current_entry()
        if not e:
            return
        tags = dict(self.cfg.get("file_tags", {}))
        if color is None:
            tags.pop(e.path, None)
        else:
            lst = list(tags.get(e.path, []))
            if color not in lst:
                lst.append(color)
            tags[e.path] = lst
        self.cfg["file_tags"] = tags
        config.save(self.cfg)
        fsmodel.set_file_tags(tags)
        pane.refresh()

    def smart_open(self, sf):
        """在活动面板打开智能文件夹（递归搜索结果）。"""
        self.active_pane().show_smart(sf.get("name", ""),
                                      sf.get("scope", ""),
                                      sf.get("query", ""))

    def add_smart_folder(self):
        self.fav_tree._smart_dialog()

    def _ensure_fav_tree_width(self):
        """收藏树从隐藏转为显示时，QSplitter 会把它压到极小，需要显式给宽。"""
        sizes = self.center_split.sizes()
        if sizes and sizes[0] >= 150:
            return
        total = max(sum(sizes), self.center_split.width(), 900)
        # 左树受 maximumWidth(240) 限制，按当前窗口宽度动态分配，
        # 不能用固定总和（窗口远宽于默认值时会被 QSplitter 重新拉伸）。
        self.center_split.setSizes([180, 220, max(400, total - 400)])

    def toggle_fav_tree(self):
        vis = not self.fav_tree.isVisible()
        self.fav_tree.setVisible(vis)
        self.cfg["fav_tree"] = vis
        if vis:
            # setVisible 后布局尚未刷新，此时 setSizes 会被 QSplitter 重算覆盖；
            # 延后到下一事件循环再应用默认宽度。
            QTimer.singleShot(0, self._ensure_fav_tree_width)

    def toggle_quick_viewer(self):
        vis = not self.quick_viewer.isVisible()
        self.quick_viewer.setVisible(vis)
        if vis:
            self.update_quick_viewer()

    def toggle_functions_bar(self):
        self.func_bar.setVisible(not self.func_bar.isVisible())

    def toggle_command_line(self):
        self.cmd.setVisible(not self.cmd.isVisible())

    def update_quick_viewer(self):
        if not self.quick_viewer.isVisible():
            return
        pane = self.active_pane()
        self.quick_viewer.show_entry(pane.current_entry())

    def _schedule_quick_viewer(self):
        """去抖 150ms：连续选中/翻行时不逐次读文件。"""
        if not self.quick_viewer.isVisible():
            return
        if not hasattr(self, "_qv_timer"):
            from PySide6.QtCore import QTimer
            self._qv_timer = QTimer(self)
            self._qv_timer.setSingleShot(True)
            self._qv_timer.setInterval(150)
            self._qv_timer.timeout.connect(self.update_quick_viewer)
        self._qv_timer.start()

    # ---- 新增工具动作 --------------------------------------------------
    def set_view_mode(self, mode):
        self.cfg["view_mode"] = mode
        for pt in self.panes:
            for i in range(pt.count()):
                pt.widget(i).set_view_mode(mode)

    def toggle_tree(self):
        vis = not self.fs_tree.isVisible()
        self.fs_tree.setVisible(vis)
        self.cfg["fs_tree"] = vis
        if vis:
            self.fs_tree.sync(self.active_pane().current_path())

    def _tree_nav(self, path):
        self.active_pane().navigate(path)

    def _tree_new_tab(self, path):
        act = self.active_pane()
        pt = self._pane_tabs(act)
        if pt:
            pt.add_pane(path)
            self.save_tabs()

    # ---- 主分隔条右键菜单（FC UiMainSplitter）----
    def _splitter_menu(self):
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QCursor
        m = QMenu(self)
        m.addAction("水平 / 垂直排列", self.toggle_orientation)
        m.addAction("单 / 双栏 (Ctrl+H / F10)", self.toggle_layout)
        m.addSeparator()
        m.addAction("对侧打开同一目录 (Ctrl+E)", self.same_folder)
        m.addAction("交换两侧目录 (Ctrl+U)", self.swap_panes)
        m.addSeparator()
        for label, ratio in (("比例 25 / 75%", 0.25), ("比例 50 / 50%", 0.5),
                             ("比例 75 / 25%", 0.75)):
            m.addAction(label, lambda rt=ratio: self._set_split_ratio(rt))
        m.exec(QCursor.pos())

    def _set_split_ratio(self, ratio):
        mid = self.split.widget(1).width()
        total = self.split.width() - mid
        self.split.setSizes([int(total * ratio), mid, int(total * (1 - ratio))])

    def same_folder(self):
        """Ctrl+E：非活动面板打开活动面板的目录。"""
        act, other = self.active_pane(), self.inactive_pane()
        if act and other and act.current_path():
            other.navigate(act.current_path())

    def swap_panes(self):
        """Ctrl+U：交换两侧面板当前标签的目录。"""
        act, other = self.active_pane(), self.inactive_pane()
        if act and other:
            a, b = act.current_path(), other.current_path()
            if a and b:
                act.navigate(b)
                other.navigate(a)

    def apply_filter_both(self, text):
        """Ctrl+Enter：把过滤条件应用到两侧全部标签。"""
        for pt in self.panes:
            for i in range(pt.count()):
                pt.widget(i).filter_edit.setText(text)

    def sync_fs_tree(self, path):
        """去抖后同步树：QFileSystemModel.index 会同步加载祖先目录链，
        焦点快速切换时不能每次都同步阻塞 UI。"""
        self._tree_sync_path = path
        if not hasattr(self, "_tree_timer"):
            from PySide6.QtCore import QTimer
            self._tree_timer = QTimer(self)
            self._tree_timer.setSingleShot(True)
            self._tree_timer.setInterval(200)
            self._tree_timer.timeout.connect(
                lambda: self.fs_tree.sync(getattr(self, "_tree_sync_path", "")))
        self._tree_timer.start()

    def _pane_path_changed(self, pane, path):
        # 只有活动侧的活动标签才联动共享树
        if pane is self.active_pane():
            self.sync_fs_tree(path)

    def _pane_focused(self, pane):
        """点到某面板（即使不改路径）也把树同步到它 + 描边高亮。"""
        if pane is None:
            return
        self.update_status()
        self.sync_fs_tree(pane.current_path())
        pt = self._pane_tabs(pane)
        if pt is not None:
            i = self.panes.index(pt)
            self._active_idx = i
            self.cfg["active_pane"] = i
        # 活动面板描边
        for pt in self.panes:
            for i in range(pt.count()):
                pt.widget(i).set_active(pt.widget(i) is pane)

    def save_tabs(self):
        panes = []
        for pt in self.panes:
            tabs = []
            for i in range(pt.count()):
                w = pt.widget(i)
                tabs.append({"path": w.current_path(),
                             "title": getattr(w, "custom_title", ""),
                             "color": getattr(w, "tab_color", "")})
            panes.append({"tabs": tabs})
        self.cfg["panes"] = panes
        config.save(self.cfg)

    def toggle_orientation(self):
        vertical = self.split.orientation() == Qt.Horizontal
        self.split.setOrientation(Qt.Vertical if vertical else Qt.Horizontal)

    def select_all(self):
        pane = self.active_pane()
        pane._cur_view().selectAll()

    def invert_selection(self):
        pane = self.active_pane()
        view = pane._cur_view()
        sel_model = view.selectionModel()
        from PySide6.QtCore import QItemSelection, QItemSelectionRange
        sel = QItemSelection()
        for r in range(pane.proxy.rowCount()):
            sel.append(QItemSelectionRange(pane.proxy.index(r, 0)))
        sel_model.select(sel, sel_model.SelectionFlag.Toggle)

    def batch_rename(self):
        sel = self.active_pane().selected_entries()
        if not sel:
            return
        if BatchRenameDialog(self, sel).exec():
            self.active_pane().refresh()

    def calc_md5(self):
        sel = [e for e in self.active_pane().selected_entries() if not e.is_dir]
        if sel:
            Md5Dialog(self, [e.path for e in sel]).show()

    def zip_selected(self):
        pane = self.active_pane()
        sel = pane.selected_entries()
        if not sel:
            return
        default = os.path.join(pane.current_path(), "archive.zip")
        dst, _ = QFileDialog.getSaveFileName(self, "压缩为 ZIP", default, "ZIP (*.zip)")
        if not dst:
            return
        task = self.op.run_zip([e.path for e in sel], dst)
        if task is not None:
            task.sig.finished.connect(lambda *_: pane.refresh())

    def unpack_here(self):
        pane = self.active_pane()
        sel = [e for e in pane.selected_entries() if e.ext == "zip"]
        for e in sel:
            dst = os.path.join(pane.current_path(),
                               os.path.splitext(e.name)[0])
            os.makedirs(dst, exist_ok=True)
            task = self.op.run_unzip(e.path, dst)
            if task is not None:
                task.sig.finished.connect(lambda *_: pane.refresh())
            break

    # ---- 命令行 -----------------------------------------------------------
    def _run_cmd(self):
        text = self.cmd.text().strip()
        self.cmd.clear()
        if not text:
            return
        pane = self.active_pane()
        cwd = pane.current_path() or os.path.expanduser("~")
        if text.lower().startswith("cd ") or text == "cd":
            target = text[2:].strip() or os.path.expanduser("~")
            if not os.path.isabs(target):
                target = os.path.join(cwd, target)
            if os.path.isdir(target):
                pane.navigate(target)
            else:
                self.st_label.setText("目录不存在：" + target)
            return
        if text.startswith("!"):
            text = text[1:]
        if text not in self._cmd_history:
            self._cmd_history.append(text)
            if len(self._cmd_history) > 60:
                self._cmd_history.pop(0)
        self._cmd_hpos = -1
        try:
            subprocess.Popen(text, cwd=cwd, shell=True,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            self.st_label.setText("已执行：" + text)
        except OSError as exc:
            QMessageBox.warning(self, "执行失败", str(exc))

    # ---- 状态栏 -----------------------------------------------------------
    def quick_search_display(self, text):
        if text:
            self.st_label.setText("快速搜索：%s   （↓ 不能用时可继续输入；Esc 取消）" % text)
        elif self.st_label.text().startswith("快速搜索"):
            self.st_label.setText("就绪")

    _free_cache = {}   # 盘符 -> (时间戳, 文本)，disk_usage 对网络盘可能很慢，60s 缓存
    _free_pending = set()

    def _request_disk_usage(self, key, path):
        """缓存未命中时后台取磁盘余量，避免网络盘首次调用冻结 UI。"""
        if key in self._free_pending:
            return
        self._free_pending.add(key)

        def work():
            try:
                import shutil as _sh
                import time as _time
                free = "    磁盘剩余 %.1f GB" % (_sh.disk_usage(path).free / 1073741824.0)
                self._free_cache[key] = (_time.time(), free)
            except OSError:
                pass
            finally:
                self._free_pending.discard(key)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_free_label(self):
        """磁盘余量后台就绪后刷新状态栏（不改变数量/选中信息）。"""
        self.update_status()

    def update_status(self):
        pane = self.active_pane()
        if pane and pane.stack.currentWidget() is pane.results_list:
            self.st_count.setText(pane.smart_status())
            return
        if not pane or not pane.model.entries:
            self.st_count.setText("")
            return
        n_all = len(pane.model.entries)
        sel = pane.selected_entries()
        n_sel = len(sel)
        size = sum(e.size for e in sel if not e.is_dir)
        free = ""
        try:
            import time as _time
            key = os.path.splitdrive(os.path.abspath(pane.model.path or os.getcwd()))[0].upper()
            cached = self._free_cache.get(key)
            if cached and _time.time() - cached[0] < 60:
                free = cached[1]
            else:
                self._request_disk_usage(key, pane.model.path or os.getcwd())
        except OSError:
            pass
        self.st_count.setText("%d 项    选中 %d 项（%.1f MB）%s"
                              % (n_all, n_sel, size / 1048576.0, free))
        self._schedule_quick_viewer()


def _activate_existing_window():
    """唤起已运行的 Commander 主窗口到前台，返回是否成功。"""
    import ctypes
    title = "Commander — 文件管理"
    hwnd = ctypes.windll.user32.FindWindowW(None, title)
    if not hwnd:
        return False
    SW_RESTORE = 9
    ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    return True


def _try_single_instance():
    """单实例互斥：已有实例运行时唤起其主窗口并返回 False（本进程退出）。"""
    if os.name != "nt":
        return True
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    ctypes.windll.kernel32.CreateMutexW(None, False, "DeskTools_Commander_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        _activate_existing_window()
        return False
    return True


def main():
    if os.name == "nt":
        # 进程级设置必须早于 QApplication / 任何窗口创建，否则 Windows 任务栏
        # 会先按 pythonw.exe 注册按钮，之后再改 AppUserModelID 也不会刷新，
        # 表现为图标变成通用「应用」图标或干脆消失。
        try:
            import ctypes
            ctypes.windll.kernel32.FreeConsole()
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "cobyg.desktools.commander")
        except Exception:
            pass
    if not _try_single_instance():
        sys.exit(0)
    app = QApplication(sys.argv)
    app.setApplicationName("commander")
    apply_theme(app)
    icon_path = os.path.join(_ASSETS, "commander.ico")
    if os.path.exists(icon_path):
        from PySide6.QtGui import QColor, QIcon as _QIcon
        app.setWindowIcon(_QIcon(icon_path))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
