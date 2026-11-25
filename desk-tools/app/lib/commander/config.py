# -*- coding: utf-8 -*-
"""commander 配置：纯本地 JSON，存于 desk-tools/cfg/commander.json。

与 command_panel 相同的约定：配置与脚本同目录，与当前工作目录无关。
"""
import json
import os
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parents[2]   # desk-tools/
CFG_DIR = _TOOLS_DIR / "cfg"
CONFIG_PATH = CFG_DIR / "commander.json"

DEFAULTS = {
    "favorites": [
        {"name": "桌面", "path": os.path.expanduser("~/Desktop")},
        {"name": "下载", "path": os.path.expanduser("~/Downloads")},
        {"name": "文档", "path": os.path.expanduser("~/Documents")},
    ],
    "panes": [
        {"tabs": [{"path": os.path.expanduser("~")}]},
        {"tabs": [{"path": os.path.expanduser("~")}]},
    ],
    "active_pane": 0,
    "show_hidden": False,
    "single_pane": False,
    "window": {"w": 1280, "h": 800, "maximized": False},
}


def load():
    cfg = {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    merged = json.loads(json.dumps(DEFAULTS, ensure_ascii=False))
    for k, v in cfg.items():
        merged[k] = v
    return merged


def save(cfg):
    try:
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass
