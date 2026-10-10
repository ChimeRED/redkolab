# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab · N 面板顺序管理

提供两块能力：

1. **本插件自己分块的顺序** —— 「主题颜色 / 饼菜单与快捷键 / 插件设置」三块
   在 N 面板里的上下顺序，拖动即改，存进插件偏好设置。
2. **整个侧栏标签页（category）的顺序** —— 像 SIMPLE TABS 那样排其它插件的标签页。
   做法是把顺序写进各面板类的 ``bl_order``，改动前会把原值记下来，可以一键恢复。

依据（不是猜的）：

* ``Panel.bl_order`` 在 Blender 4.0.2 / 4.2.23 / 5.2.2 上都存在，
  并且官方提交 "UI: Use bl_order for UI child panels" 确认它就是用来排序子面板的
  （``bl_order`` 小的画在上面）。
* Blender **没有**把「当前标签页顺序」暴露给 Python，所以标签页那一块是
  「你拖出想要的顺序 → 我们写 bl_order」，而不是读取现状再改。列表初始顺序只是
  按名字排序的参考次序，不代表界面上真实的先后。
"""

import bpy

# ------------------------------------------------------------------ 常量

#: 可排序的分块：(id, 显示名, 面板类的 bl_idname)
SECTIONS = (
    ("theme", "主题颜色", "HHZ_PT_theme"),
    ("hotkey", "饼菜单与快捷键", "HHZ_PT_hotkey"),
    ("settings", "插件设置", "HHZ_PT_settings"),
)

#: 默认顺序
DEFAULT_SECTION_ORDER = tuple(section_id for section_id, _, _ in SECTIONS)

#: 顺序编辑器自己的 bl_order（固定在最后）
ORDER_PANEL_ORDER = 100

#: 主面板固定在最前
MAIN_PANEL_ORDER = 0

#: 标签页分组的基础 bl_order 间隔（同组内保持各自相对顺序）
TAB_ORDER_STEP = 1000

#: 拖动中的临时状态，供面板绘制时高亮：{"section"/"tab": {"index": i, "target": j}}
DRAG_STATE = {}


def dump_backup(backup):
    """把 ``{类名: 原 bl_order}`` 序列化成一行文本（存进偏好设置用）。"""
    return ";".join("{}={}".format(name, value) for name, value in sorted(dict(backup or {}).items()))


def load_backup(text):
    """还原 :func:`dump_backup` 的结果。"""
    result = {}
    for chunk in (text or "").split(";"):
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if not name:
            continue
        try:
            result[name] = int(value)
        except ValueError:
            continue
    return result


def section_label(section_id):
    for current_id, label, _ in SECTIONS:
        if current_id == section_id:
            return label
    return section_id


def section_panel_idname(section_id):
    for current_id, _, panel_idname in SECTIONS:
        if current_id == section_id:
            return panel_idname
    return None


# ------------------------------------------------------------------ 列表操作


def parse_order(text, valid_ids=None):
    """把 ``"a,b,c"`` 解析成有序去重列表。

    未知项丢掉；``valid_ids`` 里没出现在文本中的项按原顺序补到末尾，
    这样以后新增分块不会因为旧配置而消失。
    """
    valid = list(valid_ids if valid_ids is not None else DEFAULT_SECTION_ORDER)
    result = []
    for chunk in (text or "").split(","):
        name = chunk.strip()
        if name and name in valid and name not in result:
            result.append(name)
    for name in valid:
        if name not in result:
            result.append(name)
    return result


def move_item(items, index, target):
    """把 ``index`` 处的元素移动到 ``target`` 处，返回新列表。"""
    result = list(items)
    if not result:
        return result
    index = max(0, min(int(index), len(result) - 1))
    target = max(0, min(int(target), len(result) - 1))
    if index == target:
        return result
    value = result.pop(index)
    result.insert(target, value)
    return result


def target_from_drag(count, start_index, delta_pixels, row_height=20.0):
    """把「鼠标相对按下位置移动了多少像素」换算成目标槽位。

    拖动排序用相对位移而不是绝对坐标：面板里拿不到每行的屏幕位置，
    但鼠标位移是有的，按行高换算既简单又能跨主题/缩放工作。
    """
    if count <= 0:
        return 0
    steps = int(round(float(delta_pixels) / max(1.0, float(row_height))))
    return max(0, min(start_index + steps, count - 1))


# ---------------------------------------------------------------- 分块顺序


def _section_panel(section_id):
    idname = section_panel_idname(section_id)
    if not idname:
        return None
    return getattr(bpy.types, idname, None)


def apply_section_order(order, extra_orders=None):
    """按给定顺序写各面板的 ``bl_order``，返回实际改到的面板数。

    ``bl_order`` 小的排在前面（官方 RNA 说明），主面板固定 0、可排序面板从 1 开始，
    这样"主面板永远在最上"，其余按用户拖出来的顺序排。
    """
    applied = 0

    main_panel = getattr(bpy.types, "HHZ_PT_main", None)
    if main_panel is not None:
        try:
            main_panel.bl_order = MAIN_PANEL_ORDER
        except Exception:
            pass

    for index, section_id in enumerate(order):
        panel = _section_panel(section_id)
        if panel is None:
            continue
        try:
            panel.bl_order = index + 1
            applied += 1
        except Exception:
            pass
    if extra_orders:
        for idname, value in extra_orders.items():
            panel = getattr(bpy.types, idname, None)
            if panel is None:
                continue
            try:
                panel.bl_order = value
            except Exception:
                pass
    return applied


def read_section_order(preferences):
    """从偏好设置里读出当前分块顺序（并对缺失/未知项做容错）。"""
    text = getattr(preferences, "panel_order", "") if preferences is not None else ""
    return parse_order(text)


# ------------------------------------------------------------ 标签页顺序


def _registered_panel_classes():
    """所有已注册的面板类（Python 注册的才会出现在 __subclasses__ 里）。"""
    classes = []
    seen = set()
    for panel in list(bpy.types.Panel.__subclasses__()):
        if id(panel) in seen:
            continue
        seen.add(id(panel))
        classes.append(panel)
    return classes


def collect_tabs():
    """收集 3D 视图侧栏的标签页 → ``[(category, [面板类, ...]), ...]``。

    只看顶层面板：子面板跟着父面板走，不单独成页。
    """
    tabs = {}
    for panel in _registered_panel_classes():
        try:
            if getattr(panel, "bl_space_type", "") != 'VIEW_3D':
                continue
            if getattr(panel, "bl_region_type", "") != 'UI':
                continue
            if getattr(panel, "bl_parent_id", ""):
                continue
            category = getattr(panel, "bl_category", "")
        except Exception:
            continue
        if not category:
            continue
        tabs.setdefault(category, []).append(panel)
    return sorted(tabs.items())


def tab_names():
    """当前侧栏里的标签页名字（按名字排序，仅供参考）。"""
    return [name for name, _ in collect_tabs()]


def order_tabs(names, stored_order):
    """按存储顺序排列标签页名字，未记录的追加到末尾（保持名字排序）。"""
    result = []
    for name in parse_order(stored_order, valid_ids=names):
        if name in names and name not in result:
            result.append(name)
    for name in names:
        if name not in result:
            result.append(name)
    return result


def display_tab_order(preferences):
    """当前要显示的标签页顺序：存储顺序优先，新出现的标签页排到后面。"""
    stored = getattr(preferences, "tab_order", "") if preferences is not None else ""
    return order_tabs(tab_names(), stored)


def panel_key(panel):
    """面板在 ``bpy.types`` 里的注册名。

    不能用 Python 类名：很多插件类名和 ``bl_idname`` 不一样（例如类名
    ``MyPanel`` 但注册成 ``ADDON_PT_thing``），用类名备份会导致还原不回去。
    """
    try:
        return panel.bl_rna.identifier
    except Exception:
        return getattr(panel, "__name__", "")


def apply_tab_order(names, backup=None):
    """按给定的标签页顺序写 ``bl_order``。

    返回 ``(改动数量, 备份字典)``：备份是 ``{面板注册名: 原 bl_order}``，
    调用方应该把它存起来，方便「恢复默认顺序」。
    """
    tabs = dict(collect_tabs())
    backup = dict(backup or {})
    changed = 0

    for index, name in enumerate(names):
        for panel in tabs.get(name, []):
            key = panel_key(panel)
            if not key:
                continue
            try:
                if key not in backup:
                    backup[key] = int(getattr(panel, "bl_order", 0))
                # 用「首次记录的原值」做同组内偏移，反复应用也不会把别人
                # 同一标签页里的面板顺序抹平
                base = backup[key] % TAB_ORDER_STEP
                panel.bl_order = index * TAB_ORDER_STEP + base
                changed += 1
            except Exception:
                continue
    return changed, backup


def restore_tab_order(backup):
    """把 ``bl_order`` 还原成备份里的原值，返回还原数量。"""
    restored = 0
    for class_name, value in dict(backup or {}).items():
        panel = getattr(bpy.types, class_name, None)
        if panel is None:
            continue
        try:
            panel.bl_order = int(value)
            restored += 1
        except Exception:
            continue
    return restored
