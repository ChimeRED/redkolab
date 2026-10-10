#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""主题颜色探针：确认候选属性路径在某个 Blender 版本上是否存在、是几位颜色。

Blender 各版本的主题结构会变（属性改名、增删），所以新增「主题颜色」功能前
先用它把候选路径跑一遍，只收录各版本都存在的：

    blender --background --factory-startup --python tools/theme_probe.py
"""

import bpy

#: 候选路径：功能里想收录的主题颜色
CANDIDATES = [
    # 3D 视图
    "view_3d.space.gradients.high_gradient",
    "view_3d.space.gradients.gradient",
    "view_3d.grid",
    "view_3d.wire",
    "view_3d.vertex",
    "view_3d.active",
    "view_3d.select",
    "view_3d.object_selected",
    "view_3d.object_active",
    "view_3d.camera",
    "view_3d.empty",
    "view_3d.light",
    "view_3d.bone_pose",
    "view_3d.text",
    "view_3d.clipping_border",
    # 界面控件
    "user_interface.wcol_regular.inner",
    "user_interface.wcol_regular.outline",
    "user_interface.wcol_regular.text",
    "user_interface.wcol_regular.item",
    "user_interface.wcol_menu.inner",
    "user_interface.wcol_menu.outline",
    "user_interface.wcol_menu.text",
    "user_interface.wcol_menu_back.inner",
    "user_interface.wcol_menu_back.outline",
    "user_interface.wcol_tool.inner",
    "user_interface.wcol_tool.outline",
    "user_interface.wcol_tool.text",
    "user_interface.wcol_box.inner",
    "user_interface.wcol_box.outline",
    "user_interface.wcol_pie_menu.inner",
    "user_interface.wcol_pie_menu.outline",
    "user_interface.wcol_state.inner_sel",
    "user_interface.wcol_state.inner_changed",
    "user_interface.panel_back",
    "user_interface.panel_header",
    "user_interface.panel_text",
    "user_interface.panel_title",
    "user_interface.panel_outline",
    "user_interface.widget_text",
    "user_interface.widget_emboss",
    "user_interface.gradient_high",
    "user_interface.gradient_low",
    "user_interface.icon_scene",
    # 节点编辑器
    "node_editor.grid",
    "node_editor.node_backdrop",
    "node_editor.wire",
    "node_editor.wire_select",
    "node_editor.node_selected",
    "node_editor.surface",
    # 时间线 / 其它
    "timeline.space",
    "timeline.grid",
    "graph_editor.grid",
    "graph_editor.vertex",
    "dopesheet_editor.grid",
    "sequencer.space",
    "console.space",
    "text_editor.space",
    "info.space",
    "regions.region_bg",
    "regions.scroll_bar",
]


def resolve(theme, path):
    """按点分路径取 (宿主结构, 属性名)；任一段不存在就返回 None。"""
    parts = path.split(".")
    owner = theme
    for part in parts[:-1]:
        owner = getattr(owner, part, None)
        if owner is None:
            return None
    name = parts[-1]
    if not hasattr(owner, name):
        return None
    return owner, name


def main():
    themes = bpy.context.preferences.themes
    print("Blender {} | 主题数量 {}".format(bpy.app.version_string, len(themes)))
    theme = themes[0]

    exists = []
    missing = []
    for path in CANDIDATES:
        resolved = resolve(theme, path)
        if resolved is None:
            missing.append(path)
            continue
        owner, name = resolved
        prop = owner.bl_rna.properties.get(name)
        length = getattr(prop, "array_length", 0) if prop is not None else 0
        if prop is None or prop.type != 'FLOAT' or length not in (3, 4):
            # 路径存在，但指向的不是颜色（例如 *_editor.space 是子结构），当作不可用
            missing.append(path)
            continue
        value = getattr(owner, name)
        subtype = getattr(prop, "subtype", "?")
        exists.append(path)
        print("  OK    {:<48} len={} subtype={:<12} value={}".format(
            path, length, subtype, tuple(round(float(v), 4) for v in value)))

    print("\n存在 {} 项，缺失 {} 项".format(len(exists), len(missing)))
    for path in missing:
        print("  MISS  {}".format(path))


if __name__ == "__main__":
    main()
