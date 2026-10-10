# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab 界面层

包含三部分：

* 饼式菜单（默认快捷键 Ctrl+E，可在插件偏好设置里改）
* 3D 视图 N 面板（侧栏里的「红红子lab」标签页），下面挂着「主题颜色」子面板
* 插件偏好设置（快捷键捕获、手动改键、启用开关、主题颜色项）

键位注册走 ``window_manager.keyconfigs.addon``，这是插件专用键位表
（4.0 ~ 5.x 都支持），卸载插件时会被自动清掉。
"""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, StringProperty

from . import compat, layout, pages, panels, theme

#: 饼菜单的 bl_idname（必须以 _MT_ 命名，Menu 才能注册成功）
PIE_IDNAME = "OBJECT_MT_hhz_pie"

#: 键位挂在 3D 视图上
KEYMAP_NAME = "3D View"
KEYMAP_SPACE = 'VIEW_3D'

#: 已注册的 (keymap, keymap_item) 对，卸载/改键时用来清理
_keymap_entries = []

#: 批量改偏好设置时挂起重建，避免改 4 个开关就重建 4 次键位
_refresh_suspended = False


# ----------------------------------------------------------------- 键位管理


def suspend_refresh(value=True):
    global _refresh_suspended
    _refresh_suspended = bool(value)


def _hotkey_settings(context=None):
    """返回 (key_type, ctrl, shift, alt, oskey, 是否启用)。"""
    preferences = compat.get_preferences(context)
    if preferences is None:
        return (compat.DEFAULT_KEY_TYPE, compat.DEFAULT_KEY_CTRL, compat.DEFAULT_KEY_SHIFT,
                compat.DEFAULT_KEY_ALT, compat.DEFAULT_KEY_OSKEY, True)
    return (
        preferences.key_type or compat.DEFAULT_KEY_TYPE,
        bool(preferences.ctrl),
        bool(preferences.shift),
        bool(preferences.alt),
        bool(preferences.oskey),
        bool(preferences.enable_pie),
    )


def unregister_keymap():
    """移除本插件注册的所有键位。"""
    for keymap, item in _keymap_entries:
        try:
            keymap.keymap_items.remove(item)
        except Exception:
            pass
    _keymap_entries[:] = []

    # 顺手清掉我们自己建的、已经空掉的 3D View 键位表
    keyconfig = compat.addon_keyconfig()
    if keyconfig is None:
        return
    try:
        keymap = keyconfig.keymaps.get(KEYMAP_NAME)
    except Exception:
        keymap = None
    if keymap is not None and not len(keymap.keymap_items):
        try:
            keyconfig.keymaps.remove(keymap)
        except Exception:
            pass


def register_keymap():
    """按当前偏好设置注册饼菜单快捷键，成功返回 True。

    ``--background`` 模式没有 keyconfig，返回 False 而不是报错。
    """
    unregister_keymap()

    keyconfig = compat.addon_keyconfig()
    if keyconfig is None:
        return False

    key_type, ctrl, shift, alt, oskey, enabled = _hotkey_settings()
    if not enabled:
        return True

    try:
        keymap = keyconfig.keymaps.new(name=KEYMAP_NAME, space_type=KEYMAP_SPACE)
        item = keymap.keymap_items.new(
            "wm.call_menu_pie", key_type, 'PRESS',
            ctrl=ctrl, shift=shift, alt=alt, oskey=oskey,
        )
    except Exception as error:
        print("[{}] 饼菜单快捷键注册失败：{}".format(compat.ADDON_NAME, error))
        return False

    try:
        # wm.call_menu_pie 的 name 属性就是菜单的 bl_idname
        item.properties.name = PIE_IDNAME
    except Exception as error:
        print("[{}] 饼菜单绑定失败：{}".format(compat.ADDON_NAME, error))

    _keymap_entries.append((keymap, item))
    return True


def refresh_keymap():
    """重建键位（偏好设置变化时调用）。"""
    if _refresh_suspended:
        return True
    return register_keymap()


def apply_hotkey(context, key_type, ctrl=False, shift=False, alt=False, oskey=False):
    """写入偏好设置并重建键位，返回键位是否注册成功。"""
    preferences = compat.get_preferences(context)
    if preferences is None:
        return False

    suspend_refresh(True)
    try:
        preferences.key_type = key_type or compat.DEFAULT_KEY_TYPE
        preferences.ctrl = bool(ctrl)
        preferences.shift = bool(shift)
        preferences.alt = bool(alt)
        preferences.oskey = bool(oskey)
    finally:
        suspend_refresh(False)
    return refresh_keymap()


def _on_theme_target_update(self, context):
    """切换颜色项时，把该项当前的色值读进选择器。

    所属分组不再缓存进偏好设置：需要时用 ``theme.group_of(theme_target)`` 推导。
    """
    theme.read_into_preferences(context, self)


def custom_items(self, context):
    """自定义配色的下拉项（动态，跟着已保存的名字走）。"""
    names = theme.custom_names(getattr(self, "theme_custom", ""))
    if not names:
        return [("__none__", "（还没有）", "还没有保存过自定义配色")]
    return [(name, name, "套用或删除「{}」".format(name)) for name in names]


def family_items(self, context):
    """控件家族下拉项。

    简洁模式下只有 6 个分组（一个选项 = 一批家族一起改），
    关掉简洁模式才列出全部 20 个家族 —— 选项太多反而没法用。
    """
    if getattr(self, "simple_mode", True):
        available = {prefix for prefix, _label in theme.family_items(context)}
        items = [(key, label) for key, label, _tip in theme.simple_family_items()]
        keep = []
        for key, label in items:
            members = theme.families_for_key(key)
            if members and any(member in available for member in members):
                keep.append((key, label, "一起改「{}」这一批控件".format(label)))
        if keep:
            return keep
    items = theme.family_items(context)
    if not items:
        return [("__none__", "（没有可用控件）", "这个 Blender 版本读不到控件颜色")]
    return [(prefix, label, "分开设置「{}」的底色 / 图标与文字 / 边框".format(label))
            for prefix, label in items]


def family_keys(preferences):
    """当前选择的家族键 -> 实际要处理的家族列表。"""
    return theme.families_for_key(getattr(preferences, "family", ""))


def load_family_parts(context, preferences):
    """把当前选择（可能是分组）的代表家族颜色读进面板属性，返回读到的项数。"""
    current = theme.current_theme(context)
    members = family_keys(preferences)
    if current is None or not members:
        return 0
    colors = theme.read_family(current, members[0])
    filled = 0
    for member, color in colors.items():
        name = "part_" + member
        if not hasattr(preferences, name):
            continue
        value = tuple(float(component) for component in color)
        if len(value) == 3:
            value = value + (1.0,)
        try:
            setattr(preferences, name, value[:4])
            filled += 1
        except Exception:
            continue
    return filled


def _on_family_update(self, context):
    """换控件家族 -> 把该家族当前的颜色读进面板。"""
    load_family_parts(context, self)


def _on_part_update(self, context):
    """面板上改了某个成员的颜色 -> 立刻写进主题（选了分组就是一批家族一起改）。"""
    current = theme.current_theme(context)
    members = family_keys(self)
    if current is None or not members:
        return
    for member, _label, _group in theme.PART_MEMBERS:
        value = getattr(self, "part_" + member, None)
        if value is None:
            continue
        for prefix in members:
            path = theme.family_path(prefix, member)
            existing = theme.get_color(current, path)
            if existing is None or theme.color_close(existing, tuple(value)):
                continue
            theme.set_color(current, path, tuple(value))
    theme.read_into_preferences(context, self)


def _on_saturation_update(self, context):
    """饱和度一改就用它重算最近一次的预设（永远从预设表出发，不累积）。"""
    current = theme.current_theme(context)
    name = getattr(self, "last_preset", "")
    if current is None or not name:
        return
    colors = theme.preset_colors(name)
    if not colors:
        return
    factor = float(getattr(self, "preset_saturation", 1.0))
    if factor < 1.0:
        colors = theme.desaturate_colors(colors, factor)
    theme.apply_colors(current, colors)
    theme.read_into_preferences(context, self)


def _on_hex_update(self, context):
    """十六进制框一改就同步到调色盘，这样「应用颜色」只看调色盘就够了。"""
    color = theme.hex_color(getattr(self, "custom_hex", ""))
    if color is not None:
        self.palette_color = color


def _on_simple_mode_update(self, context):
    """切换简洁模式：颜色项与家族选项都变了，重新读一遍当前家族的颜色。

    必须返回 None。以前这里写成返回元组的 lambda，Blender 4.x 每次拖这个开关
    都会往控制台打一条 "ValueError: the return value must be None"。
    """
    load_family_parts(context, self)
    theme.read_into_preferences(context, self)


def layout_items(self, context):
    """已保存布局的下拉项。"""
    names = layout.names(getattr(self, "layout_slots", ""))
    if not names:
        return [("__none__", "（还没有）", "还没有保存过布局")]
    return [(name, name, "使用或删除「{}」".format(name)) for name in names]


def _on_theme_color_update(self, context):
    """选择器一变就写进主题 —— 实时预览的那一步。"""
    theme.write_from_preferences(context, self)


def _on_panel_order_update(self, context):
    """面板顺序变了就重新落一遍（bl_order + 重新注册本插件的面板）。"""
    apply_panel_order(context)


def apply_panel_order(context=None):
    """把偏好设置里的面板顺序真正落到 N 面板上（注册时与拖动排序时都会调用）。

    做两件事：

    1. 写 ``bl_order`` —— 少数自己排序的编辑器（图像 / 序列 / 节点 / 资产）认它；
    2. 按目标顺序**重新注册本插件自己的面板** —— 3D 视图侧栏只看注册顺序
       （实测 Blender 源码：``interface_panel.cc`` 从不读面板类型的 order），
       这才是拖动排序真正生效的一层。

    只重排本插件自己的 5 个面板。其它插件的面板**绝不能碰**：实测把内置/他人面板
    注销再注册会让 Blender 4.0 / 4.2 直接崩溃（段错误），所以标签页顺序不做。
    """
    preferences = compat.get_preferences(context)
    section_order = panels.read_section_order(preferences)

    panels.apply_section_order(section_order, {"HHZ_PT_order": panels.ORDER_PANEL_ORDER})
    panels.reregister_our_panels(section_order)
    return section_order


def _on_preferences_update(self, context):
    refresh_keymap()


# ------------------------------------------------------------------- 饼菜单


class HHZ_MT_pie(bpy.types.Menu):
    bl_idname = PIE_IDNAME
    bl_label = compat.ADDON_NAME

    def draw(self, context):
        layout = self.layout
        pie = layout.menu_pie()

        # 四个方向：左 / 右 / 下 / 上 —— 插件设置已按需求挪到 N 面板，不再占饼菜单
        pie.operator("hhz.remove_all_materials", icon='MATERIAL')
        pie.operator("hhz.apply_all_modifiers", icon='MODIFIER')
        pie.operator("hhz.remove_all_modifiers", icon='TRASH')
        pie.operator("hhz.stairs", icon='MOD_ARRAY')


# ------------------------------------------------------------------- N 面板


class HHZ_PT_main(bpy.types.Panel):
    bl_idname = "HHZ_PT_main"
    bl_label = compat.ADDON_NAME
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME

    @classmethod
    def poll(cls, context):
        return pages.is_active(context, pages.PAGE_MODELING)

    def draw(self, context):
        layout = self.layout

        column = layout.column(align=True)
        column.operator("hhz.remove_all_materials", icon='MATERIAL')
        column.operator("hhz.apply_all_modifiers", icon='MODIFIER')
        column.operator("hhz.remove_all_modifiers", icon='TRASH')

        column.separator()
        column.operator("hhz.stairs", icon='MOD_ARRAY')
        layout.label(text="一键楼梯：鼠标换方向 滚轮改节数", icon='INFO')

        layout.separator()
        if context.mode != 'OBJECT':
            layout.label(text="请先切回物体模式", icon='ERROR')
            return

        targets = compat.get_target_objects(context)
        if not targets:
            layout.label(text="没有选中物体", icon='INFO')
            return

        slot_count = 0
        modifier_count = 0
        for obj in targets:
            slot_count += len(getattr(obj, "material_slots", ()) or ())
            modifier_count += len(getattr(obj, "modifiers", ()) or ())
        layout.label(
            text="选中 {} 个物体 材质槽 {} 修改器 {}".format(
                len(targets), slot_count, modifier_count),
            icon='INFO',
        )


class HHZ_PT_hotkey(bpy.types.Panel):
    """饼菜单与快捷键。

    顶层面板（不用 bl_parent_id）：侧栏 N 面板不保证绘制子面板，
    做成子面板会出现"注册成功但在界面上找不到"的情况。
    """

    bl_idname = "HHZ_PT_hotkey"
    bl_label = "饼菜单与快捷键"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME

    @classmethod
    def poll(cls, context):
        return pages.is_active(context, pages.PAGE_TOOLS)

    def draw(self, context):
        layout = self.layout

        key_type, ctrl, shift, alt, oskey, enabled = _hotkey_settings(context)
        row = layout.row(align=True)
        row.label(text="饼菜单：")
        if enabled:
            row.label(text=compat.key_label(key_type, ctrl, shift, alt, oskey))
        else:
            row.label(text="已禁用")
        row.operator("hhz.capture_hotkey", text="", icon='FILE_REFRESH')

        call = layout.operator("wm.call_menu_pie", text="呼出饼菜单", icon='COLLAPSEMENU')
        call.name = PIE_IDNAME

        layout.label(text="饼菜单里就这四个功能", icon='INFO')


class HHZ_PT_settings(bpy.types.Panel):
    """插件设置。

    顶层面板；这个入口原来放在饼菜单里，按需求挪到 N 面板。
    """

    bl_idname = "HHZ_PT_settings"
    bl_label = "插件设置"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME

    @classmethod
    def poll(cls, context):
        return pages.is_active(context, pages.PAGE_TOOLS)

    def draw(self, context):
        layout = self.layout

        layout.operator("hhz.open_preferences", icon='SETTINGS')

        column = layout.column(align=True)
        column.label(text="{} {}".format(compat.ADDON_NAME, compat.ADDON_NAME_EN))
        column.label(text="支持 Blender {}+ 当前 {}".format(
            ".".join(str(part) for part in compat.MIN_BLENDER_VERSION),
            compat.VERSION_STRING,
        ))


class HHZ_PT_order(bpy.types.Panel):
    """面板顺序：拖动调整本插件各面板与侧栏标签页的顺序（固定排在最后）。"""

    bl_idname = "HHZ_PT_order"
    bl_label = "面板顺序"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME

    @classmethod
    def poll(cls, context):
        return pages.is_active(context, pages.PAGE_TOOLS)

    def draw(self, context):
        layout = self.layout

        preferences = compat.get_preferences(context)
        if preferences is None:
            layout.label(text="读不到插件偏好设置", icon='ERROR')
            return

        # ---- 本插件的分块 ----
        box = layout.box()
        box.label(text="本插件的面板")
        section_state = panels.DRAG_STATE.get("section") or {}
        for index, section_id in enumerate(panels.read_section_order(preferences)):
            row = box.row(align=True)
            handle = row.operator("hhz.section_drag", text="", icon='GRIP')
            handle.index = index
            row.label(text=panels.section_label(section_id))
            if section_state.get("target") == index:
                row.alert = True
        box.label(text="按住手柄上下拖动，松开确认", icon='INFO')

        # ---- 侧栏标签页：只做展示 ----
        box = layout.box()
        box.label(text="侧栏标签页")
        for name in panels.tab_names():
            box.label(text="  {}".format(name))
        box.label(text="标签页顺序 Blender 不开放给插件", icon='ERROR')
        box.label(text="（详见 README 的说明）")


class HHZ_PT_theme(bpy.types.Panel):
    """主题颜色。

    顶层面板，按需求只放在 N 面板里，饼菜单不收录这个功能。
    """

    bl_idname = "HHZ_PT_theme"
    bl_label = "主题颜色"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME

    @classmethod
    def poll(cls, context):
        return pages.is_active(context, pages.PAGE_THEME)

    def draw(self, context):
        layout = self.layout

        preferences = compat.get_preferences(context)
        if preferences is None:
            layout.label(text="读不到插件偏好设置", icon='ERROR')
            return
        current = theme.current_theme(context)
        if current is None:
            layout.label(text="读不到当前主题", icon='ERROR')
            return

        available = theme.available_paths(context)
        path = getattr(preferences, "theme_target", "")
        if theme.resolve(current, path) is None:
            layout.label(text="当前 Blender 没有这一项颜色", icon='INFO')

        # ---- 常驻 1：配色预设 + 饱和度 ----
        box = layout.box()
        box.label(text="配色预设（按钮 / 状态 / 当前帧）")
        grid = box.grid_flow(row_major=True, columns=2, align=True)
        for name in theme.preset_names():
            operation = grid.operator("hhz.theme_apply_preset", text=name)
            operation.preset = name
        box.prop(preferences, "preset_saturation", text="饱和度", slider=True)
        box.operator("hhz.theme_reapply_preset", icon='FILE_REFRESH')
        box.label(text="调到 60% 左右最耐看：强调色会整体压灰，反复调也不会越调越灰", icon='INFO')

        # ---- 常驻 2：恢复（一个范围下拉 + 一个按钮 + 一条状态）----
        # 三个恢复范围的语义各不相同，但都由同一个算符的 scope 属性承担，
        # 所以这里只画一个下拉去驱动它，不再摆三个按钮。
        group = theme.group_of(path)
        group_paths = [item for item in theme.paths_in_group(group, available)
                       if theme.has_default(item)]
        differing = theme.differing_paths(current, group_paths)
        missing_defaults = [item for item in available if not theme.has_default(item)]
        box = layout.box()
        box.label(text="恢复出厂颜色")
        row = box.row(align=True)
        row.prop(preferences, "restore_scope", text="")
        operation = row.operator("hhz.theme_restore_default", text="恢复", icon='LOOP_BACK')
        operation.scope = preferences.restore_scope
        if not group_paths:
            status = "「{}」没有记录出厂默认值，无法一键恢复".format(group)
            status_icon = 'INFO'
        elif differing:
            status = "「{}」{} 项有默认值，已改动 {} 项".format(
                group, len(group_paths), len(differing))
            status_icon = 'INFO'
        else:
            status = "「{}」{} 项都还是出厂值".format(group, len(group_paths))
            status_icon = 'CHECKMARK'
        if missing_defaults:
            status += "；本版本另有 {} 项没有默认值记录".format(len(missing_defaults))
        box.label(text=status, icon=status_icon)

        # ---- 常驻 3：控件颜色分开设置：每一项旁边都有自己的小调色盘 ----
        box = layout.box()
        box.label(text="控件颜色分开设置")
        box.prop(preferences, "simple_mode")
        prefix = getattr(preferences, "family", "")
        box.prop(preferences, "family", text="")
        shown_parts = 0
        for member, label, _group in theme.PART_MEMBERS:
            if theme.resolve(current, theme.family_path(prefix, member)) is None:
                continue
            row = box.row(align=True)
            row.label(text=label)
            row.prop(preferences, "part_" + member, text="")
            shown_parts += 1
        if not shown_parts:
            box.label(text="这个版本上没有可分开设置的项", icon='INFO')
        row = box.row(align=True)
        row.operator("hhz.theme_part_load", text="从当前主题读回", icon='FILE_REFRESH')
        row.operator("hhz.theme_auto_contrast", text="自动配文字色", icon='COLOR')
        # 对比度清单只在真的有问题时才占位置（没问题就不加一行"一切正常"）
        for text in theme.family_contrast_report(current, prefix):
            box.label(text=text + "（字会看不清）", icon='ERROR')

        # ---- 进阶：精调项全部收进这个折叠头，默认收起 ----
        header = layout.row(align=True)
        header.prop(preferences, "theme_advanced",
                    icon='TRIA_DOWN' if preferences.theme_advanced else 'TRIA_RIGHT',
                    emboss=False)
        if preferences.theme_advanced:
            # 1) 逐项微调：颜色项下拉 + 明文当前值 + 自定义颜色 + 应用颜色
            box = layout.box()
            box.label(text="逐项微调")
            column = box.column(align=True)
            column.prop(preferences, "theme_target", text="")
            # 明文写清当前选的是哪一项：即使下拉框本身显示异常，也一眼能看出选了什么
            box.label(text="当前项 {}".format(theme.label_of(path)))
            row = column.row(align=True)
            row.prop(preferences, "theme_color", text="")
            row.operator("hhz.theme_read", text="", icon='FILE_REFRESH')
            if theme.component_count(current, path) == 3:
                box.label(text="该项是 RGB，Alpha 会被忽略", icon='INFO')

            # 自己画一行明文读数：不再依赖那个很窄的数值框（显示成一团看不清的字符）
            shown = theme.get_color(current, path)
            if shown is not None:
                box.label(text="当前值 {}   R {:.3f}  G {:.3f}  B {:.3f}".format(
                    theme.color_to_hex(shown), shown[0], shown[1], shown[2]))
            else:
                box.label(text="这一项在当前版本上不存在", icon='INFO')

            box.label(text="自定义颜色")
            row = box.row(align=True)
            row.prop(preferences, "palette_color", text="")
            row.prop(preferences, "custom_hex", text="")
            box.label(text="调色盘选色 或 填 #RRGGBB，再点下面的「应用颜色」", icon='INFO')
            box.operator("hhz.theme_apply_color", text="应用颜色", icon='CHECKMARK')

            # 2) 全局按钮配色：一次改掉所有按钮（含预选中项）
            box = layout.box()
            box.label(text="全局按钮配色")
            row = box.row(align=True)
            row.label(text="底色")
            row.prop(preferences, "global_base", text="")
            row = box.row(align=True)
            row.label(text="选中色")
            row.prop(preferences, "global_accent", text="")
            box.prop(preferences, "global_auto_text")
            if not getattr(preferences, "global_auto_text", True):
                row = box.row(align=True)
                row.label(text="文字色")
                row.prop(preferences, "global_text", text="")
            box.operator("hhz.theme_apply_global", text="应用到全部按钮（含预选中项）",
                         icon='CHECKMARK')
            box.label(text="会写 {} 个控件家族，外加大纲、文件浏览器、属性、信息、控制台、编辑器图标的选中高亮".format(
                len(theme.WIDGET_FAMILIES)), icon='INFO')

            # 3) 自定义配色
            box = layout.box()
            box.label(text="自定义配色")
            row = box.row(align=True)
            row.prop(preferences, "custom_name", text="")
            row.operator("hhz.theme_custom_save", text="", icon='ADD')
            if theme.custom_names(getattr(preferences, "theme_custom", "")):
                box.prop(preferences, "custom_choice", text="")
                row = box.row(align=True)
                row.operator("hhz.theme_custom_apply", text="套用", icon='CHECKMARK')
                row.operator("hhz.theme_custom_delete", text="删除", icon='TRASH')
                box.label(text="保存与套用都会覆盖全部 {} 项颜色".format(len(available)), icon='INFO')
            else:
                box.label(text="起个名字点 + 就能把当前配色存下来", icon='INFO')

            # 4) 字体设置（Blender 偏好设置里的项）
            box = layout.box()
            box.label(text="字体设置（写进 Blender 偏好设置）")
            for key, label, _owner_name, _default, _tip in theme.font_items(context):
                target = theme.font_target(context, key)
                if target is None:
                    continue
                owner, attr = target
                if key in ("font_path_ui", "font_path_ui_mono"):
                    row = box.row(align=True)
                    row.prop(owner, attr, text=label)
                    pick = row.operator("hhz.font_pick", text="", icon='FILEBROWSER')
                    pick.mono = key == "font_path_ui_mono"
                else:
                    box.prop(owner, attr, text=label)
            box.operator("hhz.font_restore", icon='LOOP_BACK')

        layout.separator()
        layout.operator("hhz.theme_save_preferences", icon='FILE_TICK')
        layout.label(text="改动立即生效；不保存偏好设置则重启后还原", icon='INFO')


class HHZ_PT_layout(bpy.types.Panel):
    """页面布局：把当前工作区与区域划分存下来，在新文件里一键还原。"""

    bl_idname = "HHZ_PT_layout"
    bl_label = "页面布局"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME

    @classmethod
    def poll(cls, context):
        return pages.is_active(context, pages.PAGE_TOOLS)

    def draw(self, context):
        region = self.layout
        preferences = compat.get_preferences(context)
        if preferences is None:
            region.label(text="读不到插件偏好设置", icon='ERROR')
            return

        box = region.box()
        box.label(text="保存 / 使用布局")
        row = box.row(align=True)
        row.prop(preferences, "layout_name", text="")
        row.operator("hhz.layout_save", text="", icon='ADD')
        if layout.names(getattr(preferences, "layout_slots", "")):
            box.prop(preferences, "layout_choice", text="")
            row = box.row(align=True)
            row.operator("hhz.layout_apply", text="使用", icon='CHECKMARK')
            row.operator("hhz.layout_delete", text="删除", icon='TRASH')
            slots = layout.load(getattr(preferences, "layout_slots", ""))
            chosen = slots.get(getattr(preferences, "layout_choice", "")) or {}
            workspaces = chosen.get("workspaces") or []
            areas = sum(len(item.get("areas") or ()) for item in workspaces)
            box.label(text="共 {} 个工作区 / {} 个区域".format(len(workspaces), areas), icon='INFO')
        else:
            region.label(text="先把界面排好，起个名字点 + 保存", icon='INFO')

        box = region.box()
        box.label(text="新文件也用这套界面？")
        box.operator("hhz.layout_save_startup", icon='FILE_TICK')
        box.label(text="会覆盖 Blender 启动文件（点之前会弹确认）", icon='ERROR')
        region.label(text="「使用」只改当前文件；启动布局管的是新建文件", icon='INFO')


# --------------------------------------------------------------- 偏好设置


class HHZ_AddonPreferences(bpy.types.AddonPreferences):
    """插件偏好设置。

    属性必须写成「类型注解」形式，不能写成 ``key_type = StringProperty(...)``：
    后者在 Blender 4.0 / 5.x 上不会生成真正的 RNA 属性（拿到的是 _PropertyDeferred）。
    注解式是 4.0 ~ 5.x 上唯一可行的写法。
    """

    # 传统安装是 "redkolab"，4.2+ 扩展安装是 "bl_ext.<仓库>.redkolab"
    bl_idname = compat.PACKAGE

    key_type: StringProperty(
        name="按键标识",
        description="饼菜单快捷键的按键标识，例如默认的 E 键就是 E、SPACE 是空格。\n推荐直接点上面的「修改」，然后按一下想用的键",
        default=compat.DEFAULT_KEY_TYPE,
        update=_on_preferences_update,
    )
    ctrl: BoolProperty(name="Ctrl", default=compat.DEFAULT_KEY_CTRL, update=_on_preferences_update)
    shift: BoolProperty(name="Shift", default=compat.DEFAULT_KEY_SHIFT, update=_on_preferences_update)
    alt: BoolProperty(name="Alt", default=compat.DEFAULT_KEY_ALT, update=_on_preferences_update)
    oskey: BoolProperty(
        name="Cmd / Super",
        description="macOS 上的 Cmd 键（Windows/Linux 上一般用不到）",
        default=compat.DEFAULT_KEY_OSKEY,
        update=_on_preferences_update,
    )
    enable_pie: BoolProperty(
        name="启用饼菜单快捷键",
        description="关掉之后就只能从 N 面板呼出饼菜单",
        default=True,
        update=_on_preferences_update,
    )
    theme_target: EnumProperty(
        name="颜色项",
        description="要修改的主题颜色；只列出当前 Blender 版本真实存在的项",
        items=theme.target_items,
        update=_on_theme_target_update,
    )
    theme_color: FloatVectorProperty(
        name="颜色",
        description="当前颜色项的颜色；一改动就会立刻写进主题（RGB 项的 Alpha 会被忽略）",
        subtype='COLOR',
        size=4,
        min=0.0,
        max=1.0,
        default=(0.33, 0.33, 0.33, 1.0),
        update=_on_theme_color_update,
    )
    restore_scope: EnumProperty(
        name="恢复范围",
        description="「恢复」按钮作用的范围：当前这一项 / 当前分组 / 全部有默认值的项",
        items=(
            ('CURRENT', "本项", "只恢复当前选中的这一项颜色"),
            ('GROUP', "本组", "恢复当前颜色项所在分组的全部颜色"),
            ('ALL', "全部", "把记录了出厂默认值的颜色全部恢复"),
        ),
        default='CURRENT',
    )
    theme_advanced: BoolProperty(
        name="进阶",
        description="展开逐项微调、全局按钮配色、自定义配色与字体设置（默认收起）",
        default=False,
    )
    panel_order: StringProperty(
        name="分块顺序",
        description="本插件各面板在 N 面板里的先后顺序（逗号分隔，拖动排序时自动维护）",
        default=",".join(panels.DEFAULT_SECTION_ORDER),
        update=_on_panel_order_update,
    )
    custom_name: StringProperty(
        name="自定义配色名",
        description="保存自定义配色时用的名字",
        default="",
    )
    family: EnumProperty(
        name="控件家族",
        description="要分开设置颜色的控件；简洁模式下是一个分组（一批一起改）",
        items=family_items,
        update=_on_family_update,
    )
    simple_mode: BoolProperty(
        name="简洁模式",
        description="关掉之后颜色项与控件家族会列出全部选项（适合精调）",
        default=True,
        update=_on_simple_mode_update,
    )
    part_inner: FloatVectorProperty(
        name="底色", description="按钮本体的颜色",
        subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.33, 0.33, 0.33, 1.0), update=_on_part_update,
    )
    part_text: FloatVectorProperty(
        name="图标与文字", description="按钮上的图标与文字颜色",
        subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.9, 0.9, 0.9, 1.0), update=_on_part_update,
    )
    part_outline: FloatVectorProperty(
        name="边框", description="按钮的描边颜色",
        subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.24, 0.24, 0.24, 1.0), update=_on_part_update,
    )
    part_inner_sel: FloatVectorProperty(
        name="选中底色", description="被选中 / 按下时的底色",
        subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.28, 0.45, 0.70, 1.0), update=_on_part_update,
    )
    part_text_sel: FloatVectorProperty(
        name="选中文字", description="被选中 / 按下时的图标与文字颜色",
        subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0, 1.0), update=_on_part_update,
    )
    part_outline_sel: FloatVectorProperty(
        name="选中边框", description="被选中时的描边（5.x 才有）",
        subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.28, 0.45, 0.70, 1.0), update=_on_part_update,
    )
    part_item: FloatVectorProperty(
        name="高亮条/滑块", description="滑块、进度条填充、列表高亮条",
        subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.28, 0.45, 0.70, 1.0), update=_on_part_update,
    )
    palette_color: FloatVectorProperty(
        name="调色盘",
        description="自己挑一个颜色，点「应用颜色」写进当前颜色项",
        subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.776, 1.0, 0.0, 1.0),
    )
    preset_saturation: FloatProperty(
        name="饱和度",
        description="正在使用的预设里所有高饱和颜色的强度：1.0 原样，越低越灰（适合长时间工作）",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype='PERCENTAGE',
        update=_on_saturation_update,
    )
    last_preset: StringProperty(
        name="最近使用的预设",
        description="用来在调整饱和度时重新套用（自动维护）",
        default="",
    )
    custom_hex: StringProperty(
        name="十六进制",
        description="像 #C6FF00；改这里会同步到调色盘",
        default="#C6FF00",
        update=_on_hex_update,
    )
    global_base: FloatVectorProperty(
        name="全局底色", description="所有按钮的底色",
        subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.09, 0.10, 0.07, 1.0),
    )
    global_accent: FloatVectorProperty(
        name="全局选中色", description="所有按钮被选中/预选中时的颜色，也用于大纲等处的选中高亮",
        subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.776, 1.0, 0.0, 1.0),
    )
    global_text: FloatVectorProperty(
        name="全局文字色", description="关掉自动配文字色时用这个",
        subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.94, 0.96, 0.90, 1.0),
    )
    global_auto_text: BoolProperty(
        name="按底色自动配文字色",
        description="按底色亮度自动挑深色字或浅色字，保证按钮上的图标与文字看得清",
        default=True,
    )
    custom_choice: EnumProperty(
        name="自定义配色",
        description="已保存的自定义配色",
        items=custom_items,
    )
    theme_custom: StringProperty(
        name="自定义配色数据",
        description="已保存的自定义配色（JSON，别手改）",
        default="",
    )
    layout_name: StringProperty(
        name="布局名",
        description="保存页面布局时用的名字",
        default="",
    )
    layout_choice: EnumProperty(
        name="已保存布局",
        description="已保存的页面布局",
        items=layout_items,
    )
    layout_slots: StringProperty(
        name="布局数据",
        description="已保存的页面布局（JSON，别手改）",
        default="",
    )

    def hotkey_label(self):
        return compat.key_label(self.key_type, self.ctrl, self.shift, self.alt, self.oskey)

    def draw(self, context):
        layout = self.layout

        column = layout.column()
        row = column.row(align=True)
        row.label(text="饼菜单快捷键：{}".format(self.hotkey_label()))
        row = column.row(align=True)
        row.operator("hhz.capture_hotkey", text="按下新按键…", icon='KEYINGSET')
        row.operator("hhz.reset_hotkey", text="恢复默认", icon='LOOP_BACK')

        row = column.row(align=True)
        row.prop(self, "ctrl")
        row.prop(self, "shift")
        row.prop(self, "alt")
        row.prop(self, "oskey")

        column.separator()
        column.prop(self, "enable_pie")
        column.prop(self, "key_type")

        column.separator()
        column.label(text="N 面板位置：3D 视图 ▸ 侧栏（N）▸ {}".format(compat.ADDON_NAME))
        column.label(text="也可以在 偏好设置 ▸ 键位映射 里搜索「{}」直接改键。".format(compat.ADDON_NAME))
        column.label(text="英文名 {}。支持 Blender {}+，当前 {}".format(
            compat.ADDON_NAME_EN,
            ".".join(str(part) for part in compat.MIN_BLENDER_VERSION),
            compat.VERSION_STRING,
        ))
