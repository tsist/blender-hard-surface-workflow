"""Editable profile work planes and dimension cutters for Blender 5.2.

This package owns object-mode profile authoring for the Hard Surface Workflow
add-on.  It deliberately keeps the public surface small and JSON-only:

    TOOLS
    catalog()
    execute(obj, tool, params)
    register()
    unregister()

``catalog()`` describes every ``profile_*`` tool using the same
``label``/``required``/``optional``/``read_only`` contract as the adjacent
``stack_tools.catalog()``.  ``execute`` creates or edits one work plane or one
independent dimension cutter and returns JSON-compatible evidence.

Design invariants:

* Work planes are object-local, right-handed frames.  A plane's local +Z is
  its normal.  XY uses local +X/+Y/+Z, XZ uses +X/-Z/+Y, and YZ uses
  +Y/+Z/+X so every frame has determinant +1.
* Cutters are closed solids built in work-plane UV space and extruded around
  the plane.  Their cap and side winding is constructed explicitly and checked
  against signed volume before an object is committed.
* Resizing builds and validates a complete replacement mesh first.  The live
  cutter is only touched after validation, so rejection is atomic.
* A generated cutter records a geometry signature.  If a user edited its mesh,
  resizing requires an explicit policy (refuse, copy, or replace).
* Shared mesh datablocks are never edited in place.  Resizing a cutter with
  shared data requires ``unlink_shared=True`` and makes only that object
  independent; other users keep their original mesh.
* Resizing preserves object identity and object transform, so Boolean
  modifiers that share one cutter stay bound.

The module does not register anything at import time.  The parent add-on calls
``register()``/``unregister()``; the background QA suite can use the backend
without installing or enabling the add-on.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math

import bmesh
import bpy
from mathutils import Matrix, Vector


__all__ = ("TOOLS", "catalog", "describe", "execute", "register", "unregister")


# ---------------------------------------------------------------------------
# Public names and constants


TOOL_WORK_PLANE_CREATE = "profile_work_plane_create"
TOOL_WORK_PLANE_UPDATE = "profile_work_plane_update"
TOOL_WORK_PLANE_INSPECT = "profile_work_plane_inspect"
TOOL_DIMENSION_CREATE = "profile_dimension_create"
TOOL_DIMENSION_RESIZE = "profile_dimension_resize"
TOOL_DIMENSION_INSPECT = "profile_dimension_inspect"

SHAPE_LINE = "LINE"
SHAPE_ARC = "ARC"
SHAPE_CIRCLE = "CIRCLE"
SHAPE_ROUND_SLOT = "ROUND_SLOT"
SHAPE_POLYGON = "POLYGON"
SHAPES = (SHAPE_LINE, SHAPE_ARC, SHAPE_CIRCLE, SHAPE_ROUND_SLOT, SHAPE_POLYGON)

PLANE_OBJECT_XY = "OBJECT_XY"
PLANE_OBJECT_XZ = "OBJECT_XZ"
PLANE_OBJECT_YZ = "OBJECT_YZ"
PLANE_FACE = "FACE"
PLANE_DIRECTION_EDGE = "DIRECTION_EDGE"
PLANE_MODES = (
    PLANE_OBJECT_XY,
    PLANE_OBJECT_XZ,
    PLANE_OBJECT_YZ,
    PLANE_FACE,
    PLANE_DIRECTION_EDGE,
)

USER_EDIT_REFUSE = "REFUSE"
USER_EDIT_COPY = "COPY"
USER_EDIT_REPLACE = "REPLACE"
USER_EDIT_POLICIES = (USER_EDIT_REFUSE, USER_EDIT_COPY, USER_EDIT_REPLACE)

ROLE_WORK_PLANE = "WORK_PLANE"
ROLE_DIMENSION_CUTTER = "DIMENSION_CUTTER"
PROFILE_VERSION = 1

_ROLE = "hsw_profile_role"
_VERSION = "hsw_profile_version"
_SHAPE = "hsw_profile_shape"
_SPEC = "hsw_profile_spec"
_DIMENSIONS = "hsw_profile_dimensions"
_SIGNATURE = "hsw_profile_signature"
_GENERATION = "hsw_profile_generation"
_WORK_PLANE = "hsw_profile_work_plane"
_PLANE_SPEC = "hsw_plane_spec"
_PLANE_MATRIX = "hsw_plane_matrix"
_PLANE_SIGNATURE = "hsw_plane_signature"

_PROFILE_PROPS = (
    _ROLE,
    _VERSION,
    _SHAPE,
    _SPEC,
    _DIMENSIONS,
    _SIGNATURE,
    _GENERATION,
    _WORK_PLANE,
)
_PLANE_PROPS = (_ROLE, _VERSION, _PLANE_SPEC, _PLANE_MATRIX, _PLANE_SIGNATURE)

_MAX_COORD = 1.0e6
_MAX_NAME_BYTES = 63
_EPS = 1.0e-12
_DEFAULT_DEPTH = 0.2
_DEFAULT_SEGMENTS = 64
_DEFAULT_POINTS = [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]]

_SHAPE_KEYS = {
    SHAPE_LINE: ("start", "end", "line_width"),
    SHAPE_ARC: ("radius", "start_angle", "end_angle", "arc_width", "segments"),
    SHAPE_CIRCLE: ("radius", "segments"),
    SHAPE_ROUND_SLOT: ("length", "width", "segments"),
    SHAPE_POLYGON: ("points",),
}
_COMMON_KEYS = ("center", "depth")
_ALL_SHAPE_KEYS = tuple(sorted({key for keys in _SHAPE_KEYS.values() for key in keys}))


# ---------------------------------------------------------------------------
# Catalog


def _option(kind, default, description, *, minimum=None, maximum=None,
            enum=None, items=None, min_items=None, max_items=None):
    rule = {"type": kind, "default": deepcopy(default), "description": description}
    if minimum is not None:
        rule["min"] = minimum
    if maximum is not None:
        rule["max"] = maximum
    if enum is not None:
        rule["enum"] = list(enum)
    if items is not None:
        rule["items"] = deepcopy(items)
    if min_items is not None:
        rule["min_items"] = int(min_items)
    if max_items is not None:
        rule["max_items"] = int(max_items)
    return rule


def _number(default, description, minimum, maximum):
    return _option("number", float(default), description,
                   minimum=float(minimum), maximum=float(maximum))


def _integer(default, description, minimum, maximum):
    return _option("integer", int(default), description,
                   minimum=int(minimum), maximum=int(maximum))


def _boolean(default, description):
    return _option("boolean", bool(default), description)


def _enum(default, description, choices):
    return _option("string", default, description, enum=choices)


def _string(default, description):
    return _option("string", default, description)


def _vector(items, default, description):
    return _option("array", default, description, items={"type": "number"},
                   min_items=items, max_items=items)


def _build_specs():
    mode_rule = _enum(PLANE_OBJECT_XY, "工作平面来源：对象本地轴、面或方向边", PLANE_MODES)
    shape_rule = _enum(SHAPE_CIRCLE, "刀体截面形状", SHAPES)
    policy_rule = _enum(USER_EDIT_REFUSE, "检测到用户手改网格时的处理方式", USER_EDIT_POLICIES)

    plane_create = {
        "mode": mode_rule,
        "name": _string("", "新工作平面的对象名称；留空自动命名"),
        "size": _number(2.0, "工作平面方形边长的完整尺寸", 0.01, 1000.0),
        "offset": _vector(3, [0.0, 0.0, 0.0], "沿工作平面的 U/V/N 局部偏移"),
        "flip": _boolean(False, "反转工作平面法向"),
        "face_index": _integer(0, "FACE 模式使用的网格面索引", 0, 1000000),
        "edge_index": _integer(0, "DIRECTION_EDGE 模式使用的网格边索引", 0, 1000000),
        "collection": _string("", "目标集合名称；留空使用目标对象集合"),
    }
    plane_update = {
        "mode": mode_rule,
        "name": _string("", "现有工作平面对象名称"),
        "size": _number(2.0, "新的方形边长；省略时保留原尺寸", 0.01, 1000.0),
        "offset": _vector(3, [0.0, 0.0, 0.0], "新的 U/V/N 局部偏移；省略时保留原值"),
        "flip": _boolean(False, "反转工作平面法向"),
        "face_index": _integer(0, "FACE 或方向边上下文使用的面索引", 0, 1000000),
        "edge_index": _integer(0, "DIRECTION_EDGE 模式使用的网格边索引", 0, 1000000),
        "allow_user_transform": _boolean(False, "允许覆盖用户手动修改过的工作平面变换"),
        "allow_user_edit": _boolean(False, "允许重建被用户手动修改过的工作平面网格"),
    }
    plane_inspect = {
        "name": _string("", "现有工作平面对象名称"),
    }

    cutter_plane = {
        "work_plane": _string("", "现有工作平面对象名称；留空自动创建"),
        "plane_mode": mode_rule,
        "plane_name": _string("", "自动创建工作平面的名称；留空自动命名"),
        "plane_size": _number(2.0, "自动创建工作平面的方形边长", 0.01, 1000.0),
        "plane_offset": _vector(3, [0.0, 0.0, 0.0], "自动工作平面的 U/V/N 局部偏移"),
        "plane_flip": _boolean(False, "反转自动工作平面的法向"),
        "face_index": _integer(0, "自动工作平面 FACE 模式的面索引", 0, 1000000),
        "edge_index": _integer(0, "自动工作平面 DIRECTION_EDGE 模式的边索引", 0, 1000000),
    }
    cutter_common = {
        "center": _vector(2, [0.0, 0.0], "截面在工作平面 UV 内的中心偏移"),
        "depth": _number(_DEFAULT_DEPTH, "沿工作平面法向的刀体厚度", 1.0e-6, 1000.0),
    }
    cutter_shape = {
        "start": _vector(2, [-0.5, 0.0], "线段起点 UV"),
        "end": _vector(2, [0.5, 0.0], "线段终点 UV"),
        "line_width": _number(0.1, "线段刀体的带宽", 1.0e-6, 1000.0),
        "radius": _number(0.5, "圆或圆弧半径", 1.0e-6, 1000.0),
        "start_angle": _number(0.0, "圆弧起始角（弧度）", -1.0e6, 1.0e6),
        "end_angle": _number(math.pi, "圆弧终止角（弧度）", -1.0e6, 1.0e6),
        "arc_width": _number(0.1, "圆弧刀体的径向宽度", 1.0e-6, 1000.0),
        "length": _number(1.0, "圆端槽的总长度（含两端圆头）", 1.0e-6, 1000.0),
        "width": _number(0.3, "圆端槽的直径/宽度", 1.0e-6, 1000.0),
        "segments": _integer(_DEFAULT_SEGMENTS, "圆/弧/槽的整圆离散段数", 8, 512),
        "points": _option("array", _DEFAULT_POINTS, "简单多边形 UV 顶点列表",
                          items={"type": "array"}, min_items=3, max_items=256),
    }
    cutter_extra = {
        "collection": _string("", "刀体对象的目标集合；留空使用目标或工作平面集合"),
        "clone_from": _string("", "从现有独立刀体克隆当前网格；不修改源刀体"),
    }
    resize_extra = {
        "shape": shape_rule,
        "user_edit_policy": policy_rule,
        "unlink_shared": _boolean(False, "允许为当前对象解除共享网格；其他用户保持不变"),
        "expected_signature": _string("", "调用方持有的当前网格签名，用于拒绝过期修改"),
        "copy_name": _string("", "COPY 策略生成的新刀体名称；留空自动命名"),
        "collection": _string("", "COPY 策略的新刀体集合；留空使用源刀体集合"),
    }

    specs = {
        TOOL_WORK_PLANE_CREATE: {
            "label": "创建工作平面",
            "description": "从对象本地 XY/XZ/YZ、选定面或方向边创建可编辑的正交工作平面。",
            "required": ["mode"],
            "optional": ["name", "size", "offset", "flip", "face_index",
                         "edge_index", "collection"],
            "read_only": False,
            "parameters": plane_create,
        },
        TOOL_WORK_PLANE_UPDATE: {
            "label": "更新工作平面",
            "description": "按当前目标重建工作平面变换；手动改过的变换需要显式允许覆盖。",
            "required": ["name"],
            "optional": ["mode", "size", "offset", "flip", "face_index", "edge_index",
                         "allow_user_transform", "allow_user_edit"],
            "read_only": False,
            "parameters": plane_update,
        },
        TOOL_WORK_PLANE_INSPECT: {
            "label": "检查工作平面",
            "description": "报告工作平面的矩阵、轴、法向、网格绕序和引用它的刀体。",
            "required": ["name"],
            "optional": [],
            "read_only": True,
            "parameters": plane_inspect,
        },
        TOOL_DIMENSION_CREATE: {
            "label": "创建尺寸刀体",
            "description": "在工作平面上创建独立的线段、圆弧、圆、圆端槽或简单多边形刀体。",
            "required": ["name", "shape"],
            "optional": ["work_plane", "plane_mode", "plane_name", "plane_size",
                         "plane_offset", "plane_flip", "face_index", "edge_index",
                         "center", "depth", "start", "end", "line_width", "radius",
                         "start_angle", "end_angle", "arc_width", "length", "width",
                         "segments", "points", "collection", "clone_from"],
            "read_only": False,
            "parameters": {"name": _string("", "新刀体对象名称"),
                           "shape": shape_rule,
                           **cutter_plane, **cutter_common, **cutter_shape, **cutter_extra},
        },
        TOOL_DIMENSION_RESIZE: {
            "label": "原位修改刀体尺寸",
            "description": "重建现有刀体的网格并保持对象变换；共享、手改和过期状态必须显式处理。",
            "required": ["name"],
            "optional": ["shape", "center", "depth", "start", "end", "line_width",
                         "radius", "start_angle", "end_angle", "arc_width", "length",
                         "width", "segments", "points", "user_edit_policy",
                         "unlink_shared", "expected_signature", "copy_name",
                         "collection"],
            "read_only": False,
            "parameters": {"name": _string("", "现有刀体对象名称"),
                           **cutter_common, **cutter_shape, **resize_extra},
        },
        TOOL_DIMENSION_INSPECT: {
            "label": "检查尺寸刀体",
            "description": "报告尺寸、体积、面绕序、签名状态、共享数据用户和 Boolean 消费者。",
            "required": ["name"],
            "optional": [],
            "read_only": True,
            "parameters": {"name": _string("", "现有尺寸刀体对象名称")},
        },
    }
    for tool, spec in specs.items():
        spec["category"] = "profile"
        spec["category_label"] = "尺寸刀体"
        spec["context"] = "OBJECT_MESH"
        spec["topology_changes"] = tool not in {TOOL_WORK_PLANE_INSPECT,
                                                TOOL_DIMENSION_INSPECT}
        spec["domains"] = []
        spec["tool"] = tool
        requirements = set(spec["required"]) | set(spec["optional"])
        if requirements != set(spec["parameters"]):
            missing = sorted(requirements - set(spec["parameters"]))
            extra = sorted(set(spec["parameters"]) - requirements)
            raise AssertionError(f"{tool} catalog mismatch: missing={missing} extra={extra}")
    return specs


TOOLS = _build_specs()


def catalog():
    """Return an independent description of every ``profile_*`` tool."""
    return deepcopy(TOOLS)


def describe(tool):
    """Return one tool description without exposing the module dictionary."""
    if not isinstance(tool, str) or tool not in TOOLS:
        raise ValueError(f"Unknown profile tool: {tool!r}")
    return deepcopy(TOOLS[tool])


# ---------------------------------------------------------------------------
# Validation and JSON helpers


def _name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty Blender name")
    if len(value.encode("utf-8")) > _MAX_NAME_BYTES:
        raise ValueError(f"{label} must be at most {_MAX_NAME_BYTES} UTF-8 bytes")
    return value


def _optional_name(value, label):
    if value is None or value == "":
        return None
    return _name(value, label)


def _optional_text(value, label, maximum=4096):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} must be at most {maximum} UTF-8 bytes")
    return value


def _bool(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _int(value, label, minimum, maximum):
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _number(value, label, minimum, maximum):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite number")
    try:
        finite = math.isfinite(value)
    except (OverflowError, ValueError):
        finite = False
    if not finite:
        raise ValueError(f"{label} must be a finite number")
    value = float(value)
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}")
    return value


def _enum(value, label, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{label} must be one of {', '.join(choices)}")
    return value


def _vec(value, length, label):
    if isinstance(value, Vector):
        value = list(value)
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{label} must contain exactly {length} numbers")
    return [_number(item, f"{label}[{index}]", -_MAX_COORD, _MAX_COORD)
            for index, item in enumerate(value)]


def _matrix_rows(matrix):
    return [[round(float(matrix[row][column]), 9) for column in range(4)]
            for row in range(4)]


def _matrix_list(matrix):
    return [value for row in _matrix_rows(matrix) for value in row]


def _matrix_from_list(values):
    if not isinstance(values, (list, tuple)) or len(values) != 16:
        raise ValueError("Matrix metadata must contain 16 numbers")
    return Matrix((
        list(values[0:4]),
        list(values[4:8]),
        list(values[8:12]),
        list(values[12:16]),
    ))


def _vector_list(vector):
    return [round(float(value), 9) for value in vector]


def _clean(value):
    """Convert a result to strict JSON values, rejecting non-finite numbers."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Profile result contains a non-finite number")
        return round(float(value), 9)
    if isinstance(value, Vector):
        return [_clean(item) for item in value]
    if isinstance(value, Matrix):
        return _matrix_rows(value)
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    raise ValueError(f"Profile result contains a non-JSON value: {type(value).__name__}")


def _result(tool, obj, payload):
    evidence = {
        "tool": tool,
        "status": "FINISHED",
        "object": obj.name if isinstance(obj, bpy.types.Object) else None,
    }
    evidence.update(payload)
    return _clean(evidence)


def _signature(mesh):
    digest = hashlib.sha256()
    for vertex in mesh.vertices:
        co = tuple(0.0 if abs(value) < 5.0e-10 else value for value in vertex.co)
        digest.update((",".join(f"{value:.9f}" for value in co) + ";").encode("ascii"))
    digest.update(b"|")
    for polygon in mesh.polygons:
        digest.update((",".join(str(index) for index in polygon.vertices) + ";").encode("ascii"))
    return digest.hexdigest()


def _matrix_close(left, right, tolerance=1.0e-7):
    return all(abs(float(left[row][column]) - float(right[row][column])) <= tolerance
               for row in range(4) for column in range(4))


def _profile_kind(obj):
    if not isinstance(obj, bpy.types.Object):
        return None
    return obj.get(_ROLE)


def _require_role(obj, role, label):
    kind = _profile_kind(obj)
    if kind != role:
        raise ValueError(f"{label} is not a generated {role.lower().replace('_', ' ')} object")
    version = obj.get(_VERSION)
    if type(version) is not int or version != PROFILE_VERSION:
        raise ValueError(f"{label} uses an unsupported profile version: {version!r}")
    return obj


def _object_name(value, label):
    return _name(value, label)


def _view_object(name, allowed=None, label="Object reference"):
    name = _object_name(name, label)
    obj = bpy.context.view_layer.objects.get(name)
    if obj is None:
        bpy.context.view_layer.update()
        obj = bpy.context.view_layer.objects.get(name)
    if obj is None:
        raise ValueError(f"Expected {label} in the current View Layer: {name!r}")
    if allowed is not None and obj.type not in allowed:
        raise ValueError(f"{label} must be of type {'/'.join(sorted(allowed))}: {name!r}")
    return obj


def _target_mesh(obj, write=False):
    if not isinstance(obj, bpy.types.Object):
        raise ValueError("Target must be a Blender object")
    if obj.type != "MESH" or obj.data is None:
        raise ValueError(f"Target must be a mesh object: {obj.name!r}")
    visible = bpy.context.view_layer.objects.get(obj.name)
    if visible != obj:
        bpy.context.view_layer.update()
        visible = bpy.context.view_layer.objects.get(obj.name)
    if visible != obj:
        raise ValueError(f"Target must be in the current View Layer: {obj.name!r}")
    if obj.mode != "OBJECT":
        raise ValueError(f"Target must be in Object Mode: {obj.name!r}")
    if write:
        _editable_object(obj, "Target")
    return obj


def _editable_object(obj, label):
    if obj.library or obj.override_library or not obj.is_editable:
        raise ValueError(f"{label} must be a writable local object: {obj.name!r}")
    if obj.data is not None and (obj.data.library or obj.data.override_library):
        raise ValueError(f"{label} data must be local: {obj.name!r}")
    if obj.mode != "OBJECT":
        raise ValueError(f"{label} must be in Object Mode: {obj.name!r}")
    return obj


def _collection(obj, name):
    if name is None or name == "":
        if isinstance(obj, bpy.types.Object) and len(obj.users_collection) == 1:
            result = obj.users_collection[0]
        else:
            result = bpy.context.scene.collection
    else:
        name = _name(name, "Collection name")
        root = bpy.context.scene.collection
        result = root if root.name == name else bpy.data.collections.get(name)
        if result is None or result != root and result not in root.children_recursive:
            raise ValueError(f"Collection is outside the current scene: {name!r}")
    if result.library or result.override_library or not result.is_editable:
        raise ValueError(f"Destination collection is read-only: {result.name!r}")
    return result


def _truncate_utf8(text, maximum):
    data = text.encode("utf-8")
    if len(data) <= maximum:
        return text
    return data[:maximum].decode("utf-8", "ignore")


def _unique_object_name(base):
    base = _truncate_utf8(_name(base, "Object name"), _MAX_NAME_BYTES)
    candidate = base
    index = 1
    while bpy.data.objects.get(candidate) is not None:
        suffix = f".{index:03d}"
        candidate = _truncate_utf8(base, _MAX_NAME_BYTES - len(suffix)) + suffix
        index += 1
    return candidate


def _remove_object(obj):
    try:
        if obj is not None and obj.name in bpy.data.objects:
            bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:
        pass


def _remove_mesh(mesh):
    try:
        if mesh is not None and mesh.name in bpy.data.meshes and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    except Exception:
        pass


def _snapshot_props(obj, keys):
    result = {}
    for key in keys:
        if key in obj:
            value = obj[key]
            if hasattr(value, "to_list"):
                value = list(value)
            elif isinstance(value, (list, tuple)):
                value = list(value)
            result[key] = deepcopy(value)
        else:
            result[key] = None
    return result


def _restore_props(obj, snapshot):
    for key, value in snapshot.items():
        if value is None:
            try:
                del obj[key]
            except Exception:
                pass
        else:
            obj[key] = value


@contextmanager
def _transaction():
    """Collect undo/finalize actions; undo on failure, finalize on success."""
    state = {"undo": [], "finalize": []}
    try:
        yield state
    except Exception as error:
        failures = []
        for action in reversed(state["undo"]):
            try:
                action()
            except Exception as rollback_error:
                failures.append(str(rollback_error))
        if failures:
            raise RuntimeError(f"{error}; rollback failed: {', '.join(failures)}") from error
        raise
    for action in state["finalize"]:
        try:
            action()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Frame and mesh mathematics


def _newell(points):
    normal = Vector((0.0, 0.0, 0.0))
    count = len(points)
    for index in range(count):
        current = points[index]
        following = points[(index + 1) % count]
        normal.x += (current.y - following.y) * (current.z + following.z)
        normal.y += (current.z - following.z) * (current.x + following.x)
        normal.z += (current.x - following.x) * (current.y + following.y)
    if normal.length <= 1.0e-12:
        raise ValueError("Selected geometry does not define a stable normal")
    return normal.normalized()


def _orthonormal_frame(x_axis, normal):
    normal = normal.normalized()
    x_axis = x_axis - normal * x_axis.dot(normal)
    if x_axis.length <= 1.0e-9:
        raise ValueError("The requested in-plane axis is parallel to the plane normal")
    x_axis.normalize()
    y_axis = normal.cross(x_axis)
    if y_axis.length <= 1.0e-9:
        raise ValueError("Cannot build a stable in-plane Y axis")
    y_axis.normalize()
    corrected = x_axis.cross(y_axis)
    if corrected.dot(normal) < 0.999999:
        raise ValueError("Work plane basis is not right-handed")
    return x_axis, y_axis, corrected.normalized()


def _world_axes(obj):
    rotation = obj.matrix_world.to_3x3()
    axes = []
    for index in range(3):
        axis = Vector(rotation.col[index])
        if axis.length <= 1.0e-12:
            raise ValueError(f"Target transform has a degenerate local axis: {obj.name!r}")
        axes.append(axis.normalized())
    return axes


def _face_world(obj, face_index):
    mesh = obj.data
    face_index = _int(face_index, "face_index", 0, len(mesh.polygons) - 1)
    polygon = mesh.polygons[face_index]
    points = [obj.matrix_world @ mesh.vertices[index].co for index in polygon.vertices]
    if len(points) < 3:
        raise ValueError(f"Face {face_index} has fewer than three vertices")
    if any(not all(math.isfinite(value) for value in point) for point in points):
        raise ValueError(f"Face {face_index} has non-finite world coordinates")
    normal = _newell(points)
    center = sum(points, Vector((0.0, 0.0, 0.0))) / len(points)
    return {"index": face_index, "points": points, "normal": normal, "center": center}


def _edge_world(obj, edge_index):
    mesh = obj.data
    edge_index = _int(edge_index, "edge_index", 0, len(mesh.edges) - 1)
    edge = mesh.edges[edge_index]
    first, second = (obj.matrix_world @ mesh.vertices[index].co for index in edge.vertices)
    direction = second - first
    if direction.length <= 1.0e-9:
        raise ValueError(f"Edge {edge_index} has zero length")
    return {
        "index": edge_index,
        "first": first,
        "second": second,
        "direction": direction.normalized(),
        "midpoint": (first + second) * 0.5,
        "edge_key": tuple(sorted(edge.vertices)),
    }


def _edge_faces(obj, edge_key):
    return [polygon.index for polygon in obj.data.polygons
            if edge_key in polygon.edge_keys]


def _plane_spec(params, base=None):
    source = dict(base or {})
    source.update(params)
    mode = _enum(source.get("mode"), "mode", PLANE_MODES)
    spec = {
        "mode": mode,
        "size": _number(source.get("size", 2.0), "size", 0.01, 1000.0),
        "offset": _vec(source.get("offset", [0.0, 0.0, 0.0]), 3, "offset"),
        "flip": _bool(source.get("flip", False), "flip"),
    }
    if mode == PLANE_FACE:
        if "edge_index" in params:
            raise ValueError("edge_index is not used by FACE")
        face_index = source.get("face_index")
        spec["face_index"] = _int(0 if face_index is None else face_index,
                                  "face_index", 0, 1000000)
    elif mode == PLANE_DIRECTION_EDGE:
        edge_index = source.get("edge_index")
        spec["edge_index"] = _int(0 if edge_index is None else edge_index,
                                  "edge_index", 0, 1000000)
        if source.get("face_index") is not None:
            spec["face_index"] = _int(source["face_index"], "face_index", 0, 1000000)
    else:
        if source.get("face_index") is not None and "face_index" in params:
            raise ValueError(f"face_index is not used by {mode}")
        if source.get("edge_index") is not None and "edge_index" in params:
            raise ValueError(f"edge_index is not used by {mode}")
    return spec


def _frame_for(target, spec):
    mode = spec["mode"]
    offset = Vector(spec["offset"])
    flip = spec["flip"]
    normal_source = mode
    face_index = None
    edge_index = None

    if mode in (PLANE_OBJECT_XY, PLANE_OBJECT_XZ, PLANE_OBJECT_YZ):
        axes = _world_axes(target)
        if mode == PLANE_OBJECT_XY:
            x_axis, normal = axes[0], axes[2]
        elif mode == PLANE_OBJECT_XZ:
            x_axis, normal = axes[0], axes[1]
        else:
            x_axis, normal = axes[1], axes[0]
        origin = target.matrix_world.translation.copy()
    elif mode == PLANE_FACE:
        face = _face_world(target, spec["face_index"])
        face_index = face["index"]
        normal = face["normal"]
        origin = face["center"]
        points = face["points"]
        x_axis = None
        for index in range(len(points)):
            candidate = points[(index + 1) % len(points)] - points[index]
            candidate = candidate - normal * candidate.dot(normal)
            if candidate.length > 1.0e-9:
                x_axis = candidate
                break
        if x_axis is None:
            raise ValueError(f"Face {face_index} has no usable boundary direction")
        normal_source = "FACE_WINDING"
    elif mode == PLANE_DIRECTION_EDGE:
        edge = _edge_world(target, spec["edge_index"])
        edge_index = edge["index"]
        adjacent = _edge_faces(target, edge["edge_key"])
        if "face_index" in spec:
            face_index = _int(spec["face_index"], "face_index", 0, len(target.data.polygons) - 1)
            if face_index not in adjacent:
                raise ValueError(f"Face {face_index} does not contain edge {edge_index}")
            normal = _face_world(target, face_index)["normal"]
            normal_source = "ADJACENT_FACE_WINDING"
        elif adjacent:
            face_index = min(adjacent)
            normal = _face_world(target, face_index)["normal"]
            normal_source = "ADJACENT_FACE_WINDING"
        else:
            axes = _world_axes(target)
            candidate = axes[2]
            if abs(candidate.dot(edge["direction"])) > 0.999999:
                candidate = axes[1]
            candidate = candidate - edge["direction"] * candidate.dot(edge["direction"])
            if candidate.length <= 1.0e-9:
                raise ValueError(f"Edge {edge_index} has no usable face or object-local normal")
            normal = candidate.normalized()
            normal_source = "OBJECT_LOCAL_AXIS"
        origin = edge["midpoint"]
        x_axis = edge["direction"]
    else:
        raise ValueError(f"Unsupported work plane mode: {mode!r}")

    if flip:
        normal = -normal
    x_axis, y_axis, normal = _orthonormal_frame(x_axis, normal)
    origin = origin + x_axis * offset.x + y_axis * offset.y + normal * offset.z
    matrix = Matrix((
        (x_axis.x, y_axis.x, normal.x, origin.x),
        (x_axis.y, y_axis.y, normal.y, origin.y),
        (x_axis.z, y_axis.z, normal.z, origin.z),
        (0.0, 0.0, 0.0, 1.0),
    ))
    if matrix.to_3x3().determinant() < 0.999999:
        raise ValueError("Work plane matrix is not a right-handed transform")
    return {
        "matrix": matrix,
        "origin": origin,
        "x_axis": x_axis,
        "y_axis": y_axis,
        "normal": normal,
        "mode": mode,
        "face_index": face_index,
        "edge_index": edge_index,
        "normal_source": normal_source,
        "offset": offset,
        "flip": flip,
    }


# ---------------------------------------------------------------------------
# Profile outlines


def _signed_area(points):
    total = 0.0
    for index, point in enumerate(points):
        following = points[(index + 1) % len(points)]
        total += point[0] * following[1] - following[0] * point[1]
    return total * 0.5


def _orient_ccw(points):
    if _signed_area(points) < 0.0:
        points.reverse()
    return points


def _segment_intersection(first, second, third, fourth):
    def orientation(a, b, c):
        return ((b[0] - a[0]) * (c[1] - a[1])
                - (b[1] - a[1]) * (c[0] - a[0]))

    def on_segment(a, b, point):
        return (min(a[0], b[0]) - 1.0e-12 <= point[0] <= max(a[0], b[0]) + 1.0e-12
                and min(a[1], b[1]) - 1.0e-12 <= point[1] <= max(a[1], b[1]) + 1.0e-12)

    o1 = orientation(first, second, third)
    o2 = orientation(first, second, fourth)
    o3 = orientation(third, fourth, first)
    o4 = orientation(third, fourth, second)
    if ((o1 > 1.0e-12 and o2 < -1.0e-12) or (o1 < -1.0e-12 and o2 > 1.0e-12)) and \
            ((o3 > 1.0e-12 and o4 < -1.0e-12) or (o3 < -1.0e-12 and o4 > 1.0e-12)):
        return True
    if abs(o1) <= 1.0e-12 and on_segment(first, second, third):
        return True
    if abs(o2) <= 1.0e-12 and on_segment(first, second, fourth):
        return True
    if abs(o3) <= 1.0e-12 and on_segment(third, fourth, first):
        return True
    if abs(o4) <= 1.0e-12 and on_segment(third, fourth, second):
        return True
    return False


def _validate_simple(points):
    count = len(points)
    if not 3 <= count <= 256:
        raise ValueError("POLYGON requires between 3 and 256 points")
    for index in range(count):
        following = points[(index + 1) % count]
        if (Vector((following[0] - points[index][0], following[1] - points[index][1])).length
                <= 1.0e-9):
            raise ValueError("POLYGON contains a zero-length edge")
    for index in range(count):
        previous = points[(index - 1) % count]
        current = points[index]
        following = points[(index + 1) % count]
        incoming = Vector((current[0] - previous[0], current[1] - previous[1]))
        outgoing = Vector((following[0] - current[0], following[1] - current[1]))
        cross = incoming.x * outgoing.y - incoming.y * outgoing.x
        if abs(cross) <= 1.0e-12 and incoming.dot(outgoing) < 0.0:
            raise ValueError("POLYGON contains a collinear reversal")
    for first in range(count):
        for second in range(first + 1, count):
            if second == first + 1 or (first == 0 and second == count - 1):
                continue
            if _segment_intersection(points[first], points[(first + 1) % count],
                                     points[second], points[(second + 1) % count]):
                raise ValueError("POLYGON must be simple; two non-adjacent edges intersect")
    if abs(_signed_area(points)) <= 1.0e-12:
        raise ValueError("POLYGON has zero area")


def _polygon_points(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError("points must be an array of [u, v] pairs")
    points = []
    for index, item in enumerate(value):
        point = _vec(item, 2, f"points[{index}]")
        points.append([point[0], point[1]])
    if len(points) >= 2 and all(abs(points[0][axis] - points[-1][axis]) <= 1.0e-9
                                for axis in range(2)):
        points.pop()
    _validate_simple(points)
    return _orient_ccw(points)


def _normalize_shape(shape, params, base=None):
    shape = _enum(shape, "shape", SHAPES)
    allowed = set(_SHAPE_KEYS[shape]) | set(_COMMON_KEYS)
    source = dict(base or {})
    source.update({key: value for key, value in params.items() if key in allowed})
    result = {}
    center = _vec(source.get("center", [0.0, 0.0]), 2, "center")
    depth = _number(source.get("depth", _DEFAULT_DEPTH), "depth", 1.0e-6, 1000.0)
    result["center"] = center
    result["depth"] = depth
    if shape == SHAPE_LINE:
        start = _vec(source.get("start", [-0.5, 0.0]), 2, "start")
        end = _vec(source.get("end", [0.5, 0.0]), 2, "end")
        line_width = _number(source.get("line_width", 0.1), "line_width", 1.0e-6, 1000.0)
        if math.hypot(end[0] - start[0], end[1] - start[1]) <= 1.0e-9:
            raise ValueError("LINE start and end must be distinct")
        result.update(start=start, end=end, line_width=line_width)
    elif shape == SHAPE_ARC:
        radius = _number(source.get("radius", 0.5), "radius", 1.0e-6, 1000.0)
        start_angle = _number(source.get("start_angle", 0.0), "start_angle", -1.0e6, 1.0e6)
        end_angle = _number(source.get("end_angle", math.pi), "end_angle", -1.0e6, 1.0e6)
        arc_width = _number(source.get("arc_width", 0.1), "arc_width", 1.0e-6, 1000.0)
        segments = _int(source.get("segments", _DEFAULT_SEGMENTS), "segments", 8, 512)
        sweep = abs(end_angle - start_angle)
        if sweep <= 1.0e-9:
            raise ValueError("ARC start_angle and end_angle must differ")
        if sweep >= math.tau - 1.0e-9:
            raise ValueError("ARC sweep must be less than a full circle; use CIRCLE instead")
        if arc_width >= radius * 2.0 - 1.0e-9:
            raise ValueError("arc_width must be smaller than twice the arc radius")
        result.update(radius=radius, start_angle=start_angle, end_angle=end_angle,
                      arc_width=arc_width, segments=segments)
    elif shape == SHAPE_CIRCLE:
        result["radius"] = _number(source.get("radius", 0.5), "radius", 1.0e-6, 1000.0)
        result["segments"] = _int(source.get("segments", _DEFAULT_SEGMENTS),
                                  "segments", 8, 512)
    elif shape == SHAPE_ROUND_SLOT:
        length = _number(source.get("length", 1.0), "length", 1.0e-6, 1000.0)
        width = _number(source.get("width", 0.3), "width", 1.0e-6, 1000.0)
        if length < width - 1.0e-9:
            raise ValueError("ROUND_SLOT length must be at least its width")
        result.update(length=length, width=width,
                      segments=_int(source.get("segments", _DEFAULT_SEGMENTS),
                                    "segments", 8, 512))
    elif shape == SHAPE_POLYGON:
        result["points"] = _polygon_points(source.get("points", _DEFAULT_POINTS))
    return result


def _line_outline(params):
    start = Vector(params["start"])
    end = Vector(params["end"])
    width = params["line_width"]
    direction = end - start
    length = direction.length
    if length <= 1.0e-9:
        raise ValueError("LINE has zero length")
    perpendicular = Vector((-direction.y, direction.x)) / length * (width * 0.5)
    points = [start + perpendicular, end + perpendicular,
              end - perpendicular, start - perpendicular]
    return [[point.x, point.y] for point in points], {
        "length": length,
        "width": width,
        "line_width": width,
    }


def _arc_outline(params):
    radius = params["radius"]
    width = params["arc_width"]
    start = params["start_angle"]
    end = params["end_angle"]
    sweep = end - start
    if abs(sweep) <= 1.0e-9:
        raise ValueError("ARC sweep is zero")
    inner = radius - width * 0.5
    outer = radius + width * 0.5
    if inner <= 1.0e-9:
        raise ValueError("ARC width reaches the center and would create a degenerate profile")
    full_segments = params["segments"]
    steps = max(2, int(math.ceil(full_segments * abs(sweep) / math.tau)))
    if steps > 8192:
        raise ValueError("ARC requested too many segments")

    def arc(radius_value):
        result = []
        for index in range(steps + 1):
            angle = start + sweep * index / steps
            result.append([math.cos(angle) * radius_value, math.sin(angle) * radius_value])
        return result

    points = arc(outer) + list(reversed(arc(inner)))
    return points, {
        "radius": radius,
        "width": width,
        "inner_radius": inner,
        "outer_radius": outer,
        "sweep": abs(sweep),
        "sweep_signed": sweep,
        "arc_length": radius * abs(sweep),
        "segments": params["segments"],
        "arc_segments": steps,
    }


def _circle_outline(params):
    radius = params["radius"]
    segments = params["segments"]
    points = [[math.cos(math.tau * index / segments) * radius,
               math.sin(math.tau * index / segments) * radius]
              for index in range(segments)]
    return points, {
        "radius": radius,
        "diameter": radius * 2.0,
        "segments": segments,
    }


def _slot_outline(params):
    length = params["length"]
    width = params["width"]
    radius = width * 0.5
    straight = max(0.0, length - width)
    if straight <= 1.0e-9:
        points, dimensions = _circle_outline({"radius": radius,
                                              "segments": params["segments"]})
        dimensions.update(length=length, width=width, straight_length=0.0)
        return points, dimensions
    half = straight * 0.5
    steps = max(4, int(math.ceil(params["segments"] * 0.5)))
    points = []
    for index in range(steps + 1):
        angle = -math.pi * 0.5 + math.pi * index / steps
        points.append([half + math.cos(angle) * radius, math.sin(angle) * radius])
    for index in range(steps + 1):
        angle = math.pi * 0.5 + math.pi * index / steps
        points.append([-half + math.cos(angle) * radius, math.sin(angle) * radius])
    return points, {
        "length": length,
        "width": width,
        "diameter": width,
        "radius": radius,
        "straight_length": straight,
        "segments": params["segments"],
        "cap_segments": steps,
    }


def _polygon_outline(params):
    points = [[point[0], point[1]] for point in params["points"]]
    return points, {
        "point_count": len(points),
        "nominal_area": abs(_signed_area(points)),
    }


_OUTLINE_BUILDERS = {
    SHAPE_LINE: _line_outline,
    SHAPE_ARC: _arc_outline,
    SHAPE_CIRCLE: _circle_outline,
    SHAPE_ROUND_SLOT: _slot_outline,
    SHAPE_POLYGON: _polygon_outline,
}


def _outline(shape, params):
    points, dimensions = _OUTLINE_BUILDERS[shape](params)
    center = Vector(params["center"])
    points = [[point[0] + center.x, point[1] + center.y] for point in points]
    points = _orient_ccw(points)
    area = _signed_area(points)
    if area <= 1.0e-12:
        raise ValueError(f"{shape} profile has zero area")
    us = [point[0] for point in points]
    vs = [point[1] for point in points]
    depth = params["depth"]
    dimensions.update(
        shape=shape,
        area=area,
        point_count=len(points),
        depth=depth,
        center=[center.x, center.y],
        bounds_u=[min(us), max(us)],
        bounds_v=[min(vs), max(vs)],
        bounds_w=[-depth * 0.5, depth * 0.5],
        size_u=max(us) - min(us),
        size_v=max(vs) - min(vs),
    )
    return points, dimensions


# ---------------------------------------------------------------------------
# Solid construction and validation


def _extrude_profile(name, points, depth):
    if len(points) < 3:
        raise ValueError("A cutter profile needs at least three points")
    if depth <= 0.0:
        raise ValueError("Cutter depth must be positive")
    if any(not all(math.isfinite(value) for value in point) for point in points):
        raise ValueError("Cutter profile contains non-finite coordinates")
    bm = bmesh.new()
    try:
        bottom = [bm.verts.new((point[0], point[1], -depth * 0.5)) for point in points]
        top = [bm.verts.new((point[0], point[1], depth * 0.5)) for point in points]
        bm.verts.ensure_lookup_table()
        top_face = bm.faces.new(tuple(top))
        bottom_face = bm.faces.new(tuple(reversed(bottom)))
        for index in range(len(points)):
            following = (index + 1) % len(points)
            bm.faces.new((bottom[index], bottom[following],
                          top[following], top[index]))
        bm.normal_update()
        if top_face.normal.z < 0.999999:
            raise ValueError("Cutter top cap winding is not outward along +N")
        if bottom_face.normal.z > -0.999999:
            raise ValueError("Cutter bottom cap winding is not outward along -N")
        mesh = bpy.data.meshes.new(name)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update(calc_edges=True)
    if mesh.validate(verbose=False):
        _remove_mesh(mesh)
        raise ValueError("Cutter mesh required Blender repair; profile is invalid")
    return mesh


def _face_on_z_plane(face, plane_z, tolerance=1.0e-9):
    """A cap candidate is a face whose every vertex is on the extreme Z plane.

    This is a per-face membership test; z_min/z_max are never widened globally.
    """
    return all(abs(vertex.co.z - plane_z) <= tolerance for vertex in face.verts)


def _mesh_stats(mesh):
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.normal_update()
        if not bm.verts:
            raise ValueError("Mesh is empty")
        for vertex in bm.verts:
            if not all(math.isfinite(value) for value in vertex.co):
                raise ValueError("Mesh contains non-finite coordinates")
        signed_volume = float(bm.calc_volume(signed=True))
        nonmanifold = sum(1 for edge in bm.edges if len(edge.link_faces) != 2)
        boundary = sum(1 for edge in bm.edges if len(edge.link_faces) == 1)
        zero_faces = sum(1 for face in bm.faces if face.calc_area() <= 1.0e-14)
        surface_area = sum(face.calc_area() for face in bm.faces)
        z_values = [vertex.co.z for vertex in bm.verts]
        z_min, z_max = min(z_values), max(z_values)
        top_faces = [face for face in bm.faces if _face_on_z_plane(face, z_max)]
        bottom_faces = [face for face in bm.faces if _face_on_z_plane(face, z_min)]
        top = max(top_faces, key=lambda face: face.calc_area()) if top_faces else None
        bottom = max(bottom_faces, key=lambda face: face.calc_area()) if bottom_faces else None
        xs = [vertex.co.x for vertex in bm.verts]
        ys = [vertex.co.y for vertex in bm.verts]
        stats = {
            "vertices": len(bm.verts),
            "edges": len(bm.edges),
            "faces": len(bm.faces),
            "signed_volume": signed_volume,
            "surface_area": surface_area,
            "nonmanifold_edges": nonmanifold,
            "boundary_edges": boundary,
            "zero_area_faces": zero_faces,
            "outward": bool(signed_volume > 0.0 and nonmanifold == 0 and zero_faces == 0),
            "bounds_local": [min(xs), min(ys), z_min, max(xs), max(ys), z_max],
            "top_cap": None if top is None else {
                "area": top.calc_area(),
                "normal": _vector_list(top.normal),
                "center": _vector_list(top.calc_center_median()),
            },
            "bottom_cap": None if bottom is None else {
                "area": bottom.calc_area(),
                "normal": _vector_list(bottom.normal),
                "center": _vector_list(bottom.calc_center_median()),
            },
        }
        return stats
    finally:
        bm.free()


def _validate_generated(mesh, dimensions):
    stats = _mesh_stats(mesh)
    if stats["nonmanifold_edges"] or stats["boundary_edges"]:
        raise ValueError("Generated cutter is not a closed manifold")
    if stats["zero_area_faces"]:
        raise ValueError("Generated cutter contains zero-area faces")
    if stats["signed_volume"] <= 0.0:
        raise ValueError("Generated cutter has inward face winding")
    expected_volume = dimensions["area"] * dimensions["depth"]
    tolerance = max(1.0e-6, abs(expected_volume) * 1.0e-5)
    if abs(stats["signed_volume"] - expected_volume) > tolerance:
        raise ValueError(
            f"Generated cutter volume {stats['signed_volume']:.9g} does not match "
            f"area x depth {expected_volume:.9g}")
    top = stats["top_cap"]
    bottom = stats["bottom_cap"]
    if top is None or bottom is None:
        raise ValueError("Generated cutter is missing a cap face")
    if top["normal"][2] < 0.999999 or bottom["normal"][2] > -0.999999:
        raise ValueError("Generated cutter cap winding is incorrect")
    if abs(top["area"] - dimensions["area"]) > max(1.0e-6, dimensions["area"] * 1.0e-5):
        raise ValueError("Generated cutter top cap area does not match its profile")
    stats["expected_volume"] = expected_volume
    stats["winding"] = "OUTWARD"
    return stats


def _make_solid(name, shape, params):
    points, dimensions = _outline(shape, params)
    mesh = _extrude_profile(name, points, params["depth"])
    try:
        stats = _validate_generated(mesh, dimensions)
    except Exception:
        _remove_mesh(mesh)
        raise
    return mesh, dimensions, stats


def _plane_mesh(size, name="HSW Work Plane Mesh"):
    half = size * 0.5
    bm = bmesh.new()
    try:
        points = [(-half, -half, 0.0), (half, -half, 0.0),
                  (half, half, 0.0), (-half, half, 0.0)]
        verts = [bm.verts.new(point) for point in points]
        bm.verts.ensure_lookup_table()
        face = bm.faces.new(tuple(verts))
        bm.normal_update()
        if face.normal.z < 0.999999:
            raise ValueError("Work plane mesh winding is not +Z")
        mesh = bpy.data.meshes.new(name)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update(calc_edges=True)
    if mesh.validate(verbose=False):
        _remove_mesh(mesh)
        raise ValueError("Work plane mesh required Blender repair")
    return mesh


def _plane_stats(mesh, size):
    if len(mesh.polygons) != 1 or len(mesh.vertices) != 4:
        raise ValueError("Work plane mesh must contain one quad")
    polygon = mesh.polygons[0]
    normal = Vector(polygon.normal)
    area = polygon.area
    if normal.z < 0.999999:
        raise ValueError("Work plane mesh normal does not point along local +Z")
    if abs(area - size * size) > max(1.0e-6, size * size * 1.0e-6):
        raise ValueError("Work plane mesh area does not match its size")
    return {
        "vertices": len(mesh.vertices),
        "faces": len(mesh.polygons),
        "area": area,
        "normal_local": _vector_list(normal),
        "winding": "CCW",
    }


# ---------------------------------------------------------------------------
# Metadata and ownership


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _load_json_prop(obj, key, label):
    raw = obj.get(key)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} is missing generated profile metadata")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} has damaged profile metadata") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} metadata must be a JSON object")
    return value


def _stamp_work_plane(obj, spec, frame, signature):
    obj[_ROLE] = ROLE_WORK_PLANE
    obj[_VERSION] = PROFILE_VERSION
    obj[_PLANE_SPEC] = _dump(spec)
    obj[_PLANE_MATRIX] = _matrix_list(frame["matrix"])
    obj[_PLANE_SIGNATURE] = signature


def _stamp_cutter(obj, spec, dimensions, signature, generation):
    obj[_ROLE] = ROLE_DIMENSION_CUTTER
    obj[_VERSION] = PROFILE_VERSION
    obj[_SHAPE] = spec["shape"]
    obj[_SPEC] = _dump(spec)
    obj[_DIMENSIONS] = _dump(dimensions)
    obj[_SIGNATURE] = signature
    obj[_GENERATION] = int(generation)
    obj[_WORK_PLANE] = spec.get("work_plane") or ""


def _plane_spec_from_object(obj):
    _require_role(obj, ROLE_WORK_PLANE, "Work plane")
    spec = _load_json_prop(obj, _PLANE_SPEC, "Work plane")
    return _plane_spec(spec, base=spec)


def _cutter_spec_from_object(obj):
    _require_role(obj, ROLE_DIMENSION_CUTTER, "Cutter")
    spec = _load_json_prop(obj, _SPEC, "Cutter")
    version = spec.get("version")
    if version != PROFILE_VERSION:
        raise ValueError(f"Cutter uses unsupported profile version: {version!r}")
    shape = _enum(spec.get("shape"), "Cutter shape", SHAPES)
    params = _normalize_shape(shape, spec.get("params", {}))
    work_plane = spec.get("work_plane")
    if work_plane is not None and not isinstance(work_plane, str):
        raise ValueError("Cutter work_plane metadata must be a string or null")
    plane_matrix = spec.get("plane_matrix")
    if plane_matrix is not None:
        plane_matrix = _matrix_list(_matrix_from_list(plane_matrix))
    return {
        "version": PROFILE_VERSION,
        "shape": shape,
        "params": params,
        "work_plane": work_plane or None,
        "plane_matrix": plane_matrix,
    }


def _cutter_dimensions_from_object(obj):
    try:
        value = _load_json_prop(obj, _DIMENSIONS, "Cutter")
    except ValueError:
        return None
    return value


def _require_work_plane(name, editable=False, validate_transform=True):
    obj = _view_object(name, {"MESH"}, "Work plane")
    _require_role(obj, ROLE_WORK_PLANE, "Work plane")
    if editable:
        _editable_object(obj, "Work plane")
    if validate_transform:
        matrix = obj.matrix_world.copy()
        rotation = matrix.to_3x3()
        determinant = rotation.determinant()
        if determinant <= 0.0 or abs(determinant - 1.0) > 1.0e-5:
            raise ValueError(
                f"Work plane {obj.name!r} has scale or a mirrored transform; apply scale first")
        for index in range(3):
            axis = Vector(rotation.col[index])
            if abs(axis.length - 1.0) > 1.0e-5:
                raise ValueError(
                    f"Work plane {obj.name!r} has a non-unit axis; apply scale first")
        if abs(Vector(rotation.col[0]).dot(Vector(rotation.col[1]))) > 1.0e-5:
            raise ValueError(f"Work plane {obj.name!r} is not orthogonal")
    return obj


def _require_cutter(name, editable=False):
    obj = _view_object(name, {"MESH"}, "Cutter")
    _require_role(obj, ROLE_DIMENSION_CUTTER, "Cutter")
    if editable:
        _editable_object(obj, "Cutter")
    return obj


def _consumers(cutter):
    rows = []
    for other in bpy.data.objects:
        for index, modifier in enumerate(other.modifiers):
            if modifier.type != "BOOLEAN":
                continue
            operand_type = getattr(modifier, "operand_type", "OBJECT")
            if operand_type == "OBJECT" and modifier.object == cutter:
                rows.append({
                    "object": other.name,
                    "modifier": modifier.name,
                    "index": index,
                    "operand_type": "OBJECT",
                })
            elif operand_type == "COLLECTION" and modifier.collection is not None:
                if cutter.name in modifier.collection.all_objects:
                    rows.append({
                        "object": other.name,
                        "modifier": modifier.name,
                        "index": index,
                        "operand_type": "COLLECTION",
                        "collection": modifier.collection.name,
                    })
    return sorted(rows, key=lambda row: (row["object"], row["index"]))


def _cutters_using_plane(name):
    result = []
    for obj in bpy.data.objects:
        if obj.get(_ROLE) != ROLE_DIMENSION_CUTTER:
            continue
        try:
            spec = _load_json_prop(obj, _SPEC, "Cutter")
        except ValueError:
            continue
        if spec.get("work_plane") == name:
            result.append(obj.name)
    return sorted(result)


def _frame_evidence(frame):
    return {
        "matrix": _matrix_list(frame["matrix"]),
        "origin": _vector_list(frame["origin"]),
        "x_axis": _vector_list(frame["x_axis"]),
        "y_axis": _vector_list(frame["y_axis"]),
        "normal": _vector_list(frame["normal"]),
        "mode": frame["mode"],
        "face_index": frame["face_index"],
        "edge_index": frame["edge_index"],
        "normal_source": frame["normal_source"],
        "offset": _vector_list(frame["offset"]),
        "flip": frame["flip"],
        "right_handed": True,
    }


# ---------------------------------------------------------------------------
# Work plane operations


def _create_work_plane(target, params, state, *, allow_existing_name=False):
    spec = _plane_spec(params)
    frame = _frame_for(target, spec)
    requested = _optional_name(params.get("name"), "Work plane name")
    if requested is not None:
        existing = bpy.data.objects.get(requested)
        if existing is not None and not allow_existing_name:
            raise ValueError(f"Object {requested!r} already exists")
        name = requested
    else:
        name = _unique_object_name("HSW Work Plane")
    mesh = _plane_mesh(spec["size"], f"{name} Mesh")
    state["undo"].append(lambda: _remove_mesh(mesh))
    stats = _plane_stats(mesh, spec["size"])
    obj = bpy.data.objects.new(name, mesh)
    state["undo"].append(lambda: _remove_mesh(mesh))
    state["undo"].append(lambda: _remove_object(obj))
    collection = _collection(target, params.get("collection"))
    collection.objects.link(obj)
    obj.matrix_world = frame["matrix"]
    bpy.context.view_layer.update()
    obj.show_in_front = True
    obj.hide_render = True
    obj.display_type = "WIRE"
    signature = _signature(mesh)
    _stamp_work_plane(obj, spec, frame, signature)
    return {
        "obj": obj,
        "mesh": mesh,
        "spec": spec,
        "frame": frame,
        "stats": stats,
        "signature": signature,
    }


def _plane_result(plane, target, created=False):
    obj = plane["obj"]
    return {
        "work_plane": obj.name,
        "target": target.name,
        "created": created,
        "size": plane["spec"]["size"],
        "spec": plane["spec"],
        "mesh_stats": plane["stats"],
        "signature": plane["signature"],
        **_frame_evidence(plane["frame"]),
    }


def _handler_work_plane_create(target, params, state):
    target = _target_mesh(target, write=False)
    plane = _create_work_plane(target, params, state)
    return _plane_result(plane, target, True)


def _swap_mesh(obj, new_mesh, old_mesh, stamp, snapshot, state):
    state["undo"].append(lambda: _remove_mesh(new_mesh))
    state["undo"].append(lambda: setattr(obj, "data", old_mesh))
    state["undo"].append(lambda: _restore_props(obj, snapshot))
    obj.data = new_mesh
    stamp()

    def finalize():
        if old_mesh.users == 0:
            old_name = old_mesh.name
            _remove_mesh(old_mesh)
            try:
                if new_mesh.name != old_name:
                    new_mesh.name = old_name
            except Exception:
                pass

    state["finalize"].append(finalize)


def _handler_work_plane_update(target, params, state):
    target = _target_mesh(target, write=False)
    name = _name(params["name"], "Work plane name")
    obj = _require_work_plane(name, editable=True, validate_transform=False)
    old_spec = _plane_spec_from_object(obj)
    old_snapshot = _snapshot_props(obj, _PLANE_PROPS)
    old_matrix = obj.matrix_world.copy()
    old_signature = obj.get(_PLANE_SIGNATURE)
    current_signature = _signature(obj.data)
    stored_matrix = old_snapshot.get(_PLANE_MATRIX)
    try:
        stored_matrix = _matrix_from_list(stored_matrix) if stored_matrix is not None else None
    except ValueError:
        stored_matrix = None
    user_transformed = (stored_matrix is None
                        or not _matrix_close(old_matrix, stored_matrix))
    if user_transformed and not _bool(params.get("allow_user_transform", False),
                                     "allow_user_transform"):
        raise ValueError(
            f"Work plane {obj.name!r} has a manually changed transform; "
            "pass allow_user_transform=True to replace it")
    new_spec = _plane_spec(params, base=old_spec)
    frame = _frame_for(target, new_spec)
    mesh_edited = isinstance(old_signature, str) and current_signature != old_signature
    size_changed = abs(new_spec["size"] - old_spec["size"]) > 1.0e-12
    state["undo"].append(lambda: setattr(obj, "matrix_world", old_matrix))

    mesh_replaced = False
    if size_changed or (mesh_edited and "size" in params):
        if mesh_edited and not _bool(params.get("allow_user_edit", False), "allow_user_edit"):
            raise ValueError(
                f"Work plane {obj.name!r} has manual mesh edits; "
                "pass allow_user_edit=True to rebuild it")
        new_mesh = _plane_mesh(new_spec["size"], f"{obj.name} Mesh")
        state["undo"].append(lambda: _remove_mesh(new_mesh))
        stats = _plane_stats(new_mesh, new_spec["size"])
        signature = _signature(new_mesh)

        def stamp():
            _stamp_work_plane(obj, new_spec, frame, signature)
            obj.matrix_world = frame["matrix"]

        _swap_mesh(obj, new_mesh, obj.data, stamp, old_snapshot, state)
        mesh_replaced = True
    else:
        stored_signature_for_update = (old_signature if isinstance(old_signature, str)
                                       else current_signature)
        _stamp_work_plane(obj, new_spec, frame, stored_signature_for_update)
        obj.matrix_world = frame["matrix"]
        try:
            stats = _plane_stats(obj.data, new_spec["size"])
        except Exception as error:
            stats = {"error": str(error)}
        signature = current_signature

    return {
        "work_plane": obj.name,
        "target": target.name,
        "size": new_spec["size"],
        "spec": new_spec,
        "mesh_stats": stats,
        "signature": signature,
        "mesh_replaced": mesh_replaced,
        "user_transformed": user_transformed,
        "user_edit_detected": mesh_edited,
        "referenced_cutters": _cutters_using_plane(obj.name),
        **_frame_evidence(frame),
    }


def _handler_work_plane_inspect(target, params, state):
    name = _name(params["name"], "Work plane name")
    obj = _require_work_plane(name, editable=False, validate_transform=False)
    spec = _plane_spec_from_object(obj)
    matrix = obj.matrix_world.copy()
    user_transformed = True
    stored_matrix = obj.get(_PLANE_MATRIX)
    stored_values = None
    if stored_matrix is not None:
        try:
            stored_values = list(stored_matrix)
        except TypeError:
            stored_values = None
    if stored_values is not None and len(stored_values) == 16:
        try:
            user_transformed = not _matrix_close(matrix, _matrix_from_list(stored_values))
        except Exception:
            user_transformed = True
    signature = _signature(obj.data)
    stored_signature = obj.get(_PLANE_SIGNATURE)
    try:
        stats = _plane_stats(obj.data, spec["size"])
    except Exception as error:
        stats = {"error": str(error)}
    return {
        "work_plane": obj.name,
        "spec": spec,
        "size": spec["size"],
        "matrix": _matrix_list(matrix),
        "stored_matrix": (_matrix_list(_matrix_from_list(stored_values))
                          if stored_values is not None and len(stored_values) == 16 else None),
        "origin": _vector_list(matrix.translation),
        "x_axis": _vector_list(matrix.to_3x3().col[0]),
        "y_axis": _vector_list(matrix.to_3x3().col[1]),
        "normal": _vector_list(matrix.to_3x3().col[2]),
        "mesh_stats": stats,
        "signature": signature,
        "stored_signature": stored_signature,
        "user_transformed": user_transformed,
        "mesh_edited": signature != stored_signature,
        "referenced_cutters": _cutters_using_plane(obj.name),
    }


# ---------------------------------------------------------------------------
# Dimension cutter operations


def _cutter_spec(shape, params, work_plane, plane_matrix):
    return {
        "version": PROFILE_VERSION,
        "shape": shape,
        "params": params,
        "work_plane": work_plane,
        "plane_matrix": _matrix_list(plane_matrix),
    }


def _cutter_result(obj, spec, dimensions, stats, signature, *, action,
                   plane_created=False, previous=None, extra=None):
    data_users = obj.data.users - int(bool(obj.data.use_fake_user))
    result = {
        "cutter": obj.name,
        "mesh": obj.data.name,
        "shape": spec["shape"],
        "action": action,
        "work_plane": spec.get("work_plane"),
        "plane_created": plane_created,
        "matrix": _matrix_list(obj.matrix_world),
        "dimensions": dimensions,
        "mesh_stats": stats,
        "signature": signature,
        "spec": spec,
        "data_users": data_users,
        "independent_data": data_users <= 1,
        "face_winding": "OUTWARD" if stats.get("outward") else "CHECK",
        "consumers": _consumers(obj),
        "generation": int(obj.get(_GENERATION, 0)),
    }
    if previous is not None:
        result["before"] = previous
    if extra:
        result.update(extra)
    return result


def _install_cutter_object(name, mesh, matrix, spec, dimensions, state,
                           collection, signature, generation=1):
    if bpy.data.objects.get(name) is not None:
        raise ValueError(f"Object {name!r} already exists")
    obj = bpy.data.objects.new(name, mesh)
    state["undo"].append(lambda: _remove_mesh(mesh))
    state["undo"].append(lambda: _remove_object(obj))
    collection.objects.link(obj)
    obj.matrix_world = matrix
    bpy.context.view_layer.update()
    _stamp_cutter(obj, spec, dimensions, signature, generation)
    return obj


def _clone_cutter(target, params, state):
    source = _require_cutter(params["clone_from"], editable=False)
    shape = _enum(params["shape"], "shape", SHAPES)
    source_spec = deepcopy(_cutter_spec_from_object(source))
    source_spec["plane_matrix"] = _matrix_list(source.matrix_world)
    if shape != source_spec["shape"]:
        raise ValueError(
            f"clone_from source {source.name!r} is {source_spec['shape']}, not {shape}")
    disallowed = set(params) - {"name", "shape", "clone_from", "collection"}
    if disallowed:
        raise ValueError(
            f"clone_from only accepts name, shape, clone_from and collection; "
            f"got {', '.join(sorted(disallowed))}")
    name = _name(params["name"], "Cutter name")
    if bpy.data.objects.get(name) is not None:
        raise ValueError(f"Object {name!r} already exists")
    mesh = source.data.copy()
    if mesh.library or mesh.override_library:
        raise ValueError("Cloned cutter mesh must be local")
    state["undo"].append(lambda: _remove_mesh(mesh))
    collection = _collection(source, params.get("collection"))
    signature = source.get(_SIGNATURE)
    if not isinstance(signature, str) or len(signature) != 64:
        signature = _signature(mesh)
    dimensions = _cutter_dimensions_from_object(source)
    if dimensions is None:
        dimensions = _outline(source_spec["shape"], source_spec["params"])[1]
    generation = source.get(_GENERATION)
    generation = int(generation) + 1 if type(generation) is int else 1
    obj = _install_cutter_object(name, mesh, source.matrix_world.copy(), source_spec,
                                 dimensions, state, collection, signature, generation)
    return _cutter_result(obj, source_spec, dimensions,
                          _mesh_stats(mesh), _signature(mesh), action="CLONE",
                          extra={"cloned_from": source.name, "source_unchanged": True,
                                 "target": target.name})


def _resolve_plane_for_cutter(target, params, state):
    work_plane = _optional_name(params.get("work_plane"), "Work plane name")
    if work_plane is not None:
        conflicts = [key for key in ("plane_mode", "plane_name", "plane_size",
                                     "plane_offset", "plane_flip", "face_index",
                                     "edge_index") if key in params]
        if conflicts:
            raise ValueError(
                f"work_plane cannot be combined with auto-plane fields: {', '.join(conflicts)}")
        plane = _require_work_plane(work_plane, editable=True)
        return plane, plane.matrix_world.copy(), None, False

    target = _target_mesh(target, write=False)
    plane_params = {
        "mode": params.get("plane_mode", PLANE_OBJECT_XY),
        "size": params.get("plane_size", 2.0),
        "offset": params.get("plane_offset", [0.0, 0.0, 0.0]),
        "flip": params.get("plane_flip", False),
    }
    plane_name = _optional_name(params.get("plane_name"), "Work plane name")
    if plane_name is not None:
        plane_params["name"] = plane_name
    if "face_index" in params:
        plane_params["face_index"] = params["face_index"]
    if "edge_index" in params:
        plane_params["edge_index"] = params["edge_index"]
    plane = _create_work_plane(target, plane_params, state)
    return plane["obj"], plane["frame"]["matrix"], plane, True


def _handler_dimension_create(target, params, state):
    if _optional_name(params.get("clone_from"), "Clone source") is not None:
        return _clone_cutter(target, params, state)
    shape = _enum(params["shape"], "shape", SHAPES)
    name = _name(params["name"], "Cutter name")
    if bpy.data.objects.get(name) is not None:
        raise ValueError(f"Object {name!r} already exists")
    wrong = (set(params) & set(_ALL_SHAPE_KEYS)) - (
        set(_SHAPE_KEYS[shape]) | set(_COMMON_KEYS))
    if wrong:
        raise ValueError(f"Parameters are not used by {shape}: {', '.join(sorted(wrong))}")
    plane_obj, matrix, plane, plane_created = _resolve_plane_for_cutter(target, params, state)
    shape_params = _normalize_shape(shape, params)
    mesh, dimensions, stats = _make_solid(f"{name} Mesh", shape, shape_params)
    state["undo"].append(lambda: _remove_mesh(mesh))
    signature = _signature(mesh)
    spec = _cutter_spec(shape, shape_params, plane_obj.name, matrix)
    collection = _collection(target, params.get("collection"))
    obj = _install_cutter_object(name, mesh, matrix, spec, dimensions, state,
                                 collection, signature, 1)
    return _cutter_result(obj, spec, dimensions, stats, signature,
                          action="CREATE", plane_created=plane_created,
                          extra={"target": target.name,
                                 "plane_mesh_stats": None if plane is None else plane["stats"]})


def _copy_cutter_from_spec(source, spec, params, mesh, dimensions, stats, state):
    name = _optional_name(params.get("copy_name"), "Copy name")
    if name is None:
        name = _unique_object_name(f"{source.name} Resized")
    elif bpy.data.objects.get(name) is not None:
        raise ValueError(f"Object {name!r} already exists")
    collection = _collection(source, params.get("collection"))
    for material in source.data.materials:
        mesh.materials.append(material)
    new_spec = deepcopy(spec)
    new_spec["params"] = _normalize_shape(spec["shape"], spec["params"])
    new_spec["plane_matrix"] = _matrix_list(source.matrix_world)
    generation = source.get(_GENERATION)
    generation = int(generation) + 1 if type(generation) is int else 1
    signature = _signature(mesh)
    obj = _install_cutter_object(name, mesh, source.matrix_world.copy(), new_spec,
                                 dimensions, state, collection, signature, generation)
    return _cutter_result(
        obj, new_spec, dimensions, stats, signature, action="COPY",
        extra={"copied_from": source.name, "source_unchanged": True,
               "source_signature": _signature(source.data)})


def _replace_cutter_mesh(cutter, spec, dimensions, stats, mesh, state,
                         *, shared_data, discarded_user_edits, action):
    old_mesh = cutter.data
    old_signature = cutter.get(_SIGNATURE)
    old_dimensions = _cutter_dimensions_from_object(cutter)
    old_generation = cutter.get(_GENERATION)
    old_generation = int(old_generation) if type(old_generation) is int else 0
    old_snapshot = _snapshot_props(cutter, _PROFILE_PROPS)
    old_data_users = old_mesh.users - int(bool(old_mesh.use_fake_user))
    for material in old_mesh.materials:
        mesh.materials.append(material)
    signature = _signature(mesh)

    def stamp():
        _stamp_cutter(cutter, spec, dimensions, signature, old_generation + 1)

    _swap_mesh(cutter, mesh, old_mesh, stamp, old_snapshot, state)
    consumers = _consumers(cutter)
    result = _cutter_result(
        cutter, spec, dimensions, stats, signature, action=action,
        extra={
            "before": {
                "dimensions": old_dimensions,
                "mesh": old_mesh.name,
                "signature": old_signature,
                "generation": old_generation,
            },
            "shared_data_unlinked": bool(shared_data),
            "shared_data_before": bool(shared_data),
            "data_users_before": old_data_users,
            "discarded_user_edits": bool(discarded_user_edits),
            "source_unchanged": False,
        })
    result["mesh"] = old_mesh.name if old_mesh.users == 0 else cutter.data.name
    result["consumers"] = consumers
    return result


def _handler_dimension_resize(target, params, state):
    name = _name(params["name"], "Cutter name")
    cutter = _require_cutter(name, editable=True)
    old_spec = _cutter_spec_from_object(cutter)
    shape = old_spec["shape"]
    if "shape" in params:
        requested_shape = _enum(params["shape"], "shape", SHAPES)
        if requested_shape != shape:
            raise ValueError(
                f"Cannot change shape in place ({shape} -> {requested_shape}); create a new cutter")
    allowed = set(_SHAPE_KEYS[shape]) | set(_COMMON_KEYS)
    wrong = (set(params) & set(_ALL_SHAPE_KEYS)) - allowed
    if wrong:
        raise ValueError(f"Parameters are not used by {shape}: {', '.join(sorted(wrong))}")
    supplied = set(params) & allowed
    if not supplied:
        raise ValueError(
            "Give at least one dimension parameter to resize; only policies were supplied")
    merged = dict(old_spec["params"])
    merged.update({key: params[key] for key in supplied})
    new_params = _normalize_shape(shape, merged)
    mesh, dimensions, stats = _make_solid(f"{name} Resized Mesh", shape, new_params)
    state["undo"].append(lambda: _remove_mesh(mesh))

    current_signature = _signature(cutter.data)
    stored_signature = cutter.get(_SIGNATURE)
    if not isinstance(stored_signature, str) or len(stored_signature) != 64:
        _remove_mesh(mesh)
        raise ValueError(f"Cutter {cutter.name!r} is missing a valid geometry signature")
    user_edited = current_signature != stored_signature
    expected = _optional_text(params.get("expected_signature"), "expected_signature")
    if expected is not None and expected != current_signature:
        _remove_mesh(mesh)
        raise ValueError(
            f"Stale cutter signature: expected {expected!r}, current {current_signature!r}")
    policy = _enum(params.get("user_edit_policy", USER_EDIT_REFUSE),
                   "user_edit_policy", USER_EDIT_POLICIES)
    if user_edited and policy == USER_EDIT_REFUSE:
        _remove_mesh(mesh)
        raise ValueError(
            f"Cutter {cutter.name!r} has manual mesh edits; "
            "pass user_edit_policy='COPY' to keep them or 'REPLACE' to discard them")
    raw_data_users = cutter.data.users
    data_users = raw_data_users - int(bool(cutter.data.use_fake_user))
    shared_data = data_users > 1
    unlink_shared = _bool(params.get("unlink_shared", False), "unlink_shared")
    if shared_data and policy != USER_EDIT_COPY and not unlink_shared:
        _remove_mesh(mesh)
        raise ValueError(
            f"Cutter {cutter.name!r} shares mesh data with {data_users - 1} other user(s); "
            "pass unlink_shared=True to make this object independent")

    new_spec = deepcopy(old_spec)
    new_spec["params"] = new_params
    new_spec["plane_matrix"] = _matrix_list(cutter.matrix_world)
    if user_edited and policy == USER_EDIT_COPY:
        return _copy_cutter_from_spec(cutter, new_spec, params, mesh, dimensions,
                                      stats, state)

    return _replace_cutter_mesh(
        cutter, new_spec, dimensions, stats, mesh, state,
        shared_data=shared_data,
        discarded_user_edits=user_edited and policy == USER_EDIT_REPLACE,
        action="RESIZE")


def _handler_dimension_inspect(target, params, state):
    name = _name(params["name"], "Cutter name")
    cutter = _require_cutter(name, editable=False)
    spec = _cutter_spec_from_object(cutter)
    stored_dimensions = _cutter_dimensions_from_object(cutter)
    stored_signature = cutter.get(_SIGNATURE)
    current_signature = _signature(cutter.data)
    user_edited = not isinstance(stored_signature, str) or current_signature != stored_signature
    stats_error = None
    try:
        stats = _mesh_stats(cutter.data)
    except Exception as error:
        stats = {"error": str(error)}
        stats_error = str(error)
    data_users = cutter.data.users - int(bool(cutter.data.use_fake_user))
    return {
        "cutter": cutter.name,
        "mesh": cutter.data.name,
        "shape": spec["shape"],
        "spec": spec,
        "stored_dimensions": stored_dimensions,
        "dimensions": None if user_edited else stored_dimensions,
        "dimensions_valid": not user_edited and stored_dimensions is not None,
        "matrix": _matrix_list(cutter.matrix_world),
        "work_plane": spec.get("work_plane"),
        "signature": current_signature,
        "stored_signature": stored_signature if isinstance(stored_signature, str) else None,
        "user_edited": user_edited,
        "generation": int(cutter.get(_GENERATION, 0)),
        "data_users": data_users,
        "shared_data": data_users > 1,
        "independent_data": data_users <= 1,
        "consumers": _consumers(cutter),
        "mesh_stats": stats,
        "mesh_stats_error": stats_error,
        "face_winding": "OUTWARD" if stats.get("outward") else "CHECK",
        "measured_bounds": stats.get("bounds_local"),
    }


# ---------------------------------------------------------------------------
# Public execution


_HANDLERS = {
    TOOL_WORK_PLANE_CREATE: _handler_work_plane_create,
    TOOL_WORK_PLANE_UPDATE: _handler_work_plane_update,
    TOOL_WORK_PLANE_INSPECT: _handler_work_plane_inspect,
    TOOL_DIMENSION_CREATE: _handler_dimension_create,
    TOOL_DIMENSION_RESIZE: _handler_dimension_resize,
    TOOL_DIMENSION_INSPECT: _handler_dimension_inspect,
}


def execute(obj, tool, params):
    """Execute one profile tool and return JSON-compatible evidence.

    ``obj`` is the active object supplied by the caller.  Work-plane creation
    and automatic cutter placement use it as the target mesh.  Resize and
    inspect operate on the object named by ``params['name']`` and use ``obj``
    only as call context.
    """
    if not isinstance(tool, str) or tool not in TOOLS:
        raise ValueError(f"Unknown profile tool: {tool!r}")
    if not isinstance(obj, bpy.types.Object):
        raise ValueError("A Blender object context is required")
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object")
    spec = TOOLS[tool]
    allowed = set(spec["required"]) | set(spec["optional"])
    missing = set(spec["required"]) - set(params)
    extra = set(params) - allowed
    if missing or extra:
        raise ValueError(f"Missing fields {sorted(missing)} or unknown fields {sorted(extra)}")
    handler = _HANDLERS[tool]
    if spec["read_only"]:
        payload = handler(obj, params, None)
        return _result(tool, obj, payload)
    try:
        with _transaction() as state:
            payload = handler(obj, params, state)
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(f"{tool} failed: {error}") from error
    return _result(tool, obj, payload)


# ---------------------------------------------------------------------------
# Sidebar state and non-modal operators


SCENE_SETTINGS = "hsw_profile_settings"

_PLANE_ITEMS = (
    (PLANE_OBJECT_XY, "对象本地 XY", "目标本地 +X/+Y，法向 +Z"),
    (PLANE_OBJECT_XZ, "对象本地 XZ", "目标本地 +X/-Z，法向 +Y"),
    (PLANE_OBJECT_YZ, "对象本地 YZ", "目标本地 +Y/+Z，法向 +X"),
    (PLANE_FACE, "按面", "使用选中面的世界法向和首条边界方向"),
    (PLANE_DIRECTION_EDGE, "按方向边", "使用边方向作为平面 X，邻接面或对象局部轴作为法向"),
)
_SHAPE_ITEMS = (
    (SHAPE_LINE, "线段", "带矩形截面的线段刀体"),
    (SHAPE_ARC, "圆弧", "带径向宽度的圆弧刀体"),
    (SHAPE_CIRCLE, "圆", "圆形刀体"),
    (SHAPE_ROUND_SLOT, "圆端槽", "两端为半圆的槽形刀体"),
    (SHAPE_POLYGON, "简单多边形", "无自交的闭合多边形刀体"),
)
_POLICY_ITEMS = (
    (USER_EDIT_REFUSE, "拒绝", "检测到手改网格时停止"),
    (USER_EDIT_COPY, "复制", "保留原刀体，生成一个按新尺寸重建的独立副本"),
    (USER_EDIT_REPLACE, "覆盖", "显式丢弃手改网格并原位重建"),
)


def _settings(context):
    scene = getattr(context, "scene", None)
    return getattr(scene, SCENE_SETTINGS, None) if scene is not None else None


def _poll_mesh(_self, obj):
    return getattr(obj, "type", None) == "MESH"


def _poll_work_plane(_self, obj):
    return obj is not None and obj.get(_ROLE) == ROLE_WORK_PLANE


def _poll_cutter(_self, obj):
    return obj is not None and obj.get(_ROLE) == ROLE_DIMENSION_CUTTER


def _set_status(settings, text, kind="INFO"):
    if settings is None:
        return
    settings.status = str(text)[:4000]
    settings.status_kind = kind if kind in {"INFO", "SUCCESS", "WARNING", "ERROR"} else "INFO"


def _fail(operator, settings, error):
    text = str(error) or error.__class__.__name__
    _set_status(settings, text, "ERROR")
    if settings is not None:
        settings.last_result = ""
    operator.report({"ERROR"}, text)
    return {"CANCELLED"}


def _succeed(operator, settings, result, message):
    _set_status(settings, message, "SUCCESS")
    if settings is not None:
        settings.last_result = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:4000]
    operator.report({"INFO"}, message)
    return {"FINISHED"}


def _panel_target(settings, context, require_mesh=False):
    target = settings.target_object
    if target is None:
        target = context.object if isinstance(context.object, bpy.types.Object) else None
    if target is None:
        target = settings.plane_object
    if target is None:
        raise ValueError("Choose a target object or work plane")
    if require_mesh and target.type != "MESH":
        raise ValueError("Choose a mesh target for this operation")
    return target


def _plane_params(settings, include_name=False):
    mode = settings.plane_mode
    params = {
        "mode": mode,
        "size": settings.plane_size,
        "offset": list(settings.plane_offset),
        "flip": bool(settings.plane_flip),
    }
    if include_name and settings.plane_name.strip():
        requested = settings.plane_name.strip()
        selected = settings.plane_object.name if settings.plane_object is not None else None
        if requested != selected:
            params["name"] = requested
    if mode == PLANE_FACE:
        params["face_index"] = int(settings.plane_face)
    elif mode == PLANE_DIRECTION_EDGE:
        params["edge_index"] = int(settings.plane_edge)
        if settings.plane_use_face:
            params["face_index"] = int(settings.plane_face)
    return params


def _polygon_from_panel(settings):
    raw = settings.polygon_points.strip()
    if not raw:
        raise ValueError("Polygon points JSON is empty")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Polygon points JSON is invalid at line {error.lineno}, column {error.colno}") from error
    if isinstance(value, dict) and "points" in value:
        value = value["points"]
    return value


def _cutter_shape_params(settings, shape):
    params = {
        "center": list(settings.cutter_center),
        "depth": settings.cutter_depth,
    }
    if shape == SHAPE_LINE:
        params.update(start=list(settings.line_start), end=list(settings.line_end),
                      line_width=settings.line_width)
    elif shape == SHAPE_ARC:
        params.update(radius=settings.radius,
                      start_angle=settings.arc_start,
                      end_angle=settings.arc_end,
                      arc_width=settings.arc_width,
                      segments=settings.cutter_segments)
    elif shape == SHAPE_CIRCLE:
        params.update(radius=settings.radius, segments=settings.cutter_segments)
    elif shape == SHAPE_ROUND_SLOT:
        params.update(length=settings.slot_length, width=settings.slot_width,
                      segments=settings.cutter_segments)
    elif shape == SHAPE_POLYGON:
        params["points"] = _polygon_from_panel(settings)
    else:
        raise ValueError(f"Unknown cutter shape: {shape!r}")
    return params


def _cutter_create_params(settings):
    shape = settings.cutter_shape
    name = settings.cutter_name.strip()
    selected = settings.cutter_object.name if settings.cutter_object is not None else None
    if not name or name == selected:
        name = _unique_object_name(f"HSW {shape.replace('_', ' ').title()} Cutter")
    params = {"name": name, "shape": shape, **_cutter_shape_params(settings, shape)}
    if settings.plane_object is not None:
        params["work_plane"] = settings.plane_object.name
    else:
        params.update({
            "plane_mode": settings.plane_mode,
            "plane_size": settings.plane_size,
            "plane_offset": list(settings.plane_offset),
            "plane_flip": bool(settings.plane_flip),
        })
        if settings.plane_name.strip():
            params["plane_name"] = settings.plane_name.strip()
        if settings.plane_mode == PLANE_FACE:
            params["face_index"] = int(settings.plane_face)
        elif settings.plane_mode == PLANE_DIRECTION_EDGE:
            params["edge_index"] = int(settings.plane_edge)
            if settings.plane_use_face:
                params["face_index"] = int(settings.plane_face)
    return params


def _cutter_resize_params(settings, cutter):
    shape = settings.cutter_shape
    params = {
        "name": cutter.name,
        "shape": shape,
        "user_edit_policy": settings.user_edit_policy,
        "unlink_shared": bool(settings.unlink_shared),
        **_cutter_shape_params(settings, shape),
    }
    if settings.expected_signature.strip():
        params["expected_signature"] = settings.expected_signature.strip()
    if settings.copy_name.strip():
        params["copy_name"] = settings.copy_name.strip()
    return params


def _load_plane_into_settings(settings, plane):
    spec = _plane_spec_from_object(plane)
    settings.plane_name = plane.name
    settings.plane_mode = spec["mode"]
    settings.plane_size = spec["size"]
    settings.plane_offset = spec["offset"]
    settings.plane_flip = bool(spec["flip"])
    if "face_index" in spec:
        settings.plane_face = int(spec["face_index"])
        settings.plane_use_face = True
    else:
        settings.plane_use_face = False
    if "edge_index" in spec:
        settings.plane_edge = int(spec["edge_index"])


def _load_cutter_into_settings(settings, cutter):
    spec = _cutter_spec_from_object(cutter)
    params = spec["params"]
    shape = spec["shape"]
    settings.cutter_name = cutter.name
    settings.cutter_shape = shape
    settings.cutter_center = params["center"]
    settings.cutter_depth = params["depth"]
    if shape == SHAPE_LINE:
        settings.line_start = params["start"]
        settings.line_end = params["end"]
        settings.line_width = params["line_width"]
    elif shape == SHAPE_ARC:
        settings.radius = params["radius"]
        settings.arc_start = params["start_angle"]
        settings.arc_end = params["end_angle"]
        settings.arc_width = params["arc_width"]
        settings.cutter_segments = params["segments"]
    elif shape == SHAPE_CIRCLE:
        settings.radius = params["radius"]
        settings.cutter_segments = params["segments"]
    elif shape == SHAPE_ROUND_SLOT:
        settings.slot_length = params["length"]
        settings.slot_width = params["width"]
        settings.cutter_segments = params["segments"]
    elif shape == SHAPE_POLYGON:
        settings.polygon_points = json.dumps(params["points"], ensure_ascii=False,
                                             separators=(",", ":"))
    settings.loaded_cutter = cutter.name
    work_plane = spec.get("work_plane")
    if work_plane:
        plane = bpy.data.objects.get(work_plane)
        if plane is not None:
            settings.plane_object = plane


def _on_plane_object_update(self, context):
    if self.plane_object is None:
        return
    try:
        _load_plane_into_settings(self, self.plane_object)
        _set_status(self, f"Loaded work plane {self.plane_object.name}", "INFO")
    except Exception as error:
        _set_status(self, str(error), "ERROR")


def _on_cutter_object_update(self, context):
    if self.cutter_object is None:
        return
    try:
        _load_cutter_into_settings(self, self.cutter_object)
        _set_status(self, f"Loaded cutter {self.cutter_object.name}", "INFO")
    except Exception as error:
        _set_status(self, str(error), "ERROR")


class HSW_PG_profile_settings(bpy.types.PropertyGroup):
    target_object: bpy.props.PointerProperty(
        type=bpy.types.Object, name="目标网格",
        description="工作平面和自动刀体使用的网格对象；留空时使用活动对象。",
        poll=_poll_mesh,
    )
    plane_object: bpy.props.PointerProperty(
        type=bpy.types.Object, name="工作平面",
        description="现有工作平面；创建刀体时引用它，更新/检查时操作它。",
        poll=_poll_work_plane, update=_on_plane_object_update,
    )
    plane_mode: bpy.props.EnumProperty(
        name="平面模式", description="工作平面的来源和朝向。",
        items=_PLANE_ITEMS, default=PLANE_OBJECT_XY,
    )
    plane_name: bpy.props.StringProperty(name="平面名称", default="")
    plane_size: bpy.props.FloatProperty(
        name="平面尺寸", description="工作平面方形边长。",
        default=2.0, min=0.01, max=1000.0, subtype="DISTANCE", unit="LENGTH",
    )
    plane_offset: bpy.props.FloatVectorProperty(
        name="平面偏移", description="沿工作平面 U/V/N 的局部偏移。",
        size=3, default=(0.0, 0.0, 0.0), subtype="TRANSLATION", unit="LENGTH",
    )
    plane_flip: bpy.props.BoolProperty(name="翻转法向", default=False)
    plane_face: bpy.props.IntProperty(
        name="面索引", description="按面或方向边上下文使用的网格面索引。",
        default=0, min=0, max=1000000,
    )
    plane_edge: bpy.props.IntProperty(
        name="边索引", description="方向边模式使用的网格边索引。",
        default=0, min=0, max=1000000,
    )
    plane_use_face: bpy.props.BoolProperty(
        name="使用邻接面法向", description="方向边模式下用面索引的法向作为平面法向。",
        default=True,
    )
    allow_user_transform: bpy.props.BoolProperty(
        name="覆盖手转变换", description="允许重建时覆盖手动移动/旋转的工作平面。",
        default=False,
    )
    allow_user_edit: bpy.props.BoolProperty(
        name="覆盖手改网格", description="允许重建时丢弃手动修改的工作平面网格。",
        default=False,
    )

    cutter_object: bpy.props.PointerProperty(
        type=bpy.types.Object, name="刀体",
        description="用于原位改尺寸或检查的尺寸刀体。",
        poll=_poll_cutter, update=_on_cutter_object_update,
    )
    cutter_name: bpy.props.StringProperty(name="新刀体名称", default="HSW Cutter")
    cutter_shape: bpy.props.EnumProperty(
        name="形状", description="刀体截面形状。",
        items=_SHAPE_ITEMS, default=SHAPE_CIRCLE,
    )
    cutter_center: bpy.props.FloatVectorProperty(
        name="中心偏移", description="截面在工作平面 UV 内的偏移。",
        size=2, default=(0.0, 0.0), unit="LENGTH",
    )
    cutter_depth: bpy.props.FloatProperty(
        name="厚度", description="沿工作平面法向的刀体厚度。",
        default=_DEFAULT_DEPTH, min=1.0e-6, max=1000.0,
        subtype="DISTANCE", unit="LENGTH",
    )
    cutter_segments: bpy.props.IntProperty(
        name="整圆段数", description="圆、圆弧和圆端槽的整圆离散段数。",
        default=_DEFAULT_SEGMENTS, min=8, max=512,
    )
    line_start: bpy.props.FloatVectorProperty(
        name="线段起点", description="线段起点 UV。",
        size=2, default=(-0.5, 0.0), unit="LENGTH",
    )
    line_end: bpy.props.FloatVectorProperty(
        name="线段终点", description="线段终点 UV。",
        size=2, default=(0.5, 0.0), unit="LENGTH",
    )
    line_width: bpy.props.FloatProperty(
        name="线段宽度", default=0.1, min=1.0e-6, max=1000.0,
        subtype="DISTANCE", unit="LENGTH",
    )
    radius: bpy.props.FloatProperty(
        name="半径", default=0.5, min=1.0e-6, max=1000.0,
        subtype="DISTANCE", unit="LENGTH",
    )
    arc_start: bpy.props.FloatProperty(
        name="起始角", default=0.0,
        subtype="ANGLE", unit="ROTATION",
    )
    arc_end: bpy.props.FloatProperty(
        name="终止角", default=math.pi,
        subtype="ANGLE", unit="ROTATION",
    )
    arc_width: bpy.props.FloatProperty(
        name="圆弧宽度", default=0.1, min=1.0e-6, max=1000.0,
        subtype="DISTANCE", unit="LENGTH",
    )
    slot_length: bpy.props.FloatProperty(
        name="槽总长", default=1.0, min=1.0e-6, max=1000.0,
        subtype="DISTANCE", unit="LENGTH",
    )
    slot_width: bpy.props.FloatProperty(
        name="槽宽", default=0.3, min=1.0e-6, max=1000.0,
        subtype="DISTANCE", unit="LENGTH",
    )
    polygon_points: bpy.props.StringProperty(
        name="多边形点", description='JSON 数组，例如 [[-0.5,-0.5],[0.5,-0.5],[0.5,0.5]]。',
        default=json.dumps(_DEFAULT_POINTS, separators=(",", ":")),
    )
    user_edit_policy: bpy.props.EnumProperty(
        name="手改策略", items=_POLICY_ITEMS, default=USER_EDIT_REFUSE,
    )
    unlink_shared: bpy.props.BoolProperty(
        name="解除共享网格", description="仅让当前刀体获得独立网格，其他用户保持原样。",
        default=False,
    )
    expected_signature: bpy.props.StringProperty(
        name="期望签名", description="可选；不匹配时拒绝修改。", default="",
    )
    copy_name: bpy.props.StringProperty(
        name="复制名称", description="COPY 策略的新刀体名称；留空自动命名。", default="",
    )
    loaded_cutter: bpy.props.StringProperty(default="")
    status: bpy.props.StringProperty(default="")
    status_kind: bpy.props.StringProperty(default="INFO")
    last_result: bpy.props.StringProperty(default="")


class HSW_OT_profile_work_plane_create(bpy.types.Operator):
    bl_idname = "hard_surface.profile_work_plane_create"
    bl_label = "Create Editable Work Plane"
    bl_description = "Create an object-local, face or direction-edge work plane"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _settings(context) is not None

    def execute(self, context):
        settings = _settings(context)
        try:
            target = _panel_target(settings, context, require_mesh=True)
            result = execute(target, TOOL_WORK_PLANE_CREATE,
                             _plane_params(settings, include_name=True))
        except (ValueError, TypeError, RuntimeError) as error:
            return _fail(self, settings, error)
        settings.plane_object = bpy.data.objects.get(result["work_plane"])
        return _succeed(self, settings, result,
                        f"Created work plane {result['work_plane']}")


class HSW_OT_profile_work_plane_update(bpy.types.Operator):
    bl_idname = "hard_surface.profile_work_plane_update"
    bl_label = "Update Editable Work Plane"
    bl_description = "Rebuild the selected work plane from the current target"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _settings(context) is not None

    def execute(self, context):
        settings = _settings(context)
        if settings.plane_object is None:
            return _fail(self, settings, "Choose an existing work plane")
        try:
            target = _panel_target(settings, context, require_mesh=True)
            params = _plane_params(settings)
            params["name"] = settings.plane_object.name
            params["allow_user_transform"] = bool(settings.allow_user_transform)
            params["allow_user_edit"] = bool(settings.allow_user_edit)
            result = execute(target, TOOL_WORK_PLANE_UPDATE, params)
        except (ValueError, TypeError, RuntimeError) as error:
            return _fail(self, settings, error)
        settings.plane_object = bpy.data.objects.get(result["work_plane"])
        return _succeed(self, settings, result,
                        f"Updated work plane {result['work_plane']}")


class HSW_OT_profile_work_plane_inspect(bpy.types.Operator):
    bl_idname = "hard_surface.profile_work_plane_inspect"
    bl_label = "Inspect Editable Work Plane"
    bl_description = "Report work plane axes, normal, winding and cutter references"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return _settings(context) is not None

    def execute(self, context):
        settings = _settings(context)
        if settings.plane_object is None:
            return _fail(self, settings, "Choose an existing work plane")
        try:
            result = execute(settings.plane_object, TOOL_WORK_PLANE_INSPECT,
                             {"name": settings.plane_object.name})
        except (ValueError, TypeError, RuntimeError) as error:
            return _fail(self, settings, error)
        referenced = result.get("referenced_cutters") or []
        message = (f"{result['work_plane']}: size {result['size']:.4g}, "
                   f"normal {result['normal']}, cutters {len(referenced)}")
        return _succeed(self, settings, result, message)


class HSW_OT_profile_dimension_create(bpy.types.Operator):
    bl_idname = "hard_surface.profile_dimension_create"
    bl_label = "Create Dimension Cutter"
    bl_description = "Create an independent editable cutter on a work plane"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _settings(context) is not None

    def execute(self, context):
        settings = _settings(context)
        try:
            target = _panel_target(settings, context,
                                   require_mesh=settings.plane_object is None)
            params = _cutter_create_params(settings)
            result = execute(target, TOOL_DIMENSION_CREATE, params)
        except (ValueError, TypeError, RuntimeError) as error:
            return _fail(self, settings, error)
        settings.cutter_object = bpy.data.objects.get(result["cutter"])
        return _succeed(self, settings, result,
                        f"Created {result['shape']} cutter {result['cutter']}")


class HSW_OT_profile_dimension_resize(bpy.types.Operator):
    bl_idname = "hard_surface.profile_dimension_resize"
    bl_label = "Resize Dimension Cutter In Place"
    bl_description = "Rebuild the selected cutter mesh while preserving its object transform"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _settings(context) is not None

    def execute(self, context):
        settings = _settings(context)
        cutter = settings.cutter_object
        if cutter is None:
            return _fail(self, settings, "Choose an existing cutter")
        try:
            if settings.loaded_cutter != cutter.name:
                _load_cutter_into_settings(settings, cutter)
            stored = _cutter_spec_from_object(cutter)
            if settings.cutter_shape != stored["shape"]:
                raise ValueError("Changing shape in place is not allowed; create a new cutter")
            params = _cutter_resize_params(settings, cutter)
            result = execute(cutter, TOOL_DIMENSION_RESIZE, params)
        except (ValueError, TypeError, RuntimeError) as error:
            return _fail(self, settings, error)
        settings.cutter_object = bpy.data.objects.get(result["cutter"])
        _load_cutter_into_settings(settings, settings.cutter_object)
        message = (f"{result['action']} {result['cutter']}: "
                   f"{result['shape']} depth {result['dimensions']['depth']:.4g}")
        if result.get("action") == "COPY":
            message += f" (source {result.get('copied_from')} unchanged)"
        return _succeed(self, settings, result, message)


class HSW_OT_profile_dimension_inspect(bpy.types.Operator):
    bl_idname = "hard_surface.profile_dimension_inspect"
    bl_label = "Inspect Dimension Cutter"
    bl_description = "Report cutter dimensions, winding, edit state and Boolean consumers"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return _settings(context) is not None

    def execute(self, context):
        settings = _settings(context)
        cutter = settings.cutter_object
        if cutter is None:
            return _fail(self, settings, "Choose an existing cutter")
        try:
            result = execute(cutter, TOOL_DIMENSION_INSPECT, {"name": cutter.name})
        except (ValueError, TypeError, RuntimeError) as error:
            return _fail(self, settings, error)
        dimensions = result.get("stored_dimensions")
        if not isinstance(dimensions, dict):
            dimensions = {}
        volume = result["mesh_stats"].get("signed_volume")
        prefix = (f"{result['cutter']}: {result['shape']}, volume {volume:.6g}"
                  if volume is not None else
                  f"{result['cutter']}: {result['shape']}, mesh invalid")
        message = (f"{prefix}, winding {result['face_winding']}, "
                   f"consumers {len(result['consumers'])}"
                   f"{', user-edited' if result['user_edited'] else ''}")
        if dimensions.get("diameter") is not None:
            message += f", diameter {dimensions['diameter']:.4g}"
        return _succeed(self, settings, result, message)


class HSW_OT_profile_reload(bpy.types.Operator):
    bl_idname = "hard_surface.profile_reload"
    bl_label = "Reload Profile Parameters"
    bl_description = "Reload the selected work plane and cutter parameters from metadata"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return _settings(context) is not None

    def execute(self, context):
        settings = _settings(context)
        loaded = []
        try:
            if settings.plane_object is not None:
                _load_plane_into_settings(settings, settings.plane_object)
                loaded.append(settings.plane_object.name)
            if settings.cutter_object is not None:
                _load_cutter_into_settings(settings, settings.cutter_object)
                loaded.append(settings.cutter_object.name)
        except (ValueError, TypeError, RuntimeError) as error:
            return _fail(self, settings, error)
        if not loaded:
            return _fail(self, settings, "Choose a work plane or cutter to reload")
        _set_status(settings, "Reloaded " + ", ".join(loaded), "SUCCESS")
        return {"FINISHED"}


class VIEW3D_PT_hsw_profiles(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_hsw_profiles"
    bl_label = "可编辑工作平面与尺寸刀体"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"

    @classmethod
    def poll(cls, context):
        return _settings(context) is not None and context.mode == "OBJECT"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        if settings is None:
            layout.label(text="Profile settings are not registered.")
            return

        layout.prop(settings, "target_object")

        plane = layout.box()
        plane.label(text="工作平面", icon="MESH_PLANE")
        plane.prop(settings, "plane_object")
        plane.prop(settings, "plane_mode")
        if settings.plane_mode == PLANE_FACE:
            plane.prop(settings, "plane_face")
        elif settings.plane_mode == PLANE_DIRECTION_EDGE:
            plane.prop(settings, "plane_edge")
            plane.prop(settings, "plane_use_face")
            if settings.plane_use_face:
                plane.prop(settings, "plane_face")
        plane.prop(settings, "plane_size")
        plane.prop(settings, "plane_offset")
        plane.prop(settings, "plane_flip")
        if settings.plane_object is None:
            plane.prop(settings, "plane_name")
        row = plane.row(align=True)
        row.operator("hard_surface.profile_work_plane_create", icon="ADD")
        update = plane.row(align=True)
        update.enabled = settings.plane_object is not None
        update.operator("hard_surface.profile_work_plane_update", icon="FILE_REFRESH")
        inspect = plane.row(align=True)
        inspect.enabled = settings.plane_object is not None
        inspect.operator("hard_surface.profile_work_plane_inspect", icon="INFO")
        if settings.plane_object is not None:
            plane.prop(settings, "allow_user_transform")
            plane.prop(settings, "allow_user_edit")

        cutter = layout.box()
        cutter.label(text="尺寸刀体", icon="MESH_CYLINDER")
        cutter.prop(settings, "cutter_name")
        cutter.prop(settings, "cutter_shape")
        if settings.cutter_shape == SHAPE_LINE:
            cutter.prop(settings, "line_start")
            cutter.prop(settings, "line_end")
            cutter.prop(settings, "line_width")
        elif settings.cutter_shape == SHAPE_ARC:
            cutter.prop(settings, "radius")
            cutter.prop(settings, "arc_start")
            cutter.prop(settings, "arc_end")
            cutter.prop(settings, "arc_width")
            cutter.prop(settings, "cutter_segments")
        elif settings.cutter_shape == SHAPE_CIRCLE:
            cutter.prop(settings, "radius")
            cutter.prop(settings, "cutter_segments")
        elif settings.cutter_shape == SHAPE_ROUND_SLOT:
            cutter.prop(settings, "slot_length")
            cutter.prop(settings, "slot_width")
            cutter.prop(settings, "cutter_segments")
        elif settings.cutter_shape == SHAPE_POLYGON:
            cutter.prop(settings, "polygon_points")
        cutter.prop(settings, "cutter_center")
        cutter.prop(settings, "cutter_depth")
        if settings.plane_object is None:
            cutter.label(text="留空工作平面时按上方设置自动创建", icon="INFO")
        cutter.operator("hard_surface.profile_dimension_create", icon="ADD")

        resize = layout.box()
        resize.label(text="原位改尺寸 / 检查", icon="MOD_LENGTH")
        resize.prop(settings, "cutter_object")
        row = resize.row(align=True)
        row.operator("hard_surface.profile_reload", icon="FILE_REFRESH")
        inspect = resize.row(align=True)
        inspect.enabled = settings.cutter_object is not None
        inspect.operator("hard_surface.profile_dimension_inspect", icon="INFO")
        resize.prop(settings, "user_edit_policy")
        resize.prop(settings, "unlink_shared")
        resize.prop(settings, "expected_signature")
        resize.prop(settings, "copy_name")
        button = resize.row(align=True)
        button.enabled = settings.cutter_object is not None
        button.operator("hard_surface.profile_dimension_resize", icon="FULLSCREEN_ENTER")

        status = layout.box()
        status.label(text="状态")
        icon = {"ERROR": "ERROR", "SUCCESS": "CHECKMARK",
                "WARNING": "ERROR"}.get(settings.status_kind, "INFO")
        status.label(text=settings.status or "Ready", icon=icon)
        if settings.last_result:
            preview = settings.last_result
            if len(preview) > 220:
                preview = preview[:217] + "..."
            status.label(text=preview)


# ---------------------------------------------------------------------------
# Registration


_UI_CLASSES = (
    HSW_PG_profile_settings,
    HSW_OT_profile_work_plane_create,
    HSW_OT_profile_work_plane_update,
    HSW_OT_profile_work_plane_inspect,
    HSW_OT_profile_dimension_create,
    HSW_OT_profile_dimension_resize,
    HSW_OT_profile_dimension_inspect,
    HSW_OT_profile_reload,
    VIEW3D_PT_hsw_profiles,
)
_KNOWN_CLASS_NAMES = (
    "HSW_PG_profile_settings",
    "HSW_OT_profile_work_plane_create",
    "HSW_OT_profile_work_plane_update",
    "HSW_OT_profile_work_plane_inspect",
    "HSW_OT_profile_dimension_create",
    "HSW_OT_profile_dimension_resize",
    "HSW_OT_profile_dimension_inspect",
    "HSW_OT_profile_reload",
    "VIEW3D_PT_hsw_profiles",
)

_REGISTERED = False


def _remove_scene_settings():
    try:
        if hasattr(bpy.types.Scene, SCENE_SETTINGS):
            delattr(bpy.types.Scene, SCENE_SETTINGS)
    except Exception:
        pass


def _purge_known_classes():
    _remove_scene_settings()
    for name in _KNOWN_CLASS_NAMES:
        cls = getattr(bpy.types, name, None)
        if cls is None:
            continue
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    # PropertyGroup classes are not necessarily exposed on bpy.types.
    for base in (bpy.types.PropertyGroup, bpy.types.Operator, bpy.types.Panel):
        stack = list(base.__subclasses__())
        while stack:
            cls = stack.pop()
            stack.extend(cls.__subclasses__())
            if cls.__name__ not in _KNOWN_CLASS_NAMES:
                continue
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass


def register():
    """Register the profile property group, non-modal operators and sidebar panel."""
    global _REGISTERED
    if (_REGISTERED and hasattr(bpy.types, "VIEW3D_PT_hsw_profiles")
            and hasattr(bpy.types.Scene, SCENE_SETTINGS)):
        return
    _purge_known_classes()
    added = []
    try:
        for cls in _UI_CLASSES:
            bpy.utils.register_class(cls)
            added.append(cls)
        setattr(bpy.types.Scene, SCENE_SETTINGS,
                bpy.props.PointerProperty(type=HSW_PG_profile_settings))
    except Exception:
        _remove_scene_settings()
        for cls in reversed(added):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
        _REGISTERED = False
        raise
    _REGISTERED = True


def unregister():
    """Undo :func:`register`; safe to call when registration failed."""
    global _REGISTERED
    _remove_scene_settings()
    for cls in reversed(_UI_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    _REGISTERED = False
