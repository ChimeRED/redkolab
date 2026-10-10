# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab · 主题颜色

在 N 面板里修改 Blender 界面主题的颜色（**不进饼菜单**）。

设计要点：

* ``TARGETS`` 里列的是「跨版本都稳妥」的颜色项，实际显示时再按当前 Blender
  **动态过滤**：5.x 才有的 ``user_interface.panel_*`` 在 4.0/4.2 上会自动消失，
  不需要写任何版本判断。
* 主题颜色有的存 3 个分量（RGB）、有的存 4 个（RGBA）。写入时按属性的
  ``array_length`` 自动裁剪，多出来的 alpha 不会导致报错。
* 主题颜色内部是 **8 位量化**存储的（写 0.5 会读回 128/255 = 0.50196），
  所以不要指望写入值能原样读回，比较颜色时要留约 1/255 的容差。
* 「恢复默认」只对**实测跨版本一致**的颜色提供默认值（视口那一组，见
  ``DEFAULT_COLORS``）；没有记录的颜色项不会假装有默认值，
  避免把 5.x 的默认值套到 4.0 上。
"""

import bpy

#: (路径, 标签, 分组)。路径必须在主题里是 3/4 分量的浮点颜色。
TARGETS = (
    # 视口
    ("view_3d.space.gradients.high_gradient", "视口背景（上）", "视口"),
    ("view_3d.space.gradients.gradient", "视口背景（下）", "视口"),
    ("view_3d.grid", "网格线", "视口"),
    ("view_3d.wire", "物体线框", "视口"),
    ("view_3d.vertex", "顶点", "视口"),
    ("view_3d.object_selected", "选中物体", "视口"),
    ("view_3d.object_active", "活动物体", "视口"),
    ("view_3d.camera", "摄像机", "视口"),
    ("view_3d.empty", "空物体", "视口"),
    ("view_3d.light", "灯光", "视口"),
    ("view_3d.bone_pose", "骨骼（姿态）", "视口"),
    # 界面控件
    ("user_interface.wcol_regular.inner", "控件内部", "界面"),
    ("user_interface.wcol_regular.outline", "控件边框", "界面"),
    ("user_interface.wcol_regular.text", "控件文字", "界面"),
    ("user_interface.wcol_regular.item", "控件条目", "界面"),
    ("user_interface.wcol_menu.inner", "菜单内部", "界面"),
    ("user_interface.wcol_menu.outline", "菜单边框", "界面"),
    ("user_interface.wcol_menu.text", "菜单文字", "界面"),
    ("user_interface.wcol_tool.inner", "工具栏按钮", "界面"),
    ("user_interface.wcol_tool.text", "工具栏文字", "界面"),
    ("user_interface.wcol_box.inner", "分组框内部", "界面"),
    ("user_interface.wcol_pie_menu.inner", "饼菜单内部", "界面"),
    ("user_interface.wcol_pie_menu.outline", "饼菜单边框", "界面"),
    ("user_interface.wcol_state.inner_changed", "已修改标记", "界面"),
    ("user_interface.icon_scene", "图标着色", "界面"),
    # 只有 5.x 有的面板色（低版本自动隐藏）
    ("user_interface.panel_back", "面板背景", "面板（5.x）"),
    ("user_interface.panel_header", "面板标题背景", "面板（5.x）"),
    ("user_interface.panel_text", "面板文字", "面板（5.x）"),
    ("user_interface.panel_title", "面板标题文字", "面板（5.x）"),
    ("user_interface.panel_outline", "面板描边", "面板（5.x）"),
    # 节点编辑器
    ("node_editor.grid", "节点网格", "节点"),
    ("node_editor.node_backdrop", "节点背景", "节点"),
    ("node_editor.wire", "节点连线", "节点"),
    ("node_editor.wire_select", "选中的连线", "节点"),
    ("node_editor.node_selected", "选中的节点", "节点"),
    # 曲线 / 动画编辑器
    ("graph_editor.grid", "曲线编辑器网格", "动画"),
    ("graph_editor.vertex", "曲线关键点", "动画"),
    ("dopesheet_editor.grid", "摄影表网格", "动画"),
)

#: 视口那一组的出厂默认值：4.0.2 / 4.2.23 / 5.2.2 实测完全一致，可以放心内嵌。
DEFAULT_COLORS = {
    "view_3d.space.gradients.high_gradient": (0.2392, 0.2392, 0.2392, 1.0),
    "view_3d.space.gradients.gradient": (0.1882, 0.1882, 0.1882, 1.0),
    "view_3d.grid": (0.3294, 0.3294, 0.3294, 0.502),
    "view_3d.wire": (0.0, 0.0, 0.0, 1.0),
    "view_3d.vertex": (0.0, 0.0, 0.0, 1.0),
    "view_3d.object_selected": (0.9294, 0.3412, 0.0, 1.0),
    "view_3d.object_active": (1.0, 0.6275, 0.1569, 1.0),
    "view_3d.camera": (0.0, 0.0, 0.0, 1.0),
    "view_3d.empty": (0.0, 0.0, 0.0, 1.0),
    "view_3d.light": (0.0, 0.0, 0.0, 0.3137),
    "view_3d.bone_pose": (0.3137, 0.7843, 1.0, 1.0),
}

#: 视口配色预设：只改「背景上/下 + 网格」，改动最小、效果最直观
PRESETS = (
    ("暖灰", {
        "view_3d.space.gradients.high_gradient": (0.240, 0.215, 0.195, 1.0),
        "view_3d.space.gradients.gradient": (0.185, 0.165, 0.150, 1.0),
        "view_3d.grid": (0.350, 0.330, 0.310, 0.5),
    }),
    ("墨绿", {
        "view_3d.space.gradients.high_gradient": (0.130, 0.200, 0.160, 1.0),
        "view_3d.space.gradients.gradient": (0.090, 0.150, 0.115, 1.0),
        "view_3d.grid": (0.250, 0.380, 0.300, 0.5),
    }),
    ("深蓝", {
        "view_3d.space.gradients.high_gradient": (0.120, 0.150, 0.230, 1.0),
        "view_3d.space.gradients.gradient": (0.080, 0.100, 0.170, 1.0),
        "view_3d.grid": (0.250, 0.300, 0.420, 0.5),
    }),
    ("纯黑", {
        "view_3d.space.gradients.high_gradient": (0.020, 0.020, 0.020, 1.0),
        "view_3d.space.gradients.gradient": (0.000, 0.000, 0.000, 1.0),
        "view_3d.grid": (0.180, 0.180, 0.180, 0.35),
    }),
    ("浅灰", {
        "view_3d.space.gradients.high_gradient": (0.650, 0.650, 0.660, 1.0),
        "view_3d.space.gradients.gradient": (0.550, 0.550, 0.560, 1.0),
        "view_3d.grid": (0.300, 0.300, 0.300, 0.5),
    }),
)


# ------------------------------------------------------------------- 基础操作


def current_theme(context):
    """当前主题（``preferences.themes[0]``）；取不到返回 None。"""
    try:
        themes = context.preferences.themes
        if not len(themes):
            return None
        return themes[0]
    except Exception:
        return None


def resolve(theme, path):
    """按点分路径找出 (宿主结构, 属性名)；路径不存在或不是颜色就返回 None。"""
    if theme is None or not path:
        return None
    parts = path.split(".")
    owner = theme
    for part in parts[:-1]:
        owner = getattr(owner, part, None)
        if owner is None:
            return None

    name = parts[-1]
    try:
        prop = owner.bl_rna.properties.get(name)
    except Exception:
        return None
    if prop is None or prop.type != 'FLOAT':
        return None
    if getattr(prop, "array_length", 0) not in (3, 4):
        return None
    return owner, name


def get_color(theme, path):
    """读颜色，统一返回 4 分量 (r, g, b, a)；读不到返回 None。"""
    resolved = resolve(theme, path)
    if resolved is None:
        return None
    owner, name = resolved
    try:
        value = tuple(float(component) for component in getattr(owner, name))
    except Exception:
        return None
    if len(value) == 3:
        return value + (1.0,)
    if len(value) == 4:
        return value
    return None


def component_count(theme, path):
    """颜色项的分量数（3 = RGB，4 = RGBA）；不可用返回 0。"""
    resolved = resolve(theme, path)
    if resolved is None:
        return 0
    owner, name = resolved
    try:
        return getattr(owner.bl_rna.properties.get(name), "array_length", 0)
    except Exception:
        return 0


def set_color(theme, path, color):
    """写颜色；自动按属性分量数裁剪，写成功返回 True。"""
    resolved = resolve(theme, path)
    if resolved is None or not color:
        return False
    owner, name = resolved
    prop = owner.bl_rna.properties.get(name)
    length = getattr(prop, "array_length", 4)
    components = [max(0.0, min(1.0, float(value))) for value in color]
    if len(components) < length:
        components = components + [1.0] * (length - len(components))
    try:
        setattr(owner, name, tuple(components[:length]))
        return True
    except Exception:
        return False


def target_items(self=None, context=None):
    """给 EnumProperty 用的动态条目：只列出当前 Blender 真的有的颜色。"""
    theme = current_theme(context if context is not None else bpy.context)
    items = []
    for path, label, group in TARGETS:
        if resolve(theme, path) is None:
            continue
        items.append((path, "{} · {}".format(group, label), path))
    if not items:
        items.append(("__unavailable__", "（当前版本没有可用颜色项）", ""))
    return items


def available_paths(context=None):
    """当前版本可用的颜色项路径列表。"""
    return [item[0] for item in target_items(None, context)]


def has_default(path):
    return path in DEFAULT_COLORS


def preset_names():
    return [name for name, _ in PRESETS]


def preset_colors(name):
    for preset_name, colors in PRESETS:
        if preset_name == name:
            return colors
    return None


# ----------------------------------------------------------- 面向偏好的读写


def read_into_preferences(context, preferences):
    """把当前选中项的主题颜色读进选择器（不改主题）。"""
    path = getattr(preferences, "theme_target", "")
    color = get_color(current_theme(context), path)
    if color is None:
        return False
    preferences.theme_color = color
    return True


def write_from_preferences(context, preferences):
    """把选择器里的颜色写进主题 —— 实时生效的那一步。"""
    path = getattr(preferences, "theme_target", "")
    return set_color(current_theme(context), path, tuple(preferences.theme_color))


# --------------------------------------------------------------- 批量操作


def apply_preset(theme, name):
    """套用一套视口预设，返回改动的颜色数量。"""
    colors = preset_colors(name)
    if not colors:
        return 0
    changed = 0
    for path, color in colors.items():
        if set_color(theme, path, color):
            changed += 1
    return changed


def restore_default(theme, path):
    """把某一项恢复为记录的默认值。"""
    color = DEFAULT_COLORS.get(path)
    if color is None:
        return False
    return set_color(theme, path, color)


def restore_viewport_defaults(theme):
    """把视口那一组全部恢复默认，返回改动数量。"""
    changed = 0
    for path in DEFAULT_COLORS:
        if restore_default(theme, path):
            changed += 1
    return changed
