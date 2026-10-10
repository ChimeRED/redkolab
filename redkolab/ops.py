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
        # 视口尺寸（不是颜色）：两套马拉松预设会把编辑模式选中边画粗一档
        # —— 颜色已经拉到调色板最高饱和度，剩下的可见性只能靠线宽。
        changed_sizes = theme.apply_sizes(current, theme.preset_sizes(self.preset))
        if not changed:
            self.report({'WARNING'}, "这套预设没有可用颜色")
            return {'CANCELLED'}

        if preferences is not None:
            preferences.last_preset = self.preset
            theme.read_into_preferences(context, preferences)
        message = "已套用「{}」配色（{} 项）".format(self.preset, changed)
        if changed_sizes:
            message += "；视口尺寸 {} 项".format(changed_sizes)
        self.report({'INFO'}, message)
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
        # 视口尺寸与颜色一样"从预设表重算"，所以重复点击同样不累积
        theme.apply_sizes(current, theme.preset_sizes(name))
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
            # 按运行版本过滤：本版本没有这一项（例如 4.5 删掉的 info.info_error）
            # 不算"恢复失败"，单独计数、跳过
            absent = theme.absent_default_paths(current, paths)
            changed = theme.restore_defaults(current, paths)
            skipped = len(paths) - changed - len(absent)
            message = "「{}」组：恢复 {} 项".format(group or "?", changed)
            if absent:
                message += "，{} 项在当前版本不存在（跳过）".format(len(absent))
            if skipped > 0:
                message += "，{} 项没有记录默认值（跳过）".format(skipped)
            self.report({'INFO' if changed else 'WARNING'}, message)
            if not changed:
                return {'CANCELLED'}
        elif self.scope == 'ALL':
            paths = theme.default_paths()
            # 恢复逻辑与核对逻辑都按运行版本过滤（4.5 上 info.info_error /
            # info.info_warning 已被 Blender 删掉，表里留着它们是为 4.2 服务）
            absent = theme.absent_default_paths(current, paths)
            changed = theme.restore_defaults(current, paths)
            # 视口尺寸（不是颜色）：两套马拉松预设会把它写粗，这里一并写回默认
            sizes_changed = theme.restore_size_defaults(current)
            missing = [item for item in theme.available_paths(context)
                       if not theme.has_default(item)]
            message = "恢复了 {} 项（本版本共记录 {} 项出厂默认值，其中 {} 项在当前版本不存在已跳过）".format(
                changed, len(paths), len(absent))
            if sizes_changed:
                message += "，另外把 {} 项视口尺寸写回默认".format(sizes_changed)
            if missing:
                message += "，另有 {} 项没有默认值记录".format(len(missing))
            self.report({'INFO' if changed or sizes_changed else 'WARNING'}, message)
            if not changed and not sizes_changed:
                return {'CANCELLED'}
        else:
            if not theme.has_default(path):
                self.report({'WARNING'}, "这一项没有记录默认值，无法一键恢复")
                return {'CANCELLED'}
            if theme.resolve(current, path) is None:
                self.report({'WARNING'},
                            "这一项在当前 Blender 版本上不存在，无法恢复（{}）".format(path))
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


#: 只放工作区布局的临时布局库：放在 Blender 用户配置目录里，**不写进用户工程目录**。
LAYOUT_LIBRARY_NAME = "redkolab_startup_layout.blend"

# 追加工作区**不能**用 ``bpy.ops.wm.append``：它的 poll 是 ``wm_link_append_poll()``
# （4.0.2 / 4.2.23 / 5.2.2 三版一致），要求 ``WM_operator_winactive(C)``。
# ``read_homefile()`` 会把当前窗口/屏幕重建，切换文件之后那个 context 已经失效 ——
# 于是真实界面里点这个按钮必然报
# 「bpy.ops.wm.append.poll() failed, context is incorrect」（用户实测，无头环境里恰好不触发）。
# 改用纯数据 API ``bpy.data.libraries.load()``：不经过算子、没有 poll、不需要窗口，
# 无头与界面两种环境都一样能用（测试里有真机断言守着）。


def layout_library_path():
    """布局库路径（Blender 用户配置目录下的固定名字）。"""
    config = bpy.utils.user_resource('CONFIG') or bpy.app.tempdir
    return os.path.join(config, LAYOUT_LIBRARY_NAME)


# --------------------------------------------------- 工作区重名收尾（启动布局用）
#
# 中文界面下**出厂启动文件里也有**「布局 / 建模 / 着色 / UV 编辑」这同名的 11 个工作区，
# 于是 ``bpy.data.libraries.load()`` 把用户的工作区追加进来时撞名，Blender 给**追加
# 进来的那份**加 ``.001`` 后缀 —— 用户看到的现象是"新建场景不是我的布局"。
#
# 收尾的目标：让用户的每个工作区都拿回**原来的名字**，且不再有 ``X`` / ``X.<数字>``
# 这种重名对。

#: 重名收尾时给出厂那份工作区加的前缀（脚本删不掉它，就改名让位）。
FACTORY_WORKSPACE_PREFIX = "出厂 "

#: 工作区名字上限（Blender 的 ``MAX_ID_NAME - 2``）。
WORKSPACE_NAME_LIMIT = 63

# ------------------------------------------------- 幂等：只补「启动文件里还没有的」
#
# 旧流程每次都 `read_homefile(use_factory_startup=True)` **从出厂启动文件重新开始**，
# 再把用户的工作区无脑追加一遍。中文界面下出厂启动文件里也有「布局 / 建模 / 雕刻 / ...」
# 这同名的自带工作区，于是必然撞名、必然被改名成「出厂 xxx」——
# 用户点两次就得到 22 个工作区（11 个自己的 + 11 个「出厂 xxx」），点得越多越乱。
#
# 新流程的两个支点：
#   1. 读**上次保存的启动文件**（`read_homefile()`，不带 use_factory_startup），
#      第二次运行时看到的就是上一次的结果；
#   2. 追加之前按「同名 **且** 区域构成相同」判断，已经有的直接跳过。


def _workspace_composition(workspace):
    """工作区的**区域构成**：``(屏幕数, 区域 type 的多重集合)``。

    ``sorted(区域 type 列表)`` 就是多重集合的表示（同一种区域有两个就算两个）。
    同名但构成不同（比如用户改过区域划分）算**不同**的工作区，要各留一份。
    """
    areas = []
    screens = list(getattr(workspace, "screens", []) or [])
    for screen in screens:
        for area in getattr(screen, "areas", []) or []:
            try:
                areas.append(area.type)
            except Exception:
                pass
    return (len(screens), tuple(sorted(areas)))


def workspace_signatures():
    """当前文件里 ``工作区名 -> 区域构成`` 的快照（切文件**之前**记录，当幂等判据用）。"""
    return {workspace.name: _workspace_composition(workspace)
            for workspace in bpy.data.workspaces}


def _workspace_already_present(name, signature):
    """启动文件里是否已经有「同名**且**区域构成相同」的工作区。

    读不到本次构成（``signature is None``）时按「已经有」处理 —— 宁可少追加一次，
    也不要在判据不明的时候重复追加出一堆 ``.001``。
    """
    existing = bpy.data.workspaces.get(name)
    if existing is None:
        return False
    if signature is None:
        return True
    return _workspace_composition(existing) == signature


def _plan_workspace_appends(available, signatures):
    """把布局库里可用的工作区分成「要追加的」和「已经有的」。

    返回 ``(要追加的名字清单, 跳过的名字清单)``；跳过的就是幂等的部分 ——
    第二次运行时全部落在跳过里，一个都不追加。
    """
    wanted = []
    skipped = []
    for name in available:
        if _workspace_already_present(name, signatures.get(name)):
            skipped.append(name)
        else:
            wanted.append(name)
    return wanted, skipped


def _empty_scene_data():
    """把当前会话清成空场景（启动文件里只留工作区布局），返回清完后的物体数量。

    纯数据 API：``list()`` 之后逐个 ``bpy.data.objects.remove()``，再用
    ``orphans_purge`` 清掉因此变成孤儿的网格 / 材质 / 世界等。
    **不碰工作区** —— 实测（4.0.2 沙箱）orphans_purge 不会删掉它们
    （工作区被窗口引用着，不是孤儿）。
    """
    for obj in list(bpy.data.objects):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
    try:
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    except Exception:
        # 个别版本参数不同：退化成无参调用，清不掉也不算致命（后面还有物体数校验）
        try:
            bpy.data.orphans_purge()
        except Exception:
            pass
    return len(bpy.data.objects)


def startup_report_parts(appended, skipped, leftover_factory):
    """「设为启动布局」的结果文案（单独成函数，真机测试直接断言文案如实）。

    必须说清四件事：本次**新增了几个**、**跳过了几个**（因为启动文件里已经有了）、
    启动文件仍然是**空场景**、以及启动文件里还留着的「出厂 xxx」怎么办
    （脚本删不掉工作区，只能在工作区标签上右键删）。
    """
    parts = ["启动布局已更新"]
    if appended:
        parts.append("本次新增 {} 个工作区".format(len(appended)))
    else:
        parts.append("本次新增 0 个工作区（要补的工作区启动文件里都已经有了）")
    if skipped:
        parts.append("跳过 {} 个已存在的（同名且区域构成相同，不再重复追加）".format(
            len(skipped)))
    parts.append("启动文件仍是空场景 + 你的工作区布局（不含当前场景）")
    if leftover_factory:
        parts.append("启动文件里还留着 {} 个带「出厂」前缀的工作区，"
                     "本版 Blender 的脚本接口删不掉工作区："
                     "要清掉它们，在工作区标签上点右键 ▸ 删除工作区".format(
                         len(leftover_factory)))
    return parts


#: 最近一次「把当前窗口布局设为启动布局」的内部计数。
#: **不是设置项 / 不是新按钮 / 不是偏好设置**（这些东西一个都没加），只是把这次运行
#: 新增了几个、跳过了几个、启动文件里最后有几个工作区留个痕，好让真机测试直接断言
#: 「连点两次，第二次新增 0 个」—— 不用靠间接推断。
LAST_STARTUP_LAYOUT_RESULT = {}


def _workspace_collision_pairs():
    """当前文件里「``X`` 与 ``X.<数字>`` 同时存在」的工作区对。

    返回 ``[(X 那件, X.<数字> 那件), ...]``。**只有成对的才算** ——
    名字带 ``.数字`` 却没有同名兄弟的（用户自己就这么命名的）一律不碰。
    """
    by_name = {}
    for workspace in bpy.data.workspaces:
        by_name[workspace.name] = workspace
    pairs = []
    for name in sorted(by_name):
        base, dot, suffix = name.partition(".")
        if not dot or not suffix.isdigit():
            continue
        plain = by_name.get(base)
        if plain is None or plain is by_name[name]:
            continue
        pairs.append((plain, by_name[name]))
    return pairs


def _active_workspace_of_windows():
    """每个窗口当前的工作区（读不到就是 None）。"""
    result = []
    for window_manager in bpy.data.window_managers:
        for window in window_manager.windows:
            try:
                result.append(window.workspace)
            except Exception:
                result.append(None)
    return result


def _workspace_is_current(workspace):
    """``workspace`` 是不是**每个**窗口当前的工作区。

    删除算符删的是"当前工作区"，所以只有这个条件成立才敢调用它 —— 否则它可能删掉
    别的工作区（甚至用户自己那份）。
    """
    active = _active_workspace_of_windows()
    return bool(active) and all(item is workspace for item in active)


def _make_workspace_current(workspace):
    """尽力把某个工作区设为当前，并**核实**是否真的成了（返回布尔）。"""
    for window_manager in bpy.data.window_managers:
        for window in window_manager.windows:
            try:
                window.workspace = workspace
            except Exception:
                pass
    return _workspace_is_current(workspace)


def _unique_workspace_name(base):
    """给 ``base`` 找一个当前没被占用的工作区名（超长自动截断）。"""
    taken = set(workspace.name for workspace in bpy.data.workspaces)
    candidate = base[:WORKSPACE_NAME_LIMIT]
    index = 2
    while candidate in taken:
        suffix = " ({})".format(index)
        candidate = base[:WORKSPACE_NAME_LIMIT - len(suffix)] + suffix
        index += 1
    return candidate


def _rename_factory_workspace(workspace):
    """把出厂那份重名工作区改成带前缀的名字，把原名让出来。

    返回新名字；改名失败返回空串（调用方据此中止，不写启动文件）。
    """
    try:
        workspace.name = _unique_workspace_name(
            FACTORY_WORKSPACE_PREFIX + workspace.name)
    except Exception:
        return ""
    return workspace.name


def _delete_workspace(workspace):
    """试着用工作区删除算符删掉 ``workspace``，返回**是否真的删掉了**。

    两道联锁 + 结果核对：

    1. 只有确认目标就是当前工作区才调用删除算符（它删的是"当前工作区"，
       不能让它删到别的工作区上去）；
    2. 检查返回值，**且**核对实际效果 —— 工作区对象真的没了、数量真的少了。

    为什么要核对效果而不只看返回值：本机实测它**返回值不可信**。4.0.2 上
    ``--background`` 与真实界面里，``bpy.ops.workspace.delete()`` 都出现过
    "返回 ``{'FINISHED}`` 却一个都没删"；另一种实测（用户真实环境）是
    它抛 ``SystemError: GPU API is not available in background mode``、
    算子不抛异常、静默返回 ``CANCELLED``。所以判据只能是"东西真的没了"。

    另外两条实测（都不能当替代方案）：
    * ``bpy.data.workspaces.remove`` / ``.new`` 根本不存在；
    * ``bpy.data.batch_remove`` 能删，但删的若是窗口当前那个，
      ``window.workspace`` 变成 None，Blender **退出时段错误**
      （实测 exit=11，栈顶 ``ED_screen_exit``）—— 所以绝不能用它。
    """
    before = len(bpy.data.workspaces)
    if not _make_workspace_current(workspace):
        return False
    try:
        result = bpy.ops.workspace.delete()
    except Exception:
        # 无头（--background）下实测会抛 SystemError: GPU API is not available，
        # 也可能静默返回 CANCELLED —— 两种情况都当"没删掉"处理。
        return False
    if result != {'FINISHED'}:
        return False
    for item in bpy.data.workspaces:
        if item is workspace:
            return False
    return len(bpy.data.workspaces) < before


def _dedupe_report_parts(collisions, removed, renamed, collisions_left, problems):
    """重名收尾的报告文案（单独成函数，真机测试可以直接断言文案如实）。

    必须说清三件事：收尾做了什么、**没能做什么**（脚本删不掉工作区）、
    以及可执行的替代路径（界面里右键删除）。
    """
    parts = []
    if not collisions:
        return parts
    parts.append("重名收尾：{} 对同名工作区 —— 删掉 {} 个出厂重名、"
                 "给出厂重名加「出厂」前缀改名 {} 个、"
                 "你的 {} 个工作区拿回了原来的名字".format(
                     collisions, removed, renamed, renamed))
    if not removed:
        # 如实说明限制：删除算符在本机实测里删不掉工作区
        # （一种环境返回 FINISHED 却什么都没删，另一种抛 GPU 不可用后静默返回
        #  CANCELLED），所以出厂那批只能改名让位。
        parts.append("限制：本版 Blender 的脚本接口删不掉工作区，"
                     "所以启动文件里仍保留了 {} 个出厂工作区"
                     "（名字前面加了「出厂」）；"
                     "要清掉它们：在工作区标签上点右键 ▸ 删除工作区".format(renamed))
    if collisions_left:
        parts.append("注意：还有 {} 对同名工作区没能收尾（{}）".format(
            len(collisions_left),
            "；".join(problems) if problems else "原因不明"))
    elif problems:
        parts.append("注意：{}".format("；".join(problems)))
    return parts


def _dedupe_appended_workspaces():
    """重名收尾，返回 ``(删除数, 改名数, 剩余重名对, 问题说明清单)``。

    对每一对 ``X`` / ``X.<数字>``：

    1. 先把出厂那份 ``X`` 设为当前工作区，能设成、且删除算符**真的删掉了**才算删掉；
    2. 删不掉（本机实测就是删不掉，见 :func:`_delete_workspace`）就**改名兜底**：
       给出厂那份加 :data:`FACTORY_WORKSPACE_PREFIX` 前缀，把干净的原名让出来；
    3. 再把用户那份 ``X.<数字>`` 改名回 ``X``；这一步若没成，就把第 2 步**回滚**，
       避免留下"两边都不是原名"的中间态。

    不成对的名字一律不碰。某一对出问题**只跳过这一对**、继续处理其它对，
    问题说明逐条返回给调用方（由调用方写进报告）—— 不因为一对失败就让整个
    功能停摆（用户最反感的是"按钮点了没反应"）。
    """
    pairs = _workspace_collision_pairs()
    removed = 0
    renamed = 0
    problems = []
    for plain, suffixed in pairs:
        target_name = plain.name
        factory_name = ""
        if _delete_workspace(plain):
            removed += 1
        else:
            factory_name = _rename_factory_workspace(plain)
            if not factory_name:
                problems.append("出厂工作区「{}」改名失败，这一对跳过".format(target_name))
                continue
        outcome = ""
        try:
            suffixed.name = target_name
        except Exception as error:
            outcome = "改名回「{}」失败：{}".format(target_name, error)
        else:
            if suffixed.name != target_name:
                outcome = "改名回「{}」没有生效".format(target_name)
        if outcome:
            if factory_name:
                try:
                    plain.name = target_name
                except Exception:
                    pass
            problems.append("{}（这一对跳过）".format(outcome))
            continue
        renamed += 1
    return removed, renamed, _workspace_collision_pairs(), problems


class HHZ_OT_layout_save_startup(bpy.types.Operator):
    bl_idname = "hhz.layout_save_startup"
    bl_label = "把当前窗口布局设为启动布局"
    bl_description = (
        "把现在的工作区 / 窗口布局设成 Blender 启动布局：启动文件会重建成"
        "「空场景 + 你这套工作区」，当前场景不会被带进启动文件。"
        "想连场景一起存，请用 Blender 原生的「文件 默认 保存启动文件」"
    )

    confirm: bpy.props.BoolProperty(
        name="确认重建启动文件",
        description="会覆盖 Blender 启动文件（startup.blend）：新的启动文件是空场景 + 当前工作区布局，不含当前场景",
        default=False,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        original_path = bpy.data.filepath
        # 硬要求：有未保存改动时绝不继续 —— 下面会切换文件，可能丢改动。
        # 例外：**无头环境**（--background）里 4.x 的 ``is_dirty`` 实测恒为 True
        # （刚启动、保存之后、载入出厂文件之后都是 True，见测试断言），那是"全局状态脏"
        # 的假报警；这时只要文件**已经落盘**（有 filepath，随时能用 文件 打开 回来），
        # 继续是安全的。没有 filepath 时仍然拒绝：那种情况真的没法恢复。
        if getattr(bpy.data, "is_dirty", False) and not (bpy.app.background and original_path):
            self.report({'WARNING'}, "请先保存当前文件（Ctrl+S），再设置启动布局")
            return {'CANCELLED'}

        workspace_names = [workspace.name for workspace in bpy.data.workspaces]
        if not workspace_names:
            self.report({'ERROR'}, "当前文件里没有工作区，无法保存布局")
            return {'CANCELLED'}
        # 幂等判据：切文件**之前**记下每个工作区的区域构成（构成只在这里读得到，
        # 切到启动文件之后当前文件的工作区就不在了）。
        signatures = workspace_signatures()
        active_name = ""
        for window_manager in bpy.data.window_managers:
            for window in window_manager.windows:
                if window.workspace is not None:
                    active_name = window.workspace.name
                    break
            if active_name:
                break

        # 顺手提示当前文件里的坏引用：它们不再被写进启动文件（新流程只存工作区），
        # 但会让**这个文件**每次保存都弹「无法打包文件」。
        unused, in_use = _missing_file_blocks()
        missing = [(label, getattr(block, "name", "?"), 0)
                   for label, _collection, block in unused]
        missing += in_use

        library = layout_library_path()
        try:
            bpy.data.libraries.write(library, set(bpy.data.workspaces), fake_user=True)
        except Exception as error:
            self.report({'ERROR'}, "写入布局库失败：{}".format(error))
            return {'CANCELLED'}

        def restore_original(reason):
            """失败之后尽量把会话恢复到用户原来的文件，并给出如实的报告。"""
            if not original_path:
                self.report({'ERROR'}, "{}；当前会话停在空场景上，"
                                       "你原来的文件没有保存过，不会被改".format(reason))
                return {'CANCELLED'}
            try:
                bpy.ops.wm.open_mainfile(filepath=original_path)
            except Exception as restore_error:
                self.report({'ERROR'},
                            "{}；而且自动恢复会话也失败了（{}），"
                            "请用 文件 打开 回到 {}".format(reason, restore_error, original_path))
                return {'CANCELLED'}
            self.report({'ERROR'}, "{}；会话已恢复到原来的文件（{}）".format(
                reason, os.path.basename(original_path)))
            return {'CANCELLED'}

        try:
            # 读**上次保存的启动文件**（用户目录里的 startup.blend），**不带**
            # ``use_factory_startup=True``。这是本次幂等修复的核心：出厂启动文件里必然
            # 有一整套自带工作区，每次从它重来就必然与用户的工作区撞名、必然改名让位，
            # 于是每点一次按钮就多一批「出厂 xxx」；读用户启动文件之后，第二次运行时
            # 看到的就是第一次的结果，同名同构成会被下面的 _plan_workspace_appends 跳过。
            #
            # 实测（4.0.2 沙箱）：命令行带着 ``--factory-startup`` 也**不影响**这一次
            # 读取 —— 加载的确实是用户目录里的 startup.blend（探针里只可能来自用户启动
            # 文件的标记存活、出厂工作区不在），所以真机测试与真实界面行为一致。
            # 它也不会重置用户偏好设置（那是 read_factory_settings 才干的事）。
            bpy.ops.wm.read_homefile()
        except Exception as error:
            return restore_original("载入启动文件失败：{}".format(error))

        # 启动文件只留工作区布局：把场景数据清空。
        # 此刻**磁盘上的启动文件还没动**（只在最后 save_homefile 时落盘），
        # 用户原来的文件也还没动（最后会重新打开）。
        objects_left = _empty_scene_data()
        if objects_left:
            return restore_original(
                "清空场景失败（还剩 {} 个物体），已中止、没有写启动文件".format(
                    objects_left))

        appended = []
        skipped = []
        try:
            # 纯数据 API：不经过算子、没有窗口 poll，界面与无头都一样能用
            # （用 bpy.ops.wm.append 会报 poll() failed / context is incorrect，见上面注释）
            before_append = {workspace.as_pointer() for workspace in bpy.data.workspaces}
            with bpy.data.libraries.load(library, link=False) as (source, target):
                # 幂等的关键一步：只追加「启动文件里没有同名同构成」的那些
                wanted, skipped = _plan_workspace_appends(list(source.workspaces), signatures)
                target.workspaces = wanted
            added = [workspace for workspace in bpy.data.workspaces
                     if workspace.as_pointer() not in before_append]
        except Exception as error:
            return restore_original("追加工作区失败：{}".format(error))
        appended = added
        # 注意：全部被跳过（``appended`` 为空）是**正常**的 —— 那正是幂等第二次运行的
        # 样子，不能像旧流程那样把它当成 "追加不完整" 报错。

        # 校验：每个记录下来的工作区要么本来就在（被跳过），要么真的追加回来了
        # （同名冲突时 Blender 会给 .001 后缀，下面重名收尾会把原名还回来）
        appended_by_name = {}
        for workspace in appended:
            base = workspace.name
            if base not in workspace_names:
                base = base.rsplit(".", 1)[0]
            appended_by_name.setdefault(base, workspace)
        missing_workspaces = [name for name in workspace_names
                              if bpy.data.workspaces.get(name) is None
                              and name not in appended_by_name]
        if missing_workspaces:
            return restore_original(
                "追加工作区不完整（缺 {}），已中止、没有写启动文件".format(
                    "、".join(missing_workspaces)))

        # ---- 重名收尾：让用户的工作区拿回原来的名字 ----
        # 中文界面下出厂启动文件里也有同名工作区（布局 / 建模 / ...），所以上面那一步
        # 追加进来的多半带着 ``.001`` 后缀。这里按用户确认的方案收尾。
        #
        # 校验态度（与"追加不完整"不同）：**丢了工作区**是数据损失，必须中止；
        # 而"出厂重名项没能清理干净"只是多几个标签 —— 照常写启动文件、如实报告，
        # 绝不让整个按钮变成"点了没反应"（用户明确表态过这一点）。
        collisions_before = len(_workspace_collision_pairs())
        workspace_total = len(bpy.data.workspaces)
        removed_duplicates, renamed_workspaces, collisions_left, dedupe_problems = (
            _dedupe_appended_workspaces())
        still_alive = [workspace for workspace in appended
                       if any(item is workspace for item in bpy.data.workspaces)]
        if len(still_alive) != len(appended):
            return restore_original("工作区重名收尾时丢了工作区，已中止、没有写启动文件")
        # 改名不改变数量、删除会让数量减少，所以对得上才算收尾正常
        if len(bpy.data.workspaces) != workspace_total - removed_duplicates:
            return restore_original(
                "工作区数量在收尾前后对不上（{} -> {}，删掉 {}），"
                "已中止、没有写启动文件".format(
                    workspace_total, len(bpy.data.workspaces), removed_duplicates))

        # ---- 落盘前校验：任何一条不过就**不写**启动文件（宁可没反应，也不写坏的）----
        # ① 用户的工作区一个都没丢（名字集合 ⊇ 运行前记录的集合）
        lost_names = [name for name in workspace_names
                      if bpy.data.workspaces.get(name) is None]
        if lost_names:
            return restore_original(
                "校验不通过：你的工作区丢了（{}），已中止、没有写启动文件".format(
                    "、".join(lost_names)))
        # ② 不存在 X 与 X.<数字> 并存（重名收尾之后还剩下就说明收尾没干净）
        pairs_left = _workspace_collision_pairs()
        if pairs_left:
            return restore_original(
                "校验不通过：还有 {} 对同名工作区（{}），已中止、没有写启动文件".format(
                    len(pairs_left), "、".join(item[0].name for item in pairs_left)))
        # ③ 启动文件是空场景
        objects_left = len(bpy.data.objects)
        if objects_left:
            return restore_original(
                "校验不通过：场景里还剩 {} 个物体，已中止、没有写启动文件".format(
                    objects_left))

        leftover_factory = sorted(workspace.name for workspace in bpy.data.workspaces
                                  if workspace.name.startswith(FACTORY_WORKSPACE_PREFIX))
        LAST_STARTUP_LAYOUT_RESULT.clear()
        LAST_STARTUP_LAYOUT_RESULT.update({
            "recorded": len(workspace_names),
            "appended": sorted(workspace.name for workspace in appended),
            "appended_total": len(appended),
            "skipped": sorted(skipped),
            "skipped_total": len(skipped),
            "objects": objects_left,
            "workspaces": sorted(workspace.name for workspace in bpy.data.workspaces),
            "factory_leftovers": leftover_factory,
            "duplicates_removed": removed_duplicates,
            "duplicates_renamed": renamed_workspaces,
        })

        # 让窗口停在**你自己**的那个工作区上。
        # 实测（4.0.2，界面与无头都测过）：脚本**改不动**当前工作区
        # （``window.workspace = ...`` 赋值不生效），所以这一步只能尽力而为，
        # 结果**核实**之后如实写进报告 —— 不假装切过去了。
        target_workspace = (appended_by_name.get(active_name)
                            or appended_by_name.get(workspace_names[0])
                            or (appended[0] if appended else None)
                            or bpy.data.workspaces.get(active_name)
                            or bpy.data.workspaces.get(workspace_names[0]))
        switched = (target_workspace is not None
                    and _make_workspace_current(target_workspace))

        try:
            bpy.ops.wm.save_homefile()
        except Exception as error:
            return restore_original("写启动文件失败：{}".format(error))

        if original_path:
            try:
                bpy.ops.wm.open_mainfile(filepath=original_path)
            except Exception as error:
                self.report({'ERROR'},
                            "启动布局已写好，但重新打开原来的文件失败：{}；"
                            "用 文件 打开 回到 {}".format(error, original_path))
                return {'CANCELLED'}

        # 文案如实说清：本次新增了几个、跳过了几个（已经有的不再重复追加）、
        # 启动文件还是空场景，以及「出厂 xxx」那批只能手工右键删。
        parts = startup_report_parts(appended, skipped, leftover_factory)
        parts.extend(_dedupe_report_parts(collisions_before, removed_duplicates,
                                         renamed_workspaces, collisions_left,
                                         dedupe_problems))
        if not switched:
            parts.append("注意：当前环境下脚本改不动窗口的活动工作区"
                         "（window.workspace 赋值实测不生效），"
                         "新开 Blender 时先显示哪一个工作区仍由 Blender 自己决定；"
                         "你的工作区都在，切一下即可")
        if original_path:
            parts.append("原来的文件已重新打开：{}".format(os.path.basename(original_path)))
        else:
            parts.append("当前会话停在空场景上；你原来的文件没有保存过，不会被改")
        if missing:
            parts.append("注意：当前文件还有 {} 个缺失引用（{}），"
                         "建议先点「检查并修复缺失文件」".format(
                             len(missing), _describe_items(missing, 2)))
        parts.append("想连场景一起存，请用 Blender 原生的 文件 默认 保存启动文件")
        self.report({'INFO'}, "；".join(parts))
        return {'FINISHED'}


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
