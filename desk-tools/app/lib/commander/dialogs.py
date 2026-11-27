# -*- coding: utf-8 -*-
"""工具对话框：批量重命名、MD5 校验、标签管理。"""
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
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
        jobs = []
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
            jobs.append((e.path, dst, e.name))
        if not jobs:
            if errs:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self, "未执行", "\n".join(errs[:10]))
            else:
                self.accept()
            return

        from .operations import RenameTask
        from .progress import ProgressDialog
        task = RenameTask(jobs)
        parent = self.parent()
        if parent is not None and hasattr(parent, "_on_paths_moved"):
            task.sig.moved.connect(parent._on_paths_moved)   # 标签路径同步
        dlg = ProgressDialog(parent, task, title="批量重命名")
        dlg.setModal(True)
        task.sig.progress.connect(dlg.set_progress)
        task.sig.file_done.connect(dlg.add_log)
        task.sig.finished.connect(dlg.on_finished)
        task.sig.finished.connect(dlg.close)
        if errs:
            dlg.lb_log.setText("跳过 %d 项（目标已存在），详见完成后提示" % len(errs))
        dlg.show()
        QThreadPool.globalInstance().start(task)
        self.accept()


class TagManagerDialog(QDialog):
    """管理命名标签：改名（含子层级）/ 改色 / 描述 / 删除（不动文件）。"""

    def __init__(self, name, color, parent=None, note=""):
        super().__init__(parent)
        self.setWindowTitle("管理标签 — " + name)
        self.resize(400, 290)
        self.result_action = None

        from . import icons, tags
        self.name_edit = QLineEdit(name, self)
        self.color_combo = QComboBox(self)
        for c in tags.TAG_COLORS:
            label = "%s  %s" % (tags.COLOR_NAMES.get(c, "自定义"), c)
            self.color_combo.addItem(icons.color_dot(c), label, c)
        i = self.color_combo.findData(str(color or "").lower())
        self.color_combo.setCurrentIndex(max(0, i))

        form = QVBoxLayout(self)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(8)
        form.addWidget(QLabel("标签名（用 / 表示层级）："))
        form.addWidget(self.name_edit)
        form.addWidget(QLabel("颜色："))
        form.addWidget(self.color_combo)
        form.addWidget(QLabel("描述（tooltip 中展示，支持多行）："))
        self.note_edit = QPlainTextEdit(note or "", self)
        self.note_edit.setMaximumHeight(90)
        self.note_edit.setPlaceholderText("说明这个标签的用途，例如：接口自动化回归用例")
        form.addWidget(self.note_edit)

        h = QHBoxLayout()
        h.addStretch(1)
        b_save = QPushButton("保存", self)
        b_save.setObjectName("accent")
        b_del = QPushButton("删除标签", self)
        b_cancel = QPushButton("取消", self)
        h.addWidget(b_del)
        h.addWidget(b_cancel)
        h.addWidget(b_save)
        form.addLayout(h)

        def finish(action):
            self.result_action = action
            self.accept()
        b_save.clicked.connect(lambda: finish("save"))
        b_del.clicked.connect(lambda: finish("delete"))
        b_cancel.clicked.connect(self.reject)


class NewLinkDialog(QDialog):
    """新建链接文件：名称 + URL + 描述 + 挂载标签（可选/可新建）。

    只收集输入，不写盘；主窗口校验后用 tags.save_link() 生成标准 .url，
    保证离屏测试既能直接测数据层，也能测到对话框组装出的值。
    """

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("新建链接…")
        self.resize(500, 460)

        from . import tags as _tags
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("例如：季度报表（OneDrive）")
        self.url_edit = QLineEdit(self)
        self.url_edit.setPlaceholderText("https://… 或 file:///…")
        self.note_edit = QPlainTextEdit(self)
        self.note_edit.setMaximumHeight(80)
        self.note_edit.setPlaceholderText("这条链接的用途说明（存于 link_notes）")
        self.dir_edit = QLineEdit(_tags.links_dir(cfg), self)

        b_browse = QPushButton("浏览…", self)
        b_browse.clicked.connect(self._browse_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(b_browse)

        self.tag_list = QListWidget(self)
        for name in sorted((cfg.get("tag_defs") or {})):
            self._add_tag_item(name)
        b_new_tag = QPushButton("新建标签…", self)
        b_new_tag.clicked.connect(self._new_tag)
        tag_row = QHBoxLayout()
        tag_row.addWidget(b_new_tag)
        tag_row.addStretch(1)

        b_ok = QPushButton("创建链接", self)
        b_ok.setObjectName("accent")
        b_ok.clicked.connect(self.accept)
        b_cancel = QPushButton("取消", self)
        b_cancel.clicked.connect(self.reject)
        self.keep_going = QCheckBox("建完继续", self)
        self.keep_going.setToolTip("勾选后停留在当前目录，仅状态栏提示；适合连续批量建链接。")
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.keep_going)
        btn_row.addStretch(1)
        btn_row.addWidget(b_cancel)
        btn_row.addWidget(b_ok)

        form = QVBoxLayout(self)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(7)
        form.addWidget(QLabel("名称："))
        form.addWidget(self.name_edit)
        form.addWidget(QLabel("URL："))
        form.addWidget(self.url_edit)
        form.addWidget(QLabel("描述："))
        form.addWidget(self.note_edit)
        form.addWidget(QLabel("保存位置："))
        form.addLayout(dir_row)
        form.addWidget(QLabel("挂载标签（勾选已有，或新建）："))
        form.addWidget(self.tag_list, 1)
        form.addLayout(tag_row)
        form.addLayout(btn_row)

    def _add_tag_item(self, name, checked=False):
        it = QListWidgetItem(name, self.tag_list)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        return it

    def _browse_dir(self):
        start = self.dir_edit.text().strip() or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "选择链接保存位置", start)
        if d:
            self.dir_edit.setText(d)

    def _new_tag(self):
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        from . import tags as _tags
        name, ok = QInputDialog.getText(self, "新建标签", "标签名（可含 /）：")
        name = _tags.clean_name(name) if ok else ""
        if not name:
            return
        for i in range(self.tag_list.count()):
            if self.tag_list.item(i).text() == name:
                QMessageBox.information(self, "新建标签", "该标签已在列表中。")
                self.tag_list.item(i).setCheckState(Qt.Checked)
                return
        self._add_tag_item(name, checked=True)   # 定义在创建链接时统一落盘

    def selected_tags(self):
        return [self.tag_list.item(i).text() for i in range(self.tag_list.count())
                if self.tag_list.item(i).checkState() == Qt.Checked]

    def values(self):
        return {"name": self.name_edit.text().strip(),
                "url": self.url_edit.text().strip(),
                "note": self.note_edit.toPlainText().strip(),
                "directory": self.dir_edit.text().strip(),
                "tags": self.selected_tags(),
                "keep_going": self.keep_going.isChecked()}


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


class HealthCheckDialog(QDialog):
    """标签与收藏体检：汇总 + 失效项列表（路径 + 所属），勾选后精确清理。

    只负责展示与收集勾选，不直接改配置；主窗口拿 selected_rows() 调用
    maintenance.clean_selected() 并统一落盘刷新，离屏测试可断言勾选集合。
    """

    def __init__(self, cfg, rows, stat, parent=None):
        super().__init__(parent)
        self.setWindowTitle("标签与收藏体检")
        self.resize(680, 440)
        self._rows = list(rows or [])

        from . import maintenance
        head = ("体检结果：%d 个标签 · %d 条收藏 · "
                "失效标签记录 <b>%d</b> 条 · 失效收藏 <b>%d</b> 条"
                % (stat.get("tags", 0), stat.get("favorites", 0),
                   stat.get("ghost_tags", 0), stat.get("ghost_favs", 0)))
        label = QLabel(head, self)
        label.setWordWrap(True)

        self.list = QListWidget(self)
        for r in self._rows:
            kind = "标签" if r.get("kind") == "tag" else "收藏"
            text = "[%s] %s  —  属于：%s" % (
                kind, r.get("path") or "(空路径)", r.get("owner") or "-")
            it = QListWidgetItem(text, self.list)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            it.setData(Qt.UserRole, r)
            it.setToolTip((r.get("path") or "") + "\n" + maintenance.GHOST_TEXT)

        b_all = QPushButton("全选", self)
        b_none = QPushButton("反选", self)
        b_clean = QPushButton("清理选中", self)
        b_clean.setObjectName("accent")
        b_cancel = QPushButton("取消", self)
        b_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        b_none.clicked.connect(self._invert)
        b_clean.clicked.connect(self.accept)
        b_cancel.clicked.connect(self.reject)

        h = QHBoxLayout()
        h.addWidget(b_all)
        h.addWidget(b_none)
        h.addStretch(1)
        h.addWidget(b_cancel)
        h.addWidget(b_clean)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)
        v.addWidget(label)
        v.addWidget(QLabel("勾选要清理的记录（文件可能只是挪了地方，请确认后再清理）：", self))
        v.addWidget(self.list, 1)
        v.addLayout(h)

    def _set_all(self, state):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def _invert(self):
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked)

    def selected_rows(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


class ExportDialog(QDialog):
    """汇总导出：勾选 Markdown / JSON 与保存目录，只收集选项不写盘。"""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("汇总导出")
        self.resize(460, 230)

        from . import tags
        default_dir = tags.links_dir(cfg)

        self.cb_md = QCheckBox("Markdown（人读，可贴 wiki / 笔记）", self)
        self.cb_md.setChecked(True)
        self.cb_json = QCheckBox("JSON（全量快照，备份 / 迁移）", self)
        self.cb_json.setChecked(True)

        self.dir_edit = QLineEdit(default_dir, self)
        b_browse = QPushButton("浏览…", self)
        b_browse.clicked.connect(self._browse)

        b_ok = QPushButton("导出", self)
        b_ok.setObjectName("accent")
        b_cancel = QPushButton("取消", self)
        b_ok.clicked.connect(self.accept)
        b_cancel.clicked.connect(self.reject)

        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(b_browse)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(b_cancel)
        btn_row.addWidget(b_ok)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)
        v.addWidget(QLabel("选择导出格式与保存目录：", self))
        v.addWidget(self.cb_md)
        v.addWidget(self.cb_json)
        v.addWidget(QLabel("保存到：", self))
        v.addLayout(dir_row)
        v.addWidget(QLabel("失效项会在导出物中标注 ⚠，建议先体检清理再导出干净快照。", self))
        v.addLayout(btn_row)

    def _browse(self):
        from PySide6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(
            self, "选择导出目录", self.dir_edit.text() or "")
        if path:
            self.dir_edit.setText(path)

    def values(self):
        return {"markdown": self.cb_md.isChecked(),
                "json": self.cb_json.isChecked(),
                "dir": self.dir_edit.text().strip()}


class SmartFolderDialog(QDialog):
    """新建/编辑智能文件夹：名称、搜索范围、条件在同一窗口完成。"""

    def __init__(self, parent=None, name="", scope="", query="", default_scope=""):
        super().__init__(parent)
        self.setWindowTitle("智能文件夹")
        self.resize(560, 240)
        self.default_scope = default_scope or os.getcwd()

        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("例如：最近修改的用例")
        self.scope_edit = QLineEdit(scope or self.default_scope)
        self.scope_edit.setPlaceholderText("递归搜索的起始目录")
        self.query_edit = QLineEdit(query)
        self.query_edit.setPlaceholderText("留空表示全部；如 ext:py date:7 size:>10mb")

        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        here = QPushButton("当前目录")
        here.clicked.connect(lambda: self.scope_edit.setText(self.default_scope))
        scope_row = QHBoxLayout()
        scope_row.addWidget(self.scope_edit, 1)
        scope_row.addWidget(here)
        scope_row.addWidget(browse)

        hint = QLabel("多个条件用空格（与）或分号 / 逗号（或）连接；"
                      "支持 ext: / size: / date: / type: / tag: / /正则/")
        hint.setObjectName("muted")
        hint.setWordWrap(True)

        self.btn_ok = QPushButton("保存")
        self.btn_ok.setObjectName("accent")
        self.btn_ok.setDefault(False)
        self.btn_ok.setAutoDefault(False)
        self.btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("取消")
        btn_cancel.setDefault(False)
        btn_cancel.setAutoDefault(False)
        btn_cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(btn_cancel)
        buttons.addWidget(self.btn_ok)

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)
        v.addWidget(QLabel("名称："))
        v.addWidget(self.name_edit)
        v.addWidget(QLabel("搜索范围："))
        v.addLayout(scope_row)
        v.addWidget(QLabel("搜索条件："))
        v.addWidget(self.query_edit)
        v.addWidget(hint)
        v.addStretch(1)
        v.addLayout(buttons)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(
            self, "选择搜索范围", self.scope_edit.text() or self.default_scope)
        if path:
            self.scope_edit.setText(path)

    def values(self):
        return {
            "name": self.name_edit.text().strip(),
            "scope": self.scope_edit.text().strip(),
            "query": self.query_edit.text().strip(),
        }

    def accept(self):
        v = self.values()
        if not v["name"]:
            QMessageBox.information(self, "智能文件夹", "请输入名称。")
            return
        if not os.path.isdir(v["scope"]):
            QMessageBox.information(self, "智能文件夹", "搜索范围不是有效目录。")
            return
        super().accept()
