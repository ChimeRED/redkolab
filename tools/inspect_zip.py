#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""安装包结构自检 —— 发布前的守门人。

这个脚本把开发过程中真实踩过的坑固化成了检查项，任何一条不过就说明包不能发：

1. 布局：必须是 `redkolab/` 单层目录 —— 4.0/4.1 的传统安装器和 4.2+ 的扩展安装器
   都认这一种（根目录放 __init__.py 会被 4.2+ 的插件页安装直接拒绝）
2. 干净：不能夹带 __pycache__ / .pyc
3. 元数据：bl_info 与 blender_manifest.toml 的名字、版本、id、最低版本必须一致
4. 属性写法：ui.py 里的 bpy.props 必须是注解式 —— 赋值式在 4.0+ 上根本不生成 RNA 属性
5. 上下文覆盖：不允许把字典当第一个位置参数传给 bpy.ops —— 4.0 已移除该写法
6. 兼容层：compat.py 必须真的用了 bpy.context.temp_override

    python tools/inspect_zip.py dist/redkolab-1.1.0.zip
"""

import ast
import os
import re
import sys
import zipfile

PACKAGE_NAME = "redkolab"
MIN_BLENDER_VERSION = (4, 0, 0)

EXPECTED_ENTRIES = {
    "{}/__init__.py".format(PACKAGE_NAME),
    "{}/blender_manifest.toml".format(PACKAGE_NAME),
    "{}/compat.py".format(PACKAGE_NAME),
    "{}/ops.py".format(PACKAGE_NAME),
    "{}/pages.py".format(PACKAGE_NAME),
    "{}/panels.py".format(PACKAGE_NAME),
    "{}/stairs.py".format(PACKAGE_NAME),
    "{}/theme.py".format(PACKAGE_NAME),
    "{}/ui.py".format(PACKAGE_NAME),
}

def find_assign_style_properties(source):
    """找出赋值式 bpy.props 属性（4.0+ 上根本不生成 RNA 属性）。

    用 AST 判断而不是正则：文档字符串里提到这种写法不该被误判。
    注解式是 ast.AnnAssign，赋值式是 ast.Assign。

    例外：``bpy.types.WindowManager.foo = bpy.props.EnumProperty(...)`` 这种
    **挂在 bpy.types 上**的写法是官方支持的（不需要也不能用注解），
    所以只看目标是普通名字的赋值（也就是类体里那一批）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return ["语法错误：{}".format(error)]
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if not (name and name.endswith("Property")):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                continue
            offenders.append("行 {} 的 {}".format(node.lineno, name))
    return offenders


def find_legacy_context_calls(source):
    """找出把覆盖字典当第一个位置参数传给 bpy.ops 的调用（4.0+ 已移除）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return ["语法错误：{}".format(error)]
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        target = func.value
        if not (isinstance(target, ast.Attribute) and target.attr == "ops"):
            continue
        if isinstance(node.args[0], ast.Dict):
            offenders.append("行 {}".format(node.lineno))
    return offenders


def read_manifest_field(text, name):
    match = re.search(r'(?m)^{}\s*=\s*"([^"]*)"'.format(re.escape(name)), text)
    return match.group(1) if match else None


def read_bl_info(source):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "bl_info":
                    return ast.literal_eval(node.value)
    return None


def inspect(path):
    """检查一个 zip，返回问题列表。"""
    problems = []
    if not os.path.exists(path):
        return ["找不到文件：{}".format(path)]

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        contents = {}
        for name in names:
            if name.endswith((".py", ".toml")):
                contents[name] = archive.read(name).decode("utf-8")

    print("检查 {}（{} 项）".format(os.path.basename(path), len(names)))

    # 1. 布局
    missing = EXPECTED_ENTRIES - names
    extra = names - EXPECTED_ENTRIES
    if missing:
        problems.append("缺少文件：{}".format(", ".join(sorted(missing))))
    if extra:
        problems.append("多出文件（布局应为单层 {} 目录）：{}".format(
            PACKAGE_NAME, ", ".join(sorted(extra))))

    # 2. 干净
    dirty = [name for name in names if "__pycache__" in name or name.endswith((".pyc", ".pyo"))]
    if dirty:
        problems.append("夹带了缓存文件：{}".format(", ".join(sorted(dirty))))

    init_source = contents.get("{}/__init__.py".format(PACKAGE_NAME), "")
    compat_source = contents.get("{}/compat.py".format(PACKAGE_NAME), "")
    manifest = contents.get("{}/blender_manifest.toml".format(PACKAGE_NAME), "")

    # 3. 元数据一致
    bl_info = read_bl_info(init_source) if init_source else None
    if bl_info is None:
        problems.append("__init__.py 里找不到 bl_info（4.0/4.1 靠它识别插件）")
    else:
        version = ".".join(str(part) for part in bl_info.get("version", ()))
        manifest_version = read_manifest_field(manifest, "version")
        manifest_id = read_manifest_field(manifest, "id")
        min_version = read_manifest_field(manifest, "blender_version_min")

        if tuple(bl_info.get("blender", ())) != MIN_BLENDER_VERSION:
            problems.append("bl_info['blender'] 应为 {}，实际 {}".format(
                MIN_BLENDER_VERSION, bl_info.get("blender")))
        if min_version != ".".join(str(part) for part in MIN_BLENDER_VERSION):
            problems.append("清单 blender_version_min 应为 {}，实际 {}".format(
                ".".join(str(part) for part in MIN_BLENDER_VERSION), min_version))
        if manifest_version != version:
            problems.append("版本号不同步：bl_info={} 清单={}".format(version, manifest_version))
        if manifest_id != PACKAGE_NAME:
            problems.append("清单 id 应为 {}，实际 {}".format(PACKAGE_NAME, manifest_id))
        name = bl_info.get("name", "")
        if "红红子lab" not in name or "REDKO_LAB" not in name:
            problems.append("插件名应同时含中文名与英文名（REDKO_LAB），实际「{}」".format(name))
        if read_manifest_field(manifest, "name") != name:
            problems.append("bl_info 与清单的 name 不一致")

    # 4 + 5. 源码级检查：包内所有 .py 都扫一遍（文档字符串里提到这些写法不算违规）
    for entry_name, source in sorted(contents.items()):
        if not entry_name.endswith(".py"):
            continue
        short = entry_name.split("/")[-1]
        offenders = find_assign_style_properties(source)
        if offenders:
            problems.append("{} 用了赋值式 bpy.props 属性（4.0+ 不生效）：{}".format(
                short, offenders[:3]))
        legacy = find_legacy_context_calls(source)
        if legacy:
            problems.append("{} 还在把覆盖字典当第一个位置参数传给 bpy.ops（4.0+ 已移除）：{}".format(
                short, legacy[:3]))

    # 6. 兼容层确实走 temp_override
    if "temp_override" not in compat_source:
        problems.append("compat.py 没有使用 bpy.context.temp_override")
    if 'ADDON_ID = "{}"'.format(PACKAGE_NAME) not in compat_source:
        problems.append("compat.py 的 ADDON_ID 不是 {}".format(PACKAGE_NAME))

    for problem in problems:
        print("  [FAIL] {}".format(problem))
    if not problems:
        print("  [ ok ] 结构、元数据、属性写法、上下文覆盖全部通过")
    return problems


def main(argv):
    if not argv:
        print("用法：python tools/inspect_zip.py <安装包.zip> [更多 zip...]")
        return 2
    failures = []
    for path in argv:
        failures += inspect(path)
    if failures:
        print("\n共 {} 个问题，这个包不能发布".format(len(failures)))
        return 1
    print("\n可以发布")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
