# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab 功能算符

核心功能（都作用在「选中的物体」，没选中时作用在活动物体，目标机制见
``compat.get_target_objects``，不额外引入设置项）：

* :class:`HHZ_OT_remove_all_materials`  一键删除所有材质
* :class:`HHZ_OT_apply_all_modifiers`   一键应用所有修改器
* :class:`HHZ_OT_remove_all_modifiers`  一键删除所有修改器
* :class:`HHZ_OT_flip_normals`          一键翻面（整体翻转网格所有面的朝向）
* :class:`HHZ_OT_detect_flipped`        检测是否有物体整体翻面（只读，不改任何数据）
* :class:`HHZ_OT_hide_cutters`          一键隐藏组里的所有刀具（被布尔引用的切割体，只隐藏不改数据）
* :class:`HHZ_OT_select_active_camera`  一键选中当前相机（当前视角使用的相机，只改选择不改数据）
* :class:`HHZ_OT_grayscale_filter`      灰度滤镜（切换色彩管理 Look，会一起影响最终渲染输出）

另外还有菜单/面板用到的辅助算符：打开偏好设置、捕获快捷键、恢复默认快捷键、
检查并修复缺失文件，以及 N 面板「主题颜色」用的几个算符（主题功能不进饼菜单）。
"""

import ast
import hashlib
import math
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile

import addon_utils
import bmesh
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


# ------------------------------------------------------------------- 一键翻面

#: 面朝向的判定结果（`detect_flip` / `_flip_verdict` 的返回值）
FACE_ORIENTATION_FLIPPED = "flipped"    #: 可靠结论：整体翻面
FACE_ORIENTATION_NORMAL = "normal"      #: 可靠结论：朝向正常
FACE_ORIENTATION_SUSPECT = "suspect"    #: 只敢说「疑似」（非闭合网格的启发式）
FACE_ORIENTATION_UNKNOWN = "unknown"    #: 判不了（没有面数据等）


def score_threshold(coordinates):
    """闭合性 / 体积判定的长度尺度。

    用包围盒对角线，空网格或退化网格退化成 1.0，保证阈值不是 0。
    """
    if not coordinates:
        return 1.0
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    for coordinate in coordinates:
        for axis in range(3):
            value = float(coordinate[axis])
            if value < low[axis]:
                low[axis] = value
            if value > high[axis]:
                high[axis] = value
    span = 0.0
    for axis in range(3):
        span += (high[axis] - low[axis]) ** 2
    return math.sqrt(span) or 1.0


def _signed_volume(coordinates, faces):
    """有符号体积：**外向法线**的闭合网格为正（散度定理的三角扇展开）。

    只需要「顶点坐标 + 多边形顶点索引」，可以脱离 bpy 数据块单独测试。
    """
    total = 0.0
    for face in faces:
        indices = list(face)
        if len(indices) < 3:
            continue
        anchor = coordinates[indices[0]]
        for position in range(1, len(indices) - 1):
            second = coordinates[indices[position]]
            third = coordinates[indices[position + 1]]
            total += (
                float(anchor[0]) * (float(second[1]) * float(third[2]) - float(second[2]) * float(third[1]))
                - float(anchor[1]) * (float(second[0]) * float(third[2]) - float(second[2]) * float(third[0]))
                + float(anchor[2]) * (float(second[0]) * float(third[1]) - float(second[1]) * float(third[0]))
            )
    return total / 6.0


def _is_closed_manifold(faces):
    """所有边恰好被两个面用到 —— 只有主判据（有符号体积）在这个前提下才成立。"""
    counts = {}
    for face in faces:
        indices = [int(index) for index in face]
        length = len(indices)
        if length < 3:
            continue
        for position in range(length):
            first = indices[position]
            second = indices[(position + 1) % length]
            edge = (first, second) if first < second else (second, first)
            counts[edge] = counts.get(edge, 0) + 1
    if not counts:
        return False
    return all(count == 2 for count in counts.values())


def _face_center(coordinates, face):
    indices = list(face)
    total = [0.0, 0.0, 0.0]
    for index in indices:
        coordinate = coordinates[index]
        total[0] += float(coordinate[0])
        total[1] += float(coordinate[1])
        total[2] += float(coordinate[2])
    count = max(len(indices), 1)
    return (total[0] / count, total[1] / count, total[2] / count)


def _centroid(coordinates):
    total = [0.0, 0.0, 0.0]
    for coordinate in coordinates:
        total[0] += float(coordinate[0])
        total[1] += float(coordinate[1])
        total[2] += float(coordinate[2])
    count = max(len(coordinates), 1)
    return (total[0] / count, total[1] / count, total[2] / count)


def _newell_normal(coordinates, face):
    """多边形法线（Newell 法，与网格自带的法线定义一致）。"""
    indices = list(face)
    normal = [0.0, 0.0, 0.0]
    length = len(indices)
    for position in range(length):
        current = coordinates[indices[position]]
        following = coordinates[indices[(position + 1) % length]]
        normal[0] += (float(current[1]) - float(following[1])) * (float(current[2]) + float(following[2]))
        normal[1] += (float(current[2]) - float(following[2])) * (float(current[0]) + float(following[0]))
        normal[2] += (float(current[0]) - float(following[0])) * (float(current[1]) + float(following[1]))
    return normal


def _inward_face_ratio(coordinates, faces):
    """「质心朝外」启发式：法线与「质心 -> 面心」同向的面算朝内。

    返回 ``(比例, 参与统计的面数)``。这是启发式，不是几何事实（见
    :func:`detect_flip` 的说明）。
    """
    centroid = _centroid(coordinates)
    inward = 0
    considered = 0
    for face in faces:
        indices = list(face)
        if len(indices) < 3:
            continue
        center = _face_center(coordinates, face)
        outward = [center[0] - centroid[0], center[1] - centroid[1], center[2] - centroid[2]]
        length = math.sqrt(outward[0] ** 2 + outward[1] ** 2 + outward[2] ** 2)
        if length <= 1e-12:
            continue
        normal = _newell_normal(coordinates, face)
        dot = (
            normal[0] * outward[0] + normal[1] * outward[1] + normal[2] * outward[2]
        ) / length
        considered += 1
        if dot < 0.0:
            inward += 1
    if not considered:
        return 0.0, 0
    return inward / considered, considered


def detect_flip(coordinates, faces, closed=None):
    """判断一组网格数据是否整体翻面 —— **纯函数**，不碰 bpy。

    返回 ``{"verdict", "criterion", "volume", "is_closed", "faces",
    "inward_ratio", "inward_faces", "reliable"}``。

    两份判据，可靠性完全不同：

    1. **闭合网格（每条边恰好两个面）→ 有符号体积**。外向法线的闭合网格
       体积为正，整体翻面后为负。这是几何事实，判为 ``flipped`` / ``normal``。
    2. **非闭合网格 → 「质心朝外」启发式**。统计法线指向质心的面（内指面）
       占比，超过 ``FACE_SUSPECT_RATIO`` 就报 ``suspect``。**它只是启发式**：
       整体翻面的空心壳、法线一致向内的球都能被它看出来，但
       * 凹壳（例如 L 形、甜甜圈）里「朝质心」的面本来就可能过半 → 误报；
       * 平面 / 单层结构没有内外之分 → 无意义；
       * 非流形的碎面组合更是随口一说。
       所以非闭合一律标成「疑似」，**不得当成确定结论**。

    空数据（没有顶点或没有面）判为 ``unknown``。
    """
    coordinates = list(coordinates or ())
    faces = [list(face) for face in (faces or ())]
    usable = [face for face in faces if len(face) >= 3]

    result = {
        "verdict": FACE_ORIENTATION_UNKNOWN,
        "criterion": "",
        "volume": None,
        "is_closed": None,
        "faces": len(usable),
        "inward_ratio": None,
        "inward_faces": 0,
        "reliable": False,
    }
    if not coordinates or not usable:
        result["criterion"] = "没有面数据，无法判定"
        return result

    if closed is None:
        closed = _is_closed_manifold(usable)
    result["is_closed"] = bool(closed)

    scale = score_threshold(coordinates)
    volume = _signed_volume(coordinates, usable)
    tolerance = (scale ** 3) * VOLUME_ZERO_RATIO

    if closed:
        result["criterion"] = "闭合网格：有符号体积"
        if math.isfinite(volume) and volume < -tolerance:
            result["volume"] = volume
            result["verdict"] = FACE_ORIENTATION_FLIPPED
            result["reliable"] = True
            return result
        if math.isfinite(volume) and volume > tolerance:
            result["volume"] = volume
            result["verdict"] = FACE_ORIENTATION_NORMAL
            result["reliable"] = True
            return result
        # 闭合但体积约等于 0（自相交、零体积壳）：体积判不出方向，
        # 但**仍能**用启发式看一眼，结论照样只是「疑似」。
        result["volume"] = volume if math.isfinite(volume) else None

    ratio, considered = _inward_face_ratio(coordinates, usable)
    result["inward_ratio"] = ratio
    result["inward_faces"] = int(round(ratio * considered))
    result["faces"] = considered or len(usable)
    if considered and ratio >= FACE_SUSPECT_RATIO:
        result["verdict"] = FACE_ORIENTATION_SUSPECT
        if closed:
            result["criterion"] = "闭合但体积约等于 0：改用「质心朝外」启发式（疑似）"
        else:
            result["criterion"] = "非闭合网格：「质心朝外」启发式（疑似）"
    else:
        result["verdict"] = FACE_ORIENTATION_NORMAL
        result["criterion"] = (
            "闭合但体积约等于 0 且内指面不占多数（按启发式判正常）"
            if closed else "非闭合网格：内指面不占多数（按启发式判正常，但不可靠）"
        )
    return result


def detect_objects_flip(objects):
    """对一串物体跑 :func:`detect_flip`，返回 ``(结果列表, 跳过计数)``。

    跳过 = 非网格 / 空网格 / 没有面。**只读**：不写邮箱、不改选择、不动网格。
    """
    results = []
    skipped = 0
    for obj in objects:
        coordinates, faces = mesh_geometry(obj)
        if not faces:
            skipped += 1
            continue
        verdict = detect_flip(coordinates, faces)
        verdict["name"] = obj.name
        results.append(verdict)
    return results, skipped


def describe_flip_result(result):
    """把一条判定结果压成一行中文说明（报告与控制台共用）。"""
    verdict = result.get("verdict")
    if verdict == FACE_ORIENTATION_FLIPPED:
        return "整体翻面（{} {:.6g}）".format(result.get("criterion") or "", result.get("volume") or 0.0)
    if verdict == FACE_ORIENTATION_SUSPECT:
        return "疑似翻面（{} 内指面 {:.0%}）".format(
            result.get("criterion") or "", result.get("inward_ratio") or 0.0)
    if verdict == FACE_ORIENTATION_NORMAL:
        return "正常（{}）".format(result.get("criterion") or "")
    return "无法判定（{}）".format(result.get("criterion") or "")


def mesh_geometry(obj):
    """取出物体的 ``(顶点坐标列表, 面顶点索引列表)``；非网格返回 ``([], [])``。

    只读操作：优先用只读的 ``to_mesh()``，拿不到时退回 ``data`` 本体
    （此时只取顶点坐标与 polygon 索引，不算、也不写任何网格数据）。
    """
    if getattr(obj, "type", "") != 'MESH' or getattr(obj, "data", None) is None:
        return [], []
    mesh = None
    temporary = False
    try:
        mesh = obj.to_mesh()
        temporary = mesh is not None
    except Exception:
        mesh = None
    if mesh is None:
        mesh = obj.data
        temporary = False
    try:
        coordinates = [tuple(vertex.co) for vertex in mesh.vertices]
        faces = [tuple(polygon.vertices) for polygon in mesh.polygons]
        return coordinates, faces
    except Exception:
        return [], []
    finally:
        if temporary:
            try:
                obj.to_mesh_clear()
            except Exception:
                pass


def flip_faces_in_place(mesh):
    """用 bmesh 原地翻转网格的所有面，返回翻过的面数。

    不进入编辑模式（所以不会打扰用户、也不会踩编辑模式快捷键），
    不动 UV / 顶点组 / 材质槽 —— ``bmesh.ops.reverse_faces`` 只重排每个面的
    顶点顺序，这些图层是按「面 / 顶点」索引存的，不受影响；自定义拆分法线
    数据（corner 层）同样保留，只是它的方向会跟着面一起翻过去。

    ``bmesh.ops.reverse_faces`` 只接受 ``faces`` 与 ``flip_multires`` 两个参数
    （4.5.14 实测：传 ``flip_uvs`` 会直接 ``TypeError``），所以 UV 由它原样保留。
    """
    bmesh_module = bmesh
    mesh_bmesh = bmesh_module.new()
    try:
        mesh_bmesh.from_mesh(mesh)
        targets = [face for face in mesh_bmesh.faces]
        if not targets:
            return 0
        bmesh_module.ops.reverse_faces(
            mesh_bmesh, faces=targets, flip_multires=False)
        mesh_bmesh.to_mesh(mesh)
    finally:
        mesh_bmesh.free()
    mesh.update()
    return len(targets)

def _flip_object_mesh(obj):
    """翻一个物体的网格，返回 ``(翻过的面数, 是否做了单用户化)``。

    多用户网格先复制成单用户（复用 ``compat.ensure_single_user_data``），
    否则会把共享同一份网格数据的**其它物体**一起改掉。
    """
    if getattr(obj, "type", "") != 'MESH' or getattr(obj, "data", None) is None:
        return 0, False
    became_single_user = compat.ensure_single_user_data(obj)
    faces = flip_faces_in_place(obj.data)
    return faces, became_single_user

class HHZ_OT_flip_normals(_BaseTargetsOperator):
    bl_idname = "hhz.flip_normals"
    bl_label = "一键翻面"
    bl_description = (
        "把选中物体（没选中时是活动物体）的网格所有面整体翻转，"
        "即把朝内的法线翻成朝外；多用户网格会先复制成单用户，非网格物体跳过"
    )

    def execute(self, context):
        global _LAST_RUN
        usable, skipped_library = _editable_targets(context)
        if not usable:
            self.report({'WARNING'}, "没有可处理的物体（库链接物体已跳过）")
            return {'CANCELLED'}

        flipped = 0
        faces_total = 0
        skipped_mesh = 0
        skipped_empty = 0
        single_user = 0

        for obj in usable:
            if getattr(obj, "type", "") != 'MESH' or getattr(obj, "data", None) is None:
                skipped_mesh += 1
                continue
            if not len(getattr(obj.data, "polygons", ()) or ()):
                skipped_empty += 1
                continue
            faces, became_single_user = _flip_object_mesh(obj)
            if became_single_user:
                single_user += 1
            if faces:
                flipped += 1
                faces_total += faces

        _LAST_RUN = {
            "flipped": flipped,
            "faces": faces_total,
            "skipped_non_mesh": skipped_mesh,
            "skipped_empty": skipped_empty,
            "single_user": single_user,
            "skipped_library": len(skipped_library),
        }

        notes = []
        if skipped_mesh:
            notes.append("跳过 {} 个非网格物体".format(skipped_mesh))
        if skipped_empty:
            notes.append("跳过 {} 个没有面的网格".format(skipped_empty))
        if single_user:
            notes.append("其中 {} 个多用户网格已复制成单用户".format(single_user))
        if skipped_library:
            notes.append("跳过 {} 个库链接物体".format(len(skipped_library)))

        if flipped == 0:
            self.report({'INFO'}, _join_notes(["没有可翻面的网格"] + notes))
        else:
            self.report({'INFO'}, _join_notes(
                ["已翻面 {} 个 / 跳过 {} 个（非网格）".format(flipped, skipped_mesh)]
                + ["（共 {} 个面）".format(faces_total)]
                + notes))
        return {'FINISHED'}


# ------------------------------------------------- 检测「是否有物体有反面」

#: 非闭合网格启发式：内指面占到这么多就报「疑似」
FACE_SUSPECT_RATIO = 0.5

#: 有符号体积小于「包围盒对角线³ × 这个比例」就算约等于 0（判不了方向）
VOLUME_ZERO_RATIO = 1e-6

#: 检测算符最近一次的汇总结果（真机测试直接断言它，不必去解析 report 文本）
_LAST_RUN = {}


class HHZ_OT_detect_flipped(_BaseTargetsOperator):
    bl_idname = "hhz.detect_flipped"
    bl_label = "检测是否有反面"
    bl_description = (
        "只读检查：闭合网格用有符号体积判断是否整体翻面（可靠），"
        "非闭合网格用「质心朝外」启发式给出疑似结果；不修改任何网格与选择"
    )

    def execute(self, context):
        global _LAST_RUN
        usable, skipped_library = _editable_targets(context)
        if not usable:
            self.report({'WARNING'}, "没有可检测的物体（库链接物体已跳过）")
            return {'CANCELLED'}

        results, skipped = detect_objects_flip(usable)

        counts = {
            FACE_ORIENTATION_FLIPPED: 0,
            FACE_ORIENTATION_SUSPECT: 0,
            FACE_ORIENTATION_NORMAL: 0,
            FACE_ORIENTATION_UNKNOWN: 0,
        }
        for result in results:
            counts[result.get("verdict", FACE_ORIENTATION_UNKNOWN)] += 1

        total_skipped = skipped + len(skipped_library)
        _LAST_RUN = {
            "detected": len(results),
            "flipped": counts[FACE_ORIENTATION_FLIPPED],
            "suspect": counts[FACE_ORIENTATION_SUSPECT],
            "normal": counts[FACE_ORIENTATION_NORMAL],
            "unknown": counts[FACE_ORIENTATION_UNKNOWN],
            "skipped": total_skipped,
            "skipped_non_mesh": skipped,
            "skipped_library": len(skipped_library),
            "results": [dict(result) for result in results],
        }

        # 逐个物体打到系统控制台：判据、体积或内指面比例都要能看到
        for result in results:
            print("[{} 检测] {}：{}".format(
                compat.ADDON_NAME, result.get("name", "?"), describe_flip_result(result)))
            if result.get("volume") is not None:
                print("[{} 检测]     有符号体积 {:.6g}（闭合网格，外向法线为正）".format(
                    compat.ADDON_NAME, result["volume"]))
            if result.get("inward_ratio") is not None:
                print("[{} 检测]     内指面 {}/{} = {:.1%}（非闭合时只作「疑似」参考）".format(
                    compat.ADDON_NAME, result.get("inward_faces", 0), result.get("faces", 0),
                    result["inward_ratio"]))

        summary = "检测 {} 个：整体翻面 {} 个 / 疑似 {} 个 / 正常 {} 个 / 跳过 {} 个".format(
            len(results) + total_skipped, counts[FACE_ORIENTATION_FLIPPED],
            counts[FACE_ORIENTATION_SUSPECT], counts[FACE_ORIENTATION_NORMAL], total_skipped)

        notes = []
        if counts[FACE_ORIENTATION_UNKNOWN]:
            notes.append("{} 个无法判定".format(counts[FACE_ORIENTATION_UNKNOWN]))
        if skipped:
            notes.append("跳过 {} 个非网格或空网格".format(skipped))
        if skipped_library:
            notes.append("跳过 {} 个库链接物体".format(len(skipped_library)))
        notes.append("非闭合网格只能给出「疑似」（质心朝外是启发式，凹壳会误报）")

        level = {'INFO'}
        if counts[FACE_ORIENTATION_FLIPPED]:
            level = {'WARNING'}
        self.report(level, _join_notes([summary] + notes))
        return {'FINISHED'}


# ------------------------------------------------ 一键隐藏组里的所有刀具
#
# 需求原话是「一键隐藏组里面的所有刀具」。这里的「刀具」**不靠名字猜**：
# HardOps / Boxcutter 那一类工作流里，刀具 = 被拿来布尔切割别的物体的那个物体，
# 切完通常只剩线框显示。所以判据按可靠性排优先级：
#
#   A 被任何布尔修改器引用（含集合型布尔里的每个物体）—— 最可靠，同时记下「谁引用它」；
#   B 线框显示（display_type == 'WIRE' 或 show_wire）且名字像刀具 —— 交叉验证；
#   C 只靠名字 —— 兜底，报告里必须写明「仅按名字推断」，不许当成确定结论。
#
# 执行只调 ``obj.hide_set(True)``（当前视图层，等价于按 H）：不改显示类型、
# 不删物体、不动修改器、不碰选择。

#: 名字里出现这些词就算「像刀具」（小写比较，含中文；判据 B / C 共用）
CUTTER_NAME_KEYWORDS = ("cutter", "bool", "刀具", "cut")

#: 能当刀具的物体类型（其它类型一律跳过并计数）
CUTTER_TYPES = ('MESH', 'CURVE')

#: 判定来源（报告与真机测试都按这三个键核对依据）
CUTTER_BY_MODIFIER = "modifier"
CUTTER_BY_WIRE_NAME = "wire_name"
CUTTER_BY_NAME_ONLY = "name_only"

#: 判定来源 -> 统计里的计数键
CUTTER_REASON_KEYS = {
    CUTTER_BY_MODIFIER: "by_modifier",
    CUTTER_BY_WIRE_NAME: "by_wire_name",
    CUTTER_BY_NAME_ONLY: "by_name_only",
}

#: 两种目标模式（「组」= 集合；枚举 identifier 必须是纯 ASCII）
CUTTER_TARGET_SELECTED = 'SELECTED'
CUTTER_TARGET_COLLECTION = 'COLLECTION'

#: 刀具算符最近一次的汇总（真机测试直接断言它，不去解析 report 文本）
_LAST_CUTTER_RUN = {}


def cutter_name_matches(name):
    """名字里是否含刀具关键词 —— **纯函数**，不区分大小写。"""
    text = (name or "").lower()
    return any(token in text for token in CUTTER_NAME_KEYWORDS)


def is_wire_display(obj):
    """物体是否是线框显示（``display_type == 'WIRE'`` 或 ``show_wire``）—— 只读。"""
    if getattr(obj, "display_type", "") == 'WIRE':
        return True
    return bool(getattr(obj, "show_wire", False))


def is_hidden_object(obj):
    """物体在当前视图层里是否已经被隐藏（``hide_get()``，退化到 ``hide_viewport``）。"""
    getter = getattr(obj, "hide_get", None)
    if callable(getter):
        try:
            return bool(getter())
        except Exception:
            pass
    return bool(getattr(obj, "hide_viewport", False))


def cutter_reference_map(holders):
    """扫出「谁被布尔修改器引用」：返回 ``{被引用物体名: [引用者描述, ...]}``。

    **只读**：只看 ``modifiers`` 里 ``type == 'BOOLEAN'`` 的项，不写任何数据。
    ``operand_type == 'OBJECT'`` 取 ``mod.object``；``'COLLECTION'`` 取
    ``mod.collection.objects`` 里的每个物体。引用者描述进报告，能看出依据。
    """
    mapping = {}
    for holder in holders or ():
        if holder is None:
            continue
        holder_name = getattr(holder, "name", "") or "?"
        for modifier in getattr(holder, "modifiers", ()) or ():
            if getattr(modifier, "type", "") != 'BOOLEAN':
                continue
            modifier_name = getattr(modifier, "name", "") or "?"
            if getattr(modifier, "operand_type", 'OBJECT') == 'COLLECTION':
                collection = getattr(modifier, "collection", None)
                operands = list(getattr(collection, "objects", ()) or ())
                label = "{} 的 {}（集合布尔）".format(holder_name, modifier_name)
            else:
                single = getattr(modifier, "object", None)
                operands = [single] if single is not None else []
                label = "{} 的 {}".format(holder_name, modifier_name)
            for operand in operands:
                key = getattr(operand, "name", None)
                if not key:
                    continue
                labels = mapping.setdefault(key, [])
                if label not in labels:
                    labels.append(label)
    return mapping


def detect_cutters(objs, references=None):
    """按优先级识别刀具 —— **纯函数**（不 import bpy、不写任何数据）。

    优先级 A > B > C：

    * A 被布尔修改器引用（``references`` 里登记了它）—— 最可靠；
    * B 线框显示 **且** 名字含关键词 —— 交叉验证；
    * C 只靠名字 —— 兜底，调用方必须在报告里写明「仅按名字推断」。

    返回 ``(刀具列表, 统计)``：

    * 刀具 = ``[{"object", "name", "reason", "referenced_by"}, ...]``，
      ``reason`` 是 ``modifier`` / ``wire_name`` / ``name_only``；
    * 统计 = ``{"total", "cutters", "by_modifier", "by_wire_name", "by_name_only",
      "skipped_non_mesh", "skipped_hidden", "found"}``。

    非 Mesh / Curve 的物体计入 ``skipped_non_mesh``；**已经隐藏的**刀具计入
    ``skipped_hidden``（不会再 hide 一次，也不谎报成隐藏成功）。
    """
    stats = {
        "total": 0,
        "cutters": 0,
        "by_modifier": 0,
        "by_wire_name": 0,
        "by_name_only": 0,
        "skipped_non_mesh": 0,
        "skipped_hidden": 0,
    }
    reference_map = references or {}
    cutters = []
    seen = set()
    for obj in objs or ():
        if obj is None:
            continue
        name = getattr(obj, "name", "") or ""
        if name:
            if name in seen:
                continue
            seen.add(name)
        stats["total"] += 1
        if getattr(obj, "type", "") not in CUTTER_TYPES:
            stats["skipped_non_mesh"] += 1
            continue

        referenced_by = list(reference_map.get(name, ()) or ())
        if referenced_by:
            reason = CUTTER_BY_MODIFIER
        elif cutter_name_matches(name) and is_wire_display(obj):
            reason = CUTTER_BY_WIRE_NAME
        elif cutter_name_matches(name):
            reason = CUTTER_BY_NAME_ONLY
        else:
            continue

        if is_hidden_object(obj):
            stats["skipped_hidden"] += 1
            continue

        cutters.append({
            "object": obj,
            "name": name,
            "reason": reason,
            "referenced_by": referenced_by,
        })
        stats["cutters"] += 1
        stats[CUTTER_REASON_KEYS[reason]] += 1

    stats["found"] = bool(cutters)
    return cutters, stats


def describe_cutters_report(stats, failed=0):
    """把识别 / 隐藏统计压成一行中文报告（隐藏成功数单独算，失败如实写）。"""
    attempted = int(stats.get("cutters", 0))
    hidden = max(attempted - int(failed or 0), 0)
    text = "已隐藏 {} 个刀具：被布尔修改器引用 {} 个、线框+命名 {} 个、仅按名字 {} 个".format(
        hidden, stats.get("by_modifier", 0), stats.get("by_wire_name", 0),
        stats.get("by_name_only", 0))
    notes = [
        "跳过非网格 {} 个".format(stats.get("skipped_non_mesh", 0)),
        "已在隐藏集合 {} 个".format(stats.get("skipped_hidden", 0)),
        "失败 {} 个".format(int(failed or 0)),
    ]
    text = "{}；{}".format(text, "、".join(notes))
    if stats.get("by_name_only"):
        text = "{}；其中 {} 个仅按名字推断，不是确定结论".format(
            text, stats["by_name_only"])
    return text


def collection_objects(collection, _seen=None):
    """集合及其所有子集合里的物体（每个物体最多一次，防递归死循环）—— 只读。"""
    if collection is None:
        return []
    seen = _seen if _seen is not None else set()
    key = getattr(collection, "name", "?")
    if key in seen:
        return []
    seen.add(key)

    objects = []
    names = set()
    for obj in getattr(collection, "objects", ()) or ():
        name = getattr(obj, "name", None)
        if not name or name in names:
            continue
        names.add(name)
        objects.append(obj)
    for child in getattr(collection, "children", ()) or ():
        for obj in collection_objects(child, seen):
            name = getattr(obj, "name", "")
            if name in names:
                continue
            names.add(name)
            objects.append(obj)
    return objects


def cutter_target_objects(context, mode):
    """按目标模式取 ``(物体列表, 目标描述)``。

    * ``CUTTER_TARGET_SELECTED``：沿用既有目标机制（选中物体，没选中时是活动物体）；
    * ``CUTTER_TARGET_COLLECTION``：当前高亮的集合（``context.collection``，也就是
      Outliner 里选中的那一格）及其所有子集合 —— 「组」= 集合。
    """
    if mode == CUTTER_TARGET_COLLECTION:
        collection = getattr(context, "collection", None)
        if collection is None:
            layer = getattr(getattr(context, "view_layer", None),
                            "active_layer_collection", None)
            collection = getattr(layer, "collection", None)
        if collection is None:
            return [], "当前集合"
        return collection_objects(collection), "集合「{}」".format(collection.name)
    return compat.get_target_objects(context), "选中物体"


class HHZ_OT_hide_cutters(_BaseTargetsOperator):
    """一键隐藏组里的所有刀具（同一个算符两种目标：选中物体 / 当前集合）。"""

    bl_idname = "hhz.hide_cutters"
    bl_label = "一键隐藏刀具"
    bl_description = (
        "把目标里被布尔切割用的刀具物体在当前视图层隐藏（等价于逐个按 H）："
        "优先认被布尔修改器引用的物体，其次认线框显示且名字像刀具的物体，"
        "两者都没命中时才仅按名字推断；只隐藏，不改显示类型、不删物体、不动修改器"
    )

    target_mode: bpy.props.EnumProperty(
        name="目标",
        description="刀具从哪里找：选中的物体，或当前高亮的集合（含子集合）",
        items=(
            (CUTTER_TARGET_SELECTED, "选中物体", "只在选中的物体里找刀具"),
            (CUTTER_TARGET_COLLECTION, "当前集合", "在当前高亮的集合及其子集合里找刀具"),
        ),
        default=CUTTER_TARGET_SELECTED,
    )

    @classmethod
    def poll(cls, context):
        # 「按集合」模式下即使什么都没选中也应该能点，所以 poll 放宽成
        # 「物体模式 + 选中物体或当前集合里至少有一个物体」。
        if getattr(context, "mode", "OBJECT") != "OBJECT":
            return False
        if compat.poll_objects(context):
            return True
        return bool(collection_objects(getattr(context, "collection", None)))

    def execute(self, context):
        global _LAST_CUTTER_RUN
        targets, target_label = cutter_target_objects(context, self.target_mode)
        if not targets:
            _LAST_CUTTER_RUN = {
                "mode": self.target_mode, "target": target_label, "cutters": 0,
                "hidden": 0, "failed": 0, "by_modifier": 0, "by_wire_name": 0,
                "by_name_only": 0, "skipped_non_mesh": 0, "skipped_hidden": 0,
                "details": [], "failures": [],
            }
            self.report({'WARNING'}, "{}里没有可检查的物体".format(target_label))
            return {'CANCELLED'}

        # 布尔引用一律扫整个文件里的物体：刀具可能被别的集合里的主体引用，
        # 只看目标集合会漏掉最可靠的判据 A。
        references = cutter_reference_map(bpy.data.objects)
        cutters, stats = detect_cutters(targets, references)

        hidden = 0
        failed = 0
        failures = []
        for item in cutters:
            try:
                item["object"].hide_set(True)
            except Exception as error:
                failed += 1
                failures.append("{}（{}）".format(item["name"], error))
                continue
            hidden += 1

        _LAST_CUTTER_RUN = {
            "mode": self.target_mode,
            "target": target_label,
            "cutters": stats["cutters"],
            "hidden": hidden,
            "failed": failed,
            "by_modifier": stats["by_modifier"],
            "by_wire_name": stats["by_wire_name"],
            "by_name_only": stats["by_name_only"],
            "skipped_non_mesh": stats["skipped_non_mesh"],
            "skipped_hidden": stats["skipped_hidden"],
            "details": [{"name": item["name"], "reason": item["reason"],
                         "referenced_by": list(item["referenced_by"])}
                        for item in cutters],
            "failures": failures,
        }

        if not cutters:
            if stats["skipped_hidden"]:
                text = "{}里的刀具已经全部隐藏（{} 个）".format(
                    target_label, stats["skipped_hidden"])
            else:
                text = "{}里没有找到刀具（依据：布尔修改器引用 / 线框+命名）".format(
                    target_label)
            if stats["skipped_non_mesh"]:
                text = "{}；跳过非网格 {} 个".format(text, stats["skipped_non_mesh"])
            self.report({'INFO'}, text)
            return {'CANCELLED'}

        text = "{}；恢复：Alt+H".format(describe_cutters_report(stats, failed))
        if failed and failures:
            # 失败原因单独打一行，报告里只留计数（状态栏太短）
            for line in failures:
                print("[{} 刀具] 隐藏失败：{}".format(compat.ADDON_NAME, line))
        self.report({'WARNING'} if failed else {'INFO'}, text)
        return {'FINISHED'}


# ------------------------------------------------------- 一键查看面朝向

#: 3D 视口叠加层里「面朝向」的开关属性名（Blender 自带，不是本插件的设置项）
FACE_ORIENTATION_PROPERTY = "show_face_orientation"


def next_face_orientation_state(states):
    """由一组「当前开关状态」算出切换后的目标状态 —— **纯函数**。

    语义：只要还有任何一个视口是关着的，就**全部打开**；全部已经打开才**全部关闭**。
    这样点一次就能看到面朝向、再点一次就收起来。

    空输入返回 ``True``（没有视口可看时按「要打开」处理，交给调用方去报告
    找不到视口）。逻辑本身与 bpy 上下文完全解耦，所以无头环境也能完整测。
    """
    values = [bool(state) for state in (states or ())]
    if not values:
        return True
    return not all(values)


def _viewport_overlays(context):
    """当前屏幕（``context.screen``）里所有 3D 视口的叠加层设置。

    取的是**当前屏幕的全部 3D 视口**，不是只看鼠标所在的那一个 ——
    所以从饼菜单点、在任何一个 3D 视口上都能用。
    ``--background`` 下没有 ``context.screen``（``bpy.context.screen`` 是 None），
    这时返回空列表，由调用方给出友好提示。
    """
    overlays = []
    screen = getattr(context, "screen", None)
    if screen is None:
        return overlays
    try:
        areas = list(screen.areas)
    except Exception:
        return overlays
    for area in areas:
        if getattr(area, "type", "") != 'VIEW_3D':
            continue
        space = None
        try:
            for candidate in area.spaces:
                if getattr(candidate, "type", "") == 'VIEW_3D':
                    space = candidate
                    break
        except Exception:
            space = None
        if space is None:
            continue
        overlay = getattr(space, "overlay", None)
        if overlay is None:
            continue
        overlays.append(overlay)
    return overlays


class HHZ_OT_toggle_face_orientation(bpy.types.Operator):
    bl_idname = "hhz.toggle_face_orientation"
    bl_label = "一键查看面朝向"
    bl_description = (
        "切换当前屏幕里所有 3D 视口的面朝向叠加层（朝外蓝、朝内红）："
        "只要有视口是关着的就全部打开，全部已打开才全部关闭"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        try:
            return getattr(context, "screen", None) is not None
        except Exception:
            return False

    def execute(self, context):
        overlays = _viewport_overlays(context)
        if not overlays:
            self.report({'WARNING'}, "当前上下文没有 3D 视口")
            return {'CANCELLED'}

        current = []
        for overlay in overlays:
            try:
                current.append(bool(getattr(overlay, FACE_ORIENTATION_PROPERTY)))
            except Exception:
                current.append(False)

        target = next_face_orientation_state(current)
        changed = 0
        for overlay in overlays:
            try:
                setattr(overlay, FACE_ORIENTATION_PROPERTY, target)
                changed += 1
            except Exception:
                pass

        if changed == 0:
            self.report({'WARNING'}, "当前上下文没有 3D 视口")
            return {'CANCELLED'}

        # 切换可能发生在非 3D 区域的上下文里，全部视口都要重画
        compat.tag_redraw(context)
        if target:
            self.report({'INFO'}, "已打开 {} 个视口的面朝向".format(changed))
        else:
            self.report({'INFO'}, "已关闭 {} 个视口的面朝向".format(changed))
        return {'FINISHED'}


# ----------------------------------------------------------------- 灰度滤镜
#
# 2.18.0 实测结论（4.0.2 与 5.2.2 各跑一遍，读数见 README 第四节）：
#
# * 色彩管理曲线 ``view_settings.curve_mapping`` 的 ``curves`` **只有 4 条**
#   （Composite / R / G / B，通道名读不到、``curves`` 也不支持 ``new()``），
#   **没有 Saturation(S) 通道**，所以「把 S 曲线输出设成 0」这条路不存在。
# * ``ColorManagedViewSettings`` 上**也没有** ``saturation`` 标量
#   （4.0.2 与 5.2.2 的 RNA 属性列表里都没有）。
# * 合成器 ``CompositorNodeRGBToBW`` 存在，但它必须改用户的 ``scene.use_nodes``
#   与合成树（5.x 的树还搬到了 ``scene.compositing_node_group``），
#   代价远大于收益，不采用。
# * **Blender 没有「只作用于视口」的灰度开关**（``View3DShading`` /
#   ``View3DOverlay`` 里没有任何 gray/bw/mono/desatur 属性）。
#
# 最终采用现成的色彩管理 Look ``AgX - Greyscale``：它是 OCIO 配置里的
# 真灰度（Rec.709 亮度权重矩阵，R=G=B），两个版本都存在。
#
# **重要：它作用在显示变换（display transform）上，所以会一起改变最终渲染
# 输出**，不是视口专属开关。因此本算符每次执行都会在报告里明确告知用户，
# 绝不偷偷影响渲染。

#: 两版 Blender 内置 OCIO 配置里都存在的灰度 Look（实测可写、可读回）
GRAYSCALE_LOOK_ID = "AgX - Greyscale"

#: 场景自定义属性：本算符开灰度前记录的原值，以及「当前是否已开灰度」。
#: 这两个值必须能随 .blend 保存并参与撤销，才谈得上「关闭后完全恢复原状态」。
GRAYSCALE_STATE_PROPERTY = "hhz_grayscale_filter"
GRAYSCALE_PREVIOUS_LOOK_PROPERTY = "hhz_grayscale_filter_previous_look"


def _scene_view_settings(scene):
    """场景的色彩管理视图设置；拿不到就返回 None（不抛异常）。"""
    try:
        return getattr(scene, "view_settings", None)
    except Exception:
        return None


def _current_look(view_settings):
    """当前 Look 名；读不到返回 None。"""
    try:
        return getattr(view_settings, "look", None)
    except Exception:
        return None


def _iter_scenes():
    """当前文件里的所有场景（灰度是场景级设置，每个场景各记各的原值）。"""
    try:
        return list(bpy.data.scenes)
    except Exception:
        return []


def grayscale_preview_state(scene):
    """返回 ``(是否已开灰度, 记录的原 Look)``。

    已开灰度时原值可能是 ``None``（原本 Look 为空也属于「原本就关着」），
    所以用第二个布尔一起回传，调用方才分得清「没记录」和「记录的是空值」。
    """
    if scene is None:
        return False, None, False
    try:
        enabled = bool(scene.get(GRAYSCALE_STATE_PROPERTY, False))
    except Exception:
        enabled = False
    recorded = False
    try:
        recorded = GRAYSCALE_PREVIOUS_LOOK_PROPERTY in scene.keys()
    except Exception:
        recorded = False
    previous = None
    try:
        previous = scene.get(GRAYSCALE_PREVIOUS_LOOK_PROPERTY, None)
    except Exception:
        previous = None
    return enabled, previous, recorded


class HHZ_OT_grayscale_filter(bpy.types.Operator):
    """一键相机视角灰度滤镜：在 3D 视口里把画面切换成黑白。"""

    bl_idname = "hhz.grayscale_filter"
    bl_label = "灰度滤镜"
    bl_description = (
        "把画面切换成黑白（用色彩管理内的 AgX - Greyscale 显示 Look）；"
        "再按一次完全恢复原状态（原 Look 值逐个场景记录后还原）；"
        "注意：这是显示变换，会同时影响最终渲染输出，不是视口专属开关"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # 没有场景时优雅禁用（不抛异常）；真正执行时还会再兜一层友好提示
        try:
            return context.scene is not None
        except Exception:
            return False

    def _targets(self):
        """可改的场景：拿得到 ``view_settings`` 的那些。"""
        usable = []
        for scene in _iter_scenes():
            if _scene_view_settings(scene) is not None:
                usable.append(scene)
        return usable

    def execute(self, context):
        try:
            scenes = self._targets()
        except Exception as trouble:
            self.report({'WARNING'}, "读取场景失败：{}".format(trouble))
            return {'CANCELLED'}
        if not scenes:
            self.report({'WARNING'}, "当前文件里没有可用场景，无法切换灰度滤镜")
            return {'CANCELLED'}

        # 用第一个场景的状态代表整个文件的开关状态（本算符统一开、统一关）
        current = _current_look(_scene_view_settings(scenes[0]))
        was_enabled = grayscale_preview_state(scenes[0])[0]

        if was_enabled or current == GRAYSCALE_LOOK_ID:
            return self._turn_off(scenes)

        # 明确告知：这一步会连同最终渲染输出一起变灰，绝不静默改渲染
        self._enable(scenes)
        return {'FINISHED'}

    def _enable(self, scenes):
        changed = 0
        failures = []
        for scene in scenes:
            settings = _scene_view_settings(scene)
            if settings is None:
                continue
            try:
                scene[GRAYSCALE_PREVIOUS_LOOK_PROPERTY] = _current_look(settings)
                settings.look = GRAYSCALE_LOOK_ID
                read_back = _current_look(settings)
            except Exception as trouble:
                failures.append("{}（{}）".format(scene.name, trouble))
                continue
            if read_back != GRAYSCALE_LOOK_ID:
                failures.append("{}（Blender 拒绝了灰度 Look）".format(scene.name))
                continue
            try:
                scene[GRAYSCALE_STATE_PROPERTY] = True
            except Exception:
                pass
            changed += 1

        if changed == 0:
            self.report({'WARNING'},
                        "未能打开灰度滤镜：{}".format("；".join(failures) or "当前文件里没有可改的场景"))
            return {'CANCELLED'}

        if failures:
            self.report({'WARNING'},
                        "已开启灰度滤镜（{} 个场景）；注意：这也会影响最终渲染输出；"
                        "{} 个场景没能改（{}）".format(
                            changed, len(failures), "；".join(failures[:2])))
        else:
            self.report({'INFO'},
                        "已开启灰度滤镜（{} 个场景）；注意：这也会影响最终渲染输出".format(changed))
        return {'FINISHED'}

    def _turn_off(self, scenes):
        restored = 0
        missing = []
        for scene in scenes:
            settings = _scene_view_settings(scene)
            if settings is None:
                continue
            enabled, previous, recorded = grayscale_preview_state(scene)
            try:
                if recorded:
                    settings.look = previous
                elif enabled or _current_look(settings) == GRAYSCALE_LOOK_ID:
                    # 状态属性丢失（例如场景是从旧文件合并进来的）：退回 None
                    settings.look = "None"
                else:
                    continue
            except Exception as trouble:
                missing.append("{}（{}）".format(scene.name, trouble))
                continue
            for key in (GRAYSCALE_STATE_PROPERTY, GRAYSCALE_PREVIOUS_LOOK_PROPERTY):
                try:
                    if key in scene.keys():
                        del scene[key]
                except Exception:
                    pass
            restored += 1

        if restored == 0:
            if missing:
                self.report({'WARNING'},
                            "没能还原灰度滤镜：{}".format("；".join(missing[:2])))
            else:
                self.report({'WARNING'}, "灰度滤镜并没有开着，无需关闭")
            return {'CANCELLED'}

        if missing:
            self.report({'WARNING'},
                        "已关闭灰度滤镜，{} 个场景的原始 Look 已还原；"
                        "{} 个场景没能还原（{}）".format(
                            restored, len(missing), "；".join(missing[:2])))
        else:
            self.report({'INFO'},
                        "已关闭灰度滤镜，{} 个场景的原始 Look 已还原".format(restored))
        return {'FINISHED'}


# ------------------------------------------------------------- 一键选中当前相机


def _view_layer_objects(context):
    """当前视图层里能被遍历到的物体。

    用 ``context.view_layer.objects``（视图层 + 当前集合可见范围），
    而不是 ``bpy.data.objects`` —— 被排除出视图层的物体本来就不该被这一步碰到。
    ``--background`` 下同样能取到，所以这条路径无头可测。
    """
    view_layer = getattr(context, "view_layer", None)
    if view_layer is None:
        return []
    try:
        return list(view_layer.objects)
    except Exception:
        return []


def resolve_view_camera(space_camera, view_perspective, scene_camera):
    """算出「我现在正在用它看场景的那台相机」—— **纯函数**，不碰 bpy。

    用户要的语义是「选中的相机 = 当前正在使用的相机视角的相机」，不是简单的
    ``scene.camera``：3D 视口可以有**独立的相机覆盖**，这时那个视口看的是
    ``space_data.camera``，与场景活动相机不是同一台。

    解析顺序（判定依据必须按这个顺序，先看视角、再看视口相机是否有效、最后回退）：

    1. 视口**正处于相机视角**（``region_3d.view_perspective == 'CAMERA'``）
       且该视口的相机**有效**（``space_data.camera`` 不是 None）
       → 返回**视口相机**（哪怕它和场景活动相机不是同一台）。
    2. 否则（自由 / 用户视角、不在 3D 视口、无头环境，或视口相机为 None）
       → 回退返回**场景活动相机**（``scene.camera``，也就是按 Numpad 0 会进入的那台）。
    3. 两者都没有 → 返回 ``None``，由调用方给出友好提示并 ``CANCELLED``。

    返回值是 ``(相机, 来源)``；来源取值 ``"viewport"`` / ``"scene"``，
    调用方据此在报告里说清是哪一种，用户才能判断对不对。
    逻辑与 bpy 完全解耦，所以无头环境能逐条穷举测试。
    """
    if view_perspective == 'CAMERA' and space_camera is not None:
        return space_camera, "viewport"
    if scene_camera is not None:
        return scene_camera, "scene"
    return None, ""


class HHZ_OT_select_active_camera(bpy.types.Operator):
    """一键选中当前相机：把当前视角使用的相机选成唯一选中物体并设为活动物体。"""

    bl_idname = "hhz.select_active_camera"
    bl_label = "一键选中当前相机"
    bl_description = (
        "选中当前正在使用的相机视角的那台相机：在相机视角下取该视口的相机，"
        "否则取场景活动相机；取消其它物体选择，只留它并设为活动物体；"
        "相机被隐藏或不可见时如实报告，不假装选中"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def _view_camera_arguments(self, context):
        """把上下文里与「当前视角相机」有关的三个值取出来，交给纯函数解析。"""
        space_camera = None
        view_perspective = ""
        space = getattr(context, "space_data", None)
        if space is not None and getattr(space, "type", "") == 'VIEW_3D':
            space_camera = getattr(space, "camera", None)
            region_3d = getattr(context, "region_data", None)
            if region_3d is None:
                region_3d = getattr(space, "region_3d", None)
            view_perspective = getattr(region_3d, "view_perspective", "") or ""
        scene = getattr(context, "scene", None)
        return space_camera, view_perspective, getattr(scene, "camera", None)

    def execute(self, context):
        space_camera, view_perspective, scene_camera = self._view_camera_arguments(context)
        camera, origin = resolve_view_camera(space_camera, view_perspective, scene_camera)

        # 两者都没有：友好提示 + CANCELLED，不抛异常
        if camera is None:
            self.report({'WARNING'}, "当前没有可用的相机（场景没有设置活动相机）")
            return {'CANCELLED'}

        view_layer = getattr(context, "view_layer", None)
        if view_layer is None:
            self.report({'WARNING'}, "当前上下文没有视图层，无法选中相机")
            return {'CANCELLED'}

        # 数据 API 选物体：不能用 bpy.ops.object.select_all（那需要界面上下文）
        members = _view_layer_objects(context)
        skipped_names = []
        for obj in members:
            if obj == camera:
                continue
            try:
                if obj.select_get():
                    obj.select_set(False)
            except Exception:
                # 个别物体改不动（隐藏 / 特殊状态）不影响主流程，只记录
                skipped_names.append(obj.name)

        error = None
        try:
            camera.select_set(True)
            context.view_layer.objects.active = camera
        except Exception as trouble:
            error = str(trouble)

        # 如实校验：选不上就报原因，绝不谎报成功
        selected = False
        try:
            selected = bool(camera.select_get())
        except Exception:
            selected = False

        if not selected or error:
            in_view_layer = any(obj == camera for obj in members)
            if bool(getattr(camera, "hide_viewport", False)):
                reason = "当前相机被隐藏，无法选中"
            elif not in_view_layer:
                reason = "当前相机不在当前视图层的可见集合里，无法选中"
            else:
                reason = "当前相机无法被选中（Blender 拒绝了选择）"
            self.report({'WARNING'}, reason)
            return {'CANCELLED'}

        active = getattr(view_layer.objects, "active", None)
        if active != camera:
            self.report({'WARNING'}, "当前相机无法设为活动物体")
            return {'CANCELLED'}

        # 报告里必须能看出相机来自哪种来源，用户才能判断选对了没有
        if origin == "viewport":
            source_text = "视口相机"
        else:
            source_text = "场景活动相机"
        if skipped_names:
            self.report({'WARNING'},
                        "已选中当前视角使用的相机：{}（{}，{} 个隐藏物体改不动）".format(
                            camera.name, source_text, len(skipped_names)))
        else:
            self.report({'INFO'}, "已选中当前视角使用的相机：{}（{}）".format(
                camera.name, source_text))
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
    bl_description = "一键套用一套配色（若按钮常态 / 选中色 / 状态色），或套用自己保存的「自定义预设」"

    preset: bpy.props.EnumProperty(
        name="预设",
        items=[(name, name, "套用「{}」配色".format(name))
               for name in theme.preset_button_names()],
    )

    def execute(self, context):
        current = theme.current_theme(context)
        if current is None:
            self.report({'ERROR'}, "读不到当前主题")
            return {'CANCELLED'}

        preferences = compat.get_preferences(context)
        # 进任何主题算符之前先把四个调整值的 16 个分量归一化一次（幂等）：
        # 历史遗留的垃圾分量（未初始化内存 / 越界 / NaN）在这里就被写回中性值。
        theme.normalize_adjust_preferences(preferences)
        # 色相 / 饱和度 / 明度 / 选中几何饱和度：统一走 theme.render_preset_colors。
        # 每次都从预设表（或已保存的自定义预设 JSON）重算，所以反复调整不会累积；
        # 四个值从**这套预设自己的槽位**读，所以套用 B 拿到的永远是 B 那一份。
        colors = theme.render_preset_colors(self.preset, preferences)
        if not colors:
            if self.preset == theme.CUSTOM_PRESET_NAME:
                # 从没保存过时给一条明确的下一步，而不是一句莫名的失败
                self.report({'WARNING'},
                            "还没有保存过自定义预设：先套一套预设、用四个滑杆调好，"
                            "再点「保存当前为自定义预设」")
            else:
                self.report({'WARNING'}, "这套预设没有可用颜色")
            return {'CANCELLED'}

        changed = theme.apply_colors(current, colors)
        if not changed:
            self.report({'WARNING'}, "这套预设没有可用颜色")
            return {'CANCELLED'}
        # 视口尺寸（不是颜色）：两套马拉松预设会把编辑模式选中边画粗一档
        # —— 颜色已经拉到调色板最高饱和度，剩下的可见性只能靠线宽。
        changed_sizes = theme.apply_sizes(current, theme.preset_sizes(self.preset))

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
    bl_label = "按当前调整重新套用"
    bl_description = ("用现在的色相 / 饱和度 / 明度 / 选中几何饱和度重新套用最近一次用过的预设"
                      "（从预设表重算，不会累积）")

    def execute(self, context):
        preferences = compat.get_preferences(context)
        current = theme.current_theme(context)
        if preferences is None or current is None:
            self.report({'ERROR'}, "读不到偏好设置或主题")
            return {'CANCELLED'}

        name = theme.active_preset(preferences)
        theme.normalize_adjust_preferences(preferences)
        colors = theme.render_preset_colors(name, preferences)
        if not colors:
            self.report({'WARNING'}, "没有可重新套用的预设")
            return {'CANCELLED'}

        applied = theme.apply_colors(current, colors)
        # 视口尺寸与颜色一样"从预设表重算"，所以重复点击同样不累积
        theme.apply_sizes(current, theme.preset_sizes(name))
        preferences.last_preset = name
        theme.read_into_preferences(context, preferences)
        hue, saturation, value, _selection = theme.preset_adjust(preferences, name)
        self.report({'INFO'}, "已按 色相 {:+.0f} 度 / 饱和度 {}% / 明度 {}% 重新套用「{}」（{} 项）".format(
            hue, int(round(saturation * 100)), int(round(value * 100)), name, applied))
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

    #: 由面板「保存当前为自定义预设」按钮传进来的输入（**不画在界面上、不进偏好设置**，
    #: 所以没有新增设置项）：为 True 时存的是「当前预设 + 当前四个调整」的色表，
    #: 用保留名 :data:`theme.CUSTOM_PRESET_NAME` 写进**既有的**自定义配色 JSON。
    preset_mode: bpy.props.BoolProperty(
        name="存为自定义预设",
        description="把当前预设与四个滑杆的调整存成第 4 个预设「自定义预设」（复用同一份自定义配色数据）",
        default=False,
        options={'HIDDEN'},
    )

    def execute(self, context):
        preferences = compat.get_preferences(context)
        current = theme.current_theme(context)
        if preferences is None or current is None:
            self.report({'ERROR'}, "读不到偏好设置或主题")
            return {'CANCELLED'}

        if self.preset_mode:
            return self._save_preset_mode(context, preferences)

        theme.normalize_adjust_preferences(preferences)
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

    def _save_preset_mode(self, context, preferences):
        """「保存当前为自定义预设」：当前预设 + 它自己的四个调整 -> 保留名。

        存的是**当前预设的底表**（未调整的那一份），四个调整值则**原样固化进
        「自定义预设」自己的槽位**（2.15.0 起每个预设各存一份）。所以套用它 =
        底表 + 它自己那份调整 = 保存当时看到的颜色，**既不会叠加第二次，也不会累积**；
        保存后滑杆显示的就是这批刚固化的值（它们属于自定义预设自己的槽位），
        源预设的那一份一个字都不动（改它的时候是什么样，回头套用它还是什么样）。
        """
        name = theme.active_preset(preferences)
        theme.normalize_adjust_preferences(preferences)
        hue, saturation, value, selection = theme.preset_adjust(preferences, name)
        colors = theme.preset_colors(name, getattr(preferences, "theme_custom", ""))
        if not colors:
            self.report({'WARNING'},
                        "没有可保存的配色：先套一套预设（或先保存一次自定义预设），再点这个按钮")
            return {'CANCELLED'}

        text, ok = theme.custom_save(getattr(preferences, "theme_custom", ""),
                                     theme.CUSTOM_PRESET_NAME, colors)
        if not ok:
            self.report({'WARNING'}, "保存失败（颜色为空）")
            return {'CANCELLED'}

        preferences.theme_custom = text
        preferences.last_preset = theme.CUSTOM_PRESET_NAME
        theme.write_preset_adjust(preferences, theme.CUSTOM_PRESET_NAME,
                                  (hue, saturation, value, selection))
        theme.read_into_preferences(context, preferences)
        self.report({'INFO'},
                    "已把「{}」+ 当前四个调整存为自定义预设（{} 项颜色）；"
                    "四个调整已固化进自定义预设自己那一份".format(name, len(colors)))
        compat.tag_redraw(context)
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

        # 追加工作区的**来源文件**：优先用用户自己已经保存过的那个文件
        # （上面 is_dirty 那一步已经保证了它有 filepath）。
        #
        # 为什么不再"先写一个只含工作区的部分库"：实测 Blender **4.5.14 上
        # ``bpy.data.libraries.write(<文件>, <工作区集合>)`` 必定段错误**
        # （本机 4.5.14 复现：fake_user 真/假、只传一个工作区、不传关键字，
        # 四种写法全崩，栈顶 ``PartialWriteContext::id_add_copy``；CI 上同一条崩溃，
        # exit 139）。而从**全量 .blend** 里 ``libraries.load`` 追加工作区是好的
        # （同版本探针实测通过），所以直接用用户的原文件当来源，不再写库。
        source_path = original_path
        if not source_path:
            # 极少数情况（文件还没保存过）：退回部分库。这条路上要追加的清单通常
            # 是空的（会话与启动文件里的工作区本来就一样）。
            library = layout_library_path()
            try:
                bpy.data.libraries.write(library, set(bpy.data.workspaces), fake_user=True)
            except Exception as error:
                self.report({'ERROR'}, "写入布局库失败：{}".format(error))
                return {'CANCELLED'}
            source_path = library

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
            with bpy.data.libraries.load(source_path, link=False) as (source, target):
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
            # 走 target_from_screen_drag：mouse_region_y 以左下角为原点、Y 向上，
            # 直接用它的差值会把拖动方向弄反（2.16.1 修的方向 bug）
            target = panels.target_from_screen_drag(
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


# ------------------------------------------------- ZML_LAB 一键更新（2.19.0）
#
# 需求：从源目录里自动找出 ZML_LAB 压缩包，把它**剪切**到插件 zip 库，
# 再用它替换已安装的 ZML_LAB 版本，并且**原有设置与快捷键不能变**。
#
# 快捷键为什么不丢（这是整个功能的核心机制）：
#   插件偏好设置（ZML_LAB 自己的快捷键设置就在里面）在 Blender 里是按**模块名**
#   存的。包内顶层文件夹名一变，Blender 就把它当成另一个插件，原来那套设置归不到
#   新模块名下 —— 用户看到的就是"每次更新快捷键都没了"。
#   所以本功能有一条硬守卫：候选包的模块名必须与已安装的 ZML_LAB 模块名一致，
#   不一致就**拒绝执行、不动任何文件**（``zml_module_guard``）。
#
# 这一段的约定：
#   * 扫描 / 校验 / 计划全部是**纯函数**，不依赖真实 addons 目录，可以无头测试；
#   * 只用标准库（``zipfile`` / ``shutil`` / ``os``），不引入任何外部依赖；
#   * **不写** ``userpref.blend``（绝不调用 ``wm.save_userpref``）；
#   * **不碰** keymap、**不删**用户的其它插件。

#: 备份目录名（放在 addons 目录下面；没有 ``__init__.py``，Blender 不会把它当插件）
ZML_BACKUP_DIRNAME = "_zml_backup"

#: 读包内源文件的上限（超过就不读，避免一个畸形包把内存吃光）
_ZML_MAX_SOURCE_BYTES = 4 * 1024 * 1024


def zml_lab_identity(name):
    """把文件夹名归一化成 ZML_LAB 身份键；不是 ZML_LAB 名字时返回 None。

    规则（按需求）：**不区分大小写**、**允许下划线**、**允许版本后缀**，
    所以 ``ZML_LAB`` / ``zml_lab_ultimate_max_v100`` / ``ZML-LAB-Test`` 都认，
    而 ``redkolab`` / ``Boxcutter`` 一律不认。
    """
    text = str(name or "").strip().replace("-", "_").replace(" ", "_").lower()
    if not text:
        return None
    if not text.replace("_", "").startswith("zmllab"):
        return None
    return text


def zml_same_module(left, right):
    """两个模块名是否是**同一个模块**（大小写 / 下划线与连字符差异不算不同）。"""
    left_key = zml_lab_identity(left)
    right_key = zml_lab_identity(right)
    if left_key is None or right_key is None:
        return False
    return left_key == right_key


def _zml_decode(raw):
    """尽力把包内源码解码成文本（ZML_LAB 的包里实测有 GBK 文件）。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


#: zip 成员名里的「名字是 UTF-8」标志位（APPNOTE 4.4.4 / zip 通用标志 bit 11）
_ZML_ZIP_UTF8_FLAG = 0x800

#: 下拉标签的最大长度。教训（2.14.0 颜色项乱码）：中文长标签在弹出框里会被
#: Blender 按像素宽度从中间截断，半个汉字就画成乱码 —— 所以标签必须短。
_ZML_LABEL_LIMIT = 32

#: 下拉里的占位标识符（没有候选 / 扫描失败）。必须是纯 ASCII 的稳定字符串
ZML_NONE_ID = "__none__"
ZML_SCAN_FAILED_ID = "__scan_failed"


def _zml_has_control(text):
    """文本里有没有控制字符 / 代理项（这种名字不能当模块名，也不能进 UI）。"""
    try:
        for char in str(text or ""):
            code = ord(char)
            if code < 0x20 or code == 0x7F or 0xD800 <= code <= 0xDFFF:
                return True
    except Exception:
        return True
    return False


def _zml_clean_text(text, limit=64):
    """把任意文本清成「能安全交给 Blender 画」的样子：去掉控制字符与代理项，
    中点号换成 ``-``。**绝不抛异常**。"""
    try:
        raw = str(text or "")
    except Exception:
        return ""
    chars = []
    for char in raw:
        code = ord(char)
        if code < 0x20 or code == 0x7F or 0xD800 <= code <= 0xDFFF:
            continue
        if char == "\u00b7":          # 中点号：用户换过界面字体后缺字会画成乱码
            chars.append("-")
            continue
        chars.append(char)
    try:
        return "".join(chars).strip()[:limit]
    except Exception:
        return ""


def zml_decode_name(name, utf8_flag=None):
    """把 zip 成员名解码成可读文本；**绝不抛异常**。

    为什么要它（2.21.1 修复）：``zipfile`` 只在成员名打了 UTF-8 标志
    （``flag_bits & 0x800``）时才按 UTF-8 解码，**没打标志就一律按 CP437 解**。
    中文 Windows 上打的包常常是「GBK 字节 + 不打标志」，于是 ``ZML_LAB_椎木兰``
    读回来是 ``ZML_LAB_╫╡─╫╘╜╙`` 这种乱码 —— 而且它会**冒充**一个合法模块名，
    被后续的同族改名、确认文案、下拉标签一路用下去。

    解码顺序（按需求）：
      1. ``utf8_flag`` 为真 → ``zipfile`` 已按 UTF-8 解过，原样返回；
      2. 纯 ASCII → 原样返回；
      3. 把文本按 CP437 **编回原始字节**，再依次用 ``utf-8`` → ``gbk`` 试探解码；
      4. 全都失败（或解出控制字符）→ 返回 CP437 那版原文（宁可变乱码，也不抛）。
    """
    if name is None:
        return ""
    if isinstance(name, (bytes, bytearray)):
        raw = bytes(name)
        for encoding in ("utf-8", "gbk", "cp437"):
            try:
                decoded = raw.decode(encoding)
            except Exception:
                continue
            if not _zml_has_control(decoded):
                return decoded
        return raw.decode("cp437", "replace")
    try:
        text = str(name)
    except Exception:
        return ""
    if not text:
        return ""
    if utf8_flag:
        return text
    try:
        text.encode("ascii")
        return text
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    except Exception:
        return text
    # 没有 UTF-8 标志：zipfile 是拿 CP437 解的，编回去就拿到原始字节
    try:
        raw = text.encode("cp437")
    except Exception:
        return text
    for encoding in ("utf-8", "gbk"):
        try:
            decoded = raw.decode(encoding)
        except Exception:
            continue
        if not _zml_has_control(decoded):
            return decoded
    return text


def zml_version_tag(module):
    """模块名尾部的版本序号 → 短标签（``..._V107`` → ``V1.07``）；没有后缀返回空串。

    只做机械换算：三位数字按 ``V<首位>.<后两位>``（V107 → V1.07、V076 → V0.76）。
    尾部不是纯数字（例如 ``..._V107beta``）就不认，返回空串 —— 不猜。
    """
    key = zml_lab_identity(module) or ""
    found = re.search(r"v(\d+)$", key)
    if not found:
        return ""
    digits = found.group(1)
    if len(digits) <= 3:
        digits = digits.zfill(3)
        return "V{}.{}".format(digits[:-2], digits[-2:])
    return "V" + digits


def zml_candidate_label(module, version="", path=""):
    """下拉里的**短标签**：``V1.07 - 1.7``（短、无中点号、无控制字符、截断到 32 字）。

    ``ZML_LAB`` 前缀故意省掉：文件名那一长串在弹出框里会被截断成乱码。
    """
    tag = zml_version_tag(module)
    parts = []
    if tag:
        parts.append(tag)
    clean_version = _zml_clean_text(version, 16)
    if clean_version:
        parts.append(clean_version)
    if not parts:
        parts.append(_zml_clean_text(os.path.basename(str(path or "")), 24) or "ZML_LAB 包")
    return _zml_clean_text(" - ".join(parts), _ZML_LABEL_LIMIT)


def zml_candidate_description(entry):
    """候选包的悬停说明（含真实文件名与模块名，方便核对；不含控制字符）。"""
    entry = entry or {}
    name = _zml_clean_text(os.path.basename(entry.get("path") or ""), 64) or "（文件名读不出）"
    module = _zml_clean_text(entry.get("module") or "", 64) or "（读不出）"
    version = _zml_clean_text(entry.get("version") or "", 16) or "（读不出）"
    return "文件 {}；模块名 {}；版本 {}；更新会剪切这个压缩包并替换已安装版本".format(
        name, module, version)


def zml_candidate_id(path, module):
    """候选包的枚举标识符：**纯 ASCII**（哈希），绝不含路径 / 中文 / 控制字符。

    为什么必须是纯 ASCII（2.21.1 崩溃的直接原因）：旧版直接把**完整路径**当枚举
    标识符，路径里有中文时 Blender 的枚举项缓存会变成乱码字节，面板 ``draw()``
    每次读 ``preferences.zml_candidate`` 都抛
    ``UnicodeDecodeError: 'utf-8' codec can't decode byte 0x01 in position 0``，
    下拉与「一键更新」按钮一起废掉。

    标识符只由「归一化模块名 + 文件名」哈希而来，跟目录无关、可复现、稳定。
    """
    name = os.path.basename(str(path or "")).lower()
    seed = "{}|{}".format(zml_lab_identity(module) or "", name)
    try:
        digest = hashlib.sha1(seed.encode("utf-8", "backslashreplace")).hexdigest()
    except Exception:
        digest = hashlib.sha1(name.encode("ascii", "ignore")).hexdigest()
    return "zml_{}".format(digest[:12])


def zml_zip_inspect(zip_path):
    """只读检查一个 zip：返回 ``{"module", "version", "problem"}``。

    ``problem`` 为空串表示「是合法的 ZML_LAB 包」；否则是**具体原因**（打不开 /
    文件名解码失败 / 没有 ``__init__.py`` / 模块名含控制字符），用来回答
    「这个包为什么不算候选」，绝不返回空原因。
    """
    result = {"module": None, "version": "", "problem": ""}
    path = str(zip_path or "")
    if not path:
        result["problem"] = "没有给出压缩包路径"
        return result
    try:
        if not os.path.isfile(path):
            result["problem"] = "文件不存在或不是文件"
            return result
    except OSError:
        result["problem"] = "文件路径读不了"
        return result
    try:
        archive = zipfile.ZipFile(path)
    except Exception as error:
        result["problem"] = "压缩包打不开（{}）".format(error.__class__.__name__)
        return result
    try:
        with archive:
            try:
                infos = archive.infolist()
            except Exception as error:
                result["problem"] = "包内文件名解码失败（{}）".format(
                    error.__class__.__name__)
                return result
            if not infos:
                result["problem"] = "空压缩包（没有任何条目）"
                return result
            modules = {}
            manifests = {}
            for info in infos:
                try:
                    decoded = zml_decode_name(
                        info.filename, info.flag_bits & _ZML_ZIP_UTF8_FLAG)
                except Exception:
                    continue
                parts = decoded.split("/")
                if len(parts) != 2 or not parts[0]:
                    continue
                if parts[1] == "__init__.py":
                    if zml_lab_identity(parts[0]) is None:
                        continue
                    if _zml_has_control(parts[0]):
                        result["problem"] = "模块名解码失败（含控制字符）"
                        continue
                    modules.setdefault(parts[0], info)
                elif parts[1] == "blender_manifest.toml":
                    manifests.setdefault(parts[0], info)
            if not modules:
                if not result["problem"]:
                    result["problem"] = ("包内没有 ZML_LAB 模块文件夹"
                                         "（缺 <模块名>/__init__.py）")
                return result
            module = sorted(modules)[0]
            result["module"] = module
            version = ""
            try:
                info = modules[module]
                if info.file_size <= _ZML_MAX_SOURCE_BYTES:
                    version = _zml_bl_info_version(_zml_decode(archive.read(info)))
            except Exception:
                version = ""
            if not version:
                try:
                    info = manifests.get(module)
                    if info is not None and info.file_size <= _ZML_MAX_SOURCE_BYTES:
                        version = _zml_manifest_version(_zml_decode(archive.read(info)))
                except Exception:
                    version = ""
            result["version"] = version
            return result
    except Exception as error:
        result["problem"] = "读取压缩包失败（{}）".format(error.__class__.__name__)
        return result


def _zml_version_text(value):
    if isinstance(value, (tuple, list)):
        return ".".join(str(part) for part in value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _zml_bl_info_version(text):
    """从 ``__init__.py`` 源码里读 ``bl_info['version']``；读不到返回空字符串。

    读不到就留空 —— **不猜**版本号。
    """
    if not text:
        return ""
    try:
        tree = ast.parse(text)
    except Exception:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "bl_info" not in names:
                continue
            try:
                info = ast.literal_eval(node.value)
            except Exception:
                continue
            if isinstance(info, dict):
                return _zml_version_text(info.get("version"))
    # 回退：AST 读不动（编码怪 / 语法非常规）时，只在 bl_info 那一段里找 version
    block = re.search(r"bl_info\s*=\s*\{(?P<body>.*?)\n\s*\}", text, re.S)
    if block:
        found = re.search(r"[\"']version[\"']\s*:\s*\(([^)]*)\)", block.group("body"))
        if found:
            parts = [part.strip().strip("'\"") for part in found.group(1).split(",")]
            return _zml_version_text(tuple(part for part in parts if part))
    return ""


def _zml_manifest_version(text):
    """从 ``blender_manifest.toml`` 文本里读 version；读不到返回空字符串。"""
    if not text:
        return ""
    found = re.search(r"(?m)^\s*version\s*=\s*[\"']([^\"']+)[\"']", text)
    return found.group(1).strip() if found else ""


def zml_zip_module(zip_path):
    """读一个 zip 的 ``(模块名, 版本)``；不是 ZML_LAB 包时返回 ``(None, "")``。

    只读：只打开一次压缩包、读两个候选文件，不改磁盘上的任何东西。
    判定"是 ZML_LAB 包"的机械条件（按需求）：包内存在**顶层模块文件夹**，
    也就是 ``<模块名>/__init__.py`` 这一项，且该文件夹名匹配 ZML_LAB。
    成员名一律先过 :func:`zml_decode_name`（修掉「不打 UTF-8 标志的中文名」）。
    """
    try:
        info = zml_zip_inspect(zip_path)
    except Exception:
        return (None, "")
    module = info.get("module")
    if not module:
        return (None, "")
    return (module, info.get("version") or "")



def zml_scan_detail(source_dir):
    """扫描源目录，返回 ``{"source", "total", "entries", "problems"}``；**绝不抛异常**。

    ``entries``：``[{"path","module","version","mtime","id"}]``，按 mtime 倒序
    （``id`` 是纯 ASCII 的下拉标识符，见 :func:`zml_candidate_id`）
    ``problems``：``[(文件名, 原因)]`` —— 看着像压缩包但校验没过的，原因写具体
    ``total``：源目录里 zip 的总数（用来回答「找到 N 个 zip，其中 M 个没通过」）
    """
    detail = {"source": str(source_dir or ""), "total": 0, "entries": [], "problems": []}
    if not detail["source"] or not os.path.isdir(detail["source"]):
        return detail
    try:
        names = os.listdir(detail["source"])
    except OSError as error:
        detail["problems"].append(
            ("（目录）", "列不出目录内容（{}）".format(error.__class__.__name__)))
        return detail
    for name in sorted(names):
        if not name.lower().endswith(".zip"):
            continue
        path = os.path.join(detail["source"], name)
        try:
            if not os.path.isfile(path):
                continue
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        detail["total"] += 1
        try:
            info = zml_zip_inspect(path)
        except Exception:
            info = {"module": None, "version": "", "problem": "检查时抛出异常"}
        if not info.get("module") or info.get("problem"):
            detail["problems"].append(
                (name, info.get("problem") or "读不出模块名"))
            continue
        detail["entries"].append({
            "path": path,
            "module": info["module"],
            "version": info.get("version") or "",
            "mtime": mtime,
            "id": zml_candidate_id(path, info["module"]),
        })
    detail["entries"].sort(key=lambda item: (-item["mtime"], item["module"]))
    return detail


def zml_scan(source_dir):
    """扫描源目录里的 ZML_LAB 压缩包，返回 ``[(zip 路径, 模块名, 版本, mtime)]``。

    按 **mtime 倒序**（最新改动的排最前；mtime 相同再按模块名排，保证结果稳定）。
    校验不通过的 zip（空包、无关包、模块名不含 ZML_LAB、名字解码失败）不会出现在
    结果里，具体原因见 :func:`zml_scan_detail` 的 ``problems``。
    """
    detail = zml_scan_detail(source_dir)
    return [(entry["path"], entry["module"], entry["version"], entry["mtime"])
            for entry in detail["entries"]]


def zml_package_items(source_dir):
    """下拉项：``[(ASCII id, 短标签, 悬停说明)]``；没有候选时给一项占位说明。

    标签只用短版（``V1.07 - 1.7``），**不用完整路径/文件名**：长中文标签在弹出框里
    会被 Blender 按像素宽度截断成乱码。同标签时补 ``(2)`` ``(3)`` 区分。
    绝不抛异常（外层 UI 回调还会再包一层 try / except）。
    """
    items = []
    try:
        detail = zml_scan_detail(source_dir)
        if not detail["entries"]:
            return [(ZML_NONE_ID, "（没有找到 ZML_LAB 压缩包）",
                     "源目录里没有通过校验的 ZML_LAB 压缩包")]
        used = {}
        for entry in detail["entries"]:
            label = zml_candidate_label(entry["module"], entry["version"], entry["path"])
            used[label] = used.get(label, 0) + 1
            if used[label] > 1:
                label = _zml_clean_text("{} ({})".format(label, used[label]),
                                        _ZML_LABEL_LIMIT)
            items.append((entry["id"], label, zml_candidate_description(entry)))
    except Exception:
        return [(ZML_SCAN_FAILED_ID, "（扫描失败，详见控制台）",
                 "扫描 ZML_LAB 压缩包时出错，详情已打印到控制台")]
    return items


def zml_resolve_candidate(source_dir, value):
    """把下拉里的值反查成扫描结果里的真实 zip：返回 ``(entry, detail)``。

    按需求：算符**不依赖名字**，而是从扫描结果表里按 ASCII id 反查真实路径。
    为了兼容 2.21.0 及更早版本存下的「完整路径」，值本身是存在的文件时也认。
    找不到时 ``entry`` 为 ``None``，``detail`` 带扫描现状（供报告使用）。
    """
    text = str(value or "").strip()
    detail = zml_scan_detail(source_dir)
    if not text:
        return (None, detail)
    for entry in detail["entries"]:
        if entry["id"] == text:
            return (entry, detail)
    # 旧版存的是完整路径（也接受用户手工填一个路径）
    try:
        if os.path.isfile(text):
            for entry in detail["entries"]:
                try:
                    if os.path.normcase(os.path.abspath(entry["path"])) == \
                            os.path.normcase(os.path.abspath(text)):
                        return (entry, detail)
                except OSError:
                    continue
            return ({"path": text, "module": "", "version": "", "mtime": 0.0,
                     "id": ""}, detail)
    except OSError:
        pass
    return (None, detail)


def zml_missing_candidate_message(source_dir, value, detail=None):
    """算符找不到候选时的报告文案：说清扫到了什么、为什么不算候选。

    **绝不留空冒号**（2.21.0 的缺陷：报告只有「找不到要更新的 ZML_LAB 压缩包：」）。
    """
    source = str(source_dir or "")
    shown = _zml_clean_text(value, 64)
    if not source:
        return ("还没有设置 ZML_LAB 源目录（偏好设置里选一个放 ZML_LAB 压缩包的目录）")
    if not os.path.isdir(source):
        return "ZML_LAB 源目录不存在：{}".format(source)
    if detail is None:
        detail = zml_scan_detail(source)
    total = detail.get("total", 0)
    entries = detail.get("entries") or []
    problems = detail.get("problems") or []
    parts = []
    if total:
        parts.append("在 {} 找到 {} 个 zip".format(source, total))
    else:
        parts.append("源目录 {} 里一个 zip 都没有".format(source))
    if problems:
        shown_problems = "；".join("{}：{}".format(name, reason)
                                  for name, reason in problems[:4])
        parts.append("其中 {} 个像 ZML_LAB 但校验未通过（{}）".format(
            len(problems), shown_problems))
        if len(problems) > 4:
            parts.append("只列出前 4 个")
    if entries:
        names = " / ".join(os.path.basename(entry["path"]) for entry in entries[:3])
        if not shown:
            parts.append("下拉里还没有选择候选包，请先在下拉里选一个"
                         "（现在有 {} 个候选：{}）".format(len(entries), names))
    else:
        parts.append("所以没有可以更新的候选包")
    if shown and not any(entry["id"] == shown for entry in entries):
        parts.append("当前选择 {} 不在扫描结果里，请重新选一个".format(shown))
    return "；".join(part for part in parts if part)


def zml_addons_dir():
    """Blender 当前使用的 addons 目录（只读，不改）。"""
    try:
        return bpy.utils.user_resource('SCRIPTS', path='addons') or ""
    except Exception:
        return ""


def zml_installed_modules(addons_dir):
    """只读列出目录里与 ZML_LAB 同族的已安装模块文件夹名。

    跳过 ``_zml_backup`` 这种以下划线开头的目录、以及没有 ``__init__.py`` 的目录
    （后者本来就不是插件）。
    """
    addons_dir = str(addons_dir or "")
    if not addons_dir or not os.path.isdir(addons_dir):
        return []
    found = []
    try:
        entries = sorted(os.listdir(addons_dir))
    except OSError:
        return []
    for name in entries:
        if name.startswith(".") or name.startswith("_"):
            continue
        path = os.path.join(addons_dir, name)
        try:
            if not os.path.isdir(path):
                continue
        except OSError:
            continue
        if not os.path.isfile(os.path.join(path, "__init__.py")):
            continue
        if zml_lab_identity(name) is None:
            continue
        found.append(name)
    return found


def zml_module_family(name):
    """模块名的「同族键」：把尾部的版本后缀去掉之后的归一化名字。

    为什么需要它（2.20.0 的核心）：ZML_LAB 每次发版都换顶层文件夹名
    （``..._V100`` → ``..._V107``），而 Blender 是按**模块名（= 顶层文件夹名）**
    归属插件设置与快捷键的。所以"新版的文件夹名不等于已安装的文件夹名"并不代表
    它是另一个插件 —— 去掉版本后缀后它们是**同一个家族**，把新代码装进旧文件夹
    才能既升级又保住设置与快捷键。

    机械规则（可断言）：
      * 先按 ZML_LAB 身份规则归一化（大小写、连字符、空格、下划线都不敏感）；
      * 只有尾部是 ``v`` + **纯数字**（可带一个前置下划线）才算版本后缀，去掉它；
      * ``..._V107beta`` 这种尾部不是纯数字的**不算**版本后缀，因此与 ``..._V100``
        不同族（宁可不认，也不要猜）。
    """
    key = zml_lab_identity(name)
    if key is None:
        return None
    stripped = re.sub(r"v\d+$", "", key).strip("_")
    return stripped or None


def zml_module_ordinal(name):
    """模块名里的版本序号（``..._V107`` → 107）；没有版本后缀时返回 ``-1``。"""
    key = zml_lab_identity(name)
    if key is None:
        return -1
    found = re.search(r"v(\d+)$", key)
    return int(found.group(1)) if found else -1


def zml_target_module(installed_modules, enabled_modules, candidate_module):
    """算出「该装进哪个旧模块名」，返回 dict。

    键：``allowed`` / ``target`` / ``exact`` / ``family`` / ``reason``。

    选择顺序（按需求）：
      1. 候选模块名**已在**已安装列表里 → 直接用它（原样覆盖，``exact=True``）；
      2. 否则找**同族**（``zml_module_family`` 相同）的已安装模块：
         * 其中**当前已启用**的优先（用户实际在用的那个）；
         * 都没有启用就选**版本序号最高**的（序号相同再按名字排，保证结果确定）；
      3. 没有同族 → 拒绝（``allowed=False``）：装下去会成为新插件，
         原有设置与快捷键无法继承。
    """
    candidate = str(candidate_module or "").strip()
    installed = [name for name in (installed_modules or []) if name]
    enabled = [name for name in (enabled_modules or []) if name]
    result = {"allowed": False, "target": "", "exact": False, "family": "", "reason": ""}

    if zml_lab_identity(candidate) is None:
        result["reason"] = ("候选包读不出 ZML_LAB 模块名，无法判断能不能继承设置与快捷键，"
                            "已停止，没有移动或删除任何文件")
        return result
    if not installed:
        result.update(allowed=True, target=candidate, exact=True,
                      reason="本机还没装过 ZML_LAB，这将是首次安装（模块名 {}）".format(candidate))
        return result

    for name in installed:
        if zml_same_module(name, candidate):
            result.update(
                allowed=True, target=name, exact=True, family=zml_module_family(name) or "",
                reason="模块名与已安装的 {} 一致，直接覆盖它（设置与快捷键归在同一个模块名下）".format(name))
            return result

    family = zml_module_family(candidate)
    same_family = [name for name in installed if zml_module_family(name) == family]
    result["family"] = family or ""
    if same_family:
        chosen = ""
        for name in same_family:
            if any(zml_same_module(name, item) for item in enabled):
                chosen = name
                break
        if chosen:
            by = "当前已启用"
        else:
            chosen = sorted(same_family,
                            key=lambda item: (-zml_module_ordinal(item), item))[0]
            by = "版本序号最高"
        result.update(
            allowed=True, target=chosen,
            reason=("候选包的模块名是 {}，与已安装的 {} 同族（去掉版本后缀后相同），"
                    "按「{}」选用 {}：安装时会把新版顶层文件夹改名为 {}，"
                    "从而沿用同名插件，保住原有的设置与快捷键").format(
                        candidate, " / ".join(same_family), by, chosen, chosen))
        return result

    result["reason"] = (
        "找不到可继承的同族模块：候选包的模块名是 {}，本机已安装的是 {}，"
        "去掉版本后缀后不是同一个插件名，装下去会成为新插件，原有设置与快捷键"
        "无法继承，所以已经停止，没有移动或删除任何文件"
    ).format(candidate, " / ".join(installed))
    return result


def zml_module_guard(candidate_module, installed_modules, enabled_modules=None):
    """模块名守卫的入口：返回 ``(是否放行, 说明)``（内部走 ``zml_target_module``）。"""
    decision = zml_target_module(installed_modules, enabled_modules, candidate_module)
    return (bool(decision["allowed"]), decision["reason"])


def zml_enabled_modules(context=None):
    """当前**已启用**的插件模块名（只读；后台 / 未启用时返回空列表）。"""
    ctx = context if context is not None else bpy.context
    try:
        addons = ctx.preferences.addons
    except Exception:
        return []
    try:
        return [key for key in addons.keys()]
    except Exception:
        return []


def zml_rewrite_module(zip_path, target_module, out_path):
    """把候选 zip 的**顶层文件夹改名**后另存，返回 dict（只读原包、只写 ``out_path``）。

    为什么改名：Blender 按模块名（顶层文件夹名）归属插件设置与快捷键。新版包的
    文件夹名变了，直接装会变成另一个插件、设置全丢；所以把它的顶层文件夹改成
    已安装的那个名字再装 —— **代码是新的**（bl_info 版本仍是新版的），
    模块名不变，设置与快捷键就能继承。

    返回 ``{"path", "module", "entries", "version"}``：
    ``module`` / ``version`` 是从新 zip **回读**出来的值（读不回来就说明改名失败）。
    """
    target = str(target_module or "").strip()
    empty = {"path": "", "module": None, "entries": 0, "version": ""}
    if zml_lab_identity(target) is None:
        return empty
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            decoded = {}
            folders = []
            for info in infos:
                # 成员名先解码（修掉「不打 UTF-8 标志的中文名」），否则会按乱码
                # 模块名去找顶层文件夹，接着把包写成一堆乱码目录
                name = zml_decode_name(info.filename,
                                       info.flag_bits & _ZML_ZIP_UTF8_FLAG)
                decoded[id(info)] = name
                parts = name.split("/")
                if len(parts) == 2 and parts[1] == "__init__.py" \
                        and zml_lab_identity(parts[0]) is not None \
                        and not _zml_has_control(parts[0]):
                    folders.append(parts[0])
            if not folders:
                return empty
            source = sorted(set(folders))[0]
            prefix = source + "/"
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
                for info in infos:
                    name = decoded.get(id(info), info.filename)
                    if name == prefix:
                        out.writestr(target + "/", b"")
                        continue
                    data = archive.read(info)
                    if name.startswith(prefix):
                        out.writestr(target + "/" + name[len(prefix):], data)
                    else:
                        # 包内其它顶层条目（例如 __MACOSX）原样保留，不动
                        out.writestr(name, data)
    except Exception:
        return empty
    module, version = zml_zip_module(out_path)
    return {"path": out_path, "module": module, "entries": len(infos), "version": version}


def zml_library_entries(library_dir):
    """读库目录里所有 zip 的 ``(路径, 模块名, 版本)``；模块名读不出时给空字符串。"""
    entries = []
    library_dir = str(library_dir or "")
    if not library_dir or not os.path.isdir(library_dir):
        return entries
    try:
        names = sorted(os.listdir(library_dir))
    except OSError:
        return entries
    for name in names:
        if not name.lower().endswith(".zip"):
            continue
        path = os.path.join(library_dir, name)
        try:
            if not os.path.isfile(path):
                continue
        except OSError:
            continue
        module, version = zml_zip_module(path)
        entries.append((path, module or "", version))
    return entries


def zml_library_plan(library_dir, candidate_zip, candidate_module, entries=None):
    """纯函数：库目录现状 + 候选 → 要备份什么、移到哪里、删哪些旧包。

    ``entries`` 是 ``zml_library_entries()`` 的结果，也可以手工构造（测试用），
    所以这个函数本身不碰磁盘。

    判定规则：
      * 与候选**同一模块**（模块名归一化后完全相同）的旧包 = 被替换的旧版本
        → 先备份、再删；若它的文件名与候选相同，那它就在移动的目标位置上，
          不单独删（移动会覆盖它，备份已经做了）→ ``replace_in_place``
      * 文件名与候选相同、但**模块名不同**的包绝不覆盖 → 进 ``conflict``，
        调用方必须拒绝执行
      * 其余包一个都不动 → ``kept``
    """
    library = str(library_dir or "")
    name = os.path.basename(str(candidate_zip or ""))
    target = os.path.join(library, name) if name else ""
    replace_in_place = []
    delete = []
    kept = []
    conflict = []
    target_key = os.path.normcase(os.path.abspath(target)) if target else ""
    for entry in (entries or []):
        path = entry[0] if entry else ""
        module = entry[1] if entry and len(entry) > 1 else ""
        if not path:
            continue
        same_name = bool(target_key) and \
            os.path.normcase(os.path.abspath(path)) == target_key
        if zml_same_module(module, candidate_module):
            if same_name:
                replace_in_place.append(path)
            else:
                delete.append(path)
            continue
        if same_name:
            conflict.append(path)
            continue
        kept.append(path)
    return {
        "library_dir": library,
        "move_to": target,
        "replace_in_place": replace_in_place,
        "delete": delete,
        "backup": replace_in_place + delete,
        "kept": kept,
        "conflict": conflict,
    }


def zml_confirm_lines(candidate_zip, module, version, library_dir, installed=None,
                      target_module=""):
    """二次确认的文案（纯函数，方便测试）。

    按需求必须列出：源包名 / 模块名 / 版本 / 目标库目录 / 会移动（剪切）源文件 /
    会重新启用；同族改名时还要**明确写出**「把新版顶层文件夹 X 改名为 Y，
    以沿用同名插件，从而保留设置与快捷键」。
    文案里**不用中点号**（用户换过界面字体时缺字会变乱码）。
    """
    target = str(target_module or "").strip()
    lines = [
        "源压缩包：{}".format(os.path.basename(str(candidate_zip or "")) or "（没有选择）"),
        "模块名：{}".format(module or "（读不出）"),
        "版本：{}".format(version or "（读不出）"),
        "目标库目录：{}".format(library_dir or "（没有设置）"),
    ]
    if target and not zml_same_module(target, module):
        lines.append("将把新版顶层文件夹 {} 改名为 {}，以沿用同名插件，"
                     "从而保留设置与快捷键".format(module or "（读不出）", target))
        lines.append("模块名仍是 {}，里面装的是{}版的代码".format(
            target, " {} ".format(version) if version else "新"))
    elif target:
        lines.append("模块名与已安装的 {} 一致，直接覆盖它".format(target))
    lines.append("会把源压缩包剪切（移动）到库目录，源目录里不再保留")
    lines.append("会停用再重新启用该模块，让插件自己的快捷键重新注册")
    installed = [name for name in (installed or []) if name]
    if installed:
        lines.append("本机已安装：{}".format(" / ".join(installed)))
    else:
        lines.append("本机还没装过 ZML_LAB，这将是首次安装")
    return lines


class HHZ_OT_zml_lab_update(bpy.types.Operator):
    """一键更新ZML_LAB：把源目录里的 ZML_LAB 压缩包剪切进插件库并替换已安装版本。

    执行顺序（每一步失败都回滚并如实报告）：

    a. 校验候选包；解析要**沿用**的旧模块名（同族改名，见 ``zml_target_module``）；
       没有同族就拒绝；库目录不存在就创建
    b. 备份：已安装的旧模块文件夹 → ``<addons>/_zml_backup/<模块名>-<时间戳>/``
       （库里被替换的旧 ZML 包也一起备份）
    c. 剪切源 zip 到库目录（``shutil.move``，库里放的是**原始包**），并清理库里
       同模块的旧包
    d. 安装：包内顶层文件夹**改名为旧模块名**后写到临时目录，
       再 ``bpy.ops.preferences.addon_install(overwrite=True)``（只替换同名模块），
       装完**立刻删掉临时 zip**
    e. 重新启用：对**旧模块名**做 ``addon_utils.disable`` → ``enable(default_set=True)``，
       并核对它确实处于启用状态
    f. 报告：模块名、其中的代码版本、源 zip → 库路径、备份路径、是否重新启用

    关键语义：**模块名保留旧名字，里面换的是新版代码**（所以 bl_info 的版本是新版，
    但模块名不变），这样 Blender 认为还是同一个插件，设置与快捷键才不丢。

    **不写** ``userpref.blend``（绝不调用 ``wm.save_userpref``）、
    **不改** keymap、**不删**用户的其它插件。
    """

    bl_idname = "hhz.zml_lab_update"
    bl_label = "一键更新ZML_LAB"
    bl_description = (
        "把源目录里选中的 ZML_LAB 压缩包剪切到插件库，并用它替换已安装的 ZML_LAB："
        "新版顶层文件夹会改成已安装的那个模块名（沿用同名插件，保住设置与快捷键），"
        "先备份旧版本，装完自动禁用再重新启用（快捷键重新注册），最后核对一遍。"
        "找不到可继承的同族模块时会直接拒绝，不会动任何文件"
    )
    bl_options = {'REGISTER', 'UNDO'}

    source_dir: bpy.props.StringProperty(
        name="ZML_LAB 源目录",
        description="扫描 ZML_LAB 压缩包的目录（偏好设置里同一个值）",
        subtype='DIR_PATH',
        default="",
    )
    library_dir: bpy.props.StringProperty(
        name="插件库目录",
        description="ZML_LAB 压缩包要剪切到的插件 zip 库目录（不存在会自动创建）",
        subtype='DIR_PATH',
        default="",
    )
    candidate: bpy.props.StringProperty(
        name="ZML_LAB 压缩包",
        description="要更新的那个 ZML_LAB 压缩包（下拉里的 ASCII 标识符；"
                    "旧版存下的完整路径也认）",
        default="",
    )
    addons_dir: bpy.props.StringProperty(
        name="已安装目录",
        description="留空表示 Blender 当前的 addons 目录；测试时可以指向临时目录",
        subtype='DIR_PATH',
        default="",
    )

    # -- 二次确认 --------------------------------------------------------

    def _source_dir(self, context):
        """源目录：算符属性优先，空着就退回偏好设置里的同一个值。"""
        value = str(self.source_dir or "").strip()
        if value:
            return value
        preferences = compat.get_preferences(context)
        if preferences is not None:
            return str(getattr(preferences, "zml_source_dir", "") or "").strip()
        return ""

    def _resolve_entry(self, context):
        """把下拉里的值（现在是**纯 ASCII id**）反查成扫描结果里的真实 zip。

        2.21.1 起 ``self.candidate`` 里存的是 ASCII 标识符而不是路径 —— 路径里
        有中文时 Blender 的枚举项缓存会坏掉（面板 draw 每次抛 UnicodeDecodeError）。
        这里按扫描结果表反查，**不依赖名字**。旧版存下的完整路径也认。
        """
        return zml_resolve_candidate(self._source_dir(context), self.candidate)

    def _resolve(self, context):
        """解析候选包与「要沿用的旧模块名」。

        返回 ``(module, version, installed, decision, entry)``；``decision`` 是
        ``zml_target_module`` 的结果（确认文案与执行都用它，避免两处判断不一致）。
        """
        entry, _detail = self._resolve_entry(context)
        path = (entry or {}).get("path") or ""
        module, version = zml_zip_module(path)
        installed = zml_installed_modules(str(self.addons_dir or "").strip() or zml_addons_dir())
        decision = zml_target_module(installed, zml_enabled_modules(context), module)
        return module, version, installed, decision, entry

    def _confirm_text(self, context=None):
        module, version, installed, decision, entry = self._resolve(context)
        if entry is None:
            # 找不到候选时也要有内容可看：写明扫到了什么、为什么不算候选
            return module, version, installed, decision, "\n".join([
                "找不到要更新的 ZML_LAB 压缩包",
                zml_missing_candidate_message(
                    self._source_dir(context), self.candidate,
                    self._resolve_entry(context)[1]),
            ])
        return module, version, installed, decision, "\n".join(zml_confirm_lines(
            entry.get("path") or "", module, version, self.library_dir, installed,
            decision.get("target") or ""))

    def draw(self, context):
        """确认弹窗的内容（属性弹窗与确认弹窗共用这段文案）。"""
        layout = self.layout
        _module, _version, _installed, _decision, text = self._confirm_text(context)
        for line in text.split("\n"):
            layout.label(text=line)

    def invoke(self, context, event):
        window_manager = getattr(context, "window_manager", None)
        if window_manager is None:
            return self.execute(context)
        _module, _version, _installed, _decision, text = self._confirm_text(context)
        try:
            # 5.x 的 invoke_confirm 支持 message：确认框里能列出全部细节
            return window_manager.invoke_confirm(
                self, event, title="一键更新ZML_LAB", message=text,
                confirm_text="更新", icon='ERROR')
        except TypeError:
            # 4.0 / 4.1 的 invoke_confirm 只有 (operator, event)，画不出自定义文案，
            # 退回带内容的属性弹窗 —— 同样是「确定 / 取消」的二次确认
            try:
                return window_manager.invoke_props_dialog(self, width=560)
            except TypeError:
                return window_manager.invoke_props_dialog(self)

    # -- 执行 ------------------------------------------------------------

    def execute(self, context):
        # 候选先按扫描结果表反查成真实路径：候选值是 ASCII id（旧版存的是路径）
        entry, detail = self._resolve_entry(context)
        self._candidate_path = (entry or {}).get("path") or ""
        candidate = self._candidate_path
        library_dir = str(self.library_dir or "").strip()
        if not candidate or not os.path.isfile(candidate):
            return self._cancel("找不到要更新的 ZML_LAB 压缩包。{}".format(
                zml_missing_candidate_message(
                    self._source_dir(context), self.candidate, detail)))
        module, version = zml_zip_module(candidate)
        if not module:
            return self._cancel("这个压缩包不是 ZML_LAB 插件包：{}".format(
                zml_zip_inspect(candidate).get("problem")
                or "包内没有 ZML_LAB 模块文件夹"))
        if not library_dir:
            return self._cancel("还没有设置 ZML_LAB 的目标库目录（插件库目录）")

        addons_dir = str(self.addons_dir or "").strip() or zml_addons_dir()
        if not addons_dir:
            return self._cancel("读不到 Blender 的插件目录，无法判断已安装的 ZML_LAB 模块名")
        installed = zml_installed_modules(addons_dir)
        # ---- a. 解析要沿用的旧模块名（同族改名）；没有同族就绝不继续 ----
        decision = zml_target_module(installed, zml_enabled_modules(context), module)
        if not decision["allowed"]:
            return self._cancel(decision["reason"])
        target_module = decision["target"] or module
        renamed = not zml_same_module(target_module, module)
        try:
            os.makedirs(library_dir, exist_ok=True)
        except OSError as error:
            return self._cancel("创建库目录失败：{}（{}）".format(library_dir, error))

        # 库目录里放的是**原始包**，所以按候选自己的模块名识别库里同模块的旧包
        plan = zml_library_plan(library_dir, candidate, module,
                                zml_library_entries(library_dir))
        if plan["conflict"]:
            return self._cancel("库目录里已有同名但属于其它模块的压缩包 {}，不会覆盖它".format(
                os.path.basename(plan["conflict"][0])))
        source_name = os.path.basename(candidate)

        # ---- b. 备份（已安装的旧模块文件夹 + 库里被替换的旧包）----
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = os.path.join(addons_dir, ZML_BACKUP_DIRNAME,
                                  "{}-{}".format(target_module, stamp))
        installed_old = os.path.join(addons_dir, target_module) \
            if any(zml_same_module(name, target_module) for name in installed) else ""
        carried = []
        try:
            os.makedirs(backup_dir, exist_ok=True)
            if installed_old and os.path.isdir(installed_old):
                shutil.copytree(installed_old,
                                os.path.join(backup_dir, os.path.basename(installed_old)))
                carried.append("已安装的旧版本")
            for path in plan["backup"]:
                shutil.copy2(path, os.path.join(backup_dir, os.path.basename(path)))
                carried.append(os.path.basename(path))
        except (OSError, shutil.Error) as error:
            return self._cancel("备份失败，没有移动或删除任何文件：{}".format(error))

        # ---- c. 剪切源 zip 到库目录（库里保留原始包）----
        moved_to = plan["move_to"]
        try:
            if os.path.exists(moved_to):
                # 库里同名的旧包已经在上面备份过了，先清掉它（否则 Windows 上 rename 会失败）
                os.remove(moved_to)
            shutil.move(candidate, moved_to)
        except (OSError, shutil.Error) as error:
            return self._rollback(backup_dir, "", target_module, installed_old, False, (),
                                  "", "把压缩包移动到库目录失败：{}".format(error))
        deleted = []
        for path in plan["delete"]:
            try:
                os.remove(path)
                deleted.append(path)
            except OSError as error:
                self.report({'WARNING'}, "旧包 {} 没能删掉：{}".format(
                    os.path.basename(path), error))

        # ---- d. 安装（同族时把顶层文件夹改名；只用临时目录，装完即删）----
        temp_dir = ""
        install_path = moved_to
        install_source = moved_to
        if renamed:
            try:
                temp_dir = tempfile.mkdtemp(prefix="hhz_zml_install_")
                install_source = os.path.join(temp_dir, os.path.basename(moved_to))
                rewritten = zml_rewrite_module(moved_to, target_module, install_source)
            except Exception as error:
                rewritten = {"path": "", "module": None, "version": ""}
                self.report({'WARNING'}, "改名临时包写入失败：{}".format(error))
            if not rewritten.get("path") or not zml_same_module(
                    rewritten.get("module"), target_module):
                return self._rollback(
                    backup_dir, moved_to, target_module, installed_old, False, deleted,
                    temp_dir,
                    "把新版包的顶层文件夹改名为 {} 失败，没有安装".format(target_module))
            install_path = rewritten["path"]
        try:
            result = bpy.ops.preferences.addon_install(filepath=install_path, overwrite=True)
        except Exception as error:
            return self._rollback(backup_dir, moved_to, target_module, installed_old, True,
                                  deleted, temp_dir, "安装插件失败：{}".format(error))
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
        if 'FINISHED' not in result:
            return self._rollback(backup_dir, moved_to, target_module, installed_old, True,
                                  deleted, "", "安装插件没有完成（{}）".format(sorted(result)))

        # ---- e. 重新启用**旧模块名**（让插件自己的快捷键重新注册）----
        notes = []
        try:
            addon_utils.disable(target_module)
        except Exception as error:
            notes.append("停用旧版本时报错：{}".format(error))
        try:
            addon_utils.enable(target_module, default_set=True)
        except Exception as error:
            notes.append("启用时报错：{}".format(error))
        try:
            _loaded_default, loaded_state = addon_utils.check(target_module)
        except Exception:
            loaded_state = False
        in_preferences = self._in_preferences(context, target_module)
        if not (loaded_state and in_preferences):
            return self._rollback(
                backup_dir, moved_to, target_module, installed_old, True, deleted, "",
                "启用后核对失败（启用状态 {}，偏好设置里 {}）".format(
                    loaded_state, in_preferences))

        # ---- f. 报告（不谎报；模块名与代码版本分别说清）----
        parts = ["ZML_LAB 已更新：模块 {}".format(target_module)]
        if renamed:
            parts.append("已把新版顶层文件夹 {} 改名为 {} 装入（模块名仍是 {}，里面是 {} 版的代码）".format(
                module, target_module, target_module, version or "新版"))
        elif version:
            parts.append("版本 {}".format(version))
        parts.append("压缩包 {} -> {}（库目录里保留原始包）".format(source_name, moved_to))
        if deleted:
            parts.append("清理旧包 {}".format(" / ".join(
                sorted(os.path.basename(path) for path in deleted))))
        if carried:
            parts.append("旧版本已备份到 {}".format(backup_dir))
        if not installed:
            parts.append("首次安装")
        parts.append("已停用并重新启用 {}，插件自己的快捷键已重新注册".format(target_module))
        parts.extend(notes)
        self.report({'INFO'}, "；".join(part for part in parts if part))
        return {'FINISHED'}

    # -- 内部 ------------------------------------------------------------

    def _cancel(self, message):
        self.report({'ERROR'}, message)
        return {'CANCELLED'}

    def _in_preferences(self, context, module):
        try:
            addons = context.preferences.addons
        except Exception:
            return False
        try:
            if addons.get(module) is not None:
                return True
        except Exception:
            pass
        try:
            for key in addons.keys():
                if key == module or key.endswith("." + module):
                    return True
        except Exception:
            return False
        return False

    def _rollback(self, backup_dir, moved_to, target_module, installed_old, installed_new,
                  deleted_library, temp_dir, reason):
        """回滚备份并把**具体失败原因**写进报告（绝不谎报成功）。"""
        problems = []
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        # 1) 库目录里的新包挪回原处（等于撤销"剪切"）
        source_zip = getattr(self, "_candidate_path", "") or ""
        if not source_zip:
            # 兜底：候选值本身就是一个存在的路径（旧版语义）时才用它
            candidate_value = str(self.candidate or "").strip()
            if candidate_value and os.path.isfile(candidate_value):
                source_zip = candidate_value
        if moved_to and os.path.isfile(moved_to) and source_zip:
            try:
                shutil.move(moved_to, source_zip)
            except Exception as error:
                problems.append("压缩包没能移动回原处（{}）".format(error))
        # 2) 已经动过模块文件夹：把备份的旧版本放回去（首次安装则删掉新装的那份）
        addons_dir = os.path.dirname(os.path.dirname(backup_dir)) if backup_dir else ""
        if installed_new:
            try:
                try:
                    addon_utils.disable(target_module)
                except Exception:
                    pass
            except Exception:
                pass
            if installed_old and os.path.isdir(backup_dir):
                saved = os.path.join(backup_dir, os.path.basename(installed_old))
                if os.path.isdir(saved):
                    try:
                        if os.path.isdir(installed_old):
                            shutil.rmtree(installed_old, ignore_errors=True)
                        shutil.copytree(saved, installed_old)
                        try:
                            addon_utils.enable(target_module, default_set=True)
                        except Exception as restore_error:
                            problems.append("旧版本放回去了但没能重新启用（{}）".format(
                                restore_error))
                    except Exception as error:
                        problems.append("旧版本文件夹没能还原（{}）".format(error))
            elif addons_dir and target_module:
                # 首次安装：没有旧版本可还原，把这次装进去的新模块清掉
                fresh = os.path.join(addons_dir, target_module)
                if os.path.isdir(fresh):
                    try:
                        shutil.rmtree(fresh, ignore_errors=True)
                    except Exception as error:
                        problems.append("新装的模块文件夹没能删掉（{}）".format(error))
        # 3) 被清掉的旧库包从备份放回
        for path in (deleted_library or []):
            saved = os.path.join(backup_dir, os.path.basename(path))
            if os.path.isfile(saved):
                try:
                    shutil.copy2(saved, path)
                except Exception as error:
                    problems.append("旧包 {} 没能放回（{}）".format(
                        os.path.basename(path), error))
        detail = "；".join(problems) if problems else "备份已还原，源压缩包已归位"
        self.report({'ERROR'}, "{}；已回滚：{}".format(reason, detail))
        return {'CANCELLED'}


# --------------------------------------- 打开 Blender 自动切英文输入法（Windows）
#
# 背景：Windows 上默认输入法是中文时，Blender 一打开就在中文组字模式，空格 /
# 数字 / 方向键会先被输入法吃掉。这里在启动（以及每次加载文件）之后，把**本窗口**
# 的输入法转换状态切成英文 / 半角。
#
# 硬边界（都在下面的实现里守着）：
#   * 只在 Windows 上做，其它平台直接跳过，不报错；
#   * 只用 ctypes 调 user32 / imm32，不引入任何第三方依赖；
#   * 不合成任何按键（SendInput / keybd_event / PostMessage 一律不用），
#     不改注册表，也不改系统输入法设置；
#   * 只清 IME_CMODE_NATIVE 这一位，其它位原样保留；
#   * 任何失败都只 print 一行说明，绝不抛异常，绝不影响 Blender 启动。

#: Windows IME 转换模式位：IME_CMODE_NATIVE（0x0001）。
#: 置位 = 母语组字（中文输入法正在打汉字），清位 = 英文 / 半角（字母数字模式）。
IME_CMODE_NATIVE = 0x0001

#: 一次性定时器的延时长（秒）：等窗口真正就绪再切。
ENGLISH_IME_DELAY = 0.3


def english_ime_mode(conv, sentence=0):
    """纯函数：返回「清掉 IME_CMODE_NATIVE 之后」的转换状态。

    ``conv`` 是 ``ImmGetConversionStatus`` 写回来的转换状态（DWORD 位掩码），
    ``sentence`` 是同一次调用写回的句子状态（大小写 / 全角等独立模式）。
    句子模式与「中英文」无关，写回 ``ImmSetConversionStatus`` 时原样传回，
    所以这里既不读也不改它。

    返回值保证两件事：
      1. ``result & IME_CMODE_NATIVE`` 一定为 0（必定是英文 / 半角）；
      2. 除这一位之外，其余每一位与入参**逐位相同**（含 0 与全 1 的边界）。

    纯函数：只做位运算，不碰 bpy / ctypes，可以无头测试。
    """
    _ = sentence  # 句子模式不需要改，原样保留（签名对称，调用方写回原值）
    return (conv & ~IME_CMODE_NATIVE) & 0xFFFFFFFF


def switch_ime_to_english():
    """把当前 Blender 窗口的输入法切到英文 / 半角（Windows 专用，尽力而为）。

    返回一行说明文字。**任何情况下都不抛异常**：它跑在 Blender 启动路径上，
    出错也只能打印一行说明，绝不能影响启动。

    做法（全程只是给**本窗口**发一次 IME 状态设置，不合成按键）：
    ``GetForegroundWindow`` -> ``ImmGetContext`` -> ``ImmGetConversionStatus``
    -> 清 ``IME_CMODE_NATIVE`` -> ``ImmSetConversionStatus`` -> ``ImmReleaseContext``
    """
    if sys.platform != 'win32':
        # 非 Windows：直接跳过，不报错（也不去 import ctypes）
        return "跳过：非 Windows 平台"
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        imm32 = ctypes.windll.imm32
        # 必须显式声明 restype / argtypes：HIMC 是指针，ctypes 默认按 32 位 int
        # 处理，64 位下句柄会被截断，之后每次调用都会失败。
        imm32.ImmGetContext.restype = ctypes.c_void_p
        imm32.ImmGetContext.argtypes = [wintypes.HWND]
        imm32.ImmGetConversionStatus.restype = wintypes.BOOL
        imm32.ImmGetConversionStatus.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD)]
        imm32.ImmSetConversionStatus.restype = wintypes.BOOL
        imm32.ImmSetConversionStatus.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
        imm32.ImmReleaseContext.restype = wintypes.BOOL
        imm32.ImmReleaseContext.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "跳过：拿不到前台窗口"
        process = wintypes.DWORD(0)
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process)):
            return "跳过：读不到前台窗口的进程"
        if int(process.value) != os.getpid():
            # 启动时 Blender 的窗口不一定在前台（例如双击后立刻切走）。
            # 这时切的是别人的输入法，宁可不切。
            return "跳过：前台窗口不是 Blender"

        himc = imm32.ImmGetContext(hwnd)
        if not himc:
            return "跳过：这个窗口没有输入法上下文（没装输入法 / 无 IME 窗口）"
        try:
            conv = wintypes.DWORD(0)
            sentence = wintypes.DWORD(0)
            if not imm32.ImmGetConversionStatus(
                    himc, ctypes.byref(conv), ctypes.byref(sentence)):
                return "跳过：读不到输入法转换状态"
            current = int(conv.value)
            target = english_ime_mode(current, int(sentence.value))
            if target == current:
                return "已经是英文 / 半角状态"
            if not imm32.ImmSetConversionStatus(
                    himc, wintypes.DWORD(target), sentence):
                # 某些第三方输入法（以及部分远程桌面 / 输入法框架）会忽略
                # IME 状态消息，只能如实说明，不重试、不注入按键。
                return "输入法不接受设置（某些第三方输入法会忽略 IME 消息）"
            return "已切到英文 / 半角状态"
        finally:
            imm32.ImmReleaseContext(hwnd, himc)
    except Exception as error:
        # 记不住任何东西，只报告一句：绝不抛异常、绝不影响 Blender 启动
        return "跳过：{}".format(error)


def _english_ime_timer():
    """一次性定时器回调：窗口就绪后切一次英文。

    返回 ``None`` = 不再排队（定时器只跑一次，不会反复切用户的输入法）。
    """
    try:
        print("红红子lab - 自动英文输入法：{}".format(switch_ime_to_english()))
    except Exception:
        pass
    return None


def _english_ime_on_load(_dummy=None):
    """``bpy.app.handlers.load_post`` 回调：每次加载文件后重新排一次性定时器。"""
    return schedule_english_ime()


def schedule_english_ime(delay=None):
    """排一个一次性定时器，``delay`` 秒后尝试切英文；重复调用不会叠加。"""
    if delay is None:
        delay = ENGLISH_IME_DELAY
    try:
        if bpy.app.timers.is_registered(_english_ime_timer):
            return False
        bpy.app.timers.register(_english_ime_timer, first_interval=float(delay))
        return True
    except Exception as error:
        print("红红子lab - 自动英文输入法：排定时器失败（{}）".format(error))
        return False


def register_english_ime():
    """注册「打开 Blender 自动切英文输入法」（幂等：重复调用不会重复添加）。

    handler 列表是全局的，同一个函数在里面只能出现一次，所以先查再加。
    注册时排一次定时器（打开 Blender 就生效），之后每次 ``load_post`` 再排一次。
    没有开关：默认总是执行，不新增设置项 / 面板。
    """
    try:
        handlers = bpy.app.handlers.load_post
        if _english_ime_on_load not in handlers:
            handlers.append(_english_ime_on_load)
    except Exception as error:
        print("红红子lab - 自动英文输入法：注册 load_post 失败（{}）".format(error))
    return schedule_english_ime()


def unregister_english_ime():
    """注销：移除 handler 并停掉还没跑的一次性定时器（与注册严格配对）。"""
    try:
        handlers = bpy.app.handlers.load_post
        while _english_ime_on_load in handlers:
            handlers.remove(_english_ime_on_load)
    except Exception:
        pass
    try:
        if bpy.app.timers.is_registered(_english_ime_timer):
            bpy.app.timers.unregister(_english_ime_timer)
    except Exception:
        pass
