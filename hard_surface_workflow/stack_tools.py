"""Object-mode hard-surface tools backed by Blender's native modifier RNA."""

from contextlib import contextmanager
from math import ceil, isfinite, tau

import bpy
from mathutils import Matrix, Vector


NATIVE = {
    "BOOLEAN": ("BooleanModifier", "object operation solver use_self use_hole_tolerant"),
    "BEVEL": ("BevelModifier", "width width_pct segments affect limit_method angle_limit vertex_group "
              "use_clamp_overlap offset_type profile loop_slide mark_seam mark_sharp harden_normals"),
    "SOLIDIFY": ("SolidifyModifier", "thickness offset use_rim use_rim_only use_even_offset solidify_mode "
                 "use_quality_normals vertex_group thickness_vertex_group"),
    "MIRROR": ("MirrorModifier", "use_axis use_bisect_axis use_bisect_flip_axis use_clip "
               "use_mirror_merge merge_threshold mirror_object"),
    "SUBSURF": ("SubsurfModifier", "subdivision_type levels render_levels quality boundary_smooth "
                "use_creases use_limit_surface show_only_control_edges"),
    "ARRAY": ("ArrayModifier", "count fit_type fit_length curve use_constant_offset constant_offset_displace "
              "use_relative_offset relative_offset_displace use_merge_vertices use_merge_vertices_cap "
              "merge_threshold use_object_offset offset_object start_cap end_cap"),
    "SCREW": ("ScrewModifier", "axis angle steps render_steps screw_offset iterations object "
              "use_object_screw_offset use_merge_vertices merge_threshold use_normal_flip use_normal_calculate"),
    "WELD": ("WeldModifier", "mode merge_threshold vertex_group invert_vertex_group loose_edges"),
    "WEIGHTED_NORMAL": ("WeightedNormalModifier", "weight mode thresh keep_sharp vertex_group use_face_influence"),
    "SHRINKWRAP": ("ShrinkwrapModifier", "target auxiliary_target wrap_method wrap_mode cull_face vertex_group "
                   "offset project_limit use_project_x use_project_y use_project_z use_negative_direction use_positive_direction"),
    "LATTICE": ("LatticeModifier", "object strength vertex_group invert_vertex_group"),
    "SIMPLE_DEFORM": ("SimpleDeformModifier", "deform_method deform_axis origin factor angle limits "
                       "lock_x lock_y lock_z vertex_group"),
    "CURVE": ("CurveModifier", "object deform_axis vertex_group invert_vertex_group"),
    "DECIMATE": ("DecimateModifier", "decimate_type ratio iterations angle_limit vertex_group invert_vertex_group "
                 "vertex_group_factor use_collapse_triangulate use_symmetry symmetry_axis use_dissolve_boundaries delimit"),
    "TRIANGULATE": ("TriangulateModifier", "quad_method ngon_method min_vertices keep_custom_normals"),
    "DATA_TRANSFER": ("DataTransferModifier", "object use_object_transform use_vert_data use_edge_data use_loop_data use_poly_data "
                      "data_types_verts data_types_edges data_types_loops data_types_polys vert_mapping edge_mapping loop_mapping "
                      "poly_mapping use_max_distance max_distance ray_radius islands_precision mix_mode mix_factor "
                      "vertex_group invert_vertex_group"),
}
RNA_FIELDS = {kind: definition.split() for kind, (_, definition) in NATIVE.items()}
OBJECT_TYPES = {
    ("BOOLEAN", "object"): {"MESH"}, ("ARRAY", "curve"): {"CURVE"},
    ("ARRAY", "start_cap"): {"MESH"}, ("ARRAY", "end_cap"): {"MESH"},
    ("SHRINKWRAP", "target"): {"MESH"}, ("SHRINKWRAP", "auxiliary_target"): {"MESH"},
    ("LATTICE", "object"): {"LATTICE"}, ("CURVE", "object"): {"CURVE"}, ("DATA_TRANSFER", "object"): {"MESH"},
}
MAX_ESTIMATED_WORK = 2_000_000
_COST_PROPERTIES = {
    "BOOLEAN": "object collection operand_type",
    "BEVEL": "segments",
    "MIRROR": "use_axis",
    "SUBSURF": "levels render_levels",
    "ARRAY": "count fit_type fit_length curve start_cap end_cap use_object_offset use_constant_offset "
             "use_relative_offset constant_offset_displace relative_offset_displace",
    "SCREW": "iterations steps render_steps",
    "DATA_TRANSFER": "object",
}
MAX_VALUES = {
    ("BEVEL", "segments"): 32, ("SUBSURF", "levels"): 5, ("SUBSURF", "render_levels"): 5,
    ("ARRAY", "count"): 1000, ("SCREW", "iterations"): 128,
    ("SCREW", "steps"): 512, ("SCREW", "render_steps"): 512,
}
OPERATIONS = {"DIFFERENCE", "UNION", "INTERSECT"}
_ACTIONS = {
    "stack_snapshot": ("Read modifier stack", [], []),
    "stack_add": ("Add native modifier", ["name", "type", "settings"], []),
    "stack_update": ("Update modifier parameters", ["name", "settings"], ["expected_type", "expected_index"]),
    "stack_toggle": ("Set modifier viewport and render visibility", ["name"],
                     ["viewport", "render", "expected_type", "expected_index"]),
    "stack_reorder": ("Move one modifier or set the complete stack order", [],
                      ["name", "index", "order", "expected_order"]),
    "boolean_consumers": ("Find every Boolean consumer of this cutter", [], []),
    "boolean_rebind": ("Rebind a single existing Boolean layer in place", ["name", "cutter", "expected_cutter"],
                       ["expected_index"]),
    "boolean_batch": ("Add one cutter to explicit targets", ["name", "cutter", "operation"], ["targets"]),
    "boolean_slice": ("Make independent Difference and Intersect copies", ["cutter", "difference_name", "intersection_name"],
                      ["collection", "name"]),
    "array_linear": ("Add a live constant-offset array", ["name", "count", "offset"], ["merge_threshold"]),
    "array_radial": ("Add a live Array and local-axis rotation pivot", ["name", "count", "axis", "pivot"],
                     ["angle", "collection", "controller_name", "merge_threshold"]),
    "revolve": ("Revolve a profile with a live Screw modifier", ["name", "axis", "steps"],
                ["angle", "screw_offset", "merge_threshold"]),
}


def catalog():
    """Describe the native types and callable tools without reading the active scene."""
    result = {key: {"label": label, "required": list(required), "optional": list(optional),
                    "read_only": key in {"stack_snapshot", "boolean_consumers"}}
              for key, (label, required, optional) in _ACTIONS.items()}
    for key in ("stack_add", "stack_update"):
        result[key]["native_settings"] = {kind: list(properties) for kind, properties in RNA_FIELDS.items()}
        result[key]["max_estimated_mesh_work"] = MAX_ESTIMATED_WORK
    return result


def _require_name(value, label):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 63:
        raise ValueError(f"{label} must be a nonempty Blender name of at most 63 UTF-8 bytes")
    return value


def _local_mesh(obj, write):
    if not isinstance(obj, bpy.types.Object) or obj.type != "MESH":
        raise ValueError("Target must be a mesh object")
    if bpy.context.view_layer.objects.get(obj.name) != obj:
        raise ValueError(f"{obj.name!r} must be in the current View Layer")
    if write and (obj.mode != "OBJECT" or obj.library or obj.override_library or not obj.is_editable):
        raise ValueError(f"Use a writable local target in Object Mode: {obj.name!r}")
    return obj


def _object(name, allowed=None):
    value = bpy.context.view_layer.objects.get(_require_name(name, "Object reference"))
    if value is None or allowed and value.type not in allowed:
        expected = f" of type {'/'.join(sorted(allowed))}" if allowed else ""
        raise ValueError(f"Expected an object in the current View Layer{expected}: {name!r}")
    return value


def _dependencies(obj):
    for owner in (*obj.modifiers, *obj.constraints):
        for prop in owner.bl_rna.properties:
            if prop.type != "POINTER":
                continue
            if prop.fixed_type.identifier not in {"Object", "Collection"}:
                continue
            dep = getattr(owner, prop.identifier)
            if isinstance(dep, bpy.types.Object):
                yield dep
            if isinstance(dep, bpy.types.Collection):
                yield from dep.all_objects
    if obj.parent is not None and obj.parent_type in {"VERTEX", "VERTEX_3"}:
        yield obj.parent
    for driver in getattr(obj.animation_data, "drivers", ()):
        for variable in driver.driver.variables:
            for target in variable.targets:
                if isinstance(target.id, bpy.types.Object):
                    yield target.id


def _check_reference(target, dependency):
    pending = [dependency]
    seen = set()
    while pending:
        value = pending.pop()
        if value == target:
            raise ValueError(f"Object reference would create a dependency cycle: {target.name!r} -> {dependency.name!r}")
        if value not in seen:
            seen.add(value)
            pending.extend(x for x in _dependencies(value) if x not in seen)


def _number(value, minimum, maximum, label):
    if type(value) not in (float, int) or not minimum <= value <= maximum or not isfinite(value):
        raise ValueError(f"{label} must be finite and between {minimum:g} and {maximum:g}")
    return value


def _typed(prop, value, maximum):
    if prop.type == "BOOLEAN":
        if type(value) is not bool:
            raise ValueError(f"{prop.identifier} must be a boolean")
        return value
    if prop.type in {"INT", "FLOAT"}:
        if prop.type == "INT" and type(value) is not int:
            raise ValueError(f"{prop.identifier} must be an integer")
        return _number(value, prop.hard_min, min(prop.hard_max, maximum), prop.identifier)
    if prop.type == "ENUM":
        choices = {item.identifier for item in prop.enum_items}
        if prop.is_enum_flag:
            if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) or item not in choices for item in value):
                raise ValueError(f"{prop.identifier} requires an array of known options")
            return set(value)
        if not isinstance(value, str) or value not in choices:
            raise ValueError(f"Unknown {prop.identifier} option: {value!r}")
        return value
    if prop.type == "STRING" and isinstance(value, str):
        return value
    raise ValueError(f"Unsupported {prop.identifier} value: {value!r}")


def _settings(obj, kind, source):
    if not isinstance(kind, str) or kind not in NATIVE:
        raise ValueError(f"Unsupported native modifier type: {kind!r}")
    if not isinstance(source, dict):
        raise ValueError("settings must be a JSON object")
    unsupported = set(source).difference(RNA_FIELDS[kind])
    if unsupported:
        raise ValueError(f"Unsupported {kind} settings: {', '.join(sorted(unsupported))}")
    if kind == "SIMPLE_DEFORM" and {"factor", "angle"} <= source.keys():
        raise ValueError("factor and angle write the same value")
    rna = getattr(bpy.types, NATIVE[kind][0]).bl_rna.properties
    values = {}
    for name, value in source.items():
        prop = rna.get(name)
        if prop is None or prop.is_readonly:
            raise ValueError(f"{kind}.{name} is unavailable in Blender {bpy.app.version_string}")
        if name == "vertex_group" and value and (not isinstance(value, str) or obj.vertex_groups.get(value) is None):
            raise ValueError(f"Vertex group does not exist on {obj.name!r}: {value!r}")
        if prop.type == "POINTER":
            if value is not None:
                value = _object(value, OBJECT_TYPES.get((kind, name)))
                _check_reference(obj, value)
        elif getattr(prop, "is_array", False):
            if not isinstance(value, (tuple, list)) or len(value) != prop.array_length:
                raise ValueError(f"{name} must contain exactly {prop.array_length} values")
            value = tuple(_typed(prop, item, MAX_VALUES.get((kind, name), float("inf"))) for item in value)
        else:
            value = _typed(prop, value, MAX_VALUES.get((kind, name), float("inf")))
        values[name] = value
    return values


def _source_cutter(obj, name):
    cutter = _object(name, {"MESH"})
    _check_reference(obj, cutter)
    return cutter


def _value(value):
    if isinstance(value, bpy.types.ID):
        return value.name
    if isinstance(value, (bool, float, int, str)) or value is None:
        return value
    if isinstance(value, set):
        return sorted(value)
    return list(value)


def _mesh_units(obj):
    return max(len(obj.data.vertices), len(obj.data.edges), len(obj.data.loops), 1)


def _specification(kind, values):
    if kind not in _COST_PROPERTIES:
        return {}
    rna = getattr(bpy.types, NATIVE[kind][0]).bl_rna.properties
    defaults = {}
    for name in _COST_PROPERTIES[kind].split():
        prop = rna[name]
        if prop.type == "POINTER":
            defaults[name] = None
        elif getattr(prop, "is_array", False):
            defaults[name] = tuple(prop.default_array)
        else:
            defaults[name] = prop.default
    defaults.update(values)
    return defaults


def _plan(obj):
    return {
        mod.name: (mod.type, {key: getattr(mod, key) for key in _COST_PROPERTIES.get(mod.type, "").split()},
                   mod.show_viewport, mod.show_render)
        for mod in obj.modifiers
    }


def _curve_length(curve):
    if curve is None:
        raise ValueError("Fit Curve needs a reference curve to estimate mesh work")
    data = curve.data
    if sum(len(spline.points) + len(spline.bezier_points) for spline in data.splines) > 10000:
        raise ValueError("Curve profile exceeds the mesh work estimation budget")
    return sum(spline.calc_length(resolution=64) for spline in data.splines)


def _array_copies(obj, spec, upstream):
    if spec["fit_type"] == "FIXED_COUNT":
        return max(1, spec["count"])
    offset = Vector(spec["constant_offset_displace"]) if spec["use_constant_offset"] else Vector((0, 0, 0))
    if spec["use_object_offset"] or (upstream and spec["use_relative_offset"]):
        raise ValueError("Fit Array above a geometry-changing layer needs a constant offset, or use Fixed Count")
    if spec["use_relative_offset"] and obj.data.vertices:
        positions = [vertex.co for vertex in obj.data.vertices]
        size = [max(co[axis] for co in positions)-min(co[axis] for co in positions) for axis in range(3)]
        offset += Vector([dimension*step for dimension, step in zip(size, spec["relative_offset_displace"])])
    if offset.length < 1e-9:
        raise ValueError("Fit Array needs a measurable translation or Fixed Count")
    if spec["fit_type"] == "FIT_CURVE":
        length = _curve_length(spec["curve"]) * max(1.0, *(abs(value) for value in spec["curve"].matrix_world.to_scale()))
    else:
        length = abs(spec["fit_length"])
    copies = length/offset.length
    return MAX_ESTIMATED_WORK + 1 if not isfinite(copies) else 1 + ceil(copies)


def _estimate(obj, plan, lane, visiting):
    size = _mesh_units(obj)
    if obj in visiting:
        return size
    visiting = visiting | {obj}
    upstream = False
    for name, (kind, spec, viewport, render) in plan.items():
        if not (viewport if lane == "viewport" else render):
            continue
        references = []
        growth = 1
        if kind == "MIRROR":
            growth = 2**sum(spec["use_axis"])
        elif kind == "SUBSURF":
            growth = 4**spec["levels" if lane == "viewport" else "render_levels"]
        elif kind == "ARRAY":
            growth = _array_copies(obj, spec, upstream)
            references = [spec["start_cap"], spec["end_cap"]]
        elif kind == "SCREW":
            growth = 1 + spec["iterations"] * spec["steps" if lane == "viewport" else "render_steps"]
        elif kind == "SOLIDIFY":
            growth = 6
        elif kind == "BEVEL":
            growth = 1 + spec["segments"]
        elif kind == "TRIANGULATE":
            growth = 3
        elif kind == "DATA_TRANSFER":
            references = [spec["object"]]
        elif kind == "BOOLEAN":
            if spec["operand_type"] == "COLLECTION" and spec["collection"] is not None:
                references = list(spec["collection"].all_objects)
            else:
                references = [spec["object"]]
        input_work = sum(_estimate(other, _plan(other), lane, visiting)
                         for other in references if other is not None and other.type == "MESH")
        size = growth*size + input_work
        if size > MAX_ESTIMATED_WORK and (growth > 1 or input_work):
            raise ValueError(f"{lane} mesh work estimate at {name!r} exceeds {MAX_ESTIMATED_WORK:,}: {size:,}")
        if kind not in {"WEIGHTED_NORMAL", "DATA_TRANSFER", "TRIANGULATE"}:
            upstream = True
    return size


def _budget(obj, plan):
    for lane in ("viewport", "render"):
        _estimate(obj, plan, lane, set())


def _budget_append(obj, name, kind, values):
    plan = _plan(obj)
    plan[name] = (kind, _specification(kind, values), True, True)
    _budget(obj, plan)


def _budget_update(obj, name, values=None, viewport=None, render=None):
    plan = _plan(obj)
    kind, spec, visible, enabled = plan[name]
    if values:
        spec.update(values)
    plan[name] = (kind, spec, visible if viewport is None else viewport, enabled if render is None else render)
    _budget(obj, plan)


def _snapshot(modifier):
    return {"name": modifier.name, "type": modifier.type, "viewport": modifier.show_viewport, "render": modifier.show_render,
            "settings": {name: _value(getattr(modifier, name)) for name in RNA_FIELDS.get(modifier.type, ())
                         if hasattr(modifier, name)}}


@contextmanager
def _transaction():
    undo = []
    try:
        yield undo
    except Exception as error:
        rollback_errors = []
        for restore in reversed(undo):
            try:
                restore()
            except Exception as undo_error:
                rollback_errors.append(str(undo_error))
        if rollback_errors:
            raise RuntimeError(f"{error}; rollback failed: {', '.join(rollback_errors)}") from error
        raise


def _set(modifier, key, value, undo):
    previous = getattr(modifier, key)
    if getattr(modifier.bl_rna.properties[key], "is_array", False):
        previous = tuple(previous)
    undo.append(lambda: setattr(modifier, key, previous))
    setattr(modifier, key, value)
    if isinstance(value, bpy.types.ID) and getattr(modifier, key) != value:
        raise ValueError(f"Blender declined the {modifier.type}.{key} reference")


def _append(obj, name, kind, values, undo):
    _require_name(name, "Modifier name")
    if obj.modifiers.get(name) is not None:
        raise ValueError(f"Modifier {name!r} already exists on {obj.name!r}")
    _budget_append(obj, name, kind, values)
    modifier = obj.modifiers.new(name, kind)
    undo.append(lambda: obj.modifiers.remove(modifier))
    if modifier.name != name:
        raise ValueError(f"Blender could not create a modifier named {name!r}")
    for key, value in values.items():
        _set(modifier, key, value, undo)
    return modifier


def _existing(obj, params, kind=None):
    name = _require_name(params["name"], "Modifier name")
    modifier = obj.modifiers.get(name)
    if modifier is None:
        raise ValueError(f"Modifier {name!r} does not exist on {obj.name!r}")
    expected = params.get("expected_type", kind)
    if expected is not None and modifier.type != expected:
        raise ValueError(f"Expected {expected} for modifier {name!r}, found {modifier.type}")
    if "expected_index" in params:
        expected_index = params["expected_index"]
        if type(expected_index) is not int or obj.modifiers.find(name) != expected_index:
            raise ValueError(f"Stale modifier index for {name!r}")
    return modifier


def _boolean_values(cutter, operation):
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValueError(f"Unknown Boolean operation: {operation!r}")
    return {"object": cutter, "operation": operation, "solver": "EXACT"}


def _stack_add(obj, p, undo):
    values = _settings(obj, p["type"], p["settings"])
    if p["type"] == "BOOLEAN" and values.get("object") is None:
        raise ValueError("An object operand is required for a new Boolean")
    return {"modifier": _snapshot(_append(obj, p["name"], p["type"], values, undo))}


def _stack_update(obj, p, undo):
    modifier = _existing(obj, p)
    settings = _settings(obj, modifier.type, p["settings"])
    if not settings:
        raise ValueError("Give at least one modifier parameter")
    if modifier.type == "BOOLEAN" and "object" in settings:
        raise ValueError("Use boolean_rebind to update an existing Boolean operand")
    _budget_update(obj, modifier.name, settings)
    before = _snapshot(modifier)
    for key, value in settings.items():
        _set(modifier, key, value, undo)
    return {"before": before, "modifier": _snapshot(modifier)}


def _stack_toggle(obj, p, undo):
    modifier = _existing(obj, p)
    flags = {"viewport": "show_viewport", "render": "show_render"}
    supplied = [key for key in flags if key in p]
    if not supplied or any(type(p[key]) is not bool for key in supplied):
        raise ValueError("Give at least one Boolean visibility flag")
    if any(p[key] and not getattr(modifier, flags[key]) for key in supplied):
        _budget_update(obj, modifier.name, viewport=p.get("viewport"), render=p.get("render"))
    for key in supplied:
        _set(modifier, flags[key], p[key], undo)
    return {"modifier": _snapshot(modifier)}


def _apply_order(obj, order):
    for index, name in enumerate(order):
        previous = obj.modifiers.find(name)
        if previous != index:
            obj.modifiers.move(previous, index)
    if list(obj.modifiers.keys()) != order:
        raise ValueError("Blender could not apply the requested modifier order")


def _stack_reorder(obj, p, undo):
    original = list(obj.modifiers.keys())
    if "expected_order" in p and p["expected_order"] != original:
        raise ValueError("Stale modifier order")
    if "order" in p and not {"name", "index"}.intersection(p):
        order = p["order"]
        if (not isinstance(order, list) or not all(isinstance(name, str) for name in order)
                or len(order) != len(original) or set(order) != set(original)):
            raise ValueError("order must include every current modifier exactly once")
    elif {"name", "index"} <= p.keys() and "order" not in p:
        name = _require_name(p["name"], "Modifier name")
        index = p["index"]
        if name not in original or type(index) is not int or not 0 <= index < len(original):
            raise ValueError("Existing modifier and zero-based stack index required")
        order = [current for current in original if current != name]
        order.insert(index, name)
    else:
        raise ValueError("Give the whole order or a name and index")
    plan = _plan(obj)
    _budget(obj, {name: plan[name] for name in order})
    undo.append(lambda: _apply_order(obj, original))
    _apply_order(obj, order)
    return {"before": original, "order": order}


def _boolean_consumers(obj):
    rows = []
    for other in bpy.data.objects:
        for index, modifier in enumerate(other.modifiers):
            if modifier.type != "BOOLEAN":
                continue
            if modifier.operand_type == "OBJECT" and modifier.object == obj:
                rows.append({"object": other.name, "modifier": modifier.name, "index": index, "operand_type": "OBJECT"})
            elif modifier.operand_type == "COLLECTION" and modifier.collection is not None:
                if obj.name in modifier.collection.all_objects:
                    rows.append({"object": other.name, "modifier": modifier.name, "index": index,
                                 "operand_type": "COLLECTION", "collection": modifier.collection.name})
    return sorted(rows, key=lambda row: (row["object"], row["index"]))


def _boolean_rebind(obj, p, undo):
    modifier = _existing(obj, p, "BOOLEAN")
    if modifier.operand_type != "OBJECT":
        raise ValueError("Only object Boolean operands can be rebound")
    previous = _value(modifier.object)
    if previous != p["expected_cutter"]:
        raise ValueError(f"Stale Boolean cutter: expected {p['expected_cutter']!r}, current {previous!r}")
    cutter = _source_cutter(obj, p["cutter"])
    _budget_update(obj, modifier.name, {"object": cutter})
    _set(modifier, "object", cutter, undo)
    return {"before_cutter": previous, "modifier": _snapshot(modifier), "index": obj.modifiers.find(modifier.name),
            "new_cutter_consumers": _boolean_consumers(cutter)}


def _boolean_batch(obj, p, undo):
    names = p.get("targets", [obj.name])
    if (not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names)
            or len(set(names)) != len(names) or obj.name not in names):
        raise ValueError("targets must identify distinct meshes, including the primary object")
    targets = [_local_mesh(_object(name, {"MESH"}), True) for name in names]
    cutter = _object(p["cutter"], {"MESH"})
    values = _boolean_values(cutter, p["operation"])
    name = _require_name(p["name"], "Modifier name")
    for target in targets:
        _check_reference(target, cutter)
        if target.modifiers.get(name) is not None:
            raise ValueError(f"Modifier {name!r} already exists on {target.name!r}")
        _budget_append(target, name, "BOOLEAN", values)
    return {"cutter": cutter.name, "changed": [
        {"object": target.name, "modifier": _snapshot(_append(target, name, "BOOLEAN", values, undo))}
        for target in targets]}


def _collection(obj, name):
    if name is None:
        if len(obj.users_collection) != 1:
            raise ValueError("Name the destination collection explicitly")
        result = obj.users_collection[0]
    else:
        name = _require_name(name, "Collection name")
        scene_root = bpy.context.scene.collection
        result = scene_root if scene_root.name == name else bpy.data.collections.get(name)
        if result is None or result != scene_root and result not in scene_root.children_recursive:
            raise ValueError(f"Collection is outside the scene: {name!r}")
    if result.library or result.override_library or not result.is_editable:
        raise ValueError(f"Destination collection is read-only: {result.name!r}")
    return result


def _copy_mesh_object(obj, name, collection, undo):
    clone = obj.copy()
    data = [None]

    def remove_copy():
        bpy.data.objects.remove(clone, do_unlink=True)
        if data[0] is not None:
            bpy.data.meshes.remove(data[0])

    undo.append(remove_copy)
    data[0] = obj.data.copy()
    clone.data = data[0]
    clone.name = name
    if clone.name != name:
        raise ValueError(f"Could not assign object name {name!r}")
    collection.objects.link(clone)
    return clone


def _boolean_slice(obj, p, undo):
    cutter = _source_cutter(obj, p["cutter"])
    collection = _collection(obj, p.get("collection"))
    name = _require_name(p.get("name", "Hard Surface Slice"), "Modifier name")
    difference = _require_name(p["difference_name"], "Difference output")
    intersection = _require_name(p["intersection_name"], "Intersect output")
    if difference == intersection or any(bpy.data.objects.get(value) for value in (difference, intersection)):
        raise ValueError("Slice output names must be new and distinct")
    if obj.modifiers.get(name) is not None:
        raise ValueError(f"Modifier name already exists on source {obj.name!r}: {name!r}")
    _budget_append(obj, name, "BOOLEAN", _boolean_values(cutter, "DIFFERENCE"))
    results = []
    for output, operation in ((difference, "DIFFERENCE"), (intersection, "INTERSECT")):
        clone = _copy_mesh_object(obj, output, collection, undo)
        branch = _append(clone, name, "BOOLEAN", _boolean_values(cutter, operation), undo)
        results.append({"object": clone.name, "mesh": clone.data.name, "modifier": _snapshot(branch)})
    return {"source": obj.name, "cutter": cutter.name, "outputs": results}


def _make_preset(obj, kind, name, values, undo):
    return {"modifier": _snapshot(_append(obj, name, kind, _settings(obj, kind, values), undo))}


def _merge_settings(p):
    if "merge_threshold" not in p:
        return {}
    return {"use_merge_vertices": True, "merge_threshold": p["merge_threshold"]}


def _array_linear(obj, p, undo):
    return _make_preset(obj, "ARRAY", p["name"], {
        "count": p["count"], "use_relative_offset": False, "use_constant_offset": True,
        "constant_offset_displace": p["offset"], **_merge_settings(p)}, undo)


def _axis_angle(p, default):
    axis = p["axis"]
    if not isinstance(axis, str) or axis not in {"X", "Y", "Z"}:
        raise ValueError("axis must be X, Y or Z")
    angle = _number(p.get("angle", default), -tau, tau, "angle")
    if abs(angle) < 1e-9:
        raise ValueError("Give a nonzero angle in radians")
    return axis, angle


def _array_radial(obj, p, undo):
    count = p["count"]
    if type(count) is not int or not 2 <= count <= MAX_VALUES["ARRAY", "count"]:
        raise ValueError("Radial count must be an integer between 2 and 1000")
    if not isinstance(p["pivot"], (tuple, list)) or len(p["pivot"]) != 3:
        raise ValueError("pivot must contain three local coordinates")
    pivot = Vector([_number(value, -1e6, 1e6, "pivot") for value in p["pivot"]])
    axis, angle = _axis_angle(p, tau)
    collection = _collection(obj, p.get("collection"))
    modifier_name = _require_name(p["name"], "Modifier name")
    name = _require_name(p.get("controller_name", f"{modifier_name} Pivot"), "Pivot name")
    if bpy.data.objects.get(name) is not None or obj.modifiers.get(modifier_name) is not None:
        raise ValueError("Radial array requires a new modifier name and pivot object")
    values = _settings(obj, "ARRAY", {"count": count, "use_constant_offset": False, "use_relative_offset": False,
                                      "use_object_offset": True, **_merge_settings(p)})
    _budget_append(obj, modifier_name, "ARRAY", values)
    controller = bpy.data.objects.new(name, None)
    undo.append(lambda: bpy.data.objects.remove(controller, do_unlink=True))
    collection.objects.link(controller)
    controller.empty_display_type = "PLAIN_AXES"
    controller.parent = obj
    step = angle/count
    controller.matrix_basis = Matrix.Translation(pivot) @ Matrix.Rotation(step, 4, axis) @ Matrix.Translation(-pivot)
    modifier = _append(obj, modifier_name, "ARRAY", {**values, "offset_object": controller}, undo)
    return {"modifier": _snapshot(modifier), "controller": controller.name,
            "local_pivot": list(pivot), "step_radians": step}


def _revolve(obj, p, undo):
    axis, angle = _axis_angle(p, tau)
    return _make_preset(obj, "SCREW", p["name"], {
        "axis": axis, "angle": angle, "steps": p["steps"], "render_steps": p["steps"],
        "screw_offset": p.get("screw_offset", 0.0), **_merge_settings(p)}, undo)


_IMPLEMENTATIONS = {
    "stack_add": _stack_add, "stack_update": _stack_update, "stack_toggle": _stack_toggle,
    "stack_reorder": _stack_reorder, "boolean_rebind": _boolean_rebind, "boolean_batch": _boolean_batch,
    "boolean_slice": _boolean_slice, "array_linear": _array_linear, "array_radial": _array_radial, "revolve": _revolve,
}


def execute(obj, tool, params):
    """Apply one bounded native operation and return JSON-compatible evidence."""
    if not isinstance(tool, str) or tool not in _ACTIONS or not isinstance(params, dict):
        raise ValueError("Known stack tool and a params object are required")
    _, required, optional = _ACTIONS[tool]
    missing = set(required) - params.keys()
    extra = params.keys() - set(required) - set(optional)
    if missing or extra:
        raise ValueError(f"Missing fields {sorted(missing)} or unknown fields {sorted(extra)}")
    read = tool in {"stack_snapshot", "boolean_consumers"}
    _local_mesh(obj, write=not read)
    evidence = {"tool": tool, "object": obj.name}
    if tool == "stack_snapshot":
        return {**evidence, "stack": [_snapshot(modifier) for modifier in obj.modifiers]}
    if tool == "boolean_consumers":
        return {**evidence, "consumers": _boolean_consumers(obj)}
    with _transaction() as undo:
        return {**evidence, **_IMPLEMENTATIONS[tool](obj, params, undo)}
