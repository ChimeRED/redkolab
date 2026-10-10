#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""打包红红子lab (REDKOlab) —— 一份包通吃 Blender 4.0 ~ 最新版。

默认产出 dist/redkolab-<版本>.zip，结构只有一种，源码只有一份：

    redkolab/
        __init__.py          <- 同时带 bl_info（传统插件元数据）
        compat.py
        ops.py
        ui.py
        blender_manifest.toml <- 扩展清单（4.2+）

为什么是「清单放进目录里」这一种布局（实测结论）：

    | 布局                      | 4.0/4.1 插件页 | 4.2+ 插件页 | 4.2+ 扩展页 |
    | 根目录代码 + 目录副本      | 可以           | 报错        | 可以        |
    | 只有目录（本布局）         | 可以           | 可以        | 可以        |

4.2+ 的「插件页安装」会明确拒绝根目录带 __init__.py 的包
（ZIP packaged incorrectly; __init__.py should be in a directory），
而扩展安装器既认根目录清单、也认单层目录里的清单，所以把代码和清单
一起放进 redkolab/ 目录是唯一四个入口全通的布局。

    python tools/build_zips.py                    # 打正式包
    python tools/build_zips.py --variant all      # 打出所有候选布局（对比测试用）
    python tools/build_zips.py --variant hybrid   # 指定布局（调试用）
"""

import ast
import os
import re
import sys
import zipfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_NAME = "redkolab"
PACKAGE_DIR = os.path.join(PROJECT_ROOT, PACKAGE_NAME)
MANIFEST_PATH = os.path.join(PACKAGE_DIR, "blender_manifest.toml")
DIST_DIR = os.path.join(PROJECT_ROOT, "dist")

SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".blend1", ".zip")

#: 正式布局：代码 + 扩展清单都放在 redkolab/ 目录里（四个安装入口全通）
VARIANT_NESTED = "nested"
#: 根目录放代码和清单，外加一份目录副本（4.2+ 的插件页安装会拒绝）
VARIANT_HYBRID = "hybrid"
#: 只放同名目录（仅传统插件）
VARIANT_LEGACY = "legacy"
#: 只放根目录（仅扩展）
VARIANT_EXTENSION = "extension"

DEFAULT_VARIANT = VARIANT_NESTED
VARIANTS = (VARIANT_NESTED, VARIANT_HYBRID, VARIANT_LEGACY, VARIANT_EXTENSION)


def read_bl_info():
    """从 __init__.py 静态解析出 bl_info 与源码。"""
    source_path = os.path.join(PACKAGE_DIR, "__init__.py")
    with open(source_path, encoding="utf-8") as handle:
        source = handle.read()

    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "bl_info":
                    return ast.literal_eval(node.value), source
    raise SystemExit("在 {} 里找不到 bl_info".format(source_path))


def iter_package_files():
    """产出 (绝对路径, 包内相对路径)，相对路径用 / 分隔。"""
    for root, dirs, files in os.walk(PACKAGE_DIR):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(SKIP_SUFFIXES):
                continue
            full_path = os.path.join(root, name)
            relative = os.path.relpath(full_path, PACKAGE_DIR).replace(os.sep, "/")
            yield full_path, relative


def read_manifest(version):
    with open(MANIFEST_PATH, encoding="utf-8") as handle:
        text = handle.read()
    # 版本号以 bl_info 为准，避免两个文件各写一份
    text, count = re.subn(
        r'(?m)^version\s*=\s*"[^"]*"',
        'version = "{}"'.format(version),
        text,
        count=1,
    )
    if count != 1:
        raise SystemExit("{} 里没有找到 version 字段".format(MANIFEST_PATH))
    return text


def collect_entries(variant, source, manifest):
    """返回 [(写入 zip 的路径, bytes 内容 或 源文件绝对路径)]。

    __init__.py 和 blender_manifest.toml 用生成的内容（版本号要同步），
    其余文件直接拷源文件。
    """
    package_files = list(iter_package_files())
    entries = []

    def content_for(relative):
        if relative == "__init__.py":
            return source.encode("utf-8")
        if relative == "blender_manifest.toml":
            return manifest.encode("utf-8")
        return None

    def add_root_layout():
        # 根目录布局：代码和清单都放根目录
        for full_path, relative in package_files:
            if relative == "blender_manifest.toml":
                continue
            content = content_for(relative)
            entries.append((relative, content if content is not None else full_path))
        entries.append(("blender_manifest.toml", manifest.encode("utf-8")))

    def add_folder_layout(with_manifest=True):
        prefix = PACKAGE_NAME + "/"
        for full_path, relative in package_files:
            if relative == "blender_manifest.toml" and not with_manifest:
                continue
            content = content_for(relative)
            entries.append((prefix + relative, content if content is not None else full_path))

    if variant == VARIANT_NESTED:
        add_folder_layout(with_manifest=True)
    elif variant == VARIANT_HYBRID:
        add_root_layout()
        add_folder_layout(with_manifest=False)
    elif variant == VARIANT_EXTENSION:
        add_root_layout()
    elif variant == VARIANT_LEGACY:
        add_folder_layout(with_manifest=False)
    else:
        raise SystemExit("未知布局：{}".format(variant))
    return entries


def build_zip(version, variant, source, manifest, target=None):
    if target is None:
        target = os.path.join(DIST_DIR, "{}-{}.zip".format(PACKAGE_NAME, version))
    entries = collect_entries(variant, source, manifest)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for archive_name, content in entries:
            if isinstance(content, bytes):
                archive.writestr(archive_name, content)
            else:
                archive.write(content, archive_name)
    return target


def main(argv):
    variant = DEFAULT_VARIANT
    if "--variant" in argv:
        index = argv.index("--variant")
        if index + 1 >= len(argv):
            raise SystemExit("--variant 后面要跟布局名：{}".format(", ".join(VARIANTS)))
        variant = argv[index + 1]
        if variant not in VARIANTS + ("all",):
            raise SystemExit("未知布局 {}，可选：{}、all".format(variant, "、".join(VARIANTS)))

    bl_info, source = read_bl_info()
    version = ".".join(str(part) for part in bl_info["version"])
    manifest = read_manifest(version)

    if not os.path.isdir(DIST_DIR):
        os.makedirs(DIST_DIR)

    print("插件名：{}  版本：{}  模块/id：{}".format(bl_info["name"], version, PACKAGE_NAME))

    if variant == "all":
        for name in VARIANTS:
            target = os.path.join(DIST_DIR, "{}-{}-{}.zip".format(PACKAGE_NAME, version, name))
            path = build_zip(version, name, source, manifest, target)
            print("  {:<44} {:>7.1f} KB".format(os.path.basename(path), os.path.getsize(path) / 1024.0))
        return 0

    target = None
    if variant != DEFAULT_VARIANT:
        # 显式指定了别的布局时加后缀，避免覆盖正式包
        target = os.path.join(DIST_DIR, "{}-{}-{}.zip".format(PACKAGE_NAME, version, variant))
    path = build_zip(version, variant, source, manifest, target)
    print("  布局：{}".format(variant))
    print("  {:<44} {:>7.1f} KB".format(os.path.basename(path), os.path.getsize(path) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
