# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab · 分页（建模 / 工具 / 主题）

样式参考插件库里 ZML_LAB（椎木兰LAB）的分页做法，一共三件事：

1. 在 ``WindowManager`` 上挂一个 ``EnumProperty`` 当"当前页"（每个窗口各记一份）；
2. 一个 ``bl_options={'HIDE_HEADER'}``、``bl_order = 0`` 的面板只画一行
   ``row.prop(..., expand=True)`` —— 那就是侧栏最上面那排「建模 | 工具 | 主题」按钮；
3. 每个功能面板在自己的 ``poll()`` 里判断当前页，只有本页的面板才会显示。

这样整套功能仍然只占侧栏的**一个**标签页，里面再分三页，标签页栏不会变乱。
"""

import bpy

from . import compat

PAGE_MODELING = "MODELING"
PAGE_TOOLS = "TOOLS"
PAGE_THEME = "THEME"

#: (标识, 名字, 说明, 图标, 顺序)
PAGE_ITEMS = (
    (PAGE_MODELING, "建模", "一键删材质 / 应用与删除修改器", "MODIFIER", 0),
    (PAGE_TOOLS, "工具", "饼菜单、快捷键与面板设置", "TOOL_SETTINGS", 1),
    (PAGE_THEME, "主题", "界面控件配色与酸性预设", "COLOR", 2),
)

PAGE_LABELS = {page: label for page, label, _tip, _icon, _order in PAGE_ITEMS}

#: WindowManager 上的属性名
PROP_NAME = "hhz_page"


def register_properties():
    """把"当前页"挂到 WindowManager 上。"""
    setattr(
        bpy.types.WindowManager,
        PROP_NAME,
        bpy.props.EnumProperty(
            name="页",
            description="红红子lab 的功能分页",
            items=PAGE_ITEMS,
            default=PAGE_MODELING,
        ),
    )


def unregister_properties():
    if hasattr(bpy.types.WindowManager, PROP_NAME):
        try:
            delattr(bpy.types.WindowManager, PROP_NAME)
        except Exception:
            pass


def current(context):
    """当前页标识（取不到就回落到建模页）。"""
    manager = getattr(context, "window_manager", None)
    return getattr(manager, PROP_NAME, PAGE_MODELING)


def is_active(context, page):
    """某个功能面板这一帧该不该显示。"""
    return current(context) == page


def label(context):
    """当前页的中文名（给面板标题用）。"""
    current_page = current(context)
    return PAGE_LABELS.get(current_page, current_page)


class HHZ_PT_pages(bpy.types.Panel):
    """最上面的分页标签条：建模 | 工具 | 主题（无标题栏，就一行按钮）。"""

    bl_label = "分页"
    bl_idname = "HHZ_PT_pages"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME
    bl_options = {'HIDE_HEADER'}
    bl_order = 0

    def draw(self, context):
        row = self.layout.row(align=True)
        row.scale_y = 1.35
        row.prop(context.window_manager, PROP_NAME, expand=True)


CLASSES = (HHZ_PT_pages,)
