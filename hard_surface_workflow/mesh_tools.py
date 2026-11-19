"""Native and bundled mesh operations for a single Blender Edit Mesh."""

from copy import deepcopy
import heapq
import math


def option(kind, default, description, *, minimum=None, maximum=None, enum=None, items=None):
    result = {"type": kind, "default": default, "description": description}
    for key, value in (("min", minimum), ("max", maximum), ("enum", enum), ("items", items)):
        if value is not None:
            result[key] = value
    return result


def boolean(default, description):
    return option("boolean", default, description)


def unit(default, description):
    return option("number", default, description, minimum=0.0, maximum=1.0)


def number(default, description, minimum, maximum):
    return option("number", default, description, minimum=minimum, maximum=maximum)


def integer(default, description, minimum, maximum):
    return option("integer", default, description, minimum=minimum, maximum=maximum)


def text_option(default, description, *choices):
    return option("string", default, description, enum=list(choices))


def flags(description, *choices):
    return {**option("array", [], description, items={"type": "string", "enum": list(choices)}),
            "uniqueItems": True}


def index_array(description, limit, *, required=False):
    rule = {**option("array", [], description, items={"type": "integer", "min": 0}),
            "minItems": 2 if required else 0, "maxItems": limit, "uniqueItems": True}
    if required:
        del rule["default"]
        rule["required"] = True
    return rule


def axis_lock():
    return {**option("array", [False, False, False], "锁定局部 X、Y、Z 坐标",
                     items={"type": "boolean"}), "minItems": 3, "maxItems": 3}


AXES = {"xaxis": boolean(True, "调整局部 X"),
        "yaxis": boolean(True, "调整局部 Y"),
        "zaxis": boolean(True, "调整局部 Z")}
LOOP_FLAGS = flags("原生环选终止条件", "SEAM", "SHARP", "NGONS", "INNER_CORNERS", "OUTER_CORNERS")
DISSOLVE_FLAGS = flags("禁止跨过这些边界", "NORMAL", "MATERIAL", "SEAM", "SHARP", "UV")
FLOW_MIX = {"mix": unit(1.0, "源位置与拟合位置的混合比例")}
INTERPOLATION = ("LINEAR", "PATH", "SURFACE")


def definition(tool, label, description, domains, topology_changes, parameters, *, operator=None, effect=None):
    result = {
        "tool": tool, "label": label, "description": description, "parameters": parameters,
        "context": "SINGLE_EDIT_MESH",
        "domains": list(domains), "topology_changes": topology_changes,
        "effect": effect or ("topology" if topology_changes else "geometry"),
        "coordinates_only": not topology_changes and effect not in ("winding", "attributes"),
        "selection_expansion": effect == "selection",
        "selection_expansion_conditions": "从已选种子扩展选区，几何不动。" if effect == "selection" else "",
        "repeat": "每次调用作用于当前选区和当前网格。拓扑操作后应按结果重建选区。"
        if topology_changes else "每次从当前网格和选区重新计算。",
    }
    if operator:
        result["native_operator"] = operator if "." in operator else f"mesh.{operator}"
    return result


def native(name, label, description, domains, topology_changes, operator, parameters, *, effect=None):
    return definition(f"native.{name}", label, description, domains, topology_changes, parameters,
                      operator=operator, effect=effect)


def edgeflow(name, label, description, parameters):
    return definition(f"edgeflow.{name}", label, description, ("EDGE",), False, parameters)


TOOLS = {tool["tool"]: tool for tool in (
    native("smooth", "顶点平滑", "用 Blender 顶点平滑移动已选点，支持逐轴限制。",
           ("VERT", "EDGE", "FACE"), False, "vertices_smooth",
           {"factor": unit(0.5, "平滑因子"), "repeat": integer(1, "迭代次数", 1, 100), **AXES}),
    native("smooth_normal", "沿顶点法向平滑", "从邻点平均位置计算位移，只沿各轮开始时的顶点法线移动已选点。",
           ("VERT", "EDGE", "FACE"), False, None,
           {"factor": unit(0.5, "法向位移因子"), "repeat": integer(1, "迭代次数", 1, 100)}),
    native("recalc_normals", "重算面朝向", "在已选面及相邻面关系中统一面绕序。",
           ("FACE",), False, "normals_make_consistent", {"inside": boolean(False, "朝内统一")},
           effect="winding"),
    native("flip_normals", "翻转面朝向", "翻转已选面的绕序。",
           ("FACE",), False, "flip_normals", {}, effect="winding"),
    native("space_evenly", "循环等距", "用 Blender 5.2 循环等距在原曲线或线性路径上重分布已选环。",
           ("EDGE",), False, "space_edge_loops_evenly",
           {"factor": unit(1.0, "重分布混合"),
            "interpolation": text_option("CUBIC", "采样插值", "CUBIC", "LINEAR"),
            "lock": axis_lock()}),
    native("circularize", "原生圆化", "Blender 5.2 按局部拟合圆调整有相邻面的选中环。",
           ("VERT", "EDGE"), False, "circularize",
           {"factor": unit(1.0, "圆化混合"),
            "fit_method": text_option("LEAST_SQUARE", "拟合方式", "LEAST_SQUARE", "CONTRACT"),
            "angle": number(0.0, "绕拟合圆法向旋转（弧度）", -2 * math.pi, 2 * math.pi),
            "use_custom_radius": boolean(False, "使用指定半径"),
            "custom_radius": number(1.0, "局部 Blender 单位半径", 1e-9, 1000.0),
            "regular": boolean(True, "沿圆周等距"), "flatten": unit(1.0, "投影至拟合平面"),
            "lock": axis_lock()}),
    native("flatten", "原生压平", "将选区压到拟合平面或沿选区法向调整。",
           ("VERT", "EDGE", "FACE"), False, "flatten",
           {"factor": unit(1.0, "压平混合"),
            "method": text_option("BEST_FIT", "平面模式", "BEST_FIT", "NORMAL"),
            "lock": axis_lock()}),
    native("select_loop", "原生环选", "从已选种子边沿拓扑选择边环。",
           ("EDGE",), False, "select_edge_loop_multi", {"delimit_edge_loop": LOOP_FLAGS}, effect="selection"),
    native("rotate_edge", "旋转共边", "在选择的一条双面共边上执行原生边旋转。",
           ("EDGE",), True, "edge_rotate", {"use_ccw": boolean(False, "逆向旋转")}),
    native("edge_slide", "原生边滑移", "沿邻接边轨移动选中边环，正负方向沿用 Blender 当前轨道。",
           ("EDGE",), False, "transform.edge_slide",
           {"value": number(0.25, "带符号的滑移比例", -1.0, 1.0),
            "single_side": boolean(False, "采用单侧轨道"),
            "use_even": boolean(False, "使滑移距离均匀"),
            "flipped": boolean(False, "切换均匀滑移所参照的一侧"),
            "use_clamp": boolean(True, "限制在邻接轨道范围")}),
    native("dissolve_edges", "溶解边", "移除指定边并合并其合法相邻区域。",
           ("EDGE",), True, "dissolve_edges",
           {"use_verts": boolean(False, "同时溶解多余顶点"),
            "angle_threshold": number(math.pi, "原生角度阈值（弧度）", 0.0, math.pi),
            "use_face_split": boolean(False, "允许拆面"),
            "use_preserve_quads": boolean(True, "采用原生保留四边面选项")}),
    native("dissolve_verts", "溶解顶点", "溶解明确选中的顶点并重接周围面。",
           ("VERT",), True, "dissolve_verts",
           {"use_face_split": boolean(False, "允许拆面"),
            "use_boundary_tear": boolean(False, "允许拆开边界")}),
    native("dissolve_faces", "溶解面", "合并所选相邻面，保留外轮廓。",
           ("FACE",), True, "dissolve_faces", {"use_verts": boolean(False, "同时溶解中间顶点")}),
    native("dissolve_limited", "有限溶解", "按面间夹角和分隔属性溶解近共面区域。",
           ("VERT", "EDGE", "FACE"), True, "dissolve_limited",
           {"angle_limit": number(math.radians(5.0), "最大允许角（弧度）", 0.0, math.pi),
            "use_dissolve_boundaries": boolean(False, "允许溶解边界顶点"),
            "delimit": DISSOLVE_FLAGS}),
    native("dissolve_degenerate", "溶解退化边", "在选区内溶解小于阈值的退化边。",
           ("VERT", "EDGE", "FACE"), True, "dissolve_degenerate",
           {"threshold": number(0.0001, "局部 Blender 单位阈值", 1e-9, 10.0)}),
    native("bridge", "桥接边环", "桥接选中的边界，支持中间截面、扭转及插值。",
           ("EDGE",), True, "bridge_edge_loops",
           {"type": text_option("SINGLE", "原生连接方式", "SINGLE", "CLOSED", "PAIRS"),
            "use_merge": boolean(False, "合并边环"),
            "merge_factor": unit(0.5, "合并位置"),
            "twist_offset": integer(0, "扭转偏置", -10000, 10000),
            "number_cuts": integer(0, "新增中间截面数", 0, 100),
            "interpolation": text_option("PATH", "中间截面插值", *INTERPOLATION),
            "smoothness": unit(1.0, "中间截面平滑度"),
            "profile_shape_factor": number(0.0, "中间截面轮廓尺寸变化", -1.0, 1.0),
            "profile_shape": text_option("SMOOTH", "中间截面的分布", "SMOOTH", "SPHERE", "ROOT",
                                         "INVERSE_SQUARE", "SHARP", "LINEAR")}),
    native("grid_fill", "网格填充", "从所选合法周界生成四边面填充。span=0 使用原生自动跨度。",
           ("EDGE",), True, "fill_grid",
           {"span": integer(0, "格宽，0 由 Blender 决定", 0, 10000),
            "offset": integer(0, "周界偏移", -10000, 10000),
            "use_interp_simple": boolean(False, "采用简单插值")}),
    native("subdivide", "编辑网格细分", "将选中的边和面细分为新的控制网格。",
           ("EDGE", "FACE"), True, "subdivide",
           {"number_cuts": integer(1, "每边增切数", 1, 100),
            "smoothness": unit(0.0, "新增点平滑度"),
            "ngon": boolean(True, "允许原生生成 N 边面"),
            "quadcorner": text_option("STRAIGHT_CUT", "四边面切角方式", "INNERVERT", "PATH",
                                       "STRAIGHT_CUT", "FAN")}),
    native("bevel", "编辑网格倒角", "给所选点或边作几何倒角，使用本地 Blender 单位或百分比。",
           ("VERT", "EDGE"), True, "bevel",
           {"offset_type": text_option("OFFSET", "宽度定义", "OFFSET", "WIDTH", "DEPTH", "PERCENT", "ABSOLUTE"),
            "offset": number(0.05, "宽度，Blender 局部单位", 0.0, 1000.0),
            "offset_pct": number(5.0, "偏移百分比", 0.0, 100.0),
            "segments": integer(1, "倒角分段", 1, 100), "profile": unit(0.5, "圆角轮廓"),
            "affect": text_option("EDGES", "原生作用域", "EDGES", "VERTICES"),
            "clamp_overlap": boolean(True, "限制重叠"),
            "loop_slide": boolean(True, "沿相邻边滑移"),
            "mark_seam": boolean(False, "标记 UV 缝"), "mark_sharp": boolean(False, "标记锐边"),
            "harden_normals": boolean(False, "调整倒角相邻法线"),
            "miter_outer": text_option("SHARP", "外角类型", "SHARP", "PATCH", "ARC"),
            "miter_inner": text_option("SHARP", "内角类型", "SHARP", "ARC")}),
    native("merge_by_distance", "按距离合并", "仅在选中的顶点之间按阈值焊接。",
           ("VERT", "EDGE", "FACE"), True, "remove_doubles",
           {"threshold": number(0.0001, "局部 Blender 单位阈值", 1e-9, 10.0),
            "use_centroid": boolean(True, "使用合并点平均位置"),
            "use_sharp_edge_from_normals": boolean(False, "由法线差生成锐边")}),
    native("inset", "面内插", "在已选面或连续面区创建内插边环。",
           ("FACE",), True, "inset",
           {"thickness": number(0.05, "内插距离", 0.0, 1000.0),
            "depth": number(0.0, "法线深度", -1000.0, 1000.0),
            "use_individual": boolean(False, "独立内插每张选中面"),
            "use_outset": boolean(False, "向外扩展"),
            "use_boundary": boolean(True, "处理边界")}),
    native("mark_sharp", "设定锐边", "保存或清除选中边的锐边属性。",
           ("EDGE",), False, "mark_sharp", {"clear": boolean(False, "清除锐边标记")}, effect="attributes"),
    native("mark_seam", "设定 UV 缝", "保存或清除选中边的 UV 缝属性。",
           ("EDGE",), False, "mark_seam", {"clear": boolean(False, "清除 UV 缝标记")}, effect="attributes"),
    native("edge_crease", "设定边折痕", "写入选中边的 Blender 折痕权重。0 表示清除。",
           ("EDGE",), False, None, {"value": unit(0.5, "绝对折痕权重")}, effect="attributes"),
    native("edge_bevel_weight", "设定边倒角权重", "写入选中边的 Blender 倒角权重。0 表示清除。",
           ("EDGE",), False, None, {"value": unit(0.5, "绝对倒角权重")}, effect="attributes"),
    native("shade_smooth", "光滑着色", "使选中面使用光滑着色。",
           ("FACE",), False, "faces_shade_smooth", {}, effect="attributes"),
    native("shade_flat", "平面着色", "使选中面使用平面着色。",
           ("FACE",), False, "faces_shade_flat", {}, effect="attributes"),
    native("triangulate_faces", "三角化面", "将当前选择的四边面或 N 边面拆为实际三角面。",
           ("FACE",), True, "quads_convert_to_tris",
           {"quad_method": text_option("BEAUTY", "四边面切分方式", "BEAUTY", "FIXED", "FIXED_ALTERNATE",
                                       "SHORTEST_DIAGONAL", "LONGEST_DIAGONAL"),
            "ngon_method": text_option("BEAUTY", "N 边面切分方式", "BEAUTY", "CLIP")}),
    native("tris_to_quads", "三角合并为四边", "在角度、材质及缝约束内合并相邻所选三角面。",
           ("FACE",), True, "tris_convert_to_quads",
           {"face_threshold": number(math.radians(40.0), "法向夹角阈值（弧度）", 0.0, math.pi),
            "shape_threshold": number(math.radians(40.0), "形状夹角阈值（弧度）", 0.0, math.pi),
            "topology_influence": unit(0.0, "邻域拓扑影响"),
            "uvs": boolean(False, "尊重 UV 边界"), "vcols": boolean(False, "尊重顶点色边界"),
            "seam": boolean(False, "尊重 UV 缝"), "sharp": boolean(False, "尊重锐边"),
            "materials": boolean(False, "尊重材质边界"),
            "deselect_joined": boolean(False, "取消选择合并面")}),
    edgeflow("set_flow", "EdgeFlow 随形", "沿选中环的横向邻接四边面做 Hermite 曲面拟合。",
             {**FLOW_MIX, "tension": integer(180, "EdgeFlow 曲率张力", -500, 500),
              "iterations": integer(8, "拟合迭代次数", 1, 32),
              "min_angle": integer(0, "低于该角度时线性外推（度）", 0, 180),
              "blend_mode": text_option("ABSOLUTE", "端部过渡长度单位", "ABSOLUTE", "FACTOR"),
              "blend_start_int": integer(0, "起点过渡点数", 0, 10000),
              "blend_end_int": integer(0, "终点过渡点数", 0, 10000),
              "blend_start_float": unit(0.0, "起点过渡比例"),
              "blend_end_float": unit(0.0, "终点过渡比例"),
              "blend_type": text_option("LINEAR", "端部过渡函数", "LINEAR", "SMOOTH")}),
    edgeflow("set_linear", "EdgeFlow 拉直", "沿原生 EdgeFlow 直线投影或等距移动开链内部点。",
             {**FLOW_MIX, "space_evenly": boolean(False, "拉直后沿端点连线等距")}),
    edgeflow("set_curve", "EdgeFlow 重建曲线", "以选中开链端点和可选导轨重建边曲线。",
             {**FLOW_MIX, "tension": integer(100, "EdgeFlow 曲率张力", -500, 500),
              "use_rail": boolean(False, "采用端部边作为导轨"),
              "rail_mode": text_option("FACTOR", "导轨长度单位", "ABSOLUTE", "FACTOR"),
              "rail_start": number(1.0, "起点导轨长度或系数", 0.0, 1000.0),
              "rail_end": number(1.0, "终点导轨长度或系数", 0.0, 1000.0)}),
    {
        **definition("edgeflow.set_vertex_curve", "EdgeFlow 顶点曲线",
                     "按显式顶点顺序和连续边路径拟合曲线。两个端点固定，中间导向点受模式及参数影响。",
                     ("VERT",), False,
                     {**FLOW_MIX, "anchors": index_array("按次序给出两个端点及可选导向点", 32, required=True),
                      "path": index_array("完整有序顶点路径，空列表只接受唯一最短路径", 512),
                      "use_topology_distance": boolean(False, "按边数寻路，默认按局部边长"),
                      "tension": integer(0, "EdgeFlow 曲线张力", -500, 500),
                      "flip": boolean(False, "两点圆弧反向"),
                      "rotate": boolean(False, "两点圆弧绕端点连线转 90 度"),
                      "space_evenly": boolean(False, "三点或多点模式下全路径等距")}),
        "selection_expansion": True,
        "selection_expansion_conditions": "路径含未选顶点时要求顶层 allow_expansion，保护内部路径顶点会拒绝。",
    },
    definition("edit_mesh_tools.offset_edges", "Edit Mesh Tools 偏移边",
               "按现有边链或闭环计算偏移，支持移动、平行轮廓和边挤出。",
               ("EDGE",), True,
               {"geometry_mode": text_option("offset", "偏移几何方式", "offset", "extrude", "move"),
                "width": number(0.2, "有符号局部宽度", -1000.0, 1000.0),
                "depth": number(0.0, "有符号局部深度", -1000.0, 1000.0),
                "follow_face": boolean(False, "沿相邻面确定偏移方向"),
                "edge_rail": boolean(False, "采用内部边导轨"),
                "edge_rail_only_end": boolean(False, "导轨仅应用于端点"),
                "mirror_modifier": boolean(False, "耦合编辑时可见的 Mirror 合并平面"),
                "threshold": number(math.radians(0.05), "平面判定阈值（弧度）", 0.0, math.pi)}),
)}


def describe(tool):
    if tool not in TOOLS:
        raise ValueError(f"Unknown edit tool: {tool!r}")
    return deepcopy(TOOLS[tool])


def _parameters(spec, params):
    rules = spec["parameters"]
    if params is None:
        params = {}
    if not isinstance(params, dict) or not all(isinstance(key, str) for key in params):
        raise ValueError("Edit tool parameters must be a JSON object")
    extra = params.keys() - rules.keys()
    if extra:
        raise ValueError(f"Unknown parameters for {spec['tool']}: {', '.join(sorted(extra))}")
    settings = {}
    python_types = {"boolean": (bool,), "integer": (int,), "number": (int, float),
                    "string": (str,), "array": (list,)}
    for name, rule in rules.items():
        if rule.get("required") and name not in params:
            raise ValueError(f"{name} is required")
        value = params.get(name, rule.get("default"))
        if type(value) not in python_types[rule["type"]]:
            raise ValueError(f"{name} must be {rule['type']}")
        if rule["type"] in ("number", "integer") and (
                value < rule["min"] or value > rule["max"] or not math.isfinite(value)):
            raise ValueError(f"{name} must be finite and between {rule['min']} and {rule['max']}")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"{name} must be one of {rule['enum']}")
        if rule["type"] == "array":
            if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", math.inf):
                raise ValueError(f"{name} contains an unsupported number of values")
            item_rule = rule["items"]
            for item in value:
                if (type(item) not in python_types[item_rule["type"]] or
                        ("enum" in item_rule and item not in item_rule["enum"]) or
                        ("min" in item_rule and item < item_rule["min"])):
                    raise ValueError(f"{name} contains an invalid {item_rule['type']} value")
            if rule.get("uniqueItems") and len(set(value)) != len(value):
                raise ValueError(f"{name} must not contain duplicate values")
        settings[name] = value
    return settings


def _edit_selection(spec):
    import bmesh
    import bpy

    context = bpy.context
    obj = context.edit_object
    if context.mode != "EDIT_MESH" or obj is None or len(context.objects_in_mode) != 1:
        raise RuntimeError("Tool requires exactly one mesh in Edit Mode")
    domains = [name for name, enabled in zip(("VERT", "EDGE", "FACE"),
                                           context.tool_settings.mesh_select_mode) if enabled]
    if len(domains) != 1 or domains[0] not in spec["domains"]:
        raise RuntimeError(f"{spec['tool']} expects one selection mode from {spec['domains']}")
    bm = bmesh.from_edit_mesh(obj.data)
    domain = domains[0]
    source = {"VERT": bm.verts, "EDGE": bm.edges, "FACE": bm.faces}[domain]
    selected = [element for element in source if element.select and not element.hide]
    if not selected:
        raise RuntimeError(f"{spec['tool']} requires a nonempty {domain} selection")
    return obj, bm, domain, selected


def _native(spec, domain, selection, settings):
    import bpy

    tool = spec["tool"]
    namespace, short_name = spec["native_operator"].split(".")
    op = getattr(getattr(bpy.ops, namespace), short_name)
    if not op.poll():
        raise RuntimeError(f"{spec['native_operator']} is unavailable in the current Edit Mesh context")
    call = {name: (tuple(value) if name == "lock" else set(value)) if isinstance(value, list) else value
            for name, value in settings.items()}
    if tool == "native.smooth":
        if not any(settings[key] for key in AXES):
            raise ValueError("At least one smoothing axis must be enabled")
        call["wait_for_input"] = False
    if tool == "native.edge_slide":
        # The backend has a geometry-only contract. UI transform defaults may also move UVs.
        call["correct_uv"] = False
    if tool == "native.circularize" and any(not element.link_faces for element in selection):
        raise ValueError("Native Circularize needs face-supported vertices or edges")
    if tool == "native.rotate_edge":
        if len(selection) != 1 or not selection[0].is_manifold:
            raise ValueError("Select one shared manifold edge for rotation")
    if tool == "native.bevel":
        expected = "VERTICES" if domain == "VERT" else "EDGES"
        if settings["affect"] != expected:
            raise ValueError(f"Bevel affect={settings['affect']} conflicts with {domain} selection")
    if tool == "native.grid_fill" and settings["span"] == 0:
        # Omitting the property leaves span unset so Blender computes it.
        del call["span"]
    return "FINISHED" if "FINISHED" in op("EXEC_DEFAULT", **call) else "CANCELLED"


def _set_edge_weight(obj, mesh, tool, edges, value):
    import bmesh

    name = "crease_edge" if tool == "native.edge_crease" else "bevel_weight_edge"
    attr = obj.data.attributes.get(name)
    if attr is not None and (attr.domain != "EDGE" or attr.data_type != "FLOAT"):
        raise ValueError(f"Expected {name} to be a FLOAT attribute on EDGE")
    mesh.edges.index_update()
    selected_indices = [edge.index for edge in edges]
    layer = mesh.edges.layers.float.get(name)
    if layer is None:
        layer = mesh.edges.layers.float.new(name)
    mesh.edges.ensure_lookup_table()
    for index in selected_indices:
        mesh.edges[index][layer] = value
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)


def _smooth_normal(obj, mesh, settings):
    import bmesh
    from mathutils import Vector

    selected = [v for v in mesh.verts if v.select and not v.hide and v.link_edges]
    if not selected:
        raise ValueError("Normal smoothing needs selected vertices with connected edges")
    for _ in range(settings["repeat"]):
        mesh.normal_update()
        positions = []
        for vert in selected:
            adjacent = [edge.other_vert(vert).co for edge in vert.link_edges]
            mean = sum(adjacent, Vector((0.0, 0.0, 0.0))) / len(adjacent)
            normal = vert.normal.normalized()
            positions.append(vert.co + normal * ((mean - vert.co).dot(normal) * settings["factor"]))
        if any(not all(map(math.isfinite, point)) for point in positions):
            raise ValueError("Normal smoothing produced a nonfinite coordinate")
        for vert, point in zip(selected, positions):
            vert.co = point
    mesh.normal_update()
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)


def _edgeflow(obj, mesh, tool, edges, settings):
    import bmesh
    from .vendor.edgeflow import util

    edges = set(edges)
    if any(not edge.link_faces or len(edge.link_faces) > 2 for edge in edges):
        raise ValueError("EdgeFlow requires connected mesh surface edges")
    if tool == "edgeflow.set_flow" and any(
            not edge.is_manifold or any(len(face.verts) != 4 for face in edge.link_faces) for edge in edges):
        raise ValueError("Set Flow requires selected edges between two quads")
    involved = {vertex for edge in edges for vertex in edge.verts}
    if any(sum(edge in edges for edge in vertex.link_edges) > 2 for vertex in involved):
        raise ValueError("EdgeFlow selection must form unbranched edge chains or rings")
    loops = util.get_edgeloops(mesh, edges)
    if set(edge for loop in loops for edge in loop.edges) != edges or any(
            set(loop.verts) - involved for loop in loops):
        raise ValueError("The EdgeFlow path does not match the selected edges")
    if tool != "edgeflow.set_flow" and any(loop.is_cyclic or len(loop.edges) < 2 for loop in loops):
        raise ValueError("Linear and curve fitting need open chains with at least two edges")

    initial = {vertex: vertex.co.copy() for vertex in involved}
    try:
        if tool == "edgeflow.set_flow":
            tension, angle = settings["tension"] / 100.0, math.radians(settings["min_angle"])
            for _ in range(settings["iterations"]):
                for loop in loops:
                    loop.set_flow(tension=tension, min_angle=angle)
            for loop in loops:
                if settings["blend_mode"] == "ABSOLUTE":
                    start, end = settings["blend_start_int"], settings["blend_end_int"]
                else:
                    count = len(loop.verts)
                    start = round(count * settings["blend_start_float"])
                    end = round(count * settings["blend_end_float"])
                loop.blend_start_end(start, end, settings["blend_type"])
        elif tool == "edgeflow.set_linear":
            for loop in loops:
                loop.set_linear(settings["space_evenly"])
        else:
            for loop in loops:
                loop.set_curve_flow(settings["tension"] / 100.0, settings["use_rail"],
                                    settings["rail_mode"], settings["rail_start"], settings["rail_end"])
        for vert, original in initial.items():
            vert.co = original.lerp(vert.co, settings["mix"])
            if not all(map(math.isfinite, vert.co)):
                raise ValueError("EdgeFlow produced a nonfinite coordinate")
    except Exception:
        for vert, original in initial.items():
            vert.co = original
        mesh.normal_update()
        bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)
        raise
    mesh.normal_update()
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)


def _shortest_path(mesh, start, finish, by_edges):
    distances = {start: 0.0}
    paths = {start: 1}
    parents = {}
    queue = [(0.0, start)]
    while queue:
        distance, index = heapq.heappop(queue)
        if distance != distances[index]:
            continue
        if index == finish:
            break
        v = mesh.verts[index]
        for edge in v.link_edges:
            neighbor = edge.other_vert(v)
            if edge.hide or neighbor.hide:
                continue
            length = 1.0 if by_edges else edge.calc_length()
            if not math.isfinite(length) or length <= 1e-12:
                continue
            cost = distance + length
            target = neighbor.index
            previous = distances.get(target, math.inf)
            if math.isclose(cost, previous, rel_tol=1e-9, abs_tol=1e-10):
                paths[target] = min(2, paths[target] + paths[index])
            elif cost < previous:
                distances[target] = cost
                parents[target] = index
                paths[target] = paths[index]
                heapq.heappush(queue, (cost, target))
    if finish not in parents:
        raise ValueError(f"Vertex Curve anchors {start} and {finish} are disconnected")
    if paths[finish] != 1:
        raise ValueError(f"Vertex Curve anchors {start} and {finish} have tied shortest paths; supply an exact path")
    route = [finish]
    while route[-1] != start:
        route.append(parents[route[-1]])
    return route[::-1]


def _vertex_curve(obj, mesh, selected, settings, allow_expansion, protected_indices):
    import bmesh
    from .vendor.edgeflow import op_set_vertex_curve as upstream

    mesh.verts.index_update()
    mesh.verts.ensure_lookup_table()
    anchors = settings["anchors"]
    chosen = {vertex.index for vertex in selected}
    if not set(anchors) <= chosen:
        raise ValueError("All ordered Vertex Curve anchors must belong to the current visible vertex selection")
    path = settings["path"]
    explicit_path = bool(path)
    if not path:
        path = [anchors[0]]
        for start, finish in zip(anchors, anchors[1:]):
            path.extend(_shortest_path(mesh, start, finish, settings["use_topology_distance"])[1:])
    if len(path) < 3 or len(path) > 512 or len(set(path)) != len(path):
        raise ValueError("Vertex Curve needs one simple path of 3 to 512 distinct vertices")
    if any(i >= len(mesh.verts) or mesh.verts[i].hide for i in path):
        raise ValueError("Vertex Curve path includes an invalid or hidden vertex")
    if path[0] != anchors[0] or path[-1] != anchors[-1] or [i for i in path if i in anchors] != anchors:
        raise ValueError("Path endpoints and guide order must match the ordered anchors")
    route = [mesh.verts[i] for i in path]
    for a, b in zip(route, route[1:]):
        edge = mesh.edges.get((a, b))
        length = edge.calc_length() if edge is not None else 0.0
        if edge is None or edge.hide or not math.isfinite(length) or length <= 1e-12:
            raise ValueError(f"Vertex Curve path does not connect {a.index} to {b.index} with a usable visible edge")
    outside = sorted(set(path) - chosen)
    if outside and not allow_expansion:
        raise ValueError(f"Vertex Curve path includes unselected vertices {outside[:12]}; allow_expansion is required")
    overlap = sorted(set(protected_indices) & set(path[1:-1]))
    if overlap:
        raise ValueError(f"Vertex Curve interior path intersects protected vertices {overlap[:12]}")
    if len(anchors) != 2 and (settings["flip"] or settings["rotate"]):
        raise ValueError("Vertex Curve flip and rotate only apply to the two-anchor mode")

    original = [vertex.co.copy() for vertex in route]
    tension = settings["tension"] / 100.0
    mesh.normal_update()
    try:
        if len(anchors) == 2:
            result, message = upstream.circle_2_points(mesh, anchors, route, tension,
                                                       settings["flip"], settings["rotate"])
        elif len(anchors) == 3:
            result, message = upstream.circle_3_points(mesh, anchors, route, tension, settings["space_evenly"])
        else:
            result, message = upstream.curve_hermite(mesh, anchors, route, tension, settings["space_evenly"])
        if result:
            raise ValueError(message)
        # Keep the caller's exact endpoints even when the upstream curve was sampled.
        route[0].co, route[-1].co = original[0], original[-1]
        for vertex, start in zip(route[1:-1], original[1:-1]):
            vertex.co = start.lerp(vertex.co, settings["mix"])
            if not all(map(math.isfinite, vertex.co)):
                raise ValueError("Vertex Curve produced a nonfinite coordinate")
    except Exception as exc:
        for vertex, start in zip(route, original):
            vertex.co = start
        mesh.normal_update()
        bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)
        raise ValueError(f"Vertex Curve could not fit the supplied path: {exc}") from exc
    mesh.normal_update()
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)
    return {"anchors": anchors, "path_indices": path, "path_source": "explicit" if explicit_path else "shortest",
            "outside_selection_indices": outside, "endpoints_preserved": True}


def _offset_mirror_geometry(obj, selected, offset):
    from types import SimpleNamespace

    active = [modifier for modifier in obj.modifiers if modifier.type == "MIRROR"
              and modifier.show_viewport and modifier.show_in_editmode and modifier.use_mirror_merge]
    planes = offset.collect_mirror_planes(SimpleNamespace(matrix_world=obj.matrix_world, modifiers=active))
    if not planes:
        raise ValueError("Offset Edges mirror coupling needs an Edit-visible Mirror modifier with Merge enabled")
    pairs = {}
    remaining = set(selected)
    for plane in planes:
        matches, plane_remaining = offset.get_vert_mirror_pairs(selected, [plane])
        for vertex, matching_plane in matches.items():
            if vertex in pairs and pairs[vertex] != matching_plane:
                raise ValueError("Offset Edges path touches multiple Mirror merge planes at one vertex")
            pairs[vertex] = matching_plane
        remaining &= plane_remaining
    if not remaining:
        raise ValueError("All selected offset edges lie on Mirror merge planes")
    return remaining, pairs


def _offset_edges(obj, mesh, selected, settings):
    import bmesh
    from mathutils import Vector
    from .vendor.edit_mesh_tools import mesh_offset_edges as offset

    selected = set(selected)
    if any(len(edge.link_faces) > 2 for edge in selected):
        raise ValueError("Offset Edges requires an unbranched path without multi-face edges")
    mirror_pairs = None
    if settings["mirror_modifier"]:
        selected, mirror_pairs = _offset_mirror_geometry(obj, selected, offset)
    loops = offset.collect_loops(selected)
    if loops is None:
        raise ValueError("Offset Edges requires nonoverlapping edge chains or rings")
    mesh.normal_update()
    positive = Vector((1.0, 1.0, 1.0)).normalized()
    fallback = Vector((0.0, 0.0, 1.0))
    directions = [offset.get_directions(
        loop, positive, fallback, mirror_pairs, follow_face=settings["follow_face"],
        edge_rail=settings["edge_rail"], edge_rail_only_end=settings["edge_rail_only_end"],
        threshold=settings["threshold"]) for loop in loops]
    width, depth = settings["width"], settings["depth"]
    planned = [width * tangent + depth * normal for _, vectors in directions
               for tangent, normal in vectors]
    if not planned or any(not all(map(math.isfinite, point)) for point in planned):
        raise ValueError("Offset Edges could not compute finite movement")
    mode = settings["geometry_mode"]
    if mode != "move" and max(delta.length for delta in planned) < 1e-9:
        raise ValueError("Offset or extrude requires nonzero displacement")
    extrusion = None if mode == "move" else offset.extrude_edges(mesh, list(selected))
    for vertices, vectors in directions:
        offset.move_verts(width, depth, vertices, vectors, extrusion)
    offset.clean(mesh, mode, selected, extrusion)
    mesh.select_flush_mode()
    mesh.normal_update()
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=mode != "move")


def run(tool, params=None, *, allow_expansion=False, protected_indices=()):
    """Run once against the live selection and return a JSON-safe status."""
    spec = describe(tool)
    settings = _parameters(spec, params)
    obj, mesh, domain, selection = _edit_selection(spec)
    if tool == "native.bevel" and "affect" not in (params or {}):
        # The documented default follows the current single select mode.
        settings["affect"] = "VERTICES" if domain == "VERT" else "EDGES"
    status = "FINISHED"
    if tool == "native.smooth_normal":
        _smooth_normal(obj, mesh, settings)
    elif tool in {"native.edge_crease", "native.edge_bevel_weight"}:
        _set_edge_weight(obj, mesh, tool, selection, settings["value"])
    elif tool == "edgeflow.set_vertex_curve":
        if type(allow_expansion) is not bool:
            raise ValueError("Vertex Curve allow_expansion must be boolean")
        details = _vertex_curve(obj, mesh, selection, settings, allow_expansion, protected_indices)
        return {"tool": tool, "status": status, **details}
    elif tool.startswith("edgeflow."):
        _edgeflow(obj, mesh, tool, selection, settings)
    elif tool == "edit_mesh_tools.offset_edges":
        _offset_edges(obj, mesh, selection, settings)
    else:
        status = _native(spec, domain, selection, settings)
    return {"tool": tool, "status": status}


def register():
    """All operations bind to existing Blender APIs and local algorithm modules."""


def unregister():
    """No RNA classes or menu handlers are owned by this backend."""
