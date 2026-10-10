# SPDX-License-Identifier: GPL-3.0-or-later
"""红红子lab (REDKOlab) · 跨版本兼容层

本插件只支持 **Blender 4.0 及以上**，所以这里不再有 2.80 ~ 3.1 的旧接口分支。
仍然集中放在这个文件里的差异点有两个：

1. 给 ``bpy.ops`` 注入「以某个物体为活动物体」的上下文
   统一用 ``bpy.context.temp_override()``（3.2 引入，4.0 起是唯一写法 ——
   旧的 ``bpy.ops.xxx({...})`` 字典覆盖在 4.0 已被移除）。

2. 插件偏好设置对象的取得
   * 4.0 / 4.1 传统安装：``preferences.addons["redkolab"]``
   * 4.2+ 扩展安装：``preferences.addons["bl_ext.<仓库>.redkolab"]``
   同一个包在两种安装方式下模块名不同，所以一律用 ``__package__``，
   不能写死字符串。
"""

import contextlib

import bpy

# --------------------------------------------------------------------- 常量

#: 插件模块目录名，同时也是 4.2+ 扩展的 id（必须是纯 ASCII）
ADDON_ID = "redkolab"

#: 中文名
ADDON_NAME = "红红子lab"

#: 英文名
ADDON_NAME_EN = "REDKOlab"

#: 插件列表里显示的完整名字（中英文都能搜到）
ADDON_NAME_FULL = "{} ({})".format(ADDON_NAME, ADDON_NAME_EN)

#: 支持的最低 Blender 版本
MIN_BLENDER_VERSION = (4, 0, 0)

#: 插件模块名。传统安装是 "redkolab"；4.2+ 扩展安装是
#: "bl_ext.<仓库名>.redkolab"。偏好设置的 bl_idname 必须与它一致。
PACKAGE = __package__ or ADDON_ID

#: 默认饼菜单快捷键：` 键
DEFAULT_KEY_TYPE = "ACCENT_GRAVE"

VERSION = tuple(bpy.app.version)
VERSION_STRING = ".".join(str(part) for part in VERSION)


# ----------------------------------------------------------------- 偏好设置


def get_preferences(context=None):
    """取本插件的偏好设置对象；取不到时返回 None（后台模式、未启用等）。"""
    ctx = context if context is not None else bpy.context
    try:
        addons = ctx.preferences.addons
    except Exception:
        return None

    addon = None
    try:
        addon = addons.get(PACKAGE)
    except Exception:
        addon = None

    if addon is None:
        # 扩展安装时模块名带 bl_ext.<仓库>. 前缀，退化成按后缀匹配
        try:
            for key, item in addons.items():
                if key == ADDON_ID or key.endswith("." + ADDON_ID):
                    addon = item
                    break
        except Exception:
            addon = None

    if addon is None:
        return None
    try:
        return addon.preferences
    except Exception:
        return None


# ----------------------------------------------------------------- 目标物体


def get_target_objects(context, use_active_fallback=True):
    """要处理的物体列表：优先「选中的可编辑物体」，没有选中时退回活动物体。"""
    objects = []
    try:
        selected = context.selected_editable_objects
    except Exception:
        selected = None
    if selected:
        objects = list(selected)

    if not objects and use_active_fallback:
        active = None
        try:
            active = context.view_layer.objects.active
        except Exception:
            active = None
        if active is None:
            active = getattr(context, "active_object", None)
        if active is not None:
            objects = [active]

    result = []
    seen = set()
    for obj in objects:
        if obj is None:
            continue
        key = obj.name_full if hasattr(obj, "name_full") else obj.name
        if key in seen:
            continue
        seen.add(key)
        result.append(obj)
    return result


def poll_objects(context):
    """算符 poll：物体模式下、且至少有一个可处理物体。

    修改器/材质的批量操作都在物体模式下进行（编辑模式下 Blender 不允许
    改动这些数据），所以模式不对时直接把按钮置灰。
    """
    if getattr(context, "mode", "OBJECT") != "OBJECT":
        return False
    return bool(get_target_objects(context))


def is_library_object(obj):
    """是否是链接自其它 .blend 的物体（不可修改）。"""
    return getattr(obj, "library", None) is not None


def object_in_view_layer(context, obj):
    """物体是否在当前视图层里（不在的话任何 bpy.ops 都碰不到它）。"""
    try:
        return obj.name in context.view_layer.objects
    except Exception:
        return True


# ------------------------------------------------------------ 上下文覆盖调用


def _override_kwargs(obj, window=None, area=None):
    kwargs = {
        "object": obj,
        "active_object": obj,
        "selected_objects": [obj],
        "selected_editable_objects": [obj],
    }
    if window is not None:
        kwargs["window"] = window
    if area is not None:
        kwargs["area"] = area
    return kwargs


def run_op(operator, obj=None, window=None, area=None, **kwargs):
    """在（可选的）物体覆盖上下文里调用 ``bpy.ops`` 算符。

    只用 ``temp_override``：本插件最低支持 4.0，而 4.0 已经移除了
    2.80 ~ 3.1 时代把覆盖字典当第一个位置参数的写法。
    """
    if obj is None:
        return operator(**kwargs)
    with bpy.context.temp_override(**_override_kwargs(obj, window, area)):
        return operator(**kwargs)


@contextlib.contextmanager
def temporary_visible(obj):
    """临时让物体可见/可选，退出时还原。

    部分 ``bpy.ops``（例如 modifier_apply / convert）在物体被隐藏时会
    直接失败，所以先临时打开，做完再恢复原状。
    """
    restore = []
    try:
        if getattr(obj, "hide_viewport", False):
            obj.hide_viewport = False
            restore.append(("hide_viewport", True))
        if getattr(obj, "hide_select", False):
            obj.hide_select = False
            restore.append(("hide_select", True))
        if obj.hide_get():
            obj.hide_set(False)
            restore.append(("hide_set", True))
    except Exception:
        pass

    try:
        yield
    finally:
        for name, value in restore:
            try:
                if name == "hide_set":
                    obj.hide_set(value)
                else:
                    setattr(obj, name, value)
            except Exception:
                pass


def ensure_single_user_data(obj):
    """数据块被多个物体共享时复制一份，返回是否发生了复制。

    应用修改器不允许作用在多用户数据上。
    """
    data = getattr(obj, "data", None)
    if data is None or not hasattr(data, "copy"):
        return False
    try:
        users = data.users
    except Exception:
        return False
    if users <= 1:
        return False
    try:
        obj.data = data.copy()
        return True
    except Exception:
        return False


# --------------------------------------------------------------- 键位与显示


def addon_keyconfig():
    """插件专用 keyconfig；``--background`` 下没有窗口，会返回 None。"""
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return None
    return getattr(window_manager.keyconfigs, "addon", None)


_MODIFIER_TOKENS = ("CTRL", "SHIFT", "ALT", "OSKEY")


def is_modifier_key(event_type):
    """是否是单独的修饰键（Ctrl/Shift/Alt/Cmd），这类键本身不能当快捷键。"""
    event_type = event_type or ""
    return any(token in event_type for token in _MODIFIER_TOKENS)


def modifier_flags(event):
    """从事件里取出 (ctrl, shift, alt, oskey)。"""
    return (
        bool(getattr(event, "ctrl", False)),
        bool(getattr(event, "shift", False)),
        bool(getattr(event, "alt", False)),
        bool(getattr(event, "oskey", False)),
    )


_KEY_LABELS = {
    "ACCENT_GRAVE": "`",
    "SPACE": "空格",
    "TAB": "Tab",
    "RETURN": "回车",
    "NUMPAD_ENTER": "小键盘回车",
    "BACK_SPACE": "退格",
    "ESC": "Esc",
    "DELETE": "Del",
    "LEFT_ARROW": "←",
    "RIGHT_ARROW": "→",
    "UP_ARROW": "↑",
    "DOWN_ARROW": "↓",
    "LEFT_BRACKET": "[",
    "RIGHT_BRACKET": "]",
    "MINUS": "-",
    "EQUAL": "=",
    "SEMI_COLON": ";",
    "QUOTE": "'",
    "COMMA": ",",
    "PERIOD": ".",
    "SLASH": "/",
    "BACK_SLASH": "\\",
    "PLUS": "+",
    "NUMPAD_PLUS": "小键盘 +",
    "NUMPAD_MINUS": "小键盘 -",
    "NUMPAD_ASTERIX": "小键盘 *",
    "NUMPAD_SLASH": "小键盘 /",
}


def key_label(key_type, ctrl=False, shift=False, alt=False, oskey=False):
    """把按键标识翻译成界面上显示的字符串，例如 Ctrl + `。"""
    parts = []
    if ctrl:
        parts.append("Ctrl")
    if shift:
        parts.append("Shift")
    if alt:
        parts.append("Alt")
    if oskey:
        parts.append("Cmd")
    parts.append(_KEY_LABELS.get(key_type, key_type or "?"))
    return " + ".join(parts)


def tag_redraw(context=None):
    """刷新所有界面区域（后台模式没有窗口，静默跳过）。"""
    ctx = context if context is not None else bpy.context
    window_manager = getattr(ctx, "window_manager", None)
    if window_manager is None:
        return
    try:
        windows = list(window_manager.windows)
    except Exception:
        return
    for window in windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            try:
                area.tag_redraw()
            except Exception:
                pass
