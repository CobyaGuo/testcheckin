# -*- coding: utf-8 -*-
"""操作队列栏（Operations queue bar）与收藏树面板（Favorite items tree panel）。

- QueueBar：文件面板下方的常驻条，显示运行中/排队操作数、当前进程、百分比；
  点击打开队列明细对话框。
- FavoritesTree：左侧收藏树面板，支持分组（名称含"/"归组），右键管理，双击跳转。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMenu, QProgressBar,
                               QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget, QInputDialog, QMessageBox)

import os


class QueueDialog(QDialog):
    """操作队列明细。"""

    def __init__(self, queue, parent=None):
        super().__init__(parent)
        self.queue = queue
        self.setWindowTitle("操作队列")
        self.resize(620, 320)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["操作", "当前进程", "状态", "进度"])
        self.tree.setRootIsDecorated(False)
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 8)
        v.addWidget(self.tree)
        btn = QPushButton("关闭")
        btn.clicked.connect(self.accept)
        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(btn)
        v.addLayout(h)
        queue.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        self.tree.clear()
        for it in self.queue.items:
            QTreeWidgetItem(self.tree, [it["desc"], it["cur"],
                                        it["status"], "%d%%" % it["percent"]])


class QueueBar(QWidget):
    """底部常驻操作队列栏。"""

    def __init__(self, queue, parent=None):
        super().__init__(parent)
        self.queue = queue
        self.setObjectName("queueBar")

        self.btn_open = QPushButton("队列")
        self.btn_open.setFlat(True)
        self.lb_info = QLabel("无后台操作")
        self.lb_info.setObjectName("muted")
        self.bar = QProgressBar()
        self.bar.setFixedWidth(160)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)

        h = QHBoxLayout(self)
        h.setContentsMargins(8, 2, 8, 2)
        h.setSpacing(8)
        h.addWidget(self.btn_open)
        h.addWidget(self.lb_info, 1)
        h.addWidget(self.bar)
        self.btn_open.clicked.connect(self._open_dialog)
        queue.changed.connect(self.refresh)
        self.hide()   # 无操作时默认隐藏

    def refresh(self):
        active = [it for it in self.queue.items if it["status"] == "运行中"]
        if not active:
            done = len([it for it in self.queue.items if it["status"].startswith("完成")])
            if done:
                self.lb_info.setText("最近操作已完成：%s" % self.queue.items[-1]["desc"])
                self.bar.setValue(100)
                self.show()
            else:
                self.hide()
            return
        it = active[-1]
        self.show()
        self.lb_info.setText("操作 %d 项    运行中 %d    当前：%s"
                             % (len(active), len(active), it["cur"] or it["desc"]))
        self.bar.setValue(it["percent"])

    def _open_dialog(self):
        QueueDialog(self.queue, self.window()).exec()


def _emoji_icon(ch):
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
    pm = QPixmap(16, 16)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setFont(QFont("Segoe UI Emoji", 9))
    p.drawText(pm.rect(), Qt.AlignCenter, ch)
    p.end()
    return QIcon(pm)


class FavoritesTree(QWidget):
    """收藏树面板：收藏（分组/名称）+ 智能文件夹（保存的搜索）。"""
    navigate = Signal(str)
    changed = Signal()
    smart_requested = Signal(dict)   # 双击智能文件夹→主窗口在活动面板打开

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        # 单击即打开（收藏/智能文件夹），双击保留兼容；QTreeWidget 对有子项的
        # 节点单击会展开，_open 里对无 data 的分组/智能根节点直接忽略。
        self.tree.itemClicked.connect(self._open)
        self.tree.itemDoubleClicked.connect(self._open)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.tree)
        self.reload()

    def reload(self):
        self.tree.clear()
        groups = {}
        for fav in self.cfg.get("favorites", []):
            if not isinstance(fav, dict):
                continue
            name = str(fav.get("name") or "").strip()
            path = str(fav.get("path") or "").strip()
            if not name or not path:
                continue
            if "/" in name:
                g, n = name.split("/", 1)
                groups.setdefault(g, []).append((n, path))
            else:
                groups.setdefault("", []).append((name, path))
        # 根级收藏在前，分组在后
        from .fsmodel import fav_icon
        for n, p in groups.get("", []):
            it = QTreeWidgetItem(self.tree, [" " + n])
            it.setIcon(0, fav_icon(n))
            it.setData(0, Qt.UserRole, p)
            it.setToolTip(0, p)
        from .icons import mac_folder, icon as _ic
        for g, items in groups.items():
            if not g:
                continue
            git = QTreeWidgetItem(self.tree, [" " + g])
            git.setIcon(0, mac_folder())
            for n, p in items:
                it = QTreeWidgetItem(git, [" " + n])
                it.setIcon(0, fav_icon(n))
                it.setData(0, Qt.UserRole, p)
                it.setToolTip(0, p)
            git.setExpanded(True)
        # 智能文件夹（保存的搜索）分区：始终显示，右键可新建
        sroot = QTreeWidgetItem(self.tree, [" 智能文件夹"])
        sroot.setIcon(0, _ic("search"))
        sroot.setFlags(Qt.ItemIsEnabled)
        for sf in self.cfg.get("smart_folders", []):
            sit = QTreeWidgetItem(sroot, [" " + sf.get("name", "")])
            sit.setIcon(0, _ic("search"))
            sit.setData(0, Qt.UserRole, ("smart", sf))
            sit.setToolTip(0, f"范围：{sf.get('scope','')}\n条件：{sf.get('query','')}")
        sroot.setExpanded(True)

    def _open(self, item, col):
        data = item.data(0, Qt.UserRole)
        if isinstance(data, tuple) and data[0] == "smart":
            self.smart_requested.emit(data[1])
            return
        p = data
        if p and os.path.isdir(p):
            self.navigate.emit(p)

    def _smart_menu(self, menu, it):
        """智能文件夹右键：新建/编辑/删除。"""
        from PySide6.QtWidgets import QInputDialog
        a_new = menu.addAction("新建智能文件夹…")
        a_edit = menu.addAction("编辑选中智能文件夹…")
        a_del = menu.addAction("删除选中智能文件夹")
        act = menu.exec(self.tree.mapToGlobal(self._menu_pos))
        if act is a_new:
            self._smart_dialog()
        elif act is a_edit:
            data = it.data(0, Qt.UserRole)
            if isinstance(data, tuple) and data[0] == "smart":
                self._smart_dialog(data[1])
        elif act is a_del:
            data = it.data(0, Qt.UserRole)
            if isinstance(data, tuple) and data[0] == "smart":
                self.cfg["smart_folders"] = [s for s in self.cfg.get("smart_folders", [])
                                             if s is not data[1]]
                from . import config
                config.save(self.cfg)
                self.reload()
                self.changed.emit()

    def _smart_dialog(self, sf=None):
        """三步向导：名称 → 搜索范围 → 搜索条件（空格与，分号或）。"""
        from PySide6.QtWidgets import QInputDialog
        main = self.window()
        pane = main.active_pane() if hasattr(main, "active_pane") else None
        dflt_scope = pane.current_path() if pane else os.getcwd()
        name, ok = QInputDialog.getText(self, "智能文件夹", "名称：",
                                        text=(sf or {}).get("name", ""))
        if not ok or not name.strip():
            return
        scope, ok = QInputDialog.getText(self, "智能文件夹", "搜索范围（目录）：",
                                         text=(sf or {}).get("scope", dflt_scope))
        if not ok or not os.path.isdir(scope.strip()):
            return
        query, ok = QInputDialog.getText(self, "智能文件夹",
                                         "搜索条件（文件名包含；空格与，分号或）：",
                                         text=(sf or {}).get("query", ""))
        if not ok:
            return
        item = {"name": name.strip(), "scope": scope.strip(), "query": query.strip()}
        sfs = self.cfg.setdefault("smart_folders", [])
        if sf is not None and sf in sfs:
            sfs[sfs.index(sf)] = item
        else:
            sfs.append(item)
        from . import config
        config.save(self.cfg)
        self.reload()
        self.changed.emit()

    def _menu(self, pos):
        self._menu_pos = pos
        it = self.tree.itemAt(pos)
        if it is not None:
            data = it.data(0, Qt.UserRole)
            if isinstance(data, tuple) and data[0] == "smart" or \
                    it.text(0).strip() == "智能文件夹":
                menu = QMenu(self)
                self._smart_menu(menu, it)
                return
        menu = QMenu(self)
        a_add = menu.addAction("添加当前目录…")
        a_edit = menu.addAction("编辑选中收藏…")
        a_del = menu.addAction("删除选中收藏")
        act = menu.exec(self.tree.mapToGlobal(pos))
        if act is a_add:
            main = self.window()
            pane = main.active_pane() if hasattr(main, "active_pane") else None
            path = pane.current_path() if pane else os.getcwd()
            name, ok = QInputDialog.getText(self, "添加收藏", "名称（可用 分组/名称）：",
                                            text=os.path.basename(path))
            if ok and name.strip():
                self.cfg.setdefault("favorites", []).append(
                    {"name": name.strip(), "path": path})
                from . import config
                config.save(self.cfg)
                self.reload()
                self.changed.emit()
        elif act is a_edit:
            it = self.tree.currentItem()
            if it:
                old_name = it.text(0).lstrip("★📁 ")
                if it.parent():
                    old_name = it.parent().text(0).lstrip("📁 ") + "/" + old_name
                fav = next((f for f in self.cfg.get("favorites", [])
                            if isinstance(f, dict) and f.get("name") == old_name), None)
                if fav:
                    name, ok = QInputDialog.getText(self, "编辑收藏", "名称：",
                                                    text=fav["name"])
                    if not ok or not name.strip():
                        return
                    path, ok = QInputDialog.getText(self, "编辑收藏", "路径：",
                                                    text=fav["path"])
                    if not ok or not path.strip():
                        return
                    fav["name"], fav["path"] = name.strip(), path.strip()
                    from . import config
                    config.save(self.cfg)
                    self.reload()
                    self.changed.emit()
        elif act is a_del:
            it = self.tree.currentItem()
            if it:
                name = it.text(0).lstrip("★📁 ")
                if it.parent():
                    name = it.parent().text(0).lstrip("📁 ") + "/" + name
                self.cfg["favorites"] = [f for f in self.cfg.get("favorites", [])
                                         if not isinstance(f, dict) or f.get("name") != name]
                from . import config
                config.save(self.cfg)
                self.reload()
                self.changed.emit()
