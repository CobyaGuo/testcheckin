# -*- coding: utf-8 -*-
"""标签与收藏体检 + 汇总导出：纯数据层（不依赖 Qt，可离屏测试）。

体检把两类失效项统一成同一条结构，供一个对话框集中呈现：
  {"id": 唯一键, "kind": "tag"|"fav", "path": 原始路径,
   "owner": 所属标签(含子层级命中) / 收藏集名, "index": 收藏项在集合内的原始下标}

标签侧失效 = file_tags 里路径已不存在（一条记录 = 一个路径，可能挂多个标签）；
收藏侧失效 = favorites / fav_sets 各条目的 path 已不存在。
清理按「用户勾选的具体项」执行，不做一键全清——失效不等于无用，文件可能只是
挪了地方，误杀率最低。

导出：Markdown（人读，标签层级→标题，失效项标注 ⚠）+ JSON（全量快照，备份/迁移）。
"""
import json
import os
from datetime import datetime

from . import tags

GHOST_TEXT = tags.GHOST_TEXT          # "⚠ 已不存在"，与聚合视图保持一致


def _fav_collections(cfg):
    """返回 [(集合名, 条目列表)]；常用目录用空名表示。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    out = [("", cfg.get("favorites") or [])]
    sets = cfg.get("fav_sets") or {}
    if isinstance(sets, dict):
        out.extend((str(k), v) for k, v in sets.items())
    return [(name, items) for name, items in out if isinstance(items, list)]


def scan_invalid(cfg, exists=os.path.exists):
    """一次扫描两类失效项，返回统一结构列表（同一路径在多个集合各算一条）。

    exists 可注入，便于离屏测试用假函数验证统计而不碰真实磁盘。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    out = []
    cache = {}

    def alive(path):
        key = str(path or "")
        if key not in cache:
            try:
                cache[key] = bool(exists(key))
            except Exception:
                cache[key] = False
        return cache[key]

    # 标签侧：一个路径一条记录，owner 列出命中该路径的全部标签（含父层级）
    ftags = cfg.get("file_tags") or {}
    if isinstance(ftags, dict):
        for path, recs in ftags.items():
            if alive(path):
                continue
            names = []
            for r in recs if isinstance(recs, (list, tuple)) else []:
                if isinstance(r, dict) and str(r.get("t") or "") and r["t"] not in names:
                    names.append(str(r["t"]))
            out.append({
                "id": "tag|" + tags.norm_path(path),
                "kind": "tag",
                "path": str(path or ""),
                "owner": "、".join(names),
                "index": -1,
            })

    # 收藏侧：常用目录 + 各收藏集，逐条 exists；index 保留原始下标便于精确删除
    for set_name, items in _fav_collections(cfg):
        for i, fav in enumerate(items):
            if not isinstance(fav, dict):
                continue
            path = str(fav.get("path") or "")
            if alive(path):
                continue
            out.append({
                "id": "fav|%s|%d" % (set_name, i),
                "kind": "fav",
                "path": path,
                "owner": set_name or "常用目录",
                "set": set_name,
                "index": i,
            })
    return out


def summary(cfg, exists=os.path.exists, invalid=None):
    """体检汇总：标签数 / 收藏数 / 失效标签记录数 / 失效收藏数。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    defs = cfg.get("tag_defs") or {}
    n_tags = len(defs) if isinstance(defs, dict) else 0
    n_fav = sum(len(items) for _n, items in _fav_collections(cfg))
    rows = invalid if invalid is not None else scan_invalid(cfg, exists)
    return {
        "tags": n_tags,
        "favorites": n_fav,
        "ghost_tags": sum(1 for r in rows if r.get("kind") == "tag"),
        "ghost_favs": sum(1 for r in rows if r.get("kind") == "fav"),
    }


def clean_selected(cfg, rows):
    """按用户勾选的失效项精确清理；返回 {"tag": n, "fav": n}。

    rows 为 scan_invalid() 结果的子集。标签项删整条路径记录；收藏项按集合 +
    原始下标删除（同集合多删时降序处理，避免下标漂移）。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    removed = {"tag": 0, "fav": 0}
    tag_paths = [r["path"] for r in rows if r.get("kind") == "tag"]
    ftags = cfg.get("file_tags")
    if isinstance(ftags, dict) and tag_paths:
        keys = {tags.norm_path(p) for p in tag_paths}
        for key in [k for k in ftags if tags.norm_path(k) in keys]:
            ftags.pop(key, None)
            removed["tag"] += 1

    fav_rows = [r for r in rows if r.get("kind") == "fav"]
    by_set = {}
    for r in fav_rows:
        by_set.setdefault(r.get("set", ""), []).append(int(r.get("index", -1)))
    for set_name, indexes in by_set.items():
        if set_name:
            sets = cfg.get("fav_sets") or {}
            if not isinstance(sets, dict) or set_name not in sets:
                continue
            items = sets[set_name]
        else:
            items = cfg.get("favorites")
        if not isinstance(items, list):
            continue
        for i in sorted({i for i in indexes if 0 <= i < len(items)}, reverse=True):
            items.pop(i)
            removed["fav"] += 1
    return removed


# ---------------------------------------------------------------------------
# 汇总导出
# ---------------------------------------------------------------------------
def _md_escape(text):
    return str(text or "").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def _fmt_at(ts):
    return tags._fmt_time(ts) or "-"


def export_markdown(cfg, exists=os.path.exists, when=None):
    """Markdown：标签层级→标题；每标签下列出精确挂在该标签的文件（含描述与失效标注）；
    收藏集同样列成清单。失效项路径后加 ⚠ 已不存在，导出物本身就是一次盘点。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    when = when or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    defs = cfg.get("tag_defs") or {}
    defs = defs if isinstance(defs, dict) else {}
    ftags = cfg.get("file_tags") or {}
    ftags = ftags if isinstance(ftags, dict) else {}
    rows = scan_invalid(cfg, exists)
    s = summary(cfg, invalid=rows)

    lines = ["# Commander 数据汇总", "",
             "> 导出时间：%s" % when,
             "> 标签 %d 个 · 收藏 %d 条 · 失效标签记录 %d 条 · 失效收藏 %d 条"
             % (s["tags"], s["favorites"], s["ghost_tags"], s["ghost_favs"]), ""]

    # 路径 → 是否已失效（一次 exists，全篇复用）
    alive_cache = {}
    def ghost(path):
        key = str(path or "")
        if key not in alive_cache:
            try:
                alive_cache[key] = not bool(exists(key))
            except Exception:
                alive_cache[key] = True
        return alive_cache[key]

    lines.append("## 标签")
    lines.append("")
    if not defs:
        lines.append("（暂无标签）")
        lines.append("")
    # 按标签名排序；子层级排在父后面，用标题层级表达 /
    for name in sorted(defs, key=lambda n: (n.count("/"), n)):
        depth = name.count("/") + 3            # 顶层从 ### 开始
        info = defs.get(name)
        info = info if isinstance(info, dict) else {}
        lines.append("%s %s" % ("#" * min(depth, 6), name))
        note = tags.note_for(cfg, name)
        if note:
            lines.append("")
            lines.append("> %s" % note.replace("\n", " "))
        lines.append("")
        paths = [p for p, recs in ftags.items()
                 if isinstance(recs, (list, tuple))
                 and any(isinstance(r, dict) and r.get("t") == name for r in recs)]
        if not paths:
            lines.append("（该标签下无直接文件）")
            lines.append("")
            continue
        for path in sorted(paths, key=lambda p: (ghost(p), os.path.basename(str(p)).lower(), str(p))):
            rec = next(r for r in ftags[path]
                       if isinstance(r, dict) and r.get("t") == name)
            link = tags.link_note(cfg, path)
            tail = " — %s" % link if link else ""
            mark = (" " + GHOST_TEXT) if ghost(path) else ""
            lines.append("- [%s](%s) — %s%s%s"
                         % (_md_escape(os.path.basename(str(path))), path,
                            _fmt_at(rec.get("at")), tail, mark))
        lines.append("")

    lines.append("## 收藏")
    lines.append("")
    any_fav = False
    for set_name, items in _fav_collections(cfg):
        valid = [f for f in items if isinstance(f, dict)]
        if not valid:
            continue
        any_fav = True
        lines.append("### %s" % (set_name or "常用目录"))
        lines.append("")
        for f in valid:
            path = str(f.get("path") or "")
            mark = (" " + GHOST_TEXT) if ghost(path) else ""
            lines.append("- [%s](%s)%s" % (_md_escape(f.get("name") or path), path, mark))
        lines.append("")
    if not any_fav:
        lines.append("（暂无收藏）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_json(cfg, exists=os.path.exists, when=None):
    """全量快照：tag_defs/file_tags/fav_sets/favorites/link_notes + 时间戳 + 失效清单。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    rows = scan_invalid(cfg, exists)
    snapshot = {
        "app": "commander",
        "exported_at": when or datetime.now().isoformat(timespec="seconds"),
        "tag_defs": cfg.get("tag_defs") or {},
        "file_tags": cfg.get("file_tags") or {},
        "favorites": cfg.get("favorites") or [],
        "fav_sets": cfg.get("fav_sets") or {},
        "link_notes": cfg.get(tags.LINK_NOTES) or {},
        "links_dir": cfg.get("links_dir") or "",
        "summary": summary(cfg, invalid=rows),
        "invalid": rows,
    }
    return json.dumps(snapshot, ensure_ascii=False, indent=2)
