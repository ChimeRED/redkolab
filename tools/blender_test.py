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

    for idname in ("HHZ_PT_theme", "HHZ_PT_hotkey", "HHZ_PT_settings",
                   "HHZ_PT_order", "HHZ_PT_layout"):
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
    check(theme.has_default("user_interface.wcol_regular.inner"), "按钮颜色记录了出厂默认值")
    check(not theme.has_default("user_interface.wcol_state.error", version=(4, 0, 2)),
          "5.x 专属项在 4.x 上不生效（不会被套上 5.x 的默认值）")
    check(theme.has_default("user_interface.wcol_state.error", version=(5, 2, 2))
          or not theme.has_default("user_interface.wcol_state.error"),
          "5.x 专属项在 5.x 上按 5X 表取值")
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
    result = bpy.ops.hhz.theme_restore_default(scope='GROUP')
    check(result == {'FINISHED'}, "「恢复本组」算符可用", result)
    result = bpy.ops.hhz.theme_restore_default(scope='ALL')
    check(result == {'FINISHED'}, "「全部恢复」算符可用", result)

    # ---- 分组：底色 / 文字与图标 / 边框 是分开的 ----
    groups = theme.group_names(listed)
    check("按钮 底色" in groups and "按钮 文字与图标" in groups and "按钮 边框" in groups,
          "按钮的底色 / 文字与图标 / 边框是三个独立分组", groups[:6])
    check(theme.group_of(grey_path) == "按钮 底色", "普通按钮底色归到「按钮 底色」",
          theme.group_of(grey_path))
    check(theme.group_of("user_interface.wcol_regular.text") == "按钮 文字与图标",
          "按钮文字与图标归到「按钮 文字与图标」")
    check(theme.group_of("user_interface.wcol_regular.outline") == "按钮 边框",
          "按钮边框归到「按钮 边框」")
    border_paths = theme.paths_in_group("按钮 边框", listed)
    check(len(border_paths) >= 3 and all(path.endswith(".outline") for path in border_paths),
          "「按钮·边框」组里全是 outline 项", border_paths[:4])
    check(isinstance(theme.differing_paths(current, theme.default_paths()), list),
          "能列出与出厂默认值不同的颜色项（面板上会显示这个数字）")
    check(len(theme.default_paths()) >= 40, "记录了出厂默认值的项 >= 40",
          len(theme.default_paths()))

    # ---- 图标颜色 ----
    icon_paths = [path for path in listed if ".icon_" in path]
    check(len(icon_paths) >= 6, "图标颜色项 >= 6", icon_paths)

    # ---- 时间线当前帧：4.x 有、5.x 已被 Blender 移除 ----
    frame_path = "dopesheet_editor.frame_current"
    has_frame = frame_path in listed
    if bpy.app.version < (5, 0, 0):
        check(has_frame, "4.x 上时间线当前帧色在列表里", frame_path)
        check(theme.has_default(frame_path), "时间线当前帧记录了出厂默认值")
        check(color_close(theme.get_color(current, frame_path),
                          theme.default_of(frame_path)),
              "时间线当前帧默认是蓝色", theme.get_color(current, frame_path))
    else:
        check(not has_frame, "5.x 上时间线当前帧色已被 Blender 移除（自动隐藏）", frame_path)

    # ---- 自定义配色：保存 -> 套用 -> 删除 ----
    stored = getattr(preferences, "theme_custom", "")
    saved_colors = theme.capture_colors(current, listed)
    text, ok = theme.custom_save(stored, "测试配色", saved_colors)
    check(ok and "测试配色" in theme.custom_names(text), "自定义配色可以保存")
    check(theme.custom_colors(text, "测试配色") is not None, "保存后能读回来")
    check(len(theme.custom_colors(text, "测试配色")) == len(saved_colors),
          "存下来的颜色项数量一致", len(theme.custom_colors(text, "测试配色")))
    check(theme.load_custom(theme.dump_custom(theme.load_custom(text))) == theme.load_custom(text),
          "自定义配色 JSON 往返一致")
    theme.set_color(current, grey_path, (0.9, 0.1, 0.1, 1.0))
    applied = 0
    for item_path, color in (theme.custom_colors(text, "测试配色") or {}).items():
        if theme.set_color(current, item_path, color):
            applied += 1
    check(applied >= 20, "套用自定义配色能写回很多项", applied)
    text2, removed = theme.custom_delete(text, "测试配色")
    check(removed and "测试配色" not in theme.custom_names(text2), "自定义配色可以删除")

    # ---- 字体设置 ----
    font_items = theme.font_items(context)
    check(len(font_items) >= 5, "字体设置项 >= 5", [item[0] for item in font_items])
    keys = [item[0] for item in font_items]
    for key in ("ui_scale", "font_path_ui", "text_hinting", "use_text_antialiasing"):
        check(key in keys, "字体设置里有 {}".format(key), keys)
    original_scale = theme.font_get(context, "ui_scale")
    check(theme.font_set(context, "ui_scale", 1.25), "能改界面缩放")
    check(abs(float(theme.font_get(context, "ui_scale")) - 1.25) < 0.001,
          "界面缩放写回一致", theme.font_get(context, "ui_scale"))
    check(theme.font_set(context, "ui_scale", original_scale), "能改回去")
    check(theme.font_get(context, "font_path_ui") == "", "界面字体默认是空（用自带的）")
    check("ui_scale" in theme.font_defaults(), "记录了字体出厂默认值")
    check(theme.font_restore(context) >= 5, "字体一键恢复默认可用")

    # 面板挂在 N 面板里（需求：只进 N 面板）
    panel = getattr(bpy.types, "HHZ_PT_theme", None)
    check(panel is not None, "主题面板已注册")
    if panel is not None:
        check(not getattr(panel, "bl_parent_id", ""),
              "主题面板是顶层面板（N 面板里必定显示）", getattr(panel, "bl_parent_id", ""))
        check(panel.bl_category == "红红子lab", "主题面板在红红子lab 标签页里", panel.bl_category)
        check(panel.bl_space_type == 'VIEW_3D' and panel.bl_region_type == 'UI',
              "主题面板在 3D 视图侧栏（N 面板）")

    # ---- 分开设置：底色 / 图标与文字 / 边框 各自独立 ----
    families = theme.family_items(context)
    check(len(families) >= 15, "可选控件家族 >= 15", len(families))
    prefix = families[0][0] if families else "wcol_regular"
    check(prefix == "wcol_regular", "第一个家族是普通按钮", prefix)
    parts = theme.read_family(current, prefix)
    for member in ("inner", "text", "outline", "inner_sel"):
        check(member in parts, "家族里读得到 {}".format(member), sorted(parts))

    inner_path = theme.family_path(prefix, "inner")
    text_path = theme.family_path(prefix, "text")
    original_inner = theme.get_color(current, inner_path)
    original_text = theme.get_color(current, text_path)
    check(theme.write_family(current, prefix, "inner", (0.9, 0.1, 0.1, 1.0)),
          "能单独改底色")
    check(color_close(theme.get_color(current, inner_path), (0.9, 0.1, 0.1, 1.0)),
          "底色写进去了", theme.get_color(current, inner_path))
    check(color_close(theme.get_color(current, text_path), original_text),
          "改底色不会动文字色（分开设置的核心）", theme.get_color(current, text_path))
    theme.write_family(current, prefix, "inner", original_inner)

    # ---- 可读文字色：亮底配深字、暗底配浅字 ----
    check(theme.luminance((1.0, 1.0, 1.0, 1.0)) > 0.9, "白色亮度接近 1")
    check(theme.luminance((0.0, 0.0, 0.0, 1.0)) < 0.01, "黑色亮度接近 0")
    check(theme.readable_text_color((1.0, 1.0, 0.9, 1.0)) == theme.TEXT_ON_LIGHT,
          "亮底 -> 深色字")
    check(theme.readable_text_color((0.05, 0.06, 0.04, 1.0)) == theme.TEXT_ON_DARK,
          "暗底 -> 浅色字")
    acid = theme.hex_color("#C6FF00")
    check(abs(theme.luminance(theme.readable_text_color(acid)) - theme.luminance(acid)) > 0.3,
          "酸性绿底上自动配出来的字对比度足够",
          (theme.color_to_hex(acid), theme.color_to_hex(theme.readable_text_color(acid))))

    # ---- 预设必须成对给出底色与文字，否则就是"字看不清" ----
    for preset in theme.preset_names():
        colors = theme.preset_colors(preset)
        missing = []
        low_contrast = []
        for family_prefix, _label in theme.WIDGET_FAMILIES:
            for background_key, text_key in (("inner", "text"), ("inner_sel", "text_sel")):
                background = colors.get(theme.family_path(family_prefix, background_key))
                text_color = colors.get(theme.family_path(family_prefix, text_key))
                if background is None:
                    continue
                if text_color is None:
                    missing.append("{}.{}".format(family_prefix, background_key))
                    continue
                if abs(theme.luminance(background) - theme.luminance(text_color)) < 0.3:
                    low_contrast.append("{}.{}".format(family_prefix, background_key))
        check(not missing, "预设「{}」每个家族都配了文字色".format(preset), missing[:6])
        check(not low_contrast, "预设「{}」每个家族的文字/底色对比都够".format(preset),
              low_contrast[:6])

    # 真套一次，直接查实际主题里的对比度
    theme.apply_preset(current, theme.preset_names()[0])
    for family_prefix, _label in theme.WIDGET_FAMILIES[:6]:
        check(not theme.family_contrast_report(current, family_prefix),
              "套用预设后「{}」实际主题里没有看不清的字".format(family_prefix),
              theme.family_contrast_report(current, family_prefix))

    # ---- 调色盘 / 十六进制 / 应用颜色 ----
    preferences.simple_mode = False   # 下面要按单个家族名设置，先关掉简洁模式
    preferences.family = prefix
    preferences.theme_target = grey_path
    check(module.ui.load_family_parts(context, preferences) >= 4, "能把家族颜色读进面板属性")
    preferences.palette_color = (0.5, 0.25, 0.75, 1.0)
    preferences.custom_hex = ""
    result = bpy.ops.hhz.theme_apply_color()
    check(result == {'FINISHED'}, "「应用颜色」算符可用", result)
    check(color_close(theme.get_color(current, grey_path), (0.5, 0.25, 0.75, 1.0)),
          "调色盘颜色被写进当前项", theme.get_color(current, grey_path))

    preferences.custom_hex = "#123456"
    check(color_close(tuple(preferences.palette_color), theme.hex_color("#123456")),
          "填十六进制会同步到调色盘", tuple(preferences.palette_color))
    result = bpy.ops.hhz.theme_apply_color()
    check(color_close(theme.get_color(current, grey_path), theme.hex_color("#123456")),
          "十六进制优先被应用", theme.get_color(current, grey_path))

    preferences.custom_hex = "这不是颜色"
    result = bpy.ops.hhz.theme_apply_color()
    check(result == {'CANCELLED'}, "坏十六进制被拒绝而不是乱写", result)
    preferences.custom_hex = ""

    check(theme.hex_color("#abc") is not None, "#abc 简写也认")
    check(theme.hex_color("xyz") is None, "坏写法返回 None")
    check(theme.color_to_hex((0.7765, 1.0, 0.0, 1.0)) == "#C6FF00",
          "颜色转十六进制正确", theme.color_to_hex((0.7765, 1.0, 0.0, 1.0)))

    result = bpy.ops.hhz.theme_auto_contrast()
    check(result == {'FINISHED'}, "「自动配文字色」算符可用", result)
    check(not theme.family_contrast_report(current, prefix),
          "自动配文字色之后该家族没有对比度问题",
          theme.family_contrast_report(current, prefix))
    result = bpy.ops.hhz.theme_part_load()
    check(result == {'FINISHED'}, "「从当前主题读回」算符可用", result)

    # ---- 全局按钮配色：一次覆盖所有家族 + 各编辑器的选中高亮 ----
    listed_set = set(listed)
    accents = ("outliner.selected_highlight", "outliner.active", "file_browser.selected_file",
               "properties.active_modifier", "user_interface.editor_outline")
    present = [path for path in accents if path in listed_set]
    check(len(present) >= 3, "全局选中高亮条目在当前版本可用", present)
    check("outliner.selected_highlight" in listed_set,
          "大纲视图选中行（默认偏蓝的那个）终于进了颜色列表")

    global_colors = theme.global_button_colors((0.09, 0.10, 0.07, 1.0), (0.776, 1.0, 0.0, 1.0))
    check(len(global_colors) >= len(theme.WIDGET_FAMILIES) * 7,
          "全局配色覆盖每个家族的 7 个成员", len(global_colors))
    check(all(theme.family_path(family_prefix, "inner_sel") in global_colors
              for family_prefix, _label in theme.WIDGET_FAMILIES),
          "全局配色含每个家族的选中底色")
    low_contrast_global = []
    for family_prefix, _label in theme.WIDGET_FAMILIES:
        for background_key, text_key in (("inner", "text"), ("inner_sel", "text_sel")):
            background = global_colors.get(theme.family_path(family_prefix, background_key))
            text_color = global_colors.get(theme.family_path(family_prefix, text_key))
            if background is None or text_color is None:
                continue
            if abs(theme.luminance(background) - theme.luminance(text_color)) < 0.3:
                low_contrast_global.append(family_prefix)
    check(not low_contrast_global, "全局配色的文字/底色对比也够", low_contrast_global[:5])

    before_outliner = theme.get_color(current, "outliner.selected_highlight")
    applied_global = theme.apply_colors(current, global_colors)
    check(applied_global >= 100, "全局配色一次写进很多项", applied_global)
    after_outliner = theme.get_color(current, "outliner.selected_highlight")
    expected_tint = theme.scale_color((0.776, 1.0, 0.0, 1.0), 0.35)
    check(color_close(after_outliner, expected_tint),
          "大纲选中行 = 调暗后的选中色", (after_outliner, expected_tint))
    check(not color_close(after_outliner, (0.114, 0.192, 0.302)),
          "大纲选中行不再是 Blender 默认那个偏蓝",
          (after_outliner, (0.114, 0.192, 0.302)))
    check(theme.luminance(after_outliner) < 0.35,
          "大纲选中行用调暗的选中色（上面的浅色文字仍看得清）", after_outliner)
    check(not theme.family_contrast_report(current, prefix),
          "全局配色之后按钮文字对比度正常",
          theme.family_contrast_report(current, prefix))

    result = bpy.ops.hhz.theme_apply_global()
    check(result == {'FINISHED'}, "「应用到全部按钮」算符可用", result)
    check(theme.label_of(grey_path).startswith("普通按钮"),
          "label_of 能取到中文标签", theme.label_of(grey_path))

    # ---- 标签里不用 · 这类符号（有些自定义界面字体画不出来会变乱码）----
    check("\u00b7" not in source_of(module, "theme.py")
          and "\u00b7" not in source_of(module, "ui.py"),
          "主题配置与面板代码里都没有中点号（避免字体缺字变乱码）")
    bad_labels = [label for _path, label, _group in theme.TARGETS if "\u00b7" in label]
    check(not bad_labels, "颜色项标签里没有中点号这类符号", bad_labels[:5])
    bad_groups = [group for _path, _label, group in theme.TARGETS if "\u00b7" in group]
    check(not bad_groups, "分组名里也没有中点号", bad_groups[:5])

    # ---- 简洁模式：选项要少，但关掉之后仍能拿到全部 ----
    preferences.simple_mode = True
    family_keys = [item[0] for item in module.ui.family_items(preferences, context)]
    check(len(family_keys) <= 8, "简洁模式下控件家族选项 <= 8 个", family_keys)
    check("ALL" in family_keys and "BUTTONS" in family_keys,
          "简洁模式有「全部控件」与「按钮」分组", family_keys)
    check(theme.families_for_key("ALL") == tuple(
        prefix for prefix, _name in theme.WIDGET_FAMILIES),
        "「全部控件」= 全部家族")
    check(len(theme.families_for_key("BUTTONS")) >= 3,
          "「按钮」分组至少含 3 个家族", theme.families_for_key("BUTTONS"))
    check(theme.families_for_key("wcol_menu") == ("wcol_menu",),
          "直接给家族名也认（精调时用）")
    check(theme.families_for_key("__none__") == (), "无效键返回空")

    simple_paths = [item[0] for item in theme.target_items(preferences, context)]
    check(len(simple_paths) <= 40, "简洁模式下颜色项 <= 40 个", len(simple_paths))
    check("outliner.selected_highlight" in simple_paths, "简洁模式保留了常用的全局选中高亮")
    preferences.simple_mode = False
    full_paths = [item[0] for item in theme.target_items(preferences, context)]
    check(len(full_paths) > len(simple_paths) * 3,
          "关掉简洁模式后能看到全部颜色项", (len(simple_paths), len(full_paths)))
    preferences.simple_mode = True

    # 选「全部控件」改一次底色 -> 所有家族一起变
    preferences.family = "ALL"
    check(len(module.ui.family_keys(preferences)) == len(theme.WIDGET_FAMILIES),
          "面板把「全部控件」展开成全部家族")
    before_one = theme.get_color(current, theme.family_path("wcol_tab", "inner"))
    preferences.part_inner = (0.25, 0.5, 0.75, 1.0)
    after_all = [theme.get_color(current, theme.family_path(prefix, "inner"))
                 for prefix, _name in theme.WIDGET_FAMILIES]
    check(all(item is not None and theme.color_close(item, (0.25, 0.5, 0.75, 1.0))
              for item in after_all),
          "选「全部控件」时改底色会应用到每个家族", (before_one, after_all[:2]))
    preferences.simple_mode = False
    preferences.family = prefix
    preferences.simple_mode = True

    # ---- 默认值表：全部恢复之后每一项都要真的回到记录值 ----
    # 这是"全部恢复完还有地方是绿的"的防线：预设/全局配色改过的项（大纲选中行等）
    # 必须都能被恢复回来。
    recorded = theme.default_paths()
    check(len(recorded) >= 150, "记录的出厂默认值项数 >= 150", len(recorded))
    theme.restore_defaults(current, recorded)
    not_restored = []
    for item in recorded:
        live = theme.get_color(current, item)
        expected = theme.default_of(item)
        if live is None or expected is None or not theme.color_close(live, expected):
            not_restored.append("{}: {} != {}".format(item, live, expected))
    check(not not_restored,
          "全部恢复之后每一项都回到记录的出厂值（{} 项）".format(len(recorded)),
          not_restored[:6])
    for item in ("outliner.selected_highlight", "outliner.active", "file_browser.selected_file",
                 "properties.active_modifier", "user_interface.editor_outline"):
        if item in listed_set:
            check(theme.has_default(item), "{} 记录了出厂默认值".format(item))

    # 面板上这些东西都要有（静态检查）----
    panel_source = source_of(module, "ui.py")
    for token in ('"palette_color"', '"custom_hex"', "应用颜色", "控件颜色分开设置",
                  "当前值 {}", "自动配文字色", "从当前主题读回",
                  "part_inner:", "part_text:", "part_outline:",
                  '"part_" + member', "全局按钮配色", "应用到全部按钮", '"global_base"',
                  "当前项 {}"):
        check(token in panel_source, "主题面板里有 {}".format(token))

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
        "hhz.stairs",
    }
    check(found == expected, "饼菜单就是四个功能入口（设置已移出）",
          sorted(found) if found else found)

    in_modeling = class_operator_idnames(source, "HHZ_PT_main")
    check("hhz.stairs" in in_modeling, "一键楼梯也出现在建模页的主面板里", sorted(in_modeling))

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
    check(len(panels.SECTIONS) >= 4, "可排序分块至少 4 个", len(panels.SECTIONS))
    check(panels.parse_order("") == list(panels.DEFAULT_SECTION_ORDER),
          "空配置回落到默认顺序", panels.parse_order(""))
    check(panels.parse_order("settings,theme") ==
          ["settings", "theme", "hotkey", "layout"], "解析顺序并补齐缺失分块",
          panels.parse_order("settings,theme"))
    check(panels.parse_order("nope,theme,theme,settings") ==
          ["theme", "settings", "hotkey", "layout"], "未知项丢弃、重复项去重",
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
    check(bpy.types.HHZ_PT_pages.bl_order == panels.PAGES_PANEL_ORDER,
          "分页标签条固定在 bl_order={}".format(panels.PAGES_PANEL_ORDER),
          bpy.types.HHZ_PT_pages.bl_order)
    check(bpy.types.HHZ_PT_main.bl_order == panels.MAIN_PANEL_ORDER,
          "主面板紧随其后（bl_order={}）".format(panels.MAIN_PANEL_ORDER),
          bpy.types.HHZ_PT_main.bl_order)
    check(getattr(bpy.types, "HHZ_PT_settings", None).bl_order == 2,
          "第 1 位面板的 bl_order = 2", getattr(bpy.types.HHZ_PT_settings, "bl_order", None))
    check(bpy.types.HHZ_PT_theme.bl_order == 3, "第 2 位面板的 bl_order = 3",
          bpy.types.HHZ_PT_theme.bl_order)
    check(bpy.types.HHZ_PT_hotkey.bl_order == 4, "第 3 位面板的 bl_order = 4",
          bpy.types.HHZ_PT_hotkey.bl_order)
    check(bpy.types.HHZ_PT_layout.bl_order == 5, "第 4 位面板（页面布局）的 bl_order = 5",
          bpy.types.HHZ_PT_layout.bl_order)
    check(bpy.types.HHZ_PT_order.bl_order == panels.ORDER_PANEL_ORDER,
          "顺序编辑器被固定在最后", bpy.types.HHZ_PT_order.bl_order)

    values = [bpy.types.HHZ_PT_pages.bl_order, bpy.types.HHZ_PT_main.bl_order,
              bpy.types.HHZ_PT_settings.bl_order, bpy.types.HHZ_PT_theme.bl_order,
              bpy.types.HHZ_PT_hotkey.bl_order, bpy.types.HHZ_PT_layout.bl_order,
              bpy.types.HHZ_PT_order.bl_order]
    check(len(set(values)) == len(values), "各面板 bl_order 互不相同（顺序无歧义）", values)

    order = ui.apply_panel_order(context)
    check(order == ["settings", "theme", "hotkey", "layout"],
          "注册时的应用函数返回当前顺序", order)

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
    keys = panels.our_panel_keys(panels.read_section_order(preferences))
    check(keys[:2] == ["HHZ_PT_pages", "HHZ_PT_main"],
          "分页标签条与主面板排在最前", keys)
    check(keys[-1] == "HHZ_PT_order", "顺序编辑器排在最后", keys)
    check("HHZ_PT_layout" in keys and "HHZ_PT_theme" in keys,
          "四个可排序面板都在列表里（theme / hotkey / settings / layout）", keys)

    registered, failures = panels.reregister_our_panels(panels.read_section_order(preferences))
    check(registered == 7 and not failures, "重新注册本插件 7 个面板成功",
          "{} 个 / {}".format(registered, failures))
    check(all(getattr(bpy.types, key, None) is not None
              for key in ("HHZ_PT_pages", "HHZ_PT_main", "HHZ_PT_theme", "HHZ_PT_hotkey",
                          "HHZ_PT_settings", "HHZ_PT_order", "HHZ_PT_layout")),
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


def test_pages(module):
    section("功能：分页（建模 / 工具 / 主题）")
    pages = module.pages
    context = bpy.context

    check(len(pages.PAGE_ITEMS) == 3, "一共三页", len(pages.PAGE_ITEMS))
    labels = [item[1] for item in pages.PAGE_ITEMS]
    check(labels == ["建模", "工具", "主题"], "三页名字是 建模 / 工具 / 主题", labels)

    manager = context.window_manager
    check(hasattr(manager, pages.PROP_NAME), "WindowManager 上有「当前页」属性",
          getattr(manager, pages.PROP_NAME, None))
    prop = bpy.types.WindowManager.bl_rna.properties.get(pages.PROP_NAME)
    check(prop is not None, "「当前页」属性已注册到 RNA")
    if prop is not None:
        ids = [item.identifier for item in prop.enum_items]
        check(ids == ["MODELING", "TOOLS", "THEME"], "枚举项三个且顺序正确", ids)

    # 切换页面 -> 各功能面板的 poll 跟着变
    owners = {
        "HHZ_PT_main": pages.PAGE_MODELING,
        "HHZ_PT_hotkey": pages.PAGE_TOOLS,
        "HHZ_PT_settings": pages.PAGE_TOOLS,
        "HHZ_PT_order": pages.PAGE_TOOLS,
        "HHZ_PT_layout": pages.PAGE_TOOLS,
        "HHZ_PT_theme": pages.PAGE_THEME,
    }
    for page in ("MODELING", "TOOLS", "THEME"):
        setattr(manager, pages.PROP_NAME, page)
        check(pages.current(context) == page, "切到「{}」页".format(pages.PAGE_LABELS[page]))
        for idname, owner in owners.items():
            panel = getattr(bpy.types, idname, None)
            if panel is None:
                continue
            expected = (page == owner)
            try:
                visible = bool(panel.poll(context))
            except Exception as error:
                visible = "poll 异常：{}".format(error)
            check(visible is expected,
                  "{} 在「{}」页{}".format(
                      idname, pages.PAGE_LABELS[page], "显示" if expected else "隐藏"),
                  visible)

    # 标签条本身：无标题、固定最前、和功能面板同一个侧栏标签页
    bar = getattr(bpy.types, "HHZ_PT_pages", None)
    check(bar is not None, "分页标签条面板已注册")
    if bar is not None:
        check("HIDE_HEADER" in set(bar.bl_options), "标签条没有标题栏（只有一行按钮）",
              list(bar.bl_options))
        check(bar.bl_order == module.panels.PAGES_PANEL_ORDER,
              "标签条固定在 bl_order={}".format(module.panels.PAGES_PANEL_ORDER), bar.bl_order)
        check(bar.bl_category == module.compat.ADDON_NAME,
              "标签条与功能面板在同一个侧栏标签页", bar.bl_category)
        check(not getattr(bar, "bl_parent_id", ""), "标签条是顶层面板")
        check(bar.bl_space_type == 'VIEW_3D' and bar.bl_region_type == 'UI',
              "标签条在 3D 视图侧栏")

    # 画法（静态检查）：整行按钮 + 加高，和 ZML_LAB 的样式一致
    path = os.path.join(os.path.dirname(os.path.abspath(module.__file__)), "pages.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    check("expand=True" in source, "标签条是整行等宽按钮（expand=True）")
    check("scale_y" in source, "标签条按钮加高了（scale_y）")
    check(module.pages.PROP_NAME in source, "标签条绑的是 WindowManager 上的当前页属性")

    setattr(manager, pages.PROP_NAME, pages.PAGE_MODELING)


def test_stairs(module):
    section("功能：一键楼梯（相对偏移阵列）")
    stairs = module.stairs
    context = bpy.context

    # ---- 纯逻辑：尺寸与默认系数 ----
    check(stairs.STEP_WIDTH == 0.3 and stairs.STEP_HEIGHT == 0.2,
          "台阶尺寸是 踏面 0.3 / 高 0.2", (stairs.STEP_WIDTH, stairs.STEP_HEIGHT))
    check(stairs.DEFAULT_FACTORS == (1.0, 0.0, -1.0),
          "默认系数 x=1, y=0, z=-1", stairs.DEFAULT_FACTORS)

    # ---- 方向决定 0.3 放在哪条水平轴，另一条保持选中前尺寸 ----
    check(stairs.axis_for_direction("RIGHT") == "X" and stairs.axis_for_direction("LEFT") == "X",
          "左右方向 -> 0.3 放在 X 轴")
    check(stairs.axis_for_direction("UP") == "Y" and stairs.axis_for_direction("DOWN") == "Y",
          "上下方向 -> 0.3 放在 Y 轴")
    check(stairs.step_dimensions("X", (1.0, 4.0, 0.5)) == (0.3, 4.0, 0.2),
          "左右：X=0.3、Y 不变、Z=0.2", stairs.step_dimensions("X", (1.0, 4.0, 0.5)))
    check(stairs.step_dimensions("Y", (1.0, 4.0, 0.5)) == (1.0, 0.3, 0.2),
          "上下：Y=0.3、X 不变、Z=0.2", stairs.step_dimensions("Y", (1.0, 4.0, 0.5)))
    check(stairs.step_dimensions("Y", (2.5, 7.0, 3.0)) == (2.5, 0.3, 0.2),
          "上下：X 保持选中前的 2.5（不是 0.3）", stairs.step_dimensions("Y", (2.5, 7.0, 3.0)))
    check(stairs.step_dimensions("X", (2.5, 7.0, 3.0)) == (0.3, 7.0, 0.2),
          "左右：Y 保持选中前的 7.0", stairs.step_dimensions("X", (2.5, 7.0, 3.0)))
    for axis in ("X", "Y"):
        for original in ((1.0, 4.0, 0.5), (2.5, 7.0, 3.0), (0.1, 0.2, 0.3)):
            target = stairs.step_dimensions(axis, original)
            check(abs(target[2] - 0.2) < 1e-9,
                  "{} 轴时 Z 恒为 0.2".format(axis), target)

    # ---- 鼠标位移 -> 方向 -> 系数 ----
    cases = (
        ((60.0, 4.0), "RIGHT", (1.0, 0.0, -1.0)),
        ((-60.0, 4.0), "LEFT", (-1.0, 0.0, -1.0)),
        ((4.0, 60.0), "UP", (0.0, 1.0, -1.0)),
        ((4.0, -60.0), "DOWN", (0.0, -1.0, -1.0)),
        ((3.0, -3.0), None, (1.0, 0.0, -1.0)),
    )
    for (dx, dy), direction, factors in cases:
        got = stairs.direction_from_delta(dx, dy)
        check(got == direction, "鼠标位移 {} 判定为 {}".format((int(dx), int(dy)), direction), got)
        check(stairs.factors_for_direction(got) == factors,
              "位移 {} 得到系数 {}".format((int(dx), int(dy)), factors),
              stairs.factors_for_direction(got))
        check(stairs.factors_for_direction(got)[2] < 0,
              "位移 {} 的 Z 系数始终为负（楼梯往下）".format((int(dx), int(dy))))

    # ---- 节数范围 ----
    check(stairs.clamp_count(0) == stairs.MIN_COUNT, "节数下限被夹住", stairs.clamp_count(0))
    check(stairs.clamp_count(9999) == stairs.MAX_COUNT, "节数上限被夹住", stairs.clamp_count(9999))
    check(stairs.clamp_count(7) == 7, "正常节数原样返回", stairs.clamp_count(7))

    # ---- 真造一个体块跑一遍（手搓 mesh，避免依赖界面上下文）----
    mesh = bpy.data.meshes.new("hhz_stairs_test")
    mesh.from_pydata(
        [(-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, -0.5),
         (-0.5, -0.5, 0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5)],
        [],
        [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)],
    )
    mesh.update()
    obj = bpy.data.objects.new("hhz_stairs_test", mesh)
    context.scene.collection.objects.link(obj)
    try:
        obj.dimensions = (1.0, 4.0, 0.5)
        context.view_layer.update()
        local = stairs.local_size(obj)
        original = stairs.mesh_dimensions(obj)
        check(all(abs(a - b) < 0.001 for a, b in zip(local, (1.0, 1.0, 1.0))),
              "local_size 读到网格自身包围盒（不含缩放）", local)
        check(all(abs(a - b) < 0.001 for a, b in zip(original, (1.0, 4.0, 0.5))),
              "mesh_dimensions 读到体块当前尺寸（不含修改器）", original)

        check(stairs.resize_step(obj, "X", local, original), "体块整型成功")
        context.view_layer.update()
        size = tuple(round(value, 4) for value in stairs.mesh_dimensions(obj))
        check(abs(size[0] - 0.3) < 0.001, "左右方向：X 被设成 0.3 m", size)
        check(abs(size[2] - 0.2) < 0.001, "左右方向：Z 被设成 0.2 m", size)
        check(abs(size[1] - 4.0) < 0.001, "左右方向：Y 保持选中前的尺寸", size)

        modifier = stairs.add_array(obj, stairs.DEFAULT_FACTORS, 5)
        check(modifier is not None and modifier.type == 'ARRAY', "加上了阵列修改器")
        check(modifier.use_relative_offset is True, "用的是相对偏移（系数 × 该轴尺寸）",
              modifier.use_relative_offset)
        check(tuple(round(value, 3) for value in modifier.relative_offset_displace)
              == (1.0, 0.0, -1.0), "默认系数写进了修改器",
              tuple(modifier.relative_offset_displace))
        check(modifier.count == 5, "节数写进了修改器", modifier.count)
        found_modifier = stairs.find_modifier(obj)
        check(found_modifier is not None and found_modifier == modifier,
              "能按名字找回这个修改器（同一个，不是新建的）", found_modifier)

        # 换方向 + 换节数：复用同一个修改器，不会越堆越多
        before = len(obj.modifiers)
        stairs.add_array(obj, stairs.factors_for_direction("LEFT"), 3)
        check(len(obj.modifiers) == before, "重复调用不会多出修改器", len(obj.modifiers))
        check(tuple(round(value, 3) for value in modifier.relative_offset_displace)
              == (-1.0, 0.0, -1.0), "方向改成 -X 后系数跟着变",
              tuple(modifier.relative_offset_displace))
        check(modifier.count == 3, "节数跟到 3", modifier.count)

        # 相对偏移 × 尺寸 = 每级实际位移：踏面 0.3 -> 每级挪 0.3，高度 0.2 -> 每级降 0.2
        step_x = modifier.relative_offset_displace[0] * size[0]
        step_z = modifier.relative_offset_displace[2] * size[2]
        check(abs(abs(step_x) - 0.3) < 0.001, "左右方向每级位移正是 0.3 m", step_x)
        check(abs(abs(step_z) - 0.2) < 0.001, "每级下降正是 0.2 m", step_z)

        # 关键回归：Blender 的 dimensions **设置器**会把修改器算进去（阵列会放大好几倍，
        # 3 节阵列设 0.3 会变成 0.9），所以尺寸一律用缩放换算；读取时两者一致。
        check(all(abs(a - b) < 0.001 for a, b in zip(tuple(obj.dimensions), size)),
              "obj.dimensions 的读取值与我们的尺寸一致（有坑的是设置器）",
              tuple(obj.dimensions))
        stairs_source = source_of(module, "stairs.py")
        check("obj.dimensions =" not in stairs_source,
              "stairs.py 里没有用 dimensions 设置器设尺寸（那是会被阵列污染的写法）")
        check("obj.scale = " in stairs_source, "缩放换算用的才是 obj.scale")

        # 换到上下方向：0.3 挪到 Y 轴，X 回到选中前的尺寸
        check(stairs.resize_step(obj, "Y", local, original), "换方向时能按新轴向重新整型")
        context.view_layer.update()
        switched = tuple(round(value, 4) for value in stairs.mesh_dimensions(obj))
        check(abs(switched[1] - 0.3) < 0.001, "上下方向：Y 变成 0.3 m", switched)
        check(abs(switched[0] - original[0]) < 0.001,
              "上下方向：X 回到选中前的尺寸", (switched[0], original[0]))
        check(abs(switched[2] - 0.2) < 0.001, "上下方向：Z 仍是 0.2 m", switched)

        modifier_y = stairs.add_array(obj, stairs.factors_for_direction("UP"), 4)
        step_y = modifier_y.relative_offset_displace[1] * switched[1]
        check(abs(abs(step_y) - 0.3) < 0.001, "上下方向每级 Y 位移正是 0.3 m", step_y)
        check(tuple(round(value, 3) for value in modifier_y.relative_offset_displace)
              == (0.0, 1.0, -1.0), "上下方向系数是 (0, 1, -1)",
              tuple(modifier_y.relative_offset_displace))

        # 再换回左右：Y 也要回到选中前的尺寸
        check(stairs.resize_step(obj, "X", local, original), "再换回左右方向")
        context.view_layer.update()
        back = tuple(round(value, 4) for value in stairs.mesh_dimensions(obj))
        check(abs(back[1] - original[1]) < 0.001, "换回左右：Y 回到选中前的尺寸",
              (back[1], original[1]))
        check(abs(back[0] - 0.3) < 0.001, "换回左右：X 又是 0.3 m", back)

        check(stairs.remove_array(obj), "能删掉阵列修改器")
        check(stairs.find_modifier(obj) is None, "删掉后找不到了")
    finally:
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0:
            bpy.data.meshes.remove(data)

    # ---- 算符 ----
    check(operator_class(module, "stairs") is not None, "算符 hhz.stairs 已注册")
    operator = module.ops.HHZ_OT_stairs
    check(operator.bl_label == "一键楼梯", "算符名字是「一键楼梯」", operator.bl_label)
    check("UNDO" in set(operator.bl_options), "算符可撤销（UNDO）", list(operator.bl_options))

    ops_source = source_of(module, "ops.py")
    # 注意：Python 定义的算符属性不在 <算子类>.bl_rna 里，要经 bpy.ops.<算子>.get_rna_type() 查
    count_prop = bpy.ops.hhz.stairs.get_rna_type().properties.get("count")
    check(count_prop is not None, "算符的 count 属性注册进了 RNA")
    if count_prop is not None:
        check(count_prop.type == 'INT', "count 是整数属性", count_prop.type)
        check(count_prop.default == module.stairs.DEFAULT_COUNT,
              "节数默认值 = {}".format(module.stairs.DEFAULT_COUNT), count_prop.default)
    check(module.stairs.DEFAULT_COUNT == 2, "默认节数是 2", module.stairs.DEFAULT_COUNT)
    check("min=stairs.MIN_COUNT" in ops_source and "max=stairs.MAX_COUNT" in ops_source,
          "算符的节数范围接的是 stairs 的上下限")
    for token in ("MOUSEMOVE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "LEFTMOUSE", "RUNNING_MODAL",
                  "axis_for_direction"):
        check(token in ops_source, "模态处理里用到了 {}".format(token))


def source_of(module, filename):
    """读插件包里某个源文件的内容（静态检查用）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(module.__file__)), filename)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_layout(module):
    section("功能：页面布局（保存 / 在新文件里使用）")
    layout = module.layout
    context = bpy.context

    check(operator_class(module, "layout_save") is not None, "算符 hhz.layout_save 已注册")
    for idname in ("layout_apply", "layout_delete", "layout_save_startup"):
        check(operator_class(module, idname) is not None,
              "算符 hhz.{} 已注册".format(idname))

    # 后台模式没有窗口：capture 要老老实实返回 None，apply 要明确拒绝而不是崩
    snapshot = layout.capture(context)
    check(snapshot is None or isinstance(snapshot, dict),
          "capture 返回 dict 或 None（后台模式没有窗口）", snapshot)
    if snapshot is None:
        message, ok = layout.apply(context, {"active": "Layout", "workspaces": []})
        check(not ok and isinstance(message, str), "后台模式下 apply 明确拒绝并给出说明", message)
    else:
        check(bool(snapshot.get("workspaces")), "抓到了工作区列表",
              [item.get("name") for item in snapshot.get("workspaces") or []])
        first = (snapshot.get("workspaces") or [{}])[0]
        areas = first.get("areas") or []
        check(isinstance(areas, list), "每个工作区带区域列表", len(areas))
        check(all(0.0 <= float(area.get("w", 0)) <= 1.0 for area in areas),
              "区域宽度存的是相对值（换分辨率也能用）", [area.get("w") for area in areas][:4])

    # JSON 往返 + 保存 / 列表 / 删除
    fake = {
        "active": "Layout",
        "workspaces": [
            {"name": "Layout", "areas": [
                {"type": "VIEW_3D", "x": 0.0, "y": 0.0, "w": 0.75, "h": 1.0},
                {"type": "OUTLINER", "x": 0.75, "y": 0.0, "w": 0.25, "h": 1.0},
            ]},
            {"name": "Modeling", "areas": [
                {"type": "VIEW_3D", "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            ]},
        ],
    }
    text, ok = layout.save("", "测试布局", fake)
    check(ok, "能保存一份布局")
    check(layout.names(text) == ["测试布局"], "列表里有这个名字", layout.names(text))
    loaded = layout.load(text)
    check(loaded.get("测试布局") == fake, "JSON 往返后内容一致")
    check(layout.save(text, "", fake)[1] is False, "名字为空时拒绝保存")
    text2, removed = layout.delete(text, "测试布局")
    check(removed and layout.names(text2) == [], "能删除这份布局")
    check(layout.load("这不是 JSON") == {}, "坏数据不会让插件崩")

    # 启动布局算符要带确认，因为会覆盖 startup.blend
    startup = module.ops.HHZ_OT_layout_save_startup
    check("invoke" in startup.__dict__, "「存为启动布局」走确认弹窗（不点一下就覆盖）")
    confirm = bpy.ops.hhz.layout_save_startup.get_rna_type().properties.get("confirm")
    check(confirm is not None and confirm.default is False,
          "confirm 默认 False（不会静默覆盖启动文件）", getattr(confirm, "default", None))


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
    run("一键楼梯（相对偏移阵列）", test_stairs, module)
    run("主题颜色（N 面板）", test_theme, module)
    run("饼菜单内容未被改动", test_pie_unchanged, module)
    run("分页（建模 / 工具 / 主题）", test_pages, module)
    run("页面布局（保存 / 使用）", test_layout, module)
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
