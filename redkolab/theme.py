# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab 主题颜色

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
* 预设一共三套：两套「失落星船：马拉松」风格的**酸性配色**（``MARATHON`` 里是
  hex 色值）、一套「死亡搁浅」风格的**冷炭黑 + 蓝主强调**配色（``STRANDING``），
  都只改按钮与状态色；视口只写回两个辅助元素（网格线 / 法线）。
* 「恢复默认」只对**实测跨版本一致**的颜色提供默认值（见 ``DEFAULT_COLORS``，
  含按钮 / 状态色与视口）；没有记录的颜色项不会假装有默认值，
  避免把 5.x 的默认值套到 4.0 上。
"""

import colorsys
import json

import bpy

#: 控件家族：(属性前缀, 中文名)。每个家族都会展开成
#: 「底色 / 文字与图标 / 边框 / 选中底色 / 选中文字与图标 / 选中边框 / 高亮条」几项，
#: 这样按钮的底色、图标或文字颜色、边框颜色是**分别设置**的。
WIDGET_FAMILIES = (
    ("wcol_regular", "普通按钮"),
    ("wcol_tool", "工具按钮"),
    ("wcol_toolbar_item", "工具栏图标"),
    ("wcol_option", "单选/复选"),
    ("wcol_toggle", "开关按钮"),
    ("wcol_radio", "单选圈"),
    ("wcol_num", "数字输入框"),
    ("wcol_numslider", "数值滑块"),
    ("wcol_text", "文本输入框"),
    ("wcol_menu", "菜单项"),
    ("wcol_menu_back", "菜单背景"),
    ("wcol_menu_item", "菜单高亮行"),
    ("wcol_pie_menu", "饼菜单项"),
    ("wcol_tab", "标签页"),
    ("wcol_list_item", "列表行"),
    ("wcol_tooltip", "提示框"),
    ("wcol_progress", "进度条"),
    ("wcol_scroll", "滚动条"),
    ("wcol_box", "分组框"),
    ("wcol_pulldown", "下拉菜单"),
)

#: 每个家族里「成员后缀 -> (中文后缀, 分组)」
#: 分组名与标签一律用纯中文 + 空格，不放 `` `` 这类符号 ——
#: 有些自定义界面字体（偏好设置里换过字体的）画不出这些符号，会显示成乱码。
WIDGET_MEMBERS = (
    ("inner", "底色", "按钮 底色"),
    ("text", "文字与图标", "按钮 文字与图标"),
    ("outline", "边框", "按钮 边框"),
    ("inner_sel", "选中底色", "按钮 选中底色"),
    ("text_sel", "选中文字与图标", "按钮 选中文字"),
    ("outline_sel", "选中边框", "按钮 选中边框"),
    ("item", "高亮条与滑块", "按钮 高亮"),
)

#: N 面板「分开设置」里逐项列出的成员（顺序就是面板上的顺序）
PART_MEMBERS = WIDGET_MEMBERS

#: 全局"选中/活动"高亮 —— 这些不是 wcol 家族，但**选中态就是它们画的**：
#: 大纲视图选中行（selected_highlight，默认偏蓝）、活动行、文件浏览器选中文件、
#: 属性编辑器活动修改器、信息编辑器选中行、文本编辑器选中文字、控制台选区、
#: 编辑器图标描边与高亮。来自 Blender 源码里 ``TH_ACTIVE`` / ``TH_SELECT``
#: 的映射（``ts->active`` 即各空间自己的 active/select 色）。
GLOBAL_TARGETS = (
    ("outliner.selected_highlight", "大纲 选中行底色", "选中态 全局"),
    ("outliner.active", "大纲 活动行底色", "选中态 全局"),
    ("outliner.selected_object", "大纲 选中物体", "选中态 全局"),
    ("outliner.active_object", "大纲 活动物体", "选中态 全局"),
    ("file_browser.selected_file", "文件浏览器 选中文件", "选中态 全局"),
    ("properties.active_modifier", "属性 活动修改器", "选中态 全局"),
    ("info.info_selected", "信息编辑器 选中行", "选中态 全局"),
    ("text_editor.selected_text", "文本编辑器 选中文字", "选中态 全局"),
    ("console.select", "控制台 选区", "选中态 全局"),
    ("user_interface.editor_outline", "编辑器图标描边", "选中态 全局"),
    ("user_interface.editor_outline_active", "编辑器图标高亮 5.x", "选中态 全局"),
)

#: 简洁模式下的控件家族分组：一个选项 = 一批家族一起改
SIMPLE_FAMILY_GROUPS = (
    ("ALL", "全部控件 一起改", None),
    ("BUTTONS", "按钮", ("wcol_regular", "wcol_tool", "wcol_toolbar_item")),
    ("CHOICE", "选项与开关", ("wcol_option", "wcol_toggle", "wcol_radio")),
    ("INPUT", "输入框与滑块", ("wcol_num", "wcol_numslider", "wcol_text")),
    ("MENU", "菜单与列表", ("wcol_menu", "wcol_menu_back", "wcol_menu_item",
                            "wcol_pulldown", "wcol_pie_menu", "wcol_tab",
                            "wcol_list_item")),
    ("PANEL", "面板与其它", ("wcol_box", "wcol_tooltip", "wcol_progress", "wcol_scroll")),
)

#: 简洁模式下颜色项下拉里保留的常用项（前缀 + 精确名字）
SIMPLE_KEEP_PREFIXES = (
    "user_interface.wcol_regular.",
    "user_interface.wcol_tool.",
)
SIMPLE_KEEP_EXACT = (
    "outliner.selected_highlight",
    "outliner.active",
    "file_browser.selected_file",
    "properties.active_modifier",
    "user_interface.editor_outline",
    "user_interface.icon_object",
    "user_interface.icon_modifier",
    "user_interface.icon_collection",
    "user_interface.icon_folder",
    "dopesheet_editor.frame_current",
    "graph_editor.frame_current",
    "view_3d.frame_current",
    "user_interface.panel_active",
    "view_3d.space.gradients.high_gradient",
    "view_3d.grid",
    "user_interface.wcol_state.inner_changed",
    "user_interface.wcol_state.inner_anim",
)


def is_simple_path(path):
    """简洁模式下是否保留这个颜色项。"""
    return path in SIMPLE_KEEP_EXACT or path.startswith(SIMPLE_KEEP_PREFIXES)


def simple_family_items():
    """简洁模式的家族下拉项。"""
    return tuple((key, label, "") for key, label, _members in SIMPLE_FAMILY_GROUPS)


def families_for_key(key):
    """家族下拉的键 -> 要一起改的家族列表（分组键或单个家族名都认）。"""
    if not key or key == "__none__":
        return ()
    for group_key, _label, members in SIMPLE_FAMILY_GROUPS:
        if group_key != key:
            continue
        if members is None:
            return tuple(prefix for prefix, _name in WIDGET_FAMILIES)
        return tuple(members)
    if any(prefix == key for prefix, _name in WIDGET_FAMILIES):
        return (key,)
    return ()


#: 图标颜色（Blender 按对象类型给图标上的色）
ICON_TARGETS = (
    ("user_interface.icon_scene", "场景图标", "图标颜色"),
    ("user_interface.icon_collection", "集合图标", "图标颜色"),
    ("user_interface.icon_object", "物体图标", "图标颜色"),
    ("user_interface.icon_object_data", "物体数据图标", "图标颜色"),
    ("user_interface.icon_modifier", "修改器图标", "图标颜色"),
    ("user_interface.icon_shading", "着色图标", "图标颜色"),
    ("user_interface.icon_folder", "文件夹图标", "图标颜色"),
    ("user_interface.icon_autokey", "自动关键帧图标", "图标颜色"),
)

#: 时间线 / 当前帧（4.x 才有，5.x 已移除 —— 靠动态过滤自动隐藏）
#: 说明：主题里没有独立的 timeline 结构，当前帧色分散在各编辑器的 frame_current 上，
#: 时间轴的绘制走共享模块（``editors/animation/time_scrub_ui.cc``）。
FRAME_TARGETS = (
    ("dopesheet_editor.frame_current", "时间线/摄影表 当前帧", "时间线 当前帧"),
    ("dopesheet_editor.time_scrub_background", "时间线/摄影表 时间轴底色", "时间线 当前帧"),
    ("dopesheet_editor.time_marker_line", "时间线/摄影表 标记线", "时间线 当前帧"),
    ("dopesheet_editor.time_marker_line_selected", "时间线/摄影表 选中标记线", "时间线 当前帧"),
    ("graph_editor.frame_current", "曲线编辑器 当前帧", "时间线 当前帧"),
    ("graph_editor.time_scrub_background", "曲线编辑器 时间轴底色", "时间线 当前帧"),
    ("nla_editor.frame_current", "NLA 编辑器 当前帧", "时间线 当前帧"),
    ("nla_editor.time_scrub_background", "NLA 编辑器 时间轴底色", "时间线 当前帧"),
    ("sequence_editor.frame_current", "视频序列 当前帧", "时间线 当前帧"),
    ("sequence_editor.time_scrub_background", "视频序列 时间轴底色", "时间线 当前帧"),
    ("clip_editor.frame_current", "影片剪辑 当前帧", "时间线 当前帧"),
    ("clip_editor.time_scrub_background", "影片剪辑 时间轴底色", "时间线 当前帧"),
    ("image_editor.frame_current", "图像编辑器 当前帧", "时间线 当前帧"),
    ("view_3d.frame_current", "3D 视图 当前帧", "时间线 当前帧"),
)

#: 视口
VIEWPORT_TARGETS = (
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
)

#: 面板 / 节点 / 动画（次要）
MISC_TARGETS = (
    ("user_interface.panel_back", "面板背景（5.x）", "面板（5.x）"),
    ("user_interface.panel_header", "面板标题背景（5.x）", "面板（5.x）"),
    ("user_interface.panel_text", "面板文字（5.x）", "面板（5.x）"),
    ("user_interface.panel_title", "面板标题文字（5.x）", "面板（5.x）"),
    ("user_interface.panel_outline", "面板描边（5.x）", "面板（5.x）"),
    ("user_interface.panel_active", "活动面板标题（5.x）", "面板（5.x）"),
    ("user_interface.wcol_state.inner_changed", "已修改（默认橙）", "状态"),
    ("user_interface.wcol_state.inner_changed_sel", "已修改 选中", "状态"),
    ("user_interface.wcol_state.inner_anim", "有动画", "状态"),
    ("user_interface.wcol_state.inner_anim_sel", "有动画 选中", "状态"),
    ("user_interface.wcol_state.inner_key", "关键帧", "状态"),
    ("user_interface.wcol_state.inner_key_sel", "关键帧 选中", "状态"),
    ("user_interface.wcol_state.inner_driven", "驱动器", "状态"),
    ("user_interface.wcol_state.inner_driven_sel", "驱动器 选中", "状态"),
    ("user_interface.wcol_state.inner_overridden", "被覆盖", "状态"),
    ("user_interface.wcol_state.inner_overridden_sel", "被覆盖 选中", "状态"),
    ("user_interface.wcol_state.error", "错误（5.x）", "状态"),
    ("user_interface.wcol_state.warning", "警告（5.x）", "状态"),
    ("user_interface.wcol_state.info", "信息（5.x）", "状态"),
    ("user_interface.wcol_state.success", "成功（5.x）", "状态"),
    ("user_interface.wcol_state.inner_changed", "已修改标记", "状态"),
    ("node_editor.grid", "节点网格", "节点"),
    ("node_editor.node_backdrop", "节点背景", "节点"),
    ("node_editor.wire", "节点连线", "节点"),
    ("node_editor.wire_select", "选中的连线", "节点"),
    ("node_editor.node_selected", "选中的节点", "节点"),
    ("graph_editor.grid", "曲线编辑器网格", "动画"),
    ("graph_editor.vertex", "曲线关键点", "动画"),
    ("dopesheet_editor.grid", "摄影表网格", "动画"),
)


#: 3D 视图的编辑模式要素（顶点 / 边 / 面及其选中态）
EDIT_TARGETS = (
    ("view_3d.vertex_select", "顶点 选中", "3D 编辑模式"),
    ("view_3d.vertex_active", "顶点 活动", "3D 编辑模式"),
    ("view_3d.edge", "边", "3D 编辑模式"),
    ("view_3d.edge_select", "边 选中", "3D 编辑模式"),
    ("view_3d.edge_seam", "边 缝合线", "3D 编辑模式"),
    ("view_3d.edge_sharp", "边 锐边", "3D 编辑模式"),
    ("view_3d.edge_crease", "边 折痕", "3D 编辑模式"),
    ("view_3d.edge_bevel", "边 倒角权重", "3D 编辑模式"),
    ("view_3d.edge_facesel", "边 面选中", "3D 编辑模式"),
    ("view_3d.face", "面", "3D 编辑模式"),
    ("view_3d.face_select", "面 选中", "3D 编辑模式"),
    ("view_3d.face_dot", "面 中心点", "3D 编辑模式"),
    ("view_3d.editmesh_active", "编辑模式活动元素", "3D 编辑模式"),
    ("view_3d.normal", "法线", "3D 编辑模式"),
    ("view_3d.wire_edit", "编辑模式线框", "3D 编辑模式"),
    ("view_3d.transform", "变换轴", "3D 编辑模式"),
    ("view_3d.object_origin", "物体原点", "3D 编辑模式"),
)

#: UV 编辑与图像编辑器（UV 编辑是图像编辑器的一种模式，颜色都在 image_editor 上）
UV_TARGETS = (
    ("image_editor.vertex", "UV 顶点", "UV 与图像"),
    ("image_editor.vertex_select", "UV 顶点 选中", "UV 与图像"),
    ("image_editor.vertex_active", "UV 顶点 活动", "UV 与图像"),
    ("image_editor.edge_select", "UV 边 选中", "UV 与图像"),
    ("image_editor.face_select", "UV 面 选中", "UV 与图像"),
    ("image_editor.editmesh_active", "UV 活动元素", "UV 与图像"),
    ("image_editor.handle_vertex_select", "UV 手柄 选中", "UV 与图像"),
    ("image_editor.grid", "图像网格", "UV 与图像"),
    ("image_editor.back", "图像背景", "UV 与图像"),
)

#: 节点编辑器（着色器 / 几何节点 / 合成共用）
NODE_TARGETS = (
    ("node_editor.node_active", "活动节点", "节点编辑器"),
    ("node_editor.selected_text", "选中节点文字", "节点编辑器"),
    ("node_editor.frame_node", "节点框", "节点编辑器"),
    ("node_editor.node_outline", "节点描边 5.x", "节点编辑器"),
)

#: 文本编辑器与控制台
TEXT_TARGETS = (
    ("text_editor.text", "文本正文", "文本与控制台"),
    ("text_editor.cursor", "光标", "文本与控制台"),
    ("text_editor.line_highlight", "当前行高亮", "文本与控制台"),
    ("text_editor.selected_text", "选中文字", "文本与控制台"),
    ("text_editor.syntax_string", "语法 字符串", "文本与控制台"),
    ("text_editor.syntax_comment", "语法 注释", "文本与控制台"),
    ("text_editor.syntax_special", "语法 关键字", "文本与控制台"),
    ("console.text", "控制台文字", "文本与控制台"),
    ("console.cursor", "控制台光标", "文本与控制台"),
    ("console.line_error", "控制台错误行", "文本与控制台"),
    ("console.select", "控制台选区", "文本与控制台"),
)

#: 信息编辑器与警告色
INFO_TARGETS = (
    ("info.info_text", "信息 正文", "信息与警告"),
    ("info.info_selected_text", "信息 选中文字", "信息与警告"),
    ("info.info_warning", "信息 警告", "信息与警告"),
    ("info.info_error", "信息 错误", "信息与警告"),
    ("info.info_debug", "信息 调试", "信息与警告"),
    ("info.info_title", "信息 标题 5.x", "信息与警告"),
)

#: 面板层级（不同明度的深灰，避免所有面板一个色）
def _panel_targets():
    for prefix, label in (("view_3d", "3D 视图"),
                          ("outliner", "大纲视图"),
                          ("properties", "属性编辑器"),
                          ("node_editor", "节点编辑器")):
        yield ("{}.back".format(prefix), "{} 背景".format(label), "面板层级")
        yield ("{}.text".format(prefix), "{} 文字".format(label), "面板层级")
        yield ("{}.header".format(prefix), "{} 标题栏".format(label), "面板层级")
        yield ("{}.tab_back".format(prefix), "{} 标签底".format(label), "面板层级")
        yield ("{}.tab_active".format(prefix), "{} 活动标签".format(label), "面板层级")
        yield ("{}.tab_inactive".format(prefix), "{} 非活动标签".format(label), "面板层级")


PANEL_TARGETS = tuple(_panel_targets())


def _widget_targets():
    """把控件家族 × 成员展开成 (路径, 标签, 分组) 三元组。

    标签形如「普通按钮 底色」—— 纯中文 + 空格，不放符号。
    """
    for prefix, family_label in WIDGET_FAMILIES:
        for member, member_label, group in WIDGET_MEMBERS:
            yield (
                "user_interface.{}.{}".format(prefix, member),
                "{} {}".format(family_label, member_label),
                group,
            )


#: (路径, 标签, 分组)。分组顺序就是下拉框里的顺序 —— 按钮优先。
TARGETS = (
    tuple(_widget_targets())
    + GLOBAL_TARGETS
    + EDIT_TARGETS
    + UV_TARGETS
    + NODE_TARGETS
    + TEXT_TARGETS
    + INFO_TARGETS
    + PANEL_TARGETS
    + ICON_TARGETS
    + FRAME_TARGETS
    + VIEWPORT_TARGETS
    + MISC_TARGETS
)

#: 出厂默认值：**机械生成**（tools 里跑 4.0.2 / 4.2.23 / 5.2.2 三版导出的交集），
#: 所以「恢复默认」写回去的就是真 出厂值；没有记录到的项不会被瞎猜。
#: 三个表按运行版本合并使用，见 :func:`defaults_table`。
#: 4.0 / 4.2 / 5.2 三个版本实测完全一致的出厂值（机械生成，别手改）
DEFAULT_COLORS = {
    "console.line_error": (1.0000, 0.3020, 0.5176, 1.0),
    "console.select": (1.0000, 1.0000, 1.0000, 0.1882),
    "dopesheet_editor.grid": (0.0863, 0.0863, 0.0863, 1.0),
    "file_browser.selected_file": (0.2784, 0.4471, 0.7020, 1.0),
    "graph_editor.grid": (0.1020, 0.1020, 0.1020, 1.0),
    "graph_editor.vertex": (0.0000, 0.0000, 0.0000, 1.0),
    "image_editor.edge_select": (1.0000, 0.5216, 0.0000, 1.0),
    "image_editor.editmesh_active": (1.0000, 1.0000, 1.0000, 0.2510),
    "image_editor.face_select": (1.0000, 0.5216, 0.0000, 0.2353),
    "image_editor.grid": (0.1882, 0.1882, 0.1882, 1.0),
    "image_editor.vertex": (0.0000, 0.0000, 0.0000, 1.0),
    "image_editor.vertex_select": (1.0000, 0.5216, 0.0000, 1.0),
    "info.info_debug": (0.4196, 0.1961, 0.5765, 1.0),
    "info.info_selected": (0.2314, 0.3373, 0.5373, 1.0),
    "info.info_selected_text": (1.0000, 1.0000, 1.0000, 1.0),
    "node_editor.frame_node": (0.0588, 0.0588, 0.0588, 0.8000),
    "node_editor.node_active": (1.0000, 1.0000, 1.0000, 1.0),
    "node_editor.node_backdrop": (0.1882, 0.1882, 0.1882, 1.0),
    "node_editor.node_selected": (0.9294, 0.3412, 0.0000, 1.0),
    "node_editor.wire": (0.1020, 0.1020, 0.1020, 1.0),
    "node_editor.wire_select": (1.0000, 1.0000, 1.0000, 0.7020),
    "outliner.active": (0.2000, 0.3020, 0.5020, 1.0),
    "outliner.active_object": (1.0000, 0.6863, 0.1608, 1.0),
    "outliner.selected_highlight": (0.1137, 0.1922, 0.3020, 1.0),
    "outliner.selected_object": (0.9137, 0.4157, 0.0000, 1.0),
    "text_editor.selected_text": (0.3020, 0.3020, 0.3020, 1.0),
    "text_editor.syntax_comment": (0.5765, 0.5765, 0.5765, 1.0),
    "text_editor.syntax_special": (0.4078, 0.6157, 0.0235, 1.0),
    "text_editor.syntax_string": (0.9020, 0.8353, 0.4510, 1.0),
    "user_interface.icon_collection": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.icon_folder": (0.8000, 0.6784, 0.3882, 1.0),
    "user_interface.icon_modifier": (0.4549, 0.6353, 1.0000, 1.0),
    "user_interface.icon_object": (0.8824, 0.5882, 0.3451, 1.0),
    "user_interface.icon_object_data": (0.0000, 0.8314, 0.6392, 1.0),
    "user_interface.icon_scene": (0.8000, 0.8000, 0.8000, 1.0),
    "user_interface.icon_shading": (0.8000, 0.4000, 0.4392, 1.0),
    "user_interface.wcol_box.inner": (0.1137, 0.1137, 0.1137, 0.5020),
    "user_interface.wcol_box.inner_sel": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_box.item": (0.0980, 0.0980, 0.0980, 1.0),
    "user_interface.wcol_box.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_box.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_box.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_list_item.inner": (1.0000, 1.0000, 1.0000, 0.0000),
    "user_interface.wcol_list_item.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_list_item.item": (1.0000, 1.0000, 1.0000, 0.2000),
    "user_interface.wcol_list_item.text": (0.8000, 0.8000, 0.8000, 1.0),
    "user_interface.wcol_list_item.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_menu.inner": (0.1569, 0.1569, 0.1569, 1.0),
    "user_interface.wcol_menu.inner_sel": (0.2784, 0.4471, 0.7020, 0.7020),
    "user_interface.wcol_menu.item": (0.8510, 0.8510, 0.8510, 1.0),
    "user_interface.wcol_menu.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_menu.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_menu.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_menu_back.inner": (0.0941, 0.0941, 0.0941, 1.0),
    "user_interface.wcol_menu_back.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_menu_back.item": (0.8510, 0.8510, 0.8510, 1.0),
    "user_interface.wcol_menu_back.outline": (0.1412, 0.1412, 0.1412, 1.0),
    "user_interface.wcol_menu_back.text": (0.6000, 0.6000, 0.6000, 1.0),
    "user_interface.wcol_menu_back.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_menu_item.inner": (0.0941, 0.0941, 0.0941, 0.0000),
    "user_interface.wcol_menu_item.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_menu_item.item": (1.0000, 1.0000, 1.0000, 0.5608),
    "user_interface.wcol_menu_item.outline": (0.2392, 0.2392, 0.2392, 0.0000),
    "user_interface.wcol_menu_item.text": (0.8667, 0.8667, 0.8667, 1.0),
    "user_interface.wcol_menu_item.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_num.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_num.inner_sel": (0.1333, 0.1333, 0.1333, 1.0),
    "user_interface.wcol_num.item": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_num.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_num.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_num.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_numslider.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_numslider.inner_sel": (0.1333, 0.1333, 0.1333, 1.0),
    "user_interface.wcol_numslider.item": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_numslider.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_numslider.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_numslider.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_option.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_option.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_option.item": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_option.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_option.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_option.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_pie_menu.inner": (0.0941, 0.0941, 0.0941, 1.0),
    "user_interface.wcol_pie_menu.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_pie_menu.item": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_pie_menu.outline": (0.1412, 0.1412, 0.1412, 1.0),
    "user_interface.wcol_pie_menu.text": (0.8510, 0.8510, 0.8510, 1.0),
    "user_interface.wcol_pie_menu.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_progress.inner": (0.1333, 0.1333, 0.1333, 1.0),
    "user_interface.wcol_progress.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_progress.item": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_progress.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_progress.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_progress.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_pulldown.inner": (0.1333, 0.1333, 0.1333, 0.0000),
    "user_interface.wcol_pulldown.inner_sel": (1.0000, 1.0000, 1.0000, 0.1020),
    "user_interface.wcol_pulldown.item": (1.0000, 1.0000, 1.0000, 0.5608),
    "user_interface.wcol_pulldown.outline": (0.2392, 0.2392, 0.2392, 0.0000),
    "user_interface.wcol_pulldown.text": (0.8510, 0.8510, 0.8510, 1.0),
    "user_interface.wcol_pulldown.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_radio.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_radio.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_radio.item": (0.1451, 0.1451, 0.1451, 1.0),
    "user_interface.wcol_radio.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_radio.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_radio.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_regular.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_regular.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_regular.item": (0.1137, 0.1137, 0.1137, 0.5020),
    "user_interface.wcol_regular.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_regular.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_regular.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_scroll.inner": (0.1333, 0.1333, 0.1333, 0.0000),
    "user_interface.wcol_scroll.inner_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_scroll.item": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_scroll.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_scroll.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_scroll.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_state.inner_anim": (0.3255, 0.6000, 0.1804, 1.0),
    "user_interface.wcol_state.inner_anim_sel": (0.2196, 0.6510, 0.0000, 1.0),
    "user_interface.wcol_state.inner_changed": (0.8000, 0.4588, 0.1608, 1.0),
    "user_interface.wcol_state.inner_changed_sel": (0.8235, 0.3922, 0.0000, 1.0),
    "user_interface.wcol_state.inner_driven": (0.5647, 0.0000, 0.8000, 1.0),
    "user_interface.wcol_state.inner_driven_sel": (0.6000, 0.0000, 0.9020, 1.0),
    "user_interface.wcol_state.inner_key": (0.7020, 0.6824, 0.2118, 1.0),
    "user_interface.wcol_state.inner_key_sel": (0.6980, 0.5922, 0.0000, 1.0),
    "user_interface.wcol_state.inner_overridden": (0.0000, 0.7647, 0.7647, 1.0),
    "user_interface.wcol_state.inner_overridden_sel": (0.0667, 0.5608, 0.5608, 1.0),
    "user_interface.wcol_tab.inner": (0.1137, 0.1137, 0.1137, 1.0),
    "user_interface.wcol_tab.inner_sel": (0.1882, 0.1882, 0.1882, 1.0),
    "user_interface.wcol_tab.item": (0.1137, 0.1137, 0.1137, 1.0),
    "user_interface.wcol_tab.outline": (0.1137, 0.1137, 0.1137, 1.0),
    "user_interface.wcol_tab.text": (0.5961, 0.5961, 0.5961, 1.0),
    "user_interface.wcol_tab.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_text.inner": (0.1137, 0.1137, 0.1137, 1.0),
    "user_interface.wcol_text.inner_sel": (0.0941, 0.0941, 0.0941, 1.0),
    "user_interface.wcol_text.item": (1.0000, 1.0000, 1.0000, 0.2000),
    "user_interface.wcol_text.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_text.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_text.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_toggle.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_toggle.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_toggle.item": (0.1451, 0.1451, 0.1451, 1.0),
    "user_interface.wcol_toggle.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_toggle.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_toggle.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_tool.inner": (0.3294, 0.3294, 0.3294, 1.0),
    "user_interface.wcol_tool.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_tool.item": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_tool.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_tool.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_tool.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_toolbar_item.inner": (0.1569, 0.1569, 0.1569, 1.0),
    "user_interface.wcol_toolbar_item.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_toolbar_item.item": (1.0000, 1.0000, 1.0000, 0.7020),
    "user_interface.wcol_toolbar_item.outline": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_toolbar_item.text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_toolbar_item.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "user_interface.wcol_tooltip.inner": (0.1137, 0.1137, 0.1137, 1.0),
    "user_interface.wcol_tooltip.inner_sel": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.wcol_tooltip.item": (0.8510, 0.8510, 0.8510, 1.0),
    "user_interface.wcol_tooltip.outline": (0.1412, 0.1412, 0.1412, 1.0),
    "user_interface.wcol_tooltip.text": (0.8510, 0.8510, 0.8510, 1.0),
    "user_interface.wcol_tooltip.text_sel": (1.0000, 1.0000, 1.0000, 1.0),
    "view_3d.bone_pose": (0.3137, 0.7843, 1.0000, 1.0),
    "view_3d.camera": (0.0000, 0.0000, 0.0000, 1.0),
    "view_3d.editmesh_active": (1.0000, 1.0000, 1.0000, 0.2000),
    "view_3d.empty": (0.0000, 0.0000, 0.0000, 1.0),
    "view_3d.face": (1.0000, 1.0000, 1.0000, 0.0078),
    "view_3d.grid": (0.3294, 0.3294, 0.3294, 0.5020),
    "view_3d.light": (0.0000, 0.0000, 0.0000, 0.3137),
    "view_3d.normal": (0.1333, 0.8667, 0.8667, 1.0),
    "view_3d.object_active": (1.0000, 0.6275, 0.1569, 1.0),
    "view_3d.object_selected": (0.9294, 0.3412, 0.0000, 1.0),
    "view_3d.space.gradients.gradient": (0.1882, 0.1882, 0.1882, 1.0),
    "view_3d.space.gradients.high_gradient": (0.2392, 0.2392, 0.2392, 1.0),
    "view_3d.transform": (1.0000, 1.0000, 1.0000, 1.0),
    "view_3d.vertex": (0.0000, 0.0000, 0.0000, 1.0),
    "view_3d.vertex_select": (1.0000, 0.4784, 0.0000, 1.0),
    "view_3d.wire": (0.0000, 0.0000, 0.0000, 1.0),
    "view_3d.wire_edit": (0.0000, 0.0000, 0.0000, 1.0),
}

#: 5.x 专属项，或 5.x 与 4.x 取值不同的项（只在 5.x 上用）
DEFAULT_COLORS_5X = {
    "console.cursor": (0.4431, 0.6588, 1.0000, 1.0),
    "node_editor.grid": (0.1882, 0.1882, 0.1882, 1.0),
    "node_editor.node_outline": (1.0000, 1.0000, 1.0000, 0.1490),
    "text_editor.cursor": (0.4431, 0.6588, 1.0000, 1.0),
    "user_interface.editor_outline": (1.0000, 1.0000, 1.0000, 0.0824),
    "user_interface.editor_outline_active": (1.0000, 1.0000, 1.0000, 0.1647),
    "user_interface.icon_autokey": (0.6706, 0.2353, 0.2824, 1.0),
    "user_interface.panel_active": (0.2784, 0.4471, 0.7020, 1.0),
    "user_interface.panel_back": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.panel_header": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.panel_outline": (1.0000, 1.0000, 1.0000, 0.0667),
    "user_interface.panel_text": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.panel_title": (0.9020, 0.9020, 0.9020, 1.0),
    "user_interface.wcol_box.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_list_item.outline": (0.1765, 0.1765, 0.1765, 0.0000),
    "user_interface.wcol_list_item.outline_sel": (0.1765, 0.1765, 0.1765, 1.0),
    "user_interface.wcol_menu.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_menu_back.outline_sel": (0.1412, 0.1412, 0.1412, 1.0),
    "user_interface.wcol_menu_item.outline_sel": (0.2392, 0.2392, 0.2392, 0.0000),
    "user_interface.wcol_num.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_numslider.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_option.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_pie_menu.outline_sel": (0.1412, 0.1412, 0.1412, 1.0),
    "user_interface.wcol_progress.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_pulldown.outline_sel": (0.2392, 0.2392, 0.2392, 0.0000),
    "user_interface.wcol_radio.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_regular.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_scroll.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_state.error": (0.6000, 0.0863, 0.0863, 1.0),
    "user_interface.wcol_state.info": (0.1569, 0.2824, 0.4902, 1.0),
    "user_interface.wcol_state.success": (0.0941, 0.5255, 0.1451, 1.0),
    "user_interface.wcol_state.warning": (0.6745, 0.5294, 0.2157, 1.0),
    "user_interface.wcol_tab.outline_sel": (0.1137, 0.1137, 0.1137, 1.0),
    "user_interface.wcol_text.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_toggle.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_tool.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_toolbar_item.outline_sel": (0.2392, 0.2392, 0.2392, 1.0),
    "user_interface.wcol_tooltip.outline_sel": (0.1412, 0.1412, 0.1412, 1.0),
    "view_3d.edge_select": (1.0000, 0.6000, 0.0000, 1.0),
    "view_3d.face_select": (1.0000, 0.6392, 0.0000, 0.2000),
}

#: 4.x 专属项，或与 5.x 取值不同的项（只在 4.x 上用）
DEFAULT_COLORS_4X = {
    "clip_editor.frame_current": (0.2784, 0.4471, 0.7020, 1.0),
    "clip_editor.time_scrub_background": (0.0941, 0.0941, 0.0941, 1.0),
    "dopesheet_editor.frame_current": (0.2784, 0.4471, 0.7020, 1.0),
    "dopesheet_editor.time_marker_line": (1.0000, 1.0000, 1.0000, 0.3020),
    "dopesheet_editor.time_marker_line_selected": (1.0000, 1.0000, 1.0000, 0.7020),
    "dopesheet_editor.time_scrub_background": (0.1137, 0.1137, 0.1137, 1.0),
    "graph_editor.frame_current": (0.2784, 0.4471, 0.7020, 1.0),
    "graph_editor.time_scrub_background": (0.0863, 0.0863, 0.0863, 1.0),
    "image_editor.frame_current": (0.2784, 0.4471, 0.7020, 1.0),
    "image_editor.handle_vertex_select": (1.0000, 1.0000, 0.0000, 1.0),
    "info.info_error": (0.7020, 0.2980, 0.2039, 1.0),
    "info.info_warning": (0.6745, 0.5294, 0.2157, 1.0),
    "nla_editor.frame_current": (0.2784, 0.4471, 0.7020, 1.0),
    "nla_editor.time_scrub_background": (0.0863, 0.0863, 0.0863, 1.0),
    "node_editor.grid": (0.1569, 0.1569, 0.1569, 1.0),
    "node_editor.selected_text": (0.4980, 0.4980, 0.4980, 1.0),
    "properties.active_modifier": (0.2784, 0.4471, 0.7020, 1.0),
    "sequence_editor.frame_current": (0.2784, 0.4471, 0.7020, 1.0),
    "sequence_editor.time_scrub_background": (0.0706, 0.0706, 0.0706, 1.0),
    "user_interface.editor_outline": (0.0863, 0.0863, 0.0863, 1.0),
    "user_interface.wcol_list_item.outline": (0.1765, 0.1765, 0.1765, 1.0),
    "view_3d.edge_bevel": (0.0000, 0.6471, 1.0000, 1.0),
    "view_3d.edge_crease": (0.8000, 0.0000, 0.6000, 1.0),
    "view_3d.edge_facesel": (0.2941, 0.2941, 0.2941, 1.0),
    "view_3d.edge_seam": (0.8588, 0.1451, 0.0706, 1.0),
    "view_3d.edge_sharp": (0.0000, 1.0000, 1.0000, 1.0),
    "view_3d.face_dot": (1.0000, 0.5412, 0.0000, 1.0),
    "view_3d.frame_current": (0.2784, 0.4471, 0.7020, 1.0),
}


# ------------------------------------------------------- Marathon 酸性配色
#
# 失落星船：马拉松（Marathon）那种"酸性"观感：近黑底 + 高饱和酸性绿 + 橙色状态提示。
# 色值直接写 hex，方便你自己微调；主题色存的就是显示值（0-255 归一化），不需要做线性转换。

#: 失落星船 马拉松 的界面配色（照着游戏实拍图取色）
#: 近黑底 + 浅灰任务卡 + 酸性黄绿主行动色（READY 按钮）+ 洋红 / 青蓝点缀
#: ---------------------------------------------------------------- 设计 Token
#: 集中管理的配色变量：改这一块就能整体换风格，别在下面散落 RGB。
#: 取自《失落星船 马拉松》实拍图：近黑底 + 亮黄绿关键状态 + 浅灰卡 + 极细分隔线。
TOKENS = {
    "BG_PRIMARY": "#0B0D12",        # 主背景：编辑器底、3D 视口
    "BG_SECONDARY": "#11151C",      # 次级背景：交替行、输入框
    "BG_PANEL": "#141821",          # 面板背景
    "BG_ELEVATED": "#1E2430",       # 浮起面板：菜单、弹出、活动标签
    "ACCENT_PRIMARY": "#D6F32B",    # 主强调：荧光黄绿（选中态、关键按钮）
    "ACCENT_SECONDARY": "#3FB6E0",  # 次强调：冷青（活动态、次要标识）
    "ACCENT_TERTIARY": "#E85BC8",   # 第三强调：洋红（少量点缀，不铺开）
    "TEXT_PRIMARY": "#DDE3E8",      # 主要文字
    "TEXT_SECONDARY": "#9AA3AD",    # 次级文字
    "TEXT_DISABLED": "#5A626B",     # 禁用文字（Blender 只有可改的那几处）
    "TEXT_ON_ACCENT": "#0B0D12",    # 强调色上的文字（黄绿配黑字）
    "BORDER_SUBTLE": "#2A3038",     # 低对比分隔线
    "BORDER_STRONG": "#3C444E",     # 需要一点存在感的边框
    "STATE_SELECTED": "#D6F32B",    # 选中
    "STATE_ACTIVE": "#3FB6E0",      # 活动
    "STATE_WARNING": "#E8B33C",     # 警告
    "STATE_ERROR": "#E0433A",       # 错误 / 危险
    "ACCENT_PRIMARY_CALM": "#B9CE4E",   # 主强调的低饱和版（长时间工作用）
    "ACCENT_SECONDARY_CALM": "#5E8FA8", # 次强调的低饱和版
    "ACCENT_TERTIARY_CALM": "#9E6B93",  # 第三强调的低饱和版
    "CARD": "#C4C6C9",              # 浅灰卡片（实拍图里的任务卡纸面）
    "CARD_TEXT": "#0B0D12",         # 卡片上的近黑文字
}


def token(name, alpha=1.0):
    """取设计 Token（hex -> 0-1 浮点）。"""
    return hex_color(TOKENS.get(name, "#FFFFFF"), alpha)


#: 与实拍图一一对应的色名（Token 的别名，旧代码继续可用）
MARATHON = {
    "lime": TOKENS["ACCENT_PRIMARY"],
    "lime_dim": "#9FB420",
    "magenta": TOKENS["ACCENT_TERTIARY"],
    "magenta_dim": "#A03E8B",
    "cyan": TOKENS["ACCENT_SECONDARY"],
    "cyan_dim": "#2A7E9E",
    "crimson": TOKENS["STATE_ERROR"],
    "card": TOKENS["CARD"],
    "card_edge": "#8A8F96",
    "ink": TOKENS["BG_PRIMARY"],
    "panel": TOKENS["BG_PANEL"],
    "panel_hi": TOKENS["BG_ELEVATED"],
    "text": TOKENS["TEXT_PRIMARY"],
    "text_dim": TOKENS["TEXT_SECONDARY"],
    "border": TOKENS["BORDER_SUBTLE"],
    "warning": TOKENS["STATE_WARNING"],
    "lime_calm": TOKENS["ACCENT_PRIMARY_CALM"],
}

#: 旧名字保留（早期版本叫 ACID）
ACID = MARATHON


def marathon_color(key, alpha=1.0):
    """取调色板里的颜色（hex 转 0-1 浮点）。"""
    return hex_color(MARATHON.get(key, "#FFFFFF"), alpha)


# ------------------------------------------------------- 死亡搁浅 冷炭黑配色
#
# 「死亡搁浅」（DEATH STRANDING）观感：冷炭黑底 + **蓝为主强调** + **橙只做点缀**。
# 与 :data:`MARATHON` 同级的一份**集中管理**色表 —— 改这一块就能整体换风格，
# 预设函数（:func:`_stranding_preset`）里不再散落 hex。

#: 「死亡搁浅」色表：键 -> hex（注释里的用途就是用户逐项确认过的调色板注解）。
#: rgb 是 hex 的直接换算（主题色存的是显示值，不做线性变换）。
STRANDING = {
    "bg": "#14171A",          # 3D 视口 / 节点编辑器背景（冷炭黑）
    "panel": "#1C2024",       # 面板底、常态控件底
    "elevated": "#262B30",    # 弹出面板、活动标签
    "line": "#3A4046",        # 低对比边框 / 分隔
    "text": "#D6DADE",        # 主文字（灰白，不用纯白）
    "text_dim": "#8A9299",    # 次级文字、非活动标签
    "blue": "#4B7FA8",        # 主强调：选中行 / 选中态填充（多数家族 inner_sel）
    "blue_dim": "#2E4E68",    # 蓝的深档：要在它上面配浅色文字的场合
    "orange": "#D08A2E",      # 点缀强调：极少量关键项
    "orange_dim": "#8A5A1E",  # 橙的深档
    "steel": "#6E7B85",       # 技术性元素、线框
    "red": "#B4453C",         # 危险 / 被覆盖（克制）
    "warn": "#C9A227",        # 警告
    "disabled": "#5A626B",    # 禁用态（降亮但可辨）
}

#: 暖色（红 / 橙 / 琥珀）判定的色相窗口与下限，供 :func:`warm_accent` 用。
#: 0 ~ 0.16 覆盖红（#B4453C 约 4 度）到琥珀（#C9A227 约 46 度）；
#: 冷色（蓝 #4B7FA8 约 206 度、钢灰约 0.57）与炭黑（饱和度 < 0.3 的中性灰）
#: 都落在窗口之外，所以这个判据只会抓到"点缀暖色"。
WARM_HUE_MAX = 0.16
WARM_HUE_MIN_WRAP = 0.95
WARM_MIN_SATURATION = 0.3
WARM_MIN_VALUE = 0.2


def warm_accent(color):
    """这个颜色算不算「暖色点缀」（红 / 橙 / 琥珀）——是就返回 True。

    用户明确要求"蓝是主强调、橙只做点缀，绝不把所有选中态统一染橙"，
    :func:`warm_accent` 就是这条要求的机械化判据：一套预设里出现的暖色路径
    必须全部写在白名单上（死亡搁浅预设见 :data:`STRANDING_WARM_PATHS`）。
    """
    if color is None:
        return False
    values = list(color)[:3]
    while len(values) < 3:
        values.append(0.0)
    red, green, blue = (max(0.0, min(1.0, float(value))) for value in values)
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    if saturation < WARM_MIN_SATURATION or value < WARM_MIN_VALUE:
        return False
    return hue <= WARM_HUE_MAX or hue >= WARM_HUE_MIN_WRAP


def stranding_color(key, alpha=1.0):
    """取「死亡搁浅」色表里的颜色（hex 转 0-1 浮点）。"""
    return hex_color(STRANDING.get(key, "#FFFFFF"), alpha)



def hex_color(value, alpha=1.0):
    """把 ``#RRGGBB`` 换成主题要的 0-1 浮点元组。"""
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:  # 支持 #abc 简写
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return None
    try:
        rgb = tuple(int(text[index:index + 2], 16) / 255.0 for index in (0, 2, 4))
    except ValueError:
        return None
    return rgb + (alpha,)


def color_to_hex(color):
    """``(0-1, ...)`` -> ``#RRGGBB``（面板上显示明文用）。"""
    if not color:
        return "#000000"
    channels = list(color)[:3]
    while len(channels) < 3:
        channels.append(0.0)
    return "#" + "".join(
        "{:02X}".format(max(0, min(255, int(round(float(value) * 255.0)))))
        for value in channels
    )


def luminance(color):
    """相对亮度（0-1），用来决定文字该用深色还是浅色。"""
    if not color:
        return 0.0
    channels = list(color)[:3]
    while len(channels) < 3:
        channels.append(0.0)
    return (0.2126 * float(channels[0])
            + 0.7152 * float(channels[1])
            + 0.0722 * float(channels[2]))


#: 深色字 / 浅色字（酸性配色里偏绿的那两个极端，配深底浅底都清楚）
TEXT_ON_LIGHT = (0.043, 0.055, 0.031, 1.0)
TEXT_ON_DARK = (0.94, 0.96, 0.90, 1.0)


def saturation_of(color):
    """颜色的饱和度（0-1），用来对比"降饱和前后"。"""
    if not color:
        return 0.0
    values = list(color)[:3]
    while len(values) < 3:
        values.append(0.0)
    return colorsys.rgb_to_hsv(*(max(0.0, min(1.0, float(v))) for v in values))[1]


def desaturate_color(color, factor=1.0):
    """按倍数缩放饱和度：factor=1 原样、0 变灰。

    只动饱和度、保留色相与明度 —— 所以降饱和不会把界面调暗，也不会串色系。
    """
    if color is None:
        return None
    values = [float(v) for v in color]
    while len(values) < 3:
        values.append(0.0)
    red, green, blue = (max(0.0, min(1.0, v)) for v in values[:3])
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    saturation = max(0.0, min(1.0, saturation * float(factor)))
    red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
    alpha = values[3] if len(values) > 3 else 1.0
    return (red, green, blue, alpha)


def desaturate_colors(colors, factor=1.0):
    """批量降饱和（"把正在用的预设整体压灰"就靠它）。"""
    return {path: desaturate_color(color, factor)
            for path, color in dict(colors or {}).items()}


def readable_text_color(background, threshold=0.45):
    """按背景亮度挑一个看得清的文字色：深色字与浅色字里取对比更强的那个。

    这就是"按钮统一颜色后字看不清"的解法：底色一变，文字色跟着换，
    而且一定是两者里差距更大的那个（中间灰也能挑对）。
    """
    level = luminance(background)
    light_diff = abs(luminance(TEXT_ON_DARK) - level)
    dark_diff = abs(luminance(TEXT_ON_LIGHT) - level)
    if light_diff >= dark_diff:
        return TEXT_ON_DARK
    return TEXT_ON_LIGHT


def family_path(prefix, member):
    """控件家族 + 成员 -> 主题路径。"""
    return "user_interface.{}.{}".format(prefix, member)


def family_items(context):
    """当前 Blender 版本真实存在的控件家族 ``(前缀, 中文名)``。"""
    current = current_theme(context)
    result = []
    for prefix, label in WIDGET_FAMILIES:
        if resolve(current, family_path(prefix, "inner")) is not None:
            result.append((prefix, label))
    return tuple(result)


def read_family(theme, prefix):
    """读一个家族的各个成员颜色，返回 ``{成员: [r,g,b,a]}``（当前版本没有的成员不返回）。"""
    result = {}
    for member, _label, _group in PART_MEMBERS:
        color = get_color(theme, family_path(prefix, member))
        if color is not None:
            result[member] = [float(value) for value in color]
    return result


def write_family(theme, prefix, member, color):
    """把某个家族的某个成员颜色写进主题。"""
    return set_color(theme, family_path(prefix, member), color)


def scale_color(color, factor, alpha=None):
    """把颜色按倍数调暗/调亮（用来派生描边、浅底高亮等）。"""
    values = list(color)[:3]
    while len(values) < 3:
        values.append(0.0)
    result = tuple(max(0.0, min(1.0, float(value) * factor)) for value in values)
    if alpha is None:
        alpha = color[3] if len(color) > 3 else 1.0
    return result + (float(alpha),)


def global_highlight_colors(accent):
    """派生"全局选中高亮"那一组颜色。

    分两类，避免把浅色文字压在亮底上：
    * 当**背景**用的（大纲选中行、活动行、属性活动修改器、文本选中、控制台选区）
      用调暗的 accent，浅色文字照样看得清；
    * 当**前景/描边**用的（选中物体、选中文件、编辑器图标）用完整 accent。
    """
    accent = tuple(accent)
    dark = scale_color(accent, 0.35)
    mid = scale_color(accent, 0.55)
    return {
        "outliner.selected_highlight": dark,
        "outliner.active": mid,
        "text_editor.selected_text": mid,
        "console.select": scale_color(accent, 0.5, 0.35),
        "properties.active_modifier": mid,
        "outliner.selected_object": accent,
        "outliner.active_object": accent,
        "file_browser.selected_file": accent,
        "info.info_selected": accent,
        "user_interface.editor_outline": dark,
        "user_interface.editor_outline_active": accent,
    }


def global_button_colors(base, accent, text=None, outline=None, auto_text=True):
    """一套**全局按钮配色**：所有控件家族的底色 / 文字与图标 / 边框 / 选中态，
    外加各编辑器里的全局选中高亮（大纲、文件浏览器、属性、信息、控制台、编辑器图标）。

    这就是"一次改掉所有按钮颜色，包括预选中的选项"：返回 ``{路径: 颜色}``。
    """
    base = tuple(base)
    accent = tuple(accent)
    if text is None:
        text = readable_text_color(base)
    text = tuple(text)
    text_sel = readable_text_color(accent) if auto_text else text
    if outline is None:
        outline = scale_color(base, 1.35)

    colors = {}
    for prefix, _label in WIDGET_FAMILIES:
        colors[family_path(prefix, "inner")] = base
        colors[family_path(prefix, "text")] = text
        colors[family_path(prefix, "outline")] = outline
        colors[family_path(prefix, "inner_sel")] = accent
        colors[family_path(prefix, "text_sel")] = text_sel
        colors[family_path(prefix, "outline_sel")] = accent
        colors[family_path(prefix, "item")] = accent
    colors.update(global_highlight_colors(accent))
    # 需要保持透明的两项
    colors["user_interface.wcol_list_item.inner"] = scale_color(base, 1.0, 0.0)
    colors["user_interface.wcol_scroll.inner"] = scale_color(base, 1.0, 0.0)
    return colors


def apply_colors(theme, colors):
    """把一组 ``{路径: 颜色}`` 写进主题，返回成功数量（当前版本没有的项自动跳过）。"""
    applied = 0
    for path, color in dict(colors or {}).items():
        if color is None:
            continue
        if set_color(theme, path, color):
            applied += 1
    return applied


def label_of(path):
    """某个颜色项在面板上的中文名（找不到就返回路径本身）。"""
    for target_path, label, _group in TARGETS:
        if target_path == path:
            return label
    return path


def family_contrast_report(theme, prefix):
    """检查一个家族的"文字 vs 底色"对比度，返回问题描述列表。

    预设与自定义配色都该保证这里是空的 —— 否则就会出现"字看不清"。
    """
    colors = read_family(theme, prefix)
    problems = []
    for background_key, text_key, label in (
        ("inner", "text", "常态"),
        ("inner_sel", "text_sel", "选中"),
    ):
        background = colors.get(background_key)
        text_color = colors.get(text_key)
        if background is None or text_color is None:
            continue
        difference = abs(luminance(background) - luminance(text_color))
        if difference < 0.3:
            problems.append("{}：字与底色亮度差只有 {:.2f}".format(label, difference))
    return problems


#: 视口里当"参照物"的辅助元素：基面网格线（grid）与法线（normal）。
#: 这两项被旧版预设写成"半透明暗色 / 低饱和冷色"，比出厂值更暗 ——
#: 结果就是"网格线被减淡、法线看不清"。
#:
#: 修法**不是**"预设里不覆盖它们"：主题里已经被旧版写进去的暗值不会自己变回来，
#: 用户看到的就是"完全没更新"。所以三套预设一律把它们**显式写回出厂默认值**
#: （见 :func:`viewport_helper_colors`）—— 再套一次预设就自愈。
#: 两项仍在颜色清单里，用户想单独调可以自己调。
#:
#: 不在这一组里的：``view_3d.wire_edit`` / ``view_3d.vertex_active`` 之类被预设
#: 改的是**提亮**（出厂是纯黑，预设给浅灰或纯白），不是压暗，所以不适用本约束。
VIEWPORT_HELPERS = (
    "view_3d.grid",
    "view_3d.normal",
)

#: 视口里「**被选中的几何**」的四个颜色项：编辑模式活动元素、顶点 / 边 / 面 的选中色。
#:
#: 这几项**必须用鲜亮档强调色**（``ACCENT_PRIMARY``），不能跟界面控件一起用低饱和档
#: （``ACCENT_PRIMARY_CALM``）。理由来自用户实测反馈："被选中的线饱和度太低看不清"——
#: 界面控件降饱和是为了长时间工作耐看，而"视口里哪些东西被选中"是必须一眼看清的信息。
#: 上一版把两者一起降饱和，就是那次**过度应用**：界面照旧用低饱和档，视口选中几何回鲜亮档。
#:
#: **不在**这一组里（保持现设计，不要顺手改）：
#: ``view_3d.edge``（未选中边）、``view_3d.wire_edit``（编辑模式线框）、
#: ``view_3d.edge_seam`` / ``edge_sharp`` / ``edge_crease`` / ``edge_bevel``
#: （缝合线 / 锐边 / 折痕 / 倒角权重这些特征标记）、``view_3d.vertex_active``
#: （活动顶点，预设给的是白色），以及 :data:`VIEWPORT_HELPERS`（网格线 / 法线，
#: 继续写回出厂默认值）。
VIEWPORT_SELECTION_PATHS = (
    "view_3d.editmesh_active",
    "view_3d.vertex_select",
    "view_3d.edge_select",
    "view_3d.face_select",
)

#: 「标签与选中块分离」的控件家族：Blender 里这三类按钮的**标签文字不在选中块上**，
#: 只有那个小方块 / 小圆点 / 开关体用 ``inner_sel`` 填色，标签仍然读 ``text_sel``（源码依据：
#: ``interface_widgets.cc`` 的 ``widget_state()`` 把 ``inner``/``text`` 换成 ``inner_sel``/
#: ``text_sel``，随后 ``widget_optionbut()`` 把绘制矩形收成左边那个小方块、
#: ``widget_radiobut()`` 只画圆点 —— 文字都落在矩形之外）。
OPTION_BLOCK_FAMILIES = ("wcol_option", "wcol_radio", "wcol_toggle")

#: 选项块（小方块 / 圆点 / 开关体）相对强调色的亮度倍数。
#: 0.6 是实算出来的：三套预设的强调色按 0.6 调暗后亮度都在 0.45 上下，
#: 既让**选中块自身仍是醒目色**（与深色面板有对比，看得出选没选），
#: 又让上面的浅色标签文字满足「文字 / 底色亮度差 >= 0.3」（预设必须成对给色的那条不变量）。
OPTION_BLOCK_FACTOR = 0.6

#: 选项类家族的标签文字色（选中态）：用设计 Token 里的主文字色，浅灰白。
#: 不再用 ``TEXT_ON_ACCENT``（近黑）—— 那会把标签画成黑字落深色面板。
OPTION_LABEL_COLOR = TOKENS["TEXT_PRIMARY"]

#: **标签独立型家族**：标签文字**不画在选中块 ``inner_sel`` 上**的控件家族。
#: 这些家族的 ``text`` / ``text_sel`` 是**标签**色，落点是面板 / 菜单 / 工具提示的底色，
#: 所以预设必须让它们在**每一个可能落到的底色**上都读得清（见
#: :func:`label_contrast_problems`）。判据逐个来自 Blender 源码（三版一致）：
#:
#: * ``wcol_option``（复选）：``widget_optionbut()`` 把绘制矩形收成左边那个小方块后
#:   把 ``rect->xmin`` 推到方块右边 —— 标签的矩形**完全不含**选中块。
#:   源码里那行注释就是 "option buttons have strings outside"。
#: * ``wcol_menu_back``（菜单 / 弹出面板里的选项按钮）：``widget_state_option_menu()``
#:   在 ``BLOCK_THEME_STYLE_POPUP`` 里把选项按钮的 ``text`` / ``text_sel`` 覆盖成
#:   ``wcol_menu_back.text`` / ``.text_sel``，而底色仍是弹出面板自己的 ``inner``。
#:   这就是"叠加层里勾选后标签变黑看不见"的真凶（旧版把这族也套上了近黑 ``text_sel``）。
#: * ``wcol_radio`` / ``wcol_toggle``：与复选同一类选项按钮，弹出面板里同样走
#:   ``widget_state_option_menu()``；项目从 v2.10.1 起就把它们一起按"标签独立"处理
#:   （选中块只留小圆点 / 开关体，标签用浅色）。
#:
#: **不在**这一族里的（判据：标签确实压在选中块上，见 :func:`label_backdrops` 的说明）：
#: ``wcol_menu_item``（``widget_menu_itembut()`` 整行填充，``widget_state_menu_item()``
#: 把 ``text`` 换成 ``text_sel`` —— 就是用户要保留的"强调色底 + 近黑字"高亮行）、
#: ``wcol_pulldown``（``widget_pulldownbut()`` 整块填充 ``inner_sel`` 并把 ``text_sel``
#: 拷进 ``text``）。
LABEL_INDEPENDENT_FAMILIES = ("wcol_menu_back",) + OPTION_BLOCK_FAMILIES

#: 标签独立型家族的标签文字色（hex 写法，与 :data:`OPTION_LABEL_COLOR` 同源）：
#: 浅灰白 —— 落在三套预设的深色菜单 / 弹出面板底上都读得清。
LABEL_TEXT_COLOR = OPTION_LABEL_COLOR

#: **「深档强调色 + 浅色文字」的三个控件家族**：数值滑块 / 工具按钮 / 顶部工具项。
#:
#: 这三个家族曾经被整族**写回出厂默认值**（旧版有一份「必须保持出厂默认」的清单）。
#: 那样确实保住了可读性，代价是用户看到的"滑块变回系统蓝、工具栏变回默认灰" ——
#: 与整套预设的风格断裂（用户实测反馈）。现在改成"深一档的强调色"：
#:
#: * ``inner`` = 该主题的**深面板底色** —— 深色轨道正是这三个控件的原版手感；
#: * ``inner_sel`` = 强调色的**深档**（按 :data:`DEEP_ACCENT_FACTOR` 用
#:   :func:`scale_color` 派生的值，或调色板里已经备好的深档，如
#:   :data:`STRANDING` 的 ``blue_dim``）—— 深档才压得住**浅色文字**，
#:   同时与轨道拉开亮度差，拖动色块仍然分辨得出；
#: * ``text`` / ``text_sel`` = **浅色**（该预设的主文字色），值文字在两处都读得清。
#:
#: 为什么这三个家族必须单独成对配（逐条来自 Blender 源码，三版一致）：
#:
#: * ``wcol_numslider``（数值滑块）：颜色选择器的色相 / 饱和度 / 明度 / Alpha 与
#:   RGB 滑条都是 ``UI_BTYPE_NUM_SLIDER``（源码依据：4.0.2 ``interface_region_color_picker.cc``
#:   里这些行都是 ``UI_BTYPE_NUM_SLIDER``；5.2.2 同名模板同构），而
#:   ``widget_type(UI_WTYPE_SLIDER)`` / ``WidgetStyle::Slider`` 取的就是 ``wcol_numslider``
#:   （4.0.2 / 4.2.23 / 5.2.2 一致）。拖动时 ``widget_state()`` 把 ``inner`` 换成
#:   ``inner_sel``、``text`` 换成 ``text_sel`` —— 这三支颜色必须成对配好，
#:   否则就是"整条轨道变亮、值文字看不清"。
#: * ``wcol_tool``（工具按钮 / 图标按钮）：``WidgetStyle::Exec``（``ButtonType::But`` /
#:   ``Decorator`` 这类只有图标的按钮）用它，左侧 T 型工具栏的工具按钮就走这里；
#:   图标颜色取 ``wcol->text``（``widget_draw_text_icon()``：``icon_color = wcol->text``），
#:   选中时 ``widget_state()`` 换成 ``text_sel``。
#: * ``wcol_toolbar_item``（顶部工具项）：``WidgetStyle::ToolbarItem`` 用它，
#:   同样走 ``widget_roundbut_exec`` + ``widget_state`` 的图标按钮绘制。
#:   实测这三个家族在 4.0.2 / 4.2.23 / 5.2.2 上都有出厂默认值记录（真机 dump）。
#:
#: 不在这一组里的：``wcol_scroll``（滚动条，把手是 ``item``，与拖动色块无关）、
#: ``wcol_num``（数字输入框，不是滑块，没有可拖的填充条）、
#: ``wcol_progress``（进度条，只读）。``wcol_slider`` 这个家族**在三个版本上都不存在**
#: （真机 ``bl_rna`` 查不到），所以它不是候选。
DEEP_ACCENT_FAMILIES = (
    "wcol_numslider",
    "wcol_tool",
    "wcol_toolbar_item",
)

#: 深档强调的压暗倍数（马拉松那两套预设用 ``scale_color(accent, 0.6)`` 派生深档）。
#: 0.6 落在用户给的范围（45%~60%）的上沿，也是实算出来的：
#:
#: * 强调色 ``ACCENT_PRIMARY_CALM`` #B9CE4E 压到 0.6 得 #6F7C2F，相对亮度 0.4525；
#: * 与深面板 #141821（亮度 0.0933）差 **0.359** —— 轨道与拖动块分得开；
#: * 与浅色文字 #DDE3E8（亮度 0.8866）差 **0.434** —— 值文字压在填充上仍读得清；
#: * 再往下压到 0.45：文字更清楚，但轨道与填充的差只剩 0.246，拖动块会变糊。
DEEP_ACCENT_FACTOR = 0.6


def deep_accent_family_colors(accent, track, text, inner_sel=None):
    """「深档强调色 + 浅色文字」的三个家族配色，返回 ``{路径: 颜色}``。

    * ``accent`` —— 该预设的强调色（用来派生深档，除非显式给了 ``inner_sel``）；
    * ``track`` —— 轨道底色（该主题的深面板色）；
    * ``text`` —— 浅色文字；
    * ``inner_sel`` —— 深档强调色；给 ``None`` 时按 :data:`DEEP_ACCENT_FACTOR`
      用 :func:`scale_color` 从 ``accent`` 派生（派生方式写在这里，不散落 hex）。

    ``item``（高亮条 / 拖动色块）**不在这里覆盖**：它保持预设的强调色本身，
    于是"深色轨道 + 深档填充 + 强调色色块"三层仍然分得开。
    """
    if inner_sel is None:
        inner_sel = scale_color(accent, DEEP_ACCENT_FACTOR)
    colors = {}
    for prefix in DEEP_ACCENT_FAMILIES:
        colors[family_path(prefix, "inner")] = track
        colors[family_path(prefix, "inner_sel")] = inner_sel
        colors[family_path(prefix, "text")] = text
        colors[family_path(prefix, "text_sel")] = text
    return colors


def _option_block_override(block_color, label_color):
    """修正「选项类」三个家族的选中态配色，返回 ``{路径: 颜色}``。

    问题：预设给**每个**家族都配了 ``text_sel = TEXT_ON_ACCENT``（近黑），
    但复选 / 单选圈 / 开关的标签文字也是用 ``text_sel`` 画的 ——
    于是黑字落在深色面板上，等于看不见（视口操控器弹出面板里就是这样）。

    修法：标签统一用浅色（``TEXT_PRIMARY`` 这类浅灰白）保证在深色面板上可读；
    选中块用同一强调色调暗后的颜色，保持"选中块仍然是强调色"的语义，
    同时让「选中块上的小对勾」在深浅两个方向都还看得见。

    注意**浅色标签要用在三套预设上**，包括浅色底那套：标签落的是菜单 / 饼菜单 /
    提示框的底色，这几处三套预设都是深色（``wcol_menu_back.inner`` 等），
    只有浅色字才读得清；浅灰卡自己的卡片底色只用在别的控件上。
    """
    colors = {}
    for prefix in OPTION_BLOCK_FAMILIES:
        colors[family_path(prefix, "inner_sel")] = block_color
        colors[family_path(prefix, "text_sel")] = label_color
    return colors


def _label_family_override(block_color, label_color):
    """修正「标签独立型」家族的标签色，返回 ``{路径: 颜色}``。

    * 选项块（复选 / 单选圈 / 开关）：选中块仍是强调色调暗后的颜色（语义不变），
      **选中态**标签换成浅色（理由见 :func:`_option_block_override`）。
      常态 ``text`` 不动 —— 它落在**本家族自己的底色**上，浅灰卡那套是浅底配深字，
      统一改成浅色反而会让那套的常态标签糊掉（预设自己已经给了对的颜色）。
    * ``wcol_menu_back``：``text`` 与 ``text_sel`` **都**要浅色 —— 弹出面板里被勾选的
      选项按钮，标签色取的就是它的 ``text_sel``（``widget_state_option_menu()``），
      底色是面板自己的 ``inner``。旧版只在"浅灰卡"那一套顺手改过它，另外三套漏了，
      于是"打勾后标签变黑、落在深色弹出面板上看不见"。

    **不碰** ``inner_sel``：那是"菜单选中行"的语义位，三套预设各自的强调色底 + 近黑字
    是用户要的效果（真正的高亮行 ``wcol_menu_item`` 也一样，见
    :data:`LABEL_INDEPENDENT_FAMILIES` 的说明）。
    """
    colors = _option_block_override(block_color, label_color)
    for prefix in LABEL_INDEPENDENT_FAMILIES:
        if prefix in OPTION_BLOCK_FAMILIES:
            continue
        colors[family_path(prefix, "text")] = label_color
        colors[family_path(prefix, "text_sel")] = label_color
    return colors


def option_label_backdrops(colors, prefix):
    """选项类家族的**标签**可能落在哪些底色上（按顺序返回 ``(路径, 颜色)``）。

    复选 / 单选圈 / 开关的标签不在选中块上，而是落在工具提示、菜单、饼菜单或
    普通面板底上（源码依据见 :data:`OPTION_BLOCK_FAMILIES`）。这些底色互不相同，
    所以一组预设必须让标签文字在**每一个**底色上都读得清：只要有一个底色与文字的
    亮度差低于 0.3，就会出现"弹出面板里那行字看不见"。选中块自身的底色也算一份 ——
    万一某个控件把文字画在块上，同样不能糊。

    不收 ``wcol_menu.inner``：那一个是 ``UI_WTYPE_MENU_RADIO`` 系的菜单行底色，
    走的是各自的 ``wcol_menu.text_sel``，不是本家族的标签（``interface_widgets.cc``
    第 4616 行起）。
    """
    result = []
    for path in (family_path(prefix, "inner_sel"),
                 "user_interface.wcol_menu_back.inner",
                 "user_interface.wcol_pie_menu.inner",
                 "user_interface.panel_back"):
        color = colors.get(path)
        if color is not None:
            result.append((path, color))
    return result


def option_label_contrast_problems(colors, prefix, threshold=0.3):
    """选项类家族标签文字的可读性检查，返回问题描述列表。

    对每个可能的标签底色算一次亮度差，取**最差**的那个与阈值比 ——
    这就是 `"字看不清"` 的机械化判据，和 :func:`family_contrast_report` 同一个阈值。
    """
    text_color = colors.get(family_path(prefix, "text_sel"))
    if text_color is None:
        return ["{}：预设没给选中文字色".format(prefix)]
    problems = []
    for path, background in option_label_backdrops(colors, prefix):
        difference = abs(luminance(background) - luminance(text_color))
        if difference < threshold:
            problems.append("{}：标签与 {} 亮度差只有 {:.2f}".format(prefix, path, difference))
    return problems


def label_backdrops(colors, prefix):
    """一个「标签独立型」家族的标签**实际**会落在哪些底色上。

    * 复选 / 单选圈 / 开关：标签在选中块之外，可能落在菜单 / 饼菜单 / 普通面板底上，
      也可能被某个控件画在选中块上 —— 交给 :func:`option_label_backdrops` 全列出来。
    * 菜单 / 弹出面板（``wcol_menu_back``）：它的 ``text`` / ``text_sel`` 就是**这块面板
      里的普通文字色**（弹出面板里的选项按钮由 ``widget_state_option_menu()`` 借用它），
      所以实际底色是它自己的 ``inner``；``inner_sel`` 不是它的标签底（那是主题里
      "菜单选中行"的语义位，绘制 ``widget_menu_back()`` 只用 ``inner`` / ``outline``）。
    """
    if prefix in OPTION_BLOCK_FAMILIES:
        return option_label_backdrops(colors, prefix)
    result = []
    seen = set()
    for path in (family_path(prefix, "inner"),
                 "user_interface.wcol_menu_back.inner",
                 "user_interface.wcol_pie_menu.inner",
                 "user_interface.panel_back"):
        if path in seen:
            continue
        seen.add(path)
        color = colors.get(path)
        if color is not None:
            result.append((path, color))
    return result


def label_contrast_problems(colors, prefix, threshold=0.3):
    """「标签独立型」家族的标签可读性检查，返回问题描述列表。

    两件事都要过：

    * **常态标签** ``text`` 落在本家族自己的底色 ``inner`` 上（面板 / 菜单面板底）；
    * **选中标签** ``text_sel`` 落在 :func:`label_backdrops` 的每个底色上 ——
      包括菜单 / 饼菜单 / 普通面板底，因为勾选后的标签走 ``text_sel`` 而底色是面板底。
      这正是"叠加层里打勾后标签变黑看不见"的漏网处（旧版只看了 ``inner_sel`` 那一对）。
    """
    problems = []
    text_color = colors.get(family_path(prefix, "text"))
    own_inner = colors.get(family_path(prefix, "inner"))
    if text_color is None:
        problems.append("{}：预设没给常态文字色".format(prefix))
    elif own_inner is not None:
        difference = abs(luminance(own_inner) - luminance(text_color))
        if difference < threshold:
            problems.append("{}：text 与 {} 亮度差只有 {:.2f}".format(
                prefix, family_path(prefix, "inner"), difference))

    selected_color = colors.get(family_path(prefix, "text_sel"))
    if selected_color is None:
        problems.append("{}：预设没给选中文字色".format(prefix))
        return problems
    for path, background in label_backdrops(colors, prefix):
        difference = abs(luminance(background) - luminance(selected_color))
        if difference < threshold:
            problems.append("{}：text_sel 与 {} 亮度差只有 {:.2f}".format(prefix, path, difference))
    return problems


def _marathon_card_preset():
    """马拉松「浅灰任务卡」这一套（照着游戏实拍图里的任务卡纸面）。

    浅灰底 + 近黑字 + 酸性黄绿选中。旧版这个工厂函数还负责另外两套马拉松配色
    （一个偏青蓝、一个偏洋红），它们已按用户选择**彻底删除**（不保留存档），
    所以这里不再需要 ``style`` 参数，也不再需要只有那两套用到的
    ``cyan_calm`` / ``magenta_calm`` 两个色名。
    """
    base, text, edge = MARATHON["card"], MARATHON["ink"], MARATHON["card_edge"]
    accent, accent_text = MARATHON["lime_calm"], MARATHON["ink"]

    colors = {}
    for prefix, _label in WIDGET_FAMILIES:
        colors[family_path(prefix, "inner")] = hex_color(base)
        colors[family_path(prefix, "text")] = hex_color(text)
        colors[family_path(prefix, "outline")] = hex_color(edge)
        colors[family_path(prefix, "inner_sel")] = hex_color(accent)
        colors[family_path(prefix, "text_sel")] = hex_color(accent_text)
        colors[family_path(prefix, "outline_sel")] = hex_color(accent)
        colors[family_path(prefix, "item")] = hex_color(accent)

    colors.update({
        "user_interface.wcol_list_item.inner": hex_color(base, 0.0),
        "user_interface.wcol_scroll.inner": hex_color(base, 0.0),
        "user_interface.wcol_menu_back.inner": marathon_color("panel"),
        "user_interface.wcol_menu_back.text": marathon_color("text"),
        "user_interface.wcol_pie_menu.inner": marathon_color("panel"),
        "user_interface.wcol_pie_menu.text": marathon_color("text"),
        "user_interface.wcol_tab.inner": marathon_color("panel"),
        "user_interface.wcol_tab.text": marathon_color("text"),
        "user_interface.wcol_tab.inner_sel": hex_color(accent),
        "user_interface.wcol_tab.text_sel": hex_color(accent_text),
        "user_interface.wcol_text.inner": marathon_color("ink"),
        "user_interface.wcol_text.text": marathon_color("text"),
        "user_interface.wcol_text.inner_sel": hex_color(accent),
        "user_interface.wcol_text.text_sel": hex_color(accent_text),
        "user_interface.wcol_box.inner": hex_color(base, 0.5),
        "user_interface.wcol_box.inner_sel": hex_color(accent),
        "user_interface.wcol_list_item.inner_sel": hex_color(accent),
        "user_interface.panel_active": hex_color(accent),
        # 视口里「被选中的几何」用**鲜亮档**：界面控件走低饱和档，视口选中色不跟着压灰
        # （用户实测："被选中的线饱和度太低看不清"，见 VIEWPORT_SELECTION_PATHS）
        "view_3d.editmesh_active": hex_color(MARATHON["lime"]),
        "view_3d.vertex_select": hex_color(MARATHON["lime"]),
        "view_3d.edge_select": hex_color(MARATHON["lime"]),
        "view_3d.face_select": hex_color(MARATHON["lime"], 0.5),
        "user_interface.wcol_state.inner_changed": marathon_color("lime"),
        "user_interface.wcol_state.inner_changed_sel": marathon_color("lime_dim"),
        "user_interface.wcol_state.inner_anim": marathon_color("cyan"),
        "user_interface.wcol_state.inner_anim_sel": marathon_color("cyan_dim"),
        "user_interface.wcol_state.inner_key": marathon_color("card"),
        "user_interface.wcol_state.inner_key_sel": marathon_color("magenta"),
        "user_interface.wcol_state.inner_driven": marathon_color("magenta"),
        "user_interface.wcol_state.inner_driven_sel": marathon_color("magenta_dim"),
        "user_interface.wcol_state.inner_overridden": marathon_color("crimson"),
        "user_interface.wcol_state.inner_overridden_sel": marathon_color("crimson"),
    })

    colors.update(global_highlight_colors(hex_color(accent)))
    # 浅灰卡这套里，菜单底 / 选中行底仍是深色（``panel`` / ``ink``），
    # 而菜单里的复选、单选圈走的是 ``wcol_menu_back.text`` 与 ``.text_sel``
    # （``interface_widgets.cc`` 的 ``widget_state_option_menu()`` 会用它覆盖家族色），
    # 所以标签文字必须是浅色 —— 这一条现在由 ``_label_family_override()`` 对
    # **三套预设**统一处理（不再只有浅灰卡这一套）。
    # 这里只保留"菜单选中行底色压深"：它上面配的就是那个浅色（差 0.75/0.79），
    # 而原来的浅色选中行 + 浅色文字只有 0.13，改完两头都读得清。
    colors["user_interface.wcol_menu_back.inner_sel"] = hex_color(MARATHON["ink"])
    # 标签独立型家族的标签文字：三套预设统一用浅色（理由见 _label_family_override）
    colors.update(_label_family_override(scale_color(hex_color(accent), OPTION_BLOCK_FACTOR),
                                          hex_color(LABEL_TEXT_COLOR)))
    # 滑块 / 工具按钮 / 顶部工具项：深面板轨道 + 深档强调 + 浅色文字。
    # 这一套的卡片纸面是浅灰，浅色文字落在浅灰上只有 0.11 的亮度差（不达标），
    # 所以这三族统一用 MARATHON["panel"] 这个**深面板色**当轨道 —— 见
    # DEEP_ACCENT_FAMILIES 的说明，这也是"深色轨道 + 可拖动色块"的原版手感。
    colors.update(deep_accent_family_colors(
        hex_color(accent), hex_color(MARATHON["panel"]), hex_color(TOKENS["TEXT_PRIMARY"])))
    colors.update(_frame_current_colors(hex_color(accent)))
    return {path: color for path, color in colors.items() if color is not None}


def _frame_current_colors(accent):
    """时间线 / 当前帧跟着强调色走（4.x 才有这些项，5.x 上写入会自然失败）。"""
    return {
        "dopesheet_editor.frame_current": accent,
        "graph_editor.frame_current": accent,
        "nla_editor.frame_current": accent,
        "sequence_editor.frame_current": accent,
        "clip_editor.frame_current": accent,
        "image_editor.frame_current": accent,
        "view_3d.frame_current": accent,
    }


#: 「死亡搁浅」预设里**允许**出现暖色（红 / 橙 / 琥珀）的路径白名单。
#:
#: 用户明确要求「蓝是主强调、橙只做点缀」——所以暖色路径是有清单、有上限的：
#: 真机断言会拿 :func:`warm_accent` 扫一遍整套预设，出现清单外的暖色就失败
#: （``tools/blender_test.py`` 的 ``test_preset_death_stranding``）。
#:
#: 清单分三类语义，每类只落一处，加起来 8 条：
#: * 活动面板标题 + 已修改状态 —— 调色板里 ``orange`` / ``orange_dim`` 的指定用途；
#: * 关键帧选中 / 倒角权重 —— ``warn``（警告）；
#: * 被覆盖 / 边缝合线 —— ``red``（危险，克制）。
STRANDING_WARM_PATHS = (
    "user_interface.panel_active",
    "user_interface.wcol_state.inner_changed",
    "user_interface.wcol_state.inner_changed_sel",
    "user_interface.wcol_state.inner_key_sel",
    "user_interface.wcol_state.inner_overridden",
    "user_interface.wcol_state.inner_overridden_sel",
    "view_3d.edge_seam",
    "view_3d.edge_bevel",
)


def _stranding_preset():
    """「死亡搁浅」：冷炭黑 + **蓝为主强调** + **橙只做点缀**。

    用户逐项确认的设计约束（同时是真机断言的判据）：

    * **蓝是唯一的主选中色**：控件家族的 ``inner_sel`` 只会是 ``blue`` 或
      ``blue_dim``（``wcol_tab`` 的 ``inner_sel`` 是面板层级色 ``elevated``）；
      **绝不**把所有选中态统一染成橙 —— 这条由 :func:`warm_accent` +
      :data:`STRANDING_WARM_PATHS` 机械守住；
    * **橙只落在白名单路径上**（活动面板标题、已修改状态），是点缀不是主色；
    * 常态文字用灰白 ``text`` / ``text_dim``，不用纯白大面积铺陈；
      压在亮蓝填充上的**选中态**文字用最暗的 ``bg``（推导见下面的 ``on_blue``）；
    * 面板三级层次 ``bg`` < ``panel`` < ``elevated``（亮度递增），边框用低对比 ``line``；
    * 视口辅助元素（``view_3d.grid`` / ``view_3d.normal``）**不在这里**给值 ——
      由 :func:`viewport_helper_colors` 统一写回出厂默认值（网格和默认一样明显）。

    色值全部来自 :data:`STRANDING`（与 :data:`MARATHON` 同级的一份集中色表），
    这里一个 hex 都不写。
    """
    base = stranding_color("panel")
    text = stranding_color("text")
    blue = stranding_color("blue")
    # 压在 DS_BLUE 填充上的**选中态文字**：调色板里最暗的冷炭黑 DS_BG。
    #: 为什么不用浅灰白的 DS_TEXT：把整套预设降饱和到 40% 之后，DS_BLUE 会从
    #: 亮度 0.4663 升到约 0.586（饱和蓝降饱和后三通道靠近，B 只占亮度权重的 0.07），
    #: 浅色文字的差只剩约 0.27，不满足项目既有的「降饱和后文字/底色差 >= 0.3」；
    #: 配深色则全程够（满饱和 0.399、降饱和后 0.519），而且这正是项目既有的
    #: "菜单高亮行 = 强调色底 + 近黑字"设计（见 LABEL_INDEPENDENT_FAMILIES 的说明）。
    on_blue = stranding_color("bg")
    colors = {}
    for prefix, _label in WIDGET_FAMILIES:
        colors[family_path(prefix, "inner")] = base
        colors[family_path(prefix, "text")] = text
        colors[family_path(prefix, "outline")] = stranding_color("line")
        colors[family_path(prefix, "inner_sel")] = blue
        colors[family_path(prefix, "text_sel")] = on_blue
        colors[family_path(prefix, "outline_sel")] = blue
        colors[family_path(prefix, "item")] = blue
    colors.update({
        "user_interface.wcol_list_item.inner": stranding_color("panel", 0.0),
        "user_interface.wcol_scroll.inner": stranding_color("panel", 0.0),
        "user_interface.wcol_menu_back.inner": stranding_color("elevated"),
        "user_interface.wcol_menu_back.text": text,
        "user_interface.wcol_pie_menu.inner": stranding_color("elevated"),
        "user_interface.wcol_pie_menu.text": text,
        # 非活动标签用次级文字色，活动标签的底用浮起面板色（层级，不是强调色）
        "user_interface.wcol_tab.inner": stranding_color("bg"),
        "user_interface.wcol_tab.text": stranding_color("text_dim"),
        "user_interface.wcol_tab.inner_sel": stranding_color("elevated"),
        "user_interface.wcol_tab.text_sel": text,
        "user_interface.wcol_text.inner": stranding_color("bg"),
        "user_interface.wcol_text.text": text,
        "user_interface.wcol_text.inner_sel": stranding_color("blue_dim"),
        "user_interface.wcol_text.text_sel": text,
        "user_interface.wcol_box.inner": stranding_color("panel", 0.5),
        "user_interface.wcol_box.inner_sel": blue,
        "user_interface.wcol_list_item.inner_sel": blue,
        # 面板三级层次：bg -> panel -> elevated
        "view_3d.back": stranding_color("bg"),
        "outliner.back": base,
        "properties.back": base,
        "node_editor.back": stranding_color("bg"),
        "outliner.header": stranding_color("elevated"),
        "properties.header": stranding_color("elevated"),
        "outliner.tab_back": base,
        "outliner.tab_active": stranding_color("elevated"),
        "outliner.tab_inactive": stranding_color("bg"),
        "properties.tab_back": base,
        "properties.tab_active": stranding_color("elevated"),
        "properties.tab_inactive": stranding_color("bg"),
        # 5.x 的面板配色项（4.x 上写入会自然失败，不报错）
        "user_interface.panel_back": base,
        "user_interface.panel_header": stranding_color("elevated"),
        "user_interface.panel_text": text,
        "user_interface.panel_title": text,
        "user_interface.panel_outline": stranding_color("line"),
        # ---- 点缀暖色 1/2：活动面板标题 ----
        "user_interface.panel_active": stranding_color("orange"),
        # ---- 点缀暖色 2/2：已修改状态（调色板里 orange 的指定用途）----
        "user_interface.wcol_state.inner_changed": stranding_color("orange"),
        "user_interface.wcol_state.inner_changed_sel": stranding_color("orange_dim"),
        # 其余状态色：蓝 / 钢灰 / 克制的红，都不铺开
        "user_interface.wcol_state.inner_anim": blue,
        "user_interface.wcol_state.inner_anim_sel": stranding_color("blue_dim"),
        "user_interface.wcol_state.inner_key": text,
        "user_interface.wcol_state.inner_key_sel": stranding_color("warn"),
        "user_interface.wcol_state.inner_driven": stranding_color("steel"),
        "user_interface.wcol_state.inner_driven_sel": stranding_color("steel"),
        "user_interface.wcol_state.inner_overridden": stranding_color("red"),
        "user_interface.wcol_state.inner_overridden_sel": stranding_color("red"),
        # 3D 编辑模式：选中一律蓝，线框 / 技术元素用钢灰
        "view_3d.editmesh_active": blue,
        "view_3d.vertex_select": blue,
        "view_3d.vertex_active": text,
        "view_3d.edge_select": blue,
        "view_3d.face_select": stranding_color("blue", 0.5),
        "view_3d.edge_seam": stranding_color("red"),
        "view_3d.edge_sharp": stranding_color("steel"),
        "view_3d.edge_crease": stranding_color("steel"),
        "view_3d.edge_bevel": stranding_color("warn"),
        "view_3d.wire_edit": stranding_color("steel"),
        # 节点编辑器（背景冷炭黑，网格 / 连线低对比）
        "node_editor.grid": stranding_color("line"),
        "node_editor.wire": stranding_color("line"),
        "node_editor.node_backdrop": base,
        "node_editor.node_selected": blue,
        "node_editor.node_active": text,
        # 基面网格线（view_3d.grid）与法线（view_3d.normal）不在这里出现：
        # 它们由 preset_colors() 统一写回出厂默认值（见 viewport_helper_colors()）。
    })
    colors.update(global_highlight_colors(blue))
    # 标签独立型家族的标签文字：浅灰白（理由见 _label_family_override）
    colors.update(_label_family_override(stranding_color("blue_dim"), text))
    # 滑块 / 工具按钮 / 顶部工具项：面板色轨道 + 调色板里备好的蓝深档 + 浅色文字
    colors.update(deep_accent_family_colors(
        blue, base, text, inner_sel=stranding_color("blue_dim")))
    colors.update(_frame_current_colors(blue))
    return {path: color for path, color in colors.items() if color is not None}


#: 配色预设：照着失落星船 马拉松 的三张实拍图做的
def _marathon_preset_work():
    """适合长时间建模的低干扰版本：近黑面板 + 黄绿只用在选中态，边框压到几乎看不见。

    实拍图里"黄绿只出现在关键状态"的克制感就是这套的思路：
    大面积是炭黑与低对比灰，强调色只落在当前选中 / 活动的东西上。
    """
    base = TOKENS["BG_PANEL"]
    text = TOKENS["TEXT_PRIMARY"]
    colors = {}
    for prefix, _label in WIDGET_FAMILIES:
        colors[family_path(prefix, "inner")] = hex_color(base)
        colors[family_path(prefix, "text")] = hex_color(text)
        colors[family_path(prefix, "outline")] = hex_color(TOKENS["BORDER_SUBTLE"])
        colors[family_path(prefix, "inner_sel")] = hex_color(TOKENS["ACCENT_PRIMARY_CALM"])
        colors[family_path(prefix, "text_sel")] = hex_color(TOKENS["TEXT_ON_ACCENT"])
        colors[family_path(prefix, "outline_sel")] = hex_color(TOKENS["ACCENT_PRIMARY_CALM"])
        colors[family_path(prefix, "item")] = hex_color(TOKENS["ACCENT_PRIMARY_CALM"])
    colors.update({
        "user_interface.wcol_list_item.inner": hex_color(base, 0.0),
        "user_interface.wcol_scroll.inner": hex_color(base, 0.0),
        "user_interface.wcol_menu_back.inner": hex_color(TOKENS["BG_ELEVATED"]),
        "user_interface.wcol_menu_back.text": hex_color(text),
        "user_interface.wcol_pie_menu.inner": hex_color(TOKENS["BG_ELEVATED"]),
        "user_interface.wcol_pie_menu.text": hex_color(text),
        "user_interface.wcol_tab.inner": hex_color(TOKENS["BG_PRIMARY"]),
        "user_interface.wcol_tab.text": hex_color(TOKENS["TEXT_SECONDARY"]),
        "user_interface.wcol_tab.inner_sel": hex_color(TOKENS["BG_ELEVATED"]),
        "user_interface.wcol_tab.text_sel": hex_color(TOKENS["ACCENT_PRIMARY_CALM"]),
        "user_interface.wcol_text.inner": hex_color(TOKENS["BG_SECONDARY"]),
        "user_interface.wcol_text.text": hex_color(text),
        "user_interface.wcol_text.inner_sel": hex_color(TOKENS["ACCENT_SECONDARY_CALM"]),
        "user_interface.wcol_text.text_sel": hex_color(TOKENS["TEXT_ON_ACCENT"]),
        "user_interface.wcol_box.inner": hex_color(TOKENS["BG_SECONDARY"], 0.5),
        "user_interface.wcol_box.inner_sel": hex_color(TOKENS["ACCENT_PRIMARY_CALM"]),
        "user_interface.wcol_list_item.inner_sel": hex_color(TOKENS["ACCENT_PRIMARY_CALM"]),
        "user_interface.panel_active": hex_color(TOKENS["ACCENT_PRIMARY_CALM"]),
        # 面板层级：近黑 -> 面板 -> 浮起，三级区分
        "view_3d.back": hex_color(TOKENS["BG_PRIMARY"]),
        "outliner.back": hex_color(TOKENS["BG_PANEL"]),
        "properties.back": hex_color(TOKENS["BG_PANEL"]),
        "node_editor.back": hex_color(TOKENS["BG_PRIMARY"]),
        "outliner.header": hex_color(TOKENS["BG_ELEVATED"]),
        "properties.header": hex_color(TOKENS["BG_ELEVATED"]),
        "outliner.tab_back": hex_color(TOKENS["BG_PANEL"]),
        "outliner.tab_active": hex_color(TOKENS["BG_ELEVATED"]),
        "outliner.tab_inactive": hex_color(TOKENS["BG_PRIMARY"]),
        "properties.tab_back": hex_color(TOKENS["BG_PANEL"]),
        "properties.tab_active": hex_color(TOKENS["BG_ELEVATED"]),
        "properties.tab_inactive": hex_color(TOKENS["BG_PRIMARY"]),
        # 3D 编辑模式：顶点白、**选中几何用鲜亮档**（界面控件才用低饱和档）、
        # 缝合 / 锐边 / 折痕各自可辨（见 VIEWPORT_SELECTION_PATHS 的说明）
        "view_3d.editmesh_active": hex_color(TOKENS["ACCENT_PRIMARY"]),
        "view_3d.vertex_select": hex_color(TOKENS["ACCENT_PRIMARY"]),
        "view_3d.vertex_active": hex_color("#FFFFFF"),
        "view_3d.edge_select": hex_color(TOKENS["ACCENT_PRIMARY"]),
        "view_3d.face_select": hex_color(TOKENS["ACCENT_PRIMARY"], 0.5),
        "view_3d.edge_seam": hex_color(TOKENS["STATE_ERROR"]),
        "view_3d.edge_sharp": hex_color(TOKENS["ACCENT_SECONDARY_CALM"]),
        "view_3d.edge_crease": hex_color(TOKENS["ACCENT_TERTIARY_CALM"]),
        "view_3d.edge_bevel": hex_color(TOKENS["STATE_WARNING"]),
        "view_3d.wire_edit": hex_color(TOKENS["TEXT_SECONDARY"]),
        # 基面网格线（view_3d.grid）与法线（view_3d.normal）不在这里出现：
        # 它们属于「必须写回出厂默认值」的项，由 preset_colors() 统一补上
        # （见 viewport_helper_colors()）—— 旧版留在主题里的暗值靠这一步自愈。
        # 状态色语义
        "user_interface.wcol_state.inner_changed": hex_color(TOKENS["ACCENT_PRIMARY_CALM"]),
        "user_interface.wcol_state.inner_changed_sel": hex_color(TOKENS["ACCENT_PRIMARY_CALM"]),
        "user_interface.wcol_state.inner_anim": hex_color(TOKENS["ACCENT_SECONDARY_CALM"]),
        "user_interface.wcol_state.inner_anim_sel": hex_color(TOKENS["ACCENT_SECONDARY_CALM"]),
        "user_interface.wcol_state.inner_key": hex_color(TOKENS["TEXT_PRIMARY"]),
        "user_interface.wcol_state.inner_key_sel": hex_color(TOKENS["ACCENT_PRIMARY_CALM"]),
        "user_interface.wcol_state.inner_driven": hex_color(TOKENS["ACCENT_TERTIARY_CALM"]),
        "user_interface.wcol_state.inner_driven_sel": hex_color(TOKENS["ACCENT_TERTIARY_CALM"]),
        "user_interface.wcol_state.inner_overridden": hex_color(TOKENS["STATE_ERROR"]),
        "user_interface.wcol_state.inner_overridden_sel": hex_color(TOKENS["STATE_ERROR"]),
    })
    colors.update(global_highlight_colors(hex_color(TOKENS["ACCENT_PRIMARY_CALM"])))
    # 标签独立型家族的标签文字：三套预设统一用浅色（理由见 _label_family_override）
    colors.update(_label_family_override(
        scale_color(hex_color(TOKENS["ACCENT_PRIMARY_CALM"]), OPTION_BLOCK_FACTOR),
        hex_color(LABEL_TEXT_COLOR)))
    # 滑块 / 工具按钮 / 顶部工具项：深面板轨道 + 深档强调 + 浅色文字
    # （不再整族写回出厂默认值 —— 那正是"滑块变蓝、工具栏变默认"的成因）
    colors.update(deep_accent_family_colors(
        hex_color(TOKENS["ACCENT_PRIMARY_CALM"]), hex_color(MARATHON["panel"]),
        hex_color(TOKENS["TEXT_PRIMARY"])))
    colors.update(_frame_current_colors(hex_color(TOKENS["ACCENT_PRIMARY_CALM"])))
    return {path: color for path, color in colors.items() if color is not None}


#: 配色预设：两套「失落星船：马拉松」+ 一套「死亡搁浅」。
#: 顺序就是 N 面板上按钮的顺序（按钮由 ``preset_names()`` 自动生成，ui.py 不参与）。
PRESETS = (
    ("马拉松 建模 低干扰", _marathon_preset_work()),
    ("马拉松 浅灰卡", _marathon_card_preset()),
    ("死亡搁浅", _stranding_preset()),
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


#: 控件家族那批条目的分组名（「按钮 底色」这类共 7 个）。
#: 它们的标签本身已经写明家族与部位（「普通按钮 底色」），下拉里再拼一次分组名
#: 就是把同一个词说两遍 —— 见 :func:`item_label`。
WIDGET_GROUPS = tuple(group for _member, _label, group in WIDGET_MEMBERS)


def item_label(label, group):
    """颜色项下拉里那一行的显示文本。

    默认是 ``分组 标签``；**控件家族那一批只留标签**：它们的标签已经写明家族与部位
    （「普通按钮 底色」），再拼上「按钮 底色」纯属重复，而且让条目变得很长。

    为什么要短：弹出的条目框列宽有限，Blender 会把放不下的条目标签**按像素宽度
    从中间截断**（``interface_widgets.cc`` 的 ``text_clip_middle`` / ``text_clip_middle_ex``），
    中文字被切在半个字上时，落单的多字节会被当成无效序列画成乱码（那个函数里就有
    ``TODO: proper handling of non UTF8 strings`` 的注释）。标签短一点，需要截断的条目就少一点、
    截得更靠后一点 —— 这是**缓解**，不是根治（根治在他的 Blender / 字体侧）。
    """
    if not group or group in WIDGET_GROUPS:
        return label
    return "{} {}".format(group, label)


def target_items(self=None, context=None):
    """给 EnumProperty 用的动态条目：只列出当前 Blender 真的有的颜色。

    简洁模式（``preferences.simple_mode``）下只留下常用项，避免两百多项把人绕晕；
    关掉简洁模式就能看到全部条目。条目标签的拼法见 :func:`item_label`。
    """
    simple = bool(getattr(self, "simple_mode", False))
    theme = current_theme(context if context is not None else bpy.context)
    items = []
    for path, label, group in TARGETS:
        if simple and not is_simple_path(path):
            continue
        if resolve(theme, path) is None:
            continue
        items.append((path, item_label(label, group), path))
    if not items:
        items.append(("__unavailable__", "（当前版本没有可用颜色项）", ""))
    return items


def available_paths(context=None):
    """当前版本可用的颜色项路径列表。"""
    return [item[0] for item in target_items(None, context)]


def defaults_table(version=None):
    """按运行版本把三张默认值表合并起来（5.x 用 5X 表，4.x 用 4X 表）。"""
    v = tuple(version or bpy.app.version)
    table = dict(DEFAULT_COLORS)
    if v >= (5, 0, 0):
        table.update(DEFAULT_COLORS_5X)
    else:
        table.update(DEFAULT_COLORS_4X)
    return table


def default_of(path, version=None):
    """某一项的出厂默认值（没有记录就返回 None）。"""
    return defaults_table(version).get(path)


def has_default(path, version=None):
    return path in defaults_table(version)


def viewport_helper_paths():
    """**必须写回出厂默认值**的颜色项：视口里的基面网格线与法线。

    只有这两个 —— 三个"深档强调"控件家族（:data:`DEEP_ACCENT_FAMILIES`）曾经也
    走这条路，现在改为"深档强调色 + 浅色文字"（见那个常量的说明）。
    """
    return list(VIEWPORT_HELPERS)


def viewport_helper_colors(version=None):
    """``{路径: 出厂默认值}``：预设必须把这些项**写回**默认值。

    值全部取自真机 dump 的默认值表（``DEFAULT_COLORS*``，经 :func:`default_of` 按版本合并），
    绝不臆造；当前版本没有的项（``default_of`` 返回 None）自动跳过。

    为什么是"写回"而不是"不覆盖"：主题里可能已经被旧版预设写进了更暗 / 更花的值，
    "不覆盖"不会把它们改回来 —— 用户看到的就是"套了预设完全没更新"。写回之后
    再套一次预设就自愈。
    """
    result = {}
    for path in viewport_helper_paths():
        color = default_of(path, version)
        if color is not None:
            result[path] = color
    return result


def preset_names():
    return [name for name, _ in PRESETS]


def preset_colors(name):
    """预设 ``{路径: 颜色}``。

    每次都会把「必须写回出厂默认值」的视口辅助元素（:func:`viewport_helper_colors`）
    合并进来，所以三套预设、以及"按当前饱和度重新套用"这条路径都会自愈，
    也保证以后新增预设不会再漏掉它们。
    """
    for preset_name, colors in PRESETS:
        if preset_name == name:
            merged = dict(colors)
            merged.update(viewport_helper_colors())
            return merged
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


def restore_default(theme, path):
    """把某一项恢复为记录的默认值。"""
    color = default_of(path)
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


# ------------------------------------------------------------ 分组 / 状态


def color_close(current, expected, tolerance=1.5 / 255.0):
    """两个颜色是否够接近（主题是 8 位量化存储，要留约 1/255 的容差）。"""
    if current is None or expected is None:
        return False
    pairs = min(len(current), len(expected))
    for index in range(pairs):
        if abs(float(current[index]) - float(expected[index])) > tolerance:
            return False
    return True


def group_of(path):
    """某个颜色项属于哪个分组。"""
    for target_path, _label, group in TARGETS:
        if target_path == path:
            return group
    return ""


def group_names(paths=None):
    """当前版本下存在的分组（保持 TARGETS 里的顺序）。"""
    wanted = set(paths) if paths is not None else None
    names = []
    for path, _label, group in TARGETS:
        if wanted is not None and path not in wanted:
            continue
        if group not in names:
            names.append(group)
    return names


def paths_in_group(group, paths=None):
    """某个分组里当前版本存在的颜色项。"""
    wanted = set(paths) if paths is not None else None
    result = []
    for path, _label, item_group in TARGETS:
        if item_group != group:
            continue
        if wanted is not None and path not in wanted:
            continue
        result.append(path)
    return result


def default_paths(group=""):
    """有出厂默认值的项（可按分组过滤）—— 按当前版本合并后的表。"""
    table = defaults_table()
    if group:
        return [path for path in table if group_of(path) == group]
    return list(table)


def differing_paths(theme, paths):
    """列出与出厂默认值不同的项（用来告诉用户"其实没东西要恢复"）。"""
    result = []
    for path in paths:
        default = default_of(path)
        if default is None:
            continue
        current = get_color(theme, path)
        if current is None:
            continue
        if not color_close(current, default):
            result.append(path)
    return result


def capture_colors(theme, paths):
    """读一组颜色（保存自定义配色用），返回 ``{路径: [r,g,b,a]}``。"""
    result = {}
    for path in paths or ():
        color = get_color(theme, path)
        if color is not None:
            result[path] = [float(component) for component in color]
    return result


# --------------------------------------------------------- 自定义配色（JSON）


def load_custom(text):
    """把偏好设置里那段字符串解析成 ``{名字: {路径: [r,g,b,a]}}``。"""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for name, colors in data.items():
        if not isinstance(colors, dict):
            continue
        clean = {}
        for path, value in colors.items():
            if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
                continue
            try:
                clean[str(path)] = [float(component) for component in value]
            except (TypeError, ValueError):
                continue
        if clean:
            result[str(name)] = clean
    return result


def dump_custom(items):
    """序列化自定义配色（存进偏好设置）。"""
    return json.dumps(items, ensure_ascii=False, sort_keys=True)


def custom_names(text):
    return sorted(load_custom(text).keys())


def custom_colors(text, name):
    return load_custom(text).get(name)


def custom_save(text, name, colors):
    """保存一套自定义配色（同名覆盖），返回 ``(新文本, 是否保存成功)``。"""
    name = (name or "").strip()
    if not name or not colors:
        return text, False
    items = load_custom(text)
    items[name] = {str(path): [float(c) for c in color] for path, color in colors.items()}
    return dump_custom(items), True


def custom_delete(text, name):
    items = load_custom(text)
    if name not in items:
        return text, False
    del items[name]
    return dump_custom(items), True


# --------------------------------------------------------------- 字体设置
#
# 这些是**偏好设置**（不是主题颜色）：界面缩放、DPI、自定义字体文件、
# 抗锯齿 / hinting / 线条宽度。实测 4.0.2 与 5.2.2 属性名与默认值完全一致。

#: (键, 标签, 归属, 出厂默认, 说明)
FONT_ITEMS = (
    ("ui_scale", "界面缩放", "view", 1.0, "整个界面的缩放倍数（1.0 = 100%）"),
    ("dpi", "DPI / 字号基准", "system", 72, "界面字体大小的基准值（默认 72）"),
    ("font_path_ui", "界面字体文件", "view", "", "自定义界面字体（.ttf / .otf），留空用 Blender 自带"),
    ("font_path_ui_mono", "等宽字体文件", "view", "", "文本编辑器等处的等宽字体"),
    ("ui_line_width", "线条宽度", "view", "AUTO", "面板线条粗细：THIN / AUTO / THICK"),
    ("text_hinting", "文字微调", "view", "AUTO", "字体 hinting：AUTO / NONE / SLIGHT / FULL"),
    ("use_text_antialiasing", "文字抗锯齿", "view", True, "关掉会更锐利但更难读"),
    ("use_text_render_subpixelaa", "次像素抗锯齿", "view", False, "LCD 次像素渲染，只对部分显示器有效"),
)


def font_target(context, key):
    """找到某个字体设置所在的宿主对象，返回 ``(对象, 属性名)`` 或 None。"""
    preferences = getattr(context, "preferences", None)
    if preferences is None:
        return None
    for item_key, _label, owner_name, _default, _tip in FONT_ITEMS:
        if item_key != key:
            continue
        owner = getattr(preferences, owner_name, None)
        if owner is None or not hasattr(owner, key):
            return None
        return owner, key
    return None


def font_items(context):
    """当前 Blender 真的存在的字体设置项。"""
    return tuple(item for item in FONT_ITEMS if font_target(context, item[0]) is not None)


def font_get(context, key):
    target = font_target(context, key)
    if target is None:
        return None
    return getattr(target[0], target[1], None)


def font_set(context, key, value):
    target = font_target(context, key)
    if target is None:
        return False
    try:
        setattr(target[0], target[1], value)
    except Exception:
        return False
    return True


def font_defaults():
    return {key: default for key, _label, _owner, default, _tip in FONT_ITEMS}


def font_restore(context):
    """把字体 / 缩放设置恢复出厂值，返回恢复数量。"""
    restored = 0
    for key, value in font_defaults().items():
        if font_set(context, key, value):
            restored += 1
    return restored
