# -*- coding: utf-8 -*-
"""commander 视觉主题：与 command_panel.py 同套暗色配色 + 浅色（米灰）主题。

配色：底 #1c1f27 / 面板 #1e222c / 卡片 #242836 / 边框 #2c3040 /
主色 #3b82f6 / 成功 #34d399 / 失败 #f87171 / 警示 #fbbf24。
所有视觉样式集中在此唯一 QSS 常量，其余代码不调用 setStyleSheet。
"""
import os

UI_FONT = "Microsoft YaHei UI" if os.name == "nt" else "SF Pro Text"

QSS = r"""
QWidget {
    background: #1c1f27;
    color: #e8eaf0;
    font-family: "__UI_FONT__";
    font-size: 12px;
}
QMainWindow { background: #1c1f27; }
QDialog { background: #20242e; border: 1px solid #353b4d; border-radius: 12px; }
QLabel { background: transparent; }
QLabel#muted { color: #6f7788; font-size: 10px; }
QLabel#cap { color: #7d8598; font-size: 10px; font-weight: 700; letter-spacing: 1px; }

QSplitter::handle { background: #2c3040; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }

#midOps { background: #1e222c; border-left: 1px solid #2c3040; border-right: 1px solid #2c3040; }
#actionsBar { background: #1e222c; border-bottom: 1px solid #2c3040; spacing: 2px; padding: 2px 6px; }
#actionsBar QPushButton { background: transparent; border: 0; color: #aab1c2; font-size: 14px; }
#actionsBar QPushButton:hover { background: #262b38; border-radius: 6px; }
QStatusBar { background: #22252f; color: #8b93a5; border-top: 1px solid #2c3040; font-size: 10.5px; }
QStatusBar QLabel { color: #8b93a5; font-size: 10.5px; }

QPushButton {
    background: #262b38; color: #c6ccda;
    border: 1px solid #2c3040; border-radius: 7px;
    padding: 4px 10px; font-size: 11px;
}
QPushButton:hover { background: #2c3344; border-color: #394054; color: #e8eaf0; }
QPushButton:pressed { background: #1f2430; }
QPushButton:disabled { color: #4c5262; background: #22262f; border-color: #272b36; }
QPushButton[role="accent"] { background: #3b82f6; border-color: #3b82f6; color: #ffffff; font-weight: 600; }
QPushButton[role="accent"]:hover { background: #4b8ef8; }
QPushButton:flat, QPushButton[flat="true"] { background: transparent; border: 0; color: #8b93a5; padding: 3px 7px; }
QPushButton:flat:hover { color: #e8eaf0; background: #262b38; border-radius: 6px; }

QLineEdit, QComboBox, QSpinBox {
    background: #1a1d26; color: #c3c9d8;
    border: 1px solid #2c3040; border-radius: 7px;
    padding: 4px 8px; font-size: 11px;
    selection-background-color: #3b82f6;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #3b82f6; }
QComboBox::drop-down { border: 0; width: 18px; }
QComboBox QAbstractItemView {
    background: #242836; border: 1px solid #2c3040;
    selection-background-color: #263148; selection-color: #e8eaf0;
    outline: 0; font-size: 11px;
}

QTreeWidget, QTreeView, QListWidget {
    background: #1c1f27; border: 1px solid #2c3040; border-radius: 8px;
    outline: 0; font-size: 11.5px;
}
QTreeView::item, QListWidget::item { padding: 2px 4px; border-radius: 4px; }
QTreeView::item:selected, QListWidget::item:selected {
    background: #263148; color: #e8eaf0;
}
QTreeView::item:hover, QListWidget::item:hover { background: #232838; }
QHeaderView::section {
    background: #20242e; color: #8b93a5; border: 0;
    border-right: 1px solid #2c3040; border-bottom: 1px solid #2c3040;
    padding: 4px 6px; font-size: 10.5px; font-weight: 600;
}
QTableView {
    background: #1c1f27; border: 0; gridline-color: #232735;
    outline: 0; font-size: 11.5px;
    selection-background-color: #263148; selection-color: #e8eaf0;
}
QTableView::item { padding: 1px 4px; }
QTableView::item:selected { background: #263148; color: #e8eaf0; }
QTableView QTableCornerButton::section { background: #20242e; border: 0; }

/* 标签：圆角胶囊，统一宽度，长名省略号；文字色不写死，保留每标签自定义颜色 */
QTabBar::tab {
    background: #242836;
    border: 1px solid #2c3040;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
    border-bottom-left-radius: 6px; border-bottom-right-radius: 6px;
    padding: 4px 12px; font-size: 11px; margin-right: 4px; margin-top: 2px;
    min-width: 120px; max-width: 150px; text-align: left;
}
QTabBar::tab:hover { background: #2b3044; border-color: #3a4152; }
QTabBar::tab:selected { background: #31486e; border-color: #3d5a8a; color: #ffffff; }
QTabWidget::pane { border: 0; border-top: 1px solid #2c3040; }

QMenuBar { background: #1e222c; color: #aab1c2; border-bottom: 1px solid #2c3040; font-size: 11.5px; }
QMenuBar::item { background: transparent; padding: 4px 9px; border-radius: 5px; }
QMenuBar::item:selected { background: #263148; color: #ffffff; }
QMenu { background: #242836; border: 1px solid #353b4d; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 5px 22px 5px 12px; border-radius: 5px; font-size: 11.5px; }
QMenu::item:selected { background: #263148; color: #ffffff; }
QMenu::item:disabled { color: #4c5262; }
QMenu::separator { height: 1px; background: #2c3040; margin: 4px 6px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #2c3040; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #394054; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #2c3040; border-radius: 4px; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: #394054; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QProgressBar {
    background: #1a1d26; border: 1px solid #2c3040; border-radius: 6px;
    text-align: center; color: #aab1c2; font-size: 10px; height: 14px;
}
QProgressBar::chunk { background: #3b82f6; border-radius: 5px; }

QToolTip {
    background: #242836; color: #c6ccda; border: 1px solid #353b4d;
    border-radius: 6px; padding: 4px 8px; font-size: 10.5px;
}
FilePane { border: 1px solid transparent; }
FilePane[active="true"] { border: 2px solid #46608c; border-radius: 6px; background: rgba(90,130,190,0.06); }
QMessageBox { background: #20242e; }
QCheckBox, QRadioButton { background: transparent; color: #c6ccda; spacing: 5px; font-size: 11px; }
QToolButton#upStrip { background: #20242e; border: 0; color: #6f7788; font-size: 10px; padding: 0; }
QToolButton#upStrip:hover { background: #263148; color: #7cb0ff; }
QPlainTextEdit#viewerText {
    background: #181b22; border: 0; color: #c6ccda;
    font-family: Consolas, monospace; font-size: 11px;
}
QLabel#driveInfo { color: #aab1c2; font-size: 9.5px; padding: 0 6px; }
"""


THEMES = ("dark", "light")


def apply_theme(app, name="dark"):
    """给 QApplication 挂主题（dark / light）；可重复调用以切换。

    同时把主题名存到 app 属性上，供动态样式（如驱动器占用条）读取。
    """
    from PySide6.QtGui import QFont
    # 幂等：同一主题重复调用时跳过全量 setStyleSheet（大样式表约 30ms/次），
    # 避免启动阶段与运行时切换主题的无谓开销；图标缓存也不必重复清空。
    if app.property("theme") == name and app.styleSheet():
        return
    qss = QSS if name == "dark" else LIGHT_QSS
    app.setStyleSheet(qss.replace("__UI_FONT__", UI_FONT))
    app.setFont(QFont(UI_FONT, 9))
    app.setProperty("theme", name)
    # 线性图标随主题着色：切换后清缓存重绘
    try:
        from . import icons
        icons._CACHE.clear()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 浅色主题：米灰纸感，非纯白，与暗色同一套组件命名。
# ---------------------------------------------------------------------------
LIGHT_QSS = r"""
QWidget {
    background: #eef0f4;
    color: #2a2e3a;
    font-family: "__UI_FONT__";
    font-size: 12px;
}
QMainWindow { background: #eef0f4; }
QDialog { background: #f4f5f9; border: 1px solid #c9cdd8; border-radius: 12px; }
QLabel { background: transparent; }
QLabel#muted { color: #7a8091; font-size: 10px; }
QLabel#cap { color: #6a7080; font-size: 10px; font-weight: 700; letter-spacing: 1px; }

QSplitter::handle { background: #d5d9e2; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }

#midOps { background: #e3e6ee; border-left: 1px solid #c9cdd8; border-right: 1px solid #c9cdd8; }
#actionsBar { background: #e9ebf2; border-bottom: 1px solid #d5d9e2; spacing: 2px; padding: 2px 6px; }
#actionsBar QPushButton { background: transparent; border: 0; color: #4a4f5e; font-size: 14px; }
#actionsBar QPushButton:hover { background: #dde2ec; border-radius: 6px; }
QStatusBar { background: #e3e6ee; color: #6a7080; border-top: 1px solid #d5d9e2; font-size: 10.5px; }
QStatusBar QLabel { color: #6a7080; font-size: 10.5px; }

QPushButton {
    background: #e4e7ef; color: #3a3f4d;
    border: 1px solid #c9cdd8; border-radius: 7px;
    padding: 4px 10px; font-size: 11px;
}
QPushButton:hover { background: #dbe0ea; border-color: #aeb6c8; color: #1f2430; }
QPushButton:pressed { background: #d0d6e2; }
QPushButton:disabled { color: #a8aebc; background: #e8eaf0; border-color: #dcdee6; }
QPushButton[role="accent"] { background: #3b82f6; border-color: #3b82f6; color: #ffffff; font-weight: 600; }
QPushButton[role="accent"]:hover { background: #4b8ef8; }
QPushButton:flat, QPushButton[flat="true"] { background: transparent; border: 0; color: #6a7080; padding: 3px 7px; }
QPushButton:flat:hover { color: #1f2430; background: #dde1ea; border-radius: 6px; }

QLineEdit, QComboBox, QSpinBox {
    background: #ffffff; color: #2a2e3a;
    border: 1px solid #c9cdd8; border-radius: 7px;
    padding: 4px 8px; font-size: 11px;
    selection-background-color: #3b82f6; selection-color: #ffffff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #3b82f6; }
QComboBox::drop-down { border: 0; width: 18px; }
QComboBox QAbstractItemView {
    background: #ffffff; border: 1px solid #c9cdd8;
    selection-background-color: #e3ecfd; selection-color: #1f2430;
    outline: 0; font-size: 11px;
}

QTreeWidget, QTreeView, QListWidget {
    background: #f7f8fb; border: 1px solid #d5d9e2; border-radius: 8px;
    outline: 0; font-size: 11.5px;
}
QTreeView::item, QListWidget::item { padding: 2px 4px; border-radius: 4px; }
QTreeView::item:selected, QListWidget::item:selected { background: #d6e4fc; color: #1f2430; }
QTreeView::item:hover, QListWidget::item:hover { background: #eceff5; }
QHeaderView::section {
    background: #e9ebf2; color: #6a7080; border: 0;
    border-right: 1px solid #d5d9e2; border-bottom: 1px solid #d5d9e2;
    padding: 4px 6px; font-size: 10.5px; font-weight: 600;
}
QTableView {
    background: #f7f8fb; border: 0; gridline-color: #e6e8ef;
    outline: 0; font-size: 11.5px;
    selection-background-color: #d6e4fc; selection-color: #1f2430;
}
QTableView::item { padding: 1px 4px; }
QTableView QTableCornerButton::section { background: #e9ebf2; border: 0; }

QTabBar::tab {
    background: #e6e9f0;
    border: 1px solid #d0d5e0;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
    border-bottom-left-radius: 6px; border-bottom-right-radius: 6px;
    padding: 4px 12px; font-size: 11px; margin-right: 4px; margin-top: 2px;
    min-width: 120px; max-width: 150px; text-align: left;
}
QTabBar::tab:hover { background: #dde2ec; border-color: #b6bdd0; }
QTabBar::tab:selected { background: #ffffff; border-color: #b9c6dc; color: #1f2430; }
QTabWidget::pane { border: 0; border-top: 1px solid #d5d9e2; }

QMenuBar { background: #e9ebf2; color: #4a4f5e; border-bottom: 1px solid #d5d9e2; font-size: 11.5px; }
QMenuBar::item { background: transparent; padding: 4px 9px; border-radius: 5px; }
QMenuBar::item:selected { background: #d6e4fc; color: #1f2430; }
QMenu { background: #ffffff; border: 1px solid #c9cdd8; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 5px 22px 5px 12px; border-radius: 5px; font-size: 11.5px; }
QMenu::item:selected { background: #d6e4fc; color: #1f2430; }
QMenu::item:disabled { color: #a8aebc; }
QMenu::separator { height: 1px; background: #d5d9e2; margin: 4px 6px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #c3c9d6; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #a9b1c2; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #c3c9d6; border-radius: 4px; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: #a9b1c2; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QProgressBar {
    background: #e4e7ef; border: 1px solid #c9cdd8; border-radius: 6px;
    text-align: center; color: #6a7080; font-size: 10px; height: 14px;
}
QProgressBar::chunk { background: #3b82f6; border-radius: 5px; }

QToolTip {
    background: #ffffff; color: #3a3f4d; border: 1px solid #c9cdd8;
    border-radius: 6px; padding: 4px 8px; font-size: 10.5px;
}
FilePane { border: 1px solid transparent; }
FilePane[active="true"] { border: 2px solid #b9c6dc; border-radius: 6px; background: rgba(150,170,205,0.08); }
QMessageBox { background: #f4f5f9; }
QCheckBox, QRadioButton { background: transparent; color: #3a3f4d; spacing: 5px; font-size: 11px; }
QToolButton#upStrip { background: #dde2ec; border: 0; color: #6a7080; font-size: 10px; padding: 0; }
QToolButton#upStrip:hover { background: #d6e4fc; color: #1f2430; }
QPlainTextEdit#viewerText {
    background: #ffffff; border: 0; color: #2a2e3a;
    font-family: Consolas, monospace; font-size: 11px;
}
QLabel#driveInfo { color: #3a3f4d; font-size: 9.5px; padding: 0 6px; }
QTableView::item:selected { background: #d6e4fc; color: #1f2430; }
"""
