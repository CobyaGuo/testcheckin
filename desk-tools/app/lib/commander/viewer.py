# -*- coding: utf-8 -*-
"""快速查看器（F3：文本/图片）与文件搜索对话框。"""
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QTextCursor
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QTreeWidget,
                               QStackedWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

TEXT_MAX = 4 * 1024 * 1024        # 文本预览上限 4MB
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svg"}


class ViewerDialog(QDialog):
    """只读快速查看：图片直接显示，文本截断提示。"""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("查看 — " + os.path.basename(path))
        self.resize(820, 620)
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

        ext = os.path.splitext(path)[1].lower()
        if ext in IMG_EXTS:
            pix = QPixmap(path)
            lab = QLabel()
            if not pix.isNull():
                lab.setPixmap(pix.scaled(780, 540, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
            else:
                lab.setText("无法加载图片：" + path)
            lab.setAlignment(Qt.AlignCenter)
            v.addWidget(lab)
        else:
            edit = QPlainTextEdit()
            edit.setObjectName("viewerText")
            edit.setReadOnly(True)
            edit.setLineWrapMode(QPlainTextEdit.NoWrap)
            try:
                with open(path, "rb") as f:
                    data = f.read(TEXT_MAX + 1)
                if len(data) > TEXT_MAX:
                    edit.setPlainText(data[:TEXT_MAX].decode("utf-8", "replace") +
                                      "\n\n— 文件过大已截断（仅显示前 4MB）—")
                else:
                    edit.setPlainText(data.decode("utf-8", "replace"))
            except (OSError, ValueError) as exc:
                edit.setPlainText("无法读取：%s" % exc)
            edit.moveCursor(QTextCursor.Start)
            v.addWidget(edit)

        btn = QPushButton("关闭 (Esc)")
        btn.clicked.connect(self.accept)
        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(btn)
        v.addLayout(h)


class SearchDialog(QDialog):
    """递归搜索当前目录：名称子串 / 通配符匹配，后台线程执行，双击结果跳转。"""

    item_found = Signal(str, str)     # 目录, 文件名
    search_done = Signal(int, bool)   # 找到数量, 是否被停止

    def __init__(self, root, show_hidden=False, parent=None):
        super().__init__(parent)
        self.root = root
        self.show_hidden = show_hidden
        self.result_path = None
        self._stop = False
        self.setWindowTitle("搜索 — " + root)
        self.resize(700, 560)

        h = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入名称关键字（支持 *.py 通配）")
        self.btn = QPushButton("搜索")
        self.btn.setObjectName("accent")
        self.btn.clicked.connect(self.start)
        self.input.returnPressed.connect(self.start)
        h.addWidget(self.input, 1)
        h.addWidget(self.btn)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["文件", "所在目录"])
        self.tree.setRootIsDecorated(False)
        self.tree.itemDoubleClicked.connect(self._open_item)
        self.lb = QLabel("搜索范围：" + root)
        self.lb.setObjectName("muted")

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)
        v.addLayout(h)
        v.addWidget(self.tree, 1)
        v.addWidget(self.lb)

        self.item_found.connect(self._add_ui)
        self.search_done.connect(self._finish_ui)

    def start(self):
        if self.btn.text() == "停止":
            self._stop = True
            return
        pattern = self.input.text().strip()
        if not pattern:
            return
        self.tree.clear()
        self._stop = False
        self.btn.setText("停止")

        import fnmatch
        import threading

        low = pattern.lower()
        use_fn = any(c in pattern for c in "*?[")

        def work():
            found = 0
            for dirpath, dirs, files in os.walk(self.root):
                if self._stop:
                    break
                if not self.show_hidden:
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                for n in dirs + files:
                    hit = fnmatch.fnmatch(n.lower(), low) if use_fn else low in n.lower()
                    if hit:
                        found += 1
                        self.item_found.emit(dirpath, n)
            self.search_done.emit(found, self._stop)

        threading.Thread(target=work, daemon=True).start()

    def _add_ui(self, dirpath, name):
        it = QTreeWidgetItem([name, dirpath])
        it.setData(0, Qt.UserRole, os.path.join(dirpath, name))
        self.tree.addTopLevelItem(it)

    def _finish_ui(self, found, stopped):
        self.btn.setText("搜索")
        self.lb.setText("搜索范围：%s    找到 %d 项%s"
                        % (self.root, found, "（已停止）" if stopped else ""))

    def _open_item(self, item, col):
        path = item.data(0, Qt.UserRole)
        if path and os.path.exists(path):
            self.result_path = path
            self.accept()


class QuickViewerPanel(QWidget):
    """快速查看面板（Ctrl+Q）：常驻显示活动面板当前高亮文件的内容。

    文本（前 1MB）/图片/其他类型（提示）。随选择变化实时刷新。
    """
    TEXT_LIMIT = 256 * 1024          # 只读前 256KB，避免大文件卡 UI
    SKIP_SIZE = 8 * 1024 * 1024      # 超过 8MB 的文件不再读取

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("quickViewer")
        self.lb_cap = QLabel("快速查看")
        self.lb_cap.setObjectName("cap")
        self.lb_name = QLabel("—")
        self.lb_name.setObjectName("muted")
        self.text = QPlainTextEdit()
        self.text.setObjectName("viewerText")
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.text)    # 0
        self.stack.addWidget(self.image)   # 1

        h = QHBoxLayout()
        h.setContentsMargins(8, 2, 8, 0)
        h.addWidget(self.lb_cap)
        h.addWidget(self.lb_name, 1)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addLayout(h)
        v.addWidget(self.stack, 1)

    def show_entry(self, entry):
        if entry is None:
            self.lb_name.setText("—")
            return
        self.lb_name.setText(entry.path)
        ext = os.path.splitext(entry.name)[1].lower()
        if entry.is_dir:
            self.stack.setCurrentIndex(0)
            self.text.setPlainText("〔文件夹〕%s" % entry.path)
        elif ext in IMG_EXTS:
            pix = QPixmap(entry.path)
            if pix.isNull():
                self.stack.setCurrentIndex(0)
                self.text.setPlainText("无法加载图片：%s" % entry.path)
            else:
                self.image.setPixmap(pix.scaled(560, 360, Qt.KeepAspectRatio,
                                                Qt.SmoothTransformation))
                self.stack.setCurrentIndex(1)
        else:
            self.stack.setCurrentIndex(0)
            try:
                if entry.size > self.SKIP_SIZE:
                    self.text.setPlainText("〔%.1f MB 大文件〕%s\n（快速查看已跳过，F3 可完整查看）"
                                           % (entry.size / 1048576.0, entry.path))
                    return
                with open(entry.path, "rb") as f:
                    data = f.read(self.TEXT_LIMIT + 1)
                if len(data) > self.TEXT_LIMIT:
                    self.text.setPlainText(data[:self.TEXT_LIMIT].decode("utf-8", "replace")
                                           + "\n— 已截断（前 256KB）—")
                else:
                    self.text.setPlainText(data.decode("utf-8", "replace"))
            except (OSError, ValueError) as exc:
                self.text.setPlainText("无法读取：%s" % exc)
