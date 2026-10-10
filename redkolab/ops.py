# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab · 功能算符

三个核心功能（都作用在「选中的物体」，没选中时作用在活动物体）：

* :class:`HHZ_OT_remove_all_materials`  一键删除所有材质
* :class:`HHZ_OT_apply_all_modifiers`   一键应用所有修改器
* :class:`HHZ_OT_remove_all_modifiers`  一键删除所有修改器

另外还有菜单/面板用到的辅助算符：打开偏好设置、捕获快捷键、恢复默认快捷键，
以及 N 面板「主题颜色」用的几个算符（主题功能不进饼菜单）。
"""

import bpy

from . import compat, panels, stairs, theme, ui

# --------------------------------------------------------------------- 小工具


def _editable_targets(context):
    """返回 (可处理的物体, 被跳过的库链接物体名)。

    链接自其它 .blend 的物体不能改动数据，直接跳过并在报告里说明。
    """
    usable = []
    skipped = []
    for obj in compat.get_target_objects(context):
        if compat.is_library_object(obj):
            skipped.append(obj.name)
        else:
            usable.append(obj)
    return usable, skipped


def _join_notes(notes):
    return "；".join(note for note in notes if note)


class _BaseTargetsOperator(bpy.types.Operator):
    """三个核心算符的公共部分：poll、撤销、目标物体。"""

    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return compat.poll_objects(context)


# --------------------------------------------------------------- 删除所有材质


def _assigned_material_count(obj):
    """物体上真正挂了材质的槽数量。"""
    total = 0
    for slot in list(getattr(obj, "material_slots", ()) or ()):
        try:
            if slot.material is not None:
                total += 1
        except Exception:
            pass
    return total


def _strip_materials(obj):
    """清空单个物体上的所有材质槽，返回清掉的材质数量。"""
    before = _assigned_material_count(obj)

    # 1) 物体级材质槽（material_slots[i].link == 'OBJECT'）不走数据块的材质列表
    for slot in list(getattr(obj, "material_slots", ()) or ()):
        try:
            if getattr(slot, "link", None) == 'OBJECT' and slot.material is not None:
                slot.material = None
        except Exception:
            pass

    # 2) 数据块上的材质列表，clear() 会连同材质槽一起删掉
    materials = getattr(getattr(obj, "data", None), "materials", None)
    if materials is not None:
        try:
            if len(materials):
                materials.clear()
        except Exception:
            # 个别数据类型不支持 clear()，退化成逐个 pop
            try:
                while len(materials):
                    materials.pop()
            except Exception:
                pass
    return max(before - _assigned_material_count(obj), 0)


class HHZ_OT_remove_all_materials(_BaseTargetsOperator):
    bl_idname = "hhz.remove_all_materials"
    bl_label = "删除所有材质"
    bl_description = "一键移除选中物体的全部材质槽（材质数据块本身保留在文件里）"

    def execute(self, context):
        usable, skipped = _editable_targets(context)
        if not usable:
            self.report({'WARNING'}, "没有可处理的物体（库链接物体已跳过）")
            return {'CANCELLED'}

        removed = 0
        changed = 0
        for obj in usable:
            count = _strip_materials(obj)
            if count:
                removed += count
                changed += 1

        notes = []
        if skipped:
            notes.append("跳过 {} 个库链接物体".format(len(skipped)))

        if removed == 0:
            self.report({'INFO'}, _join_notes(["选中的物体本来就没有材质"] + notes))
        else:
            self.report(
                {'INFO'},
                _join_notes(["已清空 {} 个物体的材质（共 {} 个）".format(changed, removed)] + notes),
            )
        return {'FINISHED'}


# ------------------------------------------------------------- 处理修改器


def _apply_one_modifier(obj, modifier):
    """应用单个修改器，返回 (是否成功, 错误信息, 是否原本被禁用)。"""
    name = modifier.name
    was_disabled = not getattr(modifier, "show_viewport", True)
    if was_disabled:
        # 视口中被禁用的修改器在 2.8x/2.9x 上无法直接应用
        try:
            modifier.show_viewport = True
        except Exception:
            pass

    try:
        compat.run_op(bpy.ops.object.modifier_apply, obj, modifier=name)
        return True, None, was_disabled
    except Exception as error:
        return False, str(error), was_disabled


def _convert_to_mesh(context, obj):
    """兜底：用「转换为网格」一次性把修改器烘焙进数据（可处理形态键）。"""
    try:
        compat.run_op(bpy.ops.object.convert, obj, target='MESH', keep_original=False)
    except Exception:
        return False
    try:
        return len(obj.modifiers) == 0
    except Exception:
        # 物体被替换掉了，也算成功
        return True


def _apply_object_modifiers(context, obj):
    """处理单个物体的所有修改器。

    返回 ``(应用数量, 是否用了转换兜底, 失败信息, 被禁用的数量)``。
    """
    names = [modifier.name for modifier in obj.modifiers]
    if not names:
        return 0, False, None, 0

    # 多用户数据不允许应用修改器，先复制成单用户
    compat.ensure_single_user_data(obj)

    applied = 0
    disabled_count = 0
    failure = None

    with compat.temporary_visible(obj):
        for name in names:
            modifier = obj.modifiers.get(name)
            if modifier is None:
                continue
            ok, error, was_disabled = _apply_one_modifier(obj, modifier)
            if ok:
                applied += 1
                if was_disabled:
                    disabled_count += 1
            else:
                # Blender 的报错文本常带换行，压成一行方便看
                reason = " ".join(str(error).split()) if error else ""
                failure = "{}（{}）".format(name, reason) if reason else name
                break

        if failure is not None and getattr(obj, "type", "") == 'MESH':
            pending = len(obj.modifiers)
            if _convert_to_mesh(context, obj):
                return applied + pending, True, None, disabled_count

    return applied, False, failure, disabled_count


class HHZ_OT_apply_all_modifiers(_BaseTargetsOperator):
    bl_idname = "hhz.apply_all_modifiers"
    bl_label = "应用所有修改器"
    bl_description = "一键把选中物体的全部修改器烘焙进网格（自动处理多用户数据、被禁用修改器与形态键）"

    def execute(self, context):
        usable, skipped = _editable_targets(context)
        if not usable:
            self.report({'WARNING'}, "没有可处理的物体（库链接物体已跳过）")
            return {'CANCELLED'}

        applied = 0
        converted = []
        touched = 0
        empty = 0
        disabled = 0
        failures = []
        hidden = []

        for obj in usable:
            if not compat.object_in_view_layer(context, obj):
                hidden.append(obj.name)
                continue

            count, used_convert, failure, disabled_count = _apply_object_modifiers(context, obj)
            if failure is not None:
                failures.append("{}：{}".format(obj.name, failure))
            if count:
                applied += count
                touched += 1
                disabled += disabled_count
            elif failure is None:
                empty += 1
            if used_convert:
                converted.append(obj.name)

        notes = []
        if disabled:
            notes.append("其中 {} 个原本在视口中被禁用".format(disabled))
        if converted:
            notes.append("{} 个物体用「转换为网格」烘焙（含形态键）".format(len(converted)))
        if hidden:
            notes.append("跳过 {} 个不在当前视图层的物体".format(len(hidden)))
        if skipped:
            notes.append("跳过 {} 个库链接物体".format(len(skipped)))
        if failures:
            notes.append("失败：" + "，".join(failures))

        if applied == 0 and not failures:
            message = "选中的物体没有修改器" if not empty else "没有可应用的修改器"
        else:
            message = "已在 {} 个物体上应用 {} 个修改器".format(touched, applied)

        # 一个都没成功 → ERROR；部分成功 → WARNING；全部成功 → INFO
        if failures and touched == 0:
            level = {'ERROR'}
        elif failures:
            level = {'WARNING'}
        else:
            level = {'INFO'}
        self.report(level, _join_notes([message] + notes))
        return {'FINISHED'}


# --------------------------------------------------------------- 删除所有修改器


def _strip_modifiers(obj):
    """删掉单个物体的全部修改器，返回删除数量。"""
    modifiers = getattr(obj, "modifiers", None)
    if modifiers is None:
        return 0
    removed = 0
    for modifier in reversed(list(modifiers)):
        try:
            modifiers.remove(modifier)
            removed += 1
        except Exception:
            pass
    return removed


class HHZ_OT_remove_all_modifiers(_BaseTargetsOperator):
    bl_idname = "hhz.remove_all_modifiers"
    bl_label = "删除所有修改器"
    bl_description = "一键删除选中物体的全部修改器（不烘焙，直接丢弃）"

    def execute(self, context):
        usable, skipped = _editable_targets(context)
        if not usable:
            self.report({'WARNING'}, "没有可处理的物体（库链接物体已跳过）")
            return {'CANCELLED'}

        removed = 0
        changed = 0
        for obj in usable:
            count = _strip_modifiers(obj)
            if count:
                removed += count
                changed += 1

        notes = []
        if skipped:
            notes.append("跳过 {} 个库链接物体".format(len(skipped)))

        if removed == 0:
            self.report({'INFO'}, _join_notes(["选中的物体本来就没有修改器"] + notes))
        else:
            self.report(
                {'INFO'},
                _join_notes(["已从 {} 个物体删除 {} 个修改器".format(changed, removed)] + notes),
            )
        return {'FINISHED'}


# --------------------------------------------------------------- 设置相关算符


class HHZ_OT_stairs(bpy.types.Operator):
    """一键楼梯（模态）：鼠标换方向、滚轮改节数、左键确认、右键/Esc 撤销。"""

    bl_idname = "hhz.stairs"
    bl_label = "一键楼梯"
    bl_description = (
        "把选中体块整成 0.2 高 × 0.3 宽（长度不变）并做相对偏移阵列；"
        "移动鼠标换方向、滚轮改节数、左键确认、右键/Esc 取消"
    )
    bl_options = {'REGISTER', 'UNDO'}

    count: bpy.props.IntProperty(
        name="节数",
        description="楼梯节数（阵列数量），滚轮也可以改",
        default=stairs.DEFAULT_COUNT,
        min=stairs.MIN_COUNT,
        max=stairs.MAX_COUNT,
    )

    @classmethod
    def poll(cls, context):
        return compat.poll_objects(context)

    # ---------------------------------------------------------------- 交互
    def invoke(self, context, event):
        targets = compat.get_target_objects(context)
        if not targets:
            self.report({'WARNING'}, "先选中要变成楼梯的体块")
            return {'CANCELLED'}

        self._objects = []
        self._direction = "RIGHT"
        # 从饼菜单/面板点进来时事件坐标是点击位置，所以原点等第一次鼠标移动再定
        self._origin = None

        for obj in targets:
            try:
                original = tuple(obj.dimensions)
            except Exception:
                continue
            stairs.resize_step(obj)
            stairs.add_array(obj, stairs.factors_for_direction(self._direction), self.count)
            self._objects.append((obj, original))

        if not self._objects:
            self.report({'WARNING'}, "选中的物体不支持这个操作")
            return {'CANCELLED'}

        self._set_status(context, self._hint())
        context.window_manager.modal_handler_add(self)
        compat.tag_redraw(context)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self._revert()
            self._set_status(context, None)
            compat.tag_redraw(context)
            self.report({'INFO'}, "已取消，体块恢复原样")
            return {'CANCELLED'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self._set_status(context, None)
            compat.tag_redraw(context)
            self.report({'INFO'}, "楼梯 {} 节 · {}".format(
                stairs.clamp_count(self.count), self._direction_label()))
            return {'FINISHED'}

        changed = False
        if event.type == 'WHEELUPMOUSE':
            self.count = stairs.clamp_count(self.count + 1)
            changed = True
        elif event.type == 'WHEELDOWNMOUSE':
            self.count = stairs.clamp_count(self.count - 1)
            changed = True
        elif event.type == 'MOUSEMOVE':
            if self._origin is None:
                self._origin = (event.mouse_x, event.mouse_y)
                changed = True
            else:
                direction = stairs.direction_from_delta(
                    event.mouse_x - self._origin[0], event.mouse_y - self._origin[1])
                if direction and direction != self._direction:
                    self._direction = direction
                    changed = True

        if changed:
            self._refresh()
            self._set_status(context, self._hint())
        return {'RUNNING_MODAL'}

    # ---------------------------------------------------------------- 内部
    def _direction_label(self):
        return {
            "RIGHT": "+X 向右",
            "LEFT": "-X 向左",
            "UP": "+Y 向上",
            "DOWN": "-Y 向下",
        }.get(self._direction, self._direction)

    def _hint(self):
        return "楼梯 {} 节 · {} ｜ 移动鼠标换方向 · 滚轮改节数 · 左键确认 · 右键/Esc 取消".format(
            stairs.clamp_count(self.count), self._direction_label())

    def _refresh(self):
        """按当前方向与节数更新所有阵列修改器。"""
        for obj, _original in self._objects:
            stairs.add_array(obj, stairs.factors_for_direction(self._direction), self.count)

    def _revert(self):
        """取消：删掉阵列修改器并把尺寸还原。"""
        for obj, original in self._objects:
            stairs.remove_array(obj)
            try:
                obj.dimensions = original
            except Exception:
                pass

    def _set_status(self, context, text):
        setter = getattr(getattr(context, "workspace", None), "status_text_set", None)
        if setter is None:
            return
        try:
            setter(text)
        except Exception:
            pass


class HHZ_OT_open_preferences(bpy.types.Operator):
    bl_idname = "hhz.open_preferences"
    bl_label = "红红子lab 设置"
    bl_description = "打开偏好设置里的红红子lab面板（修改饼菜单快捷键）"

    def execute(self, context):
        try:
            context.preferences.active_section = 'ADDONS'
        except Exception:
            pass
        try:
            context.window_manager.addon_search = compat.ADDON_ID
        except Exception:
            pass
        try:
            bpy.ops.screen.userpref_show('INVOKE_DEFAULT')
        except Exception as error:
            self.report({'ERROR'}, "无法打开偏好设置：{}".format(error))
            return {'CANCELLED'}
        return {'FINISHED'}


class HHZ_OT_reset_hotkey(bpy.types.Operator):
    bl_idname = "hhz.reset_hotkey"
    bl_label = "恢复默认快捷键"
    bl_description = "把饼菜单快捷键恢复为默认的 ` 键"

    def execute(self, context):
        if not ui.apply_hotkey(
            context,
            compat.DEFAULT_KEY_TYPE,
            compat.DEFAULT_KEY_CTRL,
            compat.DEFAULT_KEY_SHIFT,
            compat.DEFAULT_KEY_ALT,
            compat.DEFAULT_KEY_OSKEY,
        ):
            self.report({'WARNING'}, "已恢复默认值，但键位映射注册失败")
            return {'CANCELLED'}
        self.report({'INFO'}, "饼菜单快捷键已恢复为 {}".format(compat.default_hotkey_label()))
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_capture_hotkey(bpy.types.Operator):
    bl_idname = "hhz.capture_hotkey"
    bl_label = "修改饼菜单快捷键"
    bl_description = "按下想要使用的按键（Esc 取消，Backspace 恢复默认）"

    bl_options = {'INTERNAL'}

    _HINT = "红红子lab：请按下新的饼菜单快捷键（Esc 取消，Backspace 恢复默认 {}）".format(
        compat.default_hotkey_label())

    def invoke(self, context, event):
        if compat.get_preferences(context) is None:
            self.report({'ERROR'}, "读不到插件偏好设置")
            return {'CANCELLED'}
        try:
            context.window_manager.modal_handler_add(self)
        except Exception as error:
            self.report({'ERROR'}, "无法进入按键捕获模式：{}".format(error))
            return {'CANCELLED'}
        self._set_status(context, self._HINT)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            if event.value == 'PRESS':
                self._set_status(context, None)
                return {'CANCELLED'}
            return {'RUNNING_MODAL'}

        if event.type in {
            'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'TIMER', 'TIMER_REPORT',
            'TIMERJOBS', 'WINDOW_DEACTIVATE', 'NOTHING', 'LEFTMOUSE', 'MIDDLEMOUSE',
        }:
            return {'RUNNING_MODAL'}

        if event.type == 'RIGHTMOUSE':
            if event.value == 'PRESS':
                self._set_status(context, None)
                return {'CANCELLED'}
            return {'RUNNING_MODAL'}

        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}

        if compat.is_modifier_key(event.type):
            # 只按了修饰键：先把状态显示出来，等真正的按键
            ctrl, shift, alt, oskey = compat.modifier_flags(event)
            parts = [
                name for name, pressed in
                (("Ctrl", ctrl), ("Shift", shift), ("Alt", alt), ("Cmd", oskey)) if pressed
            ]
            self._set_status(
                context,
                "红红子lab：{} + ？".format(" + ".join(parts)) if parts else self._HINT,
            )
            return {'RUNNING_MODAL'}

        if event.type == 'BACK_SPACE':
            self._apply(
                context,
                compat.DEFAULT_KEY_TYPE,
                compat.DEFAULT_KEY_CTRL,
                compat.DEFAULT_KEY_SHIFT,
                compat.DEFAULT_KEY_ALT,
                compat.DEFAULT_KEY_OSKEY,
                reset=True,
            )
            return {'FINISHED'}

        ctrl, shift, alt, oskey = compat.modifier_flags(event)
        self._apply(context, event.type, ctrl, shift, alt, oskey)
        return {'FINISHED'}

    # -- 内部 ------------------------------------------------------------

    def _apply(self, context, key_type, ctrl, shift, alt, oskey, reset=False):
        registered = ui.apply_hotkey(context, key_type, ctrl, shift, alt, oskey)
        self._set_status(context, None)
        label = compat.key_label(key_type, ctrl, shift, alt, oskey)
        if not registered:
            self.report({'WARNING'}, "已保存快捷键 {}，但键位映射注册失败".format(label))
            return
        if reset:
            self.report({'INFO'}, "已恢复默认快捷键 {}".format(label))
        else:
            self.report({'INFO'}, "饼菜单快捷键已设为 {}".format(label))
        compat.tag_redraw(context)

    def _set_status(self, context, text):
        try:
            context.workspace.status_text_set(text)
        except Exception:
            pass


# --------------------------------------------------------------- 主题颜色算符


class HHZ_OT_theme_read(bpy.types.Operator):
    bl_idname = "hhz.theme_read"
    bl_label = "读取当前值"
    bl_description = "把主题里这一项颜色的当前值读回颜色选择器"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        if preferences is None:
            self.report({'ERROR'}, "读不到插件偏好设置")
            return {'CANCELLED'}
        if not theme.read_into_preferences(context, preferences):
            self.report({'WARNING'}, "读不到这一项颜色（当前 Blender 版本可能没有它）")
            return {'CANCELLED'}
        self.report({'INFO'}, "已读取当前颜色")
        return {'FINISHED'}


class HHZ_OT_theme_apply_preset(bpy.types.Operator):
    bl_idname = "hhz.theme_apply_preset"
    bl_label = "应用配色预设"
    bl_description = "一键套用一套配色（若按钮常态 / 选中色 / 状态色）"

    preset: bpy.props.EnumProperty(
        name="预设",
        items=[(name, name, "套用「{}」配色".format(name)) for name in theme.preset_names()],
    )

    def execute(self, context):
        current = theme.current_theme(context)
        if current is None:
            self.report({'ERROR'}, "读不到当前主题")
            return {'CANCELLED'}

        changed = theme.apply_preset(current, self.preset)
        if not changed:
            self.report({'WARNING'}, "这套预设没有可用颜色")
            return {'CANCELLED'}

        preferences = compat.get_preferences(context)
        if preferences is not None:
            theme.read_into_preferences(context, preferences)
        self.report({'INFO'}, "已套用「{}」配色（{} 项）".format(self.preset, changed))
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_theme_restore_default(bpy.types.Operator):
    bl_idname = "hhz.theme_restore_default"
    bl_label = "恢复颜色默认值"
    bl_description = "把当前颜色（或整组控件 / 视口颜色）恢复为 Blender 出厂默认"

    scope: bpy.props.EnumProperty(
        name="范围",
        items=(
            ('CURRENT', "当前颜色", "只恢复当前选中的这一项"),
            ('WIDGET', "全部控件颜色", "恢复按钮与状态色（灰色按钮 / 选中蓝色那一批）"),
            ('VIEWPORT', "全部视口颜色", "恢复视口那一组的全部颜色"),
        ),
        default='CURRENT',
    )

    def execute(self, context):
        current = theme.current_theme(context)
        if current is None:
            self.report({'ERROR'}, "读不到当前主题")
            return {'CANCELLED'}

        if self.scope == 'WIDGET':
            changed = theme.restore_widget_defaults(current)
            if not changed:
                self.report({'WARNING'}, "没有可恢复的控件颜色")
                return {'CANCELLED'}
            self.report({'INFO'}, "已恢复 {} 项控件颜色（按钮 / 状态）".format(changed))
        elif self.scope == 'VIEWPORT':
            changed = theme.restore_viewport_defaults(current)
            if not changed:
                self.report({'WARNING'}, "没有可恢复的视口颜色")
                return {'CANCELLED'}
            self.report({'INFO'}, "已恢复 {} 项视口颜色".format(changed))
        else:
            preferences = compat.get_preferences(context)
            path = getattr(preferences, "theme_target", "") if preferences else ""
            if not theme.has_default(path):
                self.report({'WARNING'}, "这一项没有记录默认值，无法一键恢复")
                return {'CANCELLED'}
            if not theme.restore_default(current, path):
                self.report({'WARNING'}, "恢复失败")
                return {'CANCELLED'}
            self.report({'INFO'}, "已恢复该颜色默认值")

        preferences = compat.get_preferences(context)
        if preferences is not None:
            theme.read_into_preferences(context, preferences)
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_theme_save_preferences(bpy.types.Operator):
    bl_idname = "hhz.theme_save_preferences"
    bl_label = "保存到偏好设置"
    bl_description = "把主题颜色写进 Blender 偏好设置（不保存的话重启后会还原）"

    def execute(self, context):
        try:
            bpy.ops.wm.save_userprefs()
        except Exception as error:
            self.report({'ERROR'}, "保存失败：{}".format(error))
            return {'CANCELLED'}
        self.report({'INFO'}, "主题颜色已保存到偏好设置")
        return {'FINISHED'}


# ------------------------------------------------------------- 面板顺序算符


class _DragReorderBase(bpy.types.Operator):
    """按住手柄上下拖动的公共实现（不注册，只给子类用）。

    目标槽位用「鼠标相对按下位置的位移 ÷ 行高」换算：面板里拿不到每一行的
    屏幕坐标，但鼠标位移是有的，这样不用依赖任何像素级布局，也不会因为
    主题行高不同而失效。

    Blender 点击面板按钮时 invoke 可能拿到按下、也可能拿到松开，两种都兼容：
    按下进入就是「拖到位置松手确认」，松开进入就是「移动后点一下确认」。
    """

    bl_options = {'INTERNAL'}

    index: bpy.props.IntProperty(name="序号", default=0)

    #: 子类指定：拖动状态键、"section" / "tab"
    kind = "section"
    #: 换一位需要拖多少像素
    row_height = 20.0

    def invoke(self, context, event):
        self._items = list(self.gather(context))
        if not 0 <= self.index < len(self._items):
            self.report({'WARNING'}, "没有可拖动的内容")
            return {'CANCELLED'}

        self._original = list(self._items)
        self._start_y = event.mouse_region_y
        self._start_index = self.index
        self._current = self.index
        self._confirm_on_release = (event.value == 'PRESS')
        panels.DRAG_STATE[self.kind] = {"index": self.index, "target": self.index}

        try:
            context.window_manager.modal_handler_add(self)
        except Exception as error:
            self.report({'ERROR'}, "无法进入拖动模式：{}".format(error))
            return {'CANCELLED'}

        if self._confirm_on_release:
            self._set_status(context, "拖动中：上下移动鼠标换位 · 松开确认 · Esc 取消")
        else:
            self._set_status(context, "拖动中：上下移动鼠标换位 · 再点一下确认 · Esc 取消")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            target = panels.target_from_drag(
                len(self._items), self._start_index,
                event.mouse_region_y - self._start_y, self.row_height)
            if target != self._current:
                self._current = target
                self._items = panels.move_item(self._original, self._start_index, target)
                panels.DRAG_STATE[self.kind] = {"index": self._start_index, "target": target}
                self.commit(context, self._items)
                compat.tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type == 'ESC' and event.value == 'PRESS':
            self.commit(context, self._original)
            self._finish(context)
            return {'CANCELLED'}

        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
            self.commit(context, self._original)
            self._finish(context)
            return {'CANCELLED'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'}:
            if self._confirm_on_release and event.value == 'PRESS':
                return {'RUNNING_MODAL'}
            self._finish(context)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    # -- 内部 ------------------------------------------------------------

    def _finish(self, context):
        panels.DRAG_STATE.pop(self.kind, None)
        self._set_status(context, None)
        compat.tag_redraw(context)

    def _set_status(self, context, text):
        try:
            context.workspace.status_text_set(text)
        except Exception:
            pass

    # -- 子类实现 --------------------------------------------------------

    def gather(self, context):
        return []

    def commit(self, context, items):
        pass


class HHZ_OT_section_drag(_DragReorderBase):
    bl_idname = "hhz.section_drag"
    bl_label = "拖动调整分块顺序"
    bl_description = "按住手柄上下拖动，实时调整本插件各分块在 N 面板里的顺序"

    kind = "section"

    def gather(self, context):
        return panels.read_section_order(compat.get_preferences(context))

    def commit(self, context, items):
        preferences = compat.get_preferences(context)
        if preferences is not None:
            preferences.panel_order = ",".join(items)
        # 统一入口：写 bl_order + 按目标顺序重新注册本插件的面板
        ui.apply_panel_order(context)


# 说明：曾经还有一个 hhz.tab_drag（拖动排其它插件的标签页）。
# 实测那需要把其它插件的面板注销再注册，会让 Blender 4.0 / 4.2 直接段错误崩溃，
# 所以这个功能已移除，只保留"面板顺序"面板里的标签页只读列表。
