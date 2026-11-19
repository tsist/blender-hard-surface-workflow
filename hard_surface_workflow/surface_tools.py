"""Explicit section cages and directional guides shared by GUI and CLI."""

import hashlib
import json
import math
import re

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from .shell_tools import _destination


SECTION = "section_surface"
PROJECT = "surface_project_path"
TOOLS = frozenset({SECTION, PROJECT})
RECIPE_PROPERTY = "hsw_surface_recipe"
MAX_PROJECTED_POINTS = 4096
VECTOR_LIMIT = 1.0e8


def catalog():
    common = {
        "name": "Exact output object name. For an update, keep the existing name.",
        "collection": "Destination scene collection; defaults to the anchor's single collection.",
        "closed": "Join the last column or guide point back to the first without duplicating it.",
        "expected_signature": "Required for updates. Use the previous operation's signature.",
    }
    return {
        SECTION: {
            "label": "Build a Named Section Cage",
            "required": ["name", "landmarks", "sections"],
            "optional": ["closed", "flip_normals", "collection", "expected_signature"],
            "read_only": False,
            "parameters": {
                **common,
                "landmarks": "Ordered unique names for columns shared across sections (2–128).",
                "sections": "Ordered {name, points} sections (2–64); world xyz BU, one point per landmark.",
                "flip_normals": "Reverse the winding of every quad. Keep it fixed for an in-place update.",
            },
        },
        PROJECT: {
            "label": "Project a Directional Surface Path",
            "required": ["name", "points", "direction", "max_distance"],
            "optional": ["closed", "samples_per_span", "max_jump", "collection", "expected_signature"],
            "read_only": False,
            "parameters": {
                **common,
                "points": "Ordered world xyz ray origins in BU. Omit any duplicate closing point.",
                "direction": "Nonzero world xyz direction. The operation normalizes it.",
                "max_distance": "Positive world ray distance in BU, measured from each origin.",
                "samples_per_span": "Uniform intervals per origin span, 1–32; default 8.",
                "max_jump": "Optional world BU limit between successive hits, including the closing edge.",
            },
        },
    }


def _name(value, label):
    if not isinstance(value, str) or not value.strip() or not re.fullmatch(r"[^\x00-\x1f\x7f]+", value):
        raise ValueError(f"{label} must be a nonempty name")
    try:
        if len(value.encode("utf-8")) <= 63:
            return value
    except UnicodeError:
        pass
    raise ValueError(f"{label} must be a valid name of at most 63 UTF-8 bytes")


def _number(value, label, *, positive=False):
    if type(value) not in (float, int) or not -VECTOR_LIMIT <= value <= VECTOR_LIMIT:
        raise ValueError(f"{label} must be finite, with magnitude at most {VECTOR_LIMIT:g}")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive")
    return float(value)


def _vector(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must be an xyz vector")
    return Vector(tuple(_number(component, label) for component in value))


def _array(value, label, lo, hi):
    if not isinstance(value, (list, tuple)) or not lo <= len(value) <= hi:
        raise ValueError(f"{label} requires {lo} to {hi} items")
    return value


def _bool(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _bounds(points):
    return {
        "world_min": [min(point[axis] for point in points) for axis in range(3)],
        "world_max": [max(point[axis] for point in points) for axis in range(3)],
    }


def _section_geometry(params, closed):
    names = [_name(item, "landmark") for item in
             _array(params["landmarks"], "landmarks", 3 if closed else 2, 128)]
    if len(set(names)) != len(names):
        raise ValueError("Landmark names must be unique")
    sections = []
    vertices = []
    for row in _array(params["sections"], "sections", 2, 64):
        if not isinstance(row, dict) or row.keys() != {"name", "points"}:
            raise ValueError("Each section must contain exactly name and points")
        station = _name(row["name"], "section name")
        points = [_vector(value, f"{station}/{landmark}") for landmark, value in zip(
            names, _array(row["points"], station, len(names), len(names)))]
        sections.append({"name": station, "points": [list(point) for point in points]})
        vertices.extend(points)
    if len({row["name"] for row in sections}) != len(sections):
        raise ValueError("Section names must be unique")

    width = len(names)
    reverse = _bool(params.get("flip_normals", False), "flip_normals")
    faces = []
    for row in range(len(sections) - 1):
        for column in range(width if closed else width - 1):
            a = row * width + column
            b = row * width + (column + 1) % width
            face = (a, b, b + width, a + width)
            p, q, r, s = (vertices[index] for index in face)
            # Check both possible triangulations. Opposing normals mean the cell
            # is too folded or concave to represent by a single working quad.
            normals = ((q - p).cross(r - p), (r - p).cross(s - p),
                       (q - p).cross(s - p), (r - q).cross(s - q))
            for first, second in (normals[:2], normals[2:]):
                if first.dot(second) <= 0:
                    raise ValueError(
                        f"Folded or degenerate quad at {sections[row]['name']} / {names[column]}")
            faces.append(tuple(reversed(face)) if reverse else face)
    return {"landmarks": names, "sections": sections, "flip_normals": reverse}, vertices, faces


def _project_geometry(target, params, closed):
    if target.type != "MESH":
        raise ValueError("A directional path requires a mesh target in the current View Layer")
    minimum = 3 if closed else 2
    origins = [_vector(value, "ray origin") for value in
               _array(params["points"], "points", minimum, MAX_PROJECTED_POINTS)]
    direction = _vector(params["direction"], "direction")
    if direction.length_squared == 0:
        raise ValueError("Direction must be nonzero")
    direction.normalize()
    distance = _number(params["max_distance"], "max_distance", positive=True)
    jump = params.get("max_jump")
    if jump is not None:
        jump = _number(jump, "max_jump", positive=True)
    steps = params.get("samples_per_span", 8)
    if type(steps) is not int or not 1 <= steps <= 32:
        raise ValueError("samples_per_span must be an integer from 1 to 32")
    end = len(origins) if closed else len(origins) - 1
    if end * steps + (0 if closed else 1) > MAX_PROJECTED_POINTS:
        raise ValueError(f"Projected path exceeds {MAX_PROJECTED_POINTS} sampled points")
    spans = [(origins[index], origins[(index + 1) % len(origins)]) for index in range(end)]
    if any(first == second for first, second in spans):
        raise ValueError("Adjacent ray origins must differ, including the closing span")
    samples = [first.lerp(second, index / steps)
               for first, second in spans for index in range(steps)]
    if not closed:
        samples.append(origins[-1])

    graph = bpy.context.evaluated_depsgraph_get()
    matrix = target.evaluated_get(graph).matrix_world.copy()
    try:
        inverse = matrix.inverted()
    except ValueError as error:
        raise ValueError("Projection target transform is singular") from error
    local_direction = inverse.to_3x3() @ direction
    scale = local_direction.length
    if not math.isfinite(scale) or scale <= 0 or distance * scale > 1.0e30:
        raise ValueError("Projection target has an invalid transform")
    tree = BVHTree.FromObject(target, graph)
    if tree is None:
        raise ValueError("Projection target has no evaluated faces")
    hits = []
    lengths = []
    for index, origin in enumerate(samples):
        hit, _, _, _ = tree.ray_cast(inverse @ origin, local_direction / scale, distance * scale)
        if hit is None:
            raise ValueError(f"Ray sample {index} missed target: {tuple(origin)}")
        world = matrix @ hit
        if not all(math.isfinite(axis) for axis in world):
            raise ValueError(f"Ray sample {index} produced a nonfinite hit")
        hits.append(world)
        lengths.append((world - origin).length)
    pairs = zip(hits, hits[1:] + (hits[:1] if closed else []))
    for index, (first, second) in enumerate(pairs):
        length = (second - first).length
        if length == 0 or (jump is not None and length > jump):
            raise ValueError(f"Projected segment {index} is collapsed or exceeds max_jump")
    return {
        "target": target.name, "points": [list(origin) for origin in origins],
        "direction": list(direction), "max_distance": distance,
        "samples_per_span": steps, "max_jump": jump,
    }, hits, lengths


def _coordinates(obj):
    if obj.type == "MESH":
        return [tuple(vertex.co) for vertex in obj.data.vertices]
    return [tuple(point.co) for point in obj.data.splines[0].points]


def _topology(obj):
    if obj.type == "MESH":
        return {
            "edges": [tuple(edge.vertices) for edge in obj.data.edges],
            "faces": [tuple(face.vertices) for face in obj.data.polygons],
        }
    return [(spline.type, spline.use_cyclic_u, len(spline.points)) for spline in obj.data.splines]


def _signature(obj, recipe):
    topology = _topology(obj)
    coordinates = _coordinates(obj) if obj.type == "MESH" or len(topology) == 1 else []
    record = (recipe, topology, coordinates, [tuple(row) for row in obj.matrix_world])
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stored_update(obj, tool, recipe, signature, points, collection):
    try:
        previous = json.loads(obj.get(RECIPE_PROPERTY, ""))
    except (TypeError, ValueError) as error:
        raise ValueError("Stale or unmanaged output name. Choose a fresh name") from error
    if (not isinstance(previous, dict) or previous.get("version") != 1 or previous.get("tool") != tool
            or obj.type != ("MESH" if tool == SECTION else "CURVE")):
        raise ValueError("Stale or unmanaged output name. Choose a fresh name")
    if (bpy.context.scene.objects.get(obj.name) != obj or obj.mode != "OBJECT"
            or not obj.is_editable or not obj.data.is_editable
            or obj.data.users - int(obj.data.use_fake_user) != 1 or getattr(obj.data, "shape_keys", None)):
        raise ValueError("The output must be editable in Object Mode in this scene, with independent data and no shape keys")
    if collection is not None and not any(item.name == collection for item in obj.users_collection):
        raise ValueError("An in-place update keeps the existing output collection")
    old_recipe = previous.get("definition")
    if (not signature or not isinstance(old_recipe, dict) or signature != previous.get("signature")
            or signature != _signature(obj, old_recipe)):
        raise ValueError("Stale output signature or hand-edited geometry. Use the current generated version or a fresh name")

    def layout(value):
        if tool == SECTION:
            return (value["closed"], value["flip_normals"], value["landmarks"],
                    [row["name"] for row in value["sections"]])
        return (value["closed"], value["target"], len(value["points"]), value["samples_per_span"])

    if len(_coordinates(obj)) != len(points) or layout(old_recipe) != layout(recipe):
        raise ValueError("An in-place update keeps named section layout, path sampling, and point count")
    return previous


def _set_coordinates(obj, coordinates):
    if obj.type == "MESH":
        obj.data.vertices.foreach_set("co", [value for point in coordinates for value in point])
        obj.data.update()
    else:
        for point, coordinate in zip(obj.data.splines[0].points, coordinates):
            point.co = coordinate
        obj.data.update_tag()


def _save_result(anchor, tool, name, recipe, points, faces, collection, expected):
    output = bpy.data.objects.get(name)
    coords = points if tool == SECTION else [(*point, 1.0) for point in points]
    if output is not None:
        previous = _stored_update(output, tool, recipe, expected, points, collection)
        previous_coordinates = _coordinates(output)
        try:
            _set_coordinates(output, coords)
            signature = _save_recipe(output, tool, recipe)
        except Exception:
            _set_coordinates(output, previous_coordinates)
            output[RECIPE_PROPERTY] = json.dumps(previous, ensure_ascii=False)
            raise
        return output, signature, True
    if expected is not None:
        raise ValueError("Stale update: the named output does not exist")

    destination = _destination(anchor, collection)
    store = bpy.data.meshes if tool == SECTION else bpy.data.curves
    data = store.new(name) if tool == SECTION else store.new(name, "CURVE")
    try:
        if tool == SECTION:
            data.from_pydata(points, [], faces)
            if data.validate(verbose=False):
                raise ValueError("Blender detected an invalid section cage")
            data.update()
        else:
            data.dimensions = "3D"
            spline = data.splines.new("POLY")
            spline.points.add(len(coords) - 1)
            for point, coordinate in zip(spline.points, coords):
                point.co = coordinate
            spline.use_cyclic_u = recipe["closed"]
        output = bpy.data.objects.new(name, data)
        destination.objects.link(output)
        signature = _save_recipe(output, tool, recipe)
        return output, signature, False
    except Exception:
        if output is not None:
            bpy.data.objects.remove(output, do_unlink=True)
        store.remove(data)
        raise


def _save_recipe(obj, tool, recipe):
    signature = _signature(obj, recipe)
    record = {"version": 1, "tool": tool, "definition": recipe, "signature": signature}
    obj[RECIPE_PROPERTY] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return signature


def execute(obj, tool, params):
    spec = catalog().get(tool)
    if spec is None or not isinstance(params, dict):
        raise ValueError("Expected a supported surface tool and a parameter object")
    present = params.keys()
    required = set(spec["required"])
    if not required <= present or present - required - set(spec["optional"]):
        raise ValueError(f"{tool} requires {spec['required']} and accepts {spec['optional']}")
    layer = bpy.context.view_layer
    if bpy.context.mode != "OBJECT" or obj is None or layer.objects.get(obj.name) != obj:
        raise ValueError("Choose a source in the current View Layer and run in Object Mode")

    name = _name(params["name"], "output")
    collection = _name(params["collection"], "collection") if "collection" in params else None
    closed = _bool(params.get("closed", False), "closed")
    expected = params.get("expected_signature")
    if "expected_signature" in params and (not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        raise ValueError("expected_signature must be a lowercase SHA-256")
    layer.update()
    report = {"tool": tool, "source": obj.name, "output": name, "closed": closed}
    if tool == SECTION:
        recipe, points, faces = _section_geometry(params, closed)
        width = len(recipe["landmarks"])
        report.update(
            vertices=len(points), quads=len(faces), landmarks=recipe["landmarks"],
            station_vertices={row["name"]: list(range(index * width, (index + 1) * width))
                              for index, row in enumerate(recipe["sections"])},
            landmark_vertices={landmark: list(range(index, len(points), width))
                               for index, landmark in enumerate(recipe["landmarks"])},
        )
    else:
        recipe, points, lengths = _project_geometry(obj, params, closed)
        faces = None
        report.update(
            point_count=len(points), frame=bpy.context.scene.frame_current,
            hit_distance_min=min(lengths), hit_distance_max=max(lengths),
        )
    recipe["closed"] = closed
    output, signature, updated = _save_result(obj, tool, name, recipe, points, faces, collection, expected)
    return {**report, **_bounds(points), "output": output.name, "signature": signature, "updated": updated}
