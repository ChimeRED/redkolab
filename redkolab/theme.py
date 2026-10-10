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
* 预设是一套「失落星船：马拉松」风格的**酸性配色**（``ACID`` 里是 hex 色值，
  方便微调），只改按钮与状态色，不动视口。
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
    "cyan_calm": TOKENS["ACCENT_SECONDARY_CALM"],
    "magenta_calm": TOKENS["ACCENT_TERTIARY_CALM"],
}

#: 旧名字保留（早期版本叫 ACID）
ACID = MARATHON


def marathon_color(key, alpha=1.0):
    """取调色板里的颜色（hex 转 0-1 浮点）。"""
    return hex_color(MARATHON.get(key, "#FFFFFF"), alpha)



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


def _marathon_preset(style):
    """按游戏实拍图生成三套配色。

    * ``card``  —— 浅灰任务卡：浅灰底 + 黑字 + 酸性黄绿选中
    * ``hud``   —— 暗色 HUD：近黑底 + 浅灰字 + 青蓝选中
    * ``alert`` —— 洋红警戒：深面板 + 浅灰字 + 洋红选中
    """
    if style == "card":
        base, text, edge = MARATHON["card"], MARATHON["ink"], MARATHON["card_edge"]
        accent, accent_text = MARATHON["lime_calm"], MARATHON["ink"]
    elif style == "alert":
        base, text, edge = MARATHON["panel"], MARATHON["text"], MARATHON["magenta_dim"]
        accent, accent_text = MARATHON["magenta_calm"], MARATHON["ink"]
    else:
        base, text, edge = MARATHON["ink"], MARATHON["text"], MARATHON["cyan_dim"]
        accent, accent_text = MARATHON["cyan_calm"], MARATHON["ink"]

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
    colors.update(_marathon_frame_colors(hex_color(accent)))
    return {path: color for path, color in colors.items() if color is not None}


def _marathon_frame_colors(accent):
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
        # 3D 编辑模式：顶点白、选中黄绿、缝合/锐边/折痕各自可辨
        "view_3d.editmesh_active": hex_color(TOKENS["ACCENT_PRIMARY_CALM"]),
        "view_3d.vertex_select": hex_color(TOKENS["ACCENT_PRIMARY_CALM"]),
        "view_3d.vertex_active": hex_color("#FFFFFF"),
        "view_3d.edge_select": hex_color(TOKENS["ACCENT_PRIMARY_CALM"]),
        "view_3d.face_select": hex_color(TOKENS["ACCENT_PRIMARY_CALM"], 0.5),
        "view_3d.edge_seam": hex_color(TOKENS["STATE_ERROR"]),
        "view_3d.edge_sharp": hex_color(TOKENS["ACCENT_SECONDARY_CALM"]),
        "view_3d.edge_crease": hex_color(TOKENS["ACCENT_TERTIARY_CALM"]),
        "view_3d.edge_bevel": hex_color(TOKENS["STATE_WARNING"]),
        "view_3d.wire_edit": hex_color(TOKENS["TEXT_SECONDARY"]),
        "view_3d.grid": hex_color(TOKENS["BORDER_SUBTLE"], 0.5),
        "view_3d.normal": hex_color(TOKENS["ACCENT_SECONDARY_CALM"]),
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
    colors.update(_marathon_frame_colors(hex_color(TOKENS["ACCENT_PRIMARY_CALM"])))
    return {path: color for path, color in colors.items() if color is not None}


PRESETS = (
    ("马拉松 建模 低干扰", _marathon_preset_work()),
    ("马拉松 浅灰卡", _marathon_preset("card")),
    ("马拉松 暗色 HUD", _marathon_preset("hud")),
    ("马拉松 洋红警戒", _marathon_preset("alert")),
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
    """给 EnumProperty 用的动态条目：只列出当前 Blender 真的有的颜色。

    简洁模式（``preferences.simple_mode``）下只留下常用项，避免两百多项把人绕晕；
    关掉简洁模式就能看到全部条目。
    """
    simple = bool(getattr(self, "simple_mode", False))
    theme = current_theme(context if context is not None else bpy.context)
    items = []
    for path, label, group in TARGETS:
        if simple and not is_simple_path(path):
            continue
        if resolve(theme, path) is None:
            continue
        items.append((path, "{} {}".format(group, label), path))
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
