# -*- coding: utf-8 -*-
"""快速查看器（F3：文本/图片）与文件搜索对话框。"""
import os

from PySide6.QtCore import QObject, Qt, QRunnable, Signal
from PySide6.QtGui import QPixmap, QTextCursor
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QTreeWidget,
                               QStackedWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

TEXT_MAX = 4 * 1024 * 1024        # 文本预览上限 4MB
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svg"}


class TextReadSignals(QObject):
    done = Signal(str, str)          # path, text（错误文本也走这里）


class TextReadTask(QRunnable):
    """后台读取文本预览，避免 UI 线程被 256KB~4MB 读取阻塞。"""

    def __init__(self, path, limit):
        super().__init__()
        self.path = path
        self.limit = limit
        self.sig = TextReadSignals()

    def run(self):
        try:
            with open(self.path, "rb") as f:
                data = f.read(self.limit + 1)
            if len(data) > self.limit:
                text = (data[:self.limit].decode("utf-8", "replace")
                        + "\n\n— 文件过大已截断（仅显示前 %s）—" % _fmt_size(self.limit))
            else:
                text = data.decode("utf-8", "replace")
        except (OSError, ValueError) as exc:
            text = "无法读取：%s" % exc
        self.sig.done.emit(self.path, text)


def _fmt_size(n):
    return "%dMB" % (n // 1048576) if n >= 1048576 else "%dKB" % (n // 1024)


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
            edit.setPlainText("正在读取…")
            v.addWidget(edit)
            self._read_task = TextReadTask(path, TEXT_MAX)   # 持有引用防止被 GC
            self._read_task.sig.done.connect(lambda _p, text, e=edit: (
                e.setPlainText(text), e.moveCursor(QTextCursor.Start)))
            from PySide6.QtCore import QThreadPool
            QThreadPool.globalInstance().start(self._read_task)

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
    result_opened = Signal(str)       # 双击结果：交给主面板定位（对话框保持打开）

    def __init__(self, root, show_hidden=False, parent=None):
        super().__init__(parent)
        self.root = root
        self.show_hidden = show_hidden
        self.result_path = None
        self._stop = False
        self._searching = False
        self.setWindowTitle("搜索 — " + root)
        self.resize(700, 560)

        h = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "名称关键字 / *.py 通配 / ext:py / size:>10mb / /正则/（空格与，分号或）")
        self.btn = QPushButton("搜索")
        self.btn.setObjectName("accent")
        # QDialog 默认按钮会在回车时同时发出 clicked，导致刚启动的搜索被立刻
        # 当成「停止」（结果 0）。关闭 autoDefault，两条输入路径彻底分开。
        self.btn.setAutoDefault(False)
        self.btn.setDefault(False)
        self.btn.clicked.connect(self.on_button_clicked)
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

    def on_button_clicked(self):
        """按钮单击：搜索中则停止，否则开始；与回车路径共用状态判定。"""
        if self._searching:
            self.stop()
        else:
            self.start()

    def stop(self):
        if not self._searching:
            return
        self._stop = True
        self.lb.setText("搜索范围：%s    正在停止…" % self.root)

    def start(self):
        # 回车连按 / 重复触发不应把刚启动的搜索当成停止
        if self._searching:
            return
        pattern = self.input.text().strip()
        if not pattern:
            return
        # 与面板智能搜索共用条件解析：ext:/size:/date:/type:/tag:/正则/
        # + 名称子串 + fnmatch 通配符（*.py）。局部导入避免模块循环依赖。
        from .pane import compile_query
        groups = compile_query(pattern)
        self.tree.clear()
        self._stop = False
        self._searching = True
        self.btn.setText("停止")
        self.lb.setText("搜索范围：%s    搜索中…" % self.root)

        import threading

        def work():
            found = 0
            for dirpath, dirs, files in os.walk(self.root):
                if self._stop:
                    break
                if not self.show_hidden:
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                for n in dirs + files:
                    if self._stop:
                        break
                    p = os.path.join(dirpath, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    is_dir = n in dirs
                    if any(all(pred(n, p, st, is_dir) for pred in grp if pred)
                           for grp in groups):
                        found += 1
                        self.item_found.emit(dirpath, n)
            self.search_done.emit(found, self._stop)

        threading.Thread(target=work, daemon=True).start()

    def _add_ui(self, dirpath, name):
        it = QTreeWidgetItem([name, dirpath])
        it.setData(0, Qt.UserRole, os.path.join(dirpath, name))
        self.tree.addTopLevelItem(it)

    def _finish_ui(self, found, stopped):
        self._searching = False
        self.btn.setText("搜索")
        self.lb.setText("搜索范围：%s    找到 %d 项%s"
                        % (self.root, found, "（已停止）" if stopped else ""))

    def _open_item(self, item, col):
        path = item.data(0, Qt.UserRole)
        if path and os.path.exists(path):
            self.result_path = path
            self.result_opened.emit(path)   # 非模态：主面板定位，对话框不关闭

    def closeEvent(self, event):
        # 用户中途关窗时让后台搜索尽快退出，避免线程继续 emit 到已关闭窗口
        self._stop = True
        super().closeEvent(event)


class QuickViewerPanel(QWidget):
    """快速查看面板（Ctrl+Q）：常驻显示活动面板当前高亮文件的内容。

    文本（前 1MB）/图片/其他类型（提示）。随选择变化实时刷新。
    """
    TEXT_LIMIT = 256 * 1024          # 只读前 256KB，避免大文件卡 UI
    SKIP_SIZE = 8 * 1024 * 1024      # 超过 8MB 的文件不再读取

    def __init__(self, parent=None):
        super().__init__(parent)
        self._read_epoch = 0
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

    def _on_read_done(self, epoch, text):
        if epoch != self._read_epoch:
            return
        if text == "正在读取…":
            return
        # 后台任务统一用「已截断（前 XXX）」文案，这里补成原面板口径
        text = text.replace("— 文件过大已截断", "— 已截断")
        self.text.setPlainText(text)

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
            if entry.size > self.SKIP_SIZE:
                self.text.setPlainText("〔%.1f MB 大文件〕%s\n（快速查看已跳过，F3 可完整查看）"
                                       % (entry.size / 1048576.0, entry.path))
                return
            self.text.setPlainText("正在读取…")
            self._read_epoch += 1
            epoch = self._read_epoch
            self._read_task = TextReadTask(entry.path, self.TEXT_LIMIT)  # 持有引用防 GC
            self._read_task.sig.done.connect(
                lambda _p, text, e=epoch: self._on_read_done(e, text))
            from PySide6.QtCore import QThreadPool
            QThreadPool.globalInstance().start(self._read_task)
