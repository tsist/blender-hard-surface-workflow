"""Hard-surface edit-mode feature pack for Blender 5.2.

The module deliberately implements its operations with Blender's own bmesh
geometry instead of depending on an external add-on.  The public API is:

    TOOLS
    describe(tool)
    run(tool, params)
    register()
    unregister()

`run` operates once on the current selection of one mesh in Edit Mode.  Every
operation is first executed on a temporary copy of the edit mesh, measured and
validated.  Only a validated result is written back to the live edit mesh.
"""

from copy import deepcopy
import math

import bmesh
import bpy
from mathutils import Matrix, Vector
from mathutils.geometry import intersect_line_line_2d

from . import precision_edit


_EPS = 1e-12
_MAX_COORD = 1e12
_DOMAINS = ("VERT", "EDGE", "FACE")
_SEQUENCES = {"VERT": "verts", "EDGE": "edges", "FACE": "faces"}
__all__ = ["TOOLS", "describe", "run", "register", "unregister"]


# ---------------------------------------------------------------------------
# Public descriptions


def _option(kind, default, description, *, minimum=None, maximum=None, enum=None, items=None):
    result = {"type": kind, "default": default, "description": description}
    if minimum is not None:
        result["min"] = minimum
    if maximum is not None:
        result["max"] = maximum
    if enum is not None:
        result["enum"] = list(enum)
    if items is not None:
        result["items"] = deepcopy(items)
    return result


def _number(default, description, minimum, maximum):
    return _option("number", float(default), description, minimum=float(minimum), maximum=float(maximum))


def _integer(default, description, minimum, maximum):
    return _option("integer", int(default), description, minimum=int(minimum), maximum=int(maximum))


def _boolean(default, description):
    return _option("boolean", bool(default), description)


def _enum(default, description, choices):
    return _option("string", default, description, enum=choices)


def _vector(default, description):
    result = _option("array", default, description, items={"type": "number", "min": -1e9, "max": 1e9})
    result.update(minItems=3, maxItems=3)
    return result


def _tool(tool, label, description, domains, topology_changes, parameters):
    return {
        "tool": tool,
        "label": label,
        "description": description,
        "parameters": parameters,
        "context": "SINGLE_EDIT_MESH",
        "domains": list(domains),
        "topology_changes": bool(topology_changes),
    }


TOOLS = {
    "form.recess": _tool(
        "form.recess", "面内嵌沉台",
        "内插后沿局部法线压入。逐面使用各自原法线，区域使用一个面区平均法线。RECESS 保留直壁，COUNTERSINK 从外圈直接过渡到内盘。",
        ("FACE",), True, {
            "thickness": _number(0.05, "内插宽度（局部单位）", 1e-6, 1000.0),
            "depth": _number(0.05, "沿面法线向内的深度（局部单位）", 1e-6, 1000.0),
            "individual": _boolean(False, "每张选中面独立内插和压入"),
            "mode": _enum("RECESS", "沉台壁类型", ("RECESS", "COUNTERSINK")),
        }),
    "form.push_pull": _tool(
        "form.push_pull", "选面法线推拉",
        "沿选中面区平均法线挤出并平移端面；individual 时逐面沿各自法线推拉。",
        ("FACE",), True, {
            "distance": _number(0.1, "有符号距离，正值为面法线方向", -1000.0, 1000.0),
            "individual": _boolean(False, "每张选中面独立挤出"),
        }),
    "vertex.merge": _tool(
        "vertex.merge", "合并选点到中心",
        "把当前选中的多个顶点合并到平均位置或当前活动顶点位置。",
        ("VERT",), True, {
            "mode": _enum("CENTER", "合并目标", ("CENTER", "ACTIVE")),
        }),
    "vertex.connect": _tool(
        "vertex.connect", "从选点建边或面",
        "按选点顺序或空间排序建边链，闭合且至少三点时建面。建边保留原面。要切开已有面，请用原生连接顶点路径（J）或切割（K）。",
        ("VERT",), True, {
            "close": _boolean(True, "闭合为面；关闭时生成开放边链"),
            "tolerance": _number(1e-5, "闭合面的平面容差（局部单位）", 1e-9, 1.0),
        }),
    "array.linear": _tool(
        "array.linear", "线性重复",
        "复制当前选中的点、边或面，并沿局部轴等距排列；count 包含原始实例。",
        ("VERT", "EDGE", "FACE"), True, {
            "count": _integer(2, "实例总数（含原始）", 2, 100),
            "distance": _number(1.0, "相邻实例的有符号间距", -1000.0, 1000.0),
            "axis": _enum("X", "局部轴", ("X", "Y", "Z")),
        }),
    "array.radial": _tool(
        "array.radial", "旋转重复",
        "复制当前选中的点、边或面，并绕局部轴旋转排列；count 包含原始实例。",
        ("VERT", "EDGE", "FACE"), True, {
            "count": _integer(2, "实例总数（含原始）", 2, 100),
            "angle": _number(360.0, "排列总角度；360 时按 count 均分整圈", 0.0001, 360.0),
            "axis": _enum("Z", "局部旋转轴", ("X", "Y", "Z")),
            "center": _enum("ORIGIN", "旋转中心", ("ORIGIN", "CURSOR", "SELECTION")),
        }),
    "align.axis": _tool(
        "align.axis", "轴向对齐",
        "把选中元素涉及的所有顶点对齐到局部某轴的最小、最大或平均坐标平面。",
        ("VERT", "EDGE", "FACE"), False, {
            "axis": _enum("Z", "局部轴", ("X", "Y", "Z")),
            "mode": _enum("CENTER", "对齐目标", ("MIN", "MAX", "CENTER")),
            "offset": _number(0.0, "对齐坐标偏移", -1000.0, 1000.0),
            "factor": _number(1.0, "从原位置到目标位置的混合比例", 0.0001, 1.0),
        }),
    "align.plane": _tool(
        "align.plane", "对齐到选择平面",
        "从选中元素邻接面或最佳拟合求出平面，把相关顶点投影到该平面。",
        ("VERT", "EDGE", "FACE"), False, {
            "source": _enum("AUTO", "平面来源", ("AUTO", "FACES", "BEST_FIT")),
            "offset": _number(0.0, "沿平面法线的偏移", -1000.0, 1000.0),
            "factor": _number(1.0, "投影混合比例", 0.0001, 1.0),
        }),
    "align.reference": _tool(
        "align.reference", "按世界基准定位选区",
        "整体移动选区。点、平面、边线或方向模式只平移；FRAME 对齐指定法向和切轴，保留世界空间尺寸与滚转关系。",
        ("VERT", "EDGE", "FACE"), False, {
            "mode": _enum("POINT", "定位方式", ("POINT", "PLANE", "EDGE", "DIRECTION", "FRAME")),
            "source_world": _vector([0, 0, 0], "源参考点（世界坐标）"),
            "target_world": _vector([0, 0, 0], "目标点、工作面原点或目标边起点（世界坐标）"),
            "target_end_world": _vector([1, 0, 0], "EDGE 模式的目标边终点（世界坐标）"),
            "target_direction": _vector([0, 0, 1], "运动方向或目标平面法向（世界方向）"),
            "plane_offset": _number(0.0, "PLANE 模式沿世界法向的有符号偏移", -1000., 1000.),
            "source_normal": _vector([0, 0, 1], "FRAME 源法向（世界方向）"),
            "source_tangent": _vector([1, 0, 0], "FRAME 源切轴（世界方向）"),
            "target_tangent": _vector([1, 0, 0], "FRAME 目标切轴（世界方向）"),
        }),
    "mesh.snap_symmetry": _tool(
        "mesh.snap_symmetry", "吸附到对称位置",
        "按局部轴从固定侧查找镜像点，只调整选中的接收侧和可选中线点，保留两侧拓扑。",
        ("VERT", "EDGE", "FACE"), False, {
            "direction": _enum("-X", "以哪一侧为固定源", ("-X", "+X", "-Y", "+Y", "-Z", "+Z")),
            "threshold": _number(0.05, "镜像候选匹配距离（局部单位）", 1e-9, 1000.),
            "factor": _number(1.0, "接收侧从当前位置向镜像位置插值的比例", 0., 1.),
            "use_center": _boolean(True, "把指定容差内的已选点吸附到中线"),
            "center_tolerance": _number(1e-5, "中线容差（局部单位，至多为匹配距离）", 0., 1000.),
        }) | {"coordinates_only": True},
    "support.face": _tool(
        "support.face", "面区支持线",
        "在选中面区内部按宽度生成一圈或多圈同心内插支持线，不改变面区外轮廓。",
        ("FACE",), True, {
            "width": _number(0.02, "每圈内插宽度（局部单位）", 1e-6, 1000.0),
            "count": _integer(1, "支持线圈数", 1, 20),
            "individual": _boolean(False, "每张选中面独立生成支持线"),
        }),
    "support.edge": _tool(
        "support.edge", "边支持线",
        "在选中边上生成单段倒角式支撑几何，形成两条平行支持边。",
        ("EDGE",), True, {
            "width": _number(0.02, "支持边偏移宽度（局部单位）", 1e-6, 1000.0),
            "clamp_overlap": _boolean(True, "限制相邻边重叠"),
        }),
    "form.lathe": _tool(
        "form.lathe", "截面回转",
        "把一条连通、无分支的选中边链作为截面，复制后绕局部轴回转生成曲面；保留原截面。",
        ("EDGE",), True, {
            "axis": _enum("Z", "局部回转轴", ("X", "Y", "Z")),
            "angle": _number(360.0, "回转角度（度）", 1.0, 360.0),
            "steps": _integer(16, "回转步数", 3, 256),
            "center": _enum("ORIGIN", "回转中心", ("ORIGIN", "CURSOR", "SELECTION")),
        }),
    "form.hole": _tool(
        "form.hole", "平面开圆孔",
        "在选中的平面面片内构造真实圆形孔洞，并以环形面连接原边界和孔边。",
        ("FACE",), True, {
            "radius": _number(0.25, "圆孔半径（局部单位）", 1e-6, 1000.0),
            "segments": _integer(16, "圆孔边数", 8, 512),
            "tolerance": _number(1e-5, "平面判定容差（局部单位）", 1e-9, 1.0),
            "center_mode": _enum("VERTEX_MEDIAN", "孔心基准", ("VERTEX_MEDIAN", "AREA_CENTROID", "EXPLICIT")),
            "center_local": _vector([0, 0, 0], "EXPLICIT 单个源面的孔心（本地坐标）"),
            "radius_mode": _enum("VERTEX_RADIUS", "半径按孔缘顶点或圆规净空解释", ("VERTEX_RADIUS", "CLEAR_RADIUS")),
        }),
}


def describe(tool):
    """Return an independent description of one tool."""
    if tool not in TOOLS:
        raise ValueError(f"Unknown tool: {tool!r}")
    return deepcopy(TOOLS[tool])


# ---------------------------------------------------------------------------
# Parameters and context


def _check_scalar(name, kind, value, rule):
    if kind == "boolean":
        if type(value) is not bool:
            raise ValueError(f"{name} must be boolean")
        return value
    if kind == "integer":
        if type(value) is not int:
            raise ValueError(f"{name} must be integer")
        if "min" in rule and value < rule["min"]:
            raise ValueError(f"{name} must be at least {rule['min']}")
        if "max" in rule and value > rule["max"]:
            raise ValueError(f"{name} must be at most {rule['max']}")
        return value
    if kind == "number":
        if type(value) not in (int, float):
            raise ValueError(f"{name} must be number")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if "min" in rule and value < rule["min"]:
            raise ValueError(f"{name} must be at least {rule['min']}")
        if "max" in rule and value > rule["max"]:
            raise ValueError(f"{name} must be at most {rule['max']}")
        return value
    if kind == "string":
        if type(value) is not str:
            raise ValueError(f"{name} must be string")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"{name} must be one of {rule['enum']}")
        return value
    if kind == "array":
        if type(value) is not list:
            raise ValueError(f"{name} must be array")
        if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", math.inf):
            raise ValueError(f"{name} has the wrong number of elements")
        items = rule.get("items")
        result = []
        for item in value:
            if items is None:
                item_kind = "number"
            else:
                item_kind = items.get("type", "number")
            result.append(_check_scalar(name, item_kind, item, items or {}))
        return result
    raise ValueError(f"Unsupported parameter type for {name}: {kind!r}")


def _parameters(spec, params):
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("Tool parameters must be an object")
    if any(not isinstance(key, str) for key in params):
        raise ValueError("Tool parameter names must be strings")
    rules = spec["parameters"]
    extra = set(params).difference(rules)
    if extra:
        raise ValueError(f"Unknown parameters for {spec['tool']}: {', '.join(sorted(extra))}")
    settings = {}
    for name, rule in rules.items():
        value = params.get(name, deepcopy(rule["default"]))
        settings[name] = _check_scalar(name, rule["type"], value, rule)
    return settings


def _lookup(bm):
    for sequence in (bm.verts, bm.edges, bm.faces):
        sequence.ensure_lookup_table()
        sequence.index_update()


def _mesh_context(spec):
    context = bpy.context
    obj = context.edit_object
    if context.mode != "EDIT_MESH" or obj is None:
        raise ValueError("Tool requires one mesh object in Edit Mode")
    if obj.type != "MESH" or obj.data is None:
        raise ValueError("Tool requires a mesh object")
    editing = context.objects_in_mode
    if len(editing) != 1 or editing[0] != obj:
        raise ValueError("Tool requires exactly one mesh in Edit Mode")
    if obj.data.users - int(obj.data.use_fake_user) > 1:
        raise ValueError("Tool refuses a mesh datablock shared by multiple objects")
    modes = tuple(bool(value) for value in context.tool_settings.mesh_select_mode)
    if sum(modes) != 1:
        raise ValueError("Select one selection domain (VERT, EDGE or FACE) before running")
    domain = _DOMAINS[modes.index(True)]
    if domain not in spec["domains"]:
        raise ValueError(f"{spec['tool']} accepts a single {'/'.join(spec['domains'])} selection")
    bm = bmesh.from_edit_mesh(obj.data)
    _lookup(bm)
    sequence = getattr(bm, _SEQUENCES[domain])
    selected = [element for element in sequence if element.select and not element.hide]
    if not selected:
        raise ValueError(f"{spec['tool']} requires a nonempty {domain} selection")
    indices = [element.index for element in selected]
    cursor = context.scene.cursor.location.copy()
    try:
        cursor_local = obj.matrix_world.inverted() @ cursor
    except (ValueError, TypeError) as exc:
        raise ValueError("Cannot convert the 3D cursor into object space") from exc
    active = getattr(bm.select_history, "active", None)
    active_index = active.index if isinstance(active, bmesh.types.BMVert) and active.select else None
    order = [v.index for v in bm.select_history if isinstance(v, bmesh.types.BMVert) and v.select]
    return obj, bm, domain, indices, {"cursor_local": cursor_local, "active_vertex_index": active_index,
                                      "vertex_order": order, "matrix_world": obj.matrix_world.copy()}


def _elements(bm, domain, indices):
    sequence = getattr(bm, _SEQUENCES[domain])
    result = []
    for index in indices:
        if type(index) is not int or not 0 <= index < len(sequence):
            raise ValueError(f"Invalid {domain} index: {index!r}")
        result.append(sequence[index])
    return result


# ---------------------------------------------------------------------------
# Geometry helpers


def _finite(value):
    return all(math.isfinite(item) and abs(item) <= _MAX_COORD for item in value)


def _area_normal(faces):
    total = Vector((0.0, 0.0, 0.0))
    for face in faces:
        if not face.is_valid:
            raise ValueError("A selected face became invalid")
        face.normal_update()
        total += face.normal * max(face.calc_area(), 0.0)
    if total.length <= 1e-12:
        raise ValueError("Selected faces do not define a usable average normal")
    return total.normalized()


def _point_segment_distance(point, start, end):
    direction = end - start
    length_sq = direction.length_squared
    if length_sq <= _EPS:
        return (point - start).length
    factor = max(0.0, min(1.0, (point - start).dot(direction) / length_sq))
    return (point - (start + direction * factor)).length


def _face_inradius(face):
    center = face.calc_center_median()
    return min(_point_segment_distance(center, edge.verts[0].co, edge.verts[1].co)
               for edge in face.edges)


def _snapshot(bm):
    return set(bm.verts), set(bm.edges), set(bm.faces)


def _new_elements(bm, before):
    before_verts, before_edges, before_faces = before
    return ([vertex for vertex in bm.verts if vertex not in before_verts],
            [edge for edge in bm.edges if edge not in before_edges],
            [face for face in bm.faces if face not in before_faces])


def _base_stats(bm, before_counts, before_sets, extra):
    new_verts, new_edges, new_faces = _new_elements(bm, before_sets)
    verts_before, edges_before, faces_before = before_counts
    stats = {
        "verts_before": verts_before,
        "edges_before": edges_before,
        "faces_before": faces_before,
        "verts_after": len(bm.verts),
        "edges_after": len(bm.edges),
        "faces_after": len(bm.faces),
        "new_vertices": len(new_verts),
        "new_edges": len(new_edges),
        "new_faces": len(new_faces),
        "verts_added": len(bm.verts) - verts_before,
        "edges_added": len(bm.edges) - edges_before,
        "faces_added": len(bm.faces) - faces_before,
    }
    stats.update(extra)
    stats["_new_verts"] = new_verts
    stats["_new_edges"] = new_edges
    stats["_new_faces"] = new_faces
    return stats


def _clear_selection(bm):
    for sequence in (bm.verts, bm.edges, bm.faces):
        for element in sequence:
            element.select = False


def _select_faces(bm, faces):
    _clear_selection(bm)
    for face in faces:
        if face is None or not face.is_valid:
            continue
        face.select = True
        for edge in face.edges:
            edge.select = True
        for vertex in face.verts:
            vertex.select = True


def _select_edges(bm, edges):
    _clear_selection(bm)
    for edge in edges:
        if edge is None or not edge.is_valid:
            continue
        edge.select = True
        for vertex in edge.verts:
            vertex.select = True


def _select_verts(bm, verts):
    _clear_selection(bm)
    for vertex in verts:
        if vertex is not None and vertex.is_valid:
            vertex.select = True


def _involved_verts(bm, domain, indices):
    elements = _elements(bm, domain, indices)
    if domain == "VERT":
        return elements
    if domain == "EDGE":
        return list({vertex for edge in elements for vertex in edge.verts})
    return list({vertex for face in elements for vertex in face.verts})


def _faces_for_domain(bm, domain, indices):
    elements = _elements(bm, domain, indices)
    if domain == "FACE":
        return elements
    if domain == "EDGE":
        return list({face for edge in elements for face in edge.link_faces})
    return list({face for vertex in elements for face in vertex.link_faces})


def _sym_eigen3(matrix):
    a = [list(row) for row in matrix]
    vectors = [[1.0 if row == col else 0.0 for col in range(3)] for row in range(3)]
    for _ in range(32):
        p, q, best = 0, 1, abs(a[0][1])
        for row, col in ((0, 2), (1, 2)):
            value = abs(a[row][col])
            if value > best:
                p, q, best = row, col, value
        diagonal = max(abs(a[0][0]), abs(a[1][1]), abs(a[2][2]), 1e-300)
        if best <= diagonal * 1e-15:
            break
        theta = (a[q][q] - a[p][p]) / (2.0 * a[p][q])
        if abs(theta) > 1e150:
            tangent = 0.0
        else:
            tangent = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1.0))
        cosine = 1.0 / math.sqrt(tangent * tangent + 1.0)
        sine = tangent * cosine
        app, aqq, apq = a[p][p], a[q][q], a[p][q]
        for index in range(3):
            if index != p and index != q:
                akp, akq = a[index][p], a[index][q]
                a[index][p] = a[p][index] = cosine * akp - sine * akq
                a[index][q] = a[q][index] = sine * akp + cosine * akq
        a[p][p] = cosine * cosine * app - 2.0 * sine * cosine * apq + sine * sine * aqq
        a[q][q] = sine * sine * app + 2.0 * sine * cosine * apq + cosine * cosine * aqq
        a[p][q] = a[q][p] = 0.0
        for index in range(3):
            vkp, vkq = vectors[index][p], vectors[index][q]
            vectors[index][p] = cosine * vkp - sine * vkq
            vectors[index][q] = sine * vkp + cosine * vkq
    values = [a[index][index] for index in range(3)]
    order = sorted(range(3), key=values.__getitem__)
    eigenvectors = [Vector((vectors[0][index], vectors[1][index], vectors[2][index])).normalized()
                    for index in order]
    return [values[index] for index in order], eigenvectors


def _pca(points):
    if len(points) < 3:
        raise ValueError("At least three points are needed for a best-fit plane")
    center = sum(points, Vector((0.0, 0.0, 0.0))) / len(points)
    xx = xy = xz = yy = yz = zz = 0.0
    for point in points:
        delta = point - center
        xx += delta.x * delta.x
        xy += delta.x * delta.y
        xz += delta.x * delta.z
        yy += delta.y * delta.y
        yz += delta.y * delta.z
        zz += delta.z * delta.z
    values, vectors = _sym_eigen3(((xx, xy, xz), (xy, yy, yz), (xz, yz, zz)))
    return center, values, vectors


def _best_fit_plane(points):
    center, values, vectors = _pca(points)
    if values[2] <= 1e-30:
        raise ValueError("Selected points are coincident")
    if values[1] <= values[2] * 1e-12:
        raise ValueError("Selected points are collinear; they do not define a plane")
    normal = vectors[0].normalized()
    if normal.length <= 0.5:
        raise ValueError("Best-fit plane normal is invalid")
    components = (abs(normal.x), abs(normal.y), abs(normal.z))
    axis = max(range(3), key=components.__getitem__)
    if normal[axis] < 0.0:
        normal.negate()
    return normal, center


def _axis_vector(axis):
    return {"X": Vector((1.0, 0.0, 0.0)),
            "Y": Vector((0.0, 1.0, 0.0)),
            "Z": Vector((0.0, 0.0, 1.0))}[axis]


def _center_from_mode(mode, verts, context):
    if mode == "ORIGIN":
        return Vector((0.0, 0.0, 0.0))
    if mode == "CURSOR":
        return context["cursor_local"].copy()
    return sum((vertex.co for vertex in verts), Vector((0.0, 0.0, 0.0))) / len(verts)


def _profile_graph(edges):
    degrees = {}
    adjacency = {}
    for edge in edges:
        for vertex in edge.verts:
            degrees[vertex] = degrees.get(vertex, 0) + 1
            adjacency.setdefault(vertex, []).append(edge)
    if any(degree > 2 for degree in degrees.values()):
        raise ValueError("The selected edge profile branches")
    endpoints = [vertex for vertex, degree in degrees.items() if degree == 1]
    if endpoints and len(endpoints) != 2:
        raise ValueError("The selected edge profile is disconnected or has too many ends")
    start = next(iter(degrees))
    seen = {start}
    pending = [start]
    while pending:
        vertex = pending.pop()
        for edge in adjacency.get(vertex, ()):
            other = edge.other_vert(vertex)
            if other not in seen:
                seen.add(other)
                pending.append(other)
    if len(seen) != len(degrees):
        raise ValueError("The selected edge profile has multiple disconnected components")
    return degrees, endpoints


def _ordered_selected_verts(bm, verts, close, tolerance, order):
    wanted = set(verts)
    history = [bm.verts[index] for index in order if bm.verts[index] in wanted]
    points = [vertex.co.copy() for vertex in verts]
    if close and len(verts) > 2:
        normal, center = _best_fit_plane(points)
        if max(abs((point - center).dot(normal)) for point in points) > tolerance:
            raise ValueError("Selected vertices are not planar enough to connect as a face")
    if len(history) == len(verts) and set(history) == wanted:
        if close and len(history) > 3:
            if len(history) > 512:
                raise ValueError("Ordered face validation supports up to 512 corners")
            axis = max(range(3), key=lambda i: abs(normal[i]))
            planar = [Vector(tuple(v.co[i] for i in range(3) if i != axis)) for v in history]
            size = len(planar)
            for i, first in enumerate(planar):
                for j in range(i + 2, size):
                    if i == 0 and j == size - 1:
                        continue
                    if intersect_line_line_2d(first, planar[(i + 1) % size], planar[j], planar[(j + 1) % size]) is not None:
                        raise ValueError("Explicit vertex order would create a self-intersecting face")
        return list(history)
    if len(verts) == 2:
        return list(verts)
    if close:
        guide = Vector((1.0, 0.0, 0.0))
        if abs(guide.dot(normal)) > 0.9:
            guide = Vector((0.0, 1.0, 0.0))
        u = (guide - normal * guide.dot(normal)).normalized()
        v = normal.cross(u).normalized()
        angles = []
        for point in points:
            delta = point - center
            angles.append(math.atan2(delta.dot(v), delta.dot(u)))
        order = sorted(range(len(verts)), key=angles.__getitem__)
        ordered = [verts[index] for index in order]
        sorted_angles = [angles[index] for index in order]
        gaps = [sorted_angles[index + 1] - sorted_angles[index]
                for index in range(len(sorted_angles) - 1)]
        gaps.append(sorted_angles[0] + 2.0 * math.pi - sorted_angles[-1])
        if any(gap <= 1e-9 or gap >= math.pi - 1e-9 for gap in gaps):
            raise ValueError("Selected vertices are not in a convex enough loop for a face")
        return ordered
    center, values, vectors = _pca(points)
    axis = vectors[2]
    if values[2] <= 1e-30:
        raise ValueError("Selected vertices do not define an ordering direction")
    return sorted(verts, key=lambda vertex: (vertex.co - center).dot(axis))


def _existing_face(bm, verts):
    wanted = set(verts)
    for face in bm.faces:
        if set(face.verts) == wanted:
            return face
    return None


def _existing_edge(bm, first, second):
    for edge in first.link_edges:
        if edge.other_vert(first) == second:
            return edge
    return None


# ---------------------------------------------------------------------------
# Tool implementations


def _lower_recess_vertices(vertices, normal, depth, measurements):
    offset = normal * depth
    for vertex in vertices:
        before = vertex.co.copy()
        vertex.co = before - offset
        actual = (before - vertex.co).dot(normal)
        if not math.isclose(actual, depth, rel_tol=0.01):
            raise ValueError("Mesh coordinate precision cannot represent the requested recess depth within 1%")
        measurements.append(actual)


def _form_recess(bm, domain, indices, settings, context):
    faces = _elements(bm, domain, indices)
    if not faces:
        raise ValueError("Select at least one face")
    bm.normal_update()
    for face in faces:
        if not face.is_valid or face.calc_area() <= _EPS:
            raise ValueError("A selected face has no usable area")
        inradius = _face_inradius(face)
        if settings["thickness"] >= inradius * 0.95:
            raise ValueError("Inset thickness is too large for the selected face")
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    cap_faces = []
    depths = []
    if settings["individual"]:
        normals = [face.normal.copy().normalized() for face in faces]
        for face, normal in zip(faces, normals):
            if not face.is_valid:
                raise ValueError("A selected face became invalid")
            if normal.length <= 0.5:
                raise ValueError("A selected face has an invalid normal")
            bmesh.ops.inset_individual(
                bm, faces=[face], thickness=settings["thickness"], depth=0.0,
                use_even_offset=True, use_interpolate=True)
            if not face.is_valid:
                raise ValueError("Inset did not leave the selected face usable")
            if settings["mode"] == "RECESS":
                extrusion = bmesh.ops.extrude_face_region(bm, geom=[face], use_keep_orig=False)
                new_vertices = [element for element in extrusion["geom"]
                                if isinstance(element, bmesh.types.BMVert)]
                if not new_vertices:
                    raise ValueError("Recess extrusion produced no cap vertices")
                cap_faces.extend(element for element in extrusion["geom"]
                                 if isinstance(element, bmesh.types.BMFace))
                _lower_recess_vertices(new_vertices, normal, settings["depth"], depths)
                if face.is_valid:
                    bm.faces.remove(face)
            else:
                _lower_recess_vertices(face.verts, normal, settings["depth"], depths)
    else:
        bmesh.ops.inset_region(
            bm, faces=list(faces), faces_exclude=[], use_boundary=True,
            use_even_offset=True, use_interpolate=True, use_relative_offset=False,
            use_edge_rail=False, thickness=settings["thickness"], depth=0.0,
            use_outset=False)
        if any(not face.is_valid for face in faces):
            raise ValueError("Region inset invalidated a selected face")
        bm.normal_update()
        normal = _area_normal(faces)
        if settings["mode"] == "RECESS":
            extrusion = bmesh.ops.extrude_face_region(bm, geom=list(faces), use_keep_orig=False)
            new_vertices = [element for element in extrusion["geom"]
                            if isinstance(element, bmesh.types.BMVert)]
            if not new_vertices:
                raise ValueError("Recess extrusion produced no cap vertices")
            cap_faces.extend(element for element in extrusion["geom"]
                             if isinstance(element, bmesh.types.BMFace))
            _lower_recess_vertices(new_vertices, normal, settings["depth"], depths)
            for face in faces:
                if face.is_valid:
                    bm.faces.remove(face)
        else:
            inner_vertices = {vertex for face in faces for vertex in face.verts}
            _lower_recess_vertices(inner_vertices, normal, settings["depth"], depths)
    bm.normal_update()
    selected = [face for face in cap_faces if face.is_valid]
    if settings["mode"] == "COUNTERSINK":
        selected = [face for face in faces if face.is_valid]
    if not selected:
        raise ValueError("Recess produced no selectable cap")
    _select_faces(bm, selected)
    return _base_stats(bm, before_counts, before_sets, {
        "mode": settings["mode"],
        "thickness": settings["thickness"],
        "depth": settings["depth"],
        "requested_depth": settings["depth"],
        "measured_depth_min": min(depths),
        "measured_depth_max": max(depths),
        "individual": settings["individual"],
        "cap_faces": len(selected),
    })


def _form_push_pull(bm, domain, indices, settings, context):
    faces = _elements(bm, domain, indices)
    if not faces:
        raise ValueError("Select at least one face")
    distance = settings["distance"]
    if abs(distance) <= 1e-12:
        raise ValueError("Push/pull distance must be nonzero")
    bm.normal_update()
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    cap_faces = []
    if settings["individual"]:
        for face in list(faces):
            if not face.is_valid:
                raise ValueError("A selected face became invalid")
            face.normal_update()
            normal = face.normal.copy().normalized()
            if normal.length <= 0.5:
                raise ValueError("A selected face has an invalid normal")
            extrusion = bmesh.ops.extrude_face_region(bm, geom=[face], use_keep_orig=False)
            new_vertices = [element for element in extrusion["geom"]
                            if isinstance(element, bmesh.types.BMVert)]
            if not new_vertices:
                raise ValueError("Extrusion produced no cap vertices")
            cap_faces.extend(element for element in extrusion["geom"]
                             if isinstance(element, bmesh.types.BMFace))
            for vertex in new_vertices:
                vertex.co = vertex.co + normal * distance
            if face.is_valid:
                bm.faces.remove(face)
    else:
        normal = _area_normal(faces)
        extrusion = bmesh.ops.extrude_face_region(bm, geom=list(faces), use_keep_orig=False)
        new_vertices = [element for element in extrusion["geom"]
                        if isinstance(element, bmesh.types.BMVert)]
        if not new_vertices:
            raise ValueError("Extrusion produced no cap vertices")
        cap_faces.extend(element for element in extrusion["geom"]
                         if isinstance(element, bmesh.types.BMFace))
        for vertex in new_vertices:
            vertex.co = vertex.co + normal * distance
        for face in faces:
            if face.is_valid:
                bm.faces.remove(face)
    bm.normal_update()
    new_verts, new_edges, new_faces = _new_elements(bm, before_sets)
    selected = [face for face in cap_faces if face.is_valid]
    if not selected:
        raise ValueError("Extrusion produced no selectable cap")
    _select_faces(bm, selected)
    return _base_stats(bm, before_counts, before_sets, {
        "distance": distance,
        "individual": settings["individual"],
        "cap_faces": len(selected),
    })


def _vertex_merge(bm, domain, indices, settings, context):
    verts = _elements(bm, domain, indices)
    if len(verts) < 2:
        raise ValueError("Select at least two vertices to merge")
    if any(not _finite(vertex.co) for vertex in verts):
        raise ValueError("Selected vertices contain a nonfinite coordinate")
    active = settings["mode"] == "ACTIVE"
    if active:
        index = context.get("active_vertex_index")
        if index not in indices:
            raise ValueError("No active vertex is recorded; use CENTER merge mode")
        result = bm.verts[index]
        target = result.co.copy()
    else:
        target = sum((vertex.co for vertex in verts), Vector((0.0, 0.0, 0.0))) / len(verts)
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    if active:
        bmesh.ops.weld_verts(bm, targetmap={vertex: result for vertex in verts if vertex != result})
    else:
        bmesh.ops.pointmerge(bm, verts=list(verts), merge_co=target)
        survivors = [vertex for vertex in verts if vertex.is_valid]
        if len(survivors) != 1:
            raise ValueError("Merge did not leave one selected vertex")
        result = survivors[0]
    bm.normal_update()
    if not result.is_valid or result.co != target:
        raise ValueError("Merge did not produce a vertex at the requested target")
    _select_verts(bm, [result])
    bm.select_history.clear()
    bm.select_history.add(result)
    return _base_stats(bm, before_counts, before_sets, {
        "mode": settings["mode"],
        "merged_vertices": len(verts),
        "target": [target.x, target.y, target.z],
    })


def _vertex_connect(bm, domain, indices, settings, context):
    verts = _elements(bm, domain, indices)
    if len(verts) < 2:
        raise ValueError("Select at least two vertices to connect")
    if any(not _finite(vertex.co) for vertex in verts):
        raise ValueError("Selected vertices contain a nonfinite coordinate")
    ordered = _ordered_selected_verts(bm, verts, settings["close"], settings["tolerance"], context["vertex_order"])
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    wire_edges_across_faces = 0
    if settings["close"] and len(ordered) >= 3:
        existing = _existing_face(bm, ordered)
        if existing is not None:
            raise ValueError("Selected vertices already form a face")
        try:
            face = bm.faces.new(ordered)
        except ValueError as exc:
            raise ValueError("Selected vertices cannot form a valid face") from exc
        bm.normal_update()
        if not face.is_valid or face.calc_area() <= _EPS:
            raise ValueError("Connected face would have no usable area")
        _select_faces(bm, [face])
        created_faces = 1
        created_edges = 0
    else:
        created_edges = 0
        new_edges = []
        for first, second in zip(ordered, ordered[1:]):
            if first == second:
                continue
            existing = _existing_edge(bm, first, second)
            if existing is not None:
                continue
            try:
                new_edges.append(bm.edges.new((first, second)))
            except ValueError as exc:
                raise ValueError("Selected vertices cannot be connected") from exc
            if any(second in face.verts for face in first.link_faces):
                wire_edges_across_faces += 1
        if not new_edges:
            raise ValueError("Every requested connection already exists")
        created_edges = len(new_edges)
        _select_edges(bm, new_edges)
        created_faces = 0
    bm.normal_update()
    return _base_stats(bm, before_counts, before_sets, {
        "close": settings["close"],
        "connected_vertices": len(ordered),
        "created_faces": created_faces,
        "created_edges": created_edges,
        "wire_edges_across_faces": wire_edges_across_faces,
    })


def _source_geometry(bm, domain, indices):
    elements = _elements(bm, domain, indices)
    if domain == "VERT":
        return elements, [], []
    if domain == "EDGE":
        verts = list({vertex for edge in elements for vertex in edge.verts})
        return verts, elements, []
    edges = list({edge for face in elements for edge in face.edges})
    verts = list({vertex for face in elements for vertex in face.verts})
    return verts, edges, elements


def _array_common(bm, domain, indices, settings, context, radial):
    verts, edges, faces = _source_geometry(bm, domain, indices)
    if not verts:
        raise ValueError("Selection has no vertices to duplicate")
    if any(not _finite(vertex.co) for vertex in verts):
        raise ValueError("Selection contains a nonfinite coordinate")
    count = settings["count"]
    if count < 2:
        raise ValueError("Array count must be at least two")
    axis = _axis_vector(settings["axis"])
    if radial:
        center = _center_from_mode(settings["center"], verts, context)
        if max(((vertex.co - center) - axis * (vertex.co - center).dot(axis)).length
               for vertex in verts) <= 1e-9:
            raise ValueError("The selection lies on the rotation axis")
        full = abs(settings["angle"] - 360.0) <= 1e-6
        step = settings["angle"] / count if full else settings["angle"] / (count - 1)
        step = math.radians(step)
    else:
        offset = axis * settings["distance"]
        if offset.length <= 1e-12:
            raise ValueError("Linear array distance must be nonzero")
    geometry = list(verts) + list(edges) + list(faces)
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    for instance in range(1, count):
        duplicate = bmesh.ops.duplicate(bm, geom=geometry)
        new_vertices = [element for element in duplicate["geom"]
                        if isinstance(element, bmesh.types.BMVert)]
        if not new_vertices:
            raise ValueError("Array duplication produced no vertices")
        if radial:
            rotation = Matrix.Rotation(step * instance, 4, axis)
            for vertex in new_vertices:
                vertex.co = center + rotation @ (vertex.co - center)
        else:
            for vertex in new_vertices:
                vertex.co = vertex.co + offset * instance
        if any(not _finite(vertex.co) for vertex in new_vertices):
            raise ValueError("Array produced a nonfinite coordinate")
    bm.normal_update()
    new_verts, new_edges, new_faces = _new_elements(bm, before_sets)
    if domain == "FACE":
        _select_faces(bm, new_faces)
    elif domain == "EDGE":
        _select_edges(bm, new_edges)
    else:
        _select_verts(bm, new_verts)
    extra = {"count": count, "axis": settings["axis"], "instances_created": count - 1}
    if radial:
        extra.update({"angle": settings["angle"], "center_mode": settings["center"]})
    else:
        extra.update({"distance": settings["distance"]})
    return _base_stats(bm, before_counts, before_sets, extra)


def _align_axis(bm, domain, indices, settings, context):
    verts = _involved_verts(bm, domain, indices)
    if not verts:
        raise ValueError("Selection has no vertices to align")
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    neighborhood = precision_edit.remember_neighborhood(verts, guard_orientation=True)
    axis_index = {"X": 0, "Y": 1, "Z": 2}[settings["axis"]]
    values = [vertex.co[axis_index] for vertex in verts]
    if settings["mode"] == "MIN":
        target = min(values)
    elif settings["mode"] == "MAX":
        target = max(values)
    else:
        target = sum(values) / len(values)
    target += settings["offset"]
    factor = settings["factor"]
    moved = 0
    for vertex in verts:
        original = vertex.co[axis_index]
        value = original + (target - original) * factor
        if abs(value - original) > 1e-12:
            moved += 1
        vertex.co[axis_index] = value
    bm.normal_update()
    precision_edit.check_neighborhood(neighborhood, "Axis alignment")
    if domain == "FACE":
        for face in _elements(bm, domain, indices):
            if face.calc_area() <= _EPS:
                raise ValueError("Axis alignment would collapse a selected face")
    if any(not _finite(vertex.co) for vertex in verts):
        raise ValueError("Axis alignment produced a nonfinite coordinate")
    return _base_stats(bm, before_counts, before_sets, {
        "axis": settings["axis"],
        "mode": settings["mode"],
        "target": target,
        "moved_vertices": moved,
    })


def _precision_alignment(bm, domain, indices, settings, context, operation):
    verts = _involved_verts(bm, domain, indices)
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    if operation == "align.reference":
        result = precision_edit.position_reference(verts, settings, context["matrix_world"])
    else:
        result = precision_edit.snap_symmetry(bm, verts, settings)
    bm.normal_update()
    return _base_stats(bm, before_counts, before_sets, result)


def _align_plane(bm, domain, indices, settings, context):
    verts = _involved_verts(bm, domain, indices)
    if not verts:
        raise ValueError("Selection has no vertices to align")
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    reference_faces = _faces_for_domain(bm, domain, indices)
    source = settings["source"]
    if source == "FACES" and not reference_faces:
        raise ValueError("No adjacent faces are available for the selection plane")
    if source == "BEST_FIT" or (source == "AUTO" and not reference_faces):
        normal, center = _best_fit_plane([vertex.co.copy() for vertex in verts])
    else:
        if not reference_faces:
            raise ValueError("No faces are available for the selection plane")
        total_area = 0.0
        weighted_center = Vector((0.0, 0.0, 0.0))
        normal_sum = Vector((0.0, 0.0, 0.0))
        bm.normal_update()
        for face in reference_faces:
            if not face.is_valid:
                continue
            area = face.calc_area()
            total_area += area
            weighted_center += face.calc_center_median() * area
            normal_sum += face.normal * area
        if total_area <= _EPS or normal_sum.length <= 1e-12:
            raise ValueError("Adjacent faces do not define a stable plane")
        normal = normal_sum.normalized()
        center = weighted_center / total_area
    target = normal * settings["offset"]
    factor = settings["factor"]
    moved = 0
    for vertex in verts:
        distance = (vertex.co - center).dot(normal) - settings["offset"]
        if abs(distance) > 1e-12:
            moved += 1
        vertex.co = vertex.co - normal * (distance * factor)
    bm.normal_update()
    if domain == "FACE":
        for face in _elements(bm, domain, indices):
            if face.calc_area() <= _EPS:
                raise ValueError("Plane alignment would collapse a selected face")
    if any(not _finite(vertex.co) for vertex in verts):
        raise ValueError("Plane alignment produced a nonfinite coordinate")
    residual = max(abs((vertex.co - (center + target)).dot(normal)) for vertex in verts)
    return _base_stats(bm, before_counts, before_sets, {
        "source": source,
        "offset": settings["offset"],
        "normal": [normal.x, normal.y, normal.z],
        "center": [center.x, center.y, center.z],
        "max_residual": residual,
        "moved_vertices": moved,
    })


def _support_face(bm, domain, indices, settings, context):
    faces = _elements(bm, domain, indices)
    if not faces:
        raise ValueError("Select at least one face")
    for face in faces:
        if not face.is_valid or face.calc_area() <= _EPS:
            raise ValueError("A selected face has no usable area")
        if settings["width"] * settings["count"] >= _face_inradius(face) * 0.95:
            raise ValueError("Support loop width/count is too large for a selected face")
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    if settings["individual"]:
        for face in list(faces):
            for _ in range(settings["count"]):
                if not face.is_valid:
                    raise ValueError("A selected face became invalid")
                bmesh.ops.inset_individual(
                    bm, faces=[face], thickness=settings["width"], depth=0.0,
                    use_even_offset=True, use_interpolate=True)
    else:
        for _ in range(settings["count"]):
            if any(not face.is_valid for face in faces):
                raise ValueError("A selected face became invalid")
            bmesh.ops.inset_region(
                bm, faces=list(faces), faces_exclude=[], use_boundary=True,
                use_even_offset=True, use_interpolate=True, use_relative_offset=False,
                use_edge_rail=False, thickness=settings["width"], depth=0.0,
                use_outset=False)
    bm.normal_update()
    new_verts, new_edges, new_faces = _new_elements(bm, before_sets)
    _select_faces(bm, faces)
    return _base_stats(bm, before_counts, before_sets, {
        "width": settings["width"],
        "loops": settings["count"],
        "individual": settings["individual"],
    })


def _support_edge(bm, domain, indices, settings, context):
    edges = _elements(bm, domain, indices)
    if not edges:
        raise ValueError("Select at least one edge")
    if settings["width"] <= 0.0:
        raise ValueError("Support edge width must be positive")
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    result = bmesh.ops.bevel(
        bm, geom=list(edges), offset=settings["width"], offset_type="OFFSET",
        profile_type="SUPERELLIPSE", segments=1, profile=0.5, affect="EDGES",
        clamp_overlap=settings["clamp_overlap"], material=-1, loop_slide=True,
        mark_seam=False, mark_sharp=False, harden_normals=False,
        miter_outer="SHARP", miter_inner="SHARP", spread=0.0, vmesh_method="ADJ")
    bm.normal_update()
    new_verts, new_edges, new_faces = _new_elements(bm, before_sets)
    if not new_edges or not new_faces:
        raise ValueError("Support edge operation produced no support geometry")
    result_edges = [edge for edge in result.get("edges", []) if edge.is_valid]
    _select_edges(bm, result_edges or new_edges)
    stats = _base_stats(bm, before_counts, before_sets, {
        "width": settings["width"],
        "clamp_overlap": settings["clamp_overlap"],
    })
    return stats


def _form_lathe(bm, domain, indices, settings, context):
    edges = _elements(bm, domain, indices)
    if not edges:
        raise ValueError("Select a connected edge profile")
    profile_verts = list({vertex for edge in edges for vertex in edge.verts})
    degrees, endpoints = _profile_graph(edges)
    for edge in edges:
        if edge.calc_length() <= _EPS:
            raise ValueError("The profile contains a zero-length edge")
    center = _center_from_mode(settings["center"], profile_verts, context)
    axis = _axis_vector(settings["axis"]).normalized()
    radii = []
    for vertex in profile_verts:
        delta = vertex.co - center
        radius = (delta - axis * delta.dot(axis)).length
        radii.append(radius)
        if radius <= 1e-6:
            raise ValueError("The profile touches the rotation axis; move it off the axis")
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    geometry = list(edges) + profile_verts
    duplicate = bmesh.ops.duplicate(bm, geom=geometry)
    new_edges = [element for element in duplicate["geom"] if isinstance(element, bmesh.types.BMEdge)]
    new_verts = [element for element in duplicate["geom"] if isinstance(element, bmesh.types.BMVert)]
    if not new_edges or not new_verts:
        raise ValueError("Lathe could not duplicate the selected profile")
    full = abs(settings["angle"] - 360.0) <= 1e-6
    bmesh.ops.spin(
        bm, geom=new_edges + new_verts, cent=center, axis=axis,
        dvec=Vector((0.0, 0.0, 0.0)), angle=math.radians(settings["angle"]),
        space=Matrix.Identity(4), steps=settings["steps"], use_merge=full,
        use_normal_flip=False, use_duplicate=False)
    bm.normal_update()
    new_verts, new_edges, new_faces = _new_elements(bm, before_sets)
    expected = len(edges) * settings["steps"]
    if len(new_faces) != expected:
        raise ValueError(f"Lathe produced {len(new_faces)} faces; expected {expected}")
    minimum = min(radii)
    maximum = max(radii)
    tolerance = max(1e-6, maximum * 1e-6)
    for vertex in new_verts:
        delta = vertex.co - center
        radius = (delta - axis * delta.dot(axis)).length
        if not minimum - tolerance <= radius <= maximum + tolerance:
            raise ValueError("Lathe produced a vertex outside the profile radius range")
    _select_faces(bm, new_faces)
    return _base_stats(bm, before_counts, before_sets, {
        "profile_edges": len(edges),
        "steps": settings["steps"],
        "angle": settings["angle"],
        "full": full,
        "radius_min": minimum,
        "radius_max": maximum,
        "open_profile": bool(endpoints),
    })


def _plan_hole(bm, face, settings):
    if not face.is_valid or face.calc_area() <= _EPS:
        raise ValueError("A selected face has no usable area")
    bm.normal_update()
    normal = face.normal.copy().normalized()
    if normal.length <= 0.5:
        raise ValueError("A selected face has an invalid normal")
    origin = face.calc_center_median()
    outer = [loop.vert for loop in face.loops]
    if len(outer) < 3:
        raise ValueError("A selected face needs at least three boundary vertices")
    if settings["segments"] < len(outer):
        raise ValueError("Hole segments must be at least the face boundary vertex count")
    if max(abs((vertex.co - origin).dot(normal)) for vertex in outer) > settings["tolerance"]:
        raise ValueError("The selected face is not planar enough for a circular hole")
    guide = None
    for index in range(len(outer)):
        candidate = outer[(index + 1) % len(outer)].co - outer[index].co
        candidate = candidate - normal * candidate.dot(normal)
        if candidate.length > 1e-9:
            guide = candidate.normalized()
            break
    if guide is None:
        raise ValueError("The selected face has no usable boundary direction")
    u = guide
    v = normal.cross(u).normalized()
    if v.length <= 0.5:
        raise ValueError("Cannot build a plane basis for the selected face")
    offsets = [vertex.co - origin for vertex in outer]
    plane_offsets = [offset - normal * offset.dot(normal) for offset in offsets]
    coords = [(offset.dot(u), offset.dot(v)) for offset in plane_offsets]
    center_mode = settings["center_mode"]
    if center_mode == "EXPLICIT":
        reference = Vector(settings["center_local"]) - origin
        if abs(reference.dot(normal)) > settings["tolerance"]:
            raise ValueError("Explicit hole center is outside the selected face plane tolerance")
        center_u, center_v = reference.dot(u), reference.dot(v)
    elif center_mode == "AREA_CENTROID":
        center_u, center_v = _polygon_centroid(coords)
    else:
        center_u, center_v = 0.0, 0.0
    center = origin + center_u * u + center_v * v
    angles = []
    projected = []
    for x, y in coords:
        x, y = x - center_u, y - center_v
        projected.append((x, y))
        angles.append(math.atan2(y, x))
    if not _point_in_polygon((0.0, 0.0), projected):
        raise ValueError("The face center is outside the boundary; the face is not star-shaped enough")
    unwrapped = [angles[0]]
    for angle in angles[1:]:
        while angle <= unwrapped[-1] + 1e-12:
            angle += 2.0 * math.pi
        unwrapped.append(angle)
    deltas = [unwrapped[index + 1] - unwrapped[index]
              for index in range(len(unwrapped) - 1)]
    deltas.append(unwrapped[0] + 2.0 * math.pi - unwrapped[-1])
    if any(delta <= 1e-9 or delta >= math.pi - 1e-9 for delta in deltas):
        raise ValueError("Face boundary subtends too much angle for a centered hole")
    if abs(sum(deltas) - 2.0 * math.pi) > 1e-6:
        raise ValueError("Face boundary angles are inconsistent")
    minimum_distance = min(
        _point_segment_distance_2d((0.0, 0.0), projected[index],
                                   projected[(index + 1) % len(projected)])
        for index in range(len(projected)))
    remaining = settings["segments"] - len(outer)
    quotas = [remaining * delta / (2.0 * math.pi) for delta in deltas]
    counts = [1 + int(math.floor(quota)) for quota in quotas]
    left = settings["segments"] - sum(counts)
    order = sorted(range(len(outer)),
                   key=lambda index: quotas[index] - math.floor(quotas[index]),
                   reverse=True)
    for index in order[:left]:
        counts[index] += 1
    if sum(counts) != settings["segments"]:
        raise ValueError("Could not allocate hole segments")
    radius = settings["radius"]
    if settings["radius_mode"] == "CLEAR_RADIUS":
        max_step = max(delta / count for delta, count in zip(deltas, counts))
        radius /= math.cos(max_step / 2.0)
    if radius >= minimum_distance - 1e-9:
        raise ValueError("Hole radius is too large for the selected face")
    return {
        "face": face,
        "normal": normal,
        "center": center,
        "outer": outer,
        "u": u,
        "v": v,
        "angles": unwrapped,
        "deltas": deltas,
        "counts": counts,
        "radius": radius,
        "material_index": face.material_index,
        "smooth": face.smooth,
    }


def _polygon_centroid(points):
    twice_area = x_sum = y_sum = 0.0
    for (x, y), (next_x, next_y) in zip(points, points[1:] + points[:1]):
        cross = x * next_y - next_x * y
        twice_area += cross
        x_sum += (x + next_x) * cross
        y_sum += (y + next_y) * cross
    if abs(twice_area) <= _EPS:
        raise ValueError("Cannot calculate the selected face area centroid")
    return x_sum / (3.0 * twice_area), y_sum / (3.0 * twice_area)


def _point_segment_distance_2d(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq <= _EPS:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    factor = max(0.0, min(1.0,
                            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq))
    x = start[0] + dx * factor
    y = start[1] + dy * factor
    return math.hypot(point[0] - x, point[1] - y)


def _point_in_polygon(point, polygon):
    x, y = point
    inside = False
    for index in range(len(polygon)):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % len(polygon)]
        if (y1 > y) != (y2 > y):
            crossing = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if crossing > x:
                inside = not inside
    return inside


def _rim_edge_distance(center, a, b):
    edge = tuple(end - start for start, end in zip(a, b))
    delta = tuple(position - start for position, start in zip(center, a))
    squared = math.fsum(component * component for component in edge)
    if squared == 0.0:
        return math.dist(center, a)
    factor = math.fsum(offset * component for offset, component in zip(delta, edge)) / squared
    fraction = min(1.0, max(0.0, factor))
    return math.hypot(*(offset - fraction * component for offset, component in zip(delta, edge)))


def _form_hole(bm, domain, indices, settings, context):
    faces = _elements(bm, domain, indices)
    if not faces:
        raise ValueError("Select at least one face")
    if settings["center_mode"] == "EXPLICIT" and len(faces) != 1:
        raise ValueError("Explicit hole center requires exactly one selected face")
    plans = [_plan_hole(bm, face, settings) for face in faces]
    before_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    before_sets = _snapshot(bm)
    created_faces = []
    created_verts = []
    clear_diameters = []
    rim_radii = []
    try:
        for plan in plans:
            radius = plan["radius"]
            u, v, center = plan["u"], plan["v"], plan["center"]
            outer = plan["outer"]
            seams = []
            for angle in plan["angles"]:
                vertex = bm.verts.new(center + radius * (math.cos(angle) * u + math.sin(angle) * v))
                created_verts.append(vertex)
                seams.append(vertex)
            arcs = []
            for index in range(len(outer)):
                points = [seams[index]]
                for step in range(1, plan["counts"][index]):
                    angle = plan["angles"][index] + plan["deltas"][index] * step / plan["counts"][index]
                    vertex = bm.verts.new(
                        center + radius * (math.cos(angle) * u + math.sin(angle) * v))
                    created_verts.append(vertex)
                    points.append(vertex)
                points.append(seams[(index + 1) % len(outer)])
                arcs.append(points)
            clear_radius = min(_rim_edge_distance(center, first.co, second.co)
                               for arc in arcs for first, second in zip(arc, arc[1:]))
            if settings["radius_mode"] == "CLEAR_RADIUS":
                requested = settings["radius"]
                if clear_radius < requested - max(1e-9, requested * 1e-6):
                    raise ValueError("The stored hole rim cannot achieve the requested clear radius")
            clear_diameters.append(2.0 * clear_radius)
            rim_radii.append([(vertex.co - center).length for arc in arcs for vertex in arc[:-1]])
            for index in range(len(outer)):
                polygon = [outer[index], outer[(index + 1) % len(outer)]] + list(reversed(arcs[index]))
                face = bm.faces.new(polygon)
                face.material_index = plan["material_index"]
                face.smooth = plan["smooth"]
                face.normal_update()
                if face.normal.dot(plan["normal"]) < 0.0:
                    face.normal_flip()
                original_loops = {loop.vert: loop for loop in plan["face"].loops}
                face.copy_from_face_interp(plan["face"], False)
                for loop in face.loops:
                    if loop.vert in original_loops:
                        loop.copy_from(original_loops[loop.vert])
                    else:
                        loop.copy_from_face_interp(plan["face"], True, False)
                created_faces.append(face)
        for plan in plans:
            bm.faces.remove(plan["face"])
    except Exception:
        for face in reversed(created_faces):
            if face.is_valid:
                bm.faces.remove(face)
        for vertex in reversed(created_verts):
            if vertex.is_valid:
                bm.verts.remove(vertex)
        raise
    bm.normal_update()
    new_verts, new_edges, new_faces = _new_elements(bm, before_sets)
    _select_faces(bm, new_faces)
    hole_edges = sum(1 for edge in new_edges if len(edge.link_faces) == 1)
    expected_hole_edges = settings["segments"] * len(plans)
    if hole_edges != expected_hole_edges:
        raise ValueError(f"Hole boundary has {hole_edges} open edges; expected {expected_hole_edges}")
    return _base_stats(bm, before_counts, before_sets, {
        "radius": settings["radius"],
        "requested_radius_local": settings["radius"],
        "radius_mode": settings["radius_mode"],
        "center_mode": settings["center_mode"],
        "centers_local": [list(plan["center"]) for plan in plans],
        "clear_diameters_local": clear_diameters,
        "rim_vertex_radius_min_local": min(min(radii) for radii in rim_radii),
        "rim_vertex_radius_max_local": max(max(radii) for radii in rim_radii),
        "segments": settings["segments"],
        "holes": len(plans),
        "hole_edges": hole_edges,
    })


_HANDLERS = {
    "form.recess": _form_recess,
    "form.push_pull": _form_push_pull,
    "vertex.merge": _vertex_merge,
    "vertex.connect": _vertex_connect,
    "array.linear": lambda bm, domain, indices, settings, context:
        _array_common(bm, domain, indices, settings, context, False),
    "array.radial": lambda bm, domain, indices, settings, context:
        _array_common(bm, domain, indices, settings, context, True),
    "align.axis": _align_axis,
    "align.plane": _align_plane,
    "align.reference": lambda bm, domain, indices, settings, context:
        _precision_alignment(bm, domain, indices, settings, context, "align.reference"),
    "mesh.snap_symmetry": lambda bm, domain, indices, settings, context:
        _precision_alignment(bm, domain, indices, settings, context, "mesh.snap_symmetry"),
    "support.face": _support_face,
    "support.edge": _support_edge,
    "form.lathe": _form_lathe,
    "form.hole": _form_hole,
}


# ---------------------------------------------------------------------------
# Result validation and public run


def _validate(bm, stats, spec, before_counts):
    _lookup(bm)
    for vertex in bm.verts:
        if not _finite(vertex.co):
            raise ValueError("Operation produced a nonfinite vertex coordinate")
    new_verts = stats.get("_new_verts", [])
    new_edges = stats.get("_new_edges", [])
    new_faces = stats.get("_new_faces", [])
    for edge in new_edges:
        if not edge.is_valid:
            raise ValueError("Operation returned an invalid edge")
        if (edge.verts[0].co - edge.verts[1].co).length <= 1e-14:
            raise ValueError("Operation produced a zero-length edge")
    for face in new_faces:
        if not face.is_valid:
            raise ValueError("Operation returned an invalid face")
        if face.calc_area() <= 1e-14:
            raise ValueError("Operation produced a zero-area face")
    after_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    if spec["topology_changes"]:
        if after_counts == before_counts:
            raise ValueError("Operation did not change mesh topology")
    elif after_counts != before_counts:
        raise ValueError("Geometry-only operation unexpectedly changed topology")


def _public_stats(stats):
    result = {}
    for key, value in stats.items():
        if key.startswith("_"):
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("Operation produced a nonfinite measurement")
            result[key] = round(value, 9)
        elif isinstance(value, (int, str, bool)) or value is None:
            result[key] = value
        elif isinstance(value, (list, tuple)):
            result[key] = [round(item, 9) if isinstance(item, float) else item for item in value]
        else:
            raise ValueError(f"Operation returned a non-serializable value for {key}")
    return result


def run(tool, params=None):
    """Run one tool once against the current single-mesh Edit Mode selection."""
    if tool not in TOOLS:
        raise ValueError(f"Unknown tool: {tool!r}")
    spec = describe(tool)
    settings = _parameters(spec, params)
    obj, live, domain, indices, context = _mesh_context(spec)
    before_counts = (len(live.verts), len(live.edges), len(live.faces))
    handler = _HANDLERS[tool]

    trial = live.copy()
    _lookup(trial)
    try:
        trial_stats = handler(trial, domain, indices, settings, context)
        _validate(trial, trial_stats, spec, before_counts)
        _public_stats(trial_stats)
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"{tool} could not produce valid geometry: {exc}") from exc
    finally:
        trial.free()

    try:
        stats = handler(live, domain, indices, settings, context)
        _validate(live, stats, spec, before_counts)
        live.normal_update()
        bmesh.update_edit_mesh(obj.data, loop_triangles=True,
                               destructive=bool(spec["topology_changes"]))
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"{tool} failed while updating the edit mesh: {exc}") from exc

    result = {"tool": tool, "status": "FINISHED"}
    result.update(_public_stats(stats))
    return result


def register():
    """No RNA classes are owned by this feature backend."""


def unregister():
    """No RNA classes are owned by this feature backend."""
