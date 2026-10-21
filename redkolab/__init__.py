# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab (REDKO_LAB) Blender 插件（4.0 ~ 最新版通用）

功能：
    1. 一键删除选中物体的所有材质
    2. 一键应用选中物体的所有修改器
    3. 一键删除选中物体的所有修改器
    4. 一键楼梯：体块整成 0.3 宽 × 0.2 高（长度不变）+ 相对偏移阵列，
       鼠标移动换方向、滚轮改节数、左键确认
    5. 一键翻面：把选中网格的所有面整体翻转（多用户网格先复制成单用户）
    6. 检测是否有反面：只读检查，闭合网格用有符号体积判定、非闭合只报「疑似」
    7. 一键查看面朝向：切换当前屏幕里所有 3D 视口的面朝向叠加层（朝外蓝 / 朝内红）
    8. 一键选中当前相机：选中当前视角正在使用的那台相机（相机视角下取该视口的相机，
       否则取场景活动相机；只改选择，不改数据）
    9. 主题颜色：**按钮底色 / 文字与图标 / 边框 / 选中态** 分开设置，
       外加时间线当前帧色、图标颜色、3 套配色预设
       （马拉松 建模 低干扰 / 马拉松 浅灰卡 / 死亡搁浅），以及**自定义配色**存取
    10. 字体设置：界面缩放、DPI、自定义界面字体 / 等宽字体、抗锯齿、hinting、线条宽度
    11. 页面布局：把工作区与区域划分存成快照，在新文件里一键还原（也可存成启动布局）
    12. 拖动调整本插件面板在 N 面板里的先后顺序
    13. 打开 Blender 自动切英文输入法（仅 Windows，尽力而为；失败只打印一行说明）

分页（样式参考插件库里 ZML_LAB 的做法）：
    N 面板里最上面一排「建模 | 工具 | 主题」三个按钮，点了只显示该页的面板：
        建模 —— 上面 1~8 八个一键功能
        工具 —— 饼菜单与快捷键 / 插件设置 / 页面布局 / 面板顺序
        主题 —— 主题颜色 + 字体设置（控件配色、自定义配色、配色预设）

呼出方式：
    * 饼菜单（默认快捷键 Ctrl+E，可在偏好设置里改）—— 上面 1~5、7、8
    * 3D 视图 ▸ 侧栏（N）▸ 红红子lab

结构：
    compat.py   跨版本兼容层（上下文覆盖、偏好设置、键位工具）
    pages.py    分页（WindowManager 上的当前页 + 顶部标签条面板）
    stairs.py   一键楼梯（尺寸、方向系数、相对偏移阵列）
    theme.py    主题颜色（控件颜色目标列表、分组、自定义配色、字体、配色预设）
    layout.py   页面布局（抓取 / 还原工作区与区域划分）
    panels.py   N 面板顺序（面板顺序读写、按注册顺序重排自己的面板）
    ops.py      功能算符
    ui.py       饼菜单、N 面板、偏好设置
"""

bl_info = {
    "name": "红红子lab (REDKO_LAB)",
    "author": "Red04",
    "version": (2, 21, 0),
    "blender": (4, 0, 0),
    "location": "3D 视图 ▸ 侧栏(N) ▸ 红红子lab（建模 / 工具 / 主题 三页）；饼菜单默认快捷键 Ctrl+E",
    "description": "一键删材质、应用/删除修改器、一键楼梯、一键翻面、反面检测、查看面朝向；主题控件配色与字体、页面布局、面板顺序（REDKO_LAB）",
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
    ops.HHZ_OT_flip_normals,
    ops.HHZ_OT_detect_flipped,
    ops.HHZ_OT_toggle_face_orientation,
    ops.HHZ_OT_select_active_camera,
    ops.HHZ_OT_grayscale_filter,
    ops.HHZ_OT_stairs,
    ops.HHZ_OT_open_preferences,
    ops.HHZ_OT_capture_hotkey,
    ops.HHZ_OT_reset_hotkey,
    ops.HHZ_OT_fix_missing_files,
    ops.HHZ_OT_theme_read,
    ops.HHZ_OT_theme_apply_preset,
    ops.HHZ_OT_theme_restore_default,
    ops.HHZ_OT_theme_apply_color,
    ops.HHZ_OT_theme_reapply_preset,
    ops.HHZ_OT_theme_apply_global,
    ops.HHZ_OT_theme_part_load,
    ops.HHZ_OT_theme_auto_contrast,
    ops.HHZ_OT_theme_custom_save,
    ops.HHZ_OT_theme_custom_apply,
    ops.HHZ_OT_theme_custom_delete,
    ops.HHZ_OT_font_pick,
    ops.HHZ_OT_font_restore,
    ops.HHZ_OT_layout_save,
    ops.HHZ_OT_layout_apply,
    ops.HHZ_OT_layout_delete,
    ops.HHZ_OT_layout_save_startup,
    ops.HHZ_OT_theme_save_preferences,
    ops.HHZ_OT_section_drag,
    ops.HHZ_OT_zml_lab_update,
    ui.HHZ_MT_pie,
    pages.HHZ_PT_pages,
    ui.HHZ_PT_main,
    ui.HHZ_PT_theme,
    ui.HHZ_PT_hotkey,
    ui.HHZ_PT_settings,
    ui.HHZ_PT_order,
    ui.HHZ_PT_layout,
)


def register():
    pages.register_properties()
    for cls in _classes:
        bpy.utils.register_class(cls)
    # bl_order 是运行时写进面板类的，重启后会丢，所以每次都按偏好设置重新应用
    ui.apply_panel_order()
    # 面板上的「分开设置」要显示当前主题的真实颜色，注册时同步一次
    preferences = compat.get_preferences()
    if preferences is not None:
        ui.load_family_parts(bpy.context, preferences)
    ui.register_keymap()
    # 打开 Blender 自动切英文输入法（仅 Windows；失败只打印一行说明，绝不抛异常）
    ops.register_english_ime()


def unregister():
    # 与注册顺序相反：先撤掉自动英文输入法的 handler 与一次性定时器
    ops.unregister_english_ime()
    ui.unregister_keymap()
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    pages.unregister_properties()


if __name__ == "__main__":
    register()
