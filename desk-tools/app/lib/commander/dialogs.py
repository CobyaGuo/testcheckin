# -*- coding: utf-8 -*-
"""工具对话框：批量重命名、MD5 校验。"""
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QSpinBox,
                               QVBoxLayout)

from .operations import Md5Task
from PySide6.QtCore import QThreadPool


class BatchRenameDialog(QDialog):
    """批量重命名：查找替换 + 前缀 + 序号后缀，实时预览，确定后执行。"""

    def __init__(self, parent, entries):
        super().__init__(parent)
        self.entries = entries
        self.setWindowTitle("批量重命名（%d 项）" % len(entries))
        self.resize(560, 460)

        g1 = QHBoxLayout()
        g1.addWidget(QLabel("查找："))
        self.ed_find = QLineEdit()
        g1.addWidget(self.ed_find, 1)
        g1.addWidget(QLabel("替换为："))
        self.ed_repl = QLineEdit()
        g1.addWidget(self.ed_repl, 1)

        g2 = QHBoxLayout()
        g2.addWidget(QLabel("添加前缀："))
        self.ed_prefix = QLineEdit()
        g2.addWidget(self.ed_prefix, 1)
        g2.addWidget(QLabel("起始序号"))
        self.sp_num = QSpinBox()
        self.sp_num.setRange(0, 99999)
        self.sp_num.setValue(1)
        g2.addWidget(self.sp_num)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setStyleSheet("QPlainTextEdit{background:#181b22;"
                                   "font-family:Consolas;font-size:11px;}")

        btn = QPushButton("执行重命名")
        btn.setObjectName("accent")
        btn.clicked.connect(self._apply)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 10)
        v.setSpacing(8)
        v.addWidget(QLabel("对 %d 个选中项生效（可组合查找替换 / 前缀 / 序号）" % len(entries)))
        v.addLayout(g1)
        v.addLayout(g2)
        v.addWidget(self.preview, 1)
        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(btn_cancel)
        h.addWidget(btn)
        v.addLayout(h)

        for w in (self.ed_find, self.ed_repl, self.ed_prefix):
            w.textChanged.connect(self._refresh_preview)
        self.sp_num.valueChanged.connect(self._refresh_preview)
        self._refresh_preview()

    def _new_name(self, name):
        stem, ext = os.path.splitext(name)
        find, repl = self.ed_find.text(), self.ed_repl.text()
        if find:
            stem = stem.replace(find, repl)
            stem = stem.replace(find.lower(), repl)   # 忽略大小写兜底
        stem = self.ed_prefix.text() + stem
        return stem, ext

    def _refresh_preview(self):
        lines = []
        start = self.sp_num.value()
        for i, e in enumerate(self.entries):
            stem, ext = self._new_name(e.name)
            new = "%s%02d%s" % (stem, start + i, ext) if stem else e.name
            mark = "  ✔" if new != e.name else ""
            lines.append("%s  →  %s%s" % (e.name, new, mark))
        self.preview.setPlainText("\n".join(lines) or "（无选中项）")

    def _apply(self):
        done = 0
        errs = []
        start = self.sp_num.value()
        for i, e in enumerate(self.entries):
            stem, ext = self._new_name(e.name)
            new = "%s%02d%s" % (stem, start + i, ext) if stem else e.name
            if new == e.name:
                continue
            dst = os.path.join(os.path.dirname(e.path), new)
            if os.path.lexists(dst):
                errs.append("已存在，跳过：%s" % new)
                continue
            try:
                os.rename(e.path, dst)
                done += 1
            except OSError as exc:
                errs.append("%s：%s" % (e.name, exc))
        if errs:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "部分失败",
                                "成功 %d 项。\n%s" % (done, "\n".join(errs[:10])))
        self.accept()


class Md5Dialog(QDialog):
    """MD5 计算窗口：后台逐个计算，可复制结果。"""

    def __init__(self, parent, paths):
        super().__init__(parent)
        self.setWindowTitle("MD5 校验（%d 项）" % len(paths))
        self.resize(680, 400)
        self.edit = QPlainTextEdit()
        self.edit.setReadOnly(True)
        self.edit.setStyleSheet("QPlainTextEdit{background:#181b22;"
                                "font-family:Consolas;font-size:11px;}")
        btn_copy = QPushButton("复制全部")
        btn_copy.clicked.connect(self._copy)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 8)
        v.addWidget(self.edit)
        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(btn_copy)
        h.addWidget(btn_close)
        v.addLayout(h)

        self.task = Md5Task(paths)
        self.task.sig.one.connect(self._on_one)
        self.task.sig.finished.connect(lambda: self.edit.setPlainText(
            self.edit.toPlainText() + "\n— 完成 —"))
        QThreadPool.globalInstance().start(self.task)

    def _on_one(self, path, md5):
        self.edit.setPlainText(self.edit.toPlainText() +
                               "%s  %s\n" % (md5, os.path.basename(path)))

    def _copy(self):
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(self.edit.toPlainText())

    def reject(self):
        self.task.cancelled.set()
        super().reject()
