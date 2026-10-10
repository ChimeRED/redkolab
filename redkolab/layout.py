# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab 页面布局

把当前 Blender 的**工作区 + 区域划分**（哪个编辑器在哪、占多大）存成一份带名字的快照，
以后在新文件里一键还原。

保存的内容（纯读取，不动界面）：

* 每个工作区的名字，以及它的区域列表：编辑器类型（``area.type``）+ 相对位置与相对大小；
* 保存时的活动工作区。

还原时：

* 缺的工作区用 ``bpy.ops.workspace.add()`` 建出来并改名；
* 当前窗口里按位置一一对应，把区域类型改回去 —— 只改能对上的；区域数量或分割比例
  不一致时**不硬拆硬合**（那属于高风险操作，容易把界面搞乱），会如实报告对上了几个。

另外提供「保存为启动布局」：调 Blender 原生的 ``wm.save_homefile()``，
这样**新建文件**时直接就是这套布局 —— 那才是最省事的"新场景一键使用"。
"""

import json

import bpy


# ------------------------------------------------------- 存到偏好设置（JSON）


def dump(layouts):
    """序列化布局表（存进偏好设置）。"""
    return json.dumps(layouts, ensure_ascii=False, sort_keys=True)


def load(text):
    """解析偏好设置里的布局表。"""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for name, layout in data.items():
        if isinstance(layout, dict) and layout.get("workspaces"):
            result[str(name)] = layout
    return result


def names(text):
    return sorted(load(text).keys())


def save(text, name, layout):
    """保存一份布局（同名覆盖），返回 ``(新文本, 是否保存成功)``。"""
    name = (name or "").strip()
    if not name or not layout:
        return text, False
    items = load(text)
    items[name] = layout
    return dump(items), True


def delete(text, name):
    items = load(text)
    if name not in items:
        return text, False
    del items[name]
    return dump(items), True


def _screen_size(screen):
    """屏幕尺寸：用所有区域的外接矩形推出来。"""
    width = 1
    height = 1
    for area in screen.areas:
        width = max(width, area.x + area.width)
        height = max(height, area.y + area.height)
    return width, height


def _capture_areas(workspace):
    """读某个工作区的区域列表（类型 + 相对几何）。"""
    screens = list(getattr(workspace, "screens", ()) or ())
    if not screens:
        return []
    screen = screens[0]
    width, height = _screen_size(screen)
    areas = []
    for area in screen.areas:
        areas.append({
            "type": area.type,
            "x": round(area.x / width, 4),
            "y": round(area.y / height, 4),
            "w": round(area.width / width, 4),
            "h": round(area.height / height, 4),
        })
    areas.sort(key=lambda item: (-item["y"], item["x"]))
    return areas


def capture(context):
    """读当前文件的工作区与区域划分；没有窗口（后台模式）返回 None。"""
    window = getattr(context, "window", None)
    if window is None:
        return None
    workspaces = []
    for workspace in bpy.data.workspaces:
        workspaces.append({
            "name": workspace.name,
            "areas": _capture_areas(workspace),
        })
    return {
        "active": getattr(getattr(window, "workspace", None), "name", ""),
        "workspaces": workspaces,
    }


def _find_workspace(name):
    if not name:
        return None
    return bpy.data.workspaces.get(name)


def apply(context, layout):
    """把布局应用到当前文件，返回 ``(说明, 是否成功)``。"""
    window = getattr(context, "window", None)
    if window is None:
        return "后台模式下没有窗口，布局只能在前台用", False

    workspaces = list(layout.get("workspaces") or ())
    if not workspaces:
        return "这份布局里没有工作区信息", False

    created = 0
    for item in workspaces:
        name = item.get("name") or ""
        if not name or _find_workspace(name) is not None:
            continue
        try:
            bpy.ops.workspace.add()
        except Exception:
            continue
        current = getattr(window, "workspace", None)
        if current is not None:
            try:
                current.name = name
                created += 1
            except Exception:
                pass

    current = getattr(window, "workspace", None)
    target = None
    for item in workspaces:
        if item.get("name") == getattr(current, "name", ""):
            target = item
            break

    applied = 0
    if target is not None and current is not None:
        applied = _apply_areas(current, list(target.get("areas") or ()))

    wanted = _find_workspace(layout.get("active") or "")
    if wanted is not None:
        try:
            window.workspace = wanted
        except Exception:
            pass

    return "已还原：补了 {} 个工作区，当前工作区改了 {} 个区域".format(created, applied), True


def match_areas(saved_areas, current_boxes):
    """把当前区域与保存的区域按**几何位置**配对，返回 [(当前下标, 保存项), ...]。

    不能按列表顺序配对：Blender 内部的 screen.areas 顺序与"按位置排序"保存的
    顺序并不一致，按顺序 zip 会把编辑器类型安到错误的区域上，界面上看起来就是
    各个功能区乱窜。这里用中心点最近 + 每个保存项只用一次（贪心）。
    """
    pairs = []
    used = set()
    for index, box in enumerate(current_boxes):
        centre_x = box["x"] + box["w"] / 2.0
        centre_y = box["y"] + box["h"] / 2.0
        best_order = None
        best_distance = None
        for order, saved in enumerate(saved_areas):
            if order in used:
                continue
            distance = ((centre_x - (saved["x"] + saved["w"] / 2.0)) ** 2
                        + (centre_y - (saved["y"] + saved["h"] / 2.0)) ** 2)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_order = order
        if best_order is None:
            continue
        used.add(best_order)
        pairs.append((index, saved_areas[best_order]))
    return pairs


def _apply_areas(workspace, saved_areas):
    """按位置把区域类型改回去，返回成功改动的数量。"""
    screens = list(getattr(workspace, "screens", ()) or ())
    if not screens or not saved_areas:
        return 0
    screen = screens[0]
    width, height = _screen_size(screen)
    areas = list(screen.areas)
    boxes = [{"x": area.x / width, "y": area.y / height,
              "w": area.width / width, "h": area.height / height} for area in areas]

    changed = 0
    for index, saved in match_areas(saved_areas, boxes):
        area_type = saved.get("type")
        if not area_type or areas[index].type == area_type:
            continue
        try:
            areas[index].type = area_type
            changed += 1
        except Exception:
            continue
    return changed


def save_startup(context):
    """把当前布局存成 Blender 启动文件（新建文件时会用它）。"""
    try:
        bpy.ops.wm.save_homefile()
    except Exception as error:
        return "保存启动文件失败：{}".format(error), False
    return "已存成启动布局，以后新建文件就是这个界面", True
