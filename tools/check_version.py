#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""版本号一致性检查（打包 / 发布前用）。

    python tools/check_version.py            # 打印当前版本
    python tools/check_version.py v1.2.0     # 校验 tag 与源码版本是否一致

发布流程里由 .github/workflows/release.yml 调用：tag 打错了、清单忘了同步，
都会在这里失败，绝不会把版本号对不上的包发出去。
"""

import ast
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_NAME = "redkolab"
PACKAGE_DIR = os.path.join(PROJECT_ROOT, PACKAGE_NAME)
MANIFEST_PATH = os.path.join(PACKAGE_DIR, "blender_manifest.toml")


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def read_bl_info():
    """静态解析 __init__.py 里的 bl_info（不 import，避免需要 bpy）。"""
    source = read_text(os.path.join(PACKAGE_DIR, "__init__.py"))
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "bl_info":
                    return ast.literal_eval(node.value)
    raise SystemExit("__init__.py 里找不到 bl_info")


def read_manifest_field(name):
    match = re.search(r'(?m)^{}\s*=\s*"([^"]*)"'.format(re.escape(name)), read_text(MANIFEST_PATH))
    return match.group(1) if match else None


def main(argv):
    problems = []

    bl_info = read_bl_info()
    version = ".".join(str(part) for part in bl_info["version"])
    manifest_version = read_manifest_field("version")
    manifest_id = read_manifest_field("id")

    if manifest_version != version:
        problems.append(
            "版本号不同步：bl_info={} 但 blender_manifest.toml={}".format(version, manifest_version))
    if manifest_id != PACKAGE_NAME:
        problems.append(
            "扩展 id 与目录名不一致：id={} 目录={}".format(manifest_id, PACKAGE_NAME))

    tag = argv[0].strip() if argv else None
    if tag:
        tag_version = tag[1:] if tag[:1] in ("v", "V") else tag
        if tag_version != version:
            problems.append("tag「{}」与源码版本「{}」不一致".format(tag, version))

    print("插件版本：{}（id={}）".format(version, PACKAGE_NAME))
    if tag:
        print("tag：{}".format(tag))

    if problems:
        for problem in problems:
            print("  [FAIL] {}".format(problem))
        return 1

    print("  [ ok ] 版本号 / id / tag 全部一致")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
