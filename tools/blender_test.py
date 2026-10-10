#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab · Blender 内部真机测试

会真的把安装包装进一个临时的用户目录，启用插件，然后跑一整套功能断言。

用法：
    blender --background --factory-startup --python tools/blender_test.py -- \
        --zip dist/redkolab-1.1.0.zip --mode auto

--mode 可选：
    auto           默认。看包内布局决定用哪种安装方式（4.2+ 有清单就走扩展，
                   否则走传统插件安装）
    addon_install  强制用 preferences.addon_install（= 偏好设置里的「从磁盘安装」）
    extensions     强制用 extensions.package_install_files（= 扩展页的安装）

脚本自己负责隔离：调用方通过 BLENDER_USER_CONFIG / BLENDER_USER_SCRIPTS /
BLENDER_USER_EXTENSIONS 环境变量把配置目录指到临时目录，就不会污染真实配置。
"""

import ast
import os
import re
import sys
import traceback
import zipfile

import bpy

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []

PACKAGE_HINT = "redkolab"
PIE_IDNAME = "OBJECT_MT_hhz_pie"

#: --mode 的历史别名
MODE_ALIASES = {"legacy": "addon_install", "extension": "extensions"}

FAILURES = []
CHECK_COUNT = [0]


def argument(name, default=None):
    if name in ARGS:
        index = ARGS.index(name)
        if index + 1 < len(ARGS):
            return ARGS[index + 1]
    return default


MODE = argument("--mode", "auto")
MODE = MODE_ALIASES.get(MODE, MODE)
ZIP_PATH = argument("--zip")


# ------------------------------------------------------------------ 断言工具


def check(condition, label, detail=""):
    CHECK_COUNT[0] += 1
    if condition:
        print("  [ ok ] {}".format(label))
    else:
        text = "{}  ->  {}".format(label, detail) if detail != "" else label
        print("  [FAIL] {}".format(text))
        FAILURES.append(text)


def section(title):
    print("\n=== {} ===".format(title))


def skip(reason):
    print("  [skip] {}".format(reason))


def run(title, function, *args):
    """跑一个测试函数，异常不让整个测试中断。"""
    try:
        function(*args)
    except Exception:
        FAILURES.append("{} 抛出异常".format(title))
        print("  [FAIL] {} 抛出异常".format(title))
        traceback.print_exc()


# ------------------------------------------------------------------ 场景工具


def reset_scene():
    """清空场景但不动偏好设置（read_factory_settings 会重置偏好，不能用）。"""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection in (bpy.data.meshes, bpy.data.curves, bpy.data.materials, bpy.data.shape_keys):
        for block in list(collection):
            if block.users == 0:
                try:
                    collection.remove(block)
                except Exception:
                    pass


def add_cube(name):
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0))
    obj = bpy.context.view_layer.objects.active
    obj.name = name
    return obj


def add_curve(name):
    bpy.ops.curve.primitive_bezier_curve_add(location=(4, 0, 0))
    obj = bpy.context.view_layer.objects.active
    obj.name = name
    return obj


def select_only(objects):
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0] if objects else None


def operator_class(module, idname):
    """把 ``hhz.xxx`` 映射到插件里的算子类。

    不能用 ``hasattr(bpy.ops.hhz, "xxx")`` 判断算子是否存在 —— bpy.ops 对不存在的
    算子名也会返回一个调用对象，只有真正调用时才报错，所以那样判断永远是"通过"。
    """
    return getattr(module.ops, "HHZ_OT_" + idname, None)


def assigned_materials(obj):
    total = 0
    for slot in obj.material_slots:
        if slot.material is not None:
            total += 1
    return total


def color_close(actual, expected, tolerance=1.5 / 255.0):
    """颜色比较：主题颜色内部是 8 位量化存储的，必须留容差。

    实测：写入 0.5 会读回 128/255 = 0.50196，写入 0.25 读回 64/255。
    """
    if actual is None or expected is None or len(actual) != len(expected):
        return False
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected))


# --------------------------------------------------------------- 安装与启用


def snapshot_modules():
    """记录当前已导入的模块，用来识别刚装进去的插件。"""
    return set(sys.modules.keys())


def _is_our_package(module):
    """靠 compat 模块的特征属性认领，而不是靠插件叫什么名字。

    传统安装的模块名可能来自 zip 文件名，4.2+ 扩展则是 bl_ext.<仓库>.<id>，
    写死名字的测试在「一份通吃包」上会失效。
    """
    try:
        compat = getattr(module, "compat", None)
    except Exception:
        return False
    if compat is None:
        return False
    return all(hasattr(compat, attribute) for attribute in ("ADDON_ID", "ADDON_NAME_EN", "run_op"))


def available_addon_names():
    """addon_utils 当前能看到的插件模块名。"""
    names = set()
    try:
        import addon_utils
        addon_utils.modules(refresh=True)
        for module in addon_utils.modules():
            names.add(module.__name__)
    except Exception:
        pass
    try:
        names.update(bpy.context.preferences.addons.keys())
    except Exception:
        pass
    return names


def discover_installed_module(before_modules, before_available):
    """找出刚安装进来的插件包名（不依赖它叫什么名字）。"""
    candidates = []

    for name, module in list(sys.modules.items()):
        if name in before_modules or not hasattr(module, "__path__"):
            continue
        if _is_our_package(module):
            candidates.append(name)

    for name in sorted(available_addon_names() - before_available):
        if name in candidates:
            continue
        try:
            module = __import__(name, fromlist=["compat"])
        except Exception:
            continue
        if _is_our_package(module):
            candidates.append(name)

    # 再直接按常见命名探一次（有的版本 addon_utils 刷新后仍列不出新装的插件）
    for name in (PACKAGE_HINT, "bl_ext.user_default." + PACKAGE_HINT):
        if name in candidates or name in before_modules:
            continue
        try:
            module = __import__(name, fromlist=["compat"])
        except Exception:
            continue
        if _is_our_package(module):
            candidates.append(name)

    # 包名短的是包本身，子模块（xxx.compat 之类）排在后面
    candidates.sort(key=len)
    return candidates


def _zip_layout(zip_path):
    """看包内布局：根目录有没有扩展清单、有哪些顶层目录。"""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
    except Exception as error:
        print("  读取安装包失败：{}".format(error))
        return False, []
    has_root_manifest = "blender_manifest.toml" in names
    folders = sorted({name.split("/")[0] for name in names if "/" in name})
    return has_root_manifest, folders


def install_package():
    """按包内布局和当前版本选择安装方式，返回 (是否成功, 方式说明)。"""
    has_root_manifest, folders = _zip_layout(ZIP_PATH)
    print("  包内布局：根目录清单={}，顶层目录={}".format(has_root_manifest, folders))

    extension_ready = has_root_manifest and hasattr(bpy.ops, "extensions")
    if MODE == "extensions":
        order = ["extensions"]
    elif MODE == "addon_install":
        order = ["addon_install"]
    elif extension_ready:
        order = ["extensions", "addon_install"]
    else:
        order = ["addon_install", "extensions"]

    for mode in order:
        if mode == "extensions":
            if not hasattr(bpy.ops, "extensions"):
                print("  当前版本没有扩展接口，跳过扩展安装")
                continue
            for kwargs in (
                {"filepath": ZIP_PATH, "repo": "user_default", "enable_on_install": True, "overwrite": True},
                {"filepath": ZIP_PATH, "repo": "user_default", "enable_on_install": True},
                {"filepath": ZIP_PATH},
            ):
                try:
                    result = bpy.ops.extensions.package_install_files(**kwargs)
                    print("  extensions.package_install_files({}) -> {}".format(kwargs, result))
                    return True, "extensions.package_install_files"
                except Exception as error:
                    print("  扩展安装尝试失败 {}：{}".format(kwargs, error))
        else:
            try:
                bpy.ops.preferences.addon_install(filepath=ZIP_PATH, overwrite=True)
                print("  preferences.addon_install 成功")
                return True, "preferences.addon_install"
            except Exception as error:
                print("  preferences.addon_install 失败：{}".format(error))

    return False, "无可用安装方式"


def install_and_enable():
    section("安装与启用")
    print("  Blender {} | temp_override={} | 扩展接口={}".format(
        bpy.app.version_string,
        hasattr(bpy.context, "temp_override"),
        hasattr(bpy.ops, "extensions"),
    ))

    before_modules = snapshot_modules()
    before_available = available_addon_names()

    installed, how = install_package()
    check(installed, "安装包安装成功（方式：{}）".format(how))

    # 让 addon_utils 重新扫描刚装进去的模块
    try:
        import addon_utils
        addon_utils.modules(refresh=True)
    except Exception:
        pass

    discovered = discover_installed_module(before_modules, before_available)
    print("  新发现的插件模块：{}".format(discovered))

    enabled_keys = list(bpy.context.preferences.addons.keys())
    module_name = None
    for name in discovered:
        if name in enabled_keys:
            module_name = name
            break
    if module_name is None and discovered:
        module_name = discovered[0]

    if module_name is not None and module_name not in bpy.context.preferences.addons.keys():
        try:
            bpy.ops.preferences.addon_enable(module=module_name)
            print("  已启用 {}".format(module_name))
        except Exception as error:
            print("  addon_enable({}) 失败：{}".format(module_name, error))

    if module_name is None:
        # 兜底：按常见命名规则猜
        for candidate in (PACKAGE_HINT, "bl_ext.user_default." + PACKAGE_HINT):
            try:
                bpy.ops.preferences.addon_enable(module=candidate)
                module_name = candidate
                break
            except Exception as error:
                print("  addon_enable({}) 失败：{}".format(candidate, error))

    check(module_name is not None, "插件已启用（模块名 {}）".format(module_name))
    if module_name is not None:
        check(module_name in bpy.context.preferences.addons.keys(),
              "插件出现在已启用插件列表里")
    return module_name


# -------------------------------------------------------------------- 测试


def test_registration(module_name, module):
    section("注册与界面结构")
    check(getattr(bpy.types, PIE_IDNAME, None) is not None, "饼菜单类已注册")
    check(getattr(bpy.types, "HHZ_PT_main", None) is not None, "N 面板类已注册")

    check(getattr(bpy.ops, "hhz", None) is not None, "算符分类 hhz 存在")
    for idname in (
        "remove_all_materials", "apply_all_modifiers", "remove_all_modifiers",
        "open_preferences", "capture_hotkey", "reset_hotkey",
    ):
        check(operator_class(module, idname) is not None,
              "算符 hhz.{} 已注册".format(idname))

    check(module.compat.PACKAGE == module_name,
          "compat.PACKAGE 与真实模块名一致", module.compat.PACKAGE)
    check(module.compat.ADDON_ID == PACKAGE_HINT,
          "compat.ADDON_ID 与包名一致", module.compat.ADDON_ID)
    check(module.compat.ADDON_NAME == "红红子lab",
          "中文名保留为 红红子lab", module.compat.ADDON_NAME)
    check(module.compat.ADDON_NAME_EN == "REDKO_LAB",
          "英文名为 REDKO_LAB", module.compat.ADDON_NAME_EN)
    check(tuple(bpy.app.version) >= tuple(module.compat.MIN_BLENDER_VERSION),
          "当前 Blender 满足最低版本要求",
          "{} vs {}".format(bpy.app.version, module.compat.MIN_BLENDER_VERSION))

    bl_info = getattr(module, "bl_info", None)
    if bl_info:
        check(bl_info["name"] == "红红子lab (REDKO_LAB)",
              "bl_info 名字同时含中英文名", bl_info["name"])
        check(tuple(bl_info["blender"]) == (4, 0, 0),
              "bl_info 声明最低 4.0", bl_info["blender"])
    else:
        skip("当前安装方式下没有 bl_info（由扩展清单接管元数据）")

    preferences = module.compat.get_preferences()
    check(preferences is not None, "偏好设置对象可访问")
    if preferences is not None:
        check(preferences.key_type == "E", "默认按键是 E", preferences.key_type)
        check(preferences.ctrl is True, "默认按住 Ctrl", preferences.ctrl)
        check(preferences.enable_pie is True, "默认启用饼菜单快捷键")
        check(preferences.hotkey_label() == "Ctrl + E",
              "默认快捷键显示为 Ctrl + E", preferences.hotkey_label())
        check(module.compat.default_hotkey_label() == "Ctrl + E",
              "compat 里的默认快捷键文本一致", module.compat.default_hotkey_label())

    panel = getattr(bpy.types, "HHZ_PT_main", None)
    if panel is not None:
        check(panel.bl_category == "红红子lab", "N 面板标签页名称正确", panel.bl_category)
        check(panel.bl_space_type == 'VIEW_3D' and panel.bl_region_type == 'UI',
              "N 面板挂在 3D 视图侧栏")

    # 侧栏 N 面板不保证绘制子面板（Blender 自己的 N 面板脚本里一个 bl_parent_id 都没有），
    # 所以本插件的面板必须全部是顶层面板，否则会出现"注册成功但界面上找不到"
    ui_path = os.path.join(os.path.dirname(os.path.abspath(module.__file__)), "ui.py")
    with open(ui_path, encoding="utf-8") as handle:
        ui_source = handle.read()
    offenders = panels_with_parent_id(ui_source)
    check(not offenders, "没有任何面板使用 bl_parent_id（否则可能不在 N 面板显示）",
          offenders)

    for idname in ("HHZ_PT_theme", "HHZ_PT_hotkey", "HHZ_PT_settings", "HHZ_PT_order"):
        panel = getattr(bpy.types, idname, None)
        check(panel is not None, "面板 {} 已注册".format(idname))
        if panel is not None:
            check(not getattr(panel, "bl_parent_id", ""),
                  "{} 是顶层面板（N 面板里独立可见）".format(idname),
                  getattr(panel, "bl_parent_id", ""))
            check(getattr(panel, "bl_category", "") == "红红子lab",
                  "{} 属于同一个 N 面板标签页".format(idname), getattr(panel, "bl_category", ""))
            check(getattr(panel, "bl_space_type", "") == 'VIEW_3D'
                  and getattr(panel, "bl_region_type", "") == 'UI',
                  "{} 在 3D 视图侧栏".format(idname))


def test_icons(module):
    section("图标名合法性（避免界面报错）")
    keys = set()
    try:
        parameter = bpy.types.UILayout.bl_rna.functions["operator"].parameters["icon"]
        keys = set(parameter.enum_items.keys())
        if not keys:
            keys = set(parameter.enum_items_static.keys())
    except Exception as error:
        skip("无法内省图标枚举：{}".format(error))
        return
    if not keys:
        skip("该版本拿不到图标枚举")
        return

    # 直接扫源码里所有 icon='...' 用法，插件用到哪个就查哪个（换图标不会漏测）
    package_dir = os.path.dirname(os.path.abspath(module.__file__))
    used = set()
    for name in sorted(os.listdir(package_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(package_dir, name), encoding="utf-8") as handle:
            used.update(re.findall(r"icon\s*=\s*['\"]([A-Za-z_0-9]+)['\"]", handle.read()))
    check(bool(used), "从源码里扫到了图标用法", sorted(used))
    for icon in sorted(used):
        check(icon in keys, "图标 {} 在 Blender {} 上存在".format(icon, bpy.app.version_string))


def _pie_shortcut_items(keyconfig):
    keymap = keyconfig.keymaps.get("3D View")
    if keymap is None:
        return []
    return [item for item in keymap.keymap_items if item.idname == "wm.call_menu_pie"]


def _ctrl_e_conflicts(module):
    """列出 Blender 自己已有的 Ctrl+E 绑定（编辑模式等场景会优先于插件）。"""
    key_type = module.compat.DEFAULT_KEY_TYPE
    conflicts = []
    try:
        keyconfigs = bpy.context.window_manager.keyconfigs
        candidates = [getattr(keyconfigs, "active", None), getattr(keyconfigs, "user", None)]
    except Exception:
        return conflicts

    for keyconfig in candidates:
        if keyconfig is None:
            continue
        for keymap_name in ("3D View", "Object Mode", "Mesh", "Edit Mode"):
            try:
                keymap = keyconfig.keymaps.get(keymap_name)
            except Exception:
                keymap = None
            if keymap is None:
                continue
            for item in keymap.keymap_items:
                if item.type == key_type and item.ctrl and item.idname != "wm.call_menu_pie":
                    conflicts.append("{}: {}".format(keymap_name, item.idname))
    return sorted(set(conflicts))


def test_keymap(module):
    section("键位注册与改键")
    keyconfig = module.compat.addon_keyconfig()
    if keyconfig is None:
        skip("当前环境没有 addon keyconfig（后台模式），跳过键位测试")
        return
    check(keyconfig is not None, "addon keyconfig 可用")

    items = _pie_shortcut_items(keyconfig)
    check(len(items) == 1, "3D View 里注册了 1 个饼菜单快捷键", len(items))
    if len(items) == 1:
        check(items[0].type == "E", "默认绑定 E 键", items[0].type)
        # KeyMapItem 的修饰键是 int（1/0），不是单例 True，所以用 bool() 判断
        check(bool(items[0].ctrl) is True, "默认按住 Ctrl 触发", items[0].ctrl)
        check(getattr(items[0].properties, "name", None) == PIE_IDNAME,
              "快捷键指向饼菜单", getattr(items[0].properties, "name", None))

    # Ctrl+E 在编辑模式等场景是 Blender 自己的键位（会优先于插件），这里只报告、不算失败
    conflicts = _ctrl_e_conflicts(module)
    print("  Ctrl+E 上已有的其它 Blender 绑定：{}（无头环境 keymap 可能不完整，仅供参考）".format(
        conflicts if conflicts else "无"))

    preferences = module.compat.get_preferences()
    preferences.key_type = "K"
    preferences.ctrl = True
    items = _pie_shortcut_items(keyconfig)
    check(len(items) == 1 and items[0].type == "K" and items[0].ctrl,
          "改偏好设置后键位自动重建",
          [(item.type, item.ctrl) for item in items])

    # 描述文本也要跟着变
    check(preferences.hotkey_label() == "Ctrl + K", "显示文本随设置变化", preferences.hotkey_label())

    bpy.ops.hhz.reset_hotkey()
    items = _pie_shortcut_items(keyconfig)
    check(len(items) == 1 and items[0].type == "E" and bool(items[0].ctrl),
          "恢复默认快捷键生效（Ctrl+E）",
          [(item.type, item.ctrl) for item in items])

    preferences.enable_pie = False
    check(len(_pie_shortcut_items(keyconfig)) == 0, "关掉开关后快捷键被移除")
    preferences.enable_pie = True
    check(len(_pie_shortcut_items(keyconfig)) == 1, "重新打开开关后快捷键回来了")


def test_materials():
    section("功能：一键删除所有材质")
    reset_scene()
    material_a = bpy.data.materials.new("MatA")
    material_b = bpy.data.materials.new("MatB")
    material_c = bpy.data.materials.new("MatC")

    cube_a = add_cube("CubeA")
    cube_a.data.materials.append(material_a)
    cube_a.data.materials.append(material_b)

    cube_b = add_cube("CubeB")
    cube_b.data.materials.append(material_a)

    empty = bpy.data.objects.new("EmptyObject", None)
    bpy.context.collection.objects.link(empty)

    select_only([cube_a, cube_b, empty])
    result = bpy.ops.hhz.remove_all_materials()
    check(result == {'FINISHED'}, "算符返回 FINISHED", result)
    check(len(cube_a.data.materials) == 0, "CubeA 材质槽已清空", len(cube_a.data.materials))
    check(len(cube_b.data.materials) == 0, "CubeB 材质槽已清空", len(cube_b.data.materials))
    check(assigned_materials(cube_a) == 0 and assigned_materials(cube_b) == 0,
          "没有残留已挂载的材质")
    check(bpy.data.materials.get("MatA") is not None, "材质数据块本身保留")
    check(len(empty.material_slots) == 0, "无数据的空物体不会报错")

    # 物体级（link == 'OBJECT'）材质槽
    cube_c = add_cube("CubeC")
    cube_c.data.materials.append(material_a)
    cube_c.data.materials.append(material_b)
    cube_c.material_slots[0].link = 'OBJECT'
    cube_c.material_slots[0].material = material_c
    select_only([cube_c])
    result = bpy.ops.hhz.remove_all_materials()
    check(result == {'FINISHED'}, "物体级材质槽：算符返回 FINISHED", result)
    check(assigned_materials(cube_c) == 0, "物体级材质槽也被清空", assigned_materials(cube_c))

    # 曲线上的材质
    curve = add_curve("CurveMaterial")
    curve.data.materials.append(material_a)
    select_only([curve])
    result = bpy.ops.hhz.remove_all_materials()
    check(result == {'FINISHED'} and len(curve.data.materials) == 0,
          "曲线物体的材质也被清空", len(curve.data.materials))

    # 库链接物体：只跳过、不报错
    linked = add_cube("FakeLinked")
    select_only([linked])
    result = bpy.ops.hhz.remove_all_materials()
    check(result == {'FINISHED'}, "选中没有材质的物体时正常结束", result)


def test_apply_modifiers(module):
    section("功能：一键应用所有修改器")

    # 先单独验证最底层的上下文覆盖：让活动物体不是目标物体，逼出覆盖逻辑
    reset_scene()
    target = add_cube("CubeOverrideTarget")
    subsurf = target.modifiers.new("Subsurf", 'SUBSURF')
    subsurf.levels = 1
    other = add_cube("CubeOtherActive")
    select_only([other])
    try:
        module.compat.run_op(bpy.ops.object.modifier_apply, target, modifier="Subsurf")
        override_ok = len(target.modifiers) == 0
        override_error = ""
    except Exception as error:
        override_ok = False
        override_error = "{}: {}".format(type(error).__name__, error)
    check(override_ok, "compat.run_op 上下文覆盖可用（活动物体不是目标物体）", override_error)

    reset_scene()
    cube = add_cube("CubeModifiers")
    before = len(cube.data.vertices)
    subsurf = cube.modifiers.new("Subsurf", 'SUBSURF')
    subsurf.levels = 2
    subsurf.render_levels = 2
    cube.modifiers.new("Solidify", 'SOLIDIFY')
    select_only([cube])
    result = bpy.ops.hhz.apply_all_modifiers()
    check(result == {'FINISHED'}, "算符返回 FINISHED", result)
    check(len(cube.modifiers) == 0, "修改器已全部应用", [m.name for m in cube.modifiers])
    check(len(cube.data.vertices) > before, "几何真的烘焙进网格了",
          "{} -> {}".format(before, len(cube.data.vertices)))

    # 视口里被禁用的修改器
    reset_scene()
    disabled = add_cube("CubeDisabledModifier")
    subsurf = disabled.modifiers.new("Subsurf", 'SUBSURF')
    subsurf.levels = 1
    subsurf.show_viewport = False
    select_only([disabled])
    result = bpy.ops.hhz.apply_all_modifiers()
    check(len(disabled.modifiers) == 0, "视口中被禁用的修改器也能应用",
          [m.name for m in disabled.modifiers])

    # 多用户（共享网格）数据
    reset_scene()
    shared = add_cube("CubeSharedA")
    subsurf = shared.modifiers.new("Subsurf", 'SUBSURF')
    subsurf.levels = 1
    other = bpy.data.objects.new("CubeSharedB", shared.data)
    bpy.context.collection.objects.link(other)
    select_only([shared])
    result = bpy.ops.hhz.apply_all_modifiers()
    check(result == {'FINISHED'}, "多用户数据：算符返回 FINISHED", result)
    check(len(shared.modifiers) == 0, "多用户数据的修改器已应用")
    check(shared.data != other.data, "数据已自动复制成单用户")

    # 带形态键：走「转换为网格」兜底
    reset_scene()
    keyed = add_cube("CubeShapeKeys")
    keyed.shape_key_add(name="Basis", from_mix=False)
    subsurf = keyed.modifiers.new("Subsurf", 'SUBSURF')
    subsurf.levels = 1
    select_only([keyed])
    result = bpy.ops.hhz.apply_all_modifiers()
    check(result == {'FINISHED'}, "形态键物体：算符返回 FINISHED", result)
    check(len(keyed.modifiers) == 0, "形态键物体的修改器已应用（转换兜底）",
          [m.name for m in keyed.modifiers])

    # 隐藏的物体
    reset_scene()
    hidden = add_cube("CubeHidden")
    subsurf = hidden.modifiers.new("Subsurf", 'SUBSURF')
    subsurf.levels = 1
    select_only([hidden])
    hidden.hide_viewport = True
    result = bpy.ops.hhz.apply_all_modifiers()
    check(len(hidden.modifiers) == 0, "隐藏物体的修改器也能应用",
          [m.name for m in hidden.modifiers])
    check(hidden.hide_viewport is True, "隐藏状态已还原", hidden.hide_viewport)

    # 曲线上的构建类修改器：Blender 本身不允许应用，插件必须优雅失败（不崩、不改数据）
    reset_scene()
    curve = add_curve("CurveModifier")
    try:
        curve.modifiers.new("Build", 'BUILD')
    except Exception as error:
        skip("该版本曲线不支持 BUILD 修改器：{}".format(error))
    else:
        select_only([curve])
        raised = None
        try:
            bpy.ops.hhz.apply_all_modifiers()
        except RuntimeError as error:
            # 算符报告 ERROR 时，脚本层会收到 RuntimeError —— 这是预期行为
            raised = str(error)
        if raised is None:
            check(len(curve.modifiers) == 0, "曲线修改器已应用（该版本允许）",
                  [m.name for m in curve.modifiers])
        else:
            check("CurveModifier" in raised, "曲线无法应用时给出明确原因", raised)
            check(len(curve.modifiers) == 1, "曲线无法应用时不改动原数据",
                  [m.name for m in curve.modifiers])

    # 网格 + 曲线混合：网格照常应用，曲线明确报错但不影响整体
    reset_scene()
    cube_ok = add_cube("CubeMixed")
    subsurf = cube_ok.modifiers.new("Subsurf", 'SUBSURF')
    subsurf.levels = 1
    curve_mixed = add_curve("CurveMixed")
    has_curve_modifier = True
    try:
        curve_mixed.modifiers.new("Build", 'BUILD')
    except Exception:
        has_curve_modifier = False
    select_only([cube_ok, curve_mixed])
    result = bpy.ops.hhz.apply_all_modifiers()
    check(result == {'FINISHED'}, "网格+曲线混合时算符仍返回 FINISHED", result)
    check(len(cube_ok.modifiers) == 0, "混合场景里网格部分正常应用",
          [m.name for m in cube_ok.modifiers])
    if has_curve_modifier:
        check(len(curve_mixed.modifiers) == 1, "混合场景里无法应用的曲线保持原样",
              [m.name for m in curve_mixed.modifiers])

    # 链接库物体以外：没有修改器时也要正常结束
    reset_scene()
    plain = add_cube("CubeNoModifier")
    select_only([plain])
    result = bpy.ops.hhz.apply_all_modifiers()
    check(result == {'FINISHED'}, "没有修改器时正常结束", result)


def test_remove_modifiers():
    section("功能：一键删除所有修改器")
    reset_scene()
    cube_a = add_cube("CubeRemoveA")
    for index, kind in enumerate(("SUBSURF", "SOLIDIFY", "BEVEL")):
        cube_a.modifiers.new("Mod{}".format(index), kind)
    cube_b = add_cube("CubeRemoveB")
    cube_b.modifiers.new("Subsurf", 'SUBSURF')

    before = len(cube_a.data.vertices)
    select_only([cube_a, cube_b])
    result = bpy.ops.hhz.remove_all_modifiers()
    check(result == {'FINISHED'}, "算符返回 FINISHED", result)
    check(len(cube_a.modifiers) == 0, "CubeRemoveA 修改器已全部删除", len(cube_a.modifiers))
    check(len(cube_b.modifiers) == 0, "CubeRemoveB 修改器已全部删除", len(cube_b.modifiers))
    check(len(cube_a.data.vertices) == before, "确认只是删除、没有烘焙几何",
          "{} -> {}".format(before, len(cube_a.data.vertices)))

    reset_scene()
    plain = add_cube("CubeNoModifier")
    select_only([plain])
    result = bpy.ops.hhz.remove_all_modifiers()
    check(result == {'FINISHED'}, "没有修改器时正常结束", result)


def test_poll():
    section("算符 poll（模式判定）")
    reset_scene()
    cube = add_cube("CubePoll")
    select_only([cube])
    check(bpy.ops.hhz.apply_all_modifiers.poll(), "物体模式下 poll 为真")
    check(bpy.ops.hhz.remove_all_materials.poll(), "物体模式下 poll 为真（材质）")
    check(bpy.ops.hhz.remove_all_modifiers.poll(), "物体模式下 poll 为真（删除修改器）")

    try:
        bpy.ops.object.mode_set(mode='EDIT')
    except Exception as error:
        skip("无法进入编辑模式：{}".format(error))
    else:
        try:
            check(not bpy.ops.hhz.apply_all_modifiers.poll(), "编辑模式下 poll 为假")
            check(not bpy.ops.hhz.remove_all_modifiers.poll(), "编辑模式下 poll 为假（删除修改器）")
        finally:
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

    reset_scene()
    check(not bpy.ops.hhz.apply_all_modifiers.poll(), "空场景下 poll 为假")


def test_menu_operator():
    section("饼菜单按钮依赖的算符属性")
    # 内置算符不会出现在 bpy.types 上，要走 bpy.ops + RNA 内省
    call = getattr(bpy.ops.wm, "call_menu_pie", None)
    check(call is not None, "wm.call_menu_pie 算符存在")
    if call is None:
        return
    try:
        identifiers = [prop.identifier for prop in call.get_rna_type().properties]
    except Exception as error:
        skip("无法读取该算符的属性列表：{}".format(error))
        return
    # N 面板上的「呼出饼菜单」按钮靠 call.name 指定菜单，属性不存在就会在绘制时报错
    check("name" in identifiers, "wm.call_menu_pie 支持 name 属性", identifiers)


def test_bad_hotkey(module):
    section("非法快捷键的安全处理")
    keyconfig = module.compat.addon_keyconfig()
    if keyconfig is None:
        skip("当前环境没有 addon keyconfig，跳过")
        return
    try:
        registered = module.ui.apply_hotkey(
            bpy.context, "NOT_A_REAL_KEY", False, False, False, False)
        check(registered is False, "非法按键标识被安全拒绝（返回 False）", registered)
    except Exception as error:
        check(False, "非法按键标识不应抛异常", "{}: {}".format(type(error).__name__, error))

    module.ui.apply_hotkey(
        bpy.context,
        module.compat.DEFAULT_KEY_TYPE,
        module.compat.DEFAULT_KEY_CTRL,
        module.compat.DEFAULT_KEY_SHIFT,
        module.compat.DEFAULT_KEY_ALT,
        module.compat.DEFAULT_KEY_OSKEY,
    )
    items = _pie_shortcut_items(keyconfig)
    check(len(items) == 1 and items[0].type == module.compat.DEFAULT_KEY_TYPE and items[0].ctrl,
          "随后恢复默认键位成功（Ctrl+E）", [(item.type, item.ctrl) for item in items])


def test_theme(module):
    section("功能：主题颜色（N 面板）")
    theme = module.theme
    context = bpy.context
    current = theme.current_theme(context)
    check(current is not None, "能取到当前主题")
    if current is None:
        return

    # 动态枚举：只列出当前版本真实存在的颜色项
    paths = theme.available_paths(context)
    check(len(paths) >= 20, "可用颜色项 >= 20", len(paths))
    listed = [item[0] for item in theme.target_items(None, context)]
    check(listed == paths, "枚举列表与可用列表一致")
    check(all(theme.resolve(current, path) is not None for path in listed),
          "枚举里每一项都能解析出颜色")
    absent = [path for path, _, _ in theme.TARGETS if theme.resolve(current, path) is None]
    print("  当前版本没有的颜色项：{}".format(absent if absent else "无"))
    check(all(path not in listed for path in absent), "缺失的颜色项没有出现在枚举里")

    # 3 分量（RGB）与 4 分量（RGBA）都要能读写
    rgb_path = "view_3d.space.gradients.high_gradient"
    rgba_path = "view_3d.grid"
    check(theme.component_count(current, rgb_path) == 3,
          "视口背景是 RGB（3 分量）", theme.component_count(current, rgb_path))
    check(theme.component_count(current, rgba_path) == 4,
          "网格线是 RGBA（4 分量）", theme.component_count(current, rgba_path))

    original_rgb = theme.get_color(current, rgb_path)
    original_rgba = theme.get_color(current, rgba_path)

    check(theme.set_color(current, rgb_path, (0.5, 0.25, 0.75, 0.9)),
          "写入 3 分量颜色成功")
    check(color_close(theme.get_color(current, rgb_path), (0.5, 0.25, 0.75, 1.0)),
          "3 分量写回一致（多余的 alpha 被裁掉）", theme.get_color(current, rgb_path))
    check(theme.set_color(current, rgba_path, (0.1, 0.2, 0.3, 0.4)),
          "写入 4 分量颜色成功")
    check(color_close(theme.get_color(current, rgba_path), (0.1, 0.2, 0.3, 0.4)),
          "4 分量写回一致（含 alpha）", theme.get_color(current, rgba_path))

    # 通过偏好设置属性走一遍：验证「实时写入主题」的回调
    preferences = module.compat.get_preferences()
    preferences.theme_target = rgb_path
    preferences.theme_color = (0.11, 0.22, 0.33, 1.0)
    check(color_close(theme.get_color(current, rgb_path), (0.11, 0.22, 0.33, 1.0)),
          "改偏好设置里的颜色会实时写进主题", theme.get_color(current, rgb_path))

    # 需求：这个功能针对的是「可互动的灰色按钮 + 选中后的蓝色按钮」，不是视口
    grey_path = "user_interface.wcol_regular.inner"
    blue_path = "user_interface.wcol_regular.inner_sel"
    check(theme.TARGETS[0][2].startswith("按钮"),
          "按钮类颜色排在列表最前面", theme.TARGETS[0])
    check(grey_path in listed, "灰色按钮颜色在列表里")
    check(blue_path in listed, "选中按钮颜色在列表里")
    check(theme.component_count(current, grey_path) == 4,
          "按钮颜色是 RGBA（4 分量）", theme.component_count(current, grey_path))
    check(theme.has_default(grey_path) and theme.has_default(blue_path),
          "按钮颜色记录了出厂默认值")
    check(not theme.has_default("user_interface.wcol_state.error"),
          "5.x 专属颜色不记录默认值（避免套到旧版上）")
    original_grey = theme.get_color(current, grey_path)
    original_blue = theme.get_color(current, blue_path)

    # 预设：Marathon 酸性配色，改的是按钮 / 状态色
    names = theme.preset_names()
    check(any("Marathon" in name for name in names), "预设里有 Marathon 酸性配色", names)
    preset_name = names[0]
    colors = theme.preset_colors(preset_name)
    check(blue_path in colors and grey_path in colors,
          "预设同时覆盖灰色按钮与选中按钮", sorted(colors)[:3])
    changed = theme.apply_preset(current, preset_name)
    check(changed >= 20, "预设改到 20 个以上颜色项", changed)
    check(color_close(theme.get_color(current, blue_path), colors[blue_path]),
          "预设把「选中按钮」改成了酸性绿", theme.get_color(current, blue_path))
    check(color_close(theme.get_color(current, grey_path), colors[grey_path]),
          "预设把「灰色按钮」改成了近黑绿", theme.get_color(current, grey_path))
    check(theme.get_color(current, blue_path)[1] > theme.get_color(current, blue_path)[2],
          "酸性绿的 G 分量高于 B（确实是绿不是蓝）", theme.get_color(current, blue_path))

    # hex -> 浮点 的换算（主题里存的就是显示值，不需要线性转换）
    check(color_close(theme.hex_color("#C6FF00"), (0.7765, 1.0, 0.0, 1.0)),
          "#C6FF00 换算正确", theme.hex_color("#C6FF00"))

    # 恢复默认：按钮那一组也要能整组还原
    check(theme.restore_default(current, blue_path), "恢复单项默认值成功")
    check(theme.restore_widget_defaults(current) >= 20, "批量恢复控件默认值成功")
    restored_blue = theme.get_color(current, blue_path)
    check(restored_blue[2] > restored_blue[0],
          "恢复默认后选中按钮回到蓝色（默认确实是蓝的）", restored_blue)
    check(theme.has_default(rgb_path) and theme.restore_viewport_defaults(current) >= 11,
          "视口那一组仍可批量恢复默认")
    theme.set_color(current, grey_path, original_grey)
    theme.set_color(current, blue_path, original_blue)

    # 算符存在 + 真跑一遍
    for idname in ("theme_read", "theme_apply_preset", "theme_restore_default",
                   "theme_save_preferences"):
        check(operator_class(module, idname) is not None,
              "算符 hhz.{} 已注册".format(idname))

    result = bpy.ops.hhz.theme_read()
    check(result == {'FINISHED'}, "「读取当前值」算符可用", result)
    result = bpy.ops.hhz.theme_apply_preset(preset=preset_name)
    check(result == {'FINISHED'}, "「应用预设」算符可用", result)
    result = bpy.ops.hhz.theme_restore_default(scope='VIEWPORT')
    check(result == {'FINISHED'}, "「全部视口恢复默认」算符可用", result)
    result = bpy.ops.hhz.theme_restore_default(scope='WIDGET')
    check(result == {'FINISHED'}, "「全部控件恢复默认」算符可用", result)

    # 面板挂在 N 面板里（需求：只进 N 面板）
    panel = getattr(bpy.types, "HHZ_PT_theme", None)
    check(panel is not None, "主题面板已注册")
    if panel is not None:
        check(not getattr(panel, "bl_parent_id", ""),
              "主题面板是顶层面板（N 面板里必定显示）", getattr(panel, "bl_parent_id", ""))
        check(panel.bl_category == "红红子lab", "主题面板在红红子lab 标签页里", panel.bl_category)
        check(panel.bl_space_type == 'VIEW_3D' and panel.bl_region_type == 'UI',
              "主题面板在 3D 视图侧栏（N 面板）")

    # 还原测试前的颜色
    theme.set_color(current, rgb_path, original_rgb)
    theme.set_color(current, rgba_path, original_rgba)


def pie_operator_idnames(source):
    """静态解析某个 Panel/Menu 类里出现的算符 idname。"""
    return class_operator_idnames(source, "HHZ_MT_pie")


def class_operator_idnames(source, class_name):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            found = set()
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "operator"
                        and inner.args
                        and isinstance(inner.args[0], ast.Constant)):
                    found.add(inner.args[0].value)
            return found
    return set()


def panels_with_parent_id(source):
    """列出真正定义了 bl_parent_id 的面板类（文档字符串里提到不算）。"""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [getattr(base, "id", getattr(base, "attr", "")) for base in node.bases]
        if "Panel" not in bases:
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == "bl_parent_id":
                    found.append("{}（行 {}）".format(node.name, statement.lineno))
    return found


def test_pie_unchanged(module):
    section("饼菜单内容（需求：设置入口挪到 N 面板，主题功能不进饼菜单）")
    ui_path = os.path.join(os.path.dirname(os.path.abspath(module.__file__)), "ui.py")
    with open(ui_path, encoding="utf-8") as handle:
        source = handle.read()

    found = pie_operator_idnames(source)
    expected = {
        "hhz.remove_all_materials",
        "hhz.apply_all_modifiers",
        "hhz.remove_all_modifiers",
    }
    check(found == expected, "饼菜单只剩三个功能入口（设置已移出）",
          sorted(found) if found else found)

    in_settings_panel = class_operator_idnames(source, "HHZ_PT_settings")
    check("hhz.open_preferences" in in_settings_panel,
          "设置入口出现在 N 面板的「插件设置」子面板里", sorted(in_settings_panel))
    check("hhz.open_preferences" not in found, "设置入口确实不在饼菜单里")


def test_panel_order(module):
    section("功能：N 面板顺序（拖动排序）")
    panels = module.panels
    ui = module.ui
    context = bpy.context

    # ---- 列表模型（纯逻辑，可精确断言）----
    check(len(panels.SECTIONS) >= 3, "可排序分块至少 3 个", len(panels.SECTIONS))
    check(panels.parse_order("") == list(panels.DEFAULT_SECTION_ORDER),
          "空配置回落到默认顺序", panels.parse_order(""))
    check(panels.parse_order("settings,theme") ==
          ["settings", "theme", "hotkey"], "解析顺序并补齐缺失分块",
          panels.parse_order("settings,theme"))
    check(panels.parse_order("nope,theme,theme,settings") ==
          ["theme", "settings", "hotkey"], "未知项丢弃、重复项去重",
          panels.parse_order("nope,theme,theme,settings"))
    check(panels.move_item(["a", "b", "c"], 0, 2) == ["b", "c", "a"], "向下移动元素")
    check(panels.move_item(["a", "b", "c"], 2, 0) == ["c", "a", "b"], "向上移动元素")
    check(panels.move_item(["a", "b"], 1, 1) == ["a", "b"], "原地不动时不变")

    # ---- 拖动位移 -> 目标槽位 ----
    check(panels.target_from_drag(3, 0, 0) == 0, "没有位移就不换位")
    check(panels.target_from_drag(3, 0, 20, 20.0) == 1, "向下拖一行换到下一位")
    check(panels.target_from_drag(3, 2, -40, 20.0) == 0, "向上拖两行换到最上")
    check(panels.target_from_drag(3, 0, 999, 20.0) == 2, "超界被夹到末尾")
    check(panels.target_from_drag(3, 2, -999, 20.0) == 0, "超界被夹到开头")

    # ---- 顺序真的写进了面板类的 bl_order ----
    preferences = module.compat.get_preferences()
    check(preferences is not None, "偏好设置可访问")
    preferences.panel_order = "settings,theme,hotkey"
    check(bpy.types.HHZ_PT_main.bl_order == panels.MAIN_PANEL_ORDER,
          "主面板固定在最前（bl_order={}）".format(panels.MAIN_PANEL_ORDER),
          bpy.types.HHZ_PT_main.bl_order)
    check(getattr(bpy.types, "HHZ_PT_settings", None).bl_order == 1,
          "第 1 位面板的 bl_order = 1", getattr(bpy.types.HHZ_PT_settings, "bl_order", None))
    check(bpy.types.HHZ_PT_theme.bl_order == 2, "第 2 位面板的 bl_order = 2",
          bpy.types.HHZ_PT_theme.bl_order)
    check(bpy.types.HHZ_PT_hotkey.bl_order == 3, "第 3 位面板的 bl_order = 3",
          bpy.types.HHZ_PT_hotkey.bl_order)
    check(bpy.types.HHZ_PT_order.bl_order == panels.ORDER_PANEL_ORDER,
          "顺序编辑器被固定在最后", bpy.types.HHZ_PT_order.bl_order)

    values = [bpy.types.HHZ_PT_main.bl_order, bpy.types.HHZ_PT_settings.bl_order,
              bpy.types.HHZ_PT_theme.bl_order, bpy.types.HHZ_PT_hotkey.bl_order,
              bpy.types.HHZ_PT_order.bl_order]
    check(len(set(values)) == len(values), "各面板 bl_order 互不相同（顺序无歧义）", values)

    order = ui.apply_panel_order(context)
    check(order == ["settings", "theme", "hotkey"], "注册时的应用函数返回当前顺序", order)

    preferences.panel_order = ",".join(panels.DEFAULT_SECTION_ORDER)
    ui.apply_panel_order(context)

    # ---- 侧栏标签页：只读展示（重排不可行，原因见 README 与 panels.py 的注释）----
    names = panels.tab_names()
    check(module.compat.ADDON_NAME in names, "标签页列表里有本插件的标签页", names)
    print("  当前侧栏标签页：{}".format(names))
    groups = panels.panel_groups()
    check(module.compat.ADDON_NAME in groups, "能读出本插件标签页的面板分组",
          sorted(groups.keys()))
    check("HHZ_PT_main" in groups[module.compat.ADDON_NAME],
          "分组里能找到本插件的主面板", groups.get(module.compat.ADDON_NAME))
    check(all(not getattr(panel, "bl_parent_id", "")
              for _, group in panels.collect_tabs() for panel in group),
          "子面板没有被当成独立标签页")

    # ---- 注册顺序那一层（3D 视图侧栏真正认的）----
    keys = panels.our_panel_keys(["theme", "settings", "hotkey"])
    check(keys[:1] == ["HHZ_PT_main"], "主面板排在最前", keys)
    check(keys[-1] == "HHZ_PT_order", "顺序编辑器排在最后", keys)
    check(keys[1:4] == ["HHZ_PT_theme", "HHZ_PT_settings", "HHZ_PT_hotkey"],
          "可排序面板按给定顺序展开", keys)

    registered, failures = panels.reregister_our_panels(["theme", "settings", "hotkey"])
    check(registered == 5 and not failures, "重新注册本插件 5 个面板成功",
          "{} 个 / {}".format(registered, failures))
    check(all(getattr(bpy.types, key, None) is not None
              for key in ("HHZ_PT_main", "HHZ_PT_theme", "HHZ_PT_hotkey",
                          "HHZ_PT_settings", "HHZ_PT_order")),
          "重新注册后所有面板类都还在")
    check(sorted(panels.panel_groups().keys()) == sorted(groups.keys()),
          "重新注册后标签页一个都没少", sorted(panels.panel_groups().keys()))

    # 只允许重排本插件自己的面板：连别的插件/内置面板一起重注册会让 Blender 4.0/4.2 段错误崩溃
    check(operator_class(module, "tab_drag") is None,
          "危险的标签页拖动算符没有注册")
    check(operator_class(module, "tab_restore") is None,
          "危险的标签页还原算符没有注册")

    # 恢复默认顺序
    preferences.panel_order = ",".join(panels.DEFAULT_SECTION_ORDER)
    ui.apply_panel_order(context)

    # ---- 算符存在 ----
    for idname in ("section_drag", "theme_read", "theme_apply_preset",
                   "theme_restore_default", "theme_save_preferences"):
        check(operator_class(module, idname) is not None,
              "算符 hhz.{} 已注册".format(idname))
    check(not panels.DRAG_STATE, "拖动状态没有残留", panels.DRAG_STATE)


def test_unregister(module_name, module):
    section("卸载清理")
    keyconfig = module.compat.addon_keyconfig()
    try:
        bpy.ops.preferences.addon_disable(module=module_name)
        disabled = True
    except Exception as error:
        disabled = False
        print("  停用失败：{}".format(error))
    check(disabled, "插件可以正常停用")
    check(getattr(bpy.types, PIE_IDNAME, None) is None, "饼菜单类已注销")
    check(getattr(bpy.types, "HHZ_PT_main", None) is None, "N 面板类已注销")
    if keyconfig is not None:
        check(len(_pie_shortcut_items(keyconfig)) == 0, "注册的键位已清理",
              len(_pie_shortcut_items(keyconfig)))


# ---------------------------------------------------------------------- main


def main():
    print("红红子lab 真机测试 | 模式={} | 安装包={}".format(MODE, ZIP_PATH))

    if not ZIP_PATH or not os.path.exists(ZIP_PATH):
        print("找不到安装包：{}".format(ZIP_PATH))
        return 1

    module_name = install_and_enable()
    if module_name is None:
        print("\n插件没有启用，测试中止")
        return 1

    module = __import__(module_name, fromlist=["compat"])
    print("  模块 __package__ = {}".format(getattr(module.compat, "PACKAGE", "?")))

    run("注册与界面结构", test_registration, module_name, module)
    run("图标名合法性", test_icons, module)
    run("饼菜单按钮依赖", test_menu_operator)
    run("键位注册与改键", test_keymap, module)
    run("非法快捷键处理", test_bad_hotkey, module)
    run("一键删除所有材质", test_materials)
    run("一键应用所有修改器", test_apply_modifiers, module)
    run("一键删除所有修改器", test_remove_modifiers)
    run("主题颜色（N 面板）", test_theme, module)
    run("饼菜单内容未被改动", test_pie_unchanged, module)
    run("N 面板顺序（拖动排序）", test_panel_order, module)
    run("算符 poll", test_poll)
    run("卸载清理", test_unregister, module_name, module)

    print("\n================ 结果 ================")
    print("Blender {}：{} 项断言，{} 项失败".format(
        bpy.app.version_string, CHECK_COUNT[0], len(FAILURES)))
    for failure in FAILURES:
        print("  - {}".format(failure))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
