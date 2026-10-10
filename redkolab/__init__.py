# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab (REDKO_LAB) · Blender 插件（4.0 ~ 最新版通用）

功能：
    1. 一键删除选中物体的所有材质
    2. 一键应用选中物体的所有修改器
    3. 一键删除选中物体的所有修改器
    4. 一键楼梯：体块整成 0.3 宽 × 0.2 高（长度不变）+ 相对偏移阵列，
       鼠标移动换方向、滚轮改节数、左键确认
    5. 在 N 面板里修改界面**控件**颜色（灰按钮 / 选中蓝 / 状态色），带 Marathon 酸性预设
    6. 拖动调整本插件面板在 N 面板里的先后顺序

分页（样式参考插件库里 ZML_LAB 的做法）：
    N 面板里最上面一排「建模 | 工具 | 主题」三个按钮，点了只显示该页的面板：
        建模 —— 上面 1~4 四个一键功能
        工具 —— 饼菜单与快捷键 / 插件设置 / 面板顺序
        主题 —— 主题颜色（控件配色 + 酸性预设）

呼出方式：
    * 饼菜单（默认快捷键 Ctrl+E，可在偏好设置里改）—— 上面 1~4
    * 3D 视图 ▸ 侧栏（N）▸ 红红子lab

结构：
    compat.py   跨版本兼容层（上下文覆盖、偏好设置、键位工具）
    pages.py    分页（WindowManager 上的当前页 + 顶部标签条面板）
    stairs.py   一键楼梯（尺寸、方向系数、相对偏移阵列）
    theme.py    主题颜色（控件颜色目标列表、读写、Marathon 酸性预设）
    panels.py   N 面板顺序（面板顺序读写、按注册顺序重排自己的面板）
    ops.py      功能算符
    ui.py       饼菜单、N 面板、偏好设置
"""

bl_info = {
    "name": "红红子lab (REDKO_LAB)",
    "author": "Red04",
    "version": (1, 9, 0),
    "blender": (4, 0, 0),
    "location": "3D 视图 ▸ 侧栏(N) ▸ 红红子lab（建模 / 工具 / 主题 三页）；饼菜单默认快捷键 Ctrl+E",
    "description": "一键删材质、应用/删除修改器、一键楼梯；N 面板改界面控件配色、拖动调整面板顺序（REDKO_LAB）",
    "warning": "",
    "doc_url": "",
    "category": "Object",
}

import bpy

from . import compat, ops, pages, panels, stairs, theme, ui

#: 注册顺序：偏好设置 → 算符 → 界面（分页标签条排在最前）
_classes = (
    ui.HHZ_AddonPreferences,
    ops.HHZ_OT_remove_all_materials,
    ops.HHZ_OT_apply_all_modifiers,
    ops.HHZ_OT_remove_all_modifiers,
    ops.HHZ_OT_stairs,
    ops.HHZ_OT_open_preferences,
    ops.HHZ_OT_capture_hotkey,
    ops.HHZ_OT_reset_hotkey,
    ops.HHZ_OT_theme_read,
    ops.HHZ_OT_theme_apply_preset,
    ops.HHZ_OT_theme_restore_default,
    ops.HHZ_OT_theme_save_preferences,
    ops.HHZ_OT_section_drag,
    ui.HHZ_MT_pie,
    pages.HHZ_PT_pages,
    ui.HHZ_PT_main,
    ui.HHZ_PT_theme,
    ui.HHZ_PT_hotkey,
    ui.HHZ_PT_settings,
    ui.HHZ_PT_order,
)


def register():
    pages.register_properties()
    for cls in _classes:
        bpy.utils.register_class(cls)
    # bl_order 是运行时写进面板类的，重启后会丢，所以每次都按偏好设置重新应用
    ui.apply_panel_order()
    ui.register_keymap()


def unregister():
    ui.unregister_keymap()
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    pages.unregister_properties()


if __name__ == "__main__":
    register()
