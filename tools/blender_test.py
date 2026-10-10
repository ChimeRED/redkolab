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
import colorsys
import glob
import os
import re
import struct
import sys
import tempfile
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
    # 每断言都刷一次 stdout：Blender 段错误（实测 4.5.14 上是 libraries.write）
    # 会把 Python 的缓冲输出整块丢掉，日志就断在崩溃前几千字节处、看不出崩在哪一条。
    try:
        sys.stdout.flush()
    except Exception:
        pass


def section(title):
    print("\n=== {} ===".format(title))
    try:
        sys.stdout.flush()
    except Exception:
        pass


def skip(reason):
    print("  [skip] {}".format(reason))
    try:
        sys.stdout.flush()
    except Exception:
        pass


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


def _make_missing_image(prefix, temporary_directory):
    """造一个「指向已删除文件的图像」样本，返回 (数据块名字, 临时 png 路径)。

    真实的缺失文件没法凭空造（不然就不缺失了），所以走真流程：生成 png 落盘 ->
    作为外部文件读进来 -> 再删掉磁盘上的文件。此时数据块仍是指向外部文件、
    自己没有打包数据、而源文件已经不在了 —— 正是保存时触发
    「无法打包文件，找不到源路径」的那种条目。
    """
    folder = tempfile.mkdtemp(prefix=prefix, dir=temporary_directory)
    path = os.path.join(folder, "sample.png")
    generated = bpy.data.images.new("hhz_sample_source", 4, 4)
    try:
        generated.filepath_raw = path
        generated.file_format = 'PNG'
        generated.save()
    finally:
        bpy.data.images.remove(generated)

    image = bpy.data.images.load(path)
    image.name = prefix
    os.remove(path)
    return image.name, path


def _clear_unused_missing_images():
    """丢掉之前用例可能留下的无使用者缺失图像（不碰有使用者的）。"""
    for image in list(bpy.data.images):
        if image.source != 'FILE' or image.packed_file or image.users != 0:
            continue
        try:
            if not os.path.exists(bpy.path.abspath(image.filepath)):
                bpy.data.images.remove(image)
        except Exception:
            pass


def _purge_test_samples(module):
    """删掉本用例造出来的样本数据块（``hhz_missing*`` / ``hhz_probe*``）。

    不看使用者数 —— 带使用者的样本（字体天生 ``users == 1``、声音用
    ``use_fake_user`` 抬到 1）不会被算符删掉，必须由测试自己清场，
    否则会污染后面的"没有缺失项"断言。集合列表直接复用算符自己的扫描函数。
    """
    for _label, collection in module.ops._packable_collections():
        for block in list(collection):
            name = getattr(block, "name", "") or ""
            if not (name.startswith("hhz_missing") or name.startswith("hhz_probe")):
                continue
            try:
                block.use_fake_user = False
            except Exception:
                pass
            try:
                collection.remove(block)
            except Exception:
                pass


def _write_sample_wav(path):
    """手写一个最小 PCM WAV（8kHz / 16bit / 单声道，44 字节头 + 静音采样）。"""
    rate = 8000
    frames = int(rate * 0.05)
    data = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    with open(path, "wb") as handle:
        handle.write(header + data)


def _make_missing_sample(kind, prefix, temporary_directory):
    """造一个"指向已删除文件"的**非图像**样本，返回 ``(数据块名字, 落点路径, 说明)``。

    做法与图像样本一致（:func:`_make_missing_image`）：先在临时目录里落一个**真文件**，
    按正常流程读进 Blender，再把磁盘上的文件删掉 —— 于是数据块仍指向外部文件、
    自己没有打包数据、而源文件已经不在了，正是保存时触发
    「无法打包文件，找不到源路径」的那种条目。

    * ``sound`` —— 手写的最小 WAV 能被 ``bpy.data.sounds.load()`` 读进来，
      读完 ``users == 0``，所以它既能验证"无使用者 → 删除"，也能用
      ``use_fake_user`` 验证"有使用者 → 绝不删除"。
    * ``movieclips`` —— 影片剪辑用同一个 WAV 也能读进来（``movieclips.load()`` 接受任何
      能被探测的文件），所以这一类**可以**端到端验证；它没有 ``packed_file`` 属性，
      正好验证"没有该属性的类型也照样扫到"。
    * ``fonts`` —— 复制一个真实存在的字体文件（优先 Blender 自带资源目录，其次系统字体）。
      找不到任何字体文件时返回 ``(None, None, 原因)``，由调用方 ``skip`` 并说明。
      读进来的字体数据块 ``users`` 天生是 1（``use_fake_user=False`` 也降不下来），
      所以它只能验证"有使用者 → 绝不删除"。
    """
    folder = tempfile.mkdtemp(prefix="hhz_missing_{}_".format(kind), dir=temporary_directory)
    if kind == "sound":
        path = os.path.join(folder, "sample.wav")
        _write_sample_wav(path)
        block = bpy.data.sounds.load(path, check_existing=False)
    elif kind == "movieclip":
        path = os.path.join(folder, "sample.wav")
        _write_sample_wav(path)
        block = bpy.data.movieclips.load(path, check_existing=False)
    elif kind == "font":
        source = None
        for pattern in (os.path.join(bpy.utils.resource_path('LOCAL'), "**", "*.ttf"),
                        os.path.join(bpy.utils.resource_path('LOCAL'), "**", "*.otf"),
                        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "*.ttf")):
            found = sorted(glob.glob(pattern, recursive=True))
            if found:
                source = found[0]
                break
        if source is None:
            return None, None, "本机找不到任何 .ttf/.otf 字体文件（Blender 资源目录与系统字体目录都为空）"
        path = os.path.join(folder, "sample.ttf")
        with open(source, "rb") as src, open(path, "wb") as dst:
            dst.write(src.read())
        block = bpy.data.fonts.load(path, check_existing=False)
    else:
        return None, None, "不支持的样本类型 {}".format(kind)

    block.name = prefix
    os.remove(path)
    return block.name, path, "由 {} 构造".format(source if kind == "font" else "手写 WAV")


def _real_config_dir():
    """用户**真实**的 Blender 配置目录（Windows 布局），用来确认测试跑在沙箱里。"""
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    version = "{}.{}".format(bpy.app.version[0], bpy.app.version[1])
    return os.path.normcase(os.path.abspath(
        os.path.join(appdata, "Blender Foundation", "Blender", version, "config")))


def _isolated_config_dir():
    """沙箱配置目录；不是沙箱（或目录不存在）时返回 ``None``。

    这道防线来自一次真实事故：沙箱目录**不存在**时，Blender 会静默忽略
    ``BLENDER_USER_CONFIG``，把偏好设置 / 启动文件写进用户真实配置目录。
    所以"要写盘的用例"必须先证明自己落在沙箱里。
    """
    config = bpy.utils.user_resource('CONFIG')
    if not config:
        return None
    config = os.path.abspath(config)
    if not os.path.isdir(config):
        return None
    if os.path.normcase(config) == _real_config_dir():
        return None
    return config


def _read_startup_file(config):
    """读回沙箱启动文件里的 ``(工作区名清单, 物体名清单)``（不追加任何东西）。

    用纯数据 API ``bpy.data.libraries.load`` 只读清单、不赋值给 ``target``，
    所以不会真的把启动文件里的数据块拉进当前会话。
    """
    path = os.path.join(config, "startup.blend")
    if not os.path.exists(path):
        return [], []
    with bpy.data.libraries.load(path) as (source, _target):
        return list(source.workspaces), list(source.objects)


def class_source(source, class_name):
    """按函数/类边界取出某个类的源码段（静态检查报告文案用）。

    和 ``class_operator_idnames`` 同一套做法：解析 AST、按名字找节点，
    不靠注释当锚点。
    """
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            end = getattr(node, "end_lineno", node.lineno)
            return "\n".join(lines[node.lineno - 1:end])
    return ""


def function_source(source, name):
    """按函数边界取出某个模块级函数的源码段（静态检查用）。

    和 ``class_source`` 同一套做法：解析 AST、按名字找节点，不靠注释当锚点。
    """
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            end = getattr(node, "end_lineno", node.lineno)
            return "\n".join(lines[node.lineno - 1:end])
    return ""


def _classify_missing(module, name):
    """``_missing_file_blocks()`` 把某个数据块归到了哪一类：unused / in_use / absent。"""
    try:
        unused, in_use = module.ops._missing_file_blocks()
    except Exception:
        return "error"
    if any(getattr(block, "name", None) == name for _label, _collection, block in unused):
        return "unused"
    if any(item[1] == name for item in in_use):
        return "in_use"
    return "absent"


def _api_calls(source, dotted):
    """源码里**真的调用过**的 ``dotted(...)``，返回行号列表（注释 / 字符串不算）。

    用 AST 判"有没有调用"，所以"把错名写在注释里"不会影响判断，
    也不会因为注释里提到正确写法就误判成"用过"。
    """
    parts = dotted.split(".")
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        names = []
        func = node.func
        while isinstance(func, ast.Attribute):
            names.append(func.attr)
            func = func.value
        if isinstance(func, ast.Name):
            names.append(func.id)
        if list(reversed(names)) == parts:
            lines.append(node.lineno)
    return lines


def _call_keywords(source, dotted):
    """源码里 ``dotted(...)`` 这次调用带了哪些关键字参数（AST 读，注释不算）。

    返回 ``None`` 表示源码里根本没有这次调用；返回 ``[]`` 表示调用了但不带参数。
    用途：确认 ``bpy.ops.wm.read_homefile()`` 是**裸调用**（注释里写
    ``use_factory_startup=True`` 不能算数）。
    """
    parts = dotted.split(".")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        names = []
        func = node.func
        while isinstance(func, ast.Attribute):
            names.append(func.attr)
            func = func.value
        if isinstance(func, ast.Name):
            names.append(func.id)
        if list(reversed(names)) == parts:
            return sorted(keyword.arg for keyword in node.keywords)
    return None


def test_fix_missing_files(module):
    section("功能：检查并修复缺失文件")
    operator = operator_class(module, "fix_missing_files")

    # ---- 1. 算符存在（用项目既有的查类方式，不用 hasattr(bpy.ops...)）----
    check(operator is not None, "算符 hhz.fix_missing_files 已注册")
    check(getattr(operator, "bl_idname", "") == "hhz.fix_missing_files",
          "算符 bl_idname 正确", getattr(operator, "bl_idname", None))

    # 面板里真的画了这个按钮（静态读源码，和 test_pie_unchanged 同一套做法）
    ui_path = os.path.join(os.path.dirname(os.path.abspath(module.__file__)), "ui.py")
    with open(ui_path, encoding="utf-8") as handle:
        ui_source = handle.read()
    in_settings = class_operator_idnames(ui_source, "HHZ_PT_settings")
    check("hhz.fix_missing_files" in in_settings,
          "按钮出现在已有的「插件设置」面板里", sorted(in_settings))

    if operator is None:
        return

    # 报告文案：运行期抓不到 self.report（后台模式没有 WM 报告收集器，
    # 实测 wm.report 不可赋值），改成按类边界静态读算符源码里的报告文案
    source = source_of(module, "ops.py")
    static = class_source(source, "HHZ_OT_fix_missing_files")
    check(bool(static), "静态读到了算符源码段（报告文案检查有依据）")

    has_autopack = hasattr(bpy.data, "use_autopack")
    check(has_autopack, "当前 Blender 上有 bpy.data.use_autopack（本功能的前置 API）")
    original_autopack = bool(getattr(bpy.data, "use_autopack", False))
    if not has_autopack:
        return

    try:
        # ---- 2. 没有缺失项：返回 FINISHED、不动 use_autopack、报告说「没有缺失」----
        _clear_unused_missing_images()
        bpy.data.use_autopack = True
        before_names = sorted(image.name for image in bpy.data.images)
        result = bpy.ops.hhz.fix_missing_files()
        check(result == {'FINISHED'}, "没有缺失文件时算符返回 FINISHED", result)
        check(bpy.data.use_autopack is True,
              "没有缺失文件时不关闭自动打包资源（幂等，不产生副作用）",
              bpy.data.use_autopack)
        check(sorted(image.name for image in bpy.data.images) == before_names,
              "没有缺失文件时图像数据块一个都没少")
        check("没有发现缺失文件" in static,
              "源码里「没有缺失」这一路明确报告「没有发现缺失文件」，不是一句「完成」")

        # ---- 3. 有缺失且没有使用者：数据块被移除，并且自动打包被关掉 ----
        _clear_unused_missing_images()
        unused_name, _unused_path = _make_missing_image("hhz_missing_unused", None)
        sample = bpy.data.images.get(unused_name)
        check(sample is not None, "样本图像已创建", unused_name)
        if sample is not None:
            check(sample.source == 'FILE' and not sample.packed_file and sample.users == 0,
                  "样本符合判据（外部文件 / 未打包 / 没有使用者）",
                  (sample.source, sample.packed_file, sample.users))
            check(not os.path.exists(bpy.path.abspath(sample.filepath)),
                  "样本的源文件确实不在磁盘上")
            check(_classify_missing(module, unused_name) == "unused",
                  "_missing_image_names() 把它归到「没有使用者」一类")

        bpy.data.use_autopack = True
        result = bpy.ops.hhz.fix_missing_files()
        check(result == {'FINISHED'}, "有缺失项时算符返回 FINISHED", result)
        check(bpy.data.images.get(unused_name) is None,
              "没有使用者的缺失图像数据块已被移除")
        check(bpy.data.use_autopack is False,
              "存在缺失项时自动打包资源被关闭（弹窗的直接原因）",
              bpy.data.use_autopack)
        check("use_autopack" in static,
              "源码里确实用了 Blender 原生的 bpy.data.use_autopack，没有自己实现打包")

        # 幂等：再点一次什么都不该变
        again = bpy.ops.hhz.fix_missing_files()
        check(again == {'FINISHED'}, "重复点击仍然正常结束", again)
        check(bpy.data.use_autopack is False, "重复点击不会把状态改回来",
              bpy.data.use_autopack)

        # ---- 4. 有缺失但仍有使用者：绝不删除 ----
        _clear_unused_missing_images()
        used_name, _used_path = _make_missing_image("hhz_missing_used", None)
        used = bpy.data.images.get(used_name)
        check(used is not None, "带使用者的样本图像已创建", used_name)
        if used is not None:
            used.use_fake_user = True  # users 从 0 变成 1，等价于"文件里还在用它"
            check(used.users == 1, "样本现在有 1 个使用者", used.users)
            check(not os.path.exists(bpy.path.abspath(used.filepath)),
                  "带使用者的样本源文件同样不在磁盘上")
            check(_classify_missing(module, used_name) == "in_use",
                  "_missing_image_names() 把它归到「仍在使用」一类")

        bpy.data.use_autopack = True
        result = bpy.ops.hhz.fix_missing_files()
        check(result == {'FINISHED'}, "只剩带使用者的缺失项时算符返回 FINISHED", result)
        check(bpy.data.images.get(used_name) is not None,
              "仍在使用的缺失图像绝对不删（删了会改动用户场景）")
        check(bpy.data.use_autopack is False,
              "存在缺失项时自动打包资源被关闭", bpy.data.use_autopack)
        check("查找缺失文件" in static and "未删除" in static,
              "源码里对仍在使用的那类给了「查找缺失文件」的提示，且说明未删除")

        # 报告与 print 都要带"类型 + 名字 + 使用者数"；状态栏只列前 3 项 + 等 N 项
        check("_describe_items" in static and "使用者" in static,
              "报告与 print 都带类型 + 名字 + 使用者数")
        check("等 {} 项" in source and "STATUS_ITEM_LIMIT" in source,
              "状态栏列表超过 3 项时以「等 N 项」收尾（常量与拼接都在 ops.py 模块级）")
        check("print(" in static and "已清理：" in static and "仍在用：" in static,
              "控制台 print 完整列出类型 + 名字 + 使用者数")

        # ---- 5. 扫描范围：覆盖所有可打包类型（不只图像）----
        scanned = [label for label, _collection in module.ops._packable_collections()]
        for label in ("图像", "声音", "字体", "影片剪辑"):
            check(label in scanned, "缺失扫描覆盖「{}」类型".format(label), scanned)
        check("libraries" not in scanned and "all_ids" not in scanned,
              "不扫链接库与 all_ids（删它们会断开链接 / 没有意义）", scanned)

        # 非图像类型 1：声音（端到端：手写 WAV -> 读入 -> 删源文件 -> 算符清理）
        _purge_test_samples(module)
        sound_name, sound_path, sound_note = _make_missing_sample(
            "sound", "hhz_missing_sound", None)
        check(sound_name is not None, "缺失声音样本已构造（{}）".format(sound_note),
              sound_name)
        if sound_name is not None:
            check(not os.path.exists(sound_path), "声音样本的源文件已删除")
            check(bpy.data.sounds.get(sound_name).users == 0,
                  "声音样本没有使用者", bpy.data.sounds.get(sound_name).users)
            check(_classify_missing(module, sound_name) == "unused",
                  "扫描把缺失声音归到「没有使用者」一类（不是只扫图像）")

            bpy.data.use_autopack = True
            result = bpy.ops.hhz.fix_missing_files()
            check(result == {'FINISHED'}, "只剩缺失声音时算符返回 FINISHED", result)
            check(bpy.data.sounds.get(sound_name) is None,
                  "没有使用者的缺失声音数据块被移除（新增类型也走同一条删除路径）")
            check(bpy.data.use_autopack is False,
                  "存在缺失声音时自动打包资源被关闭", bpy.data.use_autopack)

        # 非图像类型 2：影片剪辑（没有 packed_file 属性，照样要扫到）
        _purge_test_samples(module)
        clip_name, clip_path, clip_note = _make_missing_sample(
            "movieclip", "hhz_missing_clip", None)
        check(clip_name is not None, "缺失影片剪辑样本已构造（{}）".format(clip_note),
              clip_name)
        if clip_name is not None:
            clip = bpy.data.movieclips.get(clip_name)
            check(clip is not None and not hasattr(clip, "packed_file"),
                  "影片剪辑没有 packed_file 属性（判据不能依赖它）",
                  hasattr(clip, "packed_file") if clip else None)
            check(_classify_missing(module, clip_name) == "unused",
                  "扫描把没有使用者的缺失影片剪辑归到「没有使用者」一类")
            bpy.data.use_autopack = True
            result = bpy.ops.hhz.fix_missing_files()
            check(result == {'FINISHED'}, "只剩缺失影片剪辑时算符返回 FINISHED", result)
            check(bpy.data.movieclips.get(clip_name) is None,
                  "没有使用者的缺失影片剪辑被移除")
            check(bpy.data.use_autopack is False,
                  "存在缺失影片剪辑时自动打包资源被关闭", bpy.data.use_autopack)

        # 非图像类型 3：字体（users 天生为 1 -> 只能验证"有使用者绝不删除"）
        #   构造不出来的情况：本机找不到任何 .ttf/.otf，这时如实 skip 并说明原因，
        #   不假装验证过。
        _purge_test_samples(module)
        font_name, font_path, font_note = _make_missing_sample(
            "font", "hhz_missing_font", None)
        if font_name is None:
            skip("构造不了字体样本：{}".format(font_note))
        else:
            check(not os.path.exists(font_path), "字体样本的源文件已删除")
            font = bpy.data.fonts.get(font_name)
            check(font is not None and font.users >= 1,
                  "字体样本自带使用者（所以它验证的是「有使用者绝不删除」）",
                  font.users if font else None)
            check(_classify_missing(module, font_name) == "in_use",
                  "扫描把带使用者的缺失字体归到「仍在使用」一类")
            bpy.data.use_autopack = True
            result = bpy.ops.hhz.fix_missing_files()
            check(result == {'FINISHED'}, "只剩带使用者的缺失字体时算符返回 FINISHED", result)
            check(bpy.data.fonts.get(font_name) is not None,
                  "仍在使用的缺失字体绝不删除")
            check(bpy.data.use_autopack is False,
                  "存在缺失字体时自动打包资源被关闭", bpy.data.use_autopack)

        # 声音也有使用者时：绝不删除（对新增类型同样成立）
        _purge_test_samples(module)
        kept_name, _kept_path, _note = _make_missing_sample(
            "sound", "hhz_missing_sound_used", None)
        if kept_name is not None:
            kept = bpy.data.sounds.get(kept_name)
            kept.use_fake_user = True   # users 0 -> 1，等价于"文件里还在用它"
            check(kept.users >= 1, "声音样本现在有使用者", kept.users)
            check(_classify_missing(module, kept_name) == "in_use",
                  "扫描把带使用者的缺失声音归到「仍在使用」一类")
            bpy.data.use_autopack = True
            result = bpy.ops.hhz.fix_missing_files()
            check(result == {'FINISHED'}, "只剩带使用者的缺失声音时算符返回 FINISHED", result)
            check(bpy.data.sounds.get(kept_name) is not None,
                  "仍在使用的缺失声音绝不删除（有使用者不删对新类型同样成立）")

        # use_autopack 只在"确实存在缺失项"时才被关：清干净之后再点一次不该动它
        _purge_test_samples(module)
        bpy.data.use_autopack = True
        result = bpy.ops.hhz.fix_missing_files()
        check(result == {'FINISHED'}, "清理干净之后算符仍然正常结束", result)
        check(bpy.data.use_autopack is True,
              "没有缺失项时绝不碰 use_autopack（只在有缺失时才关）", bpy.data.use_autopack)
    finally:
        # 复位，避免污染后面的用例
        _clear_unused_missing_images()
        _purge_test_samples(module)
        try:
            bpy.data.use_autopack = original_autopack
        except Exception:
            pass


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

    # 预设：Marathon 酸性配色 + 死亡搁浅，改的是按钮 / 状态色
    names = theme.preset_names()
    check(len(names) == 3, "预设一共 3 套（删掉旧的两套马拉松、新增「死亡搁浅」）", names)
    check(names == ["马拉松 建模 低干扰", "马拉松 浅灰卡", "死亡搁浅"],
          "预设清单就是这三套（N 面板按钮按 preset_names() 自动生成，ui.py 不用改）", names)
    check(any("马拉松" in name for name in names), "预设里仍有马拉松配色", names)
    check("死亡搁浅" in names, "预设里有新增的「死亡搁浅」", names)
    theme_source = source_of(module, "theme.py")
    check("洋红警戒" not in theme_source and "暗色 HUD" not in theme_source,
          "主题源码里彻底删掉了旧的两套马拉松预设（连名字都不留）")
    check("FACTORY_LOCKED_FAMILIES" not in theme_source,
          "整族写回出厂默认值的旧机制已移除（改用 DEEP_ACCENT_FAMILIES）")
    check("DEEP_ACCENT_FAMILIES" in theme_source, "主题源码里有「深档强调」家族清单")
    preset_name = names[0]
    colors = theme.preset_colors(preset_name)
    check(blue_path in colors and grey_path in colors,
          "预设同时覆盖灰色按钮与选中按钮", sorted(colors)[:3])
    changed = theme.apply_colors(current, theme.preset_colors(preset_name) or {})
    check(changed >= 20, "预设改到 20 个以上颜色项", changed)
    check(color_close(theme.get_color(current, blue_path), colors[blue_path]),
          "预设把「选中按钮」改成了酸性绿", theme.get_color(current, blue_path))
    check(color_close(theme.get_color(current, grey_path), colors[grey_path]),
          "预设把「灰色按钮」改成了预设里的底色", theme.get_color(current, grey_path))
    check(theme.get_color(current, blue_path)[1] > theme.get_color(current, blue_path)[2],
          "选中的主行动色是黄绿系（G > B）", theme.get_color(current, blue_path))

    # hex -> 浮点 的换算（主题里存的就是显示值，不需要线性转换）
    check(color_close(theme.hex_color("#C6FF00"), (0.7765, 1.0, 0.0, 1.0)),
          "#C6FF00 换算正确", theme.hex_color("#C6FF00"))

    # 恢复默认：按钮那一组也要能整组还原
    check(theme.restore_default(current, blue_path), "恢复单项默认值成功")
    # 旧的便捷包装（按前缀挑默认值的两个批量恢复函数）已删除，这里改用通用 API，
    # 语义与原包装逐项等价（都是按路径前缀筛默认值表）。
    widget_default_paths = [item for item in theme.default_paths()
                            if item.startswith("user_interface.")]
    viewport_default_paths = [item for item in theme.default_paths()
                              if item.startswith("view_3d.")]
    check(theme.restore_defaults(current, widget_default_paths) >= 20, "批量恢复控件默认值成功")
    restored_blue = theme.get_color(current, blue_path)
    check(restored_blue[2] > restored_blue[0],
          "恢复默认后选中按钮回到蓝色（默认确实是蓝的）", restored_blue)
    check(theme.has_default(rgb_path) and theme.restore_defaults(current, viewport_default_paths) >= 11,
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
                if (background_key == "inner_sel"
                        and family_prefix in theme.LABEL_INDEPENDENT_FAMILIES):
                    # 标签独立型家族的标签**不在**选中块上，inner_sel vs text_sel 这一对
                    # 不代表标签可读性；它们由下面更严格的"实际底色"检查覆盖
                    # （清单与判据见 theme.LABEL_INDEPENDENT_FAMILIES）。
                    continue
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

        # 「标签独立型」家族的标签不在选中块上（只有小方块 / 小圆点 / 开关体用
        # inner_sel 上色；菜单 / 弹出面板里的选项按钮连标签色都借 wcol_menu_back），
        # 所以它们的 text / text_sel 必须与**每一个可能落到的底色**都有足够亮度差，
        # 否则就是"叠加层里打勾后标签变黑、看不见"。
        label_problems = []
        for label_prefix in theme.LABEL_INDEPENDENT_FAMILIES:
            label_problems.extend(theme.label_contrast_problems(colors, label_prefix))
        check(not label_problems,
              "预设「{}」标签独立型家族（含 wcol_menu_back）的标签在各实际底色上都读得清".format(preset),
              label_problems[:6])

    # 视口辅助元素（基面网格线 / 法线）：预设必须把它们**写回出厂默认值**（自愈），
    # 而不是"不覆盖"—— 旧版写进主题里的暗值不会自己变回来，
    # 用户看到的就是"套了预设完全没更新"。
    # （滑块 / 工具按钮这三个家族**不再**走这条路：它们改成"深档强调色 + 浅色文字"，
    #  见 test_preset_deep_accent_families。）
    not_default = []
    for preset in theme.preset_names():
        preset_colors = theme.preset_colors(preset) or {}
        for helper_path in theme.viewport_helper_paths():
            default = theme.default_of(helper_path)
            if default is None:
                continue
            if helper_path not in preset_colors:
                not_default.append("{} 漏了 {}".format(preset, helper_path))
            elif not color_close(preset_colors[helper_path], default):
                not_default.append("{} 的 {} 不是出厂默认值".format(preset, helper_path))
    check(not not_default, "三套预设都把视口辅助元素写回记录的出厂默认值",
          not_default[:6])
    check("view_3d.grid" in theme.default_paths()
          and color_close(theme.default_of("view_3d.grid"),
                          theme.DEFAULT_COLORS["view_3d.grid"]),
          "基面网格线仍记录着出厂默认值（恢复默认能还原它）")
    check(theme.VIEWPORT_HELPERS and
          all(helper in theme.default_paths() for helper in theme.VIEWPORT_HELPERS),
          "被列为「必须写回默认」的辅助元素都有出厂默认值记录（不猜颜色）",
          theme.VIEWPORT_HELPERS)

    # 真套一次，直接查实际主题里的对比度
    theme.apply_colors(current, theme.preset_colors(theme.preset_names()[0]) or {})
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

    preferences.simple_mode = False
    preferences.family = prefix
    preferences.part_inner = (0.5, 0.5, 0.5, 1.0)   # 中间灰最容易配错文字色
    result = bpy.ops.hhz.theme_auto_contrast()
    check(result == {'FINISHED'}, "「自动配文字色」算符可用", result)
    report = theme.family_contrast_report(current, prefix)
    check(not report, "自动配文字色之后该家族没有对比度问题",
          (report, theme.color_to_hex(theme.get_color(current, theme.family_path(prefix, "inner"))),
           theme.color_to_hex(theme.get_color(current, theme.family_path(prefix, "text")))))
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
    # 按运行版本过滤（这就是 CI 上 4.5.14 唯一那条 [FAIL] 的根因）：
    # 默认值表是真机 dump 出来的（4X 表来自 4.0 / 4.2），里面的 info.info_error /
    # info.info_warning 在 4.5 上已经被 Blender 删掉（状态色搬到了
    # user_interface.wcol_state.*）。表里留着它们是对的（4.2 上真的存在），
    # 但核对时只能查**本版本真的存在**的那些，否则会把"本版本没有这一项"误报成"没恢复"。
    present = theme.available_default_paths(current, recorded)
    absent_in_version = theme.absent_default_paths(current, recorded)
    check(len(present) + len(absent_in_version) == len(recorded),
          "「本版本可用」+「本版本不存在」= 记录总数（过滤口径完整，不丢项）",
          (len(present), len(absent_in_version), len(recorded)))
    check(all(theme.resolve(current, item) is None for item in absent_in_version),
          "被过滤掉的那些在本版本上确实不存在（不是随便丢的）",
          absent_in_version[:4])
    theme.restore_defaults(current, recorded)
    not_restored = []
    for item in present:
        live = theme.get_color(current, item)
        expected = theme.default_of(item)
        if live is None or expected is None or not theme.color_close(live, expected):
            not_restored.append("{}: {} != {}".format(item, live, expected))
    check(not not_restored,
          "全部恢复之后每一项都回到记录的出厂值（记录 {} 项 / 本版本可用 {} 项，"
          "另有 {} 项本版本不存在已跳过）".format(
              len(recorded), len(present), len(absent_in_version)),
          not_restored[:6])
    # 版本过滤机制本身：本版本不存在的路径既不会被列进可用、也不会被算成恢复成功
    bogus_path = "user_interface.hhz_not_a_real_color_path"
    check(theme.available_default_paths(current, [bogus_path]) == []
          and theme.absent_default_paths(current, [bogus_path]) == [bogus_path],
          "版本过滤：本版本不存在的路径被排除，并出现在 absent_default_paths 里",
          (theme.available_default_paths(current, [bogus_path]),
           theme.absent_default_paths(current, [bogus_path])))
    check(theme.restore_defaults(current, [bogus_path]) == 0,
          "本版本不存在的路径不会被算成「恢复成功」（不误报）")
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

    # ---- 进阶折叠：默认收起，展开才画精调项；收起不等于删实现 ----
    check(preferences.theme_advanced is False, "进阶折叠默认是收起的（theme_advanced=False）",
          preferences.theme_advanced)
    preferences.theme_advanced = True
    check(preferences.theme_advanced is True, "进阶折叠能展开（切到 True）")
    preferences.theme_advanced = False
    check(preferences.theme_advanced is False, "进阶折叠能再收回去（切回 False）")
    for token in ("theme_advanced", "TRIA_DOWN", "TRIA_RIGHT", "emboss=False"):
        check(token in panel_source, "进阶折叠头用到了 {}".format(token))
    # 面板上收起只是"不画"：被收进去的算符与偏好设置属性一个都不能少
    advanced_only = ("theme_apply_color", "theme_apply_global", "theme_custom_save",
                     "theme_custom_apply", "theme_custom_delete", "font_pick",
                     "font_restore", "theme_read", "theme_part_load", "theme_auto_contrast")
    adv_missing = [name for name in advanced_only if operator_class(module, name) is None]
    check(not adv_missing, "收进进阶的算符仍然全部注册着（隐藏不等于删除）", adv_missing)
    retained_attrs = ("palette_color", "custom_hex", "custom_name", "custom_choice",
                      "global_base", "global_accent", "global_auto_text", "global_text",
                      "theme_target", "theme_color", "preset_saturation")
    attrs_missing = [name for name in retained_attrs if not hasattr(preferences, name)]
    check(not attrs_missing, "进阶里的偏好设置属性仍然保留（隐藏不等于删除）", attrs_missing)
    # 简洁模式的 update 回调必须返回 None（返回元组会让 Blender 打 ValueError）
    check("update=lambda" not in panel_source,
          "面板属性不再用返回值的 lambda 当 update 回调")
    check("_on_simple_mode_update" in panel_source, "简洁模式改用具名回调函数")

    # ---- 恢复：一个 scope 下拉驱动同一个算符，三个范围分别真的生效 ----
    check(preferences.restore_scope == 'CURRENT', "恢复范围默认是「本项」",
          preferences.restore_scope)
    scope_items = [item.identifier for item in
                   type(preferences).bl_rna.properties["restore_scope"].enum_items]
    check(scope_items == ['CURRENT', 'GROUP', 'ALL'],
          "恢复范围下拉就是 本项 / 本组 / 全部", scope_items)
    check(panel_source.count('"hhz.theme_restore_default"') == 1,
          "主题面板只画一个恢复按钮（下拉驱动 scope，不再摆三个）",
          panel_source.count('"hhz.theme_restore_default"'))
    check('"restore_scope"' in panel_source, "恢复按钮的 scope 由面板下拉驱动")

    orig_scope = preferences.restore_scope
    orig_target = preferences.theme_target
    # 先在两个不同分组里各造一处改动：视口组 与「按钮 底色」组
    theme.set_color(current, rgb_path, (0.05, 0.05, 0.05, 1.0))
    theme.set_color(current, grey_path, (0.05, 0.05, 0.05, 1.0))
    preferences.theme_target = rgb_path
    preferences.restore_scope = 'CURRENT'
    result = bpy.ops.hhz.theme_restore_default(scope=preferences.restore_scope)
    check(result == {'FINISHED'}, "选「本项」时恢复可用", result)
    check(theme.color_close(theme.get_color(current, rgb_path), theme.default_of(rgb_path)),
          "「本项」把当前这一项恢复成出厂值",
          (theme.get_color(current, rgb_path), theme.default_of(rgb_path)))
    check(not theme.color_close(theme.get_color(current, grey_path), theme.default_of(grey_path)),
          "「本项」没有动别的分组（三个范围语义确实不同）")
    preferences.restore_scope = 'GROUP'
    result = bpy.ops.hhz.theme_restore_default(scope=preferences.restore_scope)
    check(result == {'FINISHED'}, "选「本组」时恢复可用", result)
    rs_group = theme.group_of(rgb_path)
    rs_members = [item for item in theme.paths_in_group(rs_group, listed)
                  if theme.has_default(item)]
    check(rs_members and all(theme.color_close(theme.get_color(current, item),
                                               theme.default_of(item))
                             for item in rs_members),
          "「本组」把当前分组整组恢复（{} 项）".format(len(rs_members)))
    check(not theme.color_close(theme.get_color(current, grey_path), theme.default_of(grey_path)),
          "「本组」没有波及其它分组")
    preferences.restore_scope = 'ALL'
    result = bpy.ops.hhz.theme_restore_default(scope=preferences.restore_scope)
    check(result == {'FINISHED'}, "选「全部」时恢复可用", result)
    check(theme.color_close(theme.get_color(current, grey_path), theme.default_of(grey_path)),
          "「全部」把别组的改动也恢复了")
    preferences.theme_target = orig_target
    preferences.restore_scope = orig_scope

    # 还原测试前的颜色
    theme.set_color(current, rgb_path, original_rgb)
    theme.set_color(current, rgba_path, original_rgba)
    # ---- 饱和度：预设滑杆（theme.desaturate_* + preferences.preset_saturation）----
    # 功能早就实现了，却一直没有断言：滑到 0 会不会把界面弄脏、反复拖动会不会越拖
    # 越灰，之前没人管。纯函数与真实算符两条路都验一遍。
    probe = (0.7765, 1.0, 0.0, 1.0)   # 酸性黄绿：饱和度高、明度也高
    check(color_close(theme.desaturate_color(probe, 1.0), probe),
          "factor=1.0 时颜色原样不变", theme.desaturate_color(probe, 1.0))
    grey = theme.desaturate_color(probe, 0.0)
    check(theme.saturation_of(grey) < 0.01 and max(grey[:3]) - min(grey[:3]) < 0.005,
          "factor=0.0 时变成灰（三个通道相等）", (grey, theme.saturation_of(grey)))
    softened = theme.desaturate_color(probe, 0.4)
    check(theme.saturation_of(softened) < theme.saturation_of(probe) * 0.6
          and theme.saturation_of(softened) > 0.0,
          "0.4 倍之后饱和度明显下降，但没有直接变灰",
          (theme.saturation_of(probe), theme.saturation_of(softened)))
    check(theme.luminance(softened) >= 0.45
          and theme.readable_text_color(softened) == theme.TEXT_ON_LIGHT,
          "降饱和后底色仍够亮，配深色文字仍看得清",
          (theme.luminance(softened), theme.color_to_hex(softened)))
    check(color_close(theme.desaturate_color(probe, 0.4), softened),
          "同一个颜色降饱和是确定性的（两次算出同一个值）")

    batch = {"a": probe, "b": (0.1, 0.2, 0.9, 1.0), "c": (0.5, 0.5, 0.5, 1.0)}
    batch_low = theme.desaturate_colors(batch, 0.4)
    check(set(batch_low) == set(batch), "批量降饱和不丢项（键一个不少）",
          (sorted(batch), sorted(batch_low)))
    check(all(theme.saturation_of(batch_low[key]) <= theme.saturation_of(batch[key]) + 1e-9
              for key in batch),
          "批量里每一项的饱和度都没有被调高")
    check(max(batch_low["c"][:3]) - min(batch_low["c"][:3]) < 0.005,
          "批量里的灰色项仍是灰的", batch_low["c"])
    check(theme.desaturate_colors(None, 0.4) == {}, "批量降饱和对空输入返回空字典")

    # 每个预设：高饱和的颜色在 0.4 倍之后都要明显更灰；降饱和之后
    # 底色与它**实际配的那支文字色**仍要够对比（不能只盯着"浅底配深字"这一种组合：
    # 三套预设的选中态是"深底配浅字"，旧写法会把它们误判成问题）。
    not_greyer = []
    thin_after_desaturate = []
    for name in theme.preset_names():
        preset_colors = theme.preset_colors(name) or {}
        for path, color in preset_colors.items():
            full = theme.desaturate_color(color, 1.0)
            low = theme.desaturate_color(color, 0.4)
            if (theme.saturation_of(full) > 0.2
                    and theme.saturation_of(low) >= theme.saturation_of(full) * 0.6):
                not_greyer.append("{}:{}".format(name, path))
        for family_prefix, _label in theme.WIDGET_FAMILIES:
            for background_key, text_key in (("inner", "text"), ("inner_sel", "text_sel")):
                if (background_key == "inner_sel"
                        and family_prefix in theme.LABEL_INDEPENDENT_FAMILIES):
                    # 同 test_theme：标签独立型家族的标签不在选中块上，
                    # inner_sel vs text_sel 这一对不代表标签可读性
                    continue
                background = preset_colors.get(theme.family_path(family_prefix, background_key))
                text_color = preset_colors.get(theme.family_path(family_prefix, text_key))
                if background is None or text_color is None:
                    continue
                pale_background = theme.desaturate_color(background, 0.4)
                pale_text = theme.desaturate_color(text_color, 0.4)
                if abs(theme.luminance(pale_background)
                       - theme.luminance(pale_text)) < 0.3:
                    thin_after_desaturate.append("{}.{}".format(family_prefix, background_key))
    check(not not_greyer, "每个预设里高饱和的颜色在 0.4 倍后都明显更灰", not_greyer[:6])
    check(not thin_after_desaturate,
          "降饱和到 40% 之后，预设自己的文字/底色对比仍然够（浅底深字与深底浅字都不糊）",
          thin_after_desaturate[:6])

    # 真滑杆：40% 的选中色必须比 100% 更灰，而且反复套用不累积
    original_saturation = float(preferences.preset_saturation)
    preferences.preset_saturation = 1.0
    result = bpy.ops.hhz.theme_apply_preset(preset=preset_name)
    check(result == {'FINISHED'}, "饱和度 100% 时「应用预设」算符可用", result)
    full_saturation = theme.saturation_of(theme.get_color(current, blue_path))
    preferences.preset_saturation = 0.4
    result = bpy.ops.hhz.theme_apply_preset(preset=preset_name)
    check(result == {'FINISHED'}, "饱和度 40% 时「应用预设」算符可用", result)
    low_saturation = theme.saturation_of(theme.get_color(current, blue_path))
    check(low_saturation < full_saturation * 0.7,
          "40% 的选中色比 100% 更灰（滑杆真的落到主题上）",
          (full_saturation, low_saturation, theme.color_to_hex(theme.get_color(current, blue_path))))
    first_pass = theme.get_color(current, blue_path)
    result = bpy.ops.hhz.theme_apply_preset(preset=preset_name)
    second_pass = theme.get_color(current, blue_path)
    check(result == {'FINISHED'} and color_close(first_pass, second_pass),
          "同一饱和度重复套用结果一致（从预设表重算，不累积变灰）",
          (first_pass, second_pass))
    check(abs(theme.saturation_of(second_pass) - low_saturation) < 0.01,
          "第二次套用后的饱和度与第一次相同（没有变成 16%）",
          (low_saturation, theme.saturation_of(second_pass)))
    preferences.preset_saturation = original_saturation



def test_preset_label_families(module):
    section("预设：标签独立型家族（叠加层里打勾后标签仍读得清）")
    theme = module.theme
    current = theme.current_theme(bpy.context)
    check(current is not None, "能取到当前主题")
    if current is None:
        return

    # 清单本身：漏一个就是这样一处用户可见的事故
    check("wcol_menu_back" in theme.LABEL_INDEPENDENT_FAMILIES,
          "wcol_menu_back 在「标签独立型家族」清单里（漏了它就是叠加层里打勾后标签变黑）",
          theme.LABEL_INDEPENDENT_FAMILIES)
    for option_prefix in theme.OPTION_BLOCK_FAMILIES:
        check(option_prefix in theme.LABEL_INDEPENDENT_FAMILIES,
              "{} 也在标签独立型清单里".format(option_prefix),
              theme.LABEL_INDEPENDENT_FAMILIES)
    check("wcol_menu_item" not in theme.LABEL_INDEPENDENT_FAMILIES
          and "wcol_pulldown" not in theme.LABEL_INDEPENDENT_FAMILIES,
          "菜单高亮行 / 下拉菜单**不**按标签独立处理（它们的标签确实压在选中块上）",
          theme.LABEL_INDEPENDENT_FAMILIES)
    check(theme.LABEL_TEXT_COLOR == theme.TOKENS["TEXT_PRIMARY"],
          "标签色引用的是已有设计 Token（TEXT_PRIMARY），不是新造的颜色",
          theme.LABEL_TEXT_COLOR)

    for preset in theme.preset_names():
        colors = theme.preset_colors(preset) or {}
        problems = []
        for prefix in theme.LABEL_INDEPENDENT_FAMILIES:
            problems.extend(theme.label_contrast_problems(colors, prefix))
        check(not problems,
              "预设「{}」标签独立型家族的标签在实际底色上亮度差都 >= 0.3".format(preset),
              problems[:6])

        menu_text = colors.get("user_interface.wcol_menu_back.text_sel")
        menu_back = colors.get("user_interface.wcol_menu_back.inner")
        check(menu_text is not None and menu_back is not None
              and abs(theme.luminance(menu_text) - theme.luminance(menu_back)) >= 0.3,
              "预设「{}」菜单 / 弹出面板里的选项标签（text_sel）是浅色、在面板底上可读".format(preset),
              (theme.color_to_hex(menu_text), theme.color_to_hex(menu_back)))

        # 真套一次、再从**实际主题**读回校验（8 位量化，用真实值算亮度差）
        theme.apply_colors(current, colors)
        round_trip = []
        for prefix in theme.LABEL_INDEPENDENT_FAMILIES:
            text_path = theme.family_path(prefix, "text_sel")
            text_color = theme.get_color(current, text_path)
            if text_color is None:
                continue
            for path, _preset_background in theme.label_backdrops(colors, prefix):
                real_background = theme.get_color(current, path)
                if real_background is None:
                    continue
                difference = abs(theme.luminance(real_background) - theme.luminance(text_color))
                if difference < 0.3:
                    round_trip.append("{}：{} 实际亮度差 {:.2f}".format(
                        prefix, path, difference))
        check(not round_trip,
              "预设「{}」真的套到主题上之后，标签文字仍然读得清（读回值校验）".format(preset),
              round_trip[:6])

        # 菜单高亮行 / 下拉菜单保持"强调色底 + 近黑字"的设计（用户要的效果）
        for prefix in ("wcol_menu_item", "wcol_pulldown"):
            background = colors.get(theme.family_path(prefix, "inner_sel"))
            text_color = colors.get(theme.family_path(prefix, "text_sel"))
            check(background is not None and text_color is not None
                  and abs(theme.luminance(background) - theme.luminance(text_color)) >= 0.3,
                  "预设「{}」{} 的「强调色底 + 文字」对比仍在（高亮行设计没被改掉）".format(
                      preset, prefix),
                  (theme.color_to_hex(background), theme.color_to_hex(text_color)))


def test_preset_death_stranding(module):
    section("预设：死亡搁浅（蓝为主强调 / 橙只做点缀 / 面板三级层次）")
    theme = module.theme
    current = theme.current_theme(bpy.context)
    check(current is not None, "能取到当前主题")
    if current is None:
        return

    # ---- 色表：逐项等于用户确认过的调色板（不许臆造别的色值）----
    expected_palette = {
        "bg": "#14171A", "panel": "#1C2024", "elevated": "#262B30",
        "line": "#3A4046", "text": "#D6DADE", "text_dim": "#8A9299",
        "blue": "#4B7FA8", "blue_dim": "#2E4E68", "orange": "#D08A2E",
        # 视口选中专用的**高饱和橙**：由 orange 同色相派生（见 theme.py 的推导注释）
        "orange_vivid": "#E0840B",
        "orange_dim": "#8A5A1E", "steel": "#6E7B85", "red": "#B4453C",
        "warn": "#C9A227", "disabled": "#5A626B",
    }
    check(theme.STRANDING == expected_palette,
          "「死亡搁浅」色表是集中管理的一份字典，且逐项等于确认过的调色板",
          (theme.STRANDING, expected_palette))
    # 派生关系必须成立：同色相（±1°）、饱和度被推到 >= 0.9、明度不低于原橙
    orange_hsv = colorsys.rgb_to_hsv(*theme.stranding_color("orange")[:3])
    vivid_hsv = colorsys.rgb_to_hsv(*theme.stranding_color("orange_vivid")[:3])
    check(abs(vivid_hsv[0] - orange_hsv[0]) < 1.0 / 360.0,
          "高饱和橙与点缀橙**同色相**（不臆造新色相；实测 {:.1f}° vs {:.1f}°）".format(
              orange_hsv[0] * 360.0, vivid_hsv[0] * 360.0),
          (orange_hsv, vivid_hsv))
    check(vivid_hsv[1] >= 0.9,
          "高饱和橙的饱和度 >= 0.9（实测 {:.3f}，原橙 {:.3f}）".format(
              vivid_hsv[1], orange_hsv[1]),
          vivid_hsv)
    check(vivid_hsv[2] >= orange_hsv[2],
          "高饱和橙的明度不低于原橙（实测 {:.3f} vs {:.3f}）".format(
              vivid_hsv[2], orange_hsv[2]),
          (vivid_hsv[2], orange_hsv[2]))

    name = "死亡搁浅"
    check(name in theme.preset_names(), "预设清单里有「死亡搁浅」", theme.preset_names())
    colors = theme.preset_colors(name) or {}
    check(bool(colors), "「死亡搁浅」预设能取到颜色（{} 项）".format(len(colors)))

    blue = theme.stranding_color("blue")
    blue_dim = theme.stranding_color("blue_dim")

    # ---- 蓝是主选中色：没有一族把选中态染成暖色，多数家族就是 DS_BLUE ----
    accent_families = []
    exact_blue = []
    warm_families = []
    for prefix, _label in theme.WIDGET_FAMILIES:
        color = colors.get(theme.family_path(prefix, "inner_sel"))
        if color is None:
            continue
        if theme.color_close(color, blue) or theme.color_close(color, blue_dim):
            accent_families.append(prefix)
        if theme.color_close(color, blue):
            exact_blue.append(prefix)
        if theme.warm_accent(color):
            warm_families.append(prefix)
    check(not warm_families,
          "没有任何控件家族的选中态是暖色（用户明确禁止把所有选中态染橙）",
          warm_families)
    check(len(accent_families) >= 15,
          "多数家族的选中态填充是 DS_BLUE / 其深档（{}/{} 个家族）".format(
              len(accent_families), len(theme.WIDGET_FAMILIES)),
          accent_families)
    check(len(exact_blue) >= 12,
          "其中多数就是 DS_BLUE 本身（{} 个家族）".format(len(exact_blue)), exact_blue)

    # ---- 橙（以及全部暖色）只出现在明确列出的少量路径上 ----
    warm_paths = sorted(path for path, color in colors.items() if theme.warm_accent(color))
    check(set(warm_paths) == set(theme.STRANDING_WARM_PATHS),
          "整套预设里的暖色路径**就是**白名单那 {} 条（清单外出现一条就失败）".format(
              len(theme.STRANDING_WARM_PATHS)),
          (warm_paths, sorted(theme.STRANDING_WARM_PATHS)))
    check(len(theme.STRANDING_WARM_PATHS) <= 12,
          "点缀暖色路径有数量上限（<= 12：原 8 条 + 视口选中 4 条，防止以后被铺开）",
          len(theme.STRANDING_WARM_PATHS))
    orange_paths = [path for path, color in colors.items()
                    if theme.color_close(color, theme.stranding_color("orange"))
                    or theme.color_close(color, theme.stranding_color("orange_dim"))]
    check(sorted(orange_paths) == ["user_interface.panel_active",
                                   "user_interface.wcol_state.inner_changed",
                                   "user_interface.wcol_state.inner_changed_sel"],
          "界面上的「点缀橙」仍只落在「活动面板标题 + 已修改状态」这三处（视口用高饱和橙）",
          sorted(orange_paths))

    # ---- 视口里被选中的几何：换成高饱和橙（用户原话："选中线换成高饱和的橙色"）----
    vivid = theme.stranding_color("orange_vivid")
    vivid_saturation = theme.saturation_of(vivid)
    background = theme.stranding_color("bg")
    panel = theme.stranding_color("panel")
    print("  死亡搁浅视口选中色 {} 饱和度 {:.3f}（与 DS_BG 亮度差 {:.3f} / 与 DS_PANEL 亮度差 {:.3f}）".format(
        theme.color_to_hex(vivid), vivid_saturation,
        abs(theme.luminance(vivid) - theme.luminance(background)),
        abs(theme.luminance(vivid) - theme.luminance(panel))))
    for path in theme.VIEWPORT_SELECTION_PATHS:
        color = colors.get(path)
        check(color is not None,
              "「死亡搁浅」{} 有显式赋值（视口选中色不能留空）".format(path))
        if color is None:
            continue
        check(theme.saturation_of(color) >= 0.9,
              "「死亡搁浅」{} 饱和度 >= 0.9（实测 {:.3f}）".format(
                  path, theme.saturation_of(color)),
              (theme.color_to_hex(color), theme.saturation_of(color)))
        check(theme.color_close(color, vivid, 0.02)
              or (len(color) > 3 and color[3] < 0.999
                  and theme.color_close(color[:3] + (1.0,), vivid, 0.02)),
              "「死亡搁浅」{} 就是调色板里的高饱和橙 {}（面选中允许保留 50% alpha）".format(
                  path, theme.color_to_hex(vivid)),
              (theme.color_to_hex(color), theme.color_to_hex(vivid)))
        gap = min(abs(theme.luminance(color) - theme.luminance(background)),
                  abs(theme.luminance(color) - theme.luminance(panel)))
        check(gap >= 0.3,
              "「死亡搁浅」{} 与视口背景 / 面板底的亮度差 >= 0.3（实测 {:.3f}）".format(
                  path, gap),
              (theme.luminance(color), theme.luminance(background), theme.luminance(panel)))
    # 界面控件**不许**跟着一起改橙（用户要的只是"视口里的选中线"）
    widget_warm = [prefix for prefix, _label in theme.WIDGET_FAMILIES
                   if theme.warm_accent(colors.get(theme.family_path(prefix, "inner_sel")))]
    check(not widget_warm,
          "「死亡搁浅」的界面控件选中态仍然是蓝（没有整族改成橙）", widget_warm)

    # ---- 文字：灰白，不用纯白铺陈 ----
    whites = [path for path, color in colors.items() if min(color[:3]) >= 0.99]
    check(not whites, "预设里没有一处是纯白（文字用灰白 DS_TEXT）", whites)
    family_texts = set()
    for prefix, _label in theme.WIDGET_FAMILIES:
        color = colors.get(theme.family_path(prefix, "text"))
        if color is not None:
            family_texts.add(theme.color_to_hex(color))
    check(family_texts <= {theme.STRANDING["text"], theme.STRANDING["text_dim"]},
          "家族常态文字只用 DS_TEXT / 次级文字 DS_TEXT_DIM",
          sorted(family_texts))

    # ---- 面板三级层次 ----
    levels = [theme.luminance(theme.stranding_color(key))
              for key in ("bg", "panel", "elevated")]
    check(levels[0] < levels[1] < levels[2],
          "面板层次 bg < panel < elevated（亮度递增，层级做得出来）",
          ["{:.4f}".format(value) for value in levels])
    check(theme.color_close(colors.get("view_3d.back"), theme.stranding_color("bg"))
          and theme.color_close(colors.get("node_editor.back"), theme.stranding_color("bg")),
          "3D 视口与节点编辑器背景用冷炭黑 DS_BG")

    # ---- 标签独立型家族（含 option / radio / toggle）的标签可读 ----
    label_problems = []
    for prefix in theme.LABEL_INDEPENDENT_FAMILIES:
        label_problems.extend(theme.label_contrast_problems(colors, prefix))
    check(not label_problems,
          "「死亡搁浅」标签独立型家族（wcol_menu_back / option / radio / toggle）标签可读",
          label_problems[:6])

    # ---- 视口辅助元素仍然写回出厂默认值（网格和默认一样明显）----
    helper_problems = []
    for path in theme.viewport_helper_paths():
        default = theme.default_of(path)
        if default is None:
            continue
        if path not in colors or not color_close(colors[path], default):
            helper_problems.append(path)
    check(not helper_problems, "「死亡搁浅」把视口辅助元素写回出厂默认值", helper_problems)

    # ---- 真套一次：实际主题里也要满足可读性（读回值校验，8 位量化）----
    theme.apply_colors(current, colors)
    live_problems = []
    for prefix, _label in theme.WIDGET_FAMILIES:
        live_problems.extend(theme.family_contrast_report(current, prefix))
    check(not live_problems,
          "「死亡搁浅」真的套到主题上之后，逐家族都没有看不清的字",
          live_problems[:6])
    # 套完之后网格线仍应是出厂值（预设能自愈旧版留下的暗值）
    grid_ok = color_close(theme.get_color(current, "view_3d.grid"),
                          theme.default_of("view_3d.grid"))
    check(grid_ok, "套用「死亡搁浅」之后基面网格线仍是出厂默认值",
          (theme.color_to_hex(theme.get_color(current, "view_3d.grid")),
           theme.color_to_hex(theme.default_of("view_3d.grid"))))


def test_preset_viewport_helpers(module):
    section("预设：视口辅助元素写回出厂默认值（网格线 / 法线）")
    theme = module.theme
    current = theme.current_theme(bpy.context)
    check(current is not None, "能取到当前主题")
    if current is None:
        return

    check("view_3d.grid" in theme.VIEWPORT_HELPERS
          and "view_3d.normal" in theme.VIEWPORT_HELPERS,
          "视口辅助元素清单就是网格线 + 法线", theme.VIEWPORT_HELPERS)
    check(set(theme.VIEWPORT_HELPERS) <= set(theme.viewport_helper_paths()),
          "「写回出厂默认值」的清单包含这两个辅助元素",
          theme.viewport_helper_paths()[:4])
    check(theme.resolve(current, "user_interface.wcol_slider.inner") is None,
          "候选里的 wcol_slider 在当前 Blender 上不存在（真机 bl_rna 查不到）")
    check(not (set(theme.DEEP_ACCENT_FAMILIES) & set(theme.viewport_helper_paths())),
          "「深档强调」的三个家族**不**在写回出厂默认值的清单里（机制已分家）",
          theme.DEEP_ACCENT_FAMILIES)

    for preset in theme.preset_names():
        colors = theme.preset_colors(preset) or {}
        wrong = []
        for path in theme.viewport_helper_paths():
            default = theme.default_of(path)
            if default is None:
                continue
            if path not in colors:
                wrong.append("{} 漏了".format(path))
            elif not color_close(colors[path], default):
                wrong.append("{} = {}（应为 {}）".format(
                    path, theme.color_to_hex(colors[path]), theme.color_to_hex(default)))
        check(not wrong,
              "预设「{}」把视口辅助元素写成记录的出厂默认值".format(preset),
              wrong[:6])
        check(theme.VIEWPORT_HELPERS and
              all(helper in theme.default_paths() for helper in theme.VIEWPORT_HELPERS),
              "预设「{}」的辅助元素都有出厂默认值记录（不猜颜色）".format(preset),
              theme.VIEWPORT_HELPERS)

    # 自愈：先把主题写成"旧版预设留下的坏值"，再套一次预设，必须回到出厂默认值
    for path in theme.viewport_helper_paths():
        if theme.default_of(path) is None:
            continue
        count = theme.component_count(current, path)
        theme.set_color(current, path, (0.02, 0.03, 0.02, 0.2) if count == 4 else (0.02, 0.03, 0.02))
    theme.apply_colors(current, theme.preset_colors(theme.preset_names()[0]) or {})
    healed = [path for path in theme.viewport_helper_paths()
              if theme.default_of(path) is not None
              and not color_close(theme.get_color(current, path), theme.default_of(path))]
    check(not healed,
          "主题里被旧版写坏的网格线 / 法线，再套一次预设就自愈到出厂默认值",
          healed[:6])


def test_preset_deep_accent_families(module):
    section("预设：滑块 / 工具按钮 / 顶部工具项（深档强调 + 浅色文字，不再回出厂默认）")
    theme = module.theme
    current = theme.current_theme(bpy.context)
    check(current is not None, "能取到当前主题")
    if current is None:
        return

    check("wcol_numslider" in theme.DEEP_ACCENT_FAMILIES,
          "数值滑块在「深档强调」清单里（颜色选择器的 H/S/V/Alpha 与 RGB 滑条）",
          theme.DEEP_ACCENT_FAMILIES)
    check("wcol_tool" in theme.DEEP_ACCENT_FAMILIES
          and "wcol_toolbar_item" in theme.DEEP_ACCENT_FAMILIES,
          "工具按钮类两个家族都在清单里（左侧工具栏 / 顶部工具项）",
          theme.DEEP_ACCENT_FAMILIES)
    check(not hasattr(theme, "FACTORY_LOCKED_FAMILIES"),
          "整族写回出厂默认值的旧机制已经不存在")
    check(0.45 <= theme.DEEP_ACCENT_FACTOR <= 0.6,
          "深档压暗倍数在用户给的 45%~60% 区间里", theme.DEEP_ACCENT_FACTOR)

    # 三套预设 × 三个家族：风格色 + 可读 + 拖动块可分辨 + 不再等于出厂默认值
    for preset in theme.preset_names():
        colors = theme.preset_colors(preset) or {}
        for prefix in theme.DEEP_ACCENT_FAMILIES:
            inner = colors.get(theme.family_path(prefix, "inner"))
            inner_sel = colors.get(theme.family_path(prefix, "inner_sel"))
            item = colors.get(theme.family_path(prefix, "item"))
            text = colors.get(theme.family_path(prefix, "text"))
            text_sel = colors.get(theme.family_path(prefix, "text_sel"))
            check(None not in (inner, inner_sel, item, text, text_sel),
                  "预设「{}」{} 轨道 / 填充 / 色块 / 文字都有值".format(preset, prefix))
            if None in (inner, inner_sel, item, text, text_sel):
                continue

            # 深档：填充比强调色（item 就是强调色本身）暗，不是又把强调色铺满
            check(theme.luminance(inner_sel) < theme.luminance(item),
                  "预设「{}」{} 的填充是**深档**强调色（比 item 暗）".format(preset, prefix),
                  (theme.color_to_hex(inner_sel), theme.color_to_hex(item)))
            # 拖动块可分辨：轨道与填充的亮度差够大
            gap = abs(theme.luminance(inner) - theme.luminance(inner_sel))
            check(gap >= 0.15,
                  "预设「{}」{} 轨道与填充亮度差 >= 0.15（拖动色块分得开）".format(
                      preset, prefix),
                  "{:.4f} {}".format(gap, (theme.color_to_hex(inner),
                                           theme.color_to_hex(inner_sel))))
            # 三层互不相同（不是整条一个色）
            check(not color_close(inner, inner_sel, 0.02)
                  and not color_close(inner, item, 0.02)
                  and not color_close(inner_sel, item, 0.02),
                  "预设「{}」{} 三色互不相同（拖得出色块）".format(preset, prefix),
                  (theme.color_to_hex(inner), theme.color_to_hex(inner_sel),
                   theme.color_to_hex(item)))
            # 可读：浅色文字在轨道与填充上都够对比
            check(abs(theme.luminance(text) - theme.luminance(inner)) >= 0.3,
                  "预设「{}」{} 值文字 vs 轨道亮度差 >= 0.3".format(preset, prefix),
                  (theme.color_to_hex(text), theme.color_to_hex(inner)))
            check(abs(theme.luminance(text_sel) - theme.luminance(inner_sel)) >= 0.3,
                  "预设「{}」{} 选中文字 vs 填充亮度差 >= 0.3".format(preset, prefix),
                  (theme.color_to_hex(text_sel), theme.color_to_hex(inner_sel)))
            # 不再等于出厂默认值（这就是用户报的"滑块变蓝、工具栏变默认"）
            still_default = []
            for member, value in (("inner", inner), ("inner_sel", inner_sel),
                                  ("text", text), ("text_sel", text_sel)):
                path = theme.family_path(prefix, member)
                default = theme.default_of(path)
                if default is None:
                    continue
                if color_close(value, default):
                    still_default.append("{}={}".format(member, theme.color_to_hex(value)))
            check(not still_default,
                  "预设「{}」{} 不再等于出厂默认值".format(preset, prefix),
                  still_default)

    # 派生方式写清楚：马拉松两套的深档 == scale_color(强调色, DEEP_ACCENT_FACTOR)
    for preset in ("马拉松 建模 低干扰", "马拉松 浅灰卡"):
        colors = theme.preset_colors(preset) or {}
        for prefix in theme.DEEP_ACCENT_FAMILIES:
            accent = colors.get(theme.family_path(prefix, "item"))
            inner_sel = colors.get(theme.family_path(prefix, "inner_sel"))
            derived = theme.scale_color(accent, theme.DEEP_ACCENT_FACTOR)
            check(theme.color_close(inner_sel, derived),
                  "预设「{}」{} 的深档 == scale_color(强调色, {})（派生值不是凭空取的）".format(
                      preset, prefix, theme.DEEP_ACCENT_FACTOR),
                  (theme.color_to_hex(inner_sel), theme.color_to_hex(derived)))

    # 死亡搁浅那一套用的是调色板里备好的蓝深档（不是现算的）
    colors = theme.preset_colors("死亡搁浅") or {}
    for prefix in theme.DEEP_ACCENT_FAMILIES:
        check(color_close(colors.get(theme.family_path(prefix, "inner_sel")),
                          theme.stranding_color("blue_dim")),
              "「死亡搁浅」{} 的填充就是调色板里的 DS_BLUE_DIM".format(prefix),
              theme.color_to_hex(colors.get(theme.family_path(prefix, "inner_sel"))))

    # 真套一次：轨道 / 填充 / 文字在真实主题里也分得开（8 位量化 + 读回校验）
    theme.apply_colors(current, theme.preset_colors(theme.preset_names()[0]) or {})
    for prefix in theme.DEEP_ACCENT_FAMILIES:
        live = theme.read_family(current, prefix)
        if not {"inner", "inner_sel", "text", "text_sel"} <= set(live):
            continue
        check(abs(theme.luminance(live["inner"]) - theme.luminance(live["inner_sel"])) >= 0.15,
              "套用预设后「{}」实际主题里轨道与填充仍分得开".format(prefix),
              (theme.color_to_hex(live["inner"]), theme.color_to_hex(live["inner_sel"])))
        check(not theme.family_contrast_report(current, prefix),
              "套用预设后「{}」实际主题里没有看不清的字".format(prefix),
              theme.family_contrast_report(current, prefix))


#: 视口选中几何的饱和度下限 —— 用户实测反馈给出的阈值（"被选中的线饱和度太低看不清"）。
#: 注意：界面控件的低饱和档 #B9CE4E 饱和度实测约 0.62，**也过得了这条阈值**，
#: 所以下面另有一条"不许等于低饱和档"的断言；两条一起才是完整防线。
VIEWPORT_SELECTION_MIN_SATURATION = 0.6

#: 用鲜亮档视口选中色的两套预设（「死亡搁浅」不在这里：它用高饱和橙，另有断言守着）。
VIEWPORT_SELECTION_PRESETS = ("马拉松 建模 低干扰", "马拉松 浅灰卡")

#: 「界面控件的低饱和档没有被连带改亮」的代表性路径（普通按钮 / 分组框 / 列表行）。
CALM_GUARD_PATHS = (
    "user_interface.wcol_regular.inner_sel",
    "user_interface.wcol_box.inner_sel",
    "user_interface.wcol_list_item.inner_sel",
)


def test_viewport_selection_accent(module):
    section("预设：视口里被选中的几何用鲜亮档（不被界面控件的低饱和档连带压灰）")
    theme = module.theme
    current = theme.current_theme(bpy.context)
    check(current is not None, "能取到当前主题")
    if current is None:
        return

    bright = theme.hex_color(theme.TOKENS["ACCENT_PRIMARY"])
    calm = theme.hex_color(theme.TOKENS["ACCENT_PRIMARY_CALM"])
    print("  视口选中几何：鲜亮档 {} 饱和度 {:.3f} / 低饱和档 {} 饱和度 {:.3f}".format(
        theme.color_to_hex(bright), theme.saturation_of(bright),
        theme.color_to_hex(calm), theme.saturation_of(calm)))
    # 「死亡搁浅」的视口选中色已改成高饱和橙（详见 test_preset_death_stranding）
    death_stranding = theme.preset_colors("死亡搁浅") or {}
    ds_edge = death_stranding.get("view_3d.edge_select")
    print("  死亡搁浅视口选中色 {} 饱和度 {:.3f}（高饱和橙，详见死亡搁浅那节）".format(
        theme.color_to_hex(ds_edge), theme.saturation_of(ds_edge)))

    check(tuple(theme.VIEWPORT_SELECTION_PATHS) == (
        "view_3d.editmesh_active", "view_3d.vertex_select",
        "view_3d.edge_select", "view_3d.face_select"),
        "「视口选中几何」清单就是活动元素 / 顶点 / 边 / 面 四项",
        theme.VIEWPORT_SELECTION_PATHS)
    check(not (set(theme.VIEWPORT_SELECTION_PATHS) & set(theme.viewport_helper_paths())),
          "选中几何与「写回出厂默认值」的视口辅助元素不重叠（两套机制分开）",
          theme.VIEWPORT_SELECTION_PATHS)
    untouched = [path for path in ("view_3d.edge", "view_3d.wire_edit", "view_3d.edge_seam",
                                   "view_3d.edge_sharp", "view_3d.edge_crease",
                                   "view_3d.edge_bevel")
                 if path in theme.VIEWPORT_SELECTION_PATHS]
    check(not untouched,
          "未选中边 / 线框 / 特征标记没有进「鲜亮档」清单（保持原设计）", untouched)

    measured = []
    for preset in VIEWPORT_SELECTION_PRESETS:
        colors = theme.preset_colors(preset) or {}
        for path in theme.VIEWPORT_SELECTION_PATHS:
            color = colors.get(path)
            check(color is not None,
                  "预设「{}」{} 有显式赋值（视口选中色不能留空）".format(preset, path))
            if color is None:
                continue
            saturation = theme.saturation_of(color)
            measured.append((preset, path, saturation, theme.color_to_hex(color)))
            check(saturation >= VIEWPORT_SELECTION_MIN_SATURATION,
                  "预设「{}」{} 饱和度 >= {}（阈值；实测 {:.3f}，色值 {}）".format(
                      preset, path, VIEWPORT_SELECTION_MIN_SATURATION,
                      saturation, theme.color_to_hex(color)))
            check(not color_close(color, calm, 0.02),
                  "预设「{}」{} 不再等于界面控件的低饱和档 {}（防止改回去；实测饱和度 {:.3f}）".format(
                      preset, path, theme.color_to_hex(calm), saturation),
                  (theme.color_to_hex(color), theme.color_to_hex(calm)))
    for preset, path, saturation, value in measured:
        print("    {} | {} = {} 饱和度 {:.3f}".format(preset, path, value, saturation))

    # 界面控件的低饱和档没有被"一改全亮"连带改掉（三个代表家族）
    for preset in VIEWPORT_SELECTION_PRESETS:
        colors = theme.preset_colors(preset) or {}
        for path in CALM_GUARD_PATHS:
            value = colors.get(path)
            check(value is not None and color_close(value, calm, 0.02),
                  "预设「{}」{} 仍是界面控件的低饱和档 {}（没有被连带改亮）".format(
                      preset, path, theme.color_to_hex(calm)),
                  (theme.color_to_hex(value), theme.color_to_hex(calm)))

    # 真套一次：从**实际主题**读回校验（8 位量化之后也不许掉回低饱和档）
    theme.apply_colors(current, theme.preset_colors(VIEWPORT_SELECTION_PRESETS[0]) or {})
    live = {}
    for path in theme.VIEWPORT_SELECTION_PATHS:
        color = theme.get_color(current, path)
        if color is not None:
            live[path] = theme.saturation_of(color)
    check(len(live) >= 1,
          "能从实际主题读回选中几何（{} / {} 项在当前 Blender 上存在）".format(
              len(live), len(theme.VIEWPORT_SELECTION_PATHS)), sorted(live))
    below = {path: round(value, 3) for path, value in sorted(live.items())
             if value < VIEWPORT_SELECTION_MIN_SATURATION}
    check(not below,
          "套用「{}」后实际主题里的选中几何饱和度 >= {}（读回实测 {}）".format(
              VIEWPORT_SELECTION_PRESETS[0], VIEWPORT_SELECTION_MIN_SATURATION,
              {path: round(value, 3) for path, value in sorted(live.items())}),
          below)

    # ---- 颜色到顶之后靠线宽：ThemeView3D.edge_width（真正的 bl_rna 实测，不凭记忆）----
    size_path = "view_3d.edge_width"
    check(theme.resolve_size(current, size_path) is not None,
          "本版本真的有 {}（bl_rna 实测：INT + PIXEL 子类型）".format(size_path),
          theme.resolve_size(current, size_path))
    check(theme.get_color(current, size_path) is None,
          "它**不是**颜色项（颜色那套读不到它，所以两套路径分开）")
    before_width = theme.get_size(current, size_path)
    writable = theme.set_size(current, size_path, 3)
    check(writable and theme.get_size(current, size_path) == 3,
          "{} 可写（写入 3 后读回实测 {}）".format(size_path, theme.get_size(current, size_path)),
          (writable, theme.get_size(current, size_path)))
    theme.set_size(current, size_path, before_width if before_width is not None else 1)
    check(theme.size_default(size_path) == 1,
          "出厂默认值记为 1（三版 factory-startup 实测）", theme.size_default(size_path))
    check(theme.MARATHON_VIEWPORT_SIZES == {size_path: 2},
          "两套马拉松预设把选中边线宽写成 2（粗一档）", theme.MARATHON_VIEWPORT_SIZES)
    check(set(theme.MARATHON_PRESET_NAMES) <= set(theme.preset_names()),
          "MARATHON_PRESET_NAMES 里的名字都真的在预设表里",
          (theme.MARATHON_PRESET_NAMES, theme.preset_names()))
    size_missing = [name for name in theme.MARATHON_PRESET_NAMES
                    if theme.preset_sizes(name) != {size_path: 2}]
    check(not size_missing,
          "两套马拉松预设都返回这份尺寸表", size_missing)
    check(theme.preset_sizes("死亡搁浅") == {},
          "「死亡搁浅」不碰线宽（它这一版只换视口选中的颜色）",
          theme.preset_sizes("死亡搁浅"))
    # 走真实算符：套用马拉松预设 -> 主题里的线宽真的变成 2 -> 「恢复全部」写回 1
    result = bpy.ops.hhz.theme_apply_preset(preset=theme.MARATHON_PRESET_NAMES[0])
    check(result == {'FINISHED'}, "「应用预设」算符在马拉松预设上返回 FINISHED", result)
    live_width = theme.get_size(current, size_path)
    check(live_width == 2,
          "套用「{}」之后实际主题里的线宽 = 2（读回实测 {}）".format(
              theme.MARATHON_PRESET_NAMES[0], live_width), live_width)
    check(theme.restore_size_defaults(current) >= 1,
          "视口尺寸能一键写回默认（「恢复全部」里有这一步）")
    check(theme.get_size(current, size_path) == 1,
          "写回之后线宽 = 1（读回实测 {}）".format(theme.get_size(current, size_path)))


#: 颜色项下拉标签的平均 UTF-8 字节长度上限（缓解弹出框里的宽度截断）。
#: 实测（4.0.2 / 5.2.2）：改前全部 TARGETS 平均 37.13 字节、改后 28.33；
#: 当前版本可用项 4.0.2 = 27.47、5.2.2 = 26.19 —— 32 留了余量。
TARGET_LABEL_AVG_BYTES_MAX = 32.0

#: 「超过 12 个汉字」的条目占总数的比例上限。实测可用项：改前 146/219（4.0.2）、
#: 152/225（5.2.2），改后 47/219、33/225。
TARGET_LABEL_LONG_RATIO = 1.0 / 3.0


def test_target_item_labels(module):
    section("颜色项下拉标签（缩短以减少弹出框里的宽度截断）")
    theme = module.theme
    context = bpy.context
    current = theme.current_theme(context)
    check(current is not None, "能取到当前主题")
    if current is None:
        return

    expected_groups = ("按钮 底色", "按钮 文字与图标", "按钮 边框", "按钮 选中底色",
                       "按钮 选中文字", "按钮 选中边框", "按钮 高亮")
    check(tuple(theme.WIDGET_GROUPS) == expected_groups,
          "控件家族的 7 个分组名一个没变（只是下拉里不再重复拼它们）", theme.WIDGET_GROUPS)

    items = theme.target_items(None, context)
    labels = [item[1] for item in items]
    check(bool(labels), "颜色项下拉有条目（{} 项）".format(len(labels)))
    if not labels:
        return
    check(all(label.strip() for label in labels), "每个条目的显示文本都非空")
    repeated = [label for label in labels if label.startswith("按钮 ")]
    check(not repeated,
          "控件家族的条目不再重复拼「按钮 xx」分组名（标签本身已写明家族 + 部位）",
          repeated[:5])

    by_path = dict((item[0], item[1]) for item in items)
    icon_label = by_path.get("user_interface.icon_object")
    if icon_label is not None:
        check(icon_label.startswith("图标颜色 "),
              "非控件家族的条目仍然带分组前缀（改动只作用于控件家族那批）", icon_label)

    old_labels = ["{} {}".format(group, label) for path, label, group in theme.TARGETS
                  if theme.resolve(current, path) is not None]
    old_avg = sum(len(text.encode("utf-8")) for text in old_labels) / float(len(old_labels))
    new_avg = sum(len(text.encode("utf-8")) for text in labels) / float(len(labels))
    old_long = sum(1 for text in old_labels if len(text) > 12)
    new_long = sum(1 for text in labels if len(text) > 12)
    print("  颜色项标签：{} 项，平均字节 {:.2f} -> {:.2f}；超过 12 汉字的 {} -> {}".format(
        len(labels), old_avg, new_avg, old_long, new_long))
    check(new_avg < old_avg,
          "标签确实变短了（不是把格式又拼回长的）", (old_avg, new_avg))
    check(new_avg <= TARGET_LABEL_AVG_BYTES_MAX,
          "标签平均 UTF-8 字节长度 <= {}（改后实测 {:.2f}，改前 {:.2f}）".format(
              TARGET_LABEL_AVG_BYTES_MAX, new_avg, old_avg))
    check(new_long <= len(labels) * TARGET_LABEL_LONG_RATIO,
          "超过 12 汉字的条目 <= 总数的三分之一（改后 {}/{}，改前 {}/{}）".format(
              new_long, len(labels), old_long, len(labels)))

    simple = type("_Prefs", (object,), {"simple_mode": True})()
    simple_labels = [item[1] for item in theme.target_items(simple, context)]
    check(simple_labels and len(simple_labels) <= 40,
          "简洁模式的短列表仍然 <= 40 项（没有被这次改动影响）", len(simple_labels))
    check(not [label for label in simple_labels if label.startswith("按钮 ")],
          "简洁模式下的控件家族条目同样不再重复分组名")


def test_save_preferences(module):
    section("功能：保存到偏好设置（算子名 + 真跑一次）")
    # 注意：不能用 hasattr(bpy.ops.wm, "save_userpref") —— 项目已知它对不存在的算子名恒为真
    check("save_userpref" in dir(bpy.ops.wm),
          "Blender 侧存在 wm.save_userpref（单数，正确名字）")
    check("save_userprefs" not in dir(bpy.ops.wm),
          "Blender 侧没有 wm.save_userprefs（复数，就是旧代码写错的那个名字）")
    source = source_of(module, "ops.py")
    check("save_userprefs" not in source,
          "源码里不再出现写错的算子名 save_userprefs")
    check("bpy.ops.wm.save_userpref()" in source,
          "源码调用的是正确的 bpy.ops.wm.save_userpref()")
    static = class_source(source, "HHZ_OT_theme_save_preferences")
    check("'ERROR'" in static and "'CANCELLED'" in static,
          "内层失败会 report ERROR 并返回 CANCELLED（不是吞掉后照样 FINISHED）")
    check("use_preferences_save" in static,
          "没开「自动保存偏好设置」时报告里会补一句提示（不新增设置项）")
    check(operator_class(module, "theme_save_preferences") is not None,
          "算符 hhz.theme_save_preferences 已注册")

    config = _isolated_config_dir()
    if config is None:
        skip("配置目录不是隔离沙箱，跳过真实写偏好设置（避免改到用户真实配置）")
        return
    before = set(os.listdir(config))
    result = bpy.ops.hhz.theme_save_preferences()
    check(result != {'CANCELLED'}, "「保存到偏好设置」不是失败返回（CANCELLED）", result)
    check(result == {'FINISHED'}, "「保存到偏好设置」返回 FINISHED", result)
    check("userpref.blend" in set(os.listdir(config)),
          "偏好设置真的写进了隔离目录（沙箱）里的 userpref.blend",
          (sorted(before), sorted(os.listdir(config))))


def test_startup_layout(module):
    section("功能：把当前窗口布局设为启动布局（只存工作区，不含场景）")
    operator = operator_class(module, "layout_save_startup")
    check(operator is not None, "算符 hhz.layout_save_startup 已注册")
    source = source_of(module, "ops.py")
    static = class_source(source, "HHZ_OT_layout_save_startup")

    # ---- 缺陷 7：复用缺失文件扫描，坏引用要提示（但不再被写进启动文件）----
    check("_missing_file_blocks" in static,
          "写启动文件前复用了缺陷 3 的缺失文件扫描函数（没有另写一份）")
    check("'WARNING'" in static and "检查并修复缺失文件" in static,
          "有缺失引用时用 WARNING 提示并指向「检查并修复缺失文件」")
    check("已一并写入启动文件" not in static,
          "不再声称「缺失引用被写进启动文件」（新流程只存工作区，场景不进启动文件）")

    # ---- 缺陷 8：名称 / 描述 / 确认文案如实说明它做什么 ----
    check("不含场景" in static or "空场景" in static,
          "算符文案说清启动文件是空场景 + 工作区布局（不再让人以为是存整个文件）")
    check("保存启动文件" in static,
          "报告里指向 Blender 原生的「文件 默认 保存启动文件」（连场景一起存的那条路）")
    check("bl_label" in static and "窗口布局" in static.split("bl_label")[1][:120],
          "按钮文字就是「把当前窗口布局设为启动布局」这一类的说法（不再叫存为启动布局）",
          static.split("bl_label")[1][:80] if "bl_label" in static else "")
    check("invoke_confirm" in static,
          "仍然只弹一次原生确认框（没有新增确认框 / 设置项）")

    # ---- 缺陷 9：实现步骤的源码级断言 ----
    check("is_dirty" in static and "请先保存当前文件" in static,
          "有未保存改动时先报「请先保存当前文件」并中止（绝不带着未保存改动切文件）")
    check("bpy.app.background" in static and "original_path" in static,
          "无头环境里 4.x 的 is_dirty 恒为 True 不会把功能锁死（只在文件已落盘时放行）")
    check("libraries.write" in static and "LAYOUT_LIBRARY_NAME" in source,
          "部分库只留在「文件还没保存过」的兜底分支里（4.5.14 上写部分库必崩）")
    check("source_path" in static and "libraries.load(source_path" in static,
          "追加来源改成**用户自己保存过的那个文件**（全量 .blend 追加不崩）",
          static.count("source_path"))
    # 幂等的根因修复：不能再从出厂启动文件重新开始
    # （出厂启动文件里必然有同名自带工作区 -> 每点一次就多一批「出厂 xxx」）
    read_keywords = _call_keywords(source, "bpy.ops.wm.read_homefile")
    check(read_keywords == [],
          "载入的是**上次保存的启动文件**：bpy.ops.wm.read_homefile() 是裸调用，"
          "一个关键字参数都不带（不带 use_factory_startup）",
          read_keywords)
    check("use_empty" not in static,
          "不再依赖 use_empty=True —— 空场景改成读进来之后自己清（_empty_scene_data）")
    empty_helper = function_source(source, "_empty_scene_data")
    check("list(bpy.data.objects)" in empty_helper
          and bool(_api_calls(empty_helper, "bpy.data.objects.remove")),
          "场景数据用纯数据 API 逐个移除（bpy.data.objects.remove，不用算子）",
          empty_helper[:200])
    check("orphans_purge" in empty_helper and "do_recursive=True" in empty_helper,
          "移除物体之后清理孤儿数据（orphans_purge(do_recursive=True)）", empty_helper[:400])
    check("bpy.data.workspaces" not in empty_helper,
          "清场景**不碰工作区**（工作区布局正是要留下的东西）", empty_helper[:200])
    # ---- 幂等：同名 + 同区域构成才跳过 ----
    plan_helper = function_source(source, "_plan_workspace_appends")
    present_helper = function_source(source, "_workspace_already_present")
    composition_helper = function_source(source, "_workspace_composition")
    check("_workspace_already_present" in plan_helper and "target.workspaces = wanted" in static,
          "追加前先挑出「启动文件里没有的」，只追加这些（幂等的关键一步）", plan_helper[:300])
    check("_workspace_composition" in present_helper,
          "判据是**区域构成**，不是只看名字（同名但构成不同算不同工作区）", present_helper[:300])
    check("screen" in composition_helper and "area" in composition_helper
          and "sorted" in composition_helper,
          "区域构成 = 各区域 type 的多重集合（sorted 成可比较的元组）",
          composition_helper[:300])
    check("bpy.data.workspaces.get(name)" in present_helper,
          "按名字查启动文件里现有的工作区（查不到就是要追加）", present_helper[:300])
    check("{name}" not in composition_helper and "as_pointer" not in composition_helper,
          "构成快照按**名字**索引（切文件前后对象变了，指针不可用）", composition_helper[:200])
    # ---- 落盘前校验：三条都写在源码里 ----
    check("lost_names" in static and "校验不通过" in static,
          "落盘前校验①：用户的工作区一个都没丢（名字集合 ⊇ 运行前记录的集合）")
    check("pairs_left" in static and "_workspace_collision_pairs()" in static,
          "落盘前校验②：不存在「X」与「X.<数字>」并存")
    check("objects_left" in static,
          "落盘前校验③：物体数为 0（启动文件是空场景）")
    check("return restore_original" in static and static.count("校验不通过") >= 3,
          "三条校验任何一条不过都**不写**启动文件（return restore_original）",
          static.count("校验不通过"))
    check("LAST_STARTUP_LAYOUT_RESULT" in source and "appended_total" in source,
          "本次结果（新增 / 跳过）记在模块级变量里，供真机测试直接断言（不是设置项）")
    # ---- 报告文案：新增几个 / 跳过几个 / 空场景 / 出厂项怎么删 ----
    report_helper = function_source(source, "startup_report_parts")
    check("startup_report_parts" in static and "本次新增" in report_helper,
          "报告文案由 startup_report_parts 生成，明说本次新增几个", report_helper[:200])
    report_both = "；".join(module.ops.startup_report_parts(["A", "B"], ["C", "D", "E"], []))
    check("本次新增 2 个工作区" in report_both,
          "新增数如实写进报告", report_both)
    check("跳过 3 个已存在的" in report_both and "同名且区域构成相同" in report_both,
          "跳过数 + 原因（同名且区域构成相同）如实写进报告", report_both)
    report_zero = "；".join(module.ops.startup_report_parts([], ["C"], []))
    check("本次新增 0 个工作区" in report_zero,
          "一个都没新增时报告说的是 0（连点两次就是这个结果）", report_zero)
    check("空场景" in report_zero and "不含当前场景" in report_zero,
          "报告说清启动文件仍是空场景、不含当前场景", report_zero)
    report_factory = "；".join(module.ops.startup_report_parts([], [], ["出厂 建模", "出厂 雕刻"]))
    check("还留着 2 个带「出厂」前缀的工作区" in report_factory,
          "启动文件里残留的「出厂 xxx」数量如实报出", report_factory)
    check("删不掉工作区" in report_factory and "右键" in report_factory
          and "删除工作区" in report_factory,
          "残留的出厂项给出可执行路径：本版脚本删不掉，工作区标签上右键 ▸ 删除工作区",
          report_factory)
    # 回归：用户实测「bpy.ops.wm.append.poll() failed, context is incorrect」——
    # wm.append 的 poll（wm_link_append_poll）要求 WM_operator_winactive，而 read_homefile
    # 之后界面里的窗口 context 已经失效，所以这条算子路在真实界面里必然失败。
    check(not _api_calls(source, "bpy.ops.wm.append"),
          "源码里没有 bpy.ops.wm.append() 调用（切换文件后它的 poll 在界面里必失败）",
          _api_calls(source, "bpy.ops.wm.append"))
    check(bool(_api_calls(source, "bpy.data.libraries.load"))
          and "target.workspaces" in static,
          "改用纯数据 API bpy.data.libraries.load() 载入工作区（没有算子 poll）")
    check("before_append" in static and "added" in static,
          "载入工作区后有「真的进来了」的校验（按 as_pointer 记录前后差集）")
    check("restore_original" in static and "会话已恢复到原来的文件" in static,
          "切换文件之后任何一步失败，都会把会话自动恢复回原来的文件")
    check("missing_workspaces" in static and "已中止、没有写启动文件" in static,
          "校验不通过就中止且不写启动文件（避免存出一个没有布局的启动文件）")
    check("save_homefile" in static and "open_mainfile" in static,
          "最后写启动文件并把用户原来的文件重新打开")
    check("LAYOUT_LIBRARY_NAME" in source and "user_resource('CONFIG')" in source,
          "布局库放在 Blender 用户配置目录下的固定名字里（不写进用户工程目录）")

    # ---- 重名收尾（中文界面下出厂启动文件里也有同名工作区）----
    dedupe = function_source(source, "_dedupe_appended_workspaces")
    delete_helper = function_source(source, "_delete_workspace")
    collision_helper = function_source(source, "_workspace_collision_pairs")
    report_helper = function_source(source, "_dedupe_report_parts")
    check("_dedupe_appended_workspaces" in static,
          "启动布局流程里调用了重名收尾函数")
    check("_workspace_collision_pairs" in collision_helper
          and "isdigit()" in collision_helper,
          "只认「X 与 X.<数字> 同时存在」的成对重名（不成对的一律不碰）",
          collision_helper[:200])
    check(bool(_api_calls(delete_helper, "bpy.ops.workspace.delete")),
          "删除出厂重名走的是工作区删除算符 bpy.ops.workspace.delete()")
    check("result != {'FINISHED'}" in delete_helper
          or 'result != {"FINISHED"}' in delete_helper,
          "**检查删除算符的返回值**（只有 FINISHED 才继续，不然后面全是假的）",
          delete_helper[:400])
    check("except Exception" in delete_helper,
          "删除算符抛异常也算没删掉（无头下实测会抛 GPU API 不可用）")
    check("for item in bpy.data.workspaces" in delete_helper
          and "len(bpy.data.workspaces) < before" in delete_helper,
          "不止看返回值：还要核对**实际效果**（对象真的没了 + 数量真的少了）")
    check(not _api_calls(source, "bpy.data.batch_remove"),
          "绝不用 bpy.data.batch_remove 删工作区（删到窗口当前那个会让 Blender "
          "退出时段错误，实测 exit=11）",
          _api_calls(source, "bpy.data.batch_remove"))
    check("break" not in dedupe,
          "某一对收尾失败**只跳过这一对**、继续处理其它对（不 break 掉整个循环）")
    check("_rename_factory_workspace" in dedupe and "FACTORY_WORKSPACE_PREFIX" in source,
          "删不掉时给出厂那份改名让位（把原名让给用户的工作区）")
    check("plain.name = target_name" in dedupe,
          "改名回原名失败时把出厂那份改回去（不留'两边都不是原名'的中间态）")
    check("_make_workspace_current" in source and "window.workspace" in source,
          "先把出厂那份设为当前工作区，并**核实**是否真的成了（删除算符删的是当前工作区）")
    check("GPU" in delete_helper or "background" in delete_helper,
          "把「无头下删除算符不可用」这条实测写进了实现注释（免得以后有人又踩）")
    # 文案必须如实 + 给出可执行的替代路径；而且真机测试直接调用它断言
    report_text = "；".join(module.ops._dedupe_report_parts(11, 0, 11, [], []))
    check("删掉 0 个出厂重名" in report_text,
          "收尾报告如实说出删掉了几个出厂重名", report_text)
    check("删不掉工作区" in report_text,
          "删不掉时如实写明「本版 Blender 的脚本接口删不掉工作区」", report_text)
    check("右键" in report_text and "删除工作区" in report_text,
          "报告给出可执行的替代路径（工作区标签上右键 ▸ 删除工作区）", report_text)
    leftover_text = "；".join(module.ops._dedupe_report_parts(
        11, 0, 11, [(None, None)], ["改名失败"]))
    check("还有 1 对同名工作区没能收尾" in leftover_text and "改名失败" in leftover_text,
          "还有残留重名时如实报出条数与原因", leftover_text)
    clean_text = "；".join(module.ops._dedupe_report_parts(0, 0, 0, [], []))
    check(clean_text == "", "没有重名时不多说一句废话（报告保持干净）", clean_text)

    config = _isolated_config_dir()
    if config is None:
        skip("配置目录不是隔离沙箱，跳过真实写启动文件（避免改到用户真实配置）")
        return

    _purge_test_samples(module)
    # 让 Blender 能干净地写出启动文件：清掉测试留下的孤立数据块（含带坏 from 指针的
    # 形态键），否则 Blender 自己会在写盘时报 "ShapeKey ... invalid 'from' pointer"，
    # 那是测试夹具的副作用，不是被测算符的行为。
    reset_scene()
    try:
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    except Exception:
        pass

    # ---- (a) 有未保存改动 -> CANCELLED，且不切换文件 ----
    # 无头环境里 ``is_dirty`` 两个方向都不可靠（实测：4.x 恒为 True，连保存之后也是；
    # 5.x 恒为 False，加物体也不置脏）。所以这一条**能真跑就真跑**，
    # 造不出"脏"就如实 skip，只保留源码级断言 —— 不假装验证过。
    _purge_test_samples(module)
    add_cube("hhz_dirty_marker")
    path_before_dirty = bpy.data.filepath
    if not bpy.data.is_dirty:
        skip("无头环境造不出 is_dirty=True（5.x 背景模式下加物体也不置脏），"
             "「未保存就先拒绝」这一条只保留源码级断言")
    else:
        check(bpy.data.is_dirty is True, "造出了未保存改动（is_dirty=True）", bpy.data.is_dirty)
        try:
            result = bpy.ops.hhz.layout_save_startup(confirm=True)
        except Exception as error:
            result = "抛出异常：{}".format(error)
        check(result == {'CANCELLED'},
              "有未保存改动时返回 CANCELLED（不会带着未保存改动切文件）", result)
        check(bpy.data.filepath == path_before_dirty,
              "未保存时没有切换文件（会话停在原处）",
              (path_before_dirty, bpy.data.filepath))
        check(bpy.data.objects.get("hhz_dirty_marker") is not None,
              "未保存时当前场景没有被丢掉（那个标记物体还在）")
    reset_scene()

    # ---- (b)(c)(d)(e) 完整流程：真的在沙箱里跑，跑两次验证幂等 ----
    sandbox_file = os.path.join(config, "hhz_startup_test.blend")
    try:
        bpy.ops.wm.save_as_mainfile(filepath=sandbox_file)
    except Exception as error:
        skip("无头环境里存不了测试文件（{}），跳过完整流程".format(error))
        return
    if bpy.app.version >= (5, 0, 0):
        check(bpy.data.is_dirty is False,
              "5.x：保存之后 is_dirty 清零", bpy.data.is_dirty)
    else:
        check(bpy.data.is_dirty is True,
              "4.x 无头环境下保存之后 is_dirty 仍为 True（实测特性，算符已按此放行）",
              bpy.data.is_dirty)

    recorded_path = bpy.data.filepath
    _purge_test_samples(module)

    # 造一个「启动文件里肯定没有」的工作区名：把一个出厂工作区改名。
    # 必须**存盘** —— 算符跑完会重新打开磁盘上的这个文件，不存盘改名就白改了。
    probe_name = "hhz_startup_probe"
    previous_name = ""
    try:
        previous_name = bpy.data.workspaces[0].name
        bpy.data.workspaces[0].name = probe_name
    except Exception as error:
        skip("改不动工作区名字（{}），跳过完整流程".format(error))
        return
    check(previous_name != probe_name,
          "把一个工作区改成了启动文件里没有的新名字（幂等用例的前提）",
          (previous_name, probe_name))

    sound_name, _sound_path, note = _make_missing_sample("sound", "hhz_missing_startup", None)
    check(sound_name is not None, "启动布局用例造出了缺失样本（{}）".format(note), sound_name)
    if sound_name is not None:
        unused, in_use = module.ops._missing_file_blocks()
        scanned = [getattr(block, "name", "") for _label, _collection, block in unused]
        check(sound_name in scanned or any(item[1] == sound_name for item in in_use),
              "扫描函数确实检出了这个缺失样本（提示的判据是真的）",
              (sound_name, scanned[:4], len(in_use)))
    # 改名 / 造样本都算"有改动"，先存一次让流程能继续（等于用户先按了 Ctrl+S）
    try:
        bpy.ops.wm.save_as_mainfile(filepath=recorded_path)
    except Exception as error:
        skip("存不了测试文件（{}），跳过完整流程".format(error))
        return

    recorded_workspaces = [workspace.name for workspace in bpy.data.workspaces]
    check(len(recorded_workspaces) >= 3, "测试文件里有多个工作区可存", recorded_workspaces[:4])
    check(probe_name in recorded_workspaces,
          "记录下来的工作区里有那个新名字", recorded_workspaces[:4])

    def run_startup_layout(label):
        try:
            return bpy.ops.hhz.layout_save_startup(confirm=True)
        except Exception as error:
            return "抛出异常（{}）：{}".format(label, error)

    try:
        # ---- (b) 第一次：只补「启动文件里还没有的」 ----
        first = run_startup_layout("第一次")
        check(first == {'FINISHED'}, "第一次完整流程返回 FINISHED", first)
        check(bpy.data.filepath == recorded_path,
              "跑完之后会话文件路径与开始时一致（用户回到原文件）",
              (recorded_path, bpy.data.filepath))
        check(os.path.exists(os.path.join(config, "startup.blend")),
              "启动文件写进了隔离目录（沙箱）", config)
        check(not os.path.exists(os.path.join(config, module.ops.LAYOUT_LIBRARY_NAME)),
              "正常路径下**不写**部分库（4.5.14 上 libraries.write 工作区必崩）", config)

        result_first = dict(module.ops.LAST_STARTUP_LAYOUT_RESULT)
        startup_first, objects_first = _read_startup_file(config)
        check(probe_name in result_first.get("appended", []),
              "第一次把「启动文件里还没有的」那个工作区追加进去了",
              (result_first.get("appended"), result_first.get("skipped")))
        check(result_first.get("skipped_total", 0) >= 1,
              "第一次也没有重复追加已经存在的工作区（同名且区域构成相同 -> 跳过）",
              result_first.get("skipped"))
        check(sorted(startup_first) == sorted(result_first.get("workspaces", [])),
              "报告里的工作区清单 == 启动文件里真实的工作区清单",
              (result_first.get("workspaces"), startup_first))
        check(not objects_first,
              "启动文件是空场景（没有任何物体，场景没被带进去）", objects_first[:4])
        check(result_first.get("objects") == 0,
              "落盘前校验里物体数就是 0", result_first.get("objects"))

        # ---- (c) 第二次：幂等 —— 本次优化的验收核心 ----
        second = run_startup_layout("第二次")
        check(second == {'FINISHED'}, "第二次完整流程返回 FINISHED", second)
        result_second = dict(module.ops.LAST_STARTUP_LAYOUT_RESULT)
        startup_second, objects_second = _read_startup_file(config)
        check(result_second.get("appended_total") == 0,
              "第二次新增 0 个工作区（幂等：已存在的全部跳过）",
              result_second.get("appended"))
        check(result_second.get("skipped_total") == result_second.get("recorded"),
              "第二次把本次会话记录的工作区全都算成「已存在」",
              (result_second.get("skipped_total"), result_second.get("recorded")))
        check(sorted(startup_second) == sorted(startup_first),
              "连跑两次之后启动文件里的工作区集合**完全一致**（本次验收核心）",
              (sorted(startup_first), sorted(startup_second)))
        check(len(startup_second) == len(startup_first),
              "连跑两次工作区数量一个都没变（不再每点一次就多一批）",
              (len(startup_first), len(startup_second)))
        check(not objects_second, "第二次之后启动文件仍是空场景", objects_second[:4])

        # 用户的工作区零丢失、没有 X / X.<数字> 并存（两次都查）
        for label, names in (("第一次", startup_first), ("第二次", startup_second)):
            exact_missing = [name for name in recorded_workspaces if name not in names]
            check(not exact_missing,
                  "{}：记录下来的工作区在启动文件里都是**原名**（零丢失、无 .001）".format(label),
                  (exact_missing, names[:6]))
            startup_pairs = [name for name in names
                             if name.rpartition(".")[2].isdigit()
                             and name.rpartition(".")[0] in names]
            check(not startup_pairs,
                  "{}：启动文件里没有「X」与「X.<数字>」并存的重名对".format(label),
                  startup_pairs[:6])
        # 用户实测的坏现象：每点一次按钮就多一批「出厂 xxx」
        leftover_first = [name for name in startup_first
                          if name.startswith(module.ops.FACTORY_WORKSPACE_PREFIX)]
        leftover_second = [name for name in startup_second
                           if name.startswith(module.ops.FACTORY_WORKSPACE_PREFIX)]
        check(sorted(leftover_second) == sorted(leftover_first),
              "第二次运行没有再冒出新的「出厂 xxx」工作区（用户实测的坏现象不再发生）",
              (sorted(leftover_first), sorted(leftover_second)))

        # ---- (d) 同名但**区域构成不同** -> 按设计追加（并给旧的那份「出厂」前缀让位）----
        ops = module.ops
        previous = bpy.data.workspaces.get(probe_name)
        check(previous is not None,
              "会话已被恢复：那个测试工作区还在（会话没停在空场景上）", probe_name)
        if previous is None:
            skip("会话里找不到测试工作区，跳过同名不同构成的验证")
        else:
            probe_composition = ops._workspace_composition(previous)
            same_composition = [workspace for workspace in bpy.data.workspaces
                                if workspace is not previous
                                and ops._workspace_composition(workspace) == probe_composition]
            other_composition = [workspace for workspace in bpy.data.workspaces
                                 if workspace is not previous
                                 and ops._workspace_composition(workspace) != probe_composition]
            check(bool(other_composition),
                  "会话里找得到「同名但区域构成不同」的候选工作区（判据可测）",
                  (len(same_composition), len(other_composition)))
            if not other_composition:
                skip("当前文件里找不到区域构成不同的第二个工作区，跳过这一条")
            else:
                try:
                    # 让出 probe_name，再把一个**构成不同**的工作区改成 probe_name
                    previous.name = "hhz_startup_old"
                    other_composition[0].name = probe_name
                    bpy.ops.wm.save_as_mainfile(filepath=recorded_path)
                except Exception as error:
                    skip("造不出「同名不同构成」场景（{}），跳过这一条".format(error))
                else:
                    third = run_startup_layout("第三次")
                    check(third == {'FINISHED'}, "同名不同构成时流程仍然 FINISHED", third)
                    result_third = dict(module.ops.LAST_STARTUP_LAYOUT_RESULT)
                    startup_third, objects_third = _read_startup_file(config)
                    check(probe_name in result_third.get("appended", []),
                          "同名但区域构成不同 -> **追加**（没有被误判成已存在而跳过）",
                          (result_third.get("appended"), result_third.get("skipped")[:4]))
                    check(probe_name in startup_third,
                          "追加之后那个名字在启动文件里（重名收尾把原名还回来了）",
                          startup_third[:8])
                    check(module.ops.FACTORY_WORKSPACE_PREFIX + probe_name in startup_third,
                          "启动文件里原来那份同名工作区被加「出厂」前缀让位",
                          startup_third[:8])
                    pairs_third = [name for name in startup_third
                                   if name.rpartition(".")[2].isdigit()
                                   and name.rpartition(".")[0] in startup_third]
                    check(not pairs_third,
                          "同名不同构成追加之后也没有 X / X.<数字> 并存", pairs_third[:4])
                    check(not objects_third, "第三次之后启动文件仍是空场景", objects_third[:4])

                    # ---- (e) 切文件之后出错 -> 会话自动回到用户原来的文件 ----
                    real_plan = ops._plan_workspace_appends

                    def _explode(*_args, **_kwargs):
                        raise RuntimeError("hhz 自愈测试：故意让追加步骤失败")

                    ops._plan_workspace_appends = _explode
                    try:
                        failed = run_startup_layout("自愈")
                    finally:
                        ops._plan_workspace_appends = real_plan
                    # report({'ERROR'}) 在 bpy.ops 里会**抛** RuntimeException，
                    # 所以"失败"有两种表现：抛出异常（带失败原因）或返回 CANCELLED。
                    check("追加工作区失败" in str(failed) or failed == {'CANCELLED'},
                          "切文件之后出错时明确失败（抛错 / CANCELLED），没有假装成功",
                          failed)
                    check(bpy.data.filepath == recorded_path,
                          "出错之后会话自动回到了用户原来的文件（自愈没被破坏）",
                          (recorded_path, bpy.data.filepath))
                    check(bpy.data.workspaces.get(probe_name) is not None,
                          "恢复回来的会话里有用户自己的工作区（不是空场景）", probe_name)
                    startup_after_failure, _objects_after = _read_startup_file(config)
                    check(sorted(startup_after_failure) == sorted(startup_third),
                          "失败的那次没有写启动文件（磁盘上还是上一次的结果）",
                          (sorted(startup_third), sorted(startup_after_failure)))

        # ---- 重名收尾：在沙箱会话里直接对助手函数做一次端到端验证 ----
        ops = module.ops
        check(not ops._workspace_collision_pairs(),
              "收尾之后当前会话里没有残留的重名对",
              [item[0].name for item in ops._workspace_collision_pairs()][:4])

        # 无头下删除算符不可用：助手必须**如实**返回"没删掉"，不能假装成功
        victim = bpy.data.workspaces[0]
        count_before = len(bpy.data.workspaces)
        removed_live = ops._delete_workspace(victim)
        check(removed_live is False,
              "无头下 _delete_workspace 如实返回 False（本版 Blender 脚本删不掉工作区）",
              removed_live)
        check(victim in list(bpy.data.workspaces)
              and len(bpy.data.workspaces) == count_before,
              "调用之后那个工作区仍在、数量没变（没有偷偷删掉别的工作区）",
              (victim.name, len(bpy.data.workspaces)))

        # 造一对重名，跑一遍收尾：用户那份拿回原名、原有工作区一个不少
        try:
            duplicated = bpy.ops.workspace.duplicate()
        except Exception as error:
            duplicated = "抛出异常：{}".format(error)
        if duplicated != {'FINISHED'}:
            skip("当前会话调不了工作区复制算符（{}），跳过重名收尾的动态验证".format(
                duplicated))
        else:
            pairs = ops._workspace_collision_pairs()
            check(len(pairs) >= 1,
                  "造出 .001 重名之后 _workspace_collision_pairs() 能认出来（{} 对）".format(
                      len(pairs)),
                  [item[0].name for item in pairs][:4])
            before_objects = [workspace for workspace in bpy.data.workspaces]
            removed_dynamic, renamed_dynamic, left, problems = (
                ops._dedupe_appended_workspaces())
            live = list(bpy.data.workspaces)
            check(not left, "收尾之后不再有 X / X.<数字> 重名对",
                  [item[0].name for item in left][:4])
            check(renamed_dynamic >= 1, "至少有一个工作区改名回了原名", renamed_dynamic)
            # 按**对象**核对（不能按名字：收尾本来就要把 .001 改成原名）
            vanished = [workspace.name for workspace in before_objects
                        if not any(item is workspace for item in live)]
            check(not vanished,
                  "原有的工作区一个都没丢（按对象核对，改名不算丢）", vanished[:4])
            check(len(live) == len(before_objects) - removed_dynamic,
                  "收尾只做改名 / 删除，数量账对得上",
                  (len(before_objects), len(live), removed_dynamic))
            check(not problems or removed_dynamic or renamed_dynamic,
                  "收尾要么成功要么如实报问题（不静默）", problems[:3])
    finally:
        _purge_test_samples(module)
        try:
            bpy.ops.wm.open_mainfile(filepath=sandbox_file)
        except Exception:
            pass


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
    # ---- 区域配对：必须按几何位置配，不能按列表顺序 zip ----
    # 真实事故：应用保存的布局后各功能区乱窜 —— screen.areas 的内部顺序与"按位置
    # 排序"保存的顺序并不一致，按顺序 zip 会把编辑器类型安到错误的区域上。
    # 这里用纯数据复现，不依赖真实 UI 分区。
    saved_areas = [
        {"type": "VIEW_3D", "x": 0.0, "y": 0.0, "w": 0.75, "h": 1.0},
        {"type": "OUTLINER", "x": 0.75, "y": 0.0, "w": 0.25, "h": 1.0},
    ]
    # 当前区域顺序与保存顺序相反：左边的这一格才是 VIEW_3D
    reversed_boxes = [
        {"x": 0.75, "y": 0.0, "w": 0.25, "h": 1.0},
        {"x": 0.0, "y": 0.0, "w": 0.75, "h": 1.0},
    ]
    pairs = layout.match_areas(saved_areas, reversed_boxes)
    check(len(pairs) == 2, "每个当前区域都配到一个保存项", pairs)
    check(all(0 <= index < len(reversed_boxes) for index, _saved in pairs),
          "配对结果给的是当前区域的下标",
          [(index, saved["type"]) for index, saved in pairs])
    check(len({id(saved) for _index, saved in pairs}) == len(pairs),
          "同一个保存项不会被用两次")
    matched = {index: saved["type"] for index, saved in pairs}
    check(matched.get(0) == "OUTLINER" and matched.get(1) == "VIEW_3D",
          "当前区域顺序与保存顺序相反时仍按位置配对正确",
          [(index, saved["type"]) for index, saved in pairs])
    check(len(layout.match_areas([], reversed_boxes)) == 0,
          "没有保存项时配对为空（不抛异常）")
    check(len(layout.match_areas(saved_areas, [])) == 0,
          "没有当前区域时配对为空（不抛异常）")

    # 四宫格：保存顺序与坐标顺序完全错开，当前区域又是另一种顺序
    grid_saved = [
        {"type": "D", "x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5},
        {"type": "C", "x": 0.0, "y": 0.5, "w": 0.5, "h": 0.5},
        {"type": "B", "x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5},
        {"type": "A", "x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5},
    ]
    grid_boxes = [
        {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5},   # 左下 -> A
        {"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5},   # 右上 -> D
        {"x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5},   # 右下 -> B
        {"x": 0.0, "y": 0.5, "w": 0.5, "h": 0.5},   # 左上 -> C
    ]
    grid_pairs = layout.match_areas(grid_saved, grid_boxes)
    grid_matched = {index: saved["type"] for index, saved in grid_pairs}
    check(grid_matched == {0: "A", 1: "D", 2: "B", 3: "C"},
          "四宫格里每个当前区域都配到位置上对应的保存项", grid_matched)
    check(len({id(saved) for _index, saved in grid_pairs}) == 4,
          "四宫格里四个保存项各用了一次")

    # 当前区域比保存项多：只能配出保存项那么多，且绝不重复用一个
    extra_boxes = grid_boxes + [{"x": 0.0, "y": 0.0, "w": 0.25, "h": 0.25}]
    crowded = layout.match_areas(grid_saved, extra_boxes)
    check(len(crowded) == len(grid_saved), "当前区域更多时配对数量以保存项为准", len(crowded))
    check(len({id(saved) for _index, saved in crowded}) == len(crowded),
          "多出来的当前区域不会让保存项被重复使用")



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
    run("检查并修复缺失文件", test_fix_missing_files, module)
    run("保存到偏好设置", test_save_preferences, module)
    run("一键楼梯（相对偏移阵列）", test_stairs, module)
    run("主题颜色（N 面板）", test_theme, module)
    run("颜色项下拉标签（缩短）", test_target_item_labels, module)
    run("预设：标签独立型家族", test_preset_label_families, module)
    run("预设：死亡搁浅", test_preset_death_stranding, module)
    run("预设：视口辅助元素", test_preset_viewport_helpers, module)
    run("预设：深档强调的三个家族", test_preset_deep_accent_families, module)
    run("预设：视口选中几何用鲜亮档", test_viewport_selection_accent, module)
    run("饼菜单内容未被改动", test_pie_unchanged, module)
    run("分页（建模 / 工具 / 主题）", test_pages, module)
    run("页面布局（保存 / 使用）", test_layout, module)
    run("N 面板顺序（拖动排序）", test_panel_order, module)
    run("算符 poll", test_poll)
    # 放在最后：它真的会重建启动文件并切换文件（read_homefile + append + open_mainfile），
    # 会清掉当前场景，所以后面只剩"卸载清理"这种不吃场景数据的用例
    run("把当前窗口布局设为启动布局", test_startup_layout, module)
    run("卸载清理", test_unregister, module_name, module)

    print("\n================ 结果 ================")
    print("Blender {}：{} 项断言，{} 项失败".format(
        bpy.app.version_string, CHECK_COUNT[0], len(FAILURES)))
    for failure in FAILURES:
        print("  - {}".format(failure))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
