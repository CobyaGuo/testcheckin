# -*- coding: utf-8 -*-
"""操作队列栏（Operations queue bar）与收藏树面板（Favorite items tree panel）。

- QueueBar：文件面板下方的常驻条，显示运行中/排队操作数、当前进程、百分比；
  点击打开队列明细对话框。
- FavoritesTree：左侧收藏树面板，含收藏集（第一层目录、可嵌套子集，浏览器
  书签式）、智能文件夹、标签三个分区；支持名称含"/"的分组，右键管理，单击跳转。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMenu, QProgressBar,
                               QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget, QInputDialog, QMessageBox)

import os

_KEY_ROLE = Qt.UserRole + 2      # 树节点稳定键：展开状态保持 + 分组重建匹配


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
    """收藏树面板：常用目录 / 收藏集（嵌套）/ 智能文件夹 / 标签。"""
    navigate = Signal(str)
    changed = Signal()
    smart_requested = Signal(dict)   # 双击智能文件夹→主窗口在活动面板打开
    tag_requested = Signal(str)      # 单击标签→活动面板聚合展示
    open_file = Signal(str)          # 双击收藏的文件→系统默认程序打开

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._cur_set = ""   # 最近操作集合（""=常用目录），仅内部状态，无下拉 UI
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        # 展开状态跨 reload 保持：重建期间用 _restoring 屏蔽信号
        self._expanded_keys = set()
        self._seen_keys = set()
        self._restoring = False
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemCollapsed.connect(self._on_item_collapsed)
        # 单击即打开（收藏目录/收藏文件/智能文件夹）。原来只接双击，单击无响应；
        # 改单击后不再接双击，避免收藏文件被系统程序打开两次。QTreeWidget 对有
        # 子项的分组节点单击会展开，_open 对无 data 的分组节点直接忽略。
        self.tree.itemClicked.connect(self._open)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
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

    # ---- 展开状态（跨 reload 保持）---------------------------------------
    @staticmethod
    def _item_key(item):
        return str(item.data(0, _KEY_ROLE) or "") if item is not None else ""

    def _on_item_expanded(self, item):
        key = self._item_key(item)
        if key and not self._restoring:
            self._seen_keys.add(key)
            self._expanded_keys.add(key)

    def _on_item_collapsed(self, item):
        key = self._item_key(item)
        if key and not self._restoring:
            self._seen_keys.add(key)
            self._expanded_keys.discard(key)

    def _apply_expansion(self, parent):
        """重建后恢复展开状态：收藏集/收藏分组默认展开，标签分组默认收起。"""
        for i in range(parent.childCount()):
            c = parent.child(i)
            key = self._item_key(c)
            if key:
                if key in self._seen_keys:
                    c.setExpanded(key in self._expanded_keys)
                elif key == "root:tags":
                    c.setExpanded(bool(self.cfg.get("tag_defs")))
                elif (key.startswith("fav:") or key.startswith("set:")
                      or key in ("root:common", "root:smart", "root:sets")):
                    c.setExpanded(True)
            self._apply_expansion(c)

    def reload(self):
        self._restoring = True
        self.tree.clear()
        from .fsmodel import fav_icon
        from .icons import mac_folder, icon as _ic, color_dot
        self._fav_icon = fav_icon
        self._folder_icon = mac_folder()
        self._dot_icon = color_dot
        # 常用目录（桌面/下载/文档等）独立分区，不属于收藏集
        common = self.cfg.get("favorites")
        common = common if isinstance(common, list) else []
        qroot = QTreeWidgetItem(self.tree, [" 常用目录"])
        qroot.setIcon(0, _ic("monitor"))
        qroot.setFlags(Qt.ItemIsEnabled)
        qroot.setData(0, _KEY_ROLE, "root:common")
        self._add_fav_children(qroot, common, "")
        # 收藏集分区：收藏集是第一层目录，下面可再建子集（浏览器书签式）
        from . import favsets as _sets
        croot = QTreeWidgetItem(self.tree, [" 收藏集"])
        croot.setIcon(0, self._folder_icon)
        croot.setFlags(Qt.ItemIsEnabled)
        croot.setData(0, _KEY_ROLE, "root:sets")
        for key in _sets.top_sets(self.cfg):
            self._add_set_node(croot, key)
        # 智能文件夹（保存的搜索）分区：始终显示，右键可新建
        sroot = QTreeWidgetItem(self.tree, [" 智能文件夹"])
        sroot.setIcon(0, _ic("search"))
        sroot.setFlags(Qt.ItemIsEnabled)
        sroot.setData(0, _KEY_ROLE, "root:smart")
        for sf in self.cfg.get("smart_folders", []):
            sit = QTreeWidgetItem(sroot, [" " + sf.get("name", "")])
            sit.setIcon(0, _ic("search"))
            sit.setData(0, Qt.UserRole, ("smart", sf))
            sit.setToolTip(0, f"范围：{sf.get('scope','')}\n条件：{sf.get('query','')}")
        # 标签分区：一次 exists 遍历算完全部计数，再分发到各节点
        from . import tags as _tags
        stats = _tags.build_tag_stats(self.cfg)
        troot = QTreeWidgetItem(self.tree, [" 标签"])
        troot.setIcon(0, _ic("tag"))
        troot.setFlags(Qt.ItemIsEnabled)
        troot.setData(0, _KEY_ROLE, "root:tags")
        self._add_tag_children(troot, "", stats=stats)
        self._apply_expansion(self.tree.invisibleRootItem())
        self._restoring = False

    def _open(self, item, col):
        data = item.data(0, Qt.UserRole)
        if isinstance(data, tuple) and data[0] == "smart":
            self.smart_requested.emit(data[1])
            return
        if isinstance(data, tuple) and data[0] == "tag":
            self.tag_requested.emit(data[1])
            return
        if data:
            if os.path.isdir(data):
                self.navigate.emit(data)
            else:
                self.open_file.emit(data)

    def _add_set_node(self, parent, key):
        """渲染一个收藏集节点：子集递归在前，本集合收藏项在后。"""
        from . import favsets as _sets
        n = _sets.count(self.cfg, key)
        text = " %s (%d)" % (_sets.short_label(key), n)
        node = QTreeWidgetItem(parent, [text])
        node.setIcon(0, self._folder_icon)
        node.setData(0, Qt.UserRole, ("set", key))
        node.setData(0, _KEY_ROLE, "set:" + key)
        node.setToolTip(0, "收藏集：%s\n含子集共 %d 条收藏"
                        % (_sets.short_label(key), n))
        for child in _sets.children(self.cfg, key):
            self._add_set_node(node, child)
        self._add_fav_children(node, _sets.items(self.cfg, key), key)
        return node

    def _add_fav_children(self, parent, items, set_key, prefix=""):
        """按收藏名里的 "/" 递归构建集合内部的分组层级。"""
        for fav in items:
            # 配置容错：任一收藏缺字段/非 dict 时跳过，避免启动崩溃
            if not isinstance(fav, dict):
                continue
            name = str(fav.get("name") or "").strip()
            path = str(fav.get("path") or "").strip()
            if not name or not path:
                continue
            if prefix:
                if not name.startswith(prefix):
                    continue
                rel = name[len(prefix):].lstrip("/")
            else:
                rel = name
            if "/" in rel:
                seg = rel.split("/", 1)[0]
                full = prefix + seg
                # 默认集合沿用旧 key（兼容既有展开状态），命名集合带集合前缀
                # 避免不同集合的同名分组互相复用节点
                node_key = ("fav:" + full) if not set_key \
                    else ("fav:%s:%s" % (set_key, full))
                git = self._find_node(parent, node_key)
                count = sum(1 for f in items if isinstance(f, dict)
                            and str(f.get("name") or "").startswith(full + "/"))
                text = " %s (%d)" % (seg, count)
                if git is None:
                    git = QTreeWidgetItem(parent, [text])
                    git.setIcon(0, self._folder_icon)
                    git.setData(0, _KEY_ROLE, node_key)
                else:
                    git.setText(0, text)
                self._add_fav_children(git, items, set_key, full + "/")
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

    def _find_fav_by_id(self, fav_id):
        """从全部收藏集按对象 id 定位收藏项，返回 (fav, 所属集合 key)。"""
        for key, items in self._containers():
            for fav in items:
                if isinstance(fav, dict) and id(fav) == fav_id:
                    return fav, key
        return None, ""

    def _find_node(self, parent, key):
        for i in range(parent.childCount()):
            c = parent.child(i)
            if self._item_key(c) == key:
                return c
        return None

    def _add_tag_children(self, parent, prefix, stats=None):
        """按 "/" 递归构建标签层级；计数与 tooltip 复用一次聚合，零重复 stat。

        先建分组节点再建叶子：既是标签又是父分组的节点（如「用例」有子标签
        「用例/接口」）只显示自己的计数，不会被子节点覆盖。
        """
        from . import tags as _tags
        raw_defs = self.cfg.get("tag_defs")
        defs = raw_defs if isinstance(raw_defs, dict) else {}
        names = sorted(k for k in defs if isinstance(k, str))
        local = [n for n in names if not prefix or n.startswith(prefix)]

        groups = {}                       # seg -> full（本层需要展开的分组）
        for name in local:
            rel = name[len(prefix):] if prefix else name
            if "/" in rel:
                seg = rel.split("/", 1)[0]
                groups.setdefault(seg, prefix + seg)

        for seg, full in sorted(groups.items()):
            info = defs.get(full)
            info = info if isinstance(info, dict) else {}   # 脏数据守卫：与收藏侧一致
            color = info.get("color") or "#98a0ac"
            count = _tags.count_for(self.cfg, full, stats=stats)
            git = self._find_node(parent, "tag:" + full)
            text = " %s (%d)" % (seg, count)
            if git is None:
                git = QTreeWidgetItem(parent, [text])
                git.setIcon(0, self._dot_icon(color) if full in defs else self._folder_icon)
                git.setData(0, Qt.UserRole, ("tag", full) if full in defs else None)
                git.setData(0, Qt.UserRole + 1, full if full in defs else None)
                git.setData(0, _KEY_ROLE, "tag:" + full)
            else:
                git.setText(0, text)
            if full in defs:
                git.setToolTip(0, _tags.tag_node_tooltip(self.cfg, full, stats=stats))
            self._add_tag_children(git, full + "/", stats=stats)

        for name in local:
            rel = name[len(prefix):] if prefix else name
            if "/" in rel:
                continue
            info = defs.get(name)
            info = info if isinstance(info, dict) else {}
            color = info.get("color") or "#98a0ac"
            count = _tags.count_for(self.cfg, name, stats=stats)
            key = "tag:" + name
            it = self._find_node(parent, key)      # 同时是父分组时复用，不建重复节点
            text = " %s (%d)" % (name.split("/")[-1], count)
            if it is None:
                it = QTreeWidgetItem(parent, [text])
                it.setIcon(0, self._dot_icon(color))
                it.setData(0, Qt.UserRole, ("tag", name))
                it.setData(0, Qt.UserRole + 1, name)
                it.setData(0, _KEY_ROLE, key)
            else:
                it.setText(0, text)
            it.setToolTip(0, _tags.tag_node_tooltip(self.cfg, name, stats=stats))

    def build_tag_tree_menu(self, name):
        """标签树节点右键菜单：描述 / 管理 / 子标签 / 清理失效 / 删除。"""
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        acts = {
            "note": m.addAction("编辑描述…"),
            "manage": m.addAction("管理此标签…"),
            "child": m.addAction("新建子标签…"),
            "clean": m.addAction("清理失效标签记录…"),
            "delete": m.addAction("删除标签"),
        }
        for key, action in acts.items():
            action.setObjectName("tagact:" + key)
        return m, acts

    def build_tag_root_menu(self):
        """「标签」分区根节点右键：新建标签（不再是通用收藏菜单）。"""
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        acts = {"new": m.addAction("新建标签…")}
        acts["new"].setObjectName("tagrootact:new")
        return m, acts

    def handle_tag_root_action(self, act, acts):
        """执行标签根节点菜单动作。"""
        if act is None:
            return
        key = (act.objectName() or "")[len("tagrootact:"):] \
            if (act.objectName() or "").startswith("tagrootact:") else ""
        if not key:
            key = next((k for k, a in acts.items() if a is act), "")
        if key == "new":
            main = self.window()
            if hasattr(main, "new_tag_for_paths"):
                main.new_tag_for_paths([])   # 只建定义，不挂到文件

    def _tag_action_key(self, act, acts):
        key = (act.objectName() or "")[len("tagact:"):] \
            if (act.objectName() or "").startswith("tagact:") else ""
        return key or next((k for k, a in acts.items() if a is act), "")

    def handle_tag_action(self, act, acts, name):
        """执行标签树菜单动作；改配置后统一落盘 + 刷新。"""
        from . import tags as _tags
        if act is None:
            return
        key = self._tag_action_key(act, acts)
        if key == "note":
            return self._tag_note_action(name)
        if key == "manage":
            return self._tag_manage_action(name)
        if key == "child":
            child, ok = QInputDialog.getText(self, "新建子标签", "子标签名：", text=name + "/")
            if not ok or not child.strip():
                return
            full = _tags.clean_name(child)
            if not full.startswith(name + "/"):
                full = name + "/" + full
            if _tags.create_tag(self.cfg, full):
                self._commit()
            return
        if key == "clean":
            main = self.window()
            if hasattr(main, "clean_ghost_tags"):
                main.clean_ghost_tags(name)
            return
        if key == "delete":
            if QMessageBox.question(self, "删除标签",
                                    "删除标签「%s」及其子标签？只摘除标签，不会删除文件。"
                                    % name) == QMessageBox.Yes:
                if _tags.delete_tag(self.cfg, name):
                    self._commit()

    def _tag_note_action(self, name):
        main = self.window()
        if hasattr(main, "edit_tag_note"):
            main.edit_tag_note(name)

    def _tag_manage_action(self, name):
        main = self.window()
        if hasattr(main, "manage_tag_dialog"):
            main.manage_tag_dialog(name)

    def build_smart_menu(self, parent=None):
        """智能文件夹右键菜单：新建/编辑/删除；按 objectName 识别动作。

        注意：总是返回独立新菜单（action 挂在该菜单上）；调用方必须 exec
        这个返回的菜单本身，而不是把自己创建的外层菜单拿去 exec——否则
        弹出的是空菜单（右键「什么都没有」）。
        """
        m = QMenu(parent or self)
        acts = {
            "new": m.addAction("新建智能文件夹…"),
            "edit": m.addAction("编辑选中智能文件夹…"),
            "delete": m.addAction("删除选中智能文件夹"),
        }
        for key, act in acts.items():
            act.setObjectName("smartact:" + key)
        return m, acts

    def handle_smart_action(self, act, acts, it=None):
        """执行智能文件夹菜单动作；兼容对象身份比较的旧调用。"""
        if act is None:
            return
        key = (act.objectName() or "")[len("smartact:"):] \
            if (act.objectName() or "").startswith("smartact:") else ""
        if not key:
            key = next((k for k, a in acts.items() if a is act), "")
        if key == "new":
            return self._smart_dialog()
        data = it.data(0, Qt.UserRole) if it is not None else None
        if not isinstance(data, tuple) or data[0] != "smart":
            return
        if key == "edit":
            return self._smart_dialog(data[1])
        if key == "delete":
            self.cfg["smart_folders"] = [s for s in self.cfg.get("smart_folders", [])
                                         if s is not data[1]]
            from . import config
            config.save(self.cfg)
            self.reload()
            self.changed.emit()

    def prepare_smart_menu(self, it):
        """构建智能文件夹右键菜单（triggered 已连接）：根节点只提供新建。"""
        built, acts = self.build_smart_menu()
        data = it.data(0, Qt.UserRole) if it is not None else None
        if not (isinstance(data, tuple) and data[0] == "smart"):
            built.removeAction(acts["edit"])
            built.removeAction(acts["delete"])
        # 与文件右键标签菜单同一套机制：triggered 信号驱动。
        # exec 的返回值在某些平台/焦点状态下不可靠（甚至可能为 None），
        # 依赖它会导致「点了没反应」。
        built.triggered.connect(
            lambda act: self.handle_smart_action(act, acts, it))
        return built, acts

    def _smart_menu(self, menu, it):
        """智能文件夹右键：构建 + 执行。

        menu 参数仅为兼容旧调用签名；实际弹出的是 prepare_smart_menu
        构建的菜单（action 都在它上面），否则外层是空菜单。
        """
        built, _acts = self.prepare_smart_menu(it)
        built.exec(self.tree.mapToGlobal(self._menu_pos))

    def _smart_dialog(self, sf=None, prefill=None):
        """单个对话框同时编辑名称、搜索范围、搜索条件。"""
        from PySide6.QtWidgets import QDialog
        from .dialogs import SmartFolderDialog
        main = self.window()
        pane = main.active_pane() if hasattr(main, "active_pane") else None
        dflt_scope = pane.current_path() if pane else os.getcwd()
        pre = dict(sf or {}) if sf else (prefill or {})
        dlg = SmartFolderDialog(self, pre.get("name", ""),
                                pre.get("scope", dflt_scope),
                                pre.get("query", ""), dflt_scope)
        if dlg.exec() != QDialog.Accepted:
            return
        item = dlg.values()
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
        """key="" 为常用目录（favorites），否则为命名收藏集。"""
        if not key:
            return self.cfg.setdefault("favorites", [])
        return self.cfg.setdefault("fav_sets", {}).setdefault(key, [])

    def _containers(self):
        yield "", self._container("")
        for key in list(self.cfg.get("fav_sets", {})):
            yield key, self._container(key)

    def set_choices(self, exclude=""):
        """可移动到的位置：[(key, 显示名)]，key="" 为常用目录。

        子集是合法目标（浏览器书签可把条目从父目录移进子目录），不排除。
        """
        from . import favsets as _sets
        out = [("", "常用目录")] if exclude else []
        for key, label in _sets.all_sets(self.cfg):
            if exclude and key == exclude:
                continue
            out.append((key, label))
        return out

    def new_set(self, parent=None):
        """parent=None 新建顶层收藏集；parent 为 key 时新建其子收藏集。"""
        from . import favsets as _sets
        if parent is None:
            name, ok = QInputDialog.getText(self, "新建收藏集", "收藏集名称：")
        else:
            name, ok = QInputDialog.getText(
                self, "新建子收藏集", "子收藏集名称（可含 / 建多层）：")
        if not ok or not name.strip():
            return None
        full = _sets.create(self.cfg, name.strip(), parent=parent)
        if full:
            self._cur_set = full
            self._commit()
        return full

    def rename_set(self, key=None):
        """重命名收藏集（含子层级）；常用目录不属于收藏集，固定不可改。"""
        from . import favsets as _sets
        key = self._cur_set if key is None else key
        if not key:
            return None
        name, ok = QInputDialog.getText(
            self, "重命名收藏集", "新名称：", text=_sets.short_label(key))
        if not ok or not name.strip():
            return None
        new = _sets.rename(self.cfg, key, name.strip())
        if new:
            self._cur_set = new
            self._commit()
        return new

    def delete_set(self, key=None):
        from . import favsets as _sets
        key = self._cur_set if key is None else key
        if not key:
            return 0
        if QMessageBox.question(self, "删除收藏集",
                                "删除收藏集「%s」及其所有子集？只删收藏记录，不动文件。"
                                % _sets.short_label(key)) != QMessageBox.Yes:
            return 0
        doomed = _sets.descendants(self.cfg, key)
        n = _sets.delete(self.cfg, key)
        if any(self._cur_set == k or self._cur_set.startswith(k + "/") for k in doomed):
            self._cur_set = ""
        self._commit()
        return n

    def add_current_dir(self, key=None):
        target = self._cur_set if key is None else key
        main = self.window()
        pane = main.active_pane() if hasattr(main, "active_pane") else None
        path = pane.current_path() if pane else os.getcwd()
        if not path:
            return
        name, ok = QInputDialog.getText(self, "添加收藏", "名称（可用 分组/名称）：",
                                        text=os.path.basename(path))
        if ok and name.strip():
            self._container(target).append({"name": name.strip(), "path": path})
            self._commit()

    def add_selected_file(self, key=None):
        target = self._cur_set if key is None else key
        main = self.window()
        pane = main.active_pane() if hasattr(main, "active_pane") else None
        entries = pane.selected_entries() if pane else []
        files = [e for e in entries if not e.is_dir]
        if not files:
            QMessageBox.information(self, "添加收藏", "请先在面板中选中一个文件")
        else:
            self._container(target).append(
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
        if not self._cur_set:            # 常用目录不是收藏集，固定不可改名/删除
            acts["renset"].setEnabled(False)
            acts["delset"].setEnabled(False)
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
        """显示右键菜单并执行选中动作；工具栏按钮复用同一入口。

        triggered 信号驱动：exec 返回值在某些平台/焦点状态下不可靠。
        """
        menu, acts = self.build_menu(fav, source_set, parent)
        menu.triggered.connect(
            lambda act: self.handle_action(act, acts, fav, source_set))
        if global_pos is not None:
            menu.exec(global_pos)
        else:
            menu.exec_(QCursor.pos())

    def build_set_menu(self, key):
        """收藏集节点右键菜单：添加 / 新建子集 / 重命名 / 删除。"""
        from . import favsets as _sets
        m = QMenu(self)
        acts = {
            "add": m.addAction("在此收藏集添加当前目录…"),
            "addf": m.addAction("在此收藏集添加选中文件…"),
            "sub": m.addAction("新建子收藏集…"),
            "rename": m.addAction("重命名此收藏集…"),
            "delete": m.addAction("删除此收藏集…"),
        }
        for name, act in acts.items():
            act.setObjectName("setact:" + name)
        m.setToolTip("收藏集：%s" % _sets.short_label(key))
        return m, acts

    def handle_set_action(self, act, acts, key):
        """执行收藏集菜单动作；按 objectName 识别，不依赖对象身份。"""
        if act is None:
            return
        name = (act.objectName() or "")[len("setact:"):]
        if name == "add":
            return self.add_current_dir(key)
        if name == "addf":
            return self.add_selected_file(key)
        if name == "sub":
            return self.new_set(parent=key)
        if name == "rename":
            return self.rename_set(key)
        if name == "delete":
            return self.delete_set(key)

    def popup_set_menu(self, key, global_pos=None):
        """收藏集右键：构建 + 执行；triggered 信号驱动，不依赖 exec 返回值。"""
        menu, acts = self.build_set_menu(key)
        menu.triggered.connect(lambda act: self.handle_set_action(act, acts, key))
        if global_pos is not None:
            menu.exec(global_pos)
        else:
            menu.exec_(QCursor.pos())

    def _menu(self, pos):
        self._menu_pos = pos
        it = self.tree.itemAt(pos)
        if it is not None:
            data = it.data(0, Qt.UserRole)
            key = self._item_key(it)
            if key == "root:smart":
                menu = QMenu(self)
                self._smart_menu(menu, None)
                return
            if key == "root:tags":
                menu, acts = self.build_tag_root_menu()
                menu.triggered.connect(
                    lambda act: self.handle_tag_root_action(act, acts))
                menu.exec(self.tree.mapToGlobal(pos))
                return
            if isinstance(data, tuple) and data[0] == "smart":
                menu = QMenu(self)
                self._smart_menu(menu, it)
                return
            if isinstance(data, tuple) and data[0] == "tag":
                menu, acts = self.build_tag_tree_menu(data[1])
                menu.triggered.connect(
                    lambda act: self.handle_tag_action(act, acts, data[1]))
                menu.exec(self.tree.mapToGlobal(pos))
                return
            if isinstance(data, tuple) and data[0] == "set":
                self.popup_set_menu(data[1], self.tree.mapToGlobal(pos))
                return
            fav_id = it.data(0, Qt.UserRole + 1)
            fav, owner = self._find_fav_by_id(fav_id)
            if fav is not None:
                self.popup_menu(fav, owner, self.tree.mapToGlobal(pos), self)
                return
        self.popup_menu(None, self._cur_set, self.tree.mapToGlobal(pos), self)
