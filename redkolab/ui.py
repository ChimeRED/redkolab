# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab · 界面层

包含三部分：

* 饼式菜单（默认快捷键 Ctrl+E，可在插件偏好设置里改）
* 3D 视图 N 面板（侧栏里的「红红子lab」标签页），下面挂着「主题颜色」子面板
* 插件偏好设置（快捷键捕获、手动改键、启用开关、主题颜色项）

键位注册走 ``window_manager.keyconfigs.addon``，这是插件专用键位表
（4.0 ~ 5.x 都支持），卸载插件时会被自动清掉。
"""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatVectorProperty, StringProperty

from . import compat, panels, theme

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
    """切换颜色项时，把该项当前的色值读进选择器。"""
    theme.read_into_preferences(context, self)


def _on_theme_color_update(self, context):
    """选择器一变就写进主题 —— 实时预览的那一步。"""
    theme.write_from_preferences(context, self)


def _on_panel_order_update(self, context):
    """分块顺序变了就重新写 bl_order。"""
    apply_panel_order(context)


def _on_tab_order_update(self, context):
    """标签页顺序变了就重新写各面板的 bl_order（并继续记录原值）。"""
    if not self.tab_order:
        return
    names = panels.parse_order(self.tab_order, valid_ids=panels.tab_names())
    _, backup = panels.apply_tab_order(names, panels.load_backup(self.tab_backup))
    self.tab_backup = panels.dump_backup(backup)


def apply_panel_order(context=None):
    """把偏好设置里的顺序写进各面板的 bl_order。

    注册时调用一次（bl_order 是运行时写进类里的，重启后会丢，必须重新应用），
    拖动排序时也会调用。
    """
    preferences = compat.get_preferences(context)
    order = panels.read_section_order(preferences)
    panels.apply_section_order(order, {"HHZ_PT_order": panels.ORDER_PANEL_ORDER})

    if preferences is not None:
        stored_tabs = getattr(preferences, "tab_order", "")
        if stored_tabs:
            names = panels.parse_order(stored_tabs, valid_ids=panels.tab_names())
            _, backup = panels.apply_tab_order(
                names, panels.load_backup(getattr(preferences, "tab_backup", "")))
            preferences.tab_backup = panels.dump_backup(backup)
    return order


def _on_preferences_update(self, context):
    refresh_keymap()


# ------------------------------------------------------------------- 饼菜单


class HHZ_MT_pie(bpy.types.Menu):
    bl_idname = PIE_IDNAME
    bl_label = compat.ADDON_NAME

    def draw(self, context):
        layout = self.layout
        pie = layout.menu_pie()

        # 三个方向：左 / 右 / 下 —— 插件设置已按需求挪到 N 面板，不再占饼菜单
        pie.operator("hhz.remove_all_materials", icon='MATERIAL')
        pie.operator("hhz.apply_all_modifiers", icon='MODIFIER')
        pie.operator("hhz.remove_all_modifiers", icon='TRASH')


# ------------------------------------------------------------------- N 面板


class HHZ_PT_main(bpy.types.Panel):
    bl_idname = "HHZ_PT_main"
    bl_label = compat.ADDON_NAME
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME

    def draw(self, context):
        layout = self.layout

        column = layout.column(align=True)
        column.operator("hhz.remove_all_materials", icon='MATERIAL')
        column.operator("hhz.apply_all_modifiers", icon='MODIFIER')
        column.operator("hhz.remove_all_modifiers", icon='TRASH')

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
            text="选中 {} 个物体 · 材质槽 {} · 修改器 {}".format(
                len(targets), slot_count, modifier_count),
            icon='INFO',
        )


class HHZ_PT_hotkey(bpy.types.Panel):
    """饼菜单与快捷键（属于可拖动排序的分块）。"""

    bl_idname = "HHZ_PT_hotkey"
    bl_label = "饼菜单与快捷键"
    bl_parent_id = "HHZ_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME

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

        layout.label(text="饼菜单里只有上面三个功能", icon='INFO')


class HHZ_PT_settings(bpy.types.Panel):
    """插件设置（属于可拖动排序的分块）。

    这个入口原来放在饼菜单里，按需求挪到 N 面板。
    """

    bl_idname = "HHZ_PT_settings"
    bl_label = "插件设置"
    bl_parent_id = "HHZ_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME

    def draw(self, context):
        layout = self.layout

        layout.operator("hhz.open_preferences", icon='SETTINGS')

        column = layout.column(align=True)
        column.label(text="{} · {}".format(compat.ADDON_NAME, compat.ADDON_NAME_EN))
        column.label(text="支持 Blender {}+ · 当前 {}".format(
            ".".join(str(part) for part in compat.MIN_BLENDER_VERSION),
            compat.VERSION_STRING,
        ))


class HHZ_PT_order(bpy.types.Panel):
    """面板顺序：拖动调整本插件分块与侧栏标签页的顺序（固定排在最后）。"""

    bl_idname = "HHZ_PT_order"
    bl_label = "面板顺序"
    bl_parent_id = "HHZ_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME

    def draw(self, context):
        layout = self.layout

        preferences = compat.get_preferences(context)
        if preferences is None:
            layout.label(text="读不到插件偏好设置", icon='ERROR')
            return

        # ---- 本插件的分块 ----
        box = layout.box()
        box.label(text="本插件的分块")
        section_state = panels.DRAG_STATE.get("section") or {}
        for index, section_id in enumerate(panels.read_section_order(preferences)):
            row = box.row(align=True)
            handle = row.operator("hhz.section_drag", text="", icon='GRIP')
            handle.index = index
            row.label(text=panels.section_label(section_id))
            if section_state.get("target") == index:
                row.alert = True
        box.label(text="按住手柄上下拖动，松开确认", icon='INFO')

        # ---- 侧栏标签页 ----
        box = layout.box()
        box.label(text="侧栏标签页")
        tab_state = panels.DRAG_STATE.get("tab") or {}
        tab_names = panels.display_tab_order(preferences)
        if len(tab_names) < 2:
            box.label(text="侧栏里还没有其它标签页", icon='INFO')
        for index, name in enumerate(tab_names):
            row = box.row(align=True)
            handle = row.operator("hhz.tab_drag", text="", icon='GRIP')
            handle.index = index
            row.label(text=name)
            if tab_state.get("target") == index:
                row.alert = True
        box.operator("hhz.tab_restore", icon='LOOP_BACK')
        box.label(text="会改写其它插件的面板 bl_order，可一键还原", icon='ERROR')


class HHZ_PT_theme(bpy.types.Panel):
    """主题颜色子面板。

    按需求只放在 N 面板里，饼菜单不收录这个功能。
    """

    bl_idname = "HHZ_PT_theme"
    bl_label = "主题颜色"
    bl_parent_id = "HHZ_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = compat.ADDON_NAME

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

        path = getattr(preferences, "theme_target", "")
        if theme.resolve(current, path) is None:
            layout.label(text="当前 Blender 没有这一项颜色", icon='INFO')

        column = layout.column(align=True)
        column.prop(preferences, "theme_target", text="")
        row = column.row(align=True)
        row.prop(preferences, "theme_color", text="")
        row.operator("hhz.theme_read", text="", icon='FILE_REFRESH')
        if theme.component_count(current, path) == 3:
            column.label(text="该项是 RGB，Alpha 会被忽略", icon='INFO')

        row = layout.row(align=True)
        operation = row.operator("hhz.theme_restore_default", text="恢复默认")
        operation.scope = 'CURRENT'
        operation = row.operator("hhz.theme_restore_default", text="全部视口")
        operation.scope = 'VIEWPORT'

        box = layout.box()
        box.label(text="视口配色预设")
        grid = box.grid_flow(row_major=True, columns=3, align=True)
        for name in theme.preset_names():
            operation = grid.operator("hhz.theme_apply_preset", text=name)
            operation.preset = name

        layout.separator()
        layout.operator("hhz.theme_save_preferences", icon='FILE_TICK')
        layout.label(text="改动立即生效；不保存偏好设置则重启后还原", icon='INFO')


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
    panel_order: StringProperty(
        name="分块顺序",
        description="本插件各分块在 N 面板里的先后顺序（逗号分隔，拖动排序时自动维护）",
        default=",".join(panels.DEFAULT_SECTION_ORDER),
        update=_on_panel_order_update,
    )
    tab_order: StringProperty(
        name="标签页顺序",
        description="侧栏标签页的期望顺序（逗号分隔，拖动排序时自动维护）",
        default="",
        update=_on_tab_order_update,
    )
    tab_backup: StringProperty(
        name="标签页原顺序备份",
        description="拖动排序前记录的各插件面板 bl_order 原值，用于一键还原",
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
