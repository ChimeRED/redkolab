# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab 功能算符

三个核心功能（都作用在「选中的物体」，没选中时作用在活动物体）：

* :class:`HHZ_OT_remove_all_materials`  一键删除所有材质
* :class:`HHZ_OT_apply_all_modifiers`   一键应用所有修改器
* :class:`HHZ_OT_remove_all_modifiers`  一键删除所有修改器

另外还有菜单/面板用到的辅助算符：打开偏好设置、捕获快捷键、恢复默认快捷键、
检查并修复缺失文件，以及 N 面板「主题颜色」用的几个算符（主题功能不进饼菜单）。
"""

import os

import bpy

from . import compat, layout, panels, stairs, theme, ui

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

        axis = stairs.axis_for_direction(self._direction)
        for obj in targets:
            # local 是网格自身包围盒（与修改器无关），original 是选中前的尺寸，
            # original_scale 用来取消时还原 —— 三者都在挂阵列之前抓
            try:
                local = stairs.local_size(obj)
                original = stairs.mesh_dimensions(obj)
                original_scale = tuple(obj.scale)
            except Exception:
                continue
            stairs.resize_step(obj, axis, local, original)
            stairs.add_array(obj, stairs.factors_for_direction(self._direction), self.count)
            self._objects.append((obj, local, original, original_scale))

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
            self.report({'INFO'}, "楼梯 {} 节 {}".format(
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
        return ("楼梯 {} 节 {}（0.3 沿 {} 轴，另一条水平轴保持原尺寸）"
                " ｜ 移动鼠标换方向 滚轮改节数 左键确认 右键/Esc 取消").format(
            stairs.clamp_count(self.count), self._direction_label(),
            stairs.axis_for_direction(self._direction))

    def _refresh(self):
        """按当前方向与节数更新形状与阵列（换方向时体块朝向跟着换）。"""
        axis = stairs.axis_for_direction(self._direction)
        for obj, local, original, _scale in self._objects:
            stairs.resize_step(obj, axis, local, original)
            stairs.add_array(obj, stairs.factors_for_direction(self._direction), self.count)

    def _revert(self):
        """取消：删掉阵列修改器并把缩放还原。"""
        for obj, _local, _original, original_scale in self._objects:
            stairs.remove_array(obj)
            try:
                obj.scale = original_scale
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


# --------------------------------------------------------- 检查并修复缺失文件


#: 保存时会参与「自动打包资源」的数据块集合：(``bpy.data`` 上的属性名, 中文类型名)。
#: 依据 Blender 的打包实现 ``BKE_packedfile_pack_all()``：图像（``IMA_SRC_FILE`` /
#: ``IMA_SRC_TILED``）、字体（非内置）、声音、体积；影片剪辑（``movieclips``）是另一类
#: "指向外部文件"的数据块，源文件没了同样要清。其它集合（``cache_files`` / ``texts`` 等）
#: 由 :func:`_packable_collections` 用 ``getattr`` 动态纳入 —— 不为某个版本上不存在的属性写死代码。
PACKABLE_COLLECTIONS = (
    ("images", "图像"),
    ("sounds", "声音"),
    ("fonts", "字体"),
    ("movieclips", "影片剪辑"),
    ("volumes", "体积"),
)

#: 明确**不扫**的集合：
#: * ``libraries`` —— 链接库不是可打包数据，删掉它会连带断开链接（缺失的库该用
#:   「文件 ▸ 外部数据 ▸ 查找缺失文件」重新链接），不属于本算符的范围；
#: * ``all_ids`` —— 这是"文件里所有 ID"的汇总视图（5.x），删它没有意义，也会与具体集合重复。
NON_PACKABLE_COLLECTIONS = ("libraries", "all_ids")

#: 明显"不是外部文件"的来源取值：生成 / 查看器 / 只在内存里的数据块没有可打包的源文件，
#: 保存时也不会去读磁盘，不该被当成"缺失文件"。
NON_FILE_SOURCES = ('GENERATED', 'VIEWER', 'MEMORY', 'BUILTIN')

#: 图像数据块里算"外部文件"的来源（与旧行为一致：只有 ``FILE``）。
#: 序列 / 影片来源的图像不能打包（``BKE_packedfile_pack_all()`` 只处理 ``FILE`` / ``TILED``），
#: 也不是"无法打包文件，找不到源路径"的原因；而序列的 ``filepath`` 可能带 ``#`` 占位符，
#: 按字面路径判断存在性会误报，所以一律跳过。
IMAGE_FILE_SOURCE = 'FILE'


def _packable_collections():
    """返回 ``[(类型中文名, 集合), ...]``：五类已知集合 + 其它含 ``filepath`` 的集合。

    "其它"用 ``dir(bpy.data)`` + ``getattr`` + 取第一个元素看有没有 ``filepath`` 来判断，
    所以不同 Blender 版本上集合的增减不会漏扫，也不会为不存在的属性写死代码。
    """
    found = []
    seen = set()
    for attribute, label in PACKABLE_COLLECTIONS:
        collection = getattr(bpy.data, attribute, None)
        if collection is None:
            continue
        found.append((label, collection))
        seen.add(attribute)
    for attribute in dir(bpy.data):
        if attribute.startswith("_") or attribute in seen:
            continue
        if attribute in NON_PACKABLE_COLLECTIONS:
            continue
        collection = getattr(bpy.data, attribute, None)
        if not isinstance(collection, bpy.types.bpy_prop_collection):
            continue
        for item in collection:
            # 空集合判不出来，但空集合本来也没有缺失项要报
            if hasattr(item, "filepath"):
                found.append((attribute, collection))
            break
    return found


def _missing_file_path(block):
    """数据块指向的外部文件路径（不是外部文件 / 读不到就返回 ``""``）。"""
    source = getattr(block, "source", None)
    if isinstance(source, str) and source in NON_FILE_SOURCES:
        return ""
    filepath = getattr(block, "filepath", "")
    if not isinstance(filepath, str) or not filepath:
        return ""
    try:
        return bpy.path.abspath(filepath)
    except Exception:
        # 路径写法太怪（``bpy.path.abspath`` 自己抛异常）也按"缺失"处理：
        # 保存时同样打包不了，不让整个算符中断。
        return filepath


def _is_missing_file_block(block):
    """这个数据块是不是"源文件已不在磁盘、且自己没有打包数据"。

    判据与 Blender 报「无法打包文件，找不到源路径」时一致：
    指向外部文件、自己没有打包数据（``packed_file`` 为空 —— 影片剪辑这类没有该属性的
    数据块按"没打包"处理）、而且磁盘上的源文件确实不在了。
    """
    if isinstance(block, bpy.types.Image) and getattr(block, "source", None) != IMAGE_FILE_SOURCE:
        # 图像只有 ``source == 'FILE'`` 才算外部文件（与旧行为一致）
        return False
    path = _missing_file_path(block)
    if not path:
        return False
    if getattr(block, "packed_file", None) is not None:
        return False
    try:
        return not os.path.exists(path)
    except Exception:
        return True


def _missing_file_blocks():
    """扫描**所有可打包**的数据块，返回 ``(没有使用者的, 仍在使用中的)``。

    旧版只看图像，所以字体 / 声音 / 影片剪辑缺失时保存照样报错。返回结构：

    * 没有使用者的：``[(类型中文名, 集合, 数据块)]`` —— 调用方按集合删除（遍历前已 ``list()``）；
    * 仍在使用的：``[(类型中文名, 名字, 使用者数)]`` —— 绝不删除，只报告。
    """
    unused = []
    in_use = []
    for label, collection in _packable_collections():
        for block in list(collection):
            if not _is_missing_file_block(block):
                continue
            name = getattr(block, "name", "?")
            users = int(getattr(block, "users", 0) or 0)
            if users == 0:
                unused.append((label, collection, block))
            else:
                in_use.append((label, name, users))
    return unused, in_use


#: 状态栏最多列几项（Blender 的状态栏放不下长列表）
STATUS_ITEM_LIMIT = 3


def _describe_items(items, limit=STATUS_ITEM_LIMIT):
    """把 ``(类型, 名字, 使用者数)`` 列表拼成一行；超过 ``limit`` 项时以"等 N 项"收尾。"""
    parts = ["{} {}（{} 使用者）".format(label, name, users) for label, name, users in items[:limit]]
    text = "、".join(parts)
    if len(items) > limit:
        text += " 等 {} 项".format(len(items))
    return text


class HHZ_OT_fix_missing_files(bpy.types.Operator):
    bl_idname = "hhz.fix_missing_files"
    bl_label = "检查并修复缺失文件"
    bl_description = (
        "清理指向已删除文件的数据块（图像 / 声音 / 字体 / 影片剪辑等，只清没有任何使用者的），"
        "并在必要时关闭本文件的自动打包资源"
    )

    def execute(self, context):
        unused, in_use = _missing_file_blocks()
        if not unused and not in_use:
            # 幂等：一个缺失项都没有时，一个字节都不改（use_autopack 也不动）
            self.report({'INFO'}, "没有发现缺失文件，未做任何修改")
            return {'FINISHED'}

        removed = []
        for label, collection, block in unused:
            # 先记名字：remove() 之后这个数据块就失效了，不能再读它的属性
            name = getattr(block, "name", "?")
            try:
                collection.remove(block)
            except Exception as error:
                self.report({'WARNING'}, "无法清理 {} {}：{}".format(label, name, error))
                continue
            removed.append((label, name))

        # 弹窗的直接原因就是「自动打包资源」：文件里既然还有指向已删除文件的数据块，
        # 每次保存都会再去打包它们。这里只在这个文件确实有缺失项时才关掉。
        auto_pack_off = False
        if getattr(bpy.data, "use_autopack", False):
            bpy.data.use_autopack = False
            auto_pack_off = True

        parts = ["发现 {} 个指向已删除文件的数据块".format(len(unused) + len(in_use))]
        if removed:
            parts.append("已清理 {} 个没有使用者的（{}）".format(
                len(removed),
                _describe_items([(label, name, 0) for label, name in removed])))
        if in_use:
            parts.append("仍有 {} 个在使用中，未删除（{}）：可用 文件 ▸ 外部数据 ▸ 查找缺失文件 重新链接".format(
                len(in_use), _describe_items(in_use)))
        if auto_pack_off:
            parts.append("已关闭本文件的自动打包资源，所以保存时不会再弹这个错")

        # 控制台完整列出（状态栏只列前几项）—— 类型 + 名字 + 使用者数
        print("[红红子lab] 缺失文件：共 {} 项".format(len(unused) + len(in_use)))
        for label, name in removed:
            print("  已清理：{} {}（0 使用者）".format(label, name))
        for label, name, users in in_use:
            print("  仍在用：{} {}（{} 使用者）".format(label, name, users))

        self.report({'INFO'}, "；".join(parts))
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

        preferences = compat.get_preferences(context)
        # 饱和度：永远从预设表重算再写入，所以反复调整不会累积变灰
        factor = float(getattr(preferences, "preset_saturation", 1.0)) if preferences else 1.0
        colors = theme.preset_colors(self.preset) or {}
        if factor < 1.0:
            colors = theme.desaturate_colors(colors, factor)
        changed = theme.apply_colors(current, colors)
        if not changed:
            self.report({'WARNING'}, "这套预设没有可用颜色")
            return {'CANCELLED'}

        if preferences is not None:
            preferences.last_preset = self.preset
            theme.read_into_preferences(context, preferences)
        self.report({'INFO'}, "已套用「{}」配色（{} 项）".format(self.preset, changed))
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_theme_reapply_preset(bpy.types.Operator):
    bl_idname = "hhz.theme_reapply_preset"
    bl_label = "按当前饱和度重新套用"
    bl_description = "用现在的饱和度重新套用最近一次用过的预设（从预设表重算，不会累积）"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        current = theme.current_theme(context)
        if preferences is None or current is None:
            self.report({'ERROR'}, "读不到偏好设置或主题")
            return {'CANCELLED'}

        name = getattr(preferences, "last_preset", "") or theme.preset_names()[0]
        colors = theme.preset_colors(name)
        if not colors:
            self.report({'WARNING'}, "没有可重新套用的预设")
            return {'CANCELLED'}

        factor = float(getattr(preferences, "preset_saturation", 1.0))
        colors = theme.desaturate_colors(colors, factor) if factor < 1.0 else colors
        applied = theme.apply_colors(current, colors)
        preferences.last_preset = name
        theme.read_into_preferences(context, preferences)
        self.report({'INFO'}, "已按 {}% 饱和度重新套用「{}」（{} 项）".format(
            int(round(factor * 100)), name, applied))
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_theme_restore_default(bpy.types.Operator):
    bl_idname = "hhz.theme_restore_default"
    bl_label = "恢复颜色默认值"
    bl_description = "把当前颜色、当前分组或全部有默认值的颜色恢复为 Blender 出厂默认"

    scope: bpy.props.EnumProperty(
        name="范围",
        items=(
            ('CURRENT', "当前这一项", "只恢复当前选中的颜色"),
            ('GROUP', "当前分组", "恢复下拉框里当前这一组的全部颜色"),
            ('ALL', "全部（有默认值的项）", "把记录了出厂默认值的颜色全部恢复"),
        ),
        default='CURRENT',
    )

    def execute(self, context):
        current = theme.current_theme(context)
        if current is None:
            self.report({'ERROR'}, "读不到当前主题")
            return {'CANCELLED'}

        preferences = compat.get_preferences(context)
        path = getattr(preferences, "theme_target", "") if preferences else ""
        available = theme.available_paths(context)

        if self.scope == 'GROUP':
            # 分组直接从当前颜色项推导，不再读偏好设置里缓存的副本
            group = theme.group_of(path)
            paths = theme.paths_in_group(group, available)
            if not paths:
                self.report({'WARNING'}, "这一组里没有可恢复的颜色项")
                return {'CANCELLED'}
            changed = theme.restore_defaults(current, paths)
            skipped = len(paths) - changed
            message = "「{}」组：恢复 {} 项".format(group or "?", changed)
            if skipped:
                message += "，{} 项没有记录默认值（跳过）".format(skipped)
            self.report({'INFO' if changed else 'WARNING'}, message)
            if not changed:
                return {'CANCELLED'}
        elif self.scope == 'ALL':
            paths = theme.default_paths()
            changed = theme.restore_defaults(current, paths)
            missing = [item for item in theme.available_paths(context)
                       if not theme.has_default(item)]
            message = "恢复了 {} 项（本版本共记录 {} 项出厂默认值）".format(changed, len(paths))
            if missing:
                message += "，另有 {} 项没有默认值记录".format(len(missing))
            self.report({'INFO' if changed else 'WARNING'}, message)
            if not changed:
                return {'CANCELLED'}
        else:
            if not theme.has_default(path):
                self.report({'WARNING'}, "这一项没有记录默认值，无法一键恢复")
                return {'CANCELLED'}
            before = theme.get_color(current, path)
            if not theme.restore_default(current, path):
                self.report({'WARNING'}, "恢复失败")
                return {'CANCELLED'}
            after = theme.get_color(current, path)
            if before is not None and after is not None and theme.color_close(before, after):
                self.report({'INFO'}, "这一项本来就是默认值（没有变化）")
            else:
                self.report({'INFO'}, "已恢复这一项的默认值")

        preferences = compat.get_preferences(context)
        if preferences is not None:
            theme.read_into_preferences(context, preferences)
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_theme_apply_color(bpy.types.Operator):
    bl_idname = "hhz.theme_apply_color"
    bl_label = "应用颜色"
    bl_description = "把调色盘里选的颜色（或十六进制框里的值）应用到当前颜色项"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        current = theme.current_theme(context)
        if preferences is None or current is None:
            self.report({'ERROR'}, "读不到偏好设置或主题")
            return {'CANCELLED'}

        path = getattr(preferences, "theme_target", "")
        if theme.resolve(current, path) is None:
            self.report({'WARNING'}, "当前颜色项在这个 Blender 版本上不存在")
            return {'CANCELLED'}

        color = None
        hex_text = (getattr(preferences, "custom_hex", "") or "").strip()
        if hex_text:
            color = theme.hex_color(hex_text)
            if color is None:
                self.report({'WARNING'}, "十六进制写法不对，像 #C6FF00 这样写")
                return {'CANCELLED'}
        if color is None:
            color = tuple(getattr(preferences, "palette_color", (1.0, 1.0, 1.0, 1.0)))

        if not theme.set_color(current, path, color):
            self.report({'WARNING'}, "应用失败")
            return {'CANCELLED'}

        applied = theme.get_color(current, path) or color
        preferences.theme_color = applied
        self.report({'INFO'}, "已应用 {}（{}）".format(theme.color_to_hex(applied), path))
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_theme_part_load(bpy.types.Operator):
    bl_idname = "hhz.theme_part_load"
    bl_label = "从当前主题读回"
    bl_description = "把当前控件家族的各项颜色重新读进面板（在别处改过主题后同步一下）"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        if preferences is None:
            self.report({'ERROR'}, "读不到偏好设置")
            return {'CANCELLED'}
        filled = ui.load_family_parts(context, preferences)
        if not filled:
            self.report({'WARNING'}, "这个家族在当前版本上没有可用颜色")
            return {'CANCELLED'}
        self.report({'INFO'}, "已读入 {} 项颜色".format(filled))
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_theme_auto_contrast(bpy.types.Operator):
    bl_idname = "hhz.theme_auto_contrast"
    bl_label = "自动配文字色"
    bl_description = "按底色亮度自动挑看得清的文字色（常态与选中各配一次），解决字/图标看不清"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        current = theme.current_theme(context)
        if preferences is None or current is None:
            self.report({'ERROR'}, "读不到偏好设置或主题")
            return {'CANCELLED'}

        families = ui.family_keys(preferences)
        if not families:
            self.report({'WARNING'}, "先在上面选一个控件家族或分组")
            return {'CANCELLED'}

        # 1) 先算出每个家族该用的文字色，并把面板上的值更新到位
        plan = []
        for prefix in families:
            for background_key, text_key in (("inner", "text"), ("inner_sel", "text_sel")):
                background = theme.get_color(current, theme.family_path(prefix, background_key))
                if background is None:
                    continue
                plan.append((prefix, text_key, background))
                try:
                    setattr(preferences, "part_" + text_key,
                            theme.readable_text_color(background))
                except Exception:
                    pass

        # 2) 面板值都定下来之后，最后一次性只写主题：自检不达标就换另一种字色。
        #    这一段中间不碰偏好设置，避免回调再把旧值写回来。
        changed = 0
        for prefix, text_key, background in plan:
            for text_color in (theme.readable_text_color(background),
                               theme.TEXT_ON_LIGHT, theme.TEXT_ON_DARK):
                if not theme.write_family(current, prefix, text_key, text_color):
                    continue
                written = theme.get_color(current, theme.family_path(prefix, text_key))
                if (written is not None
                        and abs(theme.luminance(written) - theme.luminance(background)) >= 0.3):
                    changed += 1
                    break

        if not changed:
            self.report({'WARNING'}, "这个家族没有可配的底色项")
            return {'CANCELLED'}
        self.report({'INFO'}, "已按底色亮度自动配好文字色（{} 项）".format(changed))
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_theme_apply_global(bpy.types.Operator):
    bl_idname = "hhz.theme_apply_global"
    bl_label = "应用到全部按钮"
    bl_description = "把全局按钮配色写到所有控件家族与各编辑器的选中高亮上（含预选中项）"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        current = theme.current_theme(context)
        if preferences is None or current is None:
            self.report({'ERROR'}, "读不到偏好设置或主题")
            return {'CANCELLED'}

        base = tuple(getattr(preferences, "global_base", (0.09, 0.10, 0.07, 1.0)))
        accent = tuple(getattr(preferences, "global_accent", (0.776, 1.0, 0.0, 1.0)))
        auto_text = bool(getattr(preferences, "global_auto_text", True))
        text = None if auto_text else tuple(
            getattr(preferences, "global_text", (0.94, 0.96, 0.90, 1.0)))

        colors = theme.global_button_colors(base, accent, text=text, auto_text=auto_text)
        applied = theme.apply_colors(current, colors)
        if not applied:
            self.report({'WARNING'}, "没有可写入的颜色项")
            return {'CANCELLED'}

        inner = theme.get_color(current, theme.family_path("wcol_regular", "inner"))
        if inner is not None:
            preferences.theme_color = inner
        self.report({'INFO'}, "已全局应用 {} 项（{} 个控件家族 + 各编辑器选中高亮）".format(
            applied, len(theme.WIDGET_FAMILIES)))
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_theme_custom_save(bpy.types.Operator):
    bl_idname = "hhz.theme_custom_save"
    bl_label = "保存为自定义配色"
    bl_description = "把当前所有可用颜色存成一套自定义配色（同名会覆盖）"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        current = theme.current_theme(context)
        if preferences is None or current is None:
            self.report({'ERROR'}, "读不到偏好设置或主题")
            return {'CANCELLED'}

        name = getattr(preferences, "custom_name", "").strip()
        if not name:
            self.report({'WARNING'}, "先给这套配色起个名字")
            return {'CANCELLED'}

        colors = theme.capture_colors(current, theme.available_paths(context))
        text, ok = theme.custom_save(getattr(preferences, "theme_custom", ""), name, colors)
        if not ok:
            self.report({'WARNING'}, "保存失败（名字或颜色为空）")
            return {'CANCELLED'}

        preferences.theme_custom = text
        preferences.custom_choice = name
        self.report({'INFO'}, "已保存自定义配色「{}」（{} 项颜色）".format(name, len(colors)))
        return {'FINISHED'}


class HHZ_OT_theme_custom_apply(bpy.types.Operator):
    bl_idname = "hhz.theme_custom_apply"
    bl_label = "套用自定义配色"
    bl_description = "把选中的自定义配色写进当前主题"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        current = theme.current_theme(context)
        if preferences is None or current is None:
            self.report({'ERROR'}, "读不到偏好设置或主题")
            return {'CANCELLED'}

        name = getattr(preferences, "custom_choice", "")
        colors = theme.custom_colors(getattr(preferences, "theme_custom", ""), name)
        if not colors:
            self.report({'WARNING'}, "没有这套自定义配色")
            return {'CANCELLED'}

        applied = 0
        skipped = 0
        for path, color in colors.items():
            if theme.resolve(current, path) is None:
                skipped += 1
                continue
            if theme.set_color(current, path, color):
                applied += 1
            else:
                skipped += 1
        theme.read_into_preferences(context, preferences)
        self.report({'INFO'}, "已套用「{}」：{} 项生效{}".format(
            name, applied, "，{} 项当前版本没有（跳过）".format(skipped) if skipped else ""))
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_theme_custom_delete(bpy.types.Operator):
    bl_idname = "hhz.theme_custom_delete"
    bl_label = "删除这套配色"
    bl_description = "把选中的自定义配色从偏好设置里删掉"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        if preferences is None:
            self.report({'ERROR'}, "读不到偏好设置")
            return {'CANCELLED'}
        name = getattr(preferences, "custom_choice", "")
        text, ok = theme.custom_delete(getattr(preferences, "theme_custom", ""), name)
        if not ok:
            self.report({'WARNING'}, "没有这套自定义配色")
            return {'CANCELLED'}
        preferences.theme_custom = text
        self.report({'INFO'}, "已删除「{}」".format(name))
        return {'FINISHED'}


class HHZ_OT_font_pick(bpy.types.Operator):
    bl_idname = "hhz.font_pick"
    bl_label = "选择字体文件"
    bl_description = "选一个 .ttf / .otf / .ttc 作为 Blender 的界面字体"

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.ttf;*.otf;*.ttc", options={'HIDDEN'})
    mono: bpy.props.BoolProperty(
        name="等宽字体",
        description="选中的是等宽字体（给文本编辑器用）",
        default=False,
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        key = "font_path_ui_mono" if self.mono else "font_path_ui"
        if not self.filepath:
            self.report({'WARNING'}, "没有选文件")
            return {'CANCELLED'}
        if not theme.font_set(context, key, self.filepath):
            self.report({'WARNING'}, "这个 Blender 版本没有这项字体设置")
            return {'CANCELLED'}
        self.report({'INFO'}, "字体已设为 {}".format(self.filepath))
        return {'FINISHED'}


class HHZ_OT_font_restore(bpy.types.Operator):
    bl_idname = "hhz.font_restore"
    bl_label = "字体恢复默认"
    bl_description = "把界面缩放、DPI、自定义字体、抗锯齿等恢复成 Blender 出厂值"

    def execute(self, context):
        restored = theme.font_restore(context)
        if not restored:
            self.report({'WARNING'}, "没有可恢复的字体设置")
            return {'CANCELLED'}
        self.report({'INFO'}, "已恢复 {} 项字体 / 缩放设置".format(restored))
        compat.tag_redraw(context)
        return {'FINISHED'}


class HHZ_OT_layout_save(bpy.types.Operator):
    bl_idname = "hhz.layout_save"
    bl_label = "保存当前布局"
    bl_description = "把当前的工作区与区域划分存成一份带名字的布局快照"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        if preferences is None:
            self.report({'ERROR'}, "读不到偏好设置")
            return {'CANCELLED'}

        name = getattr(preferences, "layout_name", "").strip()
        if not name:
            self.report({'WARNING'}, "先给这套布局起个名字")
            return {'CANCELLED'}

        snapshot = layout.capture(context)
        if not snapshot:
            self.report({'WARNING'}, "后台模式没有窗口，抓不到布局")
            return {'CANCELLED'}
        if not snapshot.get("workspaces"):
            self.report({'WARNING'}, "没读到工作区")
            return {'CANCELLED'}

        text, ok = layout.save(getattr(preferences, "layout_slots", ""), name, snapshot)
        if not ok:
            self.report({'WARNING'}, "保存失败")
            return {'CANCELLED'}

        preferences.layout_slots = text
        preferences.layout_choice = name
        areas = sum(len(item.get("areas") or ()) for item in snapshot["workspaces"])
        self.report({'INFO'}, "已保存布局「{}」：{} 个工作区 / {} 个区域".format(
            name, len(snapshot["workspaces"]), areas))
        return {'FINISHED'}


class HHZ_OT_layout_apply(bpy.types.Operator):
    bl_idname = "hhz.layout_apply"
    bl_label = "使用这套布局"
    bl_description = "把选中的布局套到当前文件（缺的工作区会补出来）"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        if preferences is None:
            self.report({'ERROR'}, "读不到偏好设置")
            return {'CANCELLED'}

        name = getattr(preferences, "layout_choice", "")
        slots = layout.load(getattr(preferences, "layout_slots", ""))
        snapshot = slots.get(name)
        if not snapshot:
            self.report({'WARNING'}, "没有这套布局")
            return {'CANCELLED'}

        message, ok = layout.apply(context, snapshot)
        self.report({'INFO' if ok else 'WARNING'}, message)
        compat.tag_redraw(context)
        return {'FINISHED'} if ok else {'CANCELLED'}


class HHZ_OT_layout_delete(bpy.types.Operator):
    bl_idname = "hhz.layout_delete"
    bl_label = "删除这套布局"
    bl_description = "把选中的布局从偏好设置里删掉"

    def execute(self, context):
        preferences = compat.get_preferences(context)
        if preferences is None:
            self.report({'ERROR'}, "读不到偏好设置")
            return {'CANCELLED'}
        name = getattr(preferences, "layout_choice", "")
        text, ok = layout.delete(getattr(preferences, "layout_slots", ""), name)
        if not ok:
            self.report({'WARNING'}, "没有这套布局")
            return {'CANCELLED'}
        preferences.layout_slots = text
        self.report({'INFO'}, "已删除布局「{}」".format(name))
        return {'FINISHED'}


class HHZ_OT_layout_save_startup(bpy.types.Operator):
    bl_idname = "hhz.layout_save_startup"
    bl_label = "存为启动布局（新建文件就用它）"
    bl_description = "把当前界面存成 Blender 启动文件；以后新建文件就是这个布局"

    confirm: bpy.props.BoolProperty(
        name="确认覆盖启动文件",
        description="会覆盖 Blender 的启动文件（startup.blend）",
        default=False,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        # 只提示、不阻止：缺失引用会被一起写进启动文件，于是**每个新建文件**都带着它、
        # 保存时继续报"无法打包文件"。行为保持原样（用户可能就是要存），只多一句警告。
        unused, in_use = _missing_file_blocks()
        missing = [(label, getattr(block, "name", "?"), 0)
                   for label, _collection, block in unused]
        missing += in_use
        message, ok = layout.save_startup(context)
        self.report({'INFO' if ok else 'ERROR'}, message)
        if ok and missing:
            self.report({'WARNING'},
                        "当前文件有 {} 个缺失引用，已一并写入启动文件（{}）；"
                        "建议先点「检查并修复缺失文件」再保存".format(
                            len(missing), _describe_items(missing, 2)))
        return {'FINISHED'} if ok else {'CANCELLED'}


class HHZ_OT_theme_save_preferences(bpy.types.Operator):
    bl_idname = "hhz.theme_save_preferences"
    bl_label = "保存到偏好设置"
    bl_description = "把主题颜色写进 Blender 偏好设置（不保存的话重启后会还原）"

    def execute(self, context):
        try:
            # 算子名是**单数** ``wm.save_userpref``：实测 4.0.2 / 4.2.23 / 5.2.2 三版
            # 都只有这个名字，多带一个复数 s 的那个名字不存在，调用直接 AttributeError。
            bpy.ops.wm.save_userpref()
        except Exception as error:
            # 内层失败必须报错并取消，不能吞掉后照样返回 FINISHED（那样看起来一直是好的）
            self.report({'ERROR'}, "保存失败：{}".format(error))
            return {'CANCELLED'}
        message = "主题颜色已保存到偏好设置"
        preferences = getattr(context, "preferences", None)
        if preferences is not None and not getattr(preferences, "use_preferences_save", True):
            # 没开"自动保存偏好设置"也不影响这里：刚才是显式写盘
            message += "（未开启自动保存偏好设置，退出 Blender 时不会再覆盖）"
        self.report({'INFO'}, message)
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
            self._set_status(context, "拖动中：上下移动鼠标换位 松开确认 Esc 取消")
        else:
            self._set_status(context, "拖动中：上下移动鼠标换位 再点一下确认 Esc 取消")
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
