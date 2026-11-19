# SPDX-License-Identifier: GPL-2.0-or-later
"""Isolated LoopTools operators for a single active mesh in Edit Mode.

Blender 5.3 ships native edge-loop operators that cover part of LoopTools 4.7.7.
This module uses them only where their RNA parameter semantics match the
vendored 4.7.7 call; every other configuration stays on the vendored path.
"""

from copy import deepcopy
from contextlib import contextmanager
from hashlib import sha256
import json
import math
from types import SimpleNamespace

import bmesh
import bpy
from mathutils import Vector

from .vendor.looptools import upstream


_CLASSES = {
    "looptools.bridge": upstream.Bridge,
    "looptools.loft": upstream.Bridge,
    "looptools.circle": upstream.Circle,
    "looptools.curve": upstream.Curve,
    "looptools.flatten": upstream.Flatten,
    "looptools.gstretch": upstream.GStretch,
    "looptools.relax": upstream.Relax,
    "looptools.space": upstream.Space,
}
_NATIVE_OPERATORS = {
    "looptools.relax": "mesh.relax_edge_loops",
    "looptools.curve": "mesh.curve_edge_loops_between_selected",
    "looptools.gstretch": "mesh.fit_edge_loops_to_annotation",
}
_NATIVE_VERSION = (5, 3, 0)
_NATIVE_BUILD = b"02f0899d78e3"
_ALIASES = {
    "looptools.bridge": "Bridge",
    "looptools.loft": "Loft",
    "looptools.gstretch": "GStretch",
}
_EXTENDS_SELECTION = {
    "looptools.circle": "A selection of isolated vertices can include their surrounding faces.",
    "looptools.curve": "Unselected vertices along inferred loops can follow the selected control points.",
    "looptools.flatten": "",
    "looptools.gstretch": "",
    "looptools.relax": "Input All may include parallel loops beside the selected loop.",
    "looptools.space": "Input All may include parallel loops beside the selected loop.",
}
_TYPES = {"BoolProperty": "boolean", "FloatProperty": "number",
          "IntProperty": "integer", "EnumProperty": "string"}
_RANGES = {"integer": (-(2**31), 2**31 - 1),
           "number": (-3.4028234663852886e38, 3.4028234663852886e38)}


def _property_spec(prop):
    kind = _TYPES[prop.function.__name__]
    raw = prop.keywords
    result = {"type": kind, "default": raw["default"], "label": raw["name"],
              "description": raw.get("description", raw["name"])}
    if kind in _RANGES:
        minimum, maximum = _RANGES[kind]
        result["min"] = raw.get("min", minimum)
        result["max"] = raw.get("max", maximum)
    for key in ("soft_min", "soft_max", "subtype", "precision"):
        if key in raw:
            result[key] = raw[key]
    if "items" in raw:
        result["enum"] = [item[0] for item in raw["items"]]
        result["enum_labels"] = {item[0]: item[1] for item in raw["items"]}
        result["enum_descriptions"] = {item[0]: item[2] for item in raw["items"]}
    return result


def _geometry_spec(key, operator):
    params = {name: _property_spec(prop) for name, prop in operator.__annotations__.items()}
    if operator is upstream.Bridge:
        params["loft"]["default"] = key.endswith(".loft")
    conditions = _EXTENDS_SELECTION.get(key, "")
    if key in _EXTENDS_SELECTION:
        conditions = " ".join(filter(None, (conditions,
            "Active Edit Mode mirror axes may also move matching vertices.")))
    return {
        "tool": key, "label": _ALIASES.get(key, operator.bl_label),
        "description": operator.bl_description, "parameters": params,
        "context": "SINGLE_EDIT_MESH", "domains": ["VERT", "EDGE", "FACE"],
        "topology_changes": operator in (upstream.Bridge, upstream.GStretch),
        "selection_expansion": key in _EXTENDS_SELECTION,
        "selection_expansion_conditions": conditions,
        "upstream_operator": operator.bl_idname,
        "upstream_version": "4.7.7",
    }


def _text(label, description):
    return {"type": "string", "label": label, "description": description, "required": True}


def _frame_fields():
    return {
        "name": _text("Guide name", "Exact annotation datablock or Grease Pencil object name."),
        "layer": _text("Layer", "Exact guide layer name."),
        "frame": {"type": "integer", "label": "Frame", "description": "Exact frame number, without time fallback.",
                  "required": True, "min": -1048574, "max": 1048574},
    }


def _guide_field():
    return {
        "type": "object", "label": "Named guide", "default": None, "nullable": True,
        "description": "Read one exact 3D annotation or Grease Pencil layer and frame.",
        "properties": {
            **_frame_fields(),
            "source": {**_text("Guide source", "Data type for the named guide."),
                       "enum": ["annotation", "grease_pencil"],
                       "enum_labels": {"annotation": "Annotation", "grease_pencil": "Grease Pencil"}},
        },
    }


def _clear_spec(key, label, upstream_id):
    return {
        "tool": key, "label": label,
        "description": "Clear one explicitly authorized, unshared guide frame.",
        "context": "EXPLICIT_GUIDE_FRAME", "domains": [],
        "topology_changes": False, "selection_expansion": False, "selection_expansion_conditions": "",
        "upstream_operator": upstream_id, "upstream_version": "4.7.7",
        "parameters": {
            **_frame_fields(),
            "expected_stroke_count": {
                "type": "integer", "label": "Expected stroke count", "required": True,
                "min": 0, "max": 2**31 - 1, "description": "Current number of strokes in the frame.",
            },
            "confirm": {"type": "boolean", "label": "Confirm clear", "default": False,
                        "description": "Explicit confirmation to clear this frame's strokes."},
        },
    }


TOOLS = {key: _geometry_spec(key, operator) for key, operator in _CLASSES.items()}
_GSTRETCH = TOOLS["looptools.gstretch"]["parameters"]
_GSTRETCH.update({
    "strokes": {
        "type": "array", "label": "Explicit 3D strokes", "default": [],
        "description": "Open 3D polylines. Pass one array of XYZ points for each stroke.",
        "items": {"type": "array", "min_items": 2,
                  "items": {"type": "array", "min_items": 3, "max_items": 3, "items": {"type": "number"}}},
    },
    "stroke_space": {"type": "string", "label": "Coordinate space", "default": "WORLD",
                     "enum": ["WORLD", "LOCAL"], "description": "Space for explicit stroke coordinates."},
    "guide": _guide_field(),
})
_GSTRETCH["delete_strokes"].update({
    "const": False,
    "description": "Guide clearing is a separate maintenance operation with a named target and explicit confirmation.",
})
TOOLS["looptools.flatten"]["parameters"]["restriction"]["description"] += (
    " LoopTools 4.7.7 uses the same projection in both modes."
)
TOOLS["looptools.flatten"]["parameters"]["plane"]["enum_descriptions"]["view"] += (
    ". Requires a live 3D view region."
)
TOOLS["looptools.gstretch"]["topology_change_conditions"] = (
    "Explicit or named strokes convert to new geometry only when the operator finds no usable selected edge loop."
)
TOOLS["looptools.relax"].update({
    "native_operator": _NATIVE_OPERATORS["looptools.relax"],
    "native_fallback_conditions": [
        "Retain LoopTools 4.7.7 coordinates for existing Relax recipes",
    ],
    "native_numeric_note": (
        "Blender 5.3 Relax produces different coordinates. This workflow uses "
        "LoopTools 4.7.7 to reproduce existing recipes."
    ),
})
TOOLS["looptools.curve"].update({
    "native_operator": _NATIVE_OPERATORS["looptools.curve"],
    "native_mapping": {
        "influence": "factor = influence / 100",
        "restriction": "none -> NONE, extrude -> RAISE, indent -> LOWER",
        "boundaries": "extend_loop = not boundaries; native is used only for boundaries=true",
        "regular": "regular -> regular",
        "lock": "[lock_x, lock_y, lock_z] -> lock",
        "interpolation": "cubic -> CUBIC, linear -> LINEAR",
    },
    "native_fallback_conditions": [
        "boundaries=false: 5.3 native extend_loop also moves perpendicular loops",
        "influence<100: vendor applies the blend once for each selected control loop",
        "regular=false: native and vendor use different distance parameterizations",
        "closed selected loop: 5.3 native curves it while the vendor path is a no-op",
    ],
})
TOOLS["looptools.gstretch"].update({
    "native_operator": _NATIVE_OPERATORS["looptools.gstretch"],
    "native_mapping": {
        "influence": "factor = influence / 100",
        "method": "regular -> SPREAD_EVENLY, irregular -> SPREAD, project -> PROJECT (project falls back)",
        "lock": "[lock_x, lock_y, lock_z] -> lock",
        "delete_strokes": "false; clearing a guide remains a separate confirmed operation",
        "guide": "world guide strokes are copied to a temporary 3D annotation; scene.annotation is restored",
    },
    "native_fallback_conditions": [
        "method=project: projection guidance is not equivalent",
        "no selected edge loop: 5.3 native has no stroke-to-geometry conversion",
        "branched selected edges or a closed loop: native alignment differs from the vendor path",
        "no 3D view context is available for annotation strokes",
    ],
})
TOOLS["looptools.remove_annotation"] = _clear_spec(
    "looptools.remove_annotation", "Clear Annotation Frame", "remove.annotation")
TOOLS["looptools.remove_gpencil"] = _clear_spec(
    "looptools.remove_gpencil", "Clear Grease Pencil Frame", "remove.gp")


def describe(tool):
    """Return an independent JSON-serializable parameter specification."""
    try:
        return deepcopy(TOOLS[tool])
    except (KeyError, TypeError):
        raise ValueError(f"Unknown LoopTools tool: {tool!r}") from None


def _checked(label, value, spec):
    if value is None and spec.get("nullable", False):
        return None
    kind = spec["type"]
    if kind == "object":
        return _parameters(value, spec["properties"], label)
    if kind == "array":
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{label} must be an array")
        if not spec.get("min_items", 0) <= len(value) <= spec.get("max_items", len(value)):
            raise ValueError(f"{label} has the wrong number of items")
        return [_checked(f"{label}[{index}]", item, spec["items"]) for index, item in enumerate(value)]
    if kind == "boolean" and type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    if kind == "integer" and type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    if kind == "string" and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{label} must be a nonempty string")
    if kind == "number":
        if type(value) not in (int, float):
            raise ValueError(f"{label} must be a number")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"{label} must be finite")
        value = float(value)
    if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
        raise ValueError(f"{label} must be in [{spec.get('min', '-infinity')}, {spec.get('max', 'infinity')}]")
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError(f"{label} must be one of: {', '.join(spec['enum'])}")
    if "const" in spec and value != spec["const"]:
        raise ValueError(f"{label} only accepts {spec['const']!r}. {spec['description']}")
    return value


def _parameters(params, specifications, label):
    if not isinstance(params, dict) or any(not isinstance(key, str) for key in params):
        raise ValueError(f"{label} must be an object with string keys")
    unknown = sorted(params.keys() - specifications.keys())
    if unknown:
        raise ValueError(f"{label} has unknown parameters: {', '.join(unknown)}")
    result = {}
    for key, spec in specifications.items():
        if key in params:
            result[key] = _checked(f"{label}.{key}", params[key], spec)
        elif "default" in spec:
            result[key] = deepcopy(spec["default"])
        elif spec.get("required", False):
            raise ValueError(f"{label}.{key} is required")
    return result


def _exact_frame(record, layer_name, frame_number, is_annotation):
    layers = [item for item in record.layers if (item.info if is_annotation else item.name) == layer_name]
    if len(layers) != 1:
        raise ValueError(f"Guide layer {layer_name!r} is missing or ambiguous")
    layer = layers[0]
    frames = [item for item in layer.frames if item.frame_number == frame_number]
    if len(frames) != 1:
        raise ValueError(f"Guide frame {frame_number} is missing or ambiguous in {layer_name!r}")
    return layer, frames[0]


def _resolve_guide(context, params, source):
    if source == "annotation":
        annotation = bpy.data.annotations.get(params["name"])
        if annotation is None:
            raise ValueError(f"Annotation {params['name']!r} not found")
        layer, frame = _exact_frame(annotation, params["layer"], params["frame"], True)
        return annotation, layer, frame, None
    obj = context.scene.objects.get(params["name"])
    if obj is None or obj.type != "GREASEPENCIL":
        raise ValueError(f"Grease Pencil object {params['name']!r} not found in the scene")
    layer, frame = _exact_frame(obj.data, params["layer"], params["frame"], False)
    return obj.data, layer, frame, obj


def _drawing(frame):
    entry = frame.drawing
    if entry is None or entry.type != "DRAWING":
        raise ValueError("The specified frame must own an editable drawing")
    return entry


def _world_strokes(context, obj, values):
    explicit = values["strokes"]
    named = values["guide"]
    if named and explicit:
        raise ValueError("Choose explicit strokes or one named guide")
    if named:
        is_annotation = named["source"] == "annotation"
        _, layer, frame, guide = _resolve_guide(context, named, named["source"])
        if is_annotation:
            if any(stroke.display_mode != "3DSPACE" for stroke in frame.strokes):
                raise ValueError("Only 3D-space Annotation strokes supply calibrated 3D coordinates")
            raw = [[point.co.copy() for point in stroke.points] for stroke in frame.strokes]
        else:
            if layer.parent:
                raise ValueError("Use explicit world strokes for a parented Grease Pencil guide layer")
            matrix = guide.matrix_world @ layer.matrix_local
            raw = [[matrix @ point.position for point in stroke.points] for stroke in _drawing(frame).strokes]
        if not raw:
            raise ValueError("The named frame contains no guide strokes")
    elif values["stroke_space"] == "LOCAL":
        raw = [[obj.matrix_world @ Vector(point) for point in stroke] for stroke in explicit]
    else:
        raw = [[Vector(point) for point in stroke] for stroke in explicit]
    result = []
    for index, stroke in enumerate(raw):
        points = []
        for value in stroke:
            if any(not math.isfinite(component) for component in value):
                raise ValueError(f"Guide stroke {index} must use finite coordinates")
            if not points or (value - points[-1]).length > 1e-7:
                points.append(value)
        if len(points) < 2:
            raise ValueError(f"Guide stroke {index} must contain two distinct points")
        result.append(upstream.gstretch_fake_stroke(points))
    return result


def _clear_strokes(tool, context, params):
    if not params["confirm"]:
        raise ValueError("confirm must be true to clear an explicitly named frame")
    annotation = tool == "looptools.remove_annotation"
    source = "annotation" if annotation else "grease_pencil"
    owner, layer, frame, obj = _resolve_guide(context, params, source)
    shared_object = obj is not None and (obj.users > 1 or
        sum(scene.objects.get(obj.name) == obj for scene in bpy.data.scenes) > 1)
    if not owner.is_editable or owner.users > 1 or layer.lock or shared_object or (obj and not obj.is_editable):
        raise ValueError("Clearing requires local unshared guide data on an unlocked layer")
    if annotation:
        changed = len(frame.strokes)
    else:
        drawing = _drawing(frame)
        if drawing.user_count > 1:
            raise ValueError("The drawing is shared by other frames")
        changed = len(drawing.strokes)
    if changed != params["expected_stroke_count"]:
        raise ValueError(f"Guide frame changed: expected {params['expected_stroke_count']} strokes, found {changed}")
    if annotation:
        for stroke in reversed(list(frame.strokes)):
            frame.strokes.remove(stroke)
    elif changed:
        drawing.remove_strokes()
    if changed:
        owner.update_tag()
    return {"tool": tool, "status": "FINISHED", "strokes_removed": changed,
            "geometry_changed": False, "topology_changed": False}


def _gstretch(values, strokes):
    obj, bm = upstream.initialise()
    mapping = False
    bm_mod = bm
    if strokes:
        derived, bm_mod, loops = upstream.get_connected_input(obj, bm, False, input="selected")
        mapping = upstream.get_mapping(derived, bm, bm_mod, False, False, loops)
        loops = upstream.check_loops(loops, mapping, bm_mod)
    else:
        selected = [upstream.edgekey(edge) for edge in bm.edges if edge.select and not edge.hide]
        loops = upstream.check_loops(upstream.get_connected_selections(selected), False, bm)
        strokes = upstream.gstretch_get_fake_strokes(obj, bm, loops)
    try:
        move = []
        if strokes and not loops:
            move.append(upstream.gstretch_create_verts(
                obj, bm, strokes, values["method"], values["conversion"], values["conversion_distance"],
                values["conversion_max"], values["conversion_min"], values["conversion_vertices"],
            ))
        elif strokes and loops:
            pairs = upstream.gstretch_match_loops_strokes(loops, strokes, obj, bm_mod)
            for loop, stroke in upstream.gstretch_align_pairs(pairs, obj, bm_mod, values["method"]):
                move.append(upstream.gstretch_calculate_verts(loop, stroke, obj, bm_mod, values["method"]))
        locks = tuple(values[f"lock_{axis}"] for axis in "xyz")
        bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
        upstream.move_verts(obj, bm, mapping, move, locks if any(locks) else False, values["influence"])
        upstream.terminate()
        return {"FINISHED"}
    finally:
        if bm_mod is not bm:
            bm_mod.free()


def _active_mesh(context):
    obj = context.active_object
    if obj is None or obj.type != "MESH" or context.mode != "EDIT_MESH":
        raise RuntimeError("LoopTools requires an active mesh in Edit Mode")
    if len(context.objects_in_mode) != 1 or not obj.is_editable or not obj.data.is_editable:
        raise RuntimeError("LoopTools requires exactly one editable mesh in Edit Mode")
    return obj


def _native_available(tool):
    """Allow automatic routing on the build verified against LoopTools 4.7.7."""
    identifier = _NATIVE_OPERATORS.get(tool)
    if identifier is None or bpy.app.version != _NATIVE_VERSION or bpy.app.build_hash != _NATIVE_BUILD:
        return False
    namespace, short_name = identifier.split(".", 1)
    try:
        getattr(getattr(bpy.ops, namespace), short_name).get_rna_type()
    except (AttributeError, KeyError, RuntimeError):
        return False
    return True


def _selected_edge_components(bm):
    """Return connected components of selected, visible mesh edges."""
    adjacency = {}
    for edge in bm.edges:
        if not edge.select or edge.hide:
            continue
        for vertex in edge.verts:
            adjacency.setdefault(vertex, set()).add(edge)
    seen = set()
    components = []
    for start in adjacency:
        if start in seen:
            continue
        pending = [start]
        seen.add(start)
        vertices, edges = set(), set()
        while pending:
            vertex = pending.pop()
            vertices.add(vertex)
            for edge in adjacency.get(vertex, ()):
                edges.add(edge)
                other = edge.other_vert(vertex)
                if other not in seen:
                    seen.add(other)
                    pending.append(other)
        components.append((vertices, edges))
    return components


def _closed_loop_through_selected(bm):
    """Detect a closed quad/wire loop through any selected vertex.

    The vendored Curve path treats such a ring as unusable and leaves it
    untouched, while 5.3 native Curve edits it. Keep the vendor behavior.
    """
    from . import selection

    cache = {}
    for vertex in bm.verts:
        if not vertex.select or vertex.hide:
            continue
        for seed in vertex.link_edges:
            if seed.hide:
                continue
            if seed not in cache:
                try:
                    cache[seed] = selection._edge_loop(seed)
                except (RuntimeError, ValueError):
                    cache[seed] = set()
            edges = cache[seed]
            if len(edges) < 3:
                continue
            vertices = {item for edge in edges for item in edge.verts}
            if all(sum(item in edges for item in vertex.link_edges) == 2 for vertex in vertices):
                return True
    return False


def _closed_selected_component(components):
    for vertices, edges in components:
        if len(vertices) < 3 or len(edges) < 3 or len(edges) != len(vertices):
            continue
        if all(sum(edge in edges for edge in vertex.link_edges if edge.select and not edge.hide) == 2
               for vertex in vertices):
            return True
    return False


def _view3d_context(context):
    """Find a VIEW_3D region attached to the same scene and edit view layer."""
    current = context.window
    windows = (current,) if current else tuple(context.window_manager.windows)
    for window in windows:
        if window.scene != context.scene or window.view_layer != context.view_layer:
            continue
        screen = window.screen
        preferred = context.area
        areas = (preferred,) if preferred and preferred.type == "VIEW_3D" and preferred in screen.areas[:] else screen.areas
        for area in areas:
            if area.type != "VIEW_3D":
                continue
            region = next((item for item in area.regions if item.type == "WINDOW"), None)
            if region:
                return {"window": window, "screen": screen, "area": area, "region": region}
    return None


@contextmanager
def _native_guide(context, strokes):
    """Temporarily publish world-space guide strokes as a 3D annotation."""
    view = _view3d_context(context)
    if view is None:
        raise RuntimeError("Native GStretch requires a 3D view in the edit window")
    annotation = bpy.data.annotations.new("HSW GStretch Guide")
    try:
        layer = annotation.layers.new("Note")
        frame = layer.frames.new(1, active=True)
        for stroke in strokes:
            points = list(stroke.points)
            if not points:
                continue
            native_stroke = frame.strokes.new()
            native_stroke.display_mode = "3DSPACE"
            native_stroke.points.add(len(points))
            for index, point in enumerate(points):
                native_stroke.points[index].co = tuple(point.co)
        annotation.update_tag()
        bpy.context.view_layer.update()
        previous = context.scene.annotation
        context.scene.annotation = annotation
        try:
            with context.temp_override(**view, annotation_data=annotation, active_annotation_layer=layer):
                yield
        finally:
            context.scene.annotation = previous
    finally:
        bpy.data.annotations.remove(annotation, do_unlink=True)


def _native_route(tool, settings, context, bm, strokes):
    """Return (use_native, reason) for a 5.3 edge-loop operator."""
    if tool == "looptools.relax":
        return False, "LoopTools 4.7.7 coordinates preserved for existing Relax recipes"
    if not _native_available(tool):
        return False, "native operator is unavailable in this Blender build"
    if tool == "looptools.curve":
        if not settings["boundaries"]:
            return False, "boundaries=false: 5.3 extend_loop also moves perpendicular loops"
        if settings["influence"] != 100.0:
            return False, "influence<100: vendor applies the blend once per selected control loop"
        if not settings["regular"]:
            return False, "regular=false: native and vendor use different distance parameterizations"
        if _closed_loop_through_selected(bm):
            return False, "closed selected loop: 5.3 native curves it while vendor is a no-op"
        return True, ""
    if tool == "looptools.gstretch":
        if settings["method"] == "project":
            return False, "method=project: projection guidance is not equivalent"
        components = _selected_edge_components(bm)
        if not any(len(vertices) >= 3 and len(edges) >= 2 for vertices, edges in components):
            return False, "no selected edge loop; 5.3 native has no stroke-to-geometry conversion"
        for vertices, edges in components:
            if any(sum(edge in edges for edge in vertex.link_edges if edge.select and not edge.hide) > 2
                   for vertex in vertices):
                return False, "branched selected edges are not mapped by native GStretch"
        if _closed_selected_component(components):
            return False, "closed selected loop: native and vendor choose a different cyclic start"
        if _view3d_context(context) is None:
            return False, "no 3D view is available in the same edit window"
        return True, ""
    return False, "no native mapping"


def _native_call(tool, settings, context, strokes):
    if tool == "looptools.curve":
        result = bpy.ops.mesh.curve_edge_loops_between_selected(
            "EXEC_DEFAULT",
            factor=settings["influence"] / 100.0,
            clamp_elevation={"none": "NONE", "extrude": "RAISE", "indent": "LOWER"}[
                settings["restriction"]],
            extend_loop=not settings["boundaries"],
            regular=settings["regular"],
            lock=(settings["lock_x"], settings["lock_y"], settings["lock_z"]),
            interpolation=settings["interpolation"].upper(),
        )
    else:
        with _native_guide(context, strokes):
            result = bpy.ops.mesh.fit_edge_loops_to_annotation(
                "EXEC_DEFAULT",
                factor=settings["influence"] / 100.0,
                delete_strokes=False,
                method={"regular": "SPREAD_EVENLY", "irregular": "SPREAD",
                        "project": "PROJECT"}[settings["method"]],
                lock=(settings["lock_x"], settings["lock_y"], settings["lock_z"]),
            )
    return {"FINISHED"} if "FINISHED" in result else {"CANCELLED"}


def _check_execution(context, tool, params, strokes, bm):
    view_lock = (context.scene.transform_orientation_slots[0].type == "VIEW"
                 and any(params.get(f"lock_{axis}") for axis in "xyz"))
    if (params.get("plane") == "view" or view_lock) and context.region_data is None:
        raise RuntimeError("View-based flattening and View-aligned axis locks require a live 3D region")
    if tool in ("looptools.bridge", "looptools.loft"):
        if params["loft_loop"] and not params["loft"]:
            raise ValueError("loft_loop requires loft mode")
        estimated = sum(edge.select and not edge.hide for edge in bm.edges) * params["segments"]
        if estimated > 1_000_000:
            raise ValueError("Bridge exceeds the per-operation budget of one million generated elements")
    if tool == "looptools.gstretch":
        if params["conversion_min"] > params["conversion_max"]:
            raise ValueError("conversion_min cannot exceed conversion_max")
        if strokes and not any(edge.select and not edge.hide for edge in bm.edges):
            if params["conversion"] == "distance":
                lengths = [sum((a.co - b.co).length for a, b in zip(stroke.points, stroke.points[1:]))
                           for stroke in strokes]
                estimate = sum(length / params["conversion_distance"] + 1 for length in lengths)
            else:
                counts = {"vertices": params["conversion_vertices"], "limit_vertices": params["conversion_max"],
                          "none": max((len(stroke.points) for stroke in strokes), default=0)}
                estimate = counts[params["conversion"]] * len(strokes)
            if estimate > 1_000_000:
                raise ValueError("GStretch exceeds the per-operation budget of one million guide samples")


def _snapshot(bm):
    ids = {vertex: index for index, vertex in enumerate(bm.verts)}
    edges = sorted(tuple(sorted(ids[v] for v in edge.verts)) for edge in bm.edges)
    faces = []
    for face in bm.faces:
        vertices = tuple(ids[v] for v in face.verts)
        start = vertices.index(min(vertices))
        faces.append(vertices[start:] + vertices[:start])
    counts = len(ids), len(edges), len(faces)
    topology = sha256(json.dumps((counts, edges, sorted(faces)), separators=(",", ":")).encode()).hexdigest()
    return counts, topology, tuple(tuple(v.co) for v in bm.verts)


def run(tool, params):
    """Apply one validated LoopTools operation to the current edit mesh or named guide frame."""
    spec = describe(tool)
    settings = _parameters(params, spec["parameters"], tool)
    context = bpy.context
    if tool not in _CLASSES:
        return _clear_strokes(tool, context, settings)
    obj = _active_mesh(context)
    edit = bmesh.from_edit_mesh(obj.data)
    strokes = _world_strokes(context, obj, settings) if tool == "looptools.gstretch" else []
    _check_execution(context, tool, settings, strokes, edit)
    before = _snapshot(edit)
    visibility = [(mod, mod.show_viewport, mod.use_mirror_merge if mod.type == "MIRROR" else None)
                  for mod in obj.modifiers]
    native, fallback_reason = _native_route(tool, settings, context, edit, strokes)
    upstream.looptools_cache.clear()
    try:
        if native:
            status = _native_call(tool, settings, context, strokes)
        elif tool == "looptools.gstretch":
            status = _gstretch(settings, strokes)
        else:
            status = _CLASSES[tool].execute(SimpleNamespace(**settings), context)
    finally:
        upstream.looptools_cache.clear()
        for modifier, shown, merge in visibility:
            if modifier.show_viewport != shown:
                modifier.show_viewport = shown
            if merge is not None and modifier.use_mirror_merge != merge:
                modifier.use_mirror_merge = merge
    after = _snapshot(bmesh.from_edit_mesh(obj.data))
    return {"tool": tool, "status": "FINISHED" if "FINISHED" in status else "CANCELLED",
            "implementation": "native" if native else "vendor",
            "native_operator": _NATIVE_OPERATORS[tool] if native else None,
            "native_fallback_reason": "" if native else fallback_reason,
            "geometry_changed": before != after,
            "topology_changed": before[1] != after[1],
            "topology_before_sha256": before[1], "topology_after_sha256": after[1],
            "before": dict(zip(("vertices", "edges", "faces"), before[0])),
            "after": dict(zip(("vertices", "edges", "faces"), after[0]))}


def register():
    """No shared Blender registration is needed for the backend."""


def unregister():
    """Discard private per-call scratch state."""
    upstream.looptools_cache.clear()
