# -*- coding: utf-8 -*-
"""commander 配置：纯本地 JSON，存于 desk-tools/app/cfg/commander.json。

与 command_panel 相同的约定：配置与脚本同目录，与当前工作目录无关。
读写异常不再静默吞掉，而是通过 get_warnings() 暴露给 UI 提示。
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


_WARNINGS = []


def _warn(msg):
    if msg not in _WARNINGS:
        _WARNINGS.append(msg)


def get_warnings():
    """返回本会话累积的配置告警（去重），供 UI 集中提示。"""
    return list(_WARNINGS)


def clear_warnings():
    _WARNINGS.clear()


def _valid_favorite(fav):
    return (isinstance(fav, dict)
            and isinstance(fav.get("name"), str) and fav["name"].strip()
            and isinstance(fav.get("path"), str) and fav["path"].strip())


def load():
    cfg = {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("根节点不是对象")
    except FileNotFoundError:
        pass                      # 首次运行，无配置属正常
    except Exception as e:
        _warn("配置读取失败，已按默认设置启动：%s" % e)
        cfg = {}
    merged = json.loads(json.dumps(DEFAULTS, ensure_ascii=False))
    for k, v in cfg.items():
        merged[k] = v             # 保留 theme/split_sizes/file_tags 等扩展字段

    # 逐字段容错：缺 name/path 的收藏会令树/菜单构建抛 KeyError 而启动崩溃
    favs = merged.get("favorites")
    if isinstance(favs, list):
        good = []
        for i, fav in enumerate(favs):
            if _valid_favorite(fav):
                good.append(fav)
            else:
                _warn("已忽略第 %d 条无效收藏（缺少名称或路径）" % (i + 1))
        if len(good) != len(favs):
            merged["favorites"] = good
    elif favs is not None:
        _warn("收藏配置格式错误，已按默认收藏启动")
        merged["favorites"] = json.loads(json.dumps(DEFAULTS["favorites"]))

    # panes 结构损坏同样会导致主窗口构建失败
    panes = merged.get("panes")
    if (not isinstance(panes, list) or len(panes) != 2
            or any(not isinstance(p, dict) or not isinstance(p.get("tabs"), list)
                   for p in panes)):
        _warn("面板配置格式错误，已按默认布局启动")
        merged["panes"] = json.loads(json.dumps(DEFAULTS["panes"]))
    return merged


def save(cfg):
    """原子写入；失败时记录告警并返回 False（不再静默丢弃用户改动）。"""
    try:
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception as e:
        _warn("配置保存失败，本次改动未落盘：%s" % e)
        return False
