# -*- coding: utf-8 -*-
"""复制/移动进度对话框：显示总进度、当前速度与已处理文件，可取消。"""
import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QVBoxLayout)


def _fmt_bytes(n):
    if n < 1024:
        return "%d B" % n
    for u in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return "%.1f %s" % (n, u)
    return "%.1f PB" % n


class ProgressDialog(QDialog):
    def __init__(self, parent, task, title="文件操作"):
        super().__init__(parent)
        self.task = task
        self.total = 0
        self._t0 = time.time()
        self._done = 0
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(460)

        self.lb_title = QLabel(title + "…")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.lb_detail = QLabel("")
        self.lb_detail.setObjectName("muted")
        self.lb_log = QLabel("")
        self.lb_log.setObjectName("muted")

        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self._cancel)
        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(btn_cancel)

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(8)
        v.addWidget(self.lb_title)
        v.addWidget(self.bar)
        v.addWidget(self.lb_detail)
        v.addWidget(self.lb_log)
        v.addLayout(h)

    # ---- 信号槽 ----------------------------------------------------------
    def set_total(self, total):
        self.total = total

    def set_progress(self, done, total):
        self._done = done
        self.total = total
        pct = int(done * 100 / total) if total else 100
        self.bar.setValue(pct)
        elapsed = max(time.time() - self._t0, 0.001)
        speed = done / elapsed
        remain = (total - done) / speed if speed > 1 and total > done else 0
        self.lb_detail.setText("%s / %s    速度 %s/s    剩余约 %d 秒"
                               % (_fmt_bytes(done), _fmt_bytes(total),
                                  _fmt_bytes(speed), int(remain)))

    def add_log(self, src):
        self.lb_log.setText("已处理：" + src)

    def set_status(self, text):
        self.lb_log.setText(text)

    def on_conflict(self, src, dst, reply_cb):
        """目标已存在：询问是否覆盖。"""
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("目标已存在")
        box.setText(dst)
        box.setInformativeText("要覆盖它吗？")
        yes = box.addButton("覆盖", QMessageBox.AcceptRole)
        box.addButton("跳过", QMessageBox.RejectRole)
        box.exec()
        reply_cb(box.clickedButton() is yes)

    def on_error(self, src, msg, reply_cb):
        """后台线程报错：弹询问框（重试 / 跳过）。"""
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("操作出错")
        box.setText("处理 %s 时出错：" % src)
        box.setInformativeText(msg)
        retry = box.addButton("重试", QMessageBox.AcceptRole)
        box.addButton("跳过", QMessageBox.RejectRole)
        box.exec()
        reply_cb(box.clickedButton() is retry)

    def on_finished(self, had_error):
        self.accept()

    def _cancel(self):
        self.task.cancelled.set()
        self.lb_title.setText("正在取消…")

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(ev)
