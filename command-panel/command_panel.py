# -*- coding: utf-8 -*-
"""本地单机命令面板：保存 shell / cmd / python 命令，按标签管理，一键运行并实时查看输出。

纯本地：无 server、无网络、无鉴权、无远程/多人能力。
配置与脚本同目录：Path(__file__).parent / "cfg" / "command_panel_commands.json"，与当前工作目录无关。
首次启动若旧位置 commands.json 尚存，会自动迁入 cfg/ 一次。
"""
import base64
import json
import os
import sys
import threading

# ---------------------------------------------------------------------------
# Windows 无黑框代理：必须在导入 PySide6 之前处理。代理进程若先加载 Qt，
# 启动/退出的延迟会表现为窗口一闪；这里只加载标准库，启动几乎瞬时。
# PySide6 未绑定 Qt 的进程创建参数修改接口，故用 pythonw.exe（GUI 子系统）
# 运行本脚本的代理模式，由代理用 CREATE_NO_WINDOW 启动真实命令并转发字节流。
# ---------------------------------------------------------------------------
RUNNER_FLAG = "--cmdpanel-runner"


def _assign_job(child):
    """给子进程挂 KILL_ON_JOB_CLOSE 的 Job：代理无论怎么死（含崩溃）子进程都陪葬。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes, byref, sizeof

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        if not kernel32.SetInformationJobObject(job, 9, byref(info), sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not kernel32.AssignProcessToJobObject(job, int(child._handle)):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _terminate_job(job):
    """显式终止 Job 内全部进程（含孙进程），而不是只杀直接子进程。"""
    if job is None:
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject(job, 1)
    except Exception:
        pass


def runner_main(payload):
    """隐藏窗口执行命令；收到中断字节或管道 EOF 都会杀掉整个进程树。"""
    import ctypes
    import subprocess

    try:
        spec = json.loads(base64.b64decode(payload).decode("utf-8"))
        child = subprocess.Popen(
            spec.get("cmdline") or spec["argv"],
            cwd=spec.get("cwd") or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        sys.stderr.write("启动失败：%s\n" % exc)
        sys.stderr.flush()
        os._exit(1)

    job = _assign_job(child)

    def watch_stop():
        try:
            sys.stdin.buffer.read(1)   # 收到中断字节或管道 EOF 都继续往下走
        except Exception:
            pass
        _terminate_job(job)            # 杀掉 cmd.exe 及其孙进程（如 ping.exe）
        if child.poll() is None:       # Job 不可用时退回只杀直接子进程
            child.kill()

    threading.Thread(target=watch_stop, daemon=True).start()

    def pump(src, dst):
        try:
            while True:
                chunk = os.read(src.fileno(), 8192)
                if not chunk:
                    break
                dst.write(chunk)
                dst.flush()
        except Exception:
            pass

    out_thread = threading.Thread(target=pump, args=(child.stdout, sys.stdout.buffer), daemon=True)
    err_thread = threading.Thread(target=pump, args=(child.stderr, sys.stderr.buffer), daemon=True)
    out_thread.start()
    err_thread.start()
    code = child.wait()
    out_thread.join(1)
    err_thread.join(1)
    sys.stdout.flush()
    sys.stderr.flush()
    if job is not None:
        try:
            ctypes.WinDLL("kernel32").CloseHandle(job)
        except Exception:
            pass
    os._exit(code if 0 <= code < 256 else 1)


if len(sys.argv) >= 3 and sys.argv[1] == RUNNER_FLAG:
    runner_main(sys.argv[2])   # 不导入 Qt、不创建 QApplication
    sys.exit(0)

# 以下仅图形界面模式需要；放在 runner 分支之后以缩短代理进程启动时间
import atexit
import locale
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QProcess, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QDialog, QFrame,
    QGraphicsDropShadowEffect, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog,
    QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSpinBox, QSplitter,
    QStackedWidget, QStatusBar, QTextEdit, QVBoxLayout, QWidget,
)

_SCRIPT_PATH = Path(__file__).resolve()
CFG_DIR = _SCRIPT_PATH.parent / "cfg"
CONFIG_PATH = CFG_DIR / "command_panel_commands.json"
LEGACY_CONFIG_PATH = _SCRIPT_PATH.parent / "commands.json"
# 日志按脚本文件名走：复制成 backup_panel.py 后自动写 logs/backup_panel.log，多副本互不混淆。
LOG_DIR = _SCRIPT_PATH.parent / "logs"
LOG_PATH = LOG_DIR / (_SCRIPT_PATH.stem + ".log")
LOG_MAX_BYTES = 10 * 1024 * 1024     # 超过 10MB 轮转为 .log.old（覆盖旧文件）
TYPES = ("shell", "cmd", "python")
PARALLEL_MODES = ("允许", "互斥")
OUTPUT_LIMIT = 1024 * 1024          # 单次运行输出缓冲上限 ~1MB，超过即截断
HISTORY_LIMIT = 60                  # 全局执行记录保留条数

# ---------------------------------------------------------------------------
# 所有视觉样式集中在此唯一 QSS 常量；其余代码不再调用 setStyleSheet。
# 配色参考用户提供的界面样式：底 #1c1f27 / 侧栏 #1e222c / 卡片 #242836 /
# 边框 #2c3040 / 主色 #3b82f6 / 成功 #34d399 / 失败 #f87171 / 警示 #fbbf24 /
# 备注琥珀左边条 #f59e0b。
# ---------------------------------------------------------------------------
# Qt 样式表的 font-family 不支持逗号分隔的回退链（会退化成 Sans Serif），
# 所以按平台选一个同时覆盖中文和西文的单一字体：Windows 用微软雅黑，macOS 用 SF Pro。
UI_FONT = "Microsoft YaHei UI" if os.name == "nt" else "SF Pro Text"

QSS = r"""
QWidget {
    background: #1c1f27;
    color: #e8eaf0;
    font-family: "__UI_FONT__";
    font-size: 12px;
}
QLabel { background: transparent; }
QLabel#pillText { background: transparent; color: #aab1c2; font-size: 10px; }
QPushButton[role="pill"][active="true"] QLabel#pillText { color: #ffffff; }
QMainWindow { background: #1c1f27; }
QDialog { background: #20242e; border: 1px solid #353b4d; border-radius: 12px; }
QWidget#dialogViewport { background: #20242e; }
QStatusBar { background: #22252f; color: #8b93a5; border-top: 1px solid #2c3040; font-size: 10.5px; }
QStatusBar QLabel { color: #8b93a5; }
QSplitter::handle { background: #2c3040; width: 1px; }

/* macOS 质感：扁平底色 + 极细边框 + 统一圆角，避免厚重渐变 */
QFrame#sidebar {
    background: #1e222c;
    border: 0; border-right: 1px solid #2c3040;
}
QFrame#card {
    background: #242836;
    border: 1px solid #2c3040; border-radius: 10px;
}
QFrame#card:hover { background: #292e3d; border: 1px solid #394054; }
QFrame#card[selected="true"] { background: #263148; border: 1px solid #3b82f6; }
QFrame#card[selected="true"]:hover { background: #2b3752; }
#miniFrame { background: transparent; border: 0; }

QLabel#alias { color: #f2f4fa; font-size: 12px; font-weight: 600; }
QLabel#muted { color: #6f7788; font-size: 10px; }
QLabel#sh3 { color: #6f7788; font-size: 9.5px; font-weight: 700; }
QLabel#cap { color: #6f7788; font-size: 10px; font-weight: 600; }
QLabel#field { color: #8b93a5; font-size: 10.5px; }
QLabel#statNum { color: #e8eaf0; font-size: 18px; font-weight: 600; }
QLabel#note {
    background: #2b2721; color: #d6c9a8; border-left: 3px solid #f59e0b;
    border-radius: 8px; padding: 6px 10px; font-size: 10.5px;
}
QLabel#noteTitle { color: #a3863c; font-size: 9px; font-weight: 700; }
QLabel[role="badge"] { border-radius: 99px; padding: 2px 9px; font-size: 9.5px; font-weight: 600; }
QLabel[role="badge"][status="running"] { background: #17305a; color: #7cb0ff; border: 1px solid #3b82f6; }
QLabel[role="badge"][status="success"] { background: #153a30; color: #34d399; border: 1px solid #34d399; }
QLabel[role="badge"][status="fail"] { background: #3a1f24; color: #f87171; border: 1px solid #f87171; }
QLabel[role="badge"][status="warn"] { background: #3a3018; color: #fbbf24; border: 1px solid #fbbf24; }
QLabel[role="type"] { background: #1f3050; color: #7cb0ff; border-radius: 99px; padding: 2px 9px; font-size: 9.5px; }
QLabel[role="tag"] { background: #282c38; color: #aab1c2; border-radius: 99px; padding: 2px 9px; font-size: 9.5px; }
QLabel[role="dot"] {
    background: #34d399; border-radius: 4px; min-width: 7px; max-width: 7px;
    min-height: 7px; max-height: 7px;
}
QLabel[role="rundot"] { background: transparent; color: #34d399; font-size: 8px; }

QPushButton {
    background: #2c3140; color: #aab1c2; border: 1px solid #3a3f4e;
    border-radius: 99px; padding: 5px 14px; font-size: 11px; font-weight: 600;
}
QPushButton:hover { background: #343a4b; color: #e8eaf0; }
QPushButton:pressed { background: #3b82f6; color: #ffffff; }
QPushButton:disabled { background: #242836; color: #5b6270; border-color: #2c3040; }
QPushButton[role="primary"] { background: #3b82f6; border: 1px solid #3b82f6; color: #ffffff; }
QPushButton[role="primary"]:hover { background: #5094ff; border-color: #5094ff; }
QPushButton[role="danger"] { background: #6b2830; border: 1px solid #8b3540; color: #fecaca; }
QPushButton[role="danger"]:hover { background: #84323c; }
QPushButton[role="pill"] {
    background: #282c38; color: #aab1c2; border: 1px solid #2c3040;
    border-radius: 99px; padding: 3px 11px; font-size: 10px; font-weight: 500;
}
QPushButton[role="pill"]:hover { background: #323749; }
QPushButton[role="pill"][active="true"] {
    background: #3b82f6; color: #ffffff; border-color: #3b82f6;
}
QPushButton[role="ghost"] {
    background: transparent; border: 1px dashed #3a3f4e; color: #6f7788;
    border-radius: 9px; padding: 6px; font-size: 11px; font-weight: 400;
}
QPushButton[role="ghost"]:hover { border-color: #3b82f6; color: #7cb0ff; }
QPushButton[role="mini"] {
    background: #2c3140; color: #aab1c2; border: 1px solid #3a3f4e;
    padding: 0; min-width: 18px; max-width: 18px; min-height: 18px; max-height: 18px; font-size: 10px;
}
QPushButton[role="link"] {
    background: transparent; color: #7cb0ff; border: 0; padding: 1px 5px; font-size: 10.5px; font-weight: 500;
}
QPushButton[role="link"]:hover { background: transparent; color: #a9cbff; }
QPushButton[role="link"]:disabled { background: transparent; color: #5b6270; border: 0; }

QLineEdit, QSpinBox, QPlainTextEdit, QTextEdit {
    background: #1b1e27; color: #e8eaf0; border: 1px solid #2c3040;
    border-radius: 7px; padding: 5px 9px; font-size: 11px; selection-background-color: #3b82f6;
}
QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus { border: 1px solid #3b82f6; }
QLineEdit#search {
    background: #282c38; border: 1px solid #313646; border-radius: 8px;
    padding: 6px 10px; color: #e8eaf0;
}
QLineEdit#search:focus { border: 1px solid #3b82f6; }
QPlainTextEdit#code, QTextEdit#output {
    background: #1a1d26; color: #c3c9d8; font-family: Consolas;
    font-size: 10.5px; border: 1px solid #2c3040; border-radius: 8px; padding: 7px 10px;
}
QTextEdit#output { min-height: 76px; }

QListWidget#cmdList { background: transparent; border: 0; padding: 0; }
QListWidget#cmdList::item { background: transparent; border: 0; }
QListWidget#cmdList::item:selected { background: transparent; }
QScrollArea { background: transparent; border: 0; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 0; border: 0; }
QScrollBar::handle:vertical { background: #41485c; border-radius: 3px; min-height: 22px; }
QScrollBar::handle:vertical:hover { background: #58617a; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 7px; }
QScrollBar::handle:horizontal { background: #41485c; border-radius: 3px; min-width: 22px; }
QScrollBar::handle:horizontal:hover { background: #58617a; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QRadioButton, QCheckBox { background: transparent; color: #c6ccda; spacing: 5px; font-size: 11px; }
QRadioButton::indicator, QCheckBox::indicator {
    width: 12px; height: 12px; border: 1px solid #3a3f4e; border-radius: 3px; background: #1b1e27;
}
QRadioButton::indicator { border-radius: 7px; }
QRadioButton::indicator:checked, QCheckBox::indicator:checked { background: #3b82f6; border: 1px solid #3b82f6; }
QGroupBox {
    background: #242836; border: 1px solid #2c3040; border-radius: 9px;
    margin-top: 10px; padding: 8px; color: #8b93a5;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QToolTip {
    background: #242836; color: #e8eaf0; border: 1px solid #3b82f6;
    border-radius: 7px; padding: 4px 7px; font-size: 11px;
}
QMessageBox { background: #20242e; border-radius: 11px; }
QMessageBox QPushButton { min-width: 72px; }
""".replace("__UI_FONT__", UI_FONT)


# ---------------------------------------------------------------------------
# 配置读写：逐字段容错 + 原子写
# ---------------------------------------------------------------------------
def default_commands():
    """内置 3 条示例命令，与 commands.json 内容一致；仅在文件缺失/损坏时回退。"""
    return [
        {"id": "b01", "alias": "磁盘占用", "type": "shell", "body": "df -h /",
         "tags": ["系统"], "note": "查看根分区占用", "cwd": "", "timeoutSec": 30, "parallel": "允许"},
        {"id": "b02", "alias": "VACUUM auto.db", "type": "cmd", "body": "node vacuum.js /auto.db",
         "tags": ["备份"], "note": "回收空闲页，压缩库体积", "cwd": "", "timeoutSec": 60, "parallel": "互斥"},
        {"id": "b03", "alias": "SSH·印度测试机", "type": "shell",
         "body": 'ssh qa@in-test-01 "tail -n 50 /var/log/app.log"',
         "tags": ["远程"], "note": "看测试机日志最后 50 行", "cwd": "", "timeoutSec": 30, "parallel": "允许"},
    ]


def load_config(path):
    """返回 (commands, warnings)。任何字段缺失/损坏都逐字段回退默认值，绝不抛异常给界面。"""
    warnings = []
    fallback = default_commands()
    if not path.exists():
        return fallback, warnings
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return fallback, ["JSON 解析失败：%s（已回退默认配置）" % exc]
    if not isinstance(raw, dict):
        return fallback, ["根节点不是对象（已回退默认配置）"]

    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        warnings.append("version 无效→1")

    src = raw.get("commands")
    if src is None:
        warnings.append("commands 缺失→预置 3 条示例")
        src = fallback
    elif not isinstance(src, list):
        warnings.append("commands 不是数组→预置 3 条示例")
        src = fallback

    out, used = [], set()
    for i, item in enumerate(src):
        where = "commands[%d]" % i
        if not isinstance(item, dict):
            warnings.append("%s 不是对象→默认命令" % where)
            item = {}
        cmd = {}

        cid = item.get("id")
        if not isinstance(cid, str) or not cid.strip():
            cid = "b%02d" % (i + 1)
            warnings.append("%s.id 缺失→%s" % (where, cid))
        if cid in used:
            n = 2
            while "%s_%d" % (cid, n) in used:
                n += 1
            warnings.append("%s.id 重复→%s_%d" % (where, cid, n))
            cid = "%s_%d" % (cid, n)
        used.add(cid)
        cmd["id"] = cid

        alias = item.get("alias")
        if not isinstance(alias, str) or not alias.strip():
            warnings.append("%s.alias 缺失→未命名命令" % where)
            alias = "未命名命令"
        cmd["alias"] = alias.strip()

        typ = item.get("type")
        if typ not in TYPES:
            warnings.append("%s.type 无效→shell" % where)
            typ = "shell"
        cmd["type"] = typ

        body = item.get("body")
        if not isinstance(body, str):
            warnings.append("%s.body 无效→空" % where)
            body = ""
        cmd["body"] = body

        tags = item.get("tags")
        if not isinstance(tags, list):
            warnings.append("%s.tags 无效→[]" % where)
            tags = []
        else:
            good = [t.strip() for t in tags if isinstance(t, str) and t.strip()]
            if len(good) != len(tags):
                warnings.append("%s.tags 含无效项→已过滤" % where)
            tags = good
        cmd["tags"] = list(dict.fromkeys(tags))

        note = item.get("note")
        if not isinstance(note, str):
            warnings.append("%s.note 无效→空" % where)
            note = ""
        cmd["note"] = note

        cwd = item.get("cwd")
        if not isinstance(cwd, str):
            warnings.append("%s.cwd 无效→空" % where)
            cwd = ""
        cmd["cwd"] = cwd

        # 0 或留空 = 不限时（ssh tail -f、长备份这类命令只能手动中断）；
        # 只有 >0 才在运行时启动超时定时器。缺失/非法值仍回退 30 并提示。
        if "timeoutSec" not in item:
            warnings.append("%s.timeoutSec 缺失→30（0/留空为不限时）" % where)
            timeout = 30
        else:
            raw = item["timeoutSec"]
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                timeout = 0
            else:
                try:
                    if isinstance(raw, bool):
                        raise ValueError
                    timeout = int(raw)
                    if timeout < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    warnings.append("%s.timeoutSec 无效→30（0/留空为不限时）" % where)
                    timeout = 30
        cmd["timeoutSec"] = timeout

        parallel = item.get("parallel")
        if parallel not in PARALLEL_MODES:
            warnings.append("%s.parallel 无效→允许" % where)
            parallel = "允许"
        cmd["parallel"] = parallel

        out.append(cmd)
    return out, warnings


def save_config(path, commands):
    """原子写：同目录临时文件 → fsync → os.replace，返回 None 表示成功。"""
    data = {"version": 1, "commands": commands}
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return None
    except Exception as exc:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return str(exc)


def migrate_config():
    """首次运行把根目录的 commands.json 一次性搬进 cfg/；返回给状态栏的提示（或空串）。

    只在「旧文件存在且新文件不存在」时搬移，绝不覆盖用户已经编辑过的新配置；
    两者并存时保留旧文件不动，避免误删。新位置缺失时也顺手建好预置示例文件。
    """
    note = ""
    try:
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        if LEGACY_CONFIG_PATH.exists() and not CONFIG_PATH.exists():
            os.replace(LEGACY_CONFIG_PATH, CONFIG_PATH)   # move：同盘符原子搬移
            note = "配置已迁移到 cfg/"
        if not CONFIG_PATH.exists():
            err = save_config(CONFIG_PATH, default_commands())
            if err:
                return "预置配置创建失败：%s" % err
    except Exception as exc:
        return "配置迁移失败：%s" % exc
    return note


def unique_id(commands):
    used = {c["id"] for c in commands}
    i = 1
    while "b%02d" % i in used:
        i += 1
    return "b%02d" % i


def fmt_elapsed(seconds):
    if seconds < 60:
        return "%.1fs" % seconds
    return "%02d:%02d" % (int(seconds) // 60, int(seconds) % 60)


# ---------------------------------------------------------------------------
# 执行日志：单文件追加，UTF-8；行级不 flush，收尾统一 flush。
# 崩溃时最多丢最后几行、无收尾块的残缺记录如实留痕，不做事后修补。
# ---------------------------------------------------------------------------
_LOG_LOCK = threading.RLock()
_LOG_FH = None


def _log_write(text, flush=False):
    """加锁追加日志。日志失败绝不影响命令执行与界面。"""
    global _LOG_FH
    with _LOG_LOCK:
        try:
            if _LOG_FH is None:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                _LOG_FH = open(LOG_PATH, "a", encoding="utf-8", newline="")
            _LOG_FH.write(text)
            if flush:
                _LOG_FH.flush()
        except Exception:
            pass


def rotate_log_if_needed():
    """启动时与每次收尾后各查一次；超限时先关句柄再轮转，下一次写入自动开新文件。"""
    global _LOG_FH
    with _LOG_LOCK:
        if _LOG_FH is not None:
            try:
                _LOG_FH.flush()
                _LOG_FH.close()
            except Exception:
                pass
            _LOG_FH = None
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                os.replace(LOG_PATH, LOG_PATH.parent / (LOG_PATH.name + ".old"))
                with open(LOG_PATH, "a", encoding="utf-8"):
                    pass               # 立即开空文件，便于用户/脚本看到轮转结果
        except OSError:
            pass


def close_log():
    """正常退出时落盘收尾；测试也可调用以释放句柄。"""
    global _LOG_FH
    with _LOG_LOCK:
        if _LOG_FH is not None:
            try:
                _LOG_FH.flush()
                _LOG_FH.close()
            except Exception:
                pass
            _LOG_FH = None


def open_log_file():
    """用系统默认编辑器打开日志；返回 None 表示成功，否则返回错误文本。"""
    path = str(LOG_PATH)
    try:
        if os.name == "nt":
            os.startfile(path)          # noqa: S606 - 本地文件，非外部输入
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return None
    except Exception as exc:
        return str(exc)


class OutputDecoder:
    """增量解码子进程输出：先试 UTF-8，失败回退系统本地编码（中文 Windows 为 GBK）。

    QProcess 按任意字节边界送达，多字节字符可能跨段；未解码的尾部暂存到
    下一块，避免一个「中」字被拆成两个替换字符。编码确定后优先复用，
    但每次仍先试 UTF-8，以兼容同一命令里混用两种编码的输出。
    """

    def __init__(self):
        self._pending = b""
        self._preferred = None

    def decode(self, data):
        raw = self._pending + data
        self._pending = b""
        candidates = ["utf-8"]
        local = locale.getpreferredencoding(False)
        for enc in (self._preferred, local, "mbcs" if os.name == "nt" else None):
            if enc and enc.lower() not in [c.lower() for c in candidates]:
                candidates.append(enc)
        for enc in candidates:
            try:
                text = raw.decode(enc)
                self._preferred = enc
                return text
            except UnicodeDecodeError as exc:
                # 仅「数据意外结束」属于跨段字符，保留尾部等下一块补齐
                if exc.end == len(raw) and exc.start > 0 and "unexpected end" in exc.reason:
                    self._pending = raw[exc.start:]
                    return ""
        return raw.decode(self._preferred or "utf-8", "replace")



def refresh_style(widget):
    """动态属性变更后强制 QSS 重新计算（不调用 setStyleSheet）。"""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def clear_layout(layout, reuse=False):
    """清空布局。必须先 hide 再摘除：可见控件被 setParent(None) 会短暂变成
    顶层窗口（标题默认为应用名 python），表现为点击运行时闪出两个 python 框。
    reuse=True 时只摘下保留（执行记录卡）；其余控件直接销毁。
    """
    if layout is None:
        return
    holder = layout.parent() if isinstance(layout.parent(), QWidget) else None
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            # 不能摘成无父对象：可见控件会瞬间成为顶层窗口（标题 python），
            # 停在布局所属控件上等待复用或销毁即可。
            widget.setParent(holder)
            if not reuse:
                widget.deleteLater()


def add_shadow(widget):
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(14)
    effect.setColor(QColor(0, 0, 0, 90))
    effect.setOffset(0, 2)
    widget.setGraphicsEffect(effect)


def system_font():
    """与 QSS 同一个字体族；Windows 下微软雅黑保证中文不是衬线体。"""
    families = QFontDatabase.families()
    name = next(
        (f for f in (UI_FONT, "Segoe UI Variable", "Segoe UI") if f in families),
        QFont().family(),
    )
    font = QFont(name)
    font.setPointSize(9)
    return font


# ---------------------------------------------------------------------------
# 左栏：命令卡片 / 标签 pill
# ---------------------------------------------------------------------------
class CommandCard(QFrame):
    clicked = Signal(str)
    edit_clicked = Signal(str)

    def __init__(self, cmd, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.cmd_id = cmd["id"]
        self.setCursor(Qt.PointingHandCursor)
        add_shadow(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 7, 10, 7)
        root.setSpacing(1)
        head = QHBoxLayout()
        head.setSpacing(5)
        self.alias_label = QLabel(cmd["alias"])
        self.alias_label.setObjectName("alias")
        self.alias_label.setWordWrap(True)
        self.dot = QLabel()
        self.dot.setProperty("role", "dot")
        self.dot.setVisible(False)
        holder = QWidget()
        holder.setObjectName("miniFrame")
        holder.setFixedSize(18, 18)
        hbox = QHBoxLayout(holder)
        hbox.setContentsMargins(0, 0, 0, 0)
        self.edit_btn = QPushButton("✎")
        self.edit_btn.setProperty("role", "mini")
        self.edit_btn.setToolTip("编辑命令")
        self.edit_btn.setVisible(False)
        self.edit_btn.clicked.connect(lambda: self.edit_clicked.emit(self.cmd_id))
        hbox.addWidget(self.edit_btn)
        head.addWidget(self.alias_label, 1)
        head.addWidget(self.dot)
        head.addWidget(holder)
        self.summary = QLabel(cmd["type"])
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        root.addLayout(head)
        root.addWidget(self.summary)

    def enterEvent(self, event):
        self.edit_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.edit_btn.setVisible(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self.clicked.emit(self.cmd_id)
        super().mousePressEvent(event)

    def set_selected(self, selected):
        self.setProperty("selected", "true" if selected else "false")
        refresh_style(self)

    def update_content(self, cmd, summary, running):
        self.alias_label.setText(cmd["alias"])
        self.summary.setText("%s · %s" % (cmd["type"], summary))
        self.dot.setVisible(running)


class TagPill(QPushButton):
    def __init__(self, text, count=None, running=False, active=False, parent=None):
        super().__init__(parent)
        self.setProperty("role", "pill")
        self.setProperty("active", "true" if active else "false")
        self.tag = text
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 3, 9, 3)
        layout.setSpacing(5)
        label = QLabel("%s  %s" % (text, count) if count is not None else text)
        label.setObjectName("pillText")
        layout.addWidget(label)
        if running:
            dot = QLabel("●")
            dot.setProperty("role", "rundot")
            layout.addWidget(dot)
        refresh_style(self)


# ---------------------------------------------------------------------------
# 执行记录卡片
# ---------------------------------------------------------------------------
class RunRecord(QFrame):
    again_clicked = Signal(str)
    interrupt_clicked = Signal()
    open_log_clicked = Signal()

    def __init__(self, cmd_id, alias, seq, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.cmd_id = cmd_id
        self.seq = seq
        self.alias = alias
        self.started_at = datetime.now()
        self.status_key = "running"
        self.buffers = {False: "", True: ""}   # stdout / stderr 分别缓冲，颜色不串
        self.interrupt_cb = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        head = QFrame()
        head.setObjectName("miniFrame")
        h = QHBoxLayout(head)
        h.setContentsMargins(10, 6, 10, 5)
        h.setSpacing(6)
        self.badge = QLabel("运行中")
        self.badge.setProperty("role", "badge")
        self.badge.setProperty("status", "running")
        title = QLabel("#%02d  %s" % (seq, alias))
        title.setObjectName("muted")
        self.dim = QLabel("0.0s")
        self.dim.setObjectName("muted")
        self.pid_label = QLabel("")
        self.pid_label.setObjectName("muted")
        self.kill_btn = QPushButton("■ 中断")
        self.kill_btn.setProperty("role", "danger")
        self.kill_btn.setCursor(Qt.PointingHandCursor)
        self.kill_btn.clicked.connect(self.interrupt_clicked.emit)
        self.again_btn = QPushButton("↻ 再次运行")
        self.again_btn.setProperty("role", "link")
        self.again_btn.setCursor(Qt.PointingHandCursor)
        self.again_btn.setEnabled(False)
        self.again_btn.clicked.connect(lambda: self.again_clicked.emit(self.cmd_id))
        self.log_btn = QPushButton("打开日志")
        self.log_btn.setProperty("role", "link")
        self.log_btn.setCursor(Qt.PointingHandCursor)
        self.log_btn.setVisible(False)      # 只有收尾后才出现
        self.log_btn.clicked.connect(self.open_log_clicked.emit)
        h.addWidget(self.badge)
        h.addWidget(title, 1)
        h.addWidget(self.pid_label)
        h.addWidget(self.dim)
        h.addWidget(self.kill_btn)
        h.addWidget(self.again_btn)
        h.addWidget(self.log_btn)
        self.output = QTextEdit()
        self.output.setObjectName("output")
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QTextEdit.WidgetWidth)
        root.addWidget(head)
        root.addWidget(self.output)
        add_shadow(self)

    def elapsed(self):
        return (datetime.now() - self.started_at).total_seconds()

    def refresh_elapsed(self):
        if self.status_key == "running":
            self.dim.setText(fmt_elapsed(self.elapsed()))

    def set_pid(self, pid):
        """运行记录在进程运行期间显示 QProcess PID。"""
        self.pid_label.setText("PID %d" % pid if pid else "")

    def set_status(self, key, text, detail=None):
        self.status_key = key
        self.badge.setText(text)
        self.badge.setProperty("status", key)
        refresh_style(self.badge)
        self.kill_btn.setVisible(key == "running")
        self.again_btn.setEnabled(key != "running")
        if detail is not None:
            self.dim.setText(detail)

    def show_log_link(self):
        """收尾后显示“打开日志”，用系统默认编辑器查看落盘日志。"""
        self.log_btn.setVisible(True)

    def append(self, text, is_error):
        """按行切分并保留半行缓冲，逐行加时间戳；stdout/stderr 分色。"""
        buf = self.buffers[is_error] + text
        parts = buf.split("\n")
        self.buffers[is_error] = parts.pop()
        for line in parts:
            self._append_line(line, is_error)

    def append_system(self, text):
        self._append_line(text, None, "#fbbf24")

    def flush(self):
        for is_error in (False, True):
            rest = self.buffers[is_error]
            if rest:
                self._append_line(rest, is_error)
                self.buffers[is_error] = ""

    def _append_line(self, line, is_error, color=None):
        ts = datetime.now().strftime("%H:%M:%S")
        if color is None:
            color = "#f87171" if is_error else "#c3c9d8"
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)
        cursor.insertText("[%s] %s\n" % (ts, line))
        self.output.setTextCursor(cursor)
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())


def python_exe():
    """始终用带控制台的 python.exe 跑脚本，保证 stdout/stderr 管道可用。

    代理自身用 pythonw.exe 启动（GUI 子系统，无黑框），但真实脚本要能往管道
    写输出；若 sys.executable 恰为 pythonw.exe 则换回同目录的 python.exe。
    """
    exe = Path(sys.executable)
    return str(exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe)


def prepare_script(cmd):
    """把命令体准备成整段脚本：shell 不落盘；cmd/python 写临时文件交给真实解释器。

    临时文件命名 %TEMP%/cmdpanel_<id>_<时间戳>.<bat|py>，UTF-8 无 BOM。
    实测 Windows cmd.exe 会把 bat 内的 UTF-8 中文字节原样透传到 stdout，
    面板 OutputDecoder 也优先按 UTF-8 解码，整条链路无乱码，因此 bat 保持
    UTF-8 而不转系统 ANSI（locale.getpreferredencoding 在 UTF-8 模式下会返回
    utf-8 而非 ANSI，更不能拿来当代码页）。
    崩溃残留不做守护清理：%TEMP% 目录自有清理机制。
    """
    typ = cmd["type"]
    if typ == "shell":
        return None
    ext = "bat" if typ == "cmd" else "py"
    safe_id = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in cmd["id"]) or "cmd"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(tempfile.gettempdir(), "cmdpanel_%s_%s.%s" % (safe_id, stamp, ext))
    # 统一 CRLF：.bat 的标准行尾；同时避免 body 已含 CRLF 时被文本模式双重转换成 \r\r\n。
    text = cmd["body"].replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


class RunState:
    def __init__(self, cmd, record, parent):
        self.cmd = dict(cmd)                 # 快照：删除命令不影响已运行任务
        self.record = record
        self.script_path = None              # cmd/python 临时脚本；shell 为 None
        self.proc = QProcess(parent)
        self.done = False
        self.stop_reason = None              # interrupted / timeout
        self.total_bytes = 0
        self.truncated = False
        self.decoder = OutputDecoder()
        self.log_buffers = {False: "", True: ""}   # 日志也按行切分，收尾时补齐末行
        self.using_runner = False
        self.timeout_timer = None
        self.kill_timer = None
        self.elapsed_timer = None


# ---------------------------------------------------------------------------
# 编辑对话框（QDialog 模态，Esc 默认关闭）
# ---------------------------------------------------------------------------
class CommandDialog(QDialog):
    def __init__(self, parent, command, all_tags, is_new=False):
        super().__init__(parent)
        self.command = dict(command)
        self.is_new = is_new
        self.action = None
        self.result_command = None
        self.extra_tags = []
        self.setModal(True)
        self.setWindowTitle("新建命令" if is_new else "编辑命令 · %s" % command["alias"])
        self.resize(580, 660)

        # 表单内容较长，放进可滚动区域，小屏也不会裁掉底部按钮
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.viewport().setObjectName("dialogViewport")
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        root = QVBoxLayout(content)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(7)

        self.alias_edit = QLineEdit(command["alias"])
        self.alias_edit.setPlaceholderText("别名（必填）")
        root.addLayout(self._row("别名 *", self.alias_edit))

        type_row = QHBoxLayout()
        type_row.setSpacing(14)
        self.type_group = QButtonGroup(self)
        for typ in TYPES:
            btn = QRadioButton(typ)
            btn.setChecked(typ == command["type"])
            self.type_group.addButton(btn)
            type_row.addWidget(btn)
        type_row.addStretch(1)
        root.addLayout(self._row("类型", type_row))

        self.body_edit = QPlainTextEdit(command["body"])
        self.body_edit.setObjectName("code")
        self.body_edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.body_edit.setMinimumHeight(120)
        root.addLayout(self._row("命令体", self.body_edit))

        self.cwd_edit = QLineEdit(command["cwd"])
        self.cwd_edit.setPlaceholderText("留空则使用启动目录")
        root.addLayout(self._row("工作目录", self.cwd_edit))

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(0, 86400)          # 0 = 不限时
        self.timeout_spin.setValue(command["timeoutSec"])
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setSpecialValueText("不限时")
        self.timeout_spin.setToolTip("0 或留空表示不限时，命令只能手动中断")
        root.addLayout(self._row("超时", self.timeout_spin))

        parallel_row = QHBoxLayout()
        parallel_row.setSpacing(14)
        self.parallel_group = QButtonGroup(self)
        for mode in PARALLEL_MODES:
            btn = QRadioButton(mode)
            btn.setChecked(mode == command["parallel"])
            self.parallel_group.addButton(btn)
            parallel_row.addWidget(btn)
        parallel_row.addStretch(1)
        root.addLayout(self._row("并行", parallel_row))

        tags_box = QGroupBox("标签")
        tags_layout = QVBoxLayout(tags_box)
        self.tags_grid = QGridLayout()
        self.tag_checkboxes = {}
        all_tags = sorted(set(all_tags) | set(command["tags"]))
        for idx, tag in enumerate(all_tags):
            cb = QCheckBox(tag)
            cb.setChecked(tag in command["tags"])
            self.tag_checkboxes[tag] = cb
            self.tags_grid.addWidget(cb, idx // 3, idx % 3)
        tags_layout.addLayout(self.tags_grid)
        new_tag_row = QHBoxLayout()
        self.new_tag_edit = QLineEdit()
        self.new_tag_edit.setPlaceholderText("新标签名")
        new_tag_btn = QPushButton("＋ 新建标签")
        new_tag_btn.setCursor(Qt.PointingHandCursor)
        new_tag_btn.clicked.connect(self._add_tag)
        new_tag_row.addWidget(self.new_tag_edit, 1)
        new_tag_row.addWidget(new_tag_btn)
        tags_layout.addLayout(new_tag_row)
        root.addWidget(tags_box)

        self.note_edit = QPlainTextEdit(command["note"])
        self.note_edit.setMinimumHeight(56)
        self.note_edit.setPlaceholderText("备注（可选）")
        root.addLayout(self._row("备注", self.note_edit))

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        run_btn = QPushButton("▶ 运行（保存并立即执行）")
        run_btn.setProperty("role", "primary")
        run_btn.clicked.connect(self._on_run)
        save_btn = QPushButton("保存并关闭")
        save_btn.clicked.connect(self._on_save)
        delete_btn = QPushButton("删除…")
        delete_btn.setProperty("role", "danger")
        delete_btn.setVisible(not is_new)
        delete_btn.clicked.connect(self._on_delete)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        for btn in (run_btn, save_btn, delete_btn, cancel_btn):
            btn.setCursor(Qt.PointingHandCursor)
            buttons.addWidget(btn)
        root.addLayout(buttons)

    @staticmethod
    def _row(title, control):
        """表单行：右侧既可以是控件，也可以是单选按钮所在的 QHBoxLayout。"""
        row = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("field")
        label.setFixedWidth(74)
        label.setAlignment(Qt.AlignTop)
        row.addWidget(label)
        if isinstance(control, QLayout):
            row.addLayout(control, 1)
        else:
            row.addWidget(control, 1)
        return row

    def _add_tag(self):
        name = self.new_tag_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "新建标签", "请输入标签名。")
            return
        if name not in self.tag_checkboxes:
            cb = QCheckBox(name)
            cb.setChecked(True)
            self.tag_checkboxes[name] = cb
            self.extra_tags.append(name)
            idx = len(self.tag_checkboxes) - 1
            self.tags_grid.addWidget(cb, idx // 3, idx % 3)
        else:
            self.tag_checkboxes[name].setChecked(True)
        self.new_tag_edit.clear()

    def _collect(self):
        typ = self.type_group.checkedButton().text()
        parallel = self.parallel_group.checkedButton().text()
        return {
            "id": self.command["id"],
            "alias": self.alias_edit.text().strip(),
            "type": typ,
            "body": self.body_edit.toPlainText(),
            "tags": [t for t, cb in self.tag_checkboxes.items() if cb.isChecked()],
            "note": self.note_edit.toPlainText(),
            "cwd": self.cwd_edit.text().strip(),
            "timeoutSec": self.timeout_spin.value(),
            "parallel": parallel,
        }

    def _validate(self):
        if not self.alias_edit.text().strip():
            QMessageBox.warning(self, "缺少别名", "别名不能为空。")
            self.alias_edit.setFocus()
            return False
        return True

    def _on_run(self):
        if self._validate():
            self.result_command = self._collect()
            self.action = "run"
            self.accept()

    def _on_save(self):
        if self._validate():
            self.result_command = self._collect()
            self.action = "save"
            self.accept()

    def _on_delete(self):
        answer = QMessageBox.question(
            self, "删除命令",
            "确认删除「%s」？\n已启动的运行任务不受影响。" % self.command["alias"],
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.result_command = dict(self.command)
            self.action = "delete"
            self.accept()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class CommandPanel(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("命令面板")
        self.resize(1060, 720)
        self.setMinimumSize(800, 540)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        migration_note = migrate_config()
        self.commands, warnings = load_config(CONFIG_PATH)
        self.active_by_id = {}      # cmd_id -> set(RunState)，同一命令允许多实例（允许模式）
        self.records = []           # 最新在前
        self.record_seq = 0
        self.last_result = {}
        self.selected_id = None
        self.active_tag = None
        self.search_text = ""
        self.card_widgets = {}
        self.item_ids = {}

        self._build_ui()
        self._refresh_all()
        if warnings:
            preview = "；".join(warnings[:2]) + ("…" if len(warnings) > 2 else "")
            self._status("配置容错加载：%d 处回退（%s）" % (len(warnings), preview), 15000)
        if migration_note:
            self._status(migration_note, 15000)   # 迁移提示优先级更高，覆盖容错提示

    def _status(self, text, ms=4000):
        self.status_bar.showMessage(text, ms)

    # ---------------- 执行日志 ----------------
    @staticmethod
    def _log_alias(state):
        return state.cmd["alias"].replace("\r", " ").replace("\n", " ")

    def _log_start(self, state):
        head = "━━━ %s · %s · %s · 启动 ━━━\n" % (
            state.record.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            self._log_alias(state), state.cmd["type"],
        )
        _log_write(head)

    def _log_line(self, state, line, is_error=False):
        # 单文件无颜色，stderr 用文字标记；时间戳与界面同风格。
        _log_write("[%s] %s%s\n" % (
            datetime.now().strftime("%H:%M:%S"),
            "[err]  " if is_error else "", line.rstrip("\r"),
        ))

    def _log_output(self, state, text, is_error):
        buf = state.log_buffers[is_error] + text
        parts = buf.split("\n")
        state.log_buffers[is_error] = parts.pop()
        for line in parts:
            self._log_line(state, line, is_error)

    def _flush_log_lines(self, state):
        for is_error in (False, True):
            rest = state.log_buffers[is_error]
            if rest:
                self._log_line(state, rest, is_error)
                state.log_buffers[is_error] = ""

    def _log_note(self, state, text):
        self._log_line(state, text)

    def _log_finish(self, state, text, key, elapsed):
        label = "退出码 0" if key == "success" else text
        tail = "━━━ %s · 结束 · %s · 耗时 %s ━━━\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), label, fmt_elapsed(elapsed),
        )
        _log_write(tail, flush=True)
        rotate_log_if_needed()   # 不打断输出流：flush 后才静默轮转

    def _open_log(self):
        error = open_log_file()
        self._status(error or ("已打开日志：%s" % LOG_PATH))

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 保留系统标题栏，不再画任何假窗控圆点或 mockup 文案，避免双层标题栏
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        outer.addWidget(splitter, 1)

        # ---- 左栏 ----
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(10, 10, 10, 10)
        side_layout.setSpacing(6)

        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("🔍 搜索命令（别名 / 备注）…")
        self.search.textChanged.connect(self._on_search)
        side_layout.addWidget(self.search)

        tag_caption = QLabel("标签")
        tag_caption.setObjectName("sh3")
        side_layout.addWidget(tag_caption)
        tag_host = QWidget()
        self.tag_layout = QHBoxLayout(tag_host)
        self.tag_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_layout.setSpacing(7)
        self.tag_layout.addStretch(1)
        side_layout.addWidget(tag_host)

        cmd_caption = QLabel("命令")
        cmd_caption.setObjectName("sh3")
        side_layout.addWidget(cmd_caption)
        self.cmd_list = QListWidget()
        self.cmd_list.setObjectName("cmdList")
        self.cmd_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.cmd_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.cmd_list.itemPressed.connect(self._on_item_pressed)
        side_layout.addWidget(self.cmd_list, 1)

        new_btn = QPushButton("＋ 新建命令")
        new_btn.setProperty("role", "ghost")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.clicked.connect(lambda: self.open_edit_dialog(None))
        side_layout.addWidget(new_btn)
        splitter.addWidget(sidebar)

        # ---- 右栏：统计页 ----
        self.stats_page = QWidget()
        stats_layout = QVBoxLayout(self.stats_page)
        stats_layout.setContentsMargins(12, 12, 12, 12)
        stats_layout.setSpacing(8)
        stats_card = QFrame()
        stats_card.setObjectName("card")
        stat_grid = QGridLayout(stats_card)
        stat_grid.setContentsMargins(14, 11, 14, 11)
        self.stat_labels = {}
        for idx, (key, label) in enumerate((("count", "命令数"), ("running", "运行中"),
                                            ("success", "近期成功"), ("tags", "标签数"))):
            num = QLabel("0")
            num.setObjectName("statNum")
            cap = QLabel(label)
            cap.setObjectName("muted")
            stat_grid.addWidget(num, 0, idx)
            stat_grid.addWidget(cap, 1, idx)
            self.stat_labels[key] = num
        stats_layout.addWidget(stats_card)
        stats_cap = QLabel("最近运行")
        stats_cap.setObjectName("cap")
        stats_layout.addWidget(stats_cap)
        self.global_records_host = QWidget()
        self.global_records_layout = QVBoxLayout(self.global_records_host)
        self.global_records_layout.setContentsMargins(0, 0, 0, 0)
        self.global_records_layout.setSpacing(8)
        self.global_records_layout.addStretch(1)
        stats_layout.addWidget(self.global_records_host, 1)
        add_shadow(stats_card)

        self.stats_scroll = QScrollArea()
        self.stats_scroll.setWidgetResizable(True)
        self.stats_scroll.setWidget(self.stats_page)

        # ---- 右栏：详情页 ----
        self.detail_page = QWidget()
        detail_layout = QVBoxLayout(self.detail_page)
        detail_layout.setContentsMargins(12, 12, 12, 12)
        detail_layout.setSpacing(8)

        self.detail_card = QFrame()
        self.detail_card.setObjectName("card")
        add_shadow(self.detail_card)
        dc = QVBoxLayout(self.detail_card)
        dc.setContentsMargins(12, 10, 12, 12)
        dc.setSpacing(7)
        header = QHBoxLayout()
        header.setSpacing(8)
        self.detail_alias = QLabel("")
        self.detail_alias.setObjectName("alias")
        self.detail_badges = QHBoxLayout()
        self.detail_badges.setSpacing(6)
        header.addWidget(self.detail_alias)
        header.addLayout(self.detail_badges)
        header.addStretch(1)
        self.run_btn = QPushButton("▶ 运行")
        self.run_btn.setProperty("role", "primary")
        self.run_btn.setCursor(Qt.PointingHandCursor)
        self.run_btn.clicked.connect(self._on_primary)
        self.blocked_btn = QPushButton("▶ 运行")
        self.blocked_btn.setProperty("role", "ghost")
        self.blocked_btn.setEnabled(False)
        self.blocked_btn.setToolTip("互斥：已有运行中的实例")
        self.blocked_btn.setVisible(False)
        self.edit_btn = QPushButton("✎ 编辑")
        self.edit_btn.setCursor(Qt.PointingHandCursor)
        self.edit_btn.clicked.connect(lambda: self.open_edit_dialog(self.selected_id))
        header.addWidget(self.run_btn)
        header.addWidget(self.blocked_btn)
        header.addWidget(self.edit_btn)
        dc.addLayout(header)
        self.detail_meta = QLabel("")
        self.detail_meta.setObjectName("muted")
        dc.addWidget(self.detail_meta)
        self.note_label = QLabel("")
        self.note_label.setObjectName("note")
        self.note_label.setWordWrap(True)
        self.note_label.setVisible(False)
        dc.addWidget(self.note_label)
        self.code_view = QPlainTextEdit()
        self.code_view.setObjectName("code")
        self.code_view.setReadOnly(True)
        self.code_view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        dc.addWidget(self.code_view)
        detail_layout.addWidget(self.detail_card)

        records_cap = QLabel("执行记录")
        records_cap.setObjectName("cap")
        detail_layout.addWidget(records_cap)
        self.detail_records_host = QWidget()
        self.detail_records_layout = QVBoxLayout(self.detail_records_host)
        self.detail_records_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_records_layout.setSpacing(8)
        self.detail_records_layout.addStretch(1)
        detail_layout.addWidget(self.detail_records_host, 1)

        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setWidget(self.detail_page)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.stats_scroll)
        self.stack.addWidget(self.detail_scroll)
        splitter.addWidget(self.stack)
        splitter.setSizes([260, 860])

    # ---------------- 界面刷新 ----------------
    def _find_command(self, cid):
        for cmd in self.commands:
            if cmd["id"] == cid:
                return cmd
        return None

    def _all_tags(self):
        tags = set()
        for cmd in self.commands:
            tags.update(cmd["tags"])
        return tags

    def is_running(self, cid):
        return bool(self.active_by_id.get(cid))

    def _running_count(self):
        return sum(len(states) for states in self.active_by_id.values())

    def _filtered_commands(self):
        q = self.search_text.strip().lower()
        result = []
        for cmd in self.commands:
            if self.active_tag is not None and self.active_tag not in cmd["tags"]:
                continue
            if q and q not in cmd["alias"].lower() and q not in cmd["note"].lower():
                continue
            result.append(cmd)
        return result

    def _on_search(self, text):
        self.search_text = text
        self._refresh_left()

    def _on_item_pressed(self, item):
        cid = self.item_ids.get(id(item))
        if cid:
            self.select_command(cid)

    def _refresh_all(self):
        self._refresh_tag_pills()
        self._refresh_left()
        self._refresh_detail()
        self._refresh_stats()
        self._refresh_records_view()

    def _refresh_tag_pills(self):
        clear_layout(self.tag_layout)
        self.tag_layout.addStretch(1)
        tags = ["全部"] + sorted(self._all_tags())
        for tag in tags:
            count = len(self.commands) if tag == "全部" else sum(1 for c in self.commands if tag in c["tags"])
            running = (self._running_count() > 0) if tag == "全部" else any(
                self.is_running(c["id"]) for c in self.commands if tag in c["tags"]
            )
            active = self.active_tag is None if tag == "全部" else self.active_tag == tag
            pill = TagPill(tag, count, running, active)
            pill.setCursor(Qt.PointingHandCursor)
            pill.clicked.connect(lambda checked=False, t=tag: self._on_tag_clicked(t))
            self.tag_layout.addWidget(pill)
        add_pill = TagPill("＋新建", None, False, False)
        add_pill.setCursor(Qt.PointingHandCursor)
        add_pill.setToolTip("新建标签（保存命令后写入 commands.json）")
        add_pill.clicked.connect(self._on_new_tag)
        self.tag_layout.addWidget(add_pill)

    def _on_tag_clicked(self, tag):
        self.active_tag = None if tag == "全部" else tag
        self._refresh_tag_pills()
        self._refresh_left()

    def _on_new_tag(self):
        name, ok = QInputDialog.getText(self, "新建标签", "标签名称：")
        if not ok or not name.strip():
            return
        self.open_edit_dialog(None, preset_tag=name.strip())

    def _refresh_left(self):
        pos = self.cmd_list.verticalScrollBar().value()
        for i in range(self.cmd_list.count()):
            widget = self.cmd_list.itemWidget(self.cmd_list.item(i))
            if widget is not None:
                # 可见卡片直接 setParent(None) 会瞬间变成顶层窗口（标题 python），
                # 点击运行时会闪框；必须先隐藏再摘除。
                widget.hide()
                widget.setParent(self.cmd_list.viewport())
                widget.deleteLater()
        self.cmd_list.clear()
        self.card_widgets.clear()
        self.item_ids.clear()
        for cmd in self._filtered_commands():
            result = self.last_result.get(cmd["id"], {})
            summary = "上次 " + result.get("text", "未运行")
            if result.get("elapsed") is not None:
                summary += " · " + fmt_elapsed(result["elapsed"])
            card = CommandCard(cmd)
            card.update_content(cmd, summary, self.is_running(cmd["id"]))
            card.set_selected(cmd["id"] == self.selected_id)
            card.clicked.connect(self.select_command)
            card.edit_clicked.connect(self.open_edit_dialog)
            item = QListWidgetItem()
            item.setSizeHint(QSize(10, 58))
            self.cmd_list.addItem(item)
            self.cmd_list.setItemWidget(item, card)
            self.card_widgets[cmd["id"]] = card
            self.item_ids[id(item)] = cmd["id"]
            if cmd["id"] == self.selected_id:
                self.cmd_list.setCurrentItem(item)
        self.cmd_list.verticalScrollBar().setValue(pos)

    def select_command(self, cid):
        if self.selected_id == cid:
            return
        self.selected_id = cid
        for key, card in self.card_widgets.items():
            card.set_selected(key == cid)
        self._refresh_detail()
        self._refresh_records_view()

    def _refresh_detail(self):
        cmd = self._find_command(self.selected_id)
        if cmd is None:
            self.selected_id = None
            self.stack.setCurrentWidget(self.stats_scroll)
            return
        self.stack.setCurrentWidget(self.detail_scroll)
        self.detail_alias.setText(cmd["alias"])
        clear_layout(self.detail_badges)
        type_badge = QLabel(cmd["type"])
        type_badge.setProperty("role", "type")
        self.detail_badges.addWidget(type_badge)
        for tag in cmd["tags"]:
            badge = QLabel(tag)
            badge.setProperty("role", "tag")
            self.detail_badges.addWidget(badge)
        self.detail_badges.addStretch(1)
        cwd = cmd["cwd"] or "—"
        timeout_text = "不限时" if cmd["timeoutSec"] <= 0 else "%ss" % cmd["timeoutSec"]
        self.detail_meta.setText("工作目录：%s    ·    超时：%s    ·    并行：%s" %
                                 (cwd, timeout_text, cmd["parallel"]))
        note = cmd["note"].strip()
        self.note_label.setVisible(bool(note))
        if note:
            self.note_label.setText("<b>备注</b><br>%s" % note.replace("&", "&amp;").replace("<", "&lt;")
                                    .replace(">", "&gt;").replace("\n", "<br>"))
        self.code_view.setPlainText(cmd["body"])
        self._refresh_detail_buttons()

    def _refresh_detail_buttons(self):
        cmd = self._find_command(self.selected_id)
        if cmd is None:
            return
        running = self.is_running(cmd["id"])
        self.run_btn.setText("■ 中断" if running else "▶ 运行")
        self.run_btn.setProperty("role", "danger" if running else "primary")
        refresh_style(self.run_btn)
        self.run_btn.setEnabled(True)
        self.blocked_btn.setVisible(running and cmd["parallel"] == "互斥")

    def _refresh_stats(self):
        self.stat_labels["count"].setText(str(len(self.commands)))
        self.stat_labels["running"].setText(str(self._running_count()))
        self.stat_labels["success"].setText(str(sum(
            1 for r in self.records[:30] if r.status_key == "success"
        )))
        self.stat_labels["tags"].setText(str(len(self._all_tags())))

    def _refresh_records_view(self):
        # 记录控件跨“统计页 / 详情页”复用：只摘下、不销毁
        clear_layout(self.detail_records_layout, reuse=True)
        clear_layout(self.global_records_layout, reuse=True)
        detail = self._find_command(self.selected_id)
        layout = self.detail_records_layout if detail else self.global_records_layout
        source = [r for r in self.records if detail and r.cmd_id == detail["id"]] if detail else self.records[:15]
        for rec in source:
            layout.addWidget(rec)
            rec.show()   # clear_layout 里显式 hide 过，重新挂回时要恢复显示
        layout.addStretch(1)

    # ---------------- 编辑 / 保存 ----------------
    def _persist(self):
        error = save_config(CONFIG_PATH, self.commands)
        if error:
            self._status("写回 commands.json 失败：%s" % error, 8000)
            return False
        return True

    def open_edit_dialog(self, cid=None, preset_tag=None):
        is_new = cid is None
        if is_new:
            command = {"id": unique_id(self.commands), "alias": "", "type": "shell", "body": "",
                       "tags": [preset_tag] if preset_tag else [], "note": "", "cwd": "",
                       "timeoutSec": 30, "parallel": "允许"}
        else:
            command = self._find_command(cid)
            if command is None:
                self._status("命令已不存在")
                return
        dialog = CommandDialog(self, command, self._all_tags(), is_new)
        if dialog.exec() != QDialog.Accepted:
            return
        if dialog.action == "delete":
            alias = command["alias"]
            self.commands = [c for c in self.commands if c["id"] != command["id"]]
            if self.selected_id == command["id"]:
                self.selected_id = None
            self._persist()
            self._refresh_all()
            self._status("已删除：%s（已运行任务不受影响）" % alias)
            return

        new_command = dialog.result_command
        if is_new:
            self.commands.append(new_command)
            self.selected_id = new_command["id"]
        else:
            for idx, existing in enumerate(self.commands):
                if existing["id"] == new_command["id"]:
                    self.commands[idx] = new_command
                    break
        if not self._persist():
            return
        self._refresh_all()
        self._status("已保存：%s" % new_command["alias"])
        if dialog.action == "run":
            self.run_command(new_command["id"])

    # ---------------- 进程执行 ----------------
    def run_command(self, cid):
        cmd = self._find_command(cid)
        if cmd is None:
            self._status("命令不存在：%s" % cid)
            return
        if self.is_running(cid):
            self._status("「%s」已有运行中的实例（%s），不能重复启动" %
                         (cmd["alias"], cmd["parallel"]))
            return
        if not cmd["body"].strip():
            self._status("命令体为空：%s" % cmd["alias"])
            return

        self.record_seq += 1
        rec = RunRecord(cid, cmd["alias"], self.record_seq, self)
        self.records.insert(0, rec)
        self._trim_records()
        state = RunState(cmd, rec, self)
        rec.again_clicked.connect(lambda target=cid: self.run_command(target))
        rec.interrupt_clicked.connect(lambda s=state: self._stop(s, "interrupted"))
        rec.open_log_clicked.connect(self._open_log)
        self.active_by_id.setdefault(cid, set()).add(state)

        state.proc.readyReadStandardOutput.connect(lambda s=state: self._read(s, False))
        state.proc.readyReadStandardError.connect(lambda s=state: self._read(s, True))
        state.proc.finished.connect(lambda code, status, s=state: self._on_finished(s, code))
        if hasattr(state.proc, "errorOccurred"):
            state.proc.errorOccurred.connect(lambda err, s=state: self._on_error(s, err))
        state.proc.started.connect(lambda s=state: s.record.set_pid(s.proc.processId()))

        elapsed_timer = QTimer(self)
        elapsed_timer.setInterval(1000)
        elapsed_timer.timeout.connect(rec.refresh_elapsed)
        elapsed_timer.start()
        state.elapsed_timer = elapsed_timer

        # 0（或配置里留空）= 不限时：不创建定时器，命令一直跑到完成或手动中断
        timeout_sec = cmd["timeoutSec"]
        if timeout_sec > 0:
            timeout_timer = QTimer(self)
            timeout_timer.setSingleShot(True)
            timeout_timer.setInterval(timeout_sec * 1000)
            timeout_timer.timeout.connect(lambda s=state: self._stop(s, "timeout"))
            timeout_timer.start()
            state.timeout_timer = timeout_timer

        self._log_start(state)   # 每次运行开头写块头，崩溃残留的残块不修补
        self._start_line(state)
        self._refresh_left()
        self._refresh_tag_pills()
        self._refresh_detail_buttons()
        self._refresh_stats()
        self._refresh_records_view()
        self._status("运行中：%s（PID %s）" % (cmd["alias"], state.proc.processId() or "启动中"))
        if cmd["parallel"] == "互斥":
            self._status("「%s」互斥运行中：重复运行按钮已置灰" % cmd["alias"], 6000)

    def _start_line(self, state):
        """一次性整段启动：三种类型都在真实解释器里跑完整脚本，行间状态保留。"""
        if state.done:
            return
        typ = state.cmd["type"]
        proc = state.proc
        cwd = state.cmd["cwd"].strip()
        effective_cwd = ""
        if cwd:
            if Path(cwd).is_dir():
                effective_cwd = cwd
                proc.setWorkingDirectory(cwd)
            else:
                self._status("工作目录不存在，已忽略：%s" % cwd)

        # 整段脚本语义：shell 直接把 body 原文交给 bash/sh -c；cmd/python 先落
        # 临时脚本再交给真实解释器。cd / 变量 / 缩进块因此与终端粘贴执行一致。
        if typ != "shell":
            try:
                state.script_path = prepare_script(state.cmd)
            except Exception as exc:
                self._feed(state, ("写入临时脚本失败：%s\n" % exc).encode("utf-8"), True)
                state.record.flush()
                self._finish(state, "fail", "写入脚本失败", str(exc))
                return

        body = state.cmd["body"]
        script = state.script_path
        if os.name == "nt":
            spec = {"cwd": effective_cwd}
            if typ == "cmd":
                spec["cmdline"] = 'cmd.exe /c "%s"' % script   # 路径加引号，兼容空格
            elif typ == "shell":
                spec["argv"] = [shutil.which("bash") or shutil.which("sh") or "sh", "-c", body]
            else:
                spec["argv"] = [python_exe(), script]
            payload = base64.b64encode(
                json.dumps(spec, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            if pythonw.exists():
                state.using_runner = True
                proc.start(str(pythonw), [__file__, RUNNER_FLAG, payload])
            else:  # 没有 pythonw 时退回原方式，可能短暂显示控制台
                self._start_direct(state, typ)
        else:
            self._start_direct(state, typ)

    @staticmethod
    def _start_direct(state, typ):
        """无 pythonw 代理时的直接启动（非 Windows 或极端回退）。"""
        proc = state.proc
        script = state.script_path
        if typ == "cmd":
            if os.name == "nt":
                proc.setProgram("cmd.exe")
                proc.setNativeArguments('/c "%s"' % script)
                proc.start()
            else:
                proc.start("sh", [script])
        elif typ == "shell":
            proc.start(shutil.which("bash") or shutil.which("sh") or "sh",
                       ["-c", state.cmd["body"]])
        else:
            proc.start(python_exe(), [script])

    def _read(self, state, is_error):
        if state.done:
            return
        data = state.proc.readAllStandardError().data() if is_error else state.proc.readAllStandardOutput().data()
        if data:
            self._feed(state, data, is_error)

    def _feed(self, state, data, is_error):
        if state.done or not data:
            return
        # 日志是完整事实：先按原始字节全量解码落盘，UI 1MB 截断只作用于展示层。
        text = state.decoder.decode(data)
        if text:
            self._log_output(state, text, is_error)
        if state.truncated:
            return
        size = len(data)
        if state.total_bytes + size > OUTPUT_LIMIT:
            room = max(0, OUTPUT_LIMIT - state.total_bytes)
            # 独立解码器只消费 UI 前缀，避免污染日志解码器的跨段字节缓冲。
            ui_text = OutputDecoder().decode(data[:room]) if room else ""
            if ui_text:
                state.record.append(ui_text, is_error)
            state.total_bytes = OUTPUT_LIMIT
            state.truncated = True
            state.record.append_system("⚠ 单次运行输出超过 1MB，后续内容已截断（日志仍完整）")
        else:
            state.total_bytes += size
            if text:
                state.record.append(text, is_error)

    def _on_finished(self, state, code):
        if state.done:
            return
        # 快速退出的进程可能在事件循环处理 readyRead 前结束：收尾时再抽干管道
        # 窗口关闭/解释器退出时 C++ 对象可能已析构，这里静默跳过收尾读取
        try:
            stdout_data = state.proc.readAllStandardOutput().data()
            stderr_data = state.proc.readAllStandardError().data()
        except RuntimeError:
            return
        self._feed(state, stdout_data, False)
        self._feed(state, stderr_data, True)
        state.record.flush()
        if state.stop_reason == "interrupted":
            self._finish(state, "warn", "已中断", fmt_elapsed(state.record.elapsed()))
        elif state.stop_reason == "timeout":
            self._finish(state, "warn", "已超时", fmt_elapsed(state.record.elapsed()))
        elif code != 0:
            self._finish(state, "fail", "失败（退出码 %d）" % code, "退出码 %d" % code)
        else:
            # 整段一次执行，退出码 0 即成功；不再逐行续跑。
            self._finish(state, "success", "成功", fmt_elapsed(state.record.elapsed()))

    def _on_error(self, state, error):
        if state.done:
            return
        if error == QProcess.FailedToStart:
            self._feed(state, state.proc.readAllStandardError().data(), True)
            state.record.flush()
            self._finish(state, "fail", "启动失败", "启动失败")

    def _stop(self, state, reason):
        if state.done:
            return
        state.stop_reason = reason
        state.record.set_status("warn", "已中断" if reason == "interrupted" else "已超时")
        if state.timeout_timer:
            state.timeout_timer.stop()
        if state.using_runner:
            # 先让代理进程杀掉真实子进程，再终止代理本身，避免留下孤儿进程
            try:
                state.proc.write(b"K")
                state.proc.waitForBytesWritten(500)
            except Exception:
                pass
        state.proc.terminate()
        # terminate() 后最多 3 秒未退出则 kill()。Windows 控制台进程通常忽略
        # WM_CLOSE，因此兜底 kill 提前到 2.5 秒，留出事件循环/退出收盘的余量，
        # 保证「3 秒内终止」。
        kill_timer = QTimer(self)
        kill_timer.setSingleShot(True)
        kill_timer.setInterval(2500)
        kill_timer.timeout.connect(lambda p=state.proc: p.kill() if p.state() != QProcess.NotRunning else None)
        kill_timer.start()
        state.kill_timer = kill_timer
        self._status(("已请求中断：%s" if reason == "interrupted" else "已超时，正在终止：%s") % state.cmd["alias"])

    def _settle_script(self, state, succeeded):
        """临时脚本生命周期：成功即删；失败/超时/中断保留并给出完整路径供手工复跑。"""
        path = state.script_path
        if not path:
            return
        if succeeded:
            try:
                os.unlink(path)
            except OSError:
                pass
        else:
            state.record.append_system("脚本已保留：%s" % path)
            self._log_note(state, "脚本已保留：%s" % path)

    def _finish(self, state, key, text, detail=""):
        self._flush_log_lines(state)     # 先补齐末行，再写脚本路径与收尾行
        self._settle_script(state, key == "success")
        state.done = True
        for timer in (state.timeout_timer, state.kill_timer, state.elapsed_timer):
            if timer is not None:
                timer.stop()
        record = state.record
        record.set_status(key, text, detail)
        record.set_pid(0)
        record.show_log_link()
        elapsed = record.elapsed()
        self._log_finish(state, text, key, elapsed)
        self.last_result[state.cmd["id"]] = {"text": text, "elapsed": elapsed, "key": key}
        states = self.active_by_id.get(state.cmd["id"])
        if states is not None:
            states.discard(state)
            if not states:
                self.active_by_id.pop(state.cmd["id"], None)
        self._refresh_left()
        self._refresh_tag_pills()
        self._refresh_detail_buttons()
        self._refresh_stats()
        self._status("%s：%s" % (text, state.cmd["alias"]))

    def _trim_records(self):
        while len(self.records) > HISTORY_LIMIT:
            rec = self.records.pop()
            layout = rec.parent().layout() if rec.parent() else None
            if layout is not None:
                layout.removeWidget(rec)
            rec.hide()
            rec.setParent(self)
            rec.deleteLater()

    def _on_primary(self):
        cmd = self._find_command(self.selected_id)
        if cmd is None:
            return
        states = list(self.active_by_id.get(cmd["id"], ()))
        if states:
            for state in states:
                self._stop(state, "interrupted")
        else:
            self.run_command(cmd["id"])


def main():
    rotate_log_if_needed()   # 启动时检查一次；超限则旧文件转 .old，本次从空文件开始
    atexit.register(close_log)
    if os.name == "nt":
        # 从 cmd/终端启动时主动脱离控制台：否则每次子进程启停都会让
        # 那个终端窗口在任务栏/桌面上闪一下，看起来像弹出「python 框」。
        # 本程序是纯 GUI，输出只写到界面，不需要保留控制台。
        try:
            import ctypes
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setFont(system_font())
    app.setStyleSheet(QSS)          # 唯一一次全局样式设置
    window = CommandPanel()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
