# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab · 一键楼梯

把选中的体块整成台阶，再加一个**相对偏移阵列**（Array 修改器）复制出楼梯：

* 高度（Z）= ``0.2`` m，宽度（X）= ``0.3`` m，长度（Y）**保持不变**
* 相对偏移系数默认 ``(1, 0, -1)`` —— Blender 的相对偏移是「系数 × 该轴尺寸」，
  所以正好每级往 ``+X`` 挪 0.3 m、往下 0.2 m
* 鼠标移动方向换系数：右 ``(1, 0, -1)`` / 左 ``(-1, 0, -1)`` /
  上 ``(0, 1, -1)`` / 下 ``(0, -1, -1)``（Z 一律 -1：楼梯往下走）
* 滚轮上下改节数（阵列数量）

纯逻辑（常量 + 方向判定 + 加修改器）都放在这个模块里，无头环境可以直接断言；
交互那层（模态算符）在 :mod:`redkolab.ops` 里。
"""

import bpy

#: 台阶尺寸（米）：宽 X、高 Z，长度 Y 不变
STEP_WIDTH = 0.3
STEP_HEIGHT = 0.2

#: 默认相对偏移系数：往 +X 一级、往下 -Z 一级
DEFAULT_FACTORS = (1.0, 0.0, -1.0)

#: 鼠标位移小于这个像素数就不换方向（避免手抖乱跳）
DIRECTION_THRESHOLD = 10.0

MIN_COUNT = 1
MAX_COUNT = 500
DEFAULT_COUNT = 2

#: 修改器名字（再次调用时复用同一个，不会越堆越多）
MODIFIER_NAME = "红红子lab 楼梯"

#: 四个方向的相对偏移系数
DIRECTION_FACTORS = {
    "RIGHT": (1.0, 0.0, -1.0),
    "LEFT": (-1.0, 0.0, -1.0),
    "UP": (0.0, 1.0, -1.0),
    "DOWN": (0.0, -1.0, -1.0),
}


def direction_from_delta(dx, dy, threshold=DIRECTION_THRESHOLD):
    """按鼠标位移判断方向：绝对值大的那个轴说话，太小就不换。"""
    if abs(dx) < threshold and abs(dy) < threshold:
        return None
    if abs(dx) >= abs(dy):
        return "RIGHT" if dx >= 0 else "LEFT"
    return "UP" if dy >= 0 else "DOWN"


def factors_for_direction(direction):
    """方向 -> 相对偏移系数（认不出来就用默认的）。"""
    return DIRECTION_FACTORS.get(direction, DEFAULT_FACTORS)


def clamp_count(count):
    """把节数夹在合法范围内。"""
    try:
        value = int(count)
    except (TypeError, ValueError):
        return DEFAULT_COUNT
    return max(MIN_COUNT, min(MAX_COUNT, value))


def find_modifier(obj):
    """找出本插件加的楼梯阵列修改器。"""
    for modifier in getattr(obj, "modifiers", ()) or ():
        if getattr(modifier, "name", "") == MODIFIER_NAME and modifier.type == 'ARRAY':
            return modifier
    return None


def resize_step(obj, width=STEP_WIDTH, height=STEP_HEIGHT):
    """把体块整成「宽 0.3 / 高 0.2 / 长度不变」。

    约定：宽度 = X、高度 = Z、长度 = Y（保持不变）。
    相对偏移的系数乘的就是这三个尺寸，所以 x=1、z=-1 正好是
    「每级往 +X 挪 0.3 m、往下 0.2 m」。
    """
    try:
        dimensions = list(obj.dimensions)
    except Exception:
        return False
    if len(dimensions) != 3:
        return False
    try:
        obj.dimensions = (width, dimensions[1], height)
    except Exception:
        return False
    return True


def add_array(obj, factors=None, count=DEFAULT_COUNT):
    """给物体加/更新相对偏移阵列，返回修改器。"""
    modifier = find_modifier(obj)
    if modifier is None:
        modifier = obj.modifiers.new(name=MODIFIER_NAME, type='ARRAY')
    modifier.use_relative_offset = True
    modifier.relative_offset_displace = tuple(factors or DEFAULT_FACTORS)
    modifier.count = clamp_count(count)
    return modifier


def remove_array(obj):
    """去掉本插件加的阵列修改器，返回是否真的删掉了。"""
    modifier = find_modifier(obj)
    if modifier is None:
        return False
    try:
        obj.modifiers.remove(modifier)
    except Exception:
        return False
    return True
