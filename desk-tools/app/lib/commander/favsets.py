# -*- coding: utf-8 -*-
"""嵌套收藏集数据层（纯 Python，不依赖 Qt）。

收藏集按浏览器书签的形态组织：收藏集是第一层目录，下面可以再建子集。
层级用名称里的 "/" 表达，与命名标签、收藏分组保持同一套渲染语言。

配置分工（桌面/下载/文档是特殊入口，不属于收藏集）::

    favorites: [fav, ...]                    # 常用目录，独立分区/工具栏入口
    fav_sets:  {"工作": [fav, ...],
                "工作/项目A": [fav, ...],    # 工作集的子集
                "学习/前端/Vue": [fav, ...]} # 可任意深度

内部 key 就是 fav_sets 的字典键（如 "工作"、"工作/项目A"）。
"""

_SEP = "/"


def clean_name(name):
    """清理集合名中的多余斜杠与空白；返回可安全拼接的短名。"""
    parts = [p.strip() for p in str(name or "").split(_SEP) if p.strip()]
    return _SEP.join(parts)


def short_label(key):
    """树节点/按钮显示名：取最后一段。"""
    return str(key or "").split(_SEP)[-1]


def all_sets(cfg):
    """全部可选集合：[(key, 显示名)]，按层级深度排序。"""
    sets = (cfg or {}).get("fav_sets") or {}
    keys = [k for k in sets if isinstance(k, str) and k.strip()]
    keys.sort(key=lambda k: (k.count(_SEP), k.casefold()))
    return [(k, k) for k in keys]


def top_sets(cfg):
    """第一层收藏集 key：自身没有父集合的所有 key。"""
    sets = (cfg or {}).get("fav_sets") or {}
    keys = {k for k in sets if isinstance(k, str) and k.strip()}
    roots = [k for k in keys
             if _SEP not in k or k.rsplit(_SEP, 1)[0] not in keys]
    return sorted(roots, key=str.casefold)


def children(cfg, key):
    """返回 key 的直接子集合 key（不含更深层级）。"""
    prefix = str(key or "") + _SEP
    out = []
    for k in ((cfg or {}).get("fav_sets") or {}):
        if not isinstance(k, str) or not k.startswith(prefix):
            continue
        rest = k[len(prefix):]
        if rest and _SEP not in rest:
            out.append(k)
    return sorted(out, key=str.casefold)


def descendants(cfg, key):
    """返回 key 及其所有子层级的 key（含自身）。"""
    prefix = str(key or "") + _SEP
    out = [key]
    for k in ((cfg or {}).get("fav_sets") or {}):
        if isinstance(k, str) and k.startswith(prefix):
            out.append(k)
    return out


def items(cfg, key):
    """返回该集合的收藏项列表；不存在时返回临时空列表。"""
    sets = (cfg or {}).get("fav_sets") or {}
    data = sets.get(key)
    return data if isinstance(data, list) else []


def count(cfg, key):
    """集合自身 + 所有子集的收藏项数（与树节点计数一致）。"""
    return sum(len(items(cfg, k)) for k in descendants(cfg, key))


def create(cfg, name, parent=None):
    """新建收藏集；parent=None 为第一层，否则为 parent 的子集。

    自动补齐中间层级；重名返回 None。
    """
    name = clean_name(name)
    if not name:
        return None
    if parent is None:
        full = name
    else:
        base = clean_name(parent)
        if not base:
            return None
        full = base + _SEP + name
    sets = (cfg or {}).setdefault("fav_sets", {})
    if not isinstance(sets, dict) or full in sets:
        return None
    parts = full.split(_SEP)
    for i in range(1, len(parts)):
        sets.setdefault(_SEP.join(parts[:i]), [])
    sets[full] = []
    return full


def rename(cfg, key, new_name):
    """重命名集合（含子层级）；返回新 key 或 None。"""
    new_name = clean_name(new_name)
    if not new_name or _SEP in new_name:
        return None
    sets = (cfg or {}).get("fav_sets") or {}
    if key not in sets:
        return None
    if _SEP in str(key):
        parent = key.rsplit(_SEP, 1)[0]
        full = parent + _SEP + new_name
    else:
        full = new_name
    if full in sets and full != key:
        return None
    mapping = {k: full + k[len(key):] for k in list(sets)
               if k == key or k.startswith(str(key) + _SEP)}
    if any(dst in sets and dst not in mapping for dst in mapping.values()):
        return None
    (cfg or {})["fav_sets"] = {mapping.get(k, k): v for k, v in sets.items()}
    return full


def delete(cfg, key):
    """删除集合及其所有子集与其中的收藏项；返回被删的收藏项数。"""
    if not key:
        return 0
    sets = (cfg or {}).get("fav_sets") or {}
    removed = 0
    for k in descendants(cfg, key):
        recs = sets.pop(k, None)
        if isinstance(recs, list):
            removed += len(recs)
    return removed
