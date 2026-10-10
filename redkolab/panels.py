# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab N 面板顺序管理

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
    ("layout", "页面布局", "HHZ_PT_layout"),
)

#: 默认顺序
DEFAULT_SECTION_ORDER = tuple(section_id for section_id, _, _ in SECTIONS)

#: 顺序编辑器自己的 bl_order（固定在最后）
ORDER_PANEL_ORDER = 100

#: 分页标签条固定在最前，主面板第二
PAGES_PANEL_ORDER = 0
MAIN_PANEL_ORDER = 1

#: 主面板 / 顺序编辑器 / 分页标签条的注册名
MAIN_PANEL_IDNAME = "HHZ_PT_main"
ORDER_PANEL_IDNAME = "HHZ_PT_order"
PAGES_PANEL_IDNAME = "HHZ_PT_pages"

#: 拖动中的临时状态，供面板绘制时高亮：{"section": {"index": i, "target": j}}
DRAG_STATE = {}


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

    # 分页标签条永远第一，主面板第二（都不可拖动排序）
    for key, value in ((PAGES_PANEL_IDNAME, PAGES_PANEL_ORDER),
                       (MAIN_PANEL_IDNAME, MAIN_PANEL_ORDER)):
        panel = getattr(bpy.types, key, None)
        if panel is None:
            continue
        try:
            panel.bl_order = value
        except Exception:
            pass

    for index, section_id in enumerate(order):
        panel = _section_panel(section_id)
        if panel is None:
            continue
        try:
            panel.bl_order = index + 2
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
    """所有已注册的面板类（Python 注册的才会出现在 __subclasses__ 里）。

    必须排除 ``bpy.types.Panel`` 基类本身 —— 它不是 Python 注册的类，
    对它调用 register_class 会报 "already registered as a subclass"。
    """
    classes = []
    seen = set()
    base = bpy.types.Panel
    for panel in list(base.__subclasses__()):
        if panel is base:
            continue
        key = panel_key(panel)
        if not key or key in seen:
            continue
        seen.add(key)
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


def panel_key(panel):
    """面板在 ``bpy.types`` 里的注册名。

    不能用 Python 类名：很多插件类名和 ``bl_idname`` 不一样（例如类名
    ``MyPanel`` 但注册成 ``ADDON_PT_thing``），用类名备份会导致还原不回去。
    """
    try:
        return panel.bl_rna.identifier
    except Exception:
        return getattr(panel, "__name__", "")


# ------------------------------------------------- 真正生效的排序：注册顺序
#
# 为什么另起一套：实测 Blender 源码里 interface_panel.cc 只按**插入顺序**给面板编号
# （BKE_panel_new 也不读面板类型的 order），bl_order 只被少数自己排序的编辑器
# （图像 / 序列 / 节点 / 资产）读取。3D 视图侧栏里面板的先后**由注册顺序决定**，
# 所以要让拖动排序真的生效，只能注销后按目标顺序重新注册。
#
# 但**只重排本插件自己的面板**：实测把 Blender 内置 / 其它插件的面板注销再注册，
# 会让 Blender 4.0 / 4.2 直接段错误崩掉（5.x 报 already registered）。
# 所以"排别的插件的标签页"这个需求在安全范围内做不到，README 里有说明。


def _is_sidebar_panel(panel):
    """是否是 3D 视图侧栏里的顶层面板。"""
    try:
        if getattr(panel, "bl_space_type", "") != 'VIEW_3D':
            return False
        if getattr(panel, "bl_region_type", "") != 'UI':
            return False
        if getattr(panel, "bl_parent_id", ""):
            return False
        return bool(getattr(panel, "bl_category", ""))
    except Exception:
        return False


def panel_groups():
    """``{标签页: [面板注册名, ...]}``（只含侧栏里的顶层面板）。"""
    groups = {}
    for panel in _registered_panel_classes():
        if not _is_sidebar_panel(panel):
            continue
        key = panel_key(panel)
        category = getattr(panel, "bl_category", "")
        if not key or not category:
            continue
        group = groups.setdefault(category, [])
        if key not in group:
            group.append(key)
    return groups


def our_panel_keys(section_order=None):
    """本插件面板的注册名，按目标顺序：分页标签条 → 主面板 → 可排序面板 → 顺序编辑器。"""
    order = list(section_order) if section_order else list(DEFAULT_SECTION_ORDER)
    keys = [PAGES_PANEL_IDNAME, MAIN_PANEL_IDNAME]
    keys += [section_panel_idname(section_id) for section_id in order]
    keys.append(ORDER_PANEL_IDNAME)
    return [key for key in keys if key and key in dir(bpy.types)]


def reregister_our_panels(section_order=None):
    """只重排本插件自己的面板（不碰别的插件）。"""
    return apply_global_order(our_panel_keys(section_order))


def apply_global_order(keys):
    """注销后按给定顺序重新注册面板类；返回 ``(成功数, 失败列表)``。

    无论如何都会把注销掉的类注册回去（finally 式保证），不会留下半注销状态。
    """
    if not keys:
        return 0, []

    classes = []
    for key in keys:
        panel = getattr(bpy.types, key, None)
        if panel is not None and panel not in classes:
            classes.append(panel)
    if not classes:
        return 0, []

    for panel in classes:
        try:
            bpy.utils.unregister_class(panel)
        except Exception:
            pass

    registered = 0
    failures = []
    for panel in classes:
        try:
            bpy.utils.register_class(panel)
            registered += 1
        except Exception as error:
            failures.append("{}: {}".format(panel_key(panel), error))
    return registered, failures
