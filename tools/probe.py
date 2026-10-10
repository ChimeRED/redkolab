# SPDX-License-Identifier: GPL-3.0-or-later
"""兼容性探针：确认「属性写法」和「算子上下文覆盖」在各版本上的真实行为。

    blender --background --factory-startup --python tools/probe.py
"""

import bpy
from bpy.props import BoolProperty, StringProperty

print("\n########## PROBE ##########")
print("version:", bpy.app.version_string)
print("has temp_override:", hasattr(bpy.context, "temp_override"))

# ---------------------------------------------------------------- 属性写法
print("\n--- 属性写法：赋值 vs 注解 ---")


class ProbeAssignPrefs(bpy.types.AddonPreferences):
    bl_idname = "probe_assign"
    x = StringProperty(default="assign")


class ProbeAnnotPrefs(bpy.types.AddonPreferences):
    bl_idname = "probe_annot"
    x: StringProperty(default="annot")


class ProbeAssignGroup(bpy.types.PropertyGroup):
    x = StringProperty(default="assign")


class ProbeAnnotGroup(bpy.types.PropertyGroup):
    x: StringProperty(default="annot")


for cls in (ProbeAssignPrefs, ProbeAnnotPrefs, ProbeAssignGroup, ProbeAnnotGroup):
    try:
        bpy.utils.register_class(cls)
        print("  registered", cls.__name__)
    except Exception as error:
        print("  register FAILED", cls.__name__, type(error).__name__, error)


def report_props(cls):
    try:
        names = [p.identifier for p in cls.bl_rna.properties if p.identifier != "rna_type"]
    except Exception as error:
        names = ["<introspect failed: {}>".format(error)]
    print("  {:<22} -> {}".format(cls.__name__, names))


for cls in (ProbeAssignPrefs, ProbeAnnotPrefs, ProbeAssignGroup, ProbeAnnotGroup):
    report_props(cls)

# 实例上读值（AddonPreferences 实例只有匹配到真实插件时才拿得到，这里用 PropertyGroup）
try:
    group = bpy.context.scene.bl_rna.properties  # 占位，避免误用
except Exception:
    pass

# ------------------------------------------------------------ 上下文覆盖
print("\n--- 算子上下文覆盖 ---")
try:
    bpy.ops.wm.read_homefile(use_empty=True)
except Exception as error:
    print("  read_homefile(use_empty) failed:", error)

bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.view_layer.objects.active
print("  cube:", cube.name, "| mode:", bpy.context.mode)


def make_modifier(obj, name):
    modifier = obj.modifiers.new(name, 'SUBSURF')
    modifier.levels = 1
    return modifier


# A 直接调用（物体本来就是活动物体）
make_modifier(cube, "ModA")
try:
    result = bpy.ops.object.modifier_apply(modifier="ModA")
    print("  A 直接调用: {} 剩余={}".format(result, [m.name for m in cube.modifiers]))
except Exception as error:
    print("  A 直接调用 FAILED: {} {}".format(type(error).__name__, error))

# B temp_override
make_modifier(cube, "ModB")
try:
    with bpy.context.temp_override(
        object=cube, active_object=cube,
        selected_objects=[cube], selected_editable_objects=[cube],
    ):
        result = bpy.ops.object.modifier_apply(modifier="ModB")
    print("  B temp_override: {} 剩余={}".format(result, [m.name for m in cube.modifiers]))
except Exception as error:
    print("  B temp_override FAILED: {} {}".format(type(error).__name__, error))

# C 旧式字典覆盖
make_modifier(cube, "ModC")
try:
    result = bpy.ops.object.modifier_apply(
        {"object": cube, "active_object": cube},
        modifier="ModC",
    )
    print("  C 字典覆盖: {} 剩余={}".format(result, [m.name for m in cube.modifiers]))
except Exception as error:
    print("  C 字典覆盖 FAILED: {} {}".format(type(error).__name__, error))

# D temp_override + 同时传 window/area
make_modifier(cube, "ModD")
try:
    kwargs = {"object": cube, "active_object": cube,
              "selected_objects": [cube], "selected_editable_objects": [cube]}
    if bpy.context.window is not None:
        kwargs["window"] = bpy.context.window
    with bpy.context.temp_override(**kwargs):
        result = bpy.ops.object.modifier_apply(modifier="ModD")
    print("  D temp_override+window: {} 剩余={}".format(result, [m.name for m in cube.modifiers]))
except Exception as error:
    print("  D temp_override+window FAILED: {} {}".format(type(error).__name__, error))

# ------------------------------------------------------------ 键位注册
print("\n--- keymap_items.new 参数 ---")
keyconfig = getattr(bpy.context.window_manager.keyconfigs, "addon", None)
print("  addon keyconfig:", keyconfig)
if keyconfig is not None:
    keymap = keyconfig.keymaps.new(name="3D View", space_type='VIEW_3D')
    try:
        item = keymap.keymap_items.new("wm.call_menu_pie", "ACCENT_GRAVE", 'PRESS')
        item.properties.name = "OBJECT_MT_probe_pie"
        print("  new() 成功:", item.idname, item.type, getattr(item.properties, "name", None))
    except Exception as error:
        print("  new() FAILED: {} {}".format(type(error).__name__, error))

print("########## PROBE END ##########\n")
