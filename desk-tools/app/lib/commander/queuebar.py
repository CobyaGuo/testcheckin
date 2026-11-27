# -*- coding: utf-8 -*-
"""操作队列栏（Operations queue bar）与收藏树面板（Favorite items tree panel）。

- QueueBar：文件面板下方的常驻条，显示运行中/排队操作数、当前进程、百分比；
  点击打开队列明细对话框。
- FavoritesTree：左侧收藏树面板，支持分组（名称含"/"归组），右键管理，双击跳转。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QMenu, QProgressBar,
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
    open_file = Signal(str)          # 双击收藏的文件→系统默认程序打开

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._cur_set = ""   # ""=默认收藏集
        self.set_combo = QComboBox()
        self.set_combo.setToolTip("切换收藏集")
        self.set_combo.currentIndexChanged.connect(self._set_changed)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        # 单击即打开（收藏目录/收藏文件/智能文件夹）。原来只接双击，单击无响应；
        # 改单击后不再接双击，避免收藏文件被系统程序打开两次。QTreeWidget 对有
        # 子项的分组节点单击会展开，_open 对无 data 的分组节点直接忽略。
        self.tree.itemClicked.connect(self._open)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.set_combo)
        v.addWidget(self.tree)
        self.reload()

    # ---- 收藏集 ----------------------------------------------------------
    def _items(self):
        if not self._cur_set:
            return self.cfg.get("favorites", [])
        return self.cfg.get("fav_sets", {}).get(self._cur_set, [])

    def _set_items(self, items):
        if self._cur_set:
            self.cfg.setdefault("fav_sets", {})[self._cur_set] = items
        else:
            self.cfg["favorites"] = items

    def _set_changed(self, idx):
        self._cur_set = "" if idx <= 0 else self.set_combo.currentText()
        self.reload()

    def _refresh_sets(self):
        self.set_combo.blockSignals(True)
        cur = self._cur_set
        self.set_combo.clear()
        self.set_combo.addItem("默认收藏")
        for k in self.cfg.get("fav_sets", {}):
            self.set_combo.addItem(k)
        i = self.set_combo.findText(cur) if cur else 0
        self.set_combo.setCurrentIndex(i if i >= 0 else 0)
        if i < 0:
            self._cur_set = ""
        self.set_combo.blockSignals(False)

    def reload(self):
        self.tree.clear()
        self._refresh_sets()
        from .fsmodel import fav_icon
        from .icons import mac_folder, icon as _ic
        self._fav_icon = fav_icon
        self._folder_icon = mac_folder()
        self._add_children(self.tree.invisibleRootItem(), "")
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
        if data:
            if os.path.isdir(data):
                self.navigate.emit(data)
            else:
                self.open_file.emit(data)

    def _add_children(self, parent, prefix):
        """按名称里的 "/" 递归构建任意深度的分组层级。"""
        items = self._items()
        for fav in items:
            # 配置容错：任一收藏缺字段/非 dict 时跳过，避免启动崩溃
            if not isinstance(fav, dict):
                continue
            name = str(fav.get("name") or "").strip()
            path = str(fav.get("path") or "").strip()
            if not name or not path:
                continue
            rel = name[len(prefix):].lstrip("/") if prefix and name.startswith(prefix) else name
            if prefix and not name.startswith(prefix):
                continue
            if "/" in rel:
                g = rel.split("/", 1)[0]
                git = None
                for i in range(parent.childCount()):
                    c = parent.child(i)
                    if c.text(0).strip() == g and c.data(0, Qt.UserRole) is None:
                        git = c
                        break
                if git is None:
                    git = QTreeWidgetItem(parent, [" " + g])
                    git.setIcon(0, self._folder_icon)
                    git.setExpanded(True)
                self._add_children(git, prefix + g + "/")
            else:
                it = QTreeWidgetItem(parent, [" " + name])
                it.setIcon(0, self._fav_icon(name))
                it.setData(0, Qt.UserRole, path)
                # 额外挂收藏项的对象 id：编辑/移动/删除按引用定位，避免同名
                # 或多层分组时按名称反查取错项。注意 Qt 的 setData 对 Python
                # 对象会返回副本，必须存 id(fav) 整数再按 id 在配置里回查。
                # UserRole 继续存路径供 _open 使用。
                it.setData(0, Qt.UserRole + 1, id(fav))
                it.setToolTip(0, path)

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

    def _smart_dialog(self, sf=None, prefill=None):
        """三步向导：名称 → 搜索范围 → 搜索条件（空格与，分号或）。"""
        from PySide6.QtWidgets import QInputDialog
        main = self.window()
        pane = main.active_pane() if hasattr(main, "active_pane") else None
        dflt_scope = pane.current_path() if pane else os.getcwd()
        pre = dict(sf or {}) if sf else (prefill or {})
        name, ok = QInputDialog.getText(self, "智能文件夹", "名称：",
                                        text=pre.get("name", ""))
        if not ok or not name.strip():
            return
        scope, ok = QInputDialog.getText(self, "智能文件夹", "搜索范围（目录）：",
                                         text=pre.get("scope", dflt_scope))
        if not ok or not os.path.isdir(scope.strip()):
            return
        query, ok = QInputDialog.getText(
            self, "智能文件夹",
            "搜索条件（空格与，分号或；支持 ext:/size:/date:/type:/tag:/:正则/）：",
            text=pre.get("query", ""))
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

    # ---- 收藏集/收藏项操作（收藏树右键与工具栏右键共用） ----------------
    def _commit(self):
        """统一落盘 + 刷新树/工具栏/菜单。"""
        from . import config
        config.save(self.cfg)
        self.reload()
        self.changed.emit()

    def _container(self, key):
        """key="" 为默认收藏集，否则为命名收藏集。"""
        if not key:
            return self.cfg.setdefault("favorites", [])
        return self.cfg.setdefault("fav_sets", {}).setdefault(key, [])

    def _containers(self):
        yield "", self._container("")
        for key in list(self.cfg.get("fav_sets", {})):
            yield key, self._container(key)

    def set_choices(self, exclude=""):
        """可移动到的收藏集列表：[(key, 显示名)]，排除来源集合。"""
        out = []
        if exclude:
            out.append(("", "默认收藏"))
        for key in self.cfg.get("fav_sets", {}):
            if key != exclude:
                out.append((key, key))
        return out

    def new_set(self):
        name, ok = QInputDialog.getText(self, "新建收藏集", "收藏集名称：")
        if ok and name.strip():
            self._cur_set = name.strip()
            self._container(self._cur_set)
            self._commit()

    def rename_set(self):
        if not self._cur_set:
            return
        name, ok = QInputDialog.getText(self, "重命名收藏集", "新名称：", text=self._cur_set)
        if ok and name.strip():
            sets = self.cfg.get("fav_sets", {})
            if self._cur_set in sets:
                sets[name.strip()] = sets.pop(self._cur_set)
            self._cur_set = name.strip()
            self._commit()

    def delete_set(self):
        if not self._cur_set:
            return
        if QMessageBox.question(self, "删除收藏集",
                                f"删除收藏集「{self._cur_set}」？") == QMessageBox.Yes:
            self.cfg.get("fav_sets", {}).pop(self._cur_set, None)
            self._cur_set = ""
            self._commit()

    def add_current_dir(self):
        main = self.window()
        pane = main.active_pane() if hasattr(main, "active_pane") else None
        path = pane.current_path() if pane else os.getcwd()
        if not path:
            return
        name, ok = QInputDialog.getText(self, "添加收藏", "名称（可用 分组/名称）：",
                                        text=os.path.basename(path))
        if ok and name.strip():
            self._container(self._cur_set).append({"name": name.strip(), "path": path})
            self._commit()

    def add_selected_file(self):
        main = self.window()
        pane = main.active_pane() if hasattr(main, "active_pane") else None
        entries = pane.selected_entries() if pane else []
        files = [e for e in entries if not e.is_dir]
        if not files:
            QMessageBox.information(self, "添加收藏", "请先在面板中选中一个文件")
        else:
            self._container(self._cur_set).append(
                {"name": os.path.basename(files[0].path), "path": files[0].path})
            self._commit()

    def edit_fav(self, fav):
        """编辑收藏项名称/路径（按对象引用定位，分组/同名也不串）。"""
        if not isinstance(fav, dict):
            return
        name, ok = QInputDialog.getText(self, "编辑收藏", "名称（可用 分组/名称）：",
                                        text=fav.get("name", ""))
        if not ok or not name.strip():
            return
        path, ok = QInputDialog.getText(self, "编辑收藏", "路径：", text=fav.get("path", ""))
        if not ok or not path.strip():
            return
        fav["name"], fav["path"] = name.strip(), path.strip()
        self._commit()

    def move_fav(self, fav, target_set=""):
        """把收藏项移到目标收藏集（target_set="" 为默认收藏集）。"""
        if not isinstance(fav, dict):
            return False
        if target_set and target_set not in self.cfg.get("fav_sets", {}):
            return False
        for _, items in self._containers():
            items[:] = [f for f in items if f is not fav]
        self._container(target_set).append(fav)
        self._cur_set = target_set       # 切到目标集合，让用户立刻看到移动结果
        self._commit()
        return True

    def delete_fav(self, fav):
        if not isinstance(fav, dict):
            return
        for _, items in self._containers():
            items[:] = [f for f in items if f is not fav]
        self._commit()

    def build_menu(self, fav=None, source_set="", parent=None):
        """构建右键菜单，返回 (menu, actions)；不 exec，便于测试与工具栏复用。"""
        menu = QMenu(parent or self)
        acts = {}
        if fav is not None:
            acts["edit"] = menu.addAction("编辑此收藏…")
            m_move = menu.addMenu("移动到收藏集")
            acts["move"] = {}
            for key, label in self.set_choices(source_set):
                a = m_move.addAction(label)
                acts["move"][a] = key
            if not acts["move"]:
                m_move.setEnabled(False)
            acts["del"] = menu.addAction("删除此收藏")
            menu.addSeparator()
        acts["add"] = menu.addAction("添加当前目录…")
        acts["addf"] = menu.addAction("添加当前选中文件…")
        m_sets = menu.addMenu("收藏集")
        acts["newset"] = m_sets.addAction("新建收藏集…")
        acts["renset"] = m_sets.addAction("重命名当前收藏集…")
        acts["delset"] = m_sets.addAction("删除当前收藏集…")
        return menu, acts

    def handle_action(self, act, acts, fav=None, source_set=""):
        if act is None:
            return
        if fav is not None:
            if act is acts.get("edit"):
                return self.edit_fav(fav)
            move_map = acts.get("move", {})
            if act in move_map:
                return self.move_fav(fav, move_map[act])
            if act is acts.get("del"):
                return self.delete_fav(fav)
        if act is acts.get("add"):
            return self.add_current_dir()
        if act is acts.get("addf"):
            return self.add_selected_file()
        if act is acts.get("newset"):
            return self.new_set()
        if act is acts.get("renset"):
            return self.rename_set()
        if act is acts.get("delset"):
            return self.delete_set()

    def popup_menu(self, fav=None, source_set="", global_pos=None, parent=None):
        """显示右键菜单并执行选中动作；工具栏按钮复用同一入口。"""
        menu, acts = self.build_menu(fav, source_set, parent)
        act = menu.exec(global_pos) if global_pos is not None else menu.exec_(QCursor.pos())
        self.handle_action(act, acts, fav, source_set)

    def _menu(self, pos):
        self._menu_pos = pos
        it = self.tree.itemAt(pos)
        if it is not None:
            data = it.data(0, Qt.UserRole)
            if (isinstance(data, tuple) and data[0] == "smart") or \
                    it.text(0).strip() == "智能文件夹":
                menu = QMenu(self)
                self._smart_menu(menu, it)
                return
            fav_id = it.data(0, Qt.UserRole + 1)
            fav = next((f for f in self._items()
                        if isinstance(f, dict) and id(f) == fav_id), None)
            if fav is not None:
                self.popup_menu(fav, self._cur_set, self.tree.mapToGlobal(pos), self)
                return
        self.popup_menu(None, self._cur_set, self.tree.mapToGlobal(pos), self)
