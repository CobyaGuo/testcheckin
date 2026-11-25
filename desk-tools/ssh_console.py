# -*- coding: utf-8 -*-
"""本地单机 SSH 长连接控制台：连上远程后像终端一样连续输入，快捷命令发到当前会话。

纯本地：无 server、无网络服务端、无鉴权、无远程/多人能力。
配置与日志均相对脚本目录：cfg/ssh_console_*.json、logs/ssh_console.log，与当前工作目录无关。
交互会话的输出不写日志（隐私敏感且量大），日志只记事件。
"""
import atexit
import codecs
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

try:
    import paramiko
except ImportError:                       # 缺依赖时界面仍可打开，连接时给出明确原因
    paramiko = None

from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDockWidget, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLayout, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QStatusBar, QTextEdit, QVBoxLayout, QWidget,
)

_SCRIPT_PATH = Path(__file__).resolve()
CFG_DIR = _SCRIPT_PATH.parent / "cfg"
HOSTS_PATH = CFG_DIR / "ssh_console_hosts.json"
COMMANDS_PATH = CFG_DIR / "ssh_console_commands.json"
KNOWN_HOSTS_PATH = CFG_DIR / "ssh_console_known_hosts"
LOG_DIR = _SCRIPT_PATH.parent / "logs"
LOG_PATH = LOG_DIR / (_SCRIPT_PATH.stem + ".log")
LOG_MAX_BYTES = 10 * 1024 * 1024         # 超过 10MB 轮转为 .log.old（覆盖旧文件）
IDLE_SECONDS = 15 * 60                   # 最后 send/recv 超过此时长自动断开（验收可临时调小）
IDLE_WARN_SECONDS = 60                   # 到点前预警；调试把 IDLE_SECONDS 调小于此值时跳过预警
KEEPALIVE_SECONDS = 30
CONNECT_TIMEOUT = 10
RECV_SIZE = 4096
TAB_SPACES = 4

ENV_CHOICES = ("生产", "测试", "跳板", "未标注")
# 高危命令词表：命中即二次确认，生产环境还需额外勾选确认框。
# 只做文本匹配，不做 shell 语义解析——宁可多问一次，不可漏拦一次。
DANGEROUS_PATTERNS = (
    (r"(^|[;&|]\s*)rm\s+(-[a-zA-Z]*[rfF][a-zA-Z]*\s+)+", "删除文件"),
    (r"\b(reboot|shutdown|halt|poweroff)\b", "重启/关机"),
    (r"\b(systemctl|service)\s+(stop|restart|disable|mask)\b", "服务变更"),
    (r"\b(pkill|killall|kill\s+-9)\b", "终止进程"),
    (r"\b(mkfs(\.[\w-]+)?|fdisk|parted|dd\s+if=)", "磁盘破坏"),
    (r"\b(chmod|chown)\s+-[a-zA-Z]*R", "递归权限变更"),
    (r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-zA-Z]*f)", "代码库变更"),
    (r"\bcurl\b.{0,120}\|\s*(ba)?sh\b", "远程脚本执行"),
)

# Qt 样式表的 font-family 不支持逗号回退链，按平台选一个同时覆盖中西文的单一字体。
# Windows 用微软雅黑（Segoe UI 的中文覆盖依赖系统替换，QSS 里写回退链会退化）。
UI_FONT = "Microsoft YaHei UI" if os.name == "nt" else "SF Pro Text"

# ---------------------------------------------------------------------------
# 视觉：所有样式集中在此唯一 QSS 常量，其余代码不再调用 setStyleSheet。
# ---------------------------------------------------------------------------
QSS = r"""
QWidget {
    background: #0d1017; color: #dfe4ee;
    font-family: "__UI_FONT__"; font-size: 12px;
}
QMainWindow, QDialog { background: #0d1017; }
QDialog { border: 1px solid #242b3d; border-radius: 12px; }
QStatusBar { background: #131722; color: #7d8598; border-top: 1px solid #242b3d; font-size: 10px; }
QStatusBar QLabel { color: #7d8598; }

/* -- 顶部应用栏 -- */
QFrame#appbar { background: #131722; border: 0; border-bottom: 1px solid #242b3d; }
QLabel#appTitle { color: #dfe4ee; font-size: 13px; font-weight: 700; }
QLabel#appSummary { color: #7d8598; font-size: 11px; }
QLabel#appDot {
    min-width: 9px; max-width: 9px; min-height: 9px; max-height: 9px;
    border-radius: 4px; background: #3ddc97;
}

/* -- 左侧会话列表 -- */
QFrame#sidebar { background: #131722; border: 0; border-right: 1px solid #242b3d; }
QLabel#sideHeader { color: #7d8598; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
QFrame#sessionCard {
    background: #131722; border: 1px solid #242b3d; border-radius: 10px;
}
QFrame#sessionCard:hover { background: #161b28; }
QFrame#sessionCard[active="true"] {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1d2740, stop:1 #1a2135);
    border: 1px solid #33507e;
}
QLabel#cardName { color: #dfe4ee; font-size: 12px; font-weight: 600; }
QLabel#cardName[dim="true"] { color: #b9c0d0; }
QLabel#cardHost { color: #7d8598; font-size: 10.5px; font-family: Consolas; }
QLabel#cardState { font-size: 10px; color: #7d8598; }
QLabel#cardState[state="connected"] { color: #5a9049; }
QLabel#sideTag {
    color: #7d8598; font-size: 10px; text-align: left;
    background: #181d2b; border: 0; border-radius: 7px;
}
QPushButton#sideTagBtn {
    background: transparent; border: 0; border-radius: 7px;
    color: #7d8598; font-size: 10px; padding: 5px 8px; text-align: left;
}
QPushButton#sideTagBtn:hover { background: #181d2b; color: #dfe4ee; }
QPushButton#sideTagBtn:checked { background: #22304d; color: #9cc3ff; }

/* -- 会话头（右侧工作区顶部） -- */
QFrame#sessionHeader { background: #131722; border: 0; border-bottom: 1px solid #242b3d; }
QLabel#headerName { color: #dfe4ee; font-size: 12.5px; font-weight: 700; }
QLabel#headerMeta { color: #7d8598; font-size: 10.5px; font-family: Consolas; }

/* -- 快捷命令抽屉 -- */
QFrame#drawer { background: #181d2b; border: 0; border-top: 1px solid #242b3d; }
QFrame#drawerPanel { background: #131722; border: 0; border-top: 1px solid #242b3d; }
QLabel#quickLabel { color: #7d8598; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
QLabel#hint { color: #5a6274; font-size: 9.5px; }
QLabel#pillGroup { font-size: 9.5px; font-weight: 700; }
QLabel#envChip { font-size: 10px; border-radius: 99px; padding: 1px 7px; border: 1px solid #242b3d; color: #7d8598; }
QLabel#envChip[env="生产"] { color: #ff9c9c; background: #3a2226; border-color: #5a2a31; }
QLabel#envChip[env="测试"] { color: #7fb0ff; background: #22304d; border-color: #33507e; }
QLabel#envChip[env="跳板"] { color: #9cc3ff; background: #22304d; border-color: #33507e; }
QLabel#targetChip { color: #9cc3ff; border: 1px solid #33507e; border-radius: 99px; padding: 2px 8px; font-size: 10px; }
QLabel#receiptOk { color: #3ddc97; font-size: 10.5px; }
QLabel#receiptFail { color: #ff6b6b; font-size: 10.5px; }
QLabel#safetyText { color: #f5c04e; font-size: 11px; }
QLabel#riskHint { color: #f5c04e; font-size: 10px; }
QFrame#safetyBar { background: #332318; border: 1px solid #854f0b; border-radius: 8px; }
QPushButton[role="pin"]:checked { background: #22304d; border-color: #4f8dff; color: #9cc3ff; }
QLabel[role="pill"][tone="0"] { color: #9cc3ff; }
QLabel[role="pill"][tone="1"] { color: #6ee7b7; }
QLabel[role="pill"][tone="2"] { color: #fcd34d; }
QLabel[role="pill"][tone="3"] { color: #c4b5fd; }
QLabel[role="pill"][tone="4"] { color: #f9a8d4; }
QLabel[role="pill"][tone="5"] { color: #67e8f9; }
QLabel#field { color: #7d8598; font-size: 10.5px; }

QPushButton {
    background: #181d2b; color: #b9c0d0; border: 1px solid #242b3d;
    border-radius: 99px; padding: 5px 13px; font-size: 11px; font-weight: 600;
}
QPushButton:hover { background: #222939; color: #dfe4ee; }
QPushButton:pressed { background: #4f8dff; color: #ffffff; }
QPushButton:disabled { background: #141824; color: #565e70; border-color: #242b3d; }
QPushButton[role="primary"] { background: #4f8dff; border-color: #4f8dff; color: #ffffff; }
QPushButton[role="primary"]:hover { background: #699dff; border-color: #699dff; }
QPushButton[role="danger"] { background: #3a2226; border-color: #5a2a31; color: #ff9c9c; }
QPushButton[role="danger"]:hover { background: #4a2a30; color: #ffc4c4; }
QPushButton[role="pill"] {
    background: transparent; border: 1px solid #242b3d; border-radius: 99px;
    padding: 3px 11px; font-size: 10.5px; font-weight: 500; color: #9aa3b5;
}
QPushButton[role="pill"]:hover { background: #222939; color: #dfe4ee; }
QPushButton[role="pill"][tone="0"] { color: #9cc3ff; border-color: #33507e; }
QPushButton[role="pill"][tone="1"] { color: #6ee7b7; border-color: #2f6b59; }
QPushButton[role="pill"][tone="2"] { color: #fcd34d; border-color: #786336; }
QPushButton[role="pill"][tone="3"] { color: #c4b5fd; border-color: #5d5291; }
QPushButton[role="pill"][tone="4"] { color: #f9a8d4; border-color: #84506d; }
QPushButton[role="pill"][tone="5"] { color: #67e8f9; border-color: #31727d; }
QPushButton[role="pill"][tone="risk"] { color: #ff9c9c; background: #3a2226; border-color: #5a2a31; }
QPushButton[role="fill"] {
    background: transparent; color: #7fb0ff; border: 1px dashed #33476b;
    border-radius: 99px; padding: 3px 7px; font-size: 9px; font-weight: 500;
}
QPushButton[role="fill"]:hover { color: #cfe2f7; border-color: #4f8dff; background: #1d2536; }
QPushButton[role="mini"] {
    background: transparent; color: #7d8598; border: 0; border-radius: 6px;
    padding: 0; min-width: 18px; max-width: 18px; min-height: 18px; max-height: 18px; font-size: 11px;
}
QPushButton[role="mini"]:hover { background: #2a3348; color: #ffffff; }
QPushButton[role="link"] {
    background: transparent; border: 0; color: #7fb0ff; padding: 2px 4px;
    font-size: 10.5px; font-weight: 500;
}
QPushButton[role="link"][tone="danger"] { color: #ff6b6b; }
QPushButton[role="link"][tone="dim"] { color: #7d8598; }

QLineEdit, QPlainTextEdit {
    background: #141824; color: #dfe4ee; border: 1px solid #242b3d;
    border-radius: 7px; padding: 5px 9px; font-size: 11px; selection-background-color: #4f8dff;
}
QLineEdit:focus, QPlainTextEdit:focus { border: 1px solid #4f8dff; }
QLineEdit#search {
    background: #181d2b; border: 1px solid #242b3d; border-radius: 8px;
    padding: 5px 10px; min-width: 150px;
}
QPlainTextEdit#input {
    background: #0a0d13; color: #dfe4ee; border: 1px solid #242b3d;
    border-radius: 9px; padding: 6px 11px; font-family: Consolas; font-size: 12px;
}
QPlainTextEdit#input:focus { border: 1px solid #33507e; }
QPlainTextEdit#code {
    background: #0a0d13; color: #b6bdcb; font-family: Consolas; font-size: 11px;
    border: 1px solid #242b3d; border-radius: 8px; padding: 7px 10px;
}
QTextEdit#terminal {
    background: #0a0d13; color: #b6bdcb; font-family: Consolas; font-size: 11.5px;
    border: 0; border-radius: 0; padding: 12px 16px;
}
QStackedWidget { background: #0a0d13; }
QScrollArea { background: transparent; border: 0; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 0; border: 0; }
QScrollBar::handle:vertical { background: #2c364e; border-radius: 3px; min-height: 22px; }
QScrollBar::handle:vertical:hover { background: #3d4a6b; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 7px; }
QScrollBar::handle:horizontal { background: #2c364e; border-radius: 3px; min-width: 22px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QListWidget { background: #141824; border: 1px solid #242b3d; border-radius: 8px; padding: 4px; }
QListWidget::item { padding: 7px 8px; border-radius: 6px; color: #c6ccda; }
QListWidget::item:selected { background: #22304d; color: #ffffff; }
QCheckBox { background: transparent; color: #9aa3b5; spacing: 5px; font-size: 10.5px; }
QCheckBox::indicator {
    width: 12px; height: 12px; border: 1px solid #333c52;
    border-radius: 3px; background: #141824;
}
QCheckBox::indicator:checked { background: #4f8dff; border-color: #4f8dff; }
QGroupBox {
    background: #131722; border: 1px solid #242b3d; border-radius: 9px;
    margin-top: 9px; padding: 8px; color: #7d8598;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QDockWidget { background: #131722; titlebar-close-icon: none; }
QDockWidget::title { background: #131722; color: #b9c0d0; padding: 7px 10px; }
QToolTip {
    background: #1d2436; color: #dfe4ee; border: 1px solid #4f8dff;
    border-radius: 7px; padding: 4px 7px; font-size: 11px;
}
QMessageBox { background: #131722; }
QMessageBox QPushButton { min-width: 72px; }
""".replace("__UI_FONT__", UI_FONT)


# ---------------------------------------------------------------------------
# 配置：逐字段容错 + 缺失时自动创建预置示例 + 原子写
# ---------------------------------------------------------------------------
def default_hosts():
    return [
        {"id": "h1", "name": "in-test-01", "user": "qa", "host": "10.2.1.11", "port": 22,
         "identityFile": "", "tags": ["测试"], "env": "测试", "note": "印度测试机"},
        {"id": "h2", "name": "us-jumpbox", "user": "ops", "host": "10.3.0.5", "port": 22,
         "identityFile": "", "tags": ["跳板"], "env": "跳板", "note": ""},
    ]


def default_commands():
    return [
        {"id": "s1", "alias": "看应用日志尾", "body": "tail -n 50 /var/log/app.log",
         "tags": ["日志"], "note": "", "starred": True, "readonly": True, "envs": []},
        {"id": "s2", "alias": "服务状态", "body": "systemctl status app",
         "tags": ["运维"], "note": "", "starred": False, "readonly": True, "envs": []},
        {"id": "s3", "alias": "磁盘占用", "body": "df -h /",
         "tags": ["运维"], "note": "", "starred": False, "readonly": True, "envs": []},
    ]


def _text(value, default=""):
    return value if isinstance(value, str) else default


def _tags(value, where, warnings):
    if not isinstance(value, list):
        warnings.append("%s.tags 无效→[]" % where)
        return []
    good = [t.strip() for t in value if isinstance(t, str) and t.strip()]
    if len(good) != len(value):
        warnings.append("%s.tags 含无效项→已过滤" % where)
    return list(dict.fromkeys(good))


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"):
        return True
    return default


def _env_of(item, tags, name, where, warnings):
    """旧配置缺 env 时按 tags/名字推断，绝不把未知主机误标成生产。"""
    raw = _text(item.get("env")).strip()
    if raw:
        if raw in ENV_CHOICES:
            return raw
        warnings.append("%s.env 未知→未标注" % where)
        return "未标注"
    text = " ".join(tags + [name]).lower()
    if any(k in text for k in ("prod", "生产", "正式", "online")):
        return "生产"
    if any(k in text for k in ("跳板", "jump")):
        return "跳板"
    if any(k in text for k in ("测试", "test", "dev", "开发")):
        return "测试"
    return "未标注"


def _envs_of(item, where, warnings):
    value = item.get("envs")
    if value is None:
        return []
    if not isinstance(value, list):
        warnings.append("%s.envs 无效→[]（适用全部环境）" % where)
        return []
    good = [v for v in value if isinstance(v, str) and v in ENV_CHOICES]
    if len(good) != len(value):
        warnings.append("%s.envs 含无效项→已过滤" % where)
    return list(dict.fromkeys(good))


def _ids(items, prefix, where, warnings):
    used, result = set(), []
    for i, item in enumerate(items):
        name = "%s[%d]" % (where, i)
        if not isinstance(item, dict):
            warnings.append("%s 不是对象→已忽略" % name)
            continue
        cid = item.get("id")
        if not isinstance(cid, str) or not cid.strip():
            cid = "%s%02d" % (prefix, i + 1)
            warnings.append("%s.id 缺失→%s" % (name, cid))
        if cid in used:
            n = 2
            while "%s_%d" % (cid, n) in used:
                n += 1
            warnings.append("%s.id 重复→%s_%d" % (name, cid, n))
            cid = "%s_%d" % (cid, n)
        used.add(cid)
        result.append((cid, item, name))
    return result


def _read_json(path, defaults, key):
    """返回 (条目列表, warnings)；文件不存在时由 ensure_configs 预置。"""
    warnings = []
    if not path.exists():
        return [dict(x) for x in defaults], warnings
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [dict(x) for x in defaults], ["%s 解析失败：%s（已回退预置）" % (path.name, exc)]
    if not isinstance(raw, dict):
        return [dict(x) for x in defaults], ["%s 根节点不是对象（已回退预置）" % path.name]
    if not isinstance(raw.get("version", 1), int) or isinstance(raw.get("version"), bool):
        warnings.append("%s.version 无效→1" % path.name)
    src = raw.get(key)
    if not isinstance(src, list):
        warnings.append("%s.%s 不是数组→回退预置" % (path.name, key))
        return [dict(x) for x in defaults], warnings
    return src, warnings


def load_hosts():
    src, warnings = _read_json(HOSTS_PATH, default_hosts(), "hosts")
    out = []
    for cid, item, where in _ids(src, "h", "hosts", warnings):
        name = _text(item.get("name")).strip() or "未命名主机"
        if not name:
            warnings.append("%s.name 无效→未命名主机" % where)
        user = _text(item.get("user")).strip() or "root"
        host = _text(item.get("host")).strip() or "127.0.0.1"
        port = item.get("port", 22)
        try:
            if isinstance(port, bool):
                raise ValueError
            port = int(port)
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            warnings.append("%s.port 无效→22" % where)
            port = 22
        tags = _tags(item.get("tags"), where, warnings)
        out.append({
            "id": cid, "name": name, "user": user, "host": host, "port": port,
            "identityFile": _text(item.get("identityFile")).strip(),
            "tags": tags, "env": _env_of(item, tags, name, where, warnings),
            "note": _text(item.get("note")),
        })
    return out, warnings


def load_commands():
    src, warnings = _read_json(COMMANDS_PATH, default_commands(), "commands")
    out = []
    for cid, item, where in _ids(src, "s", "commands", warnings):
        alias = _text(item.get("alias")).strip()
        if not alias:
            warnings.append("%s.alias 缺失→未命名命令" % where)
            alias = "未命名命令"
        out.append({
            "id": cid, "alias": alias, "body": _text(item.get("body")),
            "tags": _tags(item.get("tags"), where, warnings),
            "note": _text(item.get("note")),
            "starred": _bool(item.get("starred")),
            "readonly": _bool(item.get("readonly")),
            "envs": _envs_of(item, where, warnings),
        })
    return out, warnings


def save_json(path, data):
    """原子写：同目录临时文件 → fsync → os.replace，返回 None 表示成功。"""
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
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


def ensure_configs():
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    errors = []
    if not HOSTS_PATH.exists():
        err = save_json(HOSTS_PATH, {"version": 1, "hosts": default_hosts()})
        if err:
            errors.append("主机配置创建失败：%s" % err)
    if not COMMANDS_PATH.exists():
        err = save_json(COMMANDS_PATH, {"version": 1, "commands": default_commands()})
        if err:
            errors.append("命令库创建失败：%s" % err)
    return errors


def unique_id(commands):
    used = {c["id"] for c in commands}
    i = 1
    while ("s%02d" % i) in used or ("s%d" % i) in used:  # 兼容预置的 s1 这种单数字 id
        i += 1
    return "s%02d" % i


def tag_tone(tag):
    """标签→固定色调序号，QSS 里定义 0..5 六种颜色；同一标签每次启动颜色一致。"""
    return sum(ord(c) for c in tag) % 6


def risk_labels(text):
    """返回命中的高危类型列表；只做保守的文本匹配，宁可多问一次。"""
    if not text:
        return []
    return [label for pattern, label in DANGEROUS_PATTERNS
            if re.search(pattern, text, re.IGNORECASE)]


def command_allowed(cmd, env):
    """命令是否在当前环境可见：envs 为空表示全环境适用。"""
    envs = cmd.get("envs") or []
    return not envs or not env or env in envs


def expand_template(text, host):
    """${HOST}/${USER}/${TODAY}/${ENV} 逐主机渲染，未定义变量原样保留。"""
    if not text or "${" not in text:
        return text
    values = {
        "HOST": str(host.get("host", "")),
        "NAME": str(host.get("name", "")),
        "USER": str(host.get("user", "")),
        "ENV": str(host.get("env", "")),
        "TODAY": datetime.now().strftime("%Y-%m-%d"),
    }
    out = text
    for key, value in values.items():
        out = out.replace("${%s}" % key, value)
    return out


# ---------------------------------------------------------------------------
# 事件日志：只记事件，不记输出流；单文件追加，10MB 轮转
# ---------------------------------------------------------------------------
_LOG_LOCK = threading.Lock()
_LOG_HANDLE = None


def _log_write(text, flush=False):
    global _LOG_HANDLE
    with _LOG_LOCK:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            if _LOG_HANDLE is None:
                _LOG_HANDLE = open(LOG_PATH, "a", encoding="utf-8")
            _LOG_HANDLE.write(text)
            if flush:
                _LOG_HANDLE.flush()
        except Exception:
            pass


def _log_event(text):
    # 事件日志量很小，逐条 flush：外部编辑器/搜索随时能看到最新事件
    _log_write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), text), flush=True)


def rotate_log_if_needed():
    """启动与每次会话结束时各查一次；超限则旧文件转 .old，下一次写入新建空文件。"""
    global _LOG_HANDLE
    with _LOG_LOCK:
        try:
            if _LOG_HANDLE is not None:
                _LOG_HANDLE.flush()
            if not LOG_PATH.exists() or LOG_PATH.stat().st_size <= LOG_MAX_BYTES:
                return
            if _LOG_HANDLE is not None:
                _LOG_HANDLE.close()
                _LOG_HANDLE = None
            os.replace(LOG_PATH, LOG_PATH.parent / (LOG_PATH.name + ".old"))
            with open(LOG_PATH, "w", encoding="utf-8"):   # 新文件立即建好，从空开始
                pass
        except Exception:
            pass


def close_log():
    global _LOG_HANDLE
    with _LOG_LOCK:
        if _LOG_HANDLE is not None:
            try:
                _LOG_HANDLE.flush()
                _LOG_HANDLE.close()
            except Exception:
                pass
            _LOG_HANDLE = None


# ---------------------------------------------------------------------------
# 简化 VT：SGR 颜色 + \r 覆盖 + \x1b[2J 清屏，其他控制序列静默吞掉
# ---------------------------------------------------------------------------
_SGR_FG = {
    30: "#1e222c", 31: "#ff6b6b", 32: "#3ddc97", 33: "#f5c04e",
    34: "#7fb0ff", 35: "#c084fc", 36: "#22d3ee", 37: "#dfe4ee",
    90: "#64748b", 91: "#ff9c9c", 92: "#6ee7b7", 93: "#fde68a",
    94: "#9cc3ff", 95: "#d8b4fe", 96: "#67e8f9", 97: "#ffffff",
}
_SGR_BG = {
    40: "#14161d", 41: "#5e2730", 42: "#175347", 43: "#5c4b1c",
    44: "#1e3a8a", 45: "#581c87", 46: "#164e63", 47: "#c3c9d8",
}


class TerminalView(QTextEdit):
    """每个会话一份只读终端视图，保留自己的历史；滚动上翻暂停自动滚底。"""

    receipt_ready = Signal(str, int, float)    # token, exit_code, elapsed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("terminal")
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.WidgetWidth)
        self.setFont(QFont("Consolas", 10))
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""                  # 跨 recv chunk 的半截 ANSI 序列
        self._attrs = {"fg": None, "bg": None, "bold": False}
        self._format_cache = {}
        self._auto_scroll = True
        self._overwrite = False             # \r 之后进入覆盖模式，模拟终端行尾刷新
        self.receipts = {}                  # token -> {display, started}
        self.find_term = ""
        self.highlight_enabled = True
        self.zoom = 0
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    # -- 对外 ---------------------------------------------------------------
    def append_bytes(self, data):
        if not data:
            return
        if isinstance(data, str):             # 方便手工注入文本（网络侧实际送来的是 bytes）
            text = data
        else:
            text = self._decoder.decode(data)
        if text:
            self._push(text)

    def flush_stream(self):
        text = self._decoder.decode(b"", True)
        if text:
            self._push(text)

    def append_system(self, text, color="#5eead4"):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.setCharFormat(fmt)
        cursor.insertText(text + "\n")
        self.setTextCursor(cursor)
        if self._auto_scroll:
            self.ensureCursorVisible()

    def clear_display(self):
        self.clear()
        self._attrs = {"fg": None, "bg": None, "bold": False}
        self._overwrite = False

    def scroll_to_bottom(self):
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        self._auto_scroll = True

    # -- 回执标记 / 查找高亮 ------------------------------------------------
    def register_receipt(self, token, display):
        """登记一次待回执执行；回显的包装命令会被替换成用户原本的命令。"""
        self.receipts[token] = {"display": display, "started": time.monotonic()}

    def cancel_receipt(self, token, reason):
        info = self.receipts.pop(token, None)
        if info:
            self.receipt_ready.emit(token, -1, time.monotonic() - info["started"])

    def set_find_term(self, term):
        self.find_term = term or ""
        self._apply_highlights()

    def apply_zoom(self, delta):
        self.zoom = max(-4, min(8, self.zoom + delta))
        font = self.font()
        font.setPointSize(max(8, 10 + self.zoom))
        self.setFont(font)
        for fmt in self._format_cache.values():
            fmt.setFontPointSize(max(8, 10 + self.zoom))
        self._apply_highlights()

    def export_text(self):
        return self.toPlainText()

    def _apply_highlights(self):
        """给查找词与 ERROR/FAIL/WARN 加底色，不改文字内容、不覆盖 ANSI 前景色。"""
        selections = []
        terms = []
        if self.find_term:
            terms.append((self.find_term, "#3b5991"))
        if self.highlight_enabled:
            terms += [("ERROR", "#5e2730"), ("FAIL", "#5e2730"), ("WARN", "#3a2f1a")]
        for term, color in terms:
            cursor = QTextCursor(self.document())
            flags = QTextDocument.FindCaseSensitively
            while True:
                found = self.document().find(term, cursor, flags)
                if found.isNull():
                    break
                fmt = QTextCharFormat()
                fmt.setBackground(QColor(color))
                sel = QTextEdit.ExtraSelection()
                sel.cursor, sel.format = found, fmt
                selections.append(sel)
                cursor = found
                cursor.movePosition(QTextCursor.Right)
                if len(selections) >= 300:
                    break
        self.setExtraSelections(selections)

    def _resolve_receipt_line(self):
        """在换行落盘前处理包装命令的回显与退出码标记，不在终端留 ^A/^B 残迹。"""
        if not self.receipts:
            return
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        end = cursor.position()
        cursor.movePosition(QTextCursor.StartOfBlock)
        start = cursor.position()
        if start == end:
            return
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        line = cursor.selectedText()
        for token, info in list(self.receipts.items()):
            if token not in line:
                continue
            # 真正的退出码标记以 \x01 开头；shell 回显里的 \001 是字面反斜杠，
            # 不能把回显误当成完成标记（否则标记输出行会漏解析、回显会被吞掉）。
            match = None
            if line.startswith("\x01"):
                match = re.search(r"CPE\s+" + re.escape(token) + r"\s+(\d+)", line)
            replacement = info["display"]         # 包装命令回显 → 还原成用户原命令
            if match:
                self.receipt_ready.emit(token, int(match.group(1)),
                                        time.monotonic() - info["started"])
                self.receipts.pop(token, None)
                replacement = ""                  # 标记行本身不进终端
            fmt = QTextCharFormat()
            fmt.setForeground(QColor("#5eead4" if replacement else "#8b93a5"))
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            cursor.setCharFormat(fmt)
            cursor.insertText(replacement)
            self.setTextCursor(cursor)
            break

    # -- ANSI 解析 ----------------------------------------------------------
    def _on_scroll(self, value):
        bar = self.verticalScrollBar()
        self._auto_scroll = value >= bar.maximum() - 4

    def _char_format(self):
        key = (self._attrs["fg"], self._attrs["bg"], self._attrs["bold"])
        fmt = self._format_cache.get(key)
        if fmt is None:
            fmt = QTextCharFormat()
            if key[0]:
                fmt.setForeground(QColor(key[0]))
            if key[1]:
                fmt.setBackground(QColor(key[1]))
            fmt.setFontWeight(QFont.Bold if key[2] else QFont.Normal)
            self._format_cache[key] = fmt
        return fmt

    def _insert(self, text):
        fmt = self._char_format()
        if self._overwrite and text:
            cursor = self.textCursor()
            end = self.textCursor()
            end.movePosition(QTextCursor.EndOfLine)
            room = max(0, end.position() - cursor.position())
            if room:
                take = min(len(text), room)
                cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, take)
                cursor.setCharFormat(fmt)
                cursor.insertText(text[:take])   # 选中替换 = 覆盖写法，不是插入
                text = text[take:]
                if not text:
                    return
        cursor = self.textCursor()
        cursor.setCharFormat(fmt)
        cursor.insertText(text)
        if self._auto_scroll:
            self.ensureCursorVisible()
        if self.find_term or self.highlight_enabled:
            self._apply_highlights()

    def _carriage_return(self):
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.StartOfBlock)
        self.setTextCursor(cursor)
        self._overwrite = True

    def _line_feed(self):
        # 流式终端的 \n 等价于换行到新行末尾；\r\n 不会破坏已有内容
        self._resolve_receipt_line()
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.setCharFormat(self._char_format())
        cursor.insertText("\n")
        self.setTextCursor(cursor)
        self._overwrite = False
        if self._auto_scroll:
            self.ensureCursorVisible()

    def _csi(self, params, final):
        if final == "m":
            codes = [int(p) if p.isdigit() else 0 for p in params.split(";")] if params else [0]
            for code in codes:
                if code == 0:
                    self._attrs = {"fg": None, "bg": None, "bold": False}
                elif code == 1:
                    self._attrs["bold"] = True
                elif code == 22:
                    self._attrs["bold"] = False
                elif code == 39:
                    self._attrs["fg"] = None
                elif code == 49:
                    self._attrs["bg"] = None
                elif code in _SGR_FG:
                    self._attrs["fg"] = _SGR_FG[code]
                elif code in _SGR_BG:
                    self._attrs["bg"] = _SGR_BG[code]
        elif final == "J" and params in ("", "2"):
            self.clear_display()                  # \x1b[2J 清屏后重绘
        elif final == "K":
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
        # 其他 CSI（光标隐藏、方向键应答等）静默忽略，绝不留 ^[[ 残迹

    def _push(self, text):
        buf = self._pending + text
        self._pending = ""
        i = 0
        n = len(buf)
        while i < n:
            ch = buf[i]
            if ch == "\x1b":
                if i + 1 >= n:
                    self._pending = buf[i:]
                    break
                nxt = buf[i + 1]
                if nxt == "[":                    # CSI
                    j = i + 2
                    while j < n and (buf[j].isdigit() or buf[j] in ";:?<> =!\"$'*+"):
                        j += 1
                    if j >= n:
                        self._pending = buf[i:]   # 序列未收全，等下一个 chunk
                        break
                    if buf[j].isalpha() or buf[j] in "@`~":
                        self._csi(buf[i + 2:j], buf[j])
                        i = j + 1
                    else:
                        i += 2                    # 非法序列：只吞掉 ESC[
                    continue
                if nxt == "]":                    # OSC：标题串等，吞到 BEL 或 ST
                    j = i + 2
                    while j < n:
                        if buf[j] == "\x07":
                            j += 1
                            break
                        if buf[j] == "\x1b" and j + 1 < n and buf[j + 1] == "\\":
                            j += 2
                            break
                        j += 1
                    else:
                        self._pending = buf[i:]
                        break
                    i = j
                    continue
                i += 2                            # ESC 加单字符（字符集切换等）
                continue
            if ch == "\r":
                self._carriage_return()
                i += 1
                continue
            if ch == "\n":
                self._line_feed()
                i += 1
                continue
            if ch == "\b":
                cursor = self.textCursor()
                cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                i += 1
                continue
            if ch == "\t":
                self._insert(" " * TAB_SPACES)
                i += 1
                continue
            j = i + 1
            while j < n and buf[j] not in "\x1b\r\n\b\t":
                j += 1
            self._insert(buf[i:j])
            i = j


# ---------------------------------------------------------------------------
# 会话：每条独立 SSHClient + invoke_shell，读线程 → Qt 信号 → 终端
# ---------------------------------------------------------------------------
class Session(QObject):
    data_received = Signal(object, bytes)
    state_changed = Signal(object, str, str)     # session, state, reason
    password_needed = Signal(object)

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = dict(host)
        self.client = None
        self.channel = None
        self.state = "connecting"
        self.reason = ""
        self.password = None                     # 仅内存保存，不落盘
        self.auto_retry_used = False
        self.connected_at = None
        self.connect_ms = 0                      # TCP 握手耗时，tab 上展示
        self.last_activity = time.monotonic()
        self.draft = ""                          # 每会话独立输入草稿，切 tab 不互串
        self.history = []                        # 每会话独立输入历史（内存）
        self.pinned = False                      # 钉住后豁免空闲自动断开
        self.idle_warned = False
        self._stop = threading.Event()
        self._reader = None

    @property
    def target(self):
        return "%s@%s:%s" % (self.host["user"], self.host["host"], self.host["port"])

    def _set_state(self, state, reason=""):
        self.state = state
        self.reason = reason
        self.state_changed.emit(self, state, reason)

    def _auth_failure(self, exc):
        if paramiko is not None:
            cls = getattr(paramiko, "AuthenticationException", None)
            if cls is not None and isinstance(exc, cls):
                return True
        text = str(exc).lower()
        return "authentication" in text or "password" in text or "no auth" in text

    def connect(self, password=None, auto_retry=False):
        if paramiko is None:
            self._set_state("disconnected", "未安装 paramiko（请 pip install paramiko）")
            return
        if password is not None:
            self.password = password
        self._stop.clear()
        self.last_activity = time.monotonic()
        self._set_state("connecting", "正在连接…")
        threading.Thread(target=self._connect_worker, args=(password, auto_retry), daemon=True).start()

    def _connect_worker(self, password, auto_retry):
        client = paramiko.SSHClient()
        try:
            if KNOWN_HOSTS_PATH.exists():
                client.load_host_keys(str(KNOWN_HOSTS_PATH))  # 坏的 known_hosts 不阻断连接
        except Exception:
            pass
        try:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs = dict(
                hostname=self.host["host"], port=self.host["port"],
                username=self.host["user"], timeout=CONNECT_TIMEOUT,
                allow_agent=False, look_for_keys=False,
            )
            identity = self.host.get("identityFile", "")
            if identity:
                kwargs["key_filename"] = os.path.expanduser(identity)
            elif password:
                kwargs["password"] = password
            connect_started = time.monotonic()
            client.connect(**kwargs)
            self.connect_ms = int((time.monotonic() - connect_started) * 1000)
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(KEEPALIVE_SECONDS)
            channel = client.invoke_shell(term="xterm", width=200, height=50)
            try:                                   # known_hosts 副本存 cfg/
                client.get_host_keys().save(str(KNOWN_HOSTS_PATH))
            except Exception:
                pass
        except Exception as exc:
            try:
                client.close()
            except Exception:
                pass
            if self._auth_failure(exc) and not self.host.get("identityFile"):
                if password is not None and self.password and not auto_retry:
                    # 用户刚输入的密码错了：保留输入框结果，提示重输
                    self._set_state("disconnected", "认证失败：%s" % exc)
                    self.password_needed.emit(self)
                elif self.password and not self.auto_retry_used and not auto_retry:
                    # 断线后自动重试一次（用内存里的密码），失败才弹框
                    self.auto_retry_used = True
                    self._connect_worker(self.password, True)
                else:
                    self._set_state("disconnected", "需要密码：%s" % exc)
                    self.password_needed.emit(self)
            else:
                self._set_state("disconnected", "连接失败：%s" % exc)
            return

        self.client = client
        self.channel = channel
        self.connected_at = time.time()
        self.last_activity = time.monotonic()
        self.auto_retry_used = False
        self._set_state("connected", "")
        self._reader = threading.Thread(target=self._read_worker, daemon=True)
        self._reader.start()

    def _read_worker(self):
        while not self._stop.is_set():
            try:
                data = self.channel.recv(RECV_SIZE)
            except Exception as exc:
                if not self._stop.is_set():
                    self._set_state("disconnected", "连接中断：%s" % exc)
                break
            if not data:
                if not self._stop.is_set():
                    self._set_state("disconnected", "远端关闭连接")
                break
            self.last_activity = time.monotonic()
            self.idle_warned = False
            self.data_received.emit(self, data)

    def send_text(self, text):
        """主线程直接 send，不等回显；invoke_shell 自己回显，本地绝不重复画。"""
        if self.state != "connected" or self.channel is None:
            return False
        try:
            self.channel.send(text.encode("utf-8"))
            self.last_activity = time.monotonic()
            self.idle_warned = False
            return True
        except Exception as exc:
            self._set_state("disconnected", "发送失败：%s" % exc)
            return False

    def disconnect(self, reason="会话已断开", state="disconnected"):
        self._stop.set()
        self._set_state(state, reason)
        self.close_resources()

    def close_resources(self):
        try:
            if self.channel is not None:
                self.channel.close()
        except Exception:
            pass
        try:
            if self.client is not None:
                self.client.close()
        except Exception:
            pass

    def close(self):
        self._stop.set()
        self.close_resources()
        reader = self._reader
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(0.5)


# ---------------------------------------------------------------------------
# 对话框与小部件
# ---------------------------------------------------------------------------
class HostDialog(QDialog):
    """「＋ 会话」的主机选择框：name + user@host + 状态灯文字。"""

    def __init__(self, parent, hosts, connected_ids):
        super().__init__(parent)
        self.setWindowTitle("新建 SSH 会话")
        self.resize(430, 330)
        self.selected = None
        root = QVBoxLayout(self)
        tip = QLabel("选择主机（已在连接中的会话会直接切换）")
        tip.setObjectName("field")
        root.addWidget(tip)
        self.list = QListWidget()
        for host in hosts:
            state = "已连接" if host["id"] in connected_ids else "可连接"
            item = QListWidgetItem("%s     %s@%s:%s     [%s]" % (
                host["name"], host["user"], host["host"], host["port"], state))
            item.setData(Qt.UserRole, host)
            self.list.addItem(item)
        if hosts:
            self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(self._accept)
        root.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        ok = QPushButton("连接")
        ok.setProperty("role", "primary")
        ok.clicked.connect(self._accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        for btn in (ok, cancel):
            btn.setCursor(Qt.PointingHandCursor)
            buttons.addWidget(btn)
        root.addLayout(buttons)

    def _accept(self):
        item = self.list.currentItem()
        if item is None:
            QMessageBox.warning(self, "新建会话", "请先选择一台主机。")
            return
        self.selected = item.data(Qt.UserRole)
        self.accept()


class SshCommandDialog(QDialog):
    """精简命令编辑：别名*、命令体、标签（勾选 + 新建）、备注。"""

    def __init__(self, parent, command, all_tags, is_new=False):
        super().__init__(parent)
        self.command = dict(command)
        self.is_new = is_new
        self.result_command = None
        self.action = None
        self.setModal(True)
        self.setWindowTitle("新建快捷命令" if is_new else "编辑 · %s" % command["alias"])
        self.resize(560, 560)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)

        self.alias_edit = QLineEdit(command["alias"])
        self.alias_edit.setPlaceholderText("别名（必填）")
        root.addLayout(self._row("别名 *", self.alias_edit))

        self.body_edit = QPlainTextEdit(command["body"])
        self.body_edit.setObjectName("code")
        self.body_edit.setPlaceholderText("发到远端 shell 的文本，多行逐行发送")
        self.body_edit.setMinimumHeight(150)
        root.addLayout(self._row("命令体", self.body_edit))

        tags_box = QGroupBox("标签")
        tags_layout = QVBoxLayout(tags_box)
        self.tags_grid = QGridLayout()
        self.tag_checkboxes = {}
        for idx, tag in enumerate(sorted(set(all_tags) | set(command["tags"]))):
            cb = QCheckBox(tag)
            cb.setChecked(tag in command["tags"])
            self.tag_checkboxes[tag] = cb
            self.tags_grid.addWidget(cb, idx // 3, idx % 3)
        tags_layout.addLayout(self.tags_grid)
        new_row = QHBoxLayout()
        self.new_tag_edit = QLineEdit()
        self.new_tag_edit.setPlaceholderText("新标签名")
        add_btn = QPushButton("＋ 新建标签")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self._add_tag)
        new_row.addWidget(self.new_tag_edit, 1)
        new_row.addWidget(add_btn)
        tags_layout.addLayout(new_row)
        root.addWidget(tags_box)

        flags = QHBoxLayout()
        self.starred_cb = QCheckBox("收藏（面板置顶）")
        self.starred_cb.setChecked(bool(command.get("starred")))
        self.readonly_cb = QCheckBox("只读命令（允许批量发送）")
        self.readonly_cb.setChecked(bool(command.get("readonly")))
        flags.addWidget(self.starred_cb)
        flags.addWidget(self.readonly_cb)
        flags.addStretch(1)
        root.addLayout(flags)

        env_box = QGroupBox("适用环境（全不勾 = 所有环境）")
        env_grid = QGridLayout(env_box)
        self.env_checkboxes = {}
        current_envs = command.get("envs") or []
        for idx, env in enumerate(ENV_CHOICES):
            cb = QCheckBox(env)
            cb.setChecked(env in current_envs)
            self.env_checkboxes[env] = cb
            env_grid.addWidget(cb, idx // 4, idx % 4)
        root.addWidget(env_box)

        self.note_edit = QPlainTextEdit(command["note"])
        self.note_edit.setMinimumHeight(60)
        self.note_edit.setPlaceholderText("备注（可选）")
        root.addLayout(self._row("备注", self.note_edit))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        save = QPushButton("保存并关闭")
        save.setProperty("role", "primary")
        save.clicked.connect(self._on_save)
        delete = QPushButton("删除…")
        delete.setProperty("role", "danger")
        delete.setVisible(not is_new)
        delete.clicked.connect(self._on_delete)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        for btn in (save, delete, cancel):
            btn.setCursor(Qt.PointingHandCursor)
            buttons.addWidget(btn)
        root.addLayout(buttons)

    @staticmethod
    def _row(title, control):
        row = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("field")
        label.setFixedWidth(58)
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
            idx = len(self.tag_checkboxes) - 1
            self.tags_grid.addWidget(cb, idx // 3, idx % 3)
        else:
            self.tag_checkboxes[name].setChecked(True)
        self.new_tag_edit.clear()

    def _collect(self):
        return {
            "id": self.command["id"],
            "alias": self.alias_edit.text().strip(),
            "body": self.body_edit.toPlainText(),
            "tags": [t for t, cb in self.tag_checkboxes.items() if cb.isChecked()],
            "note": self.note_edit.toPlainText(),
            "starred": self.starred_cb.isChecked(),
            "readonly": self.readonly_cb.isChecked(),
            "envs": [env for env, cb in self.env_checkboxes.items() if cb.isChecked()],
        }

    def _on_save(self):
        alias = self.alias_edit.text().strip()
        if not alias:
            QMessageBox.warning(self, "缺少别名", "别名不能为空。")
            self.alias_edit.setFocus()
            return
        self.result_command = self._collect()
        self.action = "save"
        self.accept()

    def _on_delete(self):
        answer = QMessageBox.question(
            self, "删除命令", "确认删除「%s」？\n已发送的命令不受影响。" % self.command["alias"],
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.result_command = dict(self.command)
            self.action = "delete"
            self.accept()


class RiskDialog(QDialog):
    """高危命令二次确认：目标、完整命令、命中原因必须同时可见。"""

    def __init__(self, parent, host, text, labels, source=""):
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle("高危命令确认")
        self.resize(560, 330)
        self.choice = None
        root = QVBoxLayout(self)
        title = QLabel("即将发送到：%s  （%s@%s · %s）" % (
            host["name"], host["user"], host["host"], host.get("env", "未标注")))
        title.setObjectName("cardName")
        root.addWidget(title)
        why = QLabel("命中风险：%s%s" % ("、".join(labels) or "生产环境高危操作",
                                       "    来源：" + source if source else ""))
        why.setObjectName("field")
        root.addWidget(why)
        preview = QPlainTextEdit(text)
        preview.setObjectName("code")
        preview.setReadOnly(True)
        preview.setMinimumHeight(120)
        root.addWidget(preview)
        self.confirm_cb = None
        if host.get("env") == "生产":
            self.confirm_cb = QCheckBox("我确认在【生产】环境执行以上命令")
            root.addWidget(self.confirm_cb)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        fill = QPushButton("仅填入")
        fill.clicked.connect(lambda: self._choose("fill"))
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.send_btn = QPushButton("确认执行")
        self.send_btn.setProperty("role", "danger")
        self.send_btn.clicked.connect(lambda: self._choose("send"))
        for btn in (fill, cancel, self.send_btn):
            btn.setCursor(Qt.PointingHandCursor)
            buttons.addWidget(btn)
        root.addLayout(buttons)
        if self.confirm_cb is not None:
            self.send_btn.setEnabled(False)
            self.confirm_cb.stateChanged.connect(
                lambda: self.send_btn.setEnabled(self.confirm_cb.isChecked()))
            fill.setDefault(True)
        else:
            self.send_btn.setDefault(True)

    def _choose(self, choice):
        self.choice = choice
        self.accept()


class CommandPalette(QDialog):
    """Ctrl+K 命令面板：搜别名/命令体/备注/标签，收藏置顶，最近使用优先。"""

    def __init__(self, parent, commands, env, recent_ids):
        super().__init__(parent)
        self.setWindowTitle("快捷命令")
        self.resize(660, 460)
        self.commands, self.env = list(commands), env
        self.recent = {cid: -i for i, cid in enumerate(recent_ids)}
        self.result = None
        self.action = None
        root = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索别名 / 命令体 / 备注 / 标签…")
        root.addWidget(self.search)
        tag_row = QHBoxLayout()
        self.tag_checks = {}
        for tag in sorted({t for c in commands for t in c["tags"]}):
            cb = QCheckBox(tag)
            cb.stateChanged.connect(self._refresh)
            self.tag_checks[tag] = cb
            tag_row.addWidget(cb)
        tag_row.addStretch(1)
        root.addLayout(tag_row)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda: self._choose("send"))
        root.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(QLabel("↑↓ 选择 · Enter 发送 · Shift+Enter 填入"))
        buttons.addStretch(1)
        fill = QPushButton("填入")
        fill.clicked.connect(lambda: self._choose("fill"))
        send = QPushButton("发送")
        send.setProperty("role", "primary")
        send.clicked.connect(lambda: self._choose("send"))
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        for btn in (fill, send, cancel):
            btn.setCursor(Qt.PointingHandCursor)
            buttons.addWidget(btn)
        root.addLayout(buttons)
        self.search.textChanged.connect(self._refresh)
        self._refresh()
        self.search.setFocus()

    def _refresh(self):
        term = self.search.text().strip().lower()
        active_tags = {tag for tag, cb in self.tag_checks.items() if cb.isChecked()}
        items = []
        for cmd in self.commands:
            if not command_allowed(cmd, self.env):
                continue
            if active_tags and not active_tags.intersection(cmd["tags"]):
                continue
            haystack = " ".join([cmd["alias"], cmd["body"], cmd["note"]] + cmd["tags"]).lower()
            if term and term not in haystack:
                continue
            items.append(cmd)
        items.sort(key=lambda c: (not c.get("starred"), self.recent.get(c["id"], -999), c["alias"]),
                   reverse=False)
        items.sort(key=lambda c: self.recent.get(c["id"], -999), reverse=True)
        items.sort(key=lambda c: not c.get("starred"))
        self.list.clear()
        for cmd in items:
            mark = "★ " if cmd.get("starred") else "  "
            row = "%s%s    %s    [%s]" % (
                mark, cmd["alias"], cmd["body"].replace("\n", " ⏎ ")[:70],
                " ".join(cmd["tags"]))
            item = QListWidgetItem(row)
            item.setData(Qt.UserRole, cmd)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Down, Qt.Key_Up):
            row = self.list.currentRow()
            self.list.setCurrentRow(max(0, min(self.list.count() - 1,
                                               row + (1 if event.key() == Qt.Key_Down else -1))))
            return
        if event.key() == Qt.Key_Return:
            self._choose("fill" if event.modifiers() & Qt.ShiftModifier else "send")
            return
        super().keyPressEvent(event)

    def _choose(self, action):
        item = self.list.currentItem()
        if item is None:
            return
        self.result = item.data(Qt.UserRole)
        self.action = action
        self.accept()


class BatchDialog(QDialog):
    """批量扇出：默认只列只读命令，避免一次误操作打到多台机器。"""

    def __init__(self, parent, sessions, commands):
        super().__init__(parent)
        self.setWindowTitle("批量发送到多台会话")
        self.resize(480, 400)
        self.targets = []
        self.body = ""
        root = QVBoxLayout(self)
        root.addWidget(QLabel("勾选目标会话（仅已连接）"))
        self.checks = []
        for session in sessions:
            if session.state != "connected":
                continue
            cb = QCheckBox("%s  %s@%s · %s" % (
                session.host["name"], session.host["user"], session.host["host"],
                session.host.get("env", "未标注")))
            cb.setChecked(True)
            cb.setProperty("session_id", id(session))
            self.checks.append((session, cb))
            root.addWidget(cb)
        root.addWidget(QLabel("只读命令（可安全批量执行）"))
        self.combo = QComboBox()
        self.combo.addItem("— 自定义命令 —", None)
        for cmd in commands:
            if cmd.get("readonly") and command_allowed(cmd, ""):
                self.combo.addItem("%s  ·  %s" % (cmd["alias"], cmd["body"][:40]), cmd)
        root.addWidget(self.combo)
        self.custom = QPlainTextEdit()
        self.custom.setPlaceholderText("自定义命令（会走高危确认）")
        self.custom.setFixedHeight(70)
        root.addWidget(self.custom)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        send = QPushButton("发送到选中会话")
        send.setProperty("role", "primary")
        send.clicked.connect(self._accept)
        for btn in (cancel, send):
            btn.setCursor(Qt.PointingHandCursor)
            buttons.addWidget(btn)
        root.addLayout(buttons)

    def _accept(self):
        self.targets = [s for s, cb in self.checks if cb.isChecked()]
        cmd = self.combo.currentData()
        self.body = cmd["body"] if cmd else self.custom.toPlainText().strip()
        if not self.targets or not self.body:
            QMessageBox.warning(self, "批量发送", "请至少选择一台会话并输入命令。")
            return
        self.accept()


class SessionTab(QFrame):
    """左侧会话卡片：状态灯 + 名称 + 环境徽章 / user@host / 状态行。"""
    activated = Signal(object)
    reconnect_clicked = Signal(object)
    close_clicked = Signal(object)

    _STATE_TEXT = {
        "connected": "已连接",
        "connecting": "连接中…",
        "idle": "空闲已断开 · 点击连接",
        "disconnected": "未连接 · 点击连接",
    }

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.setObjectName("sessionCard")
        self.setProperty("active", "false")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("单击切换到此会话；状态灯点击重连；× 关闭会话")
        col = QVBoxLayout(self)
        col.setContentsMargins(11, 9, 9, 8)
        col.setSpacing(4)
        row1 = QHBoxLayout()
        row1.setSpacing(7)
        self.dot = QLabel()
        self.dot.setObjectName("dot")
        self.dot.setProperty("state", session.state)
        self.dot.setCursor(Qt.PointingHandCursor)
        self.dot.mousePressEvent = self._on_dot_clicked   # 灯点击单独处理，不触发卡片切换
        self.name = QLabel(session.host["name"])
        self.name.setObjectName("cardName")
        self.meta = QLabel("%s@%s" % (session.host["user"], session.host["host"]))
        self.meta.setObjectName("cardHost")
        env = session.host.get("env", "未标注")
        self.env_chip = QLabel(env)
        self.env_chip.setObjectName("envChip")
        self.env_chip.setProperty("env", env)
        close = QPushButton("×")
        close.setProperty("role", "mini")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(lambda: self.close_clicked.emit(session))
        row1.addWidget(self.dot)
        row1.addWidget(self.name)
        row1.addStretch(1)
        row1.addWidget(self.env_chip)
        row1.addWidget(close)
        self.state_label = QLabel(self._STATE_TEXT.get(session.state, session.state))
        self.state_label.setObjectName("cardState")
        self.state_label.setProperty("state", session.state)
        col.addLayout(row1)
        col.addWidget(self.meta)
        col.addWidget(self.state_label)

    def _on_dot_clicked(self, event):
        self.reconnect_clicked.emit(self.session)

    def set_active(self, active):
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_state(self, state):
        self.dot.setProperty("state", state)
        self.dot.setToolTip({"connected": "已连接", "connecting": "连接中",
                             "idle": "空闲已断开 · 点击重连",
                             "disconnected": "已断开 · 点击重连"}.get(state, state))
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)
        self.state_label.setText(self._STATE_TEXT.get(state, state))
        self.state_label.setProperty("state", state)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)
        self.name.setProperty("dim", "true" if state in ("idle", "disconnected") else "false")
        self.name.style().unpolish(self.name)
        self.name.style().polish(self.name)

    def set_elapsed(self, text):
        """连接时长后缀，由主窗口 _tick 每秒刷新。"""
        base = self._STATE_TEXT.get(self.session.state, self.session.state)
        if self.session.state == "connected":
            self.state_label.setText("已连接 %s" % text)
        else:
            self.state_label.setText(base)

    def mousePressEvent(self, event):
        self.activated.emit(self.session)
        super().mousePressEvent(event)


class FlowLayout(QLayout):
    """pill 横排自动换行，放进横向滚动区。"""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Horizontal

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), True)

    def sizeHint(self):
        return QSize(600, self._layout(QRect(0, 0, 600, 0), True))

    def minimumSize(self):
        return QSize(0, 0)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def _layout(self, rect, test_only):
        xpos, ypos, line_h = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            need_w, need_h = hint.width(), hint.height()
            if xpos + need_w > rect.right() and xpos > rect.x():
                xpos = rect.x()
                ypos += line_h + self._spacing
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(xpos, ypos, need_w, need_h))
            xpos += need_w + self._spacing
            line_h = max(line_h, need_h)
        return ypos - rect.y() + line_h


class HistoryEdit(QPlainTextEdit):
    """可编辑的多行输入区：Enter 发送、Shift+Enter 换行、↑/↓ 翻内存输入历史。

    快捷命令「填入」后 body 可能多行，因此必须用 QPlainTextEdit 承载；
    多行状态下方向键要留给文本编辑，历史改用 Ctrl+↑/↓。
    """

    returnPressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = []
        self.index = 0
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.textChanged.connect(self._resize_input)
        self._resize_input()

    def _resize_input(self):
        """单行时保持终端行高，填入多行命令时按需增高（上限 96px）。"""
        height = int(self.document().size().height()) + 14
        self.setFixedHeight(max(34, min(96, height)))

    # QLineEdit 风格的小接口，供主窗口与测试沿用
    def text(self):
        return self.toPlainText()

    def setText(self, value):
        self.setPlainText(value)
        self.setCursorPosition(len(value))

    def setCursorPosition(self, position):
        cursor = self.textCursor()
        cursor.setPosition(min(position, len(self.toPlainText())))
        self.setTextCursor(cursor)

    def keyPressEvent(self, event):
        modifiers = event.modifiers()
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (modifiers & Qt.ShiftModifier):
            self.returnPressed.emit()           # Enter 发送，不插入换行
            return
        if event.key() == Qt.Key_R and (modifiers & Qt.ControlModifier):
            self._search_history()
            return
        history_key = event.key() in (Qt.Key_Up, Qt.Key_Down)
        if history_key and self.history and ((modifiers & Qt.ControlModifier)
                                             or "\n" not in self.toPlainText()):
            if event.key() == Qt.Key_Up:
                self.index = max(0, self.index - 1)
            else:
                self.index = min(len(self.history), self.index + 1)
            self.setText(self.history[self.index] if self.index < len(self.history) else "")
            return
        super().keyPressEvent(event)

    def bind(self, history_list, draft):
        """切换会话时绑定到该会话的历史与草稿。"""
        self.history = history_list if history_list is not None else []
        self.index = len(self.history)
        self.setText(draft or "")

    def remember(self, text):
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.index = len(self.history)

    def _search_history(self):
        term, ok = QInputDialog.getText(self, "搜索输入历史", "关键词：")
        if not ok or not term.strip():
            return
        needle = term.strip().lower()
        for idx in range(len(self.history) - 1, -1, -1):
            if needle in self.history[idx].lower():
                self.index = idx
                self.setText(self.history[idx])
                return
        self.index = len(self.history)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class SSHConsole(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SSH 控制台")
        self.resize(1120, 760)
        self.setMinimumSize(840, 540)
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self.hosts, host_warnings = load_hosts()
        self.commands, cmd_warnings = load_commands()
        self.sessions = []                 # 全部活动会话（跨 tab 共享命令）
        self.tabs = {}                     # id(session) -> SessionTab
        self.views = {}                    # id(session) -> TerminalView
        self.active_id = None
        self.search_text = ""
        self.recent_ids = []                 # 命令面板「最近使用」（内存，不落盘）
        self.receipt_enabled = False         # 执行回执默认关，保持原始发送字节语义
        self.pending_receipts = {}           # token -> {session, text, source}
        self.pending_risk = None
        self.warnings = host_warnings + cmd_warnings
        if paramiko is None:
            self.warnings.append("未安装 paramiko：pip install paramiko 后重开")

        self._build_ui()
        self._refresh_pills()
        self._refresh_dock()
        self._tick()

        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(1000)
        self.idle_check = QTimer(self)
        self.idle_check.timeout.connect(self._check_idle)
        self.idle_check.start(10000)
        for key, slot in (("Ctrl+K", self._open_palette), ("Ctrl+F", self._find_in_terminal),
                          ("Ctrl+=", self._zoom_in), ("Ctrl+-", self._zoom_out)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(slot)
        if self.warnings:
            preview = "；".join(self.warnings[:2]) + ("…" if len(self.warnings) > 2 else "")
            self._status("配置容错：%d 处（%s）" % (len(self.warnings), preview), 15000)

    def _status(self, text, ms=5000):
        self.status_bar.showMessage(text, ms)

    # ---------------- 构建界面 ----------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 1. 顶部应用栏：状态点 + 标题 + 总览 + 新连接 / 管理
        appbar = QFrame()
        appbar.setObjectName("appbar")
        app_row = QHBoxLayout(appbar)
        app_row.setContentsMargins(14, 8, 14, 8)
        app_row.setSpacing(10)
        app_dot = QLabel()
        app_dot.setObjectName("appDot")
        app_title = QLabel("SSH 控制台")
        app_title.setObjectName("appTitle")
        self.app_summary = QLabel("0 个会话 · 0 台已连接")
        self.app_summary.setObjectName("appSummary")
        self.overview_label = QLabel("0/0 在线")
        self.overview_label.setObjectName("field")
        batch = QPushButton("批量…")
        batch.setProperty("role", "pill")
        batch.setCursor(Qt.PointingHandCursor)
        batch.setToolTip("把只读命令扇出到多台已连接会话")
        batch.clicked.connect(self._open_batch)
        new_session = QPushButton("＋ 新连接")
        new_session.setProperty("role", "pill")
        new_session.setCursor(Qt.PointingHandCursor)
        new_session.setToolTip("从 cfg/ssh_console_hosts.json 选择主机")
        new_session.clicked.connect(self._new_session_dialog)
        manage_btn = QPushButton("⚙ 管理")
        manage_btn.setProperty("role", "pill")
        manage_btn.setCursor(Qt.PointingHandCursor)
        manage_btn.setToolTip("增删改本工具独立的 SSH 快捷命令")
        manage_btn.clicked.connect(self._toggle_dock)
        app_row.addWidget(app_dot)
        app_row.addWidget(app_title)
        app_row.addWidget(self.app_summary)
        app_row.addStretch(1)
        app_row.addWidget(self.overview_label)
        app_row.addWidget(batch)
        app_row.addWidget(new_session)
        app_row.addWidget(manage_btn)
        root.addWidget(appbar)

        # 2. 主体：左侧会话列表 + 右侧工作区
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root.addLayout(body, 1)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(232)
        side_col = QVBoxLayout(sidebar)
        side_col.setContentsMargins(10, 10, 10, 10)
        side_col.setSpacing(7)
        side_head = QLabel("会 话")
        side_head.setObjectName("sideHeader")
        side_col.addWidget(side_head)
        card_scroll = QScrollArea()
        card_scroll.setWidgetResizable(True)
        card_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        card_host = QWidget()
        card_host.setStyleSheet("background: transparent;")
        self.tabs_layout = QVBoxLayout(card_host)
        self.tabs_layout.setContentsMargins(0, 0, 0, 0)
        self.tabs_layout.setSpacing(7)
        self.tabs_layout.addStretch(1)
        card_scroll.setWidget(card_host)
        side_col.addWidget(card_scroll, 1)

        quick_head = QHBoxLayout()
        quick_head.setContentsMargins(4, 6, 4, 2)
        quick_label = QLabel("快捷命令")
        quick_label.setObjectName("sideHeader")
        manage_link = QPushButton("管理 ✎")
        manage_link.setProperty("role", "link")
        manage_link.setCursor(Qt.PointingHandCursor)
        manage_link.clicked.connect(self._toggle_dock)
        quick_head.addWidget(quick_label)
        quick_head.addStretch(1)
        quick_head.addWidget(manage_link)
        side_col.addLayout(quick_head)
        self.sidebar_tags_layout = QVBoxLayout()
        self.sidebar_tags_layout.setContentsMargins(0, 0, 0, 0)
        self.sidebar_tags_layout.setSpacing(3)
        side_col.addLayout(self.sidebar_tags_layout)
        body.addWidget(sidebar)

        right = QWidget()
        right_col = QVBoxLayout(right)
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(0)
        body.addWidget(right, 1)

        # 2.1 会话头：状态灯 + 名称 + 元信息 + 操作链接
        header = QFrame()
        header.setObjectName("sessionHeader")
        head_row = QHBoxLayout(header)
        head_row.setContentsMargins(16, 8, 12, 8)
        head_row.setSpacing(10)
        self.header_dot = QLabel()
        self.header_dot.setObjectName("dot")
        self.header_name = QLabel("未连接")
        self.header_name.setObjectName("headerName")
        self.header_meta = QLabel("点击「＋ 新连接」开始")
        self.header_meta.setObjectName("headerMeta")
        self.header_meta.setFont(QFont("Consolas", 9))
        head_row.addWidget(self.header_dot)
        head_row.addWidget(self.header_name)
        head_row.addWidget(self.header_meta)
        head_row.addStretch(1)
        for label, tip, slot, tone in (
            ("🔍 查找", "Ctrl+F 在当前会话输出中查找", self._find_in_terminal, ""),
            ("⤓ 导出", "导出当前会话输出（导出前提示脱敏）", self._export_terminal, ""),
            ("⏸ 续期", "重置空闲计时，避免长任务被掐断", self._keep_alive, ""),
            ("🖥 外部终端", "用系统 ssh 客户端打开完整终端（全屏程序兜底）", self._open_external_terminal, ""),
            ("✕ 断开", "断开当前会话（可点击状态灯重连）", self._disconnect_active, "danger"),
        ):
            btn = QPushButton(label)
            btn.setProperty("role", "link")
            if tone:
                btn.setProperty("tone", tone)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            head_row.addWidget(btn)
        right_col.addWidget(header)

        # 2.2 终端区（每会话一个 QTextEdit，切换保留历史）
        self.stack = QStackedWidget()
        right_col.addWidget(self.stack, 1)
        empty = QLabel("点击左上角「＋ 新连接」连接远程主机\n快捷命令会自动发送到当前活动会话")
        empty.setAlignment(Qt.AlignCenter)
        empty.setObjectName("field")
        self.empty_index = self.stack.addWidget(empty)
        self.stack.setCurrentIndex(self.empty_index)

        # 2.3 快捷命令抽屉：折叠 pill 行 + 可展开的分组面板
        drawer = QFrame()
        drawer.setObjectName("drawer")
        drawer_row = QHBoxLayout(drawer)
        drawer_row.setContentsMargins(16, 6, 12, 6)
        drawer_row.setSpacing(8)
        drawer_label = QLabel("快捷命令")
        drawer_label.setObjectName("quickLabel")
        drawer_row.addWidget(drawer_label)
        self.drawer_pills_host = QWidget()
        self.drawer_pills_host.setStyleSheet("background: transparent;")
        self.drawer_pills_layout = QHBoxLayout(self.drawer_pills_host)
        self.drawer_pills_layout.setContentsMargins(0, 0, 0, 0)
        self.drawer_pills_layout.setSpacing(6)
        drawer_row.addWidget(self.drawer_pills_host, 1)
        self.drawer_toggle = QPushButton("▾ 展开")
        self.drawer_toggle.setProperty("role", "link")
        self.drawer_toggle.setCursor(Qt.PointingHandCursor)
        self.drawer_toggle.setToolTip("展开全部命令（按标签分组，可搜索）")
        self.drawer_toggle.clicked.connect(self._toggle_drawer)
        drawer_row.addWidget(self.drawer_toggle)
        right_col.addWidget(drawer)

        self.drawer_panel = QFrame()
        self.drawer_panel.setObjectName("drawerPanel")
        panel_col = QVBoxLayout(self.drawer_panel)
        panel_col.setContentsMargins(16, 8, 16, 10)
        panel_col.setSpacing(7)
        panel_top = QHBoxLayout()
        panel_top.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("search")
        self.search_edit.setPlaceholderText("搜索别名 / 命令体 / 备注…")
        self.search_edit.textChanged.connect(self._on_search)
        palette_btn = QPushButton("命令面板 ⌘K")
        palette_btn.setProperty("role", "pill")
        palette_btn.setCursor(Qt.PointingHandCursor)
        palette_btn.setToolTip("Ctrl+K：搜别名/命令体/最近使用/收藏")
        palette_btn.clicked.connect(self._open_palette)
        panel_top.addWidget(self.search_edit, 1)
        panel_top.addWidget(palette_btn)
        panel_col.addLayout(panel_top)
        pill_scroll = QScrollArea()
        pill_scroll.setWidgetResizable(True)
        pill_scroll.setMinimumHeight(110)
        pill_scroll.setMaximumHeight(220)
        pill_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        pill_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.pill_host = QWidget()
        self.pills_layout = FlowLayout(self.pill_host)
        pill_scroll.setWidget(self.pill_host)
        panel_col.addWidget(pill_scroll)
        self.drawer_panel.hide()
        right_col.addWidget(self.drawer_panel)

        # 2.4 输入区：提示符 + 输入框 + 发送；下方提示行
        input_bar = QFrame()
        input_bar.setObjectName("inputBar")
        input_col = QVBoxLayout(input_bar)
        input_col.setContentsMargins(16, 10, 16, 10)
        input_col.setSpacing(6)
        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        self.prompt_label = QLabel("$ ")
        self.prompt_label.setObjectName("prompt")
        self.prompt_label.setFont(QFont("Consolas", 10))
        self.input_edit = HistoryEdit()
        self.input_edit.setObjectName("input")
        self.input_edit.setPlaceholderText("输入命令，回车发送 · Shift+Enter 换行编辑")
        self.input_edit.setToolTip("回车发送；Shift+Enter 换行；↑/↓ 翻历史（多行时 Ctrl+↑/↓）")
        self.input_edit.returnPressed.connect(self._send_typed)
        send_btn = QPushButton("发送 ⏎")
        send_btn.setProperty("role", "primary")
        send_btn.clicked.connect(self._send_typed)
        input_row.addWidget(self.prompt_label)
        input_row.addWidget(self.input_edit, 1)
        input_row.addWidget(send_btn)
        input_col.addLayout(input_row)

        hint_row = QHBoxLayout()
        hint_row.setSpacing(10)
        self.risk_hint = QLabel("")
        self.risk_hint.setObjectName("riskHint")
        self.target_chip = QLabel("→ 未连接")
        self.target_chip.setObjectName("targetChip")
        self.pin_btn = QPushButton("钉住")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setProperty("role", "link")
        self.pin_btn.setProperty("tone", "dim")
        self.pin_btn.setToolTip("钉住后该会话豁免空闲自动断开")
        self.pin_btn.toggled.connect(self._toggle_pin)
        self.receipt_cb = QCheckBox("执行回执")
        self.receipt_cb.setToolTip("开启后给单行命令加退出码/耗时标记，回显仍显示原命令")
        self.receipt_cb.toggled.connect(self._toggle_receipt)
        hint_row.addWidget(self.risk_hint)
        hint_row.addStretch(1)
        for label, tip, slot, tone in (
            ("⌃C 中断", "发送 \\x03 终止远端前台进程", lambda: self._send_control("\x03", "Ctrl+C"), ""),
            ("Ctrl+D", "发送 \\x04 会退出 shell，需确认", self._ctrl_d_clicked, "danger"),
            ("Tab 补全", "发送 \\t 补全", lambda: self._send_control("\t", "Tab"), ""),
            ("清屏", "只清本地显示区，不影响远端", self._clear_display, ""),
        ):
            btn = QPushButton(label)
            btn.setProperty("role", "link")
            if tone:
                btn.setProperty("tone", tone)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            hint_row.addWidget(btn)
        hint_row.addWidget(self.target_chip)
        hint_row.addWidget(self.pin_btn)
        hint_row.addWidget(self.receipt_cb)
        input_col.addLayout(hint_row)
        right_col.addWidget(input_bar)

        self.safety_bar = QFrame()
        self.safety_bar.setObjectName("safetyBar")
        safety_row = QHBoxLayout(self.safety_bar)
        safety_row.setContentsMargins(16, 6, 16, 6)
        self.safety_text = QLabel("")
        self.safety_text.setObjectName("safetyText")
        risk_fill = QPushButton("仅填入")
        risk_fill.clicked.connect(self._safety_fill)
        risk_send = QPushButton("确认执行")
        self.risk_send_btn = risk_send
        risk_send.setProperty("role", "danger")
        risk_send.clicked.connect(self._safety_send)
        risk_cancel = QPushButton("取消")
        risk_cancel.clicked.connect(self._safety_cancel)
        self.safety_confirm = QCheckBox("确认生产环境")
        self.safety_confirm.hide()
        self.safety_confirm.stateChanged.connect(
            lambda: risk_send.setEnabled(not self.safety_confirm.isVisible()
                                         or self.safety_confirm.isChecked()))
        safety_row.addWidget(self.safety_text, 1)
        safety_row.addWidget(self.safety_confirm)
        for btn in (risk_fill, risk_send, risk_cancel):
            btn.setCursor(Qt.PointingHandCursor)
            safety_row.addWidget(btn)
        self.safety_bar.hide()
        right_col.addWidget(self.safety_bar)

        self._build_dock()

    def _disconnect_active(self):
        session = self._active_session()
        if session is None or session.state not in ("connected", "connecting"):
            self._status("当前没有已连接的会话", 3000)
            return
        session.disconnect("手动断开", state="disconnected")
        self._status("已断开 %s（点状态灯重连）" % session.host["name"], 6000)

    def _toggle_drawer(self):
        expanded = not self.drawer_panel.isVisible()
        self.drawer_panel.setVisible(expanded)
        self.drawer_toggle.setText("▴ 收起" if expanded else "▾ 展开")

    def _refresh_sidebar_tags(self):
        """左侧标签分组（🗂 日志 12 形态）：点击按该标签过滤并展开抽屉。"""
        while self.sidebar_tags_layout.count():
            item = self.sidebar_tags_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(self)
                widget.deleteLater()
        groups = {}
        for cmd in self._visible_commands():
            groups.setdefault(cmd["tags"][0] if cmd["tags"] else "其他", []).append(cmd)
        for tag in sorted(groups):
            btn = QPushButton("🗂 %s  %d" % (tag, len(groups[tag])))
            btn.setObjectName("sideTagBtn")
            btn.setCheckable(True)
            btn.setChecked(self.search_text == tag.lower())
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip("按「%s」过滤快捷命令" % tag)
            btn.clicked.connect(lambda checked=False, t=tag: self._filter_by_tag(t, checked))
            self.sidebar_tags_layout.addWidget(btn)
        self.sidebar_tags_layout.addStretch(1)

    def _filter_by_tag(self, tag, checked):
        if checked:
            self.search_edit.setText(tag)
            if not self.drawer_panel.isVisible():
                self._toggle_drawer()
        else:
            self.search_edit.clear()

    def _build_dock(self):
        self.dock = QDockWidget("快捷命令管理", self)
        self.dock.setObjectName("commandDock")
        content = QWidget()
        dock_root = QVBoxLayout(content)
        dock_root.setContentsMargins(8, 8, 8, 8)
        dock_root.setSpacing(6)
        tip = QLabel("本工具独立命令库：%s" % COMMANDS_PATH.name)
        tip.setObjectName("field")
        dock_root.addWidget(tip)
        self.dock_list = QListWidget()
        self.dock_list.itemDoubleClicked.connect(lambda: self._send_dock_item())
        dock_root.addWidget(self.dock_list, 1)
        row = QHBoxLayout()
        for label, slot in (
            ("新建", self._add_command), ("编辑", self._edit_command),
            ("删除", self._delete_command), ("发送", self._send_dock_item),
            ("填入", self._fill_dock_item),
        ):
            btn = QPushButton(label)
            btn.setProperty("role", "pill")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        dock_root.addLayout(row)
        self.dock.setWidget(content)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.hide()

    def _toggle_dock(self):
        self.dock.setVisible(not self.dock.isVisible())
        if self.dock.isVisible():
            self._refresh_dock()

    # ---------------- pill / dock ----------------
    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 先隐藏再挂回父窗口：直接 setParent(None) 会短暂变成顶层窗口闪一下
                widget.hide()
                widget.setParent(self)
                widget.deleteLater()

    def _on_search(self, text):
        self.search_text = text.strip().lower()
        self._refresh_pills()
        self._refresh_sidebar_tags()

    def _visible_commands(self):
        env = self._active_host().get("env", "")
        items = [c for c in self.commands if command_allowed(c, env)]
        if not self.search_text:
            return items
        return [c for c in items
                if any(self.search_text in str(value).lower() for value in (
                    c["alias"], c["body"], c["note"], " ".join(c["tags"])))]

    def _make_pill(self, cmd):
        pill = QPushButton(cmd["alias"] + (" ⚠" if risk_labels(cmd["body"]) else ""))
        pill.setProperty("role", "pill")
        pill.setProperty("tone", "risk" if risk_labels(cmd["body"]) else str(tag_tone(cmd["tags"][0] if cmd["tags"] else "其他")))
        pill.setCursor(Qt.PointingHandCursor)
        pill.setToolTip("点击直接发送：\n%s%s" % (
            cmd["body"], "\n备注：" + cmd["note"] if cmd["note"] else ""))
        pill.clicked.connect(lambda checked=False, c=cmd: self._send_command(c))
        return pill

    def _refresh_pills(self):
        self._clear_layout(self.pills_layout)
        quick_label = QLabel("快捷命令")
        quick_label.setObjectName("quickLabel")
        self.pills_layout.addWidget(quick_label)
        groups = {}
        for cmd in self._visible_commands():
            groups.setdefault(cmd["tags"][0] if cmd["tags"] else "其他", []).append(cmd)
        ordered = [cmd for tag in sorted(groups) for cmd in groups[tag]]
        for tag in sorted(groups):
            head = QLabel(tag)
            head.setObjectName("pillGroup")
            head.setProperty("role", "pill")
            head.setProperty("tone", str(tag_tone(tag)))
            head.setFixedWidth(46)
            self.pills_layout.addWidget(head)
            for cmd in groups[tag]:
                self.pills_layout.addWidget(self._make_pill(cmd))
                # 每条命令配一个「填入」按钮：把 body 放进输入区，改完再回车发送
                fill = QPushButton("填入")
                fill.setProperty("role", "fill")
                fill.setCursor(Qt.PointingHandCursor)
                fill.setToolTip("填入输入区编辑后再发送：\n%s" % cmd["body"])
                fill.clicked.connect(lambda checked=False, c=cmd: self._fill_command(c))
                self.pills_layout.addWidget(fill)
        self.pill_host.updateGeometry()

        # 折叠态抽屉行：各标签组前几条，一眼可点；展开态走上方分组面板
        self._clear_layout(self.drawer_pills_layout)
        for cmd in ordered[:8]:
            self.drawer_pills_layout.addWidget(self._make_pill(cmd))
        more = len(ordered) - 8
        if more > 0:
            tail = QPushButton("…+%d" % more)
            tail.setProperty("role", "pill")
            tail.setProperty("tone", "dim")
            tail.setCursor(Qt.PointingHandCursor)
            tail.clicked.connect(self._toggle_drawer)
            self.drawer_pills_layout.addWidget(tail)
        self.drawer_toggle.setText(
            ("▴ 收起" if self.drawer_panel.isVisible() else "▾ 展开（%d 条，按标签分组）" % len(ordered))
            if ordered else "▾ 展开")
        self.drawer_pills_host.updateGeometry()
        self._refresh_sidebar_tags()

    def _refresh_dock(self):
        self.dock_list.clear()
        for cmd in self.commands:
            item = QListWidgetItem("%s     %s" % (cmd["alias"], cmd["tags"][0] if cmd["tags"] else ""))
            item.setData(Qt.UserRole, cmd)
            self.dock_list.addItem(item)
        if self.commands and self.dock_list.currentRow() < 0:
            self.dock_list.setCurrentRow(0)

    def _current_dock_command(self):
        item = self.dock_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _persist_commands(self):
        err = save_json(COMMANDS_PATH, {"version": 1, "commands": self.commands})
        if err:
            self._status("命令库保存失败：%s" % err, 10000)
            return False
        self._refresh_pills()
        self._refresh_dock()
        return True

    def _command_dialog(self, cmd=None):
        preset = cmd or {"id": unique_id(self.commands), "alias": "", "body": "", "tags": [], "note": ""}
        all_tags = sorted({t for c in self.commands for t in c["tags"]})
        dlg = SshCommandDialog(self, preset, all_tags, is_new=cmd is None)
        if dlg.exec() != QDialog.Accepted:
            return
        result = dlg.result_command
        if dlg.action == "delete":
            self.commands = [c for c in self.commands if c["id"] != result["id"]]
        else:
            hit = next((c for c in self.commands if c["id"] == result["id"]), None)
            if hit is None:
                self.commands.append(result)
            else:
                hit.update(result)
        self._persist_commands()
        self._status("快捷命令已%s" % ("删除" if dlg.action == "delete" else "保存"))

    def _add_command(self):
        self._command_dialog()

    def _edit_command(self):
        cmd = self._current_dock_command()
        if cmd is None:
            QMessageBox.information(self, "编辑命令", "请先选中一条命令。")
            return
        self._command_dialog(cmd)

    def _delete_command(self):
        cmd = self._current_dock_command()
        if cmd is None:
            QMessageBox.information(self, "删除命令", "请先选中一条命令。")
            return
        self._command_dialog(cmd)

    def _send_dock_item(self):
        cmd = self._current_dock_command()
        if cmd:
            self._send_command(cmd)

    def _fill_dock_item(self):
        cmd = self._current_dock_command()
        if cmd:
            self._fill_command(cmd)

    def _fill_command(self, cmd):
        """把命令体放进主输入区（不发送），光标落末尾方便直接改参数。"""
        self._fill_text(expand_template(cmd["body"], self._active_host()), cmd["alias"])
        self._remember_command(cmd["id"])

    def _fill_text(self, text, source="命令"):
        self.input_edit.setText(text)
        self.input_edit.setCursorPosition(len(text))
        self.input_edit.setFocus()
        session = self._active_session()
        if session is not None:
            session.draft = text
        self._status("已填入「%s」到输入区，编辑后回车发送" % source)

    def _active_host(self):
        session = self._active_session()
        return session.host if session is not None else {}

    # ---------------- 会话 ----------------
    def _active_session(self):
        for s in self.sessions:
            if id(s) == self.active_id:
                return s
        return None

    def _new_session_dialog(self):
        connected = {h["id"] for h in self.hosts for s in self.sessions
                     if s.host["id"] == h["id"] and s.state in ("connecting", "connected")}
        dlg = HostDialog(self, self.hosts, connected)
        if dlg.exec() != QDialog.Accepted or dlg.selected is None:
            return
        host = dlg.selected
        existing = next((s for s in self.sessions if s.host["id"] == host["id"]), None)
        if existing is not None:
            self._activate(existing)
            self._status("已切换到现有会话：%s" % host["name"])
            return
        self._create_session(host)

    def _create_session(self, host):
        session = Session(host, self)
        view = TerminalView()
        tab = SessionTab(session)
        self.sessions.append(session)
        self.views[id(session)] = view
        self.tabs[id(session)] = tab
        self.tabs_layout.insertWidget(self.tabs_layout.count() - 1, tab)  # 保持在 stretch 之前
        view_index = self.stack.addWidget(view)
        view.setProperty("view_index", view_index)
        session.data_received.connect(lambda s, data: self._on_data(s, data), Qt.QueuedConnection)
        view.receipt_ready.connect(
            lambda token, code, elapsed, s=session: self._on_receipt(s, token, code, elapsed),
            Qt.QueuedConnection)
        session.state_changed.connect(self._on_state, Qt.QueuedConnection)
        session.password_needed.connect(self._on_password, Qt.QueuedConnection)
        tab.activated.connect(self._activate)
        tab.reconnect_clicked.connect(self._reconnect)
        tab.close_clicked.connect(self._close_session)
        self._activate(session)
        session.connect()
        self._status("正在连接 %s …" % session.target)

    def _activate(self, session):
        if session is None or id(session) not in self.tabs:
            return
        previous = self._active_session()
        if previous is not None and previous is not session:
            previous.draft = self.input_edit.text()   # 草稿留在原会话，绝不跟人跑
        self.active_id = id(session)
        for sid, tab in self.tabs.items():
            tab.set_active(sid == self.active_id)
        self.stack.setCurrentIndex(self.views[self.active_id].property("view_index"))
        self.prompt_label.setText("%s@%s:~$ " % (session.host["user"], session.host["host"]))
        self.prompt_label.setStyleSheet("color: #3ddc97;")
        self.target_chip.setText("→ %s" % session.host["name"])
        self.header_dot.setProperty("state", session.state)
        self.header_dot.style().unpolish(self.header_dot)
        self.header_dot.style().polish(self.header_dot)
        self.header_name.setText(session.host["name"])
        self.header_meta.setText("%s · %s · invoke_shell · 200×50" % (
            session.target, session.host.get("env", "未标注")))
        self.input_edit.bind(session.history, session.draft)
        self.pin_btn.setChecked(session.pinned)
        self._refresh_pills()
        self._tick()

    def _reconnect(self, session, auto=False):
        if session.state in ("connecting", "connected"):
            self._status("%s 已在连接中" % session.host["name"])
            return
        if auto:
            # 断线后用内存里的密码自动重试一次；再失败才走密码框
            session.connect(password=session.password, auto_retry=True)
        else:
            session.auto_retry_used = False
            session.connect()
        self._status("正在重连 %s …" % session.target)

    def _close_session(self, session):
        if session is None:
            return
        session.close()
        tab = self.tabs.pop(id(session), None)
        view = self.views.pop(id(session), None)
        if tab is not None:
            self.tabs_layout.removeWidget(tab)
            tab.hide()
            tab.setParent(self)
            tab.deleteLater()
        if view is not None:
            self.stack.removeWidget(view)
            view.hide()
            view.setParent(self)
            view.deleteLater()
        if session in self.sessions:
            self.sessions.remove(session)
        session.deleteLater()
        if self.active_id == id(session):
            self.active_id = None
            if self.sessions:
                self._activate(self.sessions[-1])
            else:
                self.stack.setCurrentIndex(self.empty_index)
                self.status_bar.clearMessage()
        if self.sessions:
            self._status("会话已关闭：%s" % session.host["name"])

    def _on_state(self, session, state, reason):
        tab = self.tabs.get(id(session))
        if tab is not None:
            tab.set_state(state)
        if session is self._active_session():
            self.header_dot.setProperty("state", state)
            self.header_dot.style().unpolish(self.header_dot)
            self.header_dot.style().polish(self.header_dot)
        view = self.views.get(id(session))
        if state == "connected":
            _log_event("会话建立 · %s · %s" % (session.host["name"], session.target))
            rotate_log_if_needed()
            self._status("已连接 %s · %s" % (session.host["name"], session.target))
        elif state in ("disconnected", "idle"):
            if view is not None and reason:
                view.append_system("%s · %s" % (state == "idle" and "空闲自动断开" or "连接断开", reason),
                                   "#f87171")
            _log_event("断开 · %s · %s · %s" % (
                session.host["name"], session.target, reason or "远端关闭"))
            rotate_log_if_needed()             # 收尾 flush 后静默轮转
            session.close_resources()
            # 密码型会话意外断线：静默重试一次，失败后 worker 会发密码框信号
            if (session.password and not session.auto_retry_used and state == "disconnected"
                    and ("连接中断" in reason or "远端关闭" in reason or "发送失败" in reason)):
                QTimer.singleShot(800, lambda s=session: self._reconnect(s, auto=True))
            self._status("%s：%s" % (session.host["name"], reason or "已断开"), 8000)
        else:
            self._status("连接中 %s …" % session.target)
        self._tick()

    def _on_password(self, session):
        pwd, ok = QInputDialog.getText(
            self, "SSH 密码", "%s@%s 的密码：" % (session.host["user"], session.host["host"]),
            QLineEdit.Password,
        )
        if ok and pwd:
            session.auto_retry_used = False
            session.connect(password=pwd)
        else:
            session._set_state("disconnected", "已取消密码输入")

    def _on_data(self, session, data):
        view = self.views.get(id(session))
        if view is not None:
            view.append_bytes(data)

    # ---------------- 发送 ----------------
    def _send_command(self, cmd):
        session = self._active_session()
        if session is None:
            self._status("没有活动会话，请先「＋ 会话」", 3000)
            return
        if session.state != "connected":
            self._status("当前会话未连接（%s）" % session.state, 3000)
            return
        body = expand_template(cmd["body"], session.host)
        if not self._risk_gate(session, body, cmd["alias"]):
            return
        self._remember_command(cmd["id"])
        lines = body.splitlines()
        if self.receipt_enabled and len(lines) == 1 and lines[0].strip():
            self._send_receipt(session, lines[0], "快捷命令 · %s" % cmd["alias"])
        else:
            _log_event("> 快捷命令 · %s" % cmd["alias"])
            for line in lines:
                if session.send_text(line + "\n"):
                    _log_event("> " + line)
        self.input_edit.setFocus()
        self._status("已发送「%s」→ %s" % (cmd["alias"], session.host["name"]))

    def _send_typed(self):
        text = self.input_edit.text()
        session = self._active_session()
        if session is None or session.state != "connected":
            self._status("当前没有已连接的会话", 3000)
            return
        if not text:
            return
        text = expand_template(text, session.host)
        if not self._risk_gate(session, text, "输入区"):
            return
        self.input_edit.remember(text)
        lines = text.splitlines()
        if self.receipt_enabled and len(lines) == 1 and lines[0].strip():
            self._send_receipt(session, lines[0], "输入命令")
        elif session.send_text(text + "\n"):
            _log_event("> " + text.replace("\r", " ").replace("\n", " "))
        session.draft = ""
        self.input_edit.clear()
        self.input_edit.setFocus()

    # ---------------- 风险拦截与执行回执 ----------------
    def _remember_command(self, cid):
        if cid in self.recent_ids:
            self.recent_ids.remove(cid)
        self.recent_ids.insert(0, cid)
        self.recent_ids = self.recent_ids[:30]

    def _risk_gate(self, session, text, source, cmd_id=None):
        """高危命令先拦截，返回 True 表示可以直接发送。"""
        labels = risk_labels(text)
        if not labels:
            return True
        self.pending_risk = {"session": session, "text": text, "source": source,
                             "labels": labels, "cmd_id": cmd_id}
        prod = session.host.get("env") == "生产"
        self.safety_text.setText("高危命令（%s）· 目标 %s（%s）· 来源 %s" % (
            "、".join(labels), session.host["name"], session.host.get("env", "未标注"), source))
        self.risk_hint.setText("⚠ 高危命令（%s）待确认" % "、".join(labels))
        self.safety_confirm.setVisible(prod)
        self.safety_confirm.setChecked(False)
        self.risk_send_btn.setEnabled(not prod)   # 生产必须重新勾选，不能沿用上次状态
        self.safety_bar.show()
        self._status("高危命令已拦截：确认后才会发送", 10000)
        return False

    def _safety_fill(self):
        pending = self.pending_risk
        if pending is None:
            return
        self.safety_bar.hide()
        self.risk_hint.setText("")
        self.pending_risk = None
        self.input_edit.setText(pending["text"])
        self.input_edit.setFocus()
        session = pending["session"]
        session.draft = self.input_edit.text()
        self._status("已填入输入区，未发送")

    def _safety_send(self):
        pending = self.pending_risk
        if pending is None:
            return
        if self.safety_confirm.isVisible() and not self.safety_confirm.isChecked():
            return
        session, text = pending["session"], pending["text"]
        if session.state != "connected":
            self._status("目标会话已断开，未发送", 5000)
            return
        self.safety_bar.hide()
        self.risk_hint.setText("")
        self.pending_risk = None
        _log_event("> 高危命令已确认 · %s · %s" % (session.host["name"], text.replace("\n", " ")))
        lines = text.splitlines()
        if self.receipt_enabled and len(lines) == 1 and lines[0].strip():
            if pending["cmd_id"]:
                self._remember_command(pending["cmd_id"])
            self._send_receipt(session, lines[0], "高危命令 · %s" % pending["source"])
        else:
            for line in lines:
                if session.send_text(line + "\n"):
                    _log_event("> " + line)
        if pending["cmd_id"]:
            self._remember_command(pending["cmd_id"])
        else:
            session.history.append(text)
            self.input_edit.clear()
        self._status("已确认发送 → %s" % session.host["name"])

    def _safety_cancel(self):
        self.safety_bar.hide()
        self.risk_hint.setText("")
        self.pending_risk = None
        self.input_edit.setFocus()
        self._status("已取消高危命令")

    def _send_receipt(self, session, command, source):
        """单行命令的执行回执：包装命令的回显会被终端视图还原成原命令。"""
        token = uuid.uuid4().hex[:8]
        view = self.views.get(id(session))
        if view is None:
            return
        prompt = "%s@%s:~$ " % (session.host["user"], session.host["host"])
        view.register_receipt(token, prompt + command)
        wrapped = ("printf '\\001CPB %s\\002\\n'; %s; __cp_rc=$?; "
                   "printf '\\001CPE %s %%d\\002\\n' \"$__cp_rc\"" % (token, command, token))
        if session.send_text(wrapped + "\n"):
            self.pending_receipts[token] = {"session": session, "text": command, "source": source}
            _log_event("━ 启动 · %s · %s · %s" % (session.host["name"], source, command))

    def _on_receipt(self, session, token, code, elapsed):
        info = self.pending_receipts.pop(token, None)
        if info is None:
            return
        view = self.views.get(id(session))
        if view is not None:
            if code == 0:
                view.append_system("退出码 0 · 耗时 %.1fs" % elapsed, "#34d399")
            elif code == -1:
                view.append_system("已中断 · 无退出码 · 耗时 %.1fs" % elapsed, "#f87171")
            else:
                view.append_system("退出码 %d · 耗时 %.1fs" % (code, elapsed), "#f87171")
        _log_event("━ 结束 · %s · 退出码 %s · 耗时 %.1fs" % (
            session.host["name"], code if code >= 0 else "无", elapsed))

    def _toggle_receipt(self, checked):
        self.receipt_enabled = checked
        self._status("执行回执已%s（仅影响单行命令发送）" % ("开启" if checked else "关闭"))

    def _send_control(self, raw, label):
        session = self._active_session()
        if session is None or session.state != "connected":
            self._status("当前没有已连接的会话", 3000)
            return
        if session.send_text(raw):
            if raw == "\x03":
                for token in [t for t, info in self.pending_receipts.items()
                              if info["session"] is session]:
                    view = self.views.get(id(session))
                    if view is not None:
                        view.cancel_receipt(token, "已中断")
                    self.pending_receipts.pop(token, None)
            _log_event("> " + label)
            self._status("已发送 %s → %s" % (label, session.host["name"]))

    def _clear_display(self):
        session = self._active_session()
        view = self.views.get(id(session)) if session else None
        if view is not None:
            view.clear_display()
            self._status("本地显示区已清屏（远端会话未中断）")

    # ---------------- 命令面板 / 查找 / 导出 / 批量 ----------------
    def _open_palette(self):
        env = self._active_host().get("env", "")
        dlg = CommandPalette(self, self.commands, env, self.recent_ids)
        if dlg.exec() != QDialog.Accepted or dlg.result is None:
            return
        cmd = dlg.result
        if dlg.action == "fill":
            self._fill_command(cmd)
        else:
            self._send_command(cmd)

    def _ctrl_d_clicked(self):
        session = self._active_session()
        if session is None or session.state != "connected":
            self._status("当前没有已连接的会话", 3000)
            return
        dlg = RiskDialog(self, session.host, "Ctrl+D（EOF）将退出远端 shell",
                         ["结束会话"], "控制条")
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.choice == "fill":
            self._fill_text("exit", "Ctrl+D 对应 exit")
            return
        self._send_control("\x04", "Ctrl+D")

    def _find_in_terminal(self):
        session = self._active_session()
        view = self.views.get(id(session)) if session else None
        if view is None:
            self._status("没有活动会话可查找", 3000)
            return
        term, ok = QInputDialog.getText(self, "查找输出", "关键词（留空清除高亮）：")
        if ok:
            view.set_find_term(term.strip())
            self._status("已高亮「%s」" % term.strip() if term.strip() else "已清除查找高亮")

    def _export_terminal(self):
        session = self._active_session()
        view = self.views.get(id(session)) if session else None
        if view is None:
            self._status("没有活动会话可导出", 3000)
            return
        answer = QMessageBox.question(
            self, "导出输出", "终端内容可能含密码、令牌等敏感信息。\n确认导出到本地文件？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        default = "%s_%s.txt" % (session.host["name"], datetime.now().strftime("%Y%m%d_%H%M%S"))
        path, _ = QFileDialog.getSaveFileName(self, "导出终端输出", default, "文本文件 (*.txt)")
        if not path:
            return
        try:
            Path(path).write_text(view.export_text(), encoding="utf-8")
            self._status("已导出：%s" % path)
        except Exception as exc:
            self._status("导出失败：%s" % exc, 8000)

    def _zoom_in(self):
        for view in self.views.values():
            view.apply_zoom(1)
        self._status("终端字号已放大")

    def _zoom_out(self):
        for view in self.views.values():
            view.apply_zoom(-1)
        self._status("终端字号已缩小")

    def _open_batch(self):
        connected = [s for s in self.sessions if s.state == "connected"]
        if not connected:
            self._status("没有已连接的会话可批量发送", 4000)
            return
        dlg = BatchDialog(self, connected, self.commands)
        if dlg.exec() != QDialog.Accepted:
            return
        risks = risk_labels(dlg.body)
        if risks:
            first = dlg.targets[0]
            confirm = RiskDialog(self, first, dlg.body, risks, "批量发送（%d 台）" % len(dlg.targets))
            if confirm.exec() != QDialog.Accepted or confirm.choice != "send":
                return
        for session in dlg.targets:
            text = expand_template(dlg.body, session.host)
            if session.send_text(text + "\n"):
                _log_event("> 批量发送 · %s · %s" % (session.host["name"], text.replace("\n", " ")))
        self._status("已批量发送到 %d 台会话" % len(dlg.targets))

    # ---------------- 兜底与计时 ----------------
    def _open_external_terminal(self):
        session = self._active_session()
        if session is None:
            self._status("没有活动会话", 3000)
            return
        host = session.host
        argv = ["ssh", "-p", str(host["port"])]
        if host.get("identityFile"):
            argv += ["-i", os.path.expanduser(host["identityFile"])]
        argv.append("%s@%s" % (host["user"], host["host"]))
        try:
            if os.name == "nt":
                # start 需要一个标题参数；CREATE_NEW_CONSOLE 给 ssh 独立真实终端
                subprocess.Popen(["cmd.exe", "/c", "start", "SSH %s" % host["name"]] + argv,
                                 creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            else:
                candidates = [("x-terminal-emulator", ["-e"] + argv),
                              ("gnome-terminal", ["--"] + argv),
                              ("open", ["-a", "Terminal", "--"] + argv)]
                last = None
                for program, extra in candidates:
                    try:
                        subprocess.Popen([program] + extra)
                        break
                    except OSError as exc:
                        last = exc
                else:
                    raise last or OSError("未找到可用终端")
            self._status("已在系统终端打开 %s" % session.target)
        except Exception as exc:
            self._status("打开系统终端失败：%s" % exc, 8000)

    def _tick(self):
        online = sum(1 for s in self.sessions if s.state == "connected")
        self.overview_label.setText("%d/%d 在线" % (online, len(self.sessions)))
        self.app_summary.setText("%d 个会话 · %d 台已连接" % (len(self.sessions), online))
        # 每张侧栏卡片的状态行（含连接时长）逐秒刷新
        for session in self.sessions:
            tab = self.tabs.get(id(session))
            if tab is None:
                continue
            if session.state == "connected" and session.connected_at:
                minutes = int((time.time() - session.connected_at) / 60)
                tab.set_elapsed("%d 分钟" % minutes)
            else:
                tab.set_elapsed("")
        session = self._active_session()
        if session is None:
            self.prompt_label.setText("$ ")
            self.prompt_label.setStyleSheet("color: #7d8598;")
            self.target_chip.setText("→ 未连接")
            self.header_dot.setProperty("state", "disconnected")
            self.header_dot.style().unpolish(self.header_dot)
            self.header_dot.style().polish(self.header_dot)
            self.header_name.setText("未连接")
            self.header_meta.setText("点击「＋ 新连接」开始")
            self.status_bar.showMessage("未连接会话 · 点击「＋ 新连接」开始")
            return
        idle = int(time.monotonic() - session.last_activity)
        if session.state == "connected" and session.connected_at:
            minutes = int((time.time() - session.connected_at) / 60)
            head = "已连 %dm" % minutes
        else:
            head = {"connecting": "连接中", "idle": "空闲已断开", "disconnected": "已断开"}.get(
                session.state, session.state)
        self.status_bar.showMessage("%s · %s · %s · invoke_shell · %s · 空闲 %02d:%02d · 全局 %d/%d 在线" % (
            session.host["name"], session.target, session.host.get("env", "未标注"),
            head, idle // 60, idle % 60, online, len(self.sessions)))

    def _check_idle(self):
        now = time.monotonic()
        warn_at = max(0, IDLE_SECONDS - IDLE_WARN_SECONDS)
        for session in list(self.sessions):
            if session.state != "connected" or session.pinned:
                continue
            elapsed = now - session.last_activity
            if elapsed >= IDLE_SECONDS:
                session.disconnect("空闲超过 %d 分钟，已自动断开" % (IDLE_SECONDS // 60), state="idle")
                self._status("%s 空闲超时，已断开（点状态灯重连）" % session.host["name"], 8000)
            elif IDLE_SECONDS > IDLE_WARN_SECONDS and elapsed >= warn_at and not session.idle_warned:
                session.idle_warned = True
                view = self.views.get(id(session))
                if view is not None:
                    view.append_system("空闲预警：1 分钟后自动断开，输入命令或点「保持连接」可续期",
                                       "#fbbf24")
                self._status("%s 即将因空闲断开" % session.host["name"], 15000)

    def _keep_alive(self):
        session = self._active_session()
        if session is None:
            self._status("没有活动会话", 3000)
            return
        session.last_activity = time.monotonic()
        session.idle_warned = False
        self._status("已为 %s 重置空闲计时" % session.host["name"])

    def _toggle_pin(self, checked):
        session = self._active_session()
        if session is None:
            return
        session.pinned = checked
        session.idle_warned = False
        self._status("%s 已%s空闲自动断开" % (
            session.host["name"], "豁免" if checked else "恢复"), 4000)

    def closeEvent(self, event):
        for session in list(self.sessions):
            session.close()
        close_log()
        super().closeEvent(event)


def make_app_icon():
    """代码绘制窗口/任务栏图标（无外部资产文件）：终端卡片 + 提示符 + 绿色链路徽标（SSH 连接）。"""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QIcon, QPainter, QPixmap, QPen

    pm = QPixmap(128, 128)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    # 圆角终端卡片
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#111a2c"))
    p.drawRoundedRect(QRectF(6, 6, 116, 116), 26, 26)
    # 顶部标题条（上圆角，下补直角）
    p.setBrush(QColor("#1d2b4a"))
    p.drawRoundedRect(QRectF(6, 6, 116, 36), 26, 26)
    p.drawRect(QRectF(6, 26, 116, 16))
    for idx, color in enumerate(("#ff6b6b", "#f5c04e", "#3ddc97")):
        p.setBrush(QColor(color))
        p.drawEllipse(QRectF(22 + idx * 16, 19, 10, 10))
    # 提示符 ">"
    p.setPen(QPen(QColor("#e8eef9"), 9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawLine(32, 60, 54, 78)
    p.drawLine(54, 78, 32, 96)
    # 光标 "_"
    p.setPen(QPen(QColor("#3ddc97"), 9, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(64, 96, 90, 96)
    # 右下角绿色圆徽 + 链环（连接/链路意象）
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#3ddc97"))
    p.drawEllipse(QRectF(88, 84, 32, 32))
    chain_pen = QPen(QColor("#0d1017"), 4.5)
    p.setPen(chain_pen)
    p.setBrush(Qt.NoBrush)
    for cx, cy in ((99.5, 103.5), (108.5, 96.5)):
        p.save()
        p.translate(cx, cy)
        p.rotate(-45)
        p.drawEllipse(QRectF(-9, -5.5, 18, 11))
        p.restore()
    p.end()
    return QIcon(pm)


def main():
    for message in ensure_configs():
        print(message, file=sys.stderr)
    rotate_log_if_needed()                 # 启动时检查一次
    atexit.register(close_log)
    if os.name == "nt":
        # 本程序是纯 GUI；拉起系统 ssh 时避免继承控制台造成任务栏闪框
        try:
            import ctypes
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setFont(QFont(UI_FONT, 9))
    app.setWindowIcon(make_app_icon())
    app.setStyleSheet(QSS)                  # 唯一一次全局样式设置
    window = SSHConsole()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
