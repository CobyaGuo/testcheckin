# -*- coding: utf-8 -*-
"""命名层级文件标签 + 链接文件描述：纯数据层（不依赖 Qt，可离线测试）。

配置结构（一步到位，不再保留颜色数组格式）：
  tag_defs:   {名称: {"color": "#rrggbb", "created": "YYYY-MM-DD",
                      "note": "描述", "note_at": "YYYY-MM-DD"}}
              名称中的 "/" 表示层级，例如 "用例/接口"。
              note / note_at 可选：旧数据缺字段时一律按空串兼容。
  file_tags:  {规范路径: [{"t": 名称, "at": 打标时间戳}, ...]}
  link_notes: {规范路径: "链接文件描述"}   # 标准 .url 不携带自定义字段

旧格式 {路径: ["#rrggbb", ...]} 由 migrate() 自动反查为颜色名并落盘。
所有遍历均为内存字典操作，不产生目录扫描；tooltip 文本也在这里组装，
便于离屏测试直接断言，不依赖 Qt 事件循环。
"""
import os
import time
from datetime import date, datetime

TAG_COLORS = ("#ff453a", "#ff9f0a", "#ffd60a", "#34c759",
              "#0a84ff", "#5e6ad2", "#af52de", "#98a0ac")

# 旧颜色标签的反查名字（迁移用）
COLOR_NAMES = {
    "#ff453a": "红", "#ff9f0a": "橙", "#ffd60a": "黄", "#34c759": "绿",
    "#0a84ff": "蓝", "#5e6ad2": "靛", "#af52de": "紫", "#98a0ac": "灰",
}

GHOST_TEXT = "⚠ 已不存在"
LINK_NOTES = "link_notes"           # 文件级描述（.url 标准格式无法内嵌）
DEFAULT_LINKS_DIR = os.path.join(os.path.expanduser("~"), "Links")

_REGISTRY = {}                      # 当前生效的配置字典（供搜索谓词读取）


def set_registry(cfg):
    """注册当前配置对象；搜索线程只持有它，不再重复读盘。"""
    global _REGISTRY
    _REGISTRY = cfg if isinstance(cfg, dict) else {}


def get_registry():
    return _REGISTRY


def norm_path(path):
    """统一路径键：大小写/斜杠/相对路径归一化。"""
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def clean_name(name):
    """标签名去掉首尾空白与多余斜杠；空名/纯斜杠视为无效。"""
    return str(name or "").strip().strip("/") or ""


def _prefix_match(tag, name):
    """层级前缀匹配：'用例' 命中 '用例' 与 '用例/接口'，反之不成立。"""
    t, n = str(tag or "").casefold(), str(name or "").casefold()
    return bool(n) and (t == n or t.startswith(n + "/"))


def next_color(defs):
    """取当前使用最少的色板颜色（并行定义多个标签时自动循环）。"""
    used = {}
    for info in (defs or {}).values():
        c = str((info or {}).get("color", "")).lower() if isinstance(info, dict) else ""
        used[c] = used.get(c, 0) + 1
    return min(TAG_COLORS, key=lambda c: (used.get(c, 0), TAG_COLORS.index(c)))


def _def(name, color=None, created=None, defs=None, note=""):
    return {"color": (color or next_color(defs if defs is not None
                                          else _REGISTRY.get("tag_defs", {}))).lower(),
            "created": created or date.today().isoformat(),
            "note": str(note or "")}


def _legacy_tag(value):
    """旧颜色值 → (标签名, 颜色)；未知颜色保留为自定义色标签。"""
    c = str(value or "").strip().lower()
    if c in COLOR_NAMES:
        return COLOR_NAMES[c], c
    if c in COLOR_NAMES.values():
        return c, next(k for k, v in COLOR_NAMES.items() if v == c)
    if c.startswith("#") and len(c) == 7:
        try:
            int(c[1:], 16)
            return "自定义" + c[1:], c
        except ValueError:
            pass
    return None, None


def migrate(cfg):
    """就地迁移为命名标签结构；返回是否发生了结构变更。

    幂等：已是新结构时直接返回 False。迁移结果由调用方 config.save() 落盘。
    """
    changed = False
    defs = cfg.get("tag_defs")
    if not isinstance(defs, dict):
        defs, changed = {}, True
    clean_defs = {}
    for name, info in defs.items():
        n = clean_name(name)
        if not n or not isinstance(info, dict):
            changed = True
            continue
        color = str(info.get("color") or "").lower()
        if color not in TAG_COLORS and not (color.startswith("#") and len(color) == 7):
            color = next_color(clean_defs)
            changed = True
        created = info.get("created") or date.today().isoformat()
        if not isinstance(created, str):
            created = date.today().isoformat()
            changed = True
        item = {"color": color, "created": created}
        # 描述字段是增量新增：老配置没有这两个键时不主动补，避免无谓落盘；
        # 已有的描述必须原样保留，否则下面的 item != info 会把 note 洗掉。
        for extra in ("note", "note_at"):
            if extra in info:
                item[extra] = str(info.get(extra) or "")
        clean_defs[n] = item
        if n != name or item != info:
            changed = True
    defs = clean_defs

    raw = cfg.get("file_tags")
    ftags = {}
    if isinstance(raw, dict):
        for path, value in raw.items():
            if not isinstance(path, str) or not path.strip():
                changed = True
                continue
            key = norm_path(path)
            if key != path:
                changed = True
            recs = []
            for item in (value if isinstance(value, list) else []):
                if isinstance(item, dict):
                    name = clean_name(item.get("t"))
                    at = item.get("at")
                    if not isinstance(at, (int, float)):
                        at = time.time()
                        changed = True
                    if not name:
                        changed = True
                        continue
                    rec = {"t": name, "at": at}
                    if rec != item:
                        changed = True
                    recs.append(rec)
                else:
                    name, color = _legacy_tag(item)
                    if not name:
                        changed = True
                        continue
                    if name not in defs:
                        defs[name] = _def(name, color or next_color(defs))
                    recs.append({"t": name, "at": time.time()})
                    changed = True
            if not isinstance(value, list):
                changed = True
            seen, uniq = set(), []
            for rec in recs:
                if rec["t"] in seen:
                    changed = True
                    continue
                seen.add(rec["t"])
                uniq.append(rec)
            if uniq:
                ftags[key] = uniq
    elif raw is not None:
        changed = True

    if "tag_defs" not in cfg or "file_tags" not in cfg:
        changed = True
    cfg["tag_defs"] = defs
    cfg["file_tags"] = ftags
    set_registry(cfg)
    return changed


def colors_map(cfg):
    """供 fsmodel 使用的 {路径: [颜色]} 视图（色点叠加不变）。"""
    defs = cfg.get("tag_defs", {})
    out = {}
    for path, recs in (cfg.get("file_tags") or {}).items():
        colors = [defs.get(r.get("t"), {}).get("color", "#98a0ac") for r in recs]
        if colors:
            out[norm_path(path)] = colors
    return out


# ---------------------------------------------------------------------------
# 标签描述 / tooltip 文本（纯字符串组装，UI 只管 setToolTip）
# ---------------------------------------------------------------------------
def _fmt_time(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def note_for(cfg, name):
    """标签描述；旧数据无 note 字段时按空串兼容（只读不改配置）。"""
    info = ((cfg or {}).get("tag_defs") or {}).get(name) or {}
    return str(info.get("note") or "") if isinstance(info, dict) else ""


def note_updated(cfg, name):
    """描述更新日期；没有 note_at 的老数据退回创建日期。"""
    info = ((cfg or {}).get("tag_defs") or {}).get(name) or {}
    if not isinstance(info, dict):
        return ""
    return str(info.get("note_at") or info.get("created") or "")


def set_note(cfg, name, text, when=None):
    """写标签描述并记录更新日期；标签不存在返回 False。"""
    info = ((cfg or {}).get("tag_defs") or {}).get(name)
    if not isinstance(info, dict):
        return False
    info["note"] = str(text or "").strip()
    info["note_at"] = when or date.today().isoformat()
    return True


def note_tooltip(cfg, name):
    """打标菜单项的 tooltip：描述 + 更新日期。"""
    tip = note_for(cfg, name) or "（暂无描述）"
    updated = note_updated(cfg, name)
    return tip + ("\n更新：%s" % updated if updated else "")


def last_tagged(cfg, name, stats=None):
    """该标签（含子层级）最近一次打标时间，格式化为 YYYY-mm-dd HH:MM。"""
    if isinstance(stats, dict):
        s = stats.get(str(name or "").casefold()) or {}
        at = s.get("last")
        return _fmt_time(at) if isinstance(at, (int, float)) else ""
    times = [r.get("at") for recs in ((cfg or {}).get("file_tags") or {}).values()
             for r in recs if isinstance(r, dict) and _prefix_match(r.get("t"), name)
             and isinstance(r.get("at"), (int, float))]
    return _fmt_time(max(times)) if times else ""


def tag_node_tooltip(cfg, name, exists=os.path.exists, stats=None):
    """收藏树标签节点：描述 + 文件计数 + 最近打标时间（+ 失效提示）。

    stats 为 build_tag_stats() 的一次性聚合结果，传入后本函数零磁盘访问。
    """
    info = ((cfg or {}).get("tag_defs") or {}).get(name) or {}
    color = info.get("color", "#98a0ac") if isinstance(info, dict) else "#98a0ac"
    lines = ["标签：%s" % name,
             "描述：%s" % (note_for(cfg, name) or "（未填写，右键可编辑）")]
    updated = note_updated(cfg, name)
    if updated:
        lines.append("描述更新：%s" % updated)
    lines.append("颜色：%s" % color)
    lines.append("文件：%d 个" % count_for(cfg, name, exists, stats))
    last = last_tagged(cfg, name, stats)
    if last:
        lines.append("最近打标：%s" % last)
    ghost = ghost_count(cfg, name, exists, stats)
    if ghost:
        lines.append("⚠ %d 条失效记录" % ghost)
    return "\n".join(lines)


def file_tooltip(cfg, path, ghost=None):
    """聚合视图文件行：完整路径 + 全部标签及各自描述 + 链接描述。"""
    path = str(path or "")
    if ghost is None:
        ghost = not os.path.exists(path)
    lines = [path]
    link = link_note(cfg, path)
    if link:
        lines.append("链接描述：%s" % link)
    recs = ((cfg or {}).get("file_tags") or {}).get(norm_path(path), [])
    if recs:
        lines.append("标签：")
        for r in recs:
            if not isinstance(r, dict):
                continue
            name = str(r.get("t") or "")
            note = note_for(cfg, name)
            lines.append("  ● %s%s" % (name, (" — " + note) if note else ""))
    if ghost:
        lines.append(GHOST_TEXT)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 链接文件：标准 Windows .url + 配置里的文件级描述
# ---------------------------------------------------------------------------
def link_note(cfg, path):
    return str(((cfg or {}).get(LINK_NOTES) or {}).get(norm_path(path)) or "")


def set_link_note(cfg, path, text):
    key = norm_path(path)
    notes = (cfg or {}).setdefault(LINK_NOTES, {})
    if not isinstance(notes, dict):
        notes = cfg[LINK_NOTES] = {}
    text = str(text or "").strip()
    if text:
        notes[key] = text
    else:
        notes.pop(key, None)
    return key in notes


def links_dir(cfg=None):
    value = ((cfg or {}).get("links_dir") or "").strip()
    return os.path.abspath(os.path.expanduser(value or DEFAULT_LINKS_DIR))


def url_content(url):
    """标准 Windows InternetShortcut 文本（系统原生可识别，无自定义字段）。"""
    url = str(url or "").strip()
    if not url:
        raise ValueError("URL 不能为空")
    return "[InternetShortcut]\nURL=%s\n" % url


def safe_link_name(name):
    """清理文件名非法字符，保证 .url 能落在任意目录。"""
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad or ord(c) < 32 else c for c in str(name or ""))
    return out.strip().strip(".") or "链接"


def new_link_path(directory, name):
    """生成不覆盖已有文件的 .url 绝对路径。"""
    directory = os.path.abspath(os.path.expanduser(directory or DEFAULT_LINKS_DIR))
    stem = safe_link_name(name)
    if not stem.lower().endswith(".url"):
        stem += ".url"
    path = os.path.join(directory, stem)
    i = 2
    while os.path.exists(path):
        path = os.path.join(directory, "%s (%d).url" % (stem[:-4], i))
        i += 1
    return path


def save_link(cfg, name, url, note="", directory=None):
    """写 .url 文件并把描述登记到 link_notes；返回文件绝对路径。"""
    content = url_content(url)                  # 先校验，避免建空目录
    directory = os.path.abspath(os.path.expanduser(
        directory or links_dir(cfg)))
    os.makedirs(directory, exist_ok=True)
    path = new_link_path(directory, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    set_link_note(cfg, path, note)
    return path


def names_for(cfg, path):
    return [r.get("t") for r in (cfg.get("file_tags") or {}).get(norm_path(path), [])]


def tagged_paths(cfg, name):
    """内存遍历：返回挂了该标签（含子层级）的路径列表。"""
    return [p for p, recs in (cfg.get("file_tags") or {}).items()
            if any(_prefix_match(r.get("t"), name) for r in recs)]


def build_tag_stats(cfg, exists=os.path.exists):
    """一次遍历算全：每条路径只 exists 一次，按标签层级分发计数与最近打标。

    返回 {标签名(casefold): {"count": 存在数, "ghost": 失效数, "last": 时间戳}}。
    父标签自动累加子层级（与 _prefix_match 语义一致），因此收藏树展开几十个
    节点只做 N 次 stat，而不是每个节点都全量扫一遍 file_tags。
    """
    stats = {}
    ftags = (cfg or {}).get("file_tags") or {}
    if not isinstance(ftags, dict):
        return stats
    cache = {}
    for path, recs in ftags.items():
        if not isinstance(recs, (list, tuple)):
            continue
        p = str(path or "")
        if p not in cache:
            try:
                cache[p] = bool(exists(p))
            except Exception:
                cache[p] = False       # 网络盘/权限异常按失效处理，不阻断整棵树
        ok = cache[p]
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            name = str(rec.get("t") or "").strip()
            if not name:
                continue
            at = rec.get("at")
            at = at if isinstance(at, (int, float)) else None
            parts = name.split("/")
            for i in range(len(parts), 0, -1):   # 自身 + 各级父标签
                tag = "/".join(parts[:i])
                s = stats.setdefault(tag.casefold(),
                                     {"count": 0, "ghost": 0, "last": None})
                s["count" if ok else "ghost"] += 1
                if at is not None and (s["last"] is None or at > s["last"]):
                    s["last"] = at
    return stats


def aggregate(cfg, name, exists=os.path.exists):
    """标签聚合结果：[(路径, 是否存在)]，存在项在前、按文件名排序。"""
    out = [(p, bool(exists(p))) for p in tagged_paths(cfg, name)]
    out.sort(key=lambda x: (not x[1], os.path.basename(x[0]).lower(), x[0]))
    return out


def count_for(cfg, name, exists=os.path.exists, stats=None):
    """树节点显示计数：只统计实际存在的文件（幽灵记录不计）。"""
    if isinstance(stats, dict):
        return int((stats.get(str(name or "").casefold()) or {}).get("count", 0))
    return sum(1 for _p, ok in aggregate(cfg, name, exists) if ok)


def ghost_count(cfg, name, exists=os.path.exists, stats=None):
    if isinstance(stats, dict):
        return int((stats.get(str(name or "").casefold()) or {}).get("ghost", 0))
    return sum(1 for _p, ok in aggregate(cfg, name, exists) if not ok)


def clean_ghosts(cfg, name=None, exists=os.path.exists):
    """清理失效路径的标签记录（name=None 清理全部）；返回清理条数。"""
    ftags = cfg.get("file_tags") or {}
    dead = [p for p in list(ftags)
            if not exists(p) and (name is None or any(_prefix_match(r.get("t"), name)
                                                      for r in ftags[p]))]
    for p in dead:
        ftags.pop(p, None)
    return len(dead)


def create_tag(cfg, name, color=None):
    """新建标签（含缺失的中间层级），返回最终名称；重名返回 None。"""
    name = clean_name(name)
    if not name:
        return None
    defs = cfg.setdefault("tag_defs", {})
    if name in defs:
        return None
    parts = name.split("/")
    for i in range(1, len(parts)):
        parent = "/".join(parts[:i])
        if parent not in defs:
            defs[parent] = _def(parent, defs=defs)
    defs[name] = _def(name, color, defs=defs)
    return name


def rename_tag(cfg, old, new):
    """改名标签：同名层级及其所有子层级同步改名，文件记录同步更新。"""
    new = clean_name(new)
    old = clean_name(old)
    defs = cfg.get("tag_defs") or {}
    if not new or new == old or old not in defs or new in defs:
        return False
    mapping = {n: new + n[len(old):] for n in list(defs) if _prefix_match(n, old)}
    renamed = {mapping.get(n, n): info for n, info in defs.items()}
    cfg["tag_defs"] = renamed
    for recs in (cfg.get("file_tags") or {}).values():
        for r in recs:
            if r.get("t") in mapping:
                r["t"] = mapping[r["t"]]
    return True


def set_color(cfg, name, color):
    info = (cfg.get("tag_defs") or {}).get(name)
    if not info or not str(color).startswith("#"):
        return False
    info["color"] = str(color).lower()
    return True


def delete_tag(cfg, name):
    """删除标签定义及子层级，并摘除文件上的记录（绝不动文件本身）。"""
    defs = cfg.get("tag_defs") or {}
    doomed = {n for n in defs if _prefix_match(n, name)}
    if not doomed:
        return 0
    for n in doomed:
        defs.pop(n, None)
    ftags = cfg.get("file_tags") or {}
    for path in list(ftags):
        ftags[path] = [r for r in ftags[path] if r.get("t") not in doomed]
        if not ftags[path]:
            ftags.pop(path, None)
    return len(doomed)


def add(cfg, path, name):
    """挂标签（已存在则不重复）；未定义标签自动补建定义，避免孤儿记录。"""
    name = clean_name(name)
    if not name:
        return False
    if name not in (cfg.get("tag_defs") or {}):
        create_tag(cfg, name)               # 含缺失的中间层级
    key = norm_path(path)
    ftags = cfg.setdefault("file_tags", {})
    recs = ftags.get(key, [])
    if any(r.get("t") == name for r in recs):
        return False
    recs.append({"t": name, "at": time.time()})
    ftags[key] = recs
    return True


def toggle(cfg, path, name):
    """切换一个文件上的标签；新标签自动补建定义，避免孤儿记录。"""
    name = clean_name(name)
    if not name:
        return False
    key = norm_path(path)
    ftags = cfg.setdefault("file_tags", {})
    recs = ftags.get(key, [])
    hit = next((r for r in recs if r.get("t") == name), None)
    if hit:
        recs.remove(hit)
        if not recs:
            ftags.pop(key, None)
        return False
    if name not in (cfg.get("tag_defs") or {}):
        create_tag(cfg, name)               # 含缺失的中间层级
    recs.append({"t": name, "at": time.time()})
    ftags[key] = recs
    return True


def _map_key(key, changes):
    """把一条规范路径按变更表改写；返回 (新键或 None, 是否命中)。"""
    for old, new in changes.items():
        if key == old:
            return new, True
        if key.startswith(old.rstrip(os.sep) + os.sep):
            return (new + key[len(old):]) if new else None, True
    return key, False


def _apply_list_paths(data, changes):
    """file_tags：值是一组打标记录，按标签名去重合并。"""
    out = {}
    changed = False
    for key, recs in data.items():
        new_key, hit = _map_key(key, changes)
        if not hit:
            out[key] = recs
            continue
        changed = True
        if new_key is None:
            continue
        dest = out.setdefault(new_key, [])
        for rec in recs:
            if not any(r.get("t") == rec.get("t") for r in dest
                       if isinstance(r, dict) and isinstance(rec, dict)):
                dest.append(rec)
    return out, changed


def _apply_value_paths(data, changes):
    """link_notes：值是描述文本，目标已存在时以新值为准。"""
    out = {}
    changed = False
    for key, value in data.items():
        new_key, hit = _map_key(key, changes)
        if not hit:
            out[key] = value
            continue
        changed = True
        if new_key is not None:
            out[new_key] = value
    return out, changed


def apply_path_changes(cfg, mapping):
    """文件操作后的路径同步：{旧路径: 新路径或 None}。

    目录移动/改名会把其子路径一起改；None 表示删除/移出后移除记录。
    file_tags 与 link_notes 共用同一套映射，保证 .url 改名/移动/删除后描述不丢。
    """
    changes = {}
    for old, new in (mapping or {}).items():
        if not old:
            continue
        o = norm_path(old)
        n = norm_path(new) if new else None
        if n != o:
            changes[o] = n
    if not changes:
        return False
    changed = False
    for field, apply_fn in (("file_tags", _apply_list_paths),
                            (LINK_NOTES, _apply_value_paths)):
        data = cfg.get(field)
        if not isinstance(data, dict):
            continue
        out, hit = apply_fn(data, changes)
        if hit:
            cfg[field] = out
            changed = True
    return changed


def make_pred(value, cfg=None):
    """构造 tag: 谓词：中文标签名 + 子层级前缀匹配；兼容旧颜色名/十六进制。"""
    v = str(value or "").strip()
    cfg = cfg if isinstance(cfg, dict) else get_registry()
    ftags = cfg.get("file_tags") or {}
    defs = cfg.get("tag_defs") or {}
    known = {r.get("t") for recs in ftags.values() for r in recs} | set(defs)
    if any(_prefix_match(n, v) for n in known):
        def pred(_name, path, _st, _is_dir, _v=v.casefold()):
            return any(r.get("t", "").casefold() == _v
                       or r.get("t", "").casefold().startswith(_v + "/")
                       for r in ftags.get(norm_path(path), []))
        return pred
    # 旧语法兼容：tag:红 / tag:#ff453a
    low = v.lower().lstrip("#")
    colors = set()
    if len(low) == 6:
        try:
            int(low, 16)
            colors.add("#" + low)
        except ValueError:
            pass
    for cn, cv in COLOR_NAMES.items():
        if cn in low or low in cn:
            colors.add(cv)
    if not colors:
        return None

    def color_pred(_name, path, _st, _is_dir, _colors=colors):
        recs = ftags.get(norm_path(path), [])
        return any((defs.get(r.get("t"), {}).get("color") or "").lower() in _colors
                   for r in recs)
    return color_pred
