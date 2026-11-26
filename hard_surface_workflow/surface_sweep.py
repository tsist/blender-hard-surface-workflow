"""Validated recipe, named Blender controls, and atomic surface sweep commits."""
import hashlib
import json
import math

import bpy
from mathutils import Vector

from .shell_tools import _destination
from .surface_sweep_geometry import MM, solve

TOOL = "surface_sweep"
RECIPE_KEY = "hsw_surface_sweep_recipe"
OWNER_KEY = "hsw_surface_sweep_owner"
STATION_KEY = "hsw_surface_sweep_station"
SCHEMA = 1
COORD_EPS_MM = 1e-4

_REQUIRED = {"name", "stations", "ray_direction", "outward_normal", "max_distance_mm",
             "max_jump_mm", "max_normal_angle_deg", "profile", "gap_mm", "min_clearance_mm",
             "dimensional_tolerance_mm", "gap_tolerance_mm"}
_OPTIONAL = {"sampling_mm", "collection", "preview", "expected_signature", "expected_preview_signature"}


def catalog():
    return {TOOL: {"label": "Sweep a Section Along a Surface", "read_only": False,
                   "required": sorted(_REQUIRED), "optional": sorted(_OPTIONAL),
                   "parameters": {
                       "name": "Managed output mesh object name.",
                       "stations": "Ordered unique world-mm station records. Each has name, position_mm, locked, "
                                   "and optional corner (ROUND, STRAIGHT, MITER), radius_mm, or miter_limit_deg.",
                       "ray_direction": "Nonzero world projection direction.",
                       "outward_normal": "Reference normal of the admissible installation side.",
                       "max_distance_mm": "Maximum projection distance.",
                       "max_jump_mm": "Maximum separation between adjacent projected samples.",
                       "max_normal_angle_deg": "Maximum normal angle from the installation normal or previous hit.",
                       "profile": "RECTANGLE uses width_mm, height_mm. PIPE uses outer_diameter_mm, wall_mm, "
                                  "and optional even sides.",
                       "gap_mm": "Nominal bottom/outer-wall offset along the local outward axis.",
                       "min_clearance_mm": "Minimum acceptable distance to the evaluated shell.",
                       "dimensional_tolerance_mm": "Maximum absolute dimensional error in a section.",
                       "gap_tolerance_mm": "Maximum absolute error in the installation gap.",
                       "sampling_mm": "Maximum distance between projected guide samples (default 1.5).",
                       "collection": "Destination scene collection for creation.",
                       "preview": "Return validation report without mutating the scene.",
                       "expected_preview_signature": "Bind a commit to the recipe and shell from a previous preview.",
                       "expected_signature": "Current signature required when updating a managed mesh."}}}


def _name(value, field, max_bytes=63):
    if (not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > max_bytes
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"{field} must be a nonempty, bounded Blender name")
    return value


def _number(value, field, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be a finite number from {minimum} to {maximum}")
    return float(value)


def _vector(value, field, normalize=False):
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError(f"{field} must have exactly three coordinates")
    result = [_number(axis, field, -1e6, 1e6) for axis in value]
    if normalize:
        direction = Vector(result)
        if direction.length <= 1e-6:
            raise ValueError(f"{field} cannot be zero")
        if abs(direction.length - 1.0) > 1e-7:
            direction.normalize()
        return list(direction)
    return result


def _recipe(values):
    if not isinstance(values, dict) or not _REQUIRED <= set(values) or set(values) - _REQUIRED - _OPTIONAL:
        raise ValueError("Check the required and optional surface_sweep recipe fields")
    result = {"name": _name(values["name"], "name")}
    if "collection" in values:
        result["collection"] = _name(values["collection"], "collection")
    if "preview" in values and type(values["preview"]) is not bool:
        raise ValueError("preview must be a boolean")
    for key in ("ray_direction", "outward_normal"):
        result[key] = _vector(values[key], key, normalize=True)
    if Vector(result["ray_direction"]).dot(Vector(result["outward_normal"])) >= -1e-5:
        raise ValueError("The projection direction does not approach the designated installation side")
    constraints = {
        "max_distance_mm": (0.001, 10000), "max_jump_mm": (0.001, 1000),
        "max_normal_angle_deg": (1, 89), "gap_mm": (0, 100),
        "min_clearance_mm": (0, 100), "dimensional_tolerance_mm": (0.0001, 10),
        "gap_tolerance_mm": (0.0001, 10),
    }
    for key, limits in constraints.items():
        result[key] = _number(values[key], key, *limits)
    result["sampling_mm"] = _number(values.get("sampling_mm", 1.5), "sampling_mm", 0.025, 20)
    if result["min_clearance_mm"] > result["gap_mm"]:
        raise ValueError("min_clearance_mm cannot exceed the nominal gap_mm")
    raw = values["stations"]
    if not isinstance(raw, list) or not 2 <= len(raw) <= 64:
        raise ValueError("Give 2–64 named stations")
    stations = []
    for index, row in enumerate(raw):
        if (not isinstance(row, dict) or not {"name", "position_mm", "locked"} <= row.keys()
                or row.keys() - {"name", "position_mm", "locked", "corner", "radius_mm", "miter_limit_deg"}):
            raise ValueError(f"Invalid station record {index+1}")
        name = _name(row["name"], "station name")
        if type(row["locked"]) is not bool:
            raise ValueError(f"{name}: locked must be a boolean")
        endpoint = index in (0, len(raw) - 1)
        corner = row.get("corner", "STRAIGHT" if endpoint or (row["locked"] and "radius_mm" not in row) else "ROUND")
        if corner not in ("STRAIGHT", "ROUND", "MITER"):
            raise ValueError(f"{name}: unknown corner type")
        if ("radius_mm" in row and corner != "ROUND") or ("miter_limit_deg" in row and corner != "MITER"):
            raise ValueError(f"{name}: radius requires ROUND and a miter limit requires MITER")
        if endpoint and corner != "STRAIGHT":
            raise ValueError(f"{name}: endpoint controls use straight sections")
        station = {"name": name, "locked": row["locked"],
                   "position_mm": _vector(row["position_mm"], f"{name}/position_mm"), "corner": corner}
        if corner == "ROUND":
            station["radius_mm"] = _number(row.get("radius_mm", 6), f"{name}/radius_mm", 0.01, 1000)
        elif corner == "MITER":
            station["miter_limit_deg"] = _number(row.get("miter_limit_deg", 135), f"{name}/miter_limit_deg", 1, 175)
        stations.append(station)
    if len({station["name"] for station in stations}) != len(stations):
        raise ValueError("Station names must be unique")
    if not stations[0]["locked"] or not stations[-1]["locked"]:
        raise ValueError("Lock both endpoint stations")
    result["stations"] = stations
    profile = values["profile"]
    if not isinstance(profile, dict):
        raise ValueError("profile must be a recipe object")
    if profile.get("type") == "RECTANGLE" and profile.keys() == {"type", "width_mm", "height_mm"}:
        result["profile"] = {"type": "RECTANGLE",
                             "width_mm": _number(profile["width_mm"], "width_mm", .01, 200),
                             "height_mm": _number(profile["height_mm"], "height_mm", .01, 200)}
    elif (profile.get("type") == "PIPE" and {"type", "outer_diameter_mm", "wall_mm"} <= profile.keys()
          and not profile.keys() - {"type", "outer_diameter_mm", "wall_mm", "sides"}):
        diameter = _number(profile["outer_diameter_mm"], "outer_diameter_mm", .04, 200)
        wall = _number(profile["wall_mm"], "wall_mm", .01, diameter / 2 - .005)
        sides = profile.get("sides", 32)
        if type(sides) is not int or not 16 <= sides <= 64 or sides % 4:
            raise ValueError("Pipe sides must be a multiple of 4 from 16 to 64")
        if diameter * (1 - math.cos(math.pi / sides)) > result["dimensional_tolerance_mm"]:
            raise ValueError("Pipe sides are too few for its diameter tolerance")
        result["profile"] = {"type": "PIPE", "outer_diameter_mm": diameter, "wall_mm": wall, "sides": sides}
    else:
        raise ValueError("Give a RECTANGLE or an open-bore PIPE profile with its dimensional fields")
    return result


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _mesh_hash(data):
    positions = [tuple(vertex.co) for vertex in data.vertices]
    edges = [tuple(edge.vertices) for edge in data.edges]
    faces = [tuple(face.vertices) for face in data.polygons]
    face_settings = [(face.material_index, face.use_smooth) for face in data.polygons]
    attributes = [(attr.name, attr.domain, attr.data_type) for attr in data.attributes
                  if not attr.name.startswith(".select_")]
    return _hash([positions, edges, faces, face_settings, attributes])


def _record(obj):
    try:
        record = json.loads(obj.get(RECIPE_KEY, ""))
        immutable = {key: value for key, value in record.items() if key != "signature"}
        if record["schema"] != SCHEMA or _hash(immutable) != record["signature"]:
            raise ValueError("invalid signature")
        return record
    except (AttributeError, TypeError, ValueError, KeyError) as error:
        raise ValueError("Surface sweep recipe is missing, stale, or damaged") from error


def find_output(obj):
    if obj is None:
        return None
    if obj.type == "MESH" and RECIPE_KEY in obj:
        return obj
    if obj.type != "EMPTY" or OWNER_KEY not in obj:
        return None
    candidate = bpy.context.scene.objects.get(obj[OWNER_KEY])
    return candidate if candidate is not None and RECIPE_KEY in candidate else None


def _controls(output, record):
    station_map = {}
    for name, item in record["controls"].items():
        control = bpy.context.scene.objects.get(item)
        if (control is None or find_output(control) != output or control.get(STATION_KEY) != name
                or control.parent or control.constraints or control.animation_data or not control.is_editable):
            raise ValueError(f"{name}: the saved station control identity or transform ownership is stale")
        station_map[name] = control
    return station_map


def params_from_controls(output):
    record = _record(output)
    target = bpy.context.view_layer.objects.get(record["target"])
    if target is None:
        raise ValueError("The saved surface target is absent in this View Layer")
    controls = _controls(output, record)
    params = record["recipe"]
    for station in params["stations"]:
        pos = controls[station["name"]].location / MM
        if (pos - Vector(station["position_mm"])).length > COORD_EPS_MM:
            station["position_mm"] = list(pos)
    params["expected_signature"] = record["signature"]
    return target, params


def restore_controls(output):
    record = _record(output)
    controls = _controls(output, record)
    for station in record["recipe"]["stations"]:
        controls[station["name"]].location = Vector(station["position_mm"]) * MM


def _identity(output):
    return all(abs(output.matrix_world[i][j] - float(i == j)) <= 1e-7 for i in range(4) for j in range(4))


def _check_output(output, values, target, recipe):
    expected = values.get("expected_signature")
    if output is None:
        if expected is not None:
            raise ValueError("The signed sweep output does not exist")
        return None, None
    record = _record(output)
    if expected != record["signature"] or record["target"] != target.name:
        raise ValueError("The sweep signature or target is stale")
    if (output.type != "MESH" or output.mode != "OBJECT" or output.data.users != 1
            or not output.is_editable or not output.data.is_editable or not _identity(output)
            or output.parent or output.constraints or output.modifiers or output.data.shape_keys
            or output.animation_data or output.vertex_groups or output.data.uv_layers):
        raise ValueError("The managed output needs independent, untransformed mesh data without external edits")
    if record["mesh_sha256"] != _mesh_hash(output.data):
        raise ValueError("The managed sweep mesh was edited outside its recipe")
    if output.name not in bpy.context.view_layer.objects:
        raise ValueError("The sweep output is absent in the current View Layer")
    if recipe.get("collection") != record["recipe"].get("collection"):
        raise ValueError("A signed update keeps the destination collection")
    saved = record["recipe"]["stations"]
    stations = recipe["stations"]
    if [(s["name"], s["locked"]) for s in saved] != [(s["name"], s["locked"]) for s in stations]:
        raise ValueError("A signed update keeps station identities, order and locks")
    changed = []
    for index, (before, after) in enumerate(zip(saved, stations, strict=True)):
        if (Vector(before["position_mm"]) - Vector(after["position_mm"])).length <= COORD_EPS_MM:
            after["position_mm"] = before["position_mm"]
        if before != after:
            if before["locked"]:
                raise ValueError(f"{before['name']}: a locked station cannot be changed")
            changed.append(index)
    if not changed:
        return record, None
    if len(changed) != 1:
        raise ValueError("Change one named station at a time")
    common = {key: value for key, value in recipe.items() if key != "stations"}
    if common != {key: value for key, value in record["recipe"].items() if key != "stations"}:
        raise ValueError("Apply a local station change separately from global profile or projection edits")
    i = changed[0]
    left = next(j for j in reversed(range(i)) if stations[j]["locked"])
    right = next(j for j in range(i+1, len(stations)) if stations[j]["locked"])
    return record, (stations[left]["name"], stations[right]["name"])


def _protected(output, record, result, limits):
    old = record["geometry"]
    new = result["report"]
    if old["profile_count"] != new["profile_count"] or old["has_inner_wall"] != new["has_inner_wall"]:
        raise ValueError("A local change must keep the profile vertex layout")
    left, right = limits
    if any(name not in row["control_rings"] for name in limits for row in (old, new)):
        raise ValueError(f"{left} / {right}: local edit requires exact section rings at the locked stations")
    old_coords = [tuple(vertex.co) for vertex in output.data.vertices]
    candidate = result["vertices"]
    n = new["profile_count"]
    max_delta = 0.0
    for layer in range(2 if new["has_inner_wall"] else 1):
        for name, prefix in ((left, True), (right, False)):
            def indices(data):
                start = layer * data["ring_count"]
                lock = data["control_rings"][name]
                return range(start*n, (start+lock+1)*n) if prefix else range((start+lock)*n, (start+data["ring_count"])*n)
            before, after = indices(old), indices(new)
            if len(before) != len(after):
                raise ValueError(f"{name}: the protected span changed its sampling layout")
            for a, b in zip(before, after, strict=True):
                difference = (Vector(old_coords[a]) - Vector(candidate[b])).length / MM
                max_delta = max(max_delta, difference)
                if difference > COORD_EPS_MM:
                    raise ValueError(f"{name}: local edit moved a protected section by {difference:.6g} mm")
                candidate[b] = old_coords[a]
    new["protected_max_delta_mm"] = max_delta
    new["editable_interval"] = list(limits)


def _prepared(target, values):
    if (bpy.context.mode != "OBJECT" or target is None or target.type != "MESH"
            or bpy.context.view_layer.objects.get(target.name) != target):
        raise ValueError("Choose a mesh target in Object Mode in the current View Layer")
    if not math.isclose(bpy.context.scene.unit_settings.scale_length, 1.0, abs_tol=1e-7):
        raise ValueError("The world-mm recipe expects scene scale_length=1 (1 BU per metre)")
    recipe = _recipe(values)
    bpy.context.view_layer.update()
    output = bpy.data.objects.get(recipe["name"])
    if output == target:
        raise ValueError("The output and target must be different objects")
    record, interval = _check_output(output, values, target, recipe)
    controls = _controls(output, record) if record else {}
    if record:
        for station in recipe["stations"]:
            actual = controls[station["name"]].location / MM
            saved = next(row for row in record["recipe"]["stations"] if row["name"] == station["name"])
            if min((actual - Vector(row["position_mm"])).length for row in (saved, station)) > COORD_EPS_MM:
                raise ValueError(f"{station['name']}: a pending scene control edit conflicts with this recipe")
    result = solve(target, recipe)
    if record and record["target_sha256"] != result["target_sha256"]:
        raise ValueError("The evaluated projection target changed since the last accepted sweep")
    report = result["report"]
    report["control_rings"] = {row["station"]: i for i, row in enumerate(report["stations"]) if row["is_control"]}
    if interval:
        _protected(output, record, result, interval)
    report["source"] = target.name
    report["output"] = recipe["name"]
    report["status"] = "PREVIEW"
    if record:
        report["signature"] = record["signature"]
    preview_signature = _hash([recipe, result["target_sha256"], report.get("signature")])
    if values.get("expected_preview_signature", preview_signature) != preview_signature:
        raise ValueError("The recipe or target changed after preview. Preview again before committing")
    report["preview_signature"] = preview_signature
    return result, recipe, output, record, controls


def preview_geometry(target, params):
    result, _, _, _, _ = _prepared(target, params)
    return result


def _shade_walls(mesh, report):
    profile_count = report["profile_count"]
    ring_count = report["ring_count"]
    pipe = report["has_inner_wall"]
    wall_faces = (ring_count - 1) * profile_count * (2 if pipe else 1)
    for face in mesh.polygons:
        face.use_smooth = face.index < wall_faces
    if not pipe:
        # Each longitudinal strip is smooth along the path, with hard profile corners.
        for edge in mesh.edges:
            first, last = edge.vertices
            edge.use_edge_sharp = abs(first - last) == profile_count


def execute(target, tool, params):
    if tool != TOOL:
        raise ValueError("Expected surface_sweep")
    result, recipe, output, record, controls = _prepared(target, params)
    report = result["report"]
    if params.get("preview"):
        return report
    if record and recipe == record["recipe"]:
        report["status"] = "UNCHANGED"
        return report
    destination = _destination(target, recipe.get("collection")) if output is None else None
    mesh = bpy.data.meshes.new(recipe["name"] + " Sweep")
    created = []
    old_mesh = output.data if output is not None else None
    old_record = output.get(RECIPE_KEY) if output is not None else None
    positions = {name: item.location.copy() for name, item in controls.items()}
    try:
        mesh.from_pydata(result["vertices"], [], result["faces"])
        if mesh.validate():
            raise ValueError("Blender detected invalid sweep topology")
        _shade_walls(mesh, report)
        mesh.update()
        if old_mesh is None:
            output = bpy.data.objects.new(recipe["name"], mesh)
            created.append(output)
            destination.objects.link(output)
            for index, station in enumerate(recipe["stations"], 1):
                control = bpy.data.objects.new(f"{recipe['name'][:38]} | {index:02d} {station['name']}", None)
                created.append(control)
                destination.objects.link(control)
                control.empty_display_type = "SPHERE"
                control.empty_display_size = .0008
                control.show_in_front = True
                control.hide_render = True
                control.lock_rotation = (True, True, True)
                control.lock_scale = (True, True, True)
                control.lock_location = (station["locked"],) * 3
                control[OWNER_KEY] = output.name
                control[STATION_KEY] = station["name"]
                controls[station["name"]] = control
        else:
            for material in old_mesh.materials:
                mesh.materials.append(material)
            output.data = mesh
        for station in recipe["stations"]:
            controls[station["name"]].location = Vector(station["position_mm"]) * MM
        new = {"schema": SCHEMA, "target": target.name, "target_sha256": result["target_sha256"],
               "recipe": recipe, "mesh_sha256": _mesh_hash(mesh),
               "controls": {name: item.name for name, item in controls.items()},
               "geometry": {field: report[field] for field in ("ring_count", "profile_count", "has_inner_wall",
                                                               "control_rings")},
               "path_mm": [row["position_mm"] for row in report["stations"]]}
        new["signature"] = _hash(new)
        output[RECIPE_KEY] = json.dumps(new, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        if old_mesh is not None:
            output.data = old_mesh
            output[RECIPE_KEY] = old_record
            for name, value in positions.items():
                controls[name].location = value
        for item in reversed(created):
            bpy.data.objects.remove(item, do_unlink=True)
        bpy.data.meshes.remove(mesh)
        raise
    if old_mesh is not None and old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)
    report["signature"] = new["signature"]
    report["status"] = "UPDATED" if record else "CREATED"
    return report
