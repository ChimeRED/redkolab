# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab 一键楼梯

把选中的体块整成台阶，再加一个**相对偏移阵列**（Array 修改器）复制出楼梯：

* 高度（Z）恒为 ``0.2`` m；
* **沿着阵列方向的那条水平边是 0.3 m**，另一条水平边**保持选中前的尺寸不变**：
  * 左右方向（系数 ``(±1, 0, -1)``）→ ``X = 0.3``、``Y`` 不变；
  * 上下方向（系数 ``(0, ±1, -1)``）→ ``Y = 0.3``、``X`` 不变；
* 相对偏移系数默认 ``(1, 0, -1)`` —— Blender 的相对偏移是「系数 × 该轴尺寸」，
  所以左右时每级往 ``+X`` 挪 0.3 m、上下时每级往 ``+Y`` 挪 0.3 m，两种都是往下 0.2 m；
* 鼠标移动方向换系数：右 ``(1, 0, -1)`` / 左 ``(-1, 0, -1)`` /
  上 ``(0, 1, -1)`` / 下 ``(0, -1, -1)``（Z 一律 -1：楼梯往下走），
  换方向时体块形状也跟着换成对应朝向（另一条水平轴会回到选中前的尺寸）；
* 滚轮上下改节数（阵列数量）。

纯逻辑（常量 + 方向判定 + 尺寸换算 + 加修改器）都放在这个模块里，无头环境可以直接断言；
交互那层（模态算符）在 :mod:`redkolab.ops` 里。
"""

import bpy

#: 沿阵列方向的那条水平边（踏面）= 0.3 m，高度（踢面）= 0.2 m
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

#: 方向 -> 阵列走哪条水平轴（0.3 就放这条轴上）
AXIS_FOR_DIRECTION = {
    "RIGHT": "X",
    "LEFT": "X",
    "UP": "Y",
    "DOWN": "Y",
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


def axis_for_direction(direction):
    """方向 -> 哪条水平轴是 0.3（踏面）：左右走 X，上下走 Y。"""
    return AXIS_FOR_DIRECTION.get(direction, "X")


def step_dimensions(axis, original, width=STEP_WIDTH, height=STEP_HEIGHT):
    """按阵列轴向算出目标尺寸。

    规则：Z 恒为 ``height``；阵列走的那条水平轴设成 ``width``；
    另一条水平轴**保持选中前的尺寸**（``original``）。

    >>> step_dimensions("X", (1.0, 4.0, 0.5))
    (0.3, 4.0, 0.2)
    >>> step_dimensions("Y", (1.0, 4.0, 0.5))
    (1.0, 0.3, 0.2)
    """
    base = [1.0, 1.0, 1.0]
    try:
        values = [float(value) for value in original]
    except (TypeError, ValueError):
        values = []
    for index in range(min(3, len(values))):
        base[index] = values[index]

    target = [base[0], base[1], height]
    if axis == "Y":
        target[1] = width
    else:
        target[0] = width
    return tuple(target)


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


def _scale_of(obj):
    try:
        return [abs(float(value)) for value in obj.scale]
    except Exception:
        return [1.0, 1.0, 1.0]


def local_size(obj):
    """网格**自身**的包围盒尺寸：不含缩放、也不含任何修改器。

    顶点特别多的网格（> 2 万）为了不卡，回落到「当前尺寸 ÷ 缩放」的估算
    （在挂上阵列之前读，结果一样）。
    """
    data = getattr(obj, "data", None)
    vertices = getattr(data, "vertices", None)

    if vertices is None or len(vertices) == 0 or len(vertices) > 20000:
        scale = _scale_of(obj)
        try:
            return tuple(float(value) / (scale[index] if scale[index] > 1e-9 else 1.0)
                         for index, value in enumerate(obj.dimensions))
        except Exception:
            return (1.0, 1.0, 1.0)

    mins = [float("inf")] * 3
    maxs = [float("-inf")] * 3
    for vertex in vertices:
        co = vertex.co
        for index in range(3):
            value = float(co[index])
            if value < mins[index]:
                mins[index] = value
            if value > maxs[index]:
                maxs[index] = value

    result = []
    for index in range(3):
        extent = maxs[index] - mins[index]
        result.append(extent if extent > 1e-9 else 1.0)
    return tuple(result)


def mesh_dimensions(obj):
    """物体在**不带修改器**时的尺寸 = 网格包围盒 × 缩放。

    为什么不直接用 ``obj.dimensions``：那是「带修改器」的包围盒 —— 一旦挂上阵列，
    范围会被撑成好几倍（3 节阵列 ≈ 3 倍），拿它当基准设尺寸就会越改越离谱。
    """
    local = local_size(obj)
    scale = _scale_of(obj)
    return tuple(local[index] * scale[index] for index in range(3))


def resize_step(obj, axis="X", local=None, original=None):
    """按轴向把体块整成台阶尺寸。

    ``local`` 是网格自身包围盒（见 :func:`local_size`，与修改器无关），
    ``original`` 是"选中前的尺寸"（决定哪条水平轴保持不动）；
    两者都不传时就地取当前值（相当于只做一次成型）。

    实现上用**缩放**换算而不是 ``obj.dimensions``，所以物体上已经有阵列修改器时
    也不会被撑大的包围盒带偏，而且反复调用结果一致（幂等）。
    """
    if local is None:
        local = local_size(obj)
    if original is None:
        original = mesh_dimensions(obj)

    target = step_dimensions(axis, original)
    try:
        current = [float(value) for value in obj.scale]
    except Exception:
        return False

    new_scale = list(current)
    for index in range(3):
        if local[index] > 1e-9:
            sign = -1.0 if current[index] < 0 else 1.0
            new_scale[index] = sign * target[index] / local[index]

    try:
        obj.scale = tuple(new_scale)
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
