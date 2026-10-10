# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab (REDKOlab) · Blender 插件（4.0 ~ 最新版通用）

功能：
    1. 一键删除选中物体的所有材质
    2. 一键应用选中物体的所有修改器
    3. 一键删除选中物体的所有修改器
    4. 在 N 面板里修改 Blender 界面主题颜色（不进饼菜单）

呼出方式：
    * 饼菜单（默认快捷键 ` 键，可在偏好设置里改）—— 只放上面 1~3
    * 3D 视图 ▸ 侧栏（N）▸ 红红子lab —— 1~3 + 主题颜色子面板

结构：
    compat.py   跨版本兼容层（上下文覆盖、偏好设置、键位工具）
    theme.py    主题颜色（目标列表、读写、预设、版本容错）
    ops.py      功能算符
    ui.py       饼菜单、N 面板、偏好设置
"""

bl_info = {
    "name": "红红子lab (REDKOlab)",
    "author": "Red04",
    "version": (1, 2, 0),
    "blender": (4, 0, 0),
    "location": "3D 视图 ▸ 侧栏(N) ▸ 红红子lab；饼菜单默认快捷键 `",
    "description": "一键删除物体所有材质、应用/删除物体所有修改器，并在 N 面板里改界面主题颜色（REDKOlab）",
    "warning": "",
    "doc_url": "",
    "category": "Object",
}

import bpy

from . import compat, ops, theme, ui

#: 注册顺序：偏好设置 → 算符 → 界面（子面板必须排在父面板之后）
_classes = (
    ui.HHZ_AddonPreferences,
    ops.HHZ_OT_remove_all_materials,
    ops.HHZ_OT_apply_all_modifiers,
    ops.HHZ_OT_remove_all_modifiers,
    ops.HHZ_OT_open_preferences,
    ops.HHZ_OT_capture_hotkey,
    ops.HHZ_OT_reset_hotkey,
    ops.HHZ_OT_theme_read,
    ops.HHZ_OT_theme_apply_preset,
    ops.HHZ_OT_theme_restore_default,
    ops.HHZ_OT_theme_save_preferences,
    ui.HHZ_MT_pie,
    ui.HHZ_PT_main,
    ui.HHZ_PT_theme,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    ui.register_keymap()


def unregister():
    ui.unregister_keymap()
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()
