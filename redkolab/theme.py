# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab · 主题颜色

在 N 面板里修改 Blender 界面主题的颜色（**不进饼菜单**）。

针对的是**可互动的控件**：灰色按钮（``wcol_regular.inner`` 那一批）、
被选中后变蓝的按钮（``wcol_regular.inner_sel`` 等）、以及已修改 / 动画等状态色。
视口颜色只是附带项，排在列表后面。

设计要点：

* ``TARGETS`` 里列的是「跨版本都稳妥」的颜色项，实际显示时再按当前 Blender
  **动态过滤**：5.x 才有的 ``user_interface.panel_*`` 与 ``wcol_state.error``
  在 4.0/4.2 上会自动消失，不需要写任何版本判断。
* 主题颜色有的存 3 个分量（RGB）、有的存 4 个（RGBA）。写入时按属性的
  ``array_length`` 自动裁剪，多出来的 alpha 不会导致报错。
* 主题颜色内部是 **8 位量化**存储的（写 0.5 会读回 128/255 = 0.50196），
  所以不要指望写入值能原样读回，比较颜色时要留约 1/255 的容差。
* 主题里存的**就是显示值**（0-255 归一化），所以 :func:`hex_color` 直接
  用 ``/255`` 换算，不做线性变换。
* 预设是一套「失落星船：马拉松」风格的**酸性配色**（``ACID`` 里是 hex 色值，
  方便微调），只改按钮与状态色，不动视口。
* 「恢复默认」只对**实测跨版本一致**的颜色提供默认值（见 ``DEFAULT_COLORS``，
  含按钮 / 状态色与视口）；没有记录的颜色项不会假装有默认值，
  避免把 5.x 的默认值套到 4.0 上。
"""

import bpy

#: (路径, 标签, 分组)。路径必须在主题里是 3/4 分量的浮点颜色。
#: 分组顺序就是下拉框里的顺序 —— **按钮优先**，这才是这个功能的主战场。
TARGETS = (
    # ---------------- 可互动按钮：常态（灰） ----------------
    ("user_interface.wcol_regular.inner", "普通按钮（灰）", "按钮·常态"),
    ("user_interface.wcol_regular.outline", "按钮描边", "按钮·常态"),
    ("user_interface.wcol_regular.text", "按钮文字", "按钮·常态"),
    ("user_interface.wcol_regular.item", "按钮滑块/高亮条", "按钮·常态"),
    ("user_interface.wcol_tool.inner", "工具按钮", "按钮·常态"),
    ("user_interface.wcol_toolbar_item.inner", "工具栏图标底", "按钮·常态"),
    ("user_interface.wcol_option.inner", "单选/复选按钮", "按钮·常态"),
    ("user_interface.wcol_toggle.inner", "开关按钮", "按钮·常态"),
    ("user_interface.wcol_radio.inner", "单选圈", "按钮·常态"),
    ("user_interface.wcol_num.inner", "数字输入框", "按钮·常态"),
    ("user_interface.wcol_numslider.inner", "数值滑块槽", "按钮·常态"),
    ("user_interface.wcol_text.inner", "文本输入框", "按钮·常态"),
    ("user_interface.wcol_menu.inner", "菜单项", "按钮·常态"),
    ("user_interface.wcol_menu_back.inner", "菜单背景", "按钮·常态"),
    ("user_interface.wcol_pie_menu.inner", "饼菜单项", "按钮·常态"),
    ("user_interface.wcol_tab.inner", "标签页", "按钮·常态"),
    ("user_interface.wcol_list_item.inner", "列表行", "按钮·常态"),
    ("user_interface.wcol_tooltip.inner", "提示框", "按钮·常态"),
    ("user_interface.wcol_progress.inner", "进度条底", "按钮·常态"),
    ("user_interface.wcol_scroll.inner", "滚动条槽", "按钮·常态"),
    ("user_interface.wcol_box.inner", "分组框", "按钮·常态"),
    # ---------------- 可互动按钮：选中态（默认是蓝的） ----------------
    ("user_interface.wcol_regular.inner_sel", "按钮选中（蓝）", "按钮·选中"),
    ("user_interface.wcol_regular.outline_sel", "选中按钮描边（5.x）", "按钮·选中"),
    ("user_interface.wcol_regular.text_sel", "选中按钮文字", "按钮·选中"),
    ("user_interface.wcol_tool.inner_sel", "工具按钮选中", "按钮·选中"),
    ("user_interface.wcol_toolbar_item.inner_sel", "工具栏图标选中", "按钮·选中"),
    ("user_interface.wcol_option.inner_sel", "单选/复选选中", "按钮·选中"),
    ("user_interface.wcol_toggle.inner_sel", "开关选中", "按钮·选中"),
    ("user_interface.wcol_radio.inner_sel", "单选圈选中", "按钮·选中"),
    ("user_interface.wcol_num.item", "数字框滑块", "按钮·选中"),
    ("user_interface.wcol_numslider.item", "数值滑块", "按钮·选中"),
    ("user_interface.wcol_menu.inner_sel", "菜单项选中（蓝）", "按钮·选中"),
    ("user_interface.wcol_menu_item.inner_sel", "菜单高亮行", "按钮·选中"),
    ("user_interface.wcol_pie_menu.inner_sel", "饼菜单项选中", "按钮·选中"),
    ("user_interface.wcol_tab.inner_sel", "当前标签页", "按钮·选中"),
    ("user_interface.wcol_list_item.inner_sel", "列表选中行（蓝）", "按钮·选中"),
    ("user_interface.wcol_progress.item", "进度条", "按钮·选中"),
    ("user_interface.wcol_scroll.inner_sel", "滚动条滑块", "按钮·选中"),
    ("user_interface.wcol_box.inner_sel", "分组框标题选中", "按钮·选中"),
    ("user_interface.wcol_text.inner_sel", "文本输入框选中", "按钮·选中"),
    ("user_interface.panel_active", "活动面板标题（5.x）", "按钮·选中"),
    # ---------------- 状态色（已修改 / 动画 / 关键帧…） ----------------
    ("user_interface.wcol_state.inner_changed", "已修改（默认橙）", "状态"),
    ("user_interface.wcol_state.inner_changed_sel", "已修改·选中", "状态"),
    ("user_interface.wcol_state.inner_anim", "有动画", "状态"),
    ("user_interface.wcol_state.inner_anim_sel", "有动画·选中", "状态"),
    ("user_interface.wcol_state.inner_key", "关键帧", "状态"),
    ("user_interface.wcol_state.inner_key_sel", "关键帧·选中", "状态"),
    ("user_interface.wcol_state.inner_driven", "驱动器", "状态"),
    ("user_interface.wcol_state.inner_driven_sel", "驱动器·选中", "状态"),
    ("user_interface.wcol_state.inner_overridden", "被覆盖", "状态"),
    ("user_interface.wcol_state.inner_overridden_sel", "被覆盖·选中", "状态"),
    ("user_interface.wcol_state.error", "错误（5.x）", "状态"),
    ("user_interface.wcol_state.warning", "警告（5.x）", "状态"),
    ("user_interface.wcol_state.info", "信息（5.x）", "状态"),
    ("user_interface.wcol_state.success", "成功（5.x）", "状态"),
    # ---------------- 视口（次要，需要时也能改） ----------------
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
    # ---------------- 其它 ----------------
    ("user_interface.panel_back", "面板背景（5.x）", "面板（5.x）"),
    ("user_interface.panel_header", "面板标题背景（5.x）", "面板（5.x）"),
    ("user_interface.panel_text", "面板文字（5.x）", "面板（5.x）"),
    ("user_interface.panel_title", "面板标题文字（5.x）", "面板（5.x）"),
    ("user_interface.panel_outline", "面板描边（5.x）", "面板（5.x）"),
    ("user_interface.icon_scene", "图标着色", "其它"),
    ("node_editor.grid", "节点网格", "节点"),
    ("node_editor.node_backdrop", "节点背景", "节点"),
    ("node_editor.wire", "节点连线", "节点"),
    ("node_editor.wire_select", "选中的连线", "节点"),
    ("node_editor.node_selected", "选中的节点", "节点"),
    ("graph_editor.grid", "曲线编辑器网格", "动画"),
    ("graph_editor.vertex", "曲线关键点", "动画"),
    ("dopesheet_editor.grid", "摄影表网格", "动画"),
)

#: 出厂默认值：**只在 4.0.2 与 5.2.2 上实测一致**的颜色才记在这里，
#: 这样「恢复默认」不会把 5.x 的默认值套到旧版上。5.x 专属项一律不记。
DEFAULT_COLORS = {
    # ---- 按钮常态 ----
    "user_interface.wcol_regular.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_regular.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_regular.text": (0.902, 0.902, 0.902, 1.0),
    "user_interface.wcol_regular.item": (0.1137, 0.1137, 0.1137, 0.502),
    "user_interface.wcol_tool.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_toolbar_item.inner": (0.1569, 0.1569, 0.1569, 1.0),
    "user_interface.wcol_option.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_toggle.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_radio.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_num.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_numslider.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_text.inner": (0.1137, 0.1137, 0.1137, 1.0),
    "user_interface.wcol_menu.inner": (0.1569, 0.1569, 0.1569, 1.0),
    "user_interface.wcol_menu_back.inner": (0.0941, 0.0941, 0.0941, 1.0),
    "user_interface.wcol_pie_menu.inner": (0.0941, 0.0941, 0.0941, 1.0),
    "user_interface.wcol_tab.inner": (0.1137, 0.1137, 0.1137, 1.0),
    "user_interface.wcol_list_item.inner": (1.0, 1.0, 1.0, 0.0),
    "user_interface.wcol_tooltip.inner": (0.1137, 0.1137, 0.1137, 1.0),
    "user_interface.wcol_progress.inner": (0.1333, 0.1333, 0.1333, 1.0),
    "user_interface.wcol_scroll.inner": (0.1333, 0.1333, 0.1333, 0.0),
    "user_interface.wcol_box.inner": (0.1137, 0.1137, 0.1137, 0.502),
    # ---- 按钮选中（默认蓝） ----
    "user_interface.wcol_regular.inner_sel": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_regular.text_sel": (1.0, 1.0, 1.0, 1.0),
    "user_interface.wcol_tool.inner_sel": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_toolbar_item.inner_sel": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_option.inner_sel": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_toggle.inner_sel": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_radio.inner_sel": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_num.item": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_numslider.item": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_menu.inner_sel": (0.2784, 0.4471, 0.702, 0.702),
    "user_interface.wcol_menu_item.inner_sel": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_pie_menu.inner_sel": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_tab.inner_sel": (0.1882, 0.1882, 0.1882, 1.0),
    "user_interface.wcol_list_item.inner_sel": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_progress.item": (0.2784, 0.4471, 0.702, 1.0),
    "user_interface.wcol_scroll.inner_sel": (1.0, 1.0, 1.0, 1.0),
    "user_interface.wcol_box.inner_sel": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_text.inner_sel": (0.0941, 0.0941, 0.0941, 1.0),
    # ---- 状态色 ----
    "user_interface.wcol_state.inner_changed": (0.8, 0.4588, 0.1608),
    "user_interface.wcol_state.inner_changed_sel": (0.8235, 0.3922, 0.0),
    "user_interface.wcol_state.inner_anim": (0.3255, 0.6, 0.1804),
    "user_interface.wcol_state.inner_anim_sel": (0.2196, 0.651, 0.0),
    "user_interface.wcol_state.inner_key": (0.702, 0.6824, 0.2118),
    "user_interface.wcol_state.inner_key_sel": (0.698, 0.5922, 0.0),
    "user_interface.wcol_state.inner_driven": (0.5647, 0.0, 0.8),
    "user_interface.wcol_state.inner_driven_sel": (0.6, 0.0, 0.902),
    "user_interface.wcol_state.inner_overridden": (0.0, 0.7647, 0.7647),
    "user_interface.wcol_state.inner_overridden_sel": (0.0667, 0.5608, 0.5608),
    # ---- 视口 ----
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


# ------------------------------------------------------- Marathon 酸性配色
#
# 失落星船：马拉松（Marathon）那种"酸性"观感：近黑底 + 高饱和酸性绿 + 橙色状态提示。
# 色值直接写 hex，方便你自己微调；主题色存的就是显示值（0-255 归一化），不需要做线性转换。

ACID = {
    "acid": "#C6FF00",       # 主酸性绿（选中按钮）
    "acid_hot": "#D8FF3D",   # 高对比酸性绿
    "acid_dim": "#8FBF00",   # 暗一档（列表选中）
    "ink": "#0B0D08",        # 近黑（带绿）
    "panel": "#171A12",      # 按钮底
    "panel_hi": "#232819",   # 按钮底（亮）
    "edge": "#3D4726",       # 描边（橄榄灰）
    "text": "#DCE8C8",       # 文字（偏绿白）
    "orange": "#FF7A18",     # 状态橙（已修改）
}


def hex_color(value, alpha=1.0):
    """把 ``#RRGGBB`` 换成主题要的 0-1 浮点元组。"""
    text = (value or "").lstrip("#")
    if len(text) != 6:
        return (1.0, 1.0, 1.0, alpha)
    rgb = tuple(int(text[index:index + 2], 16) / 255.0 for index in (0, 2, 4))
    return rgb + (alpha,)


def _acid_preset(hot=False):
    """生成一套酸性配色（hot=True 是高对比版本）。"""
    accent = ACID["acid_hot"] if hot else ACID["acid"]
    base = ACID["ink"] if hot else ACID["panel"]
    text = "#FFFFFF" if hot else ACID["text"]
    edge = ACID["acid_dim"] if hot else ACID["edge"]
    return {
        # 常态按钮
        "user_interface.wcol_regular.inner": hex_color(base),
        "user_interface.wcol_regular.outline": hex_color(edge),
        "user_interface.wcol_regular.text": hex_color(text),
        "user_interface.wcol_regular.item": hex_color(ACID["ink"], 0.5),
        "user_interface.wcol_tool.inner": hex_color(base),
        "user_interface.wcol_toolbar_item.inner": hex_color(ACID["ink"]),
        "user_interface.wcol_option.inner": hex_color(base),
        "user_interface.wcol_toggle.inner": hex_color(base),
        "user_interface.wcol_radio.inner": hex_color(base),
        "user_interface.wcol_num.inner": hex_color(base),
        "user_interface.wcol_numslider.inner": hex_color(base),
        "user_interface.wcol_text.inner": hex_color(ACID["ink"]),
        "user_interface.wcol_menu.inner": hex_color(base),
        "user_interface.wcol_menu_back.inner": hex_color(ACID["ink"]),
        "user_interface.wcol_pie_menu.inner": hex_color(ACID["ink"]),
        "user_interface.wcol_tab.inner": hex_color(ACID["ink"]),
        "user_interface.wcol_list_item.inner": hex_color(base, 0.0),
        "user_interface.wcol_tooltip.inner": hex_color(ACID["ink"]),
        "user_interface.wcol_progress.inner": hex_color(base),
        "user_interface.wcol_scroll.inner": hex_color(base, 0.0),
        "user_interface.wcol_box.inner": hex_color(ACID["ink"], 0.5),
        # 选中态：酸性绿 + 黑字
        "user_interface.wcol_regular.inner_sel": hex_color(accent),
        "user_interface.wcol_regular.outline_sel": hex_color(accent),
        "user_interface.wcol_regular.text_sel": hex_color(ACID["ink"]),
        "user_interface.wcol_tool.inner_sel": hex_color(accent),
        "user_interface.wcol_toolbar_item.inner_sel": hex_color(accent),
        "user_interface.wcol_option.inner_sel": hex_color(accent),
        "user_interface.wcol_toggle.inner_sel": hex_color(accent),
        "user_interface.wcol_radio.inner_sel": hex_color(accent),
        "user_interface.wcol_num.item": hex_color(accent),
        "user_interface.wcol_numslider.item": hex_color(accent),
        "user_interface.wcol_menu.inner_sel": hex_color(accent),
        "user_interface.wcol_menu_item.inner_sel": hex_color(accent),
        "user_interface.wcol_pie_menu.inner_sel": hex_color(accent),
        "user_interface.wcol_tab.inner_sel": hex_color(ACID["panel_hi"]),
        "user_interface.wcol_list_item.inner_sel": hex_color(ACID["acid_dim"]),
        "user_interface.wcol_progress.item": hex_color(accent),
        "user_interface.wcol_scroll.inner_sel": hex_color(accent),
        "user_interface.wcol_box.inner_sel": hex_color(ACID["panel_hi"]),
        "user_interface.wcol_text.inner_sel": hex_color(ACID["ink"]),
        "user_interface.panel_active": hex_color(accent),
        # 状态色
        "user_interface.wcol_state.inner_changed": hex_color(ACID["orange"]),
        "user_interface.wcol_state.inner_changed_sel": hex_color(ACID["orange"]),
        "user_interface.wcol_state.inner_anim": hex_color(ACID["acid_dim"]),
        "user_interface.wcol_state.inner_anim_sel": hex_color(accent),
        "user_interface.wcol_state.inner_key": hex_color("#E8C547"),
        "user_interface.wcol_state.inner_key_sel": hex_color(ACID["orange"]),
    }


#: 配色预设：改的是**可互动的按钮与状态色**（不再动视口）
PRESETS = (
    ("Marathon 酸性", _acid_preset(False)),
    ("Marathon 酸性·高对比", _acid_preset(True)),
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
    """套用一套配色预设（按钮常态 / 选中 / 状态色），返回改动的颜色数量。"""
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


def restore_defaults(theme, paths):
    """把给定的一组颜色全部恢复默认，返回改动数量。"""
    changed = 0
    for path in paths:
        if restore_default(theme, path):
            changed += 1
    return changed


def widget_paths():
    """记录了默认值的按钮 / 状态色路径。"""
    return [path for path in DEFAULT_COLORS if path.startswith("user_interface.")]


def viewport_paths():
    """记录了默认值的视口路径。"""
    return [path for path in DEFAULT_COLORS if path.startswith("view_3d.")]


def restore_widget_defaults(theme):
    """把按钮 / 状态色（也就是这个功能的主战场）全部恢复默认。"""
    return restore_defaults(theme, widget_paths())


def restore_viewport_defaults(theme):
    """把视口那一组全部恢复默认。"""
    return restore_defaults(theme, viewport_paths())
