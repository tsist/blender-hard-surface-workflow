"""Build a removable cover and a fixed seat from one control-mesh boundary."""

import hashlib
import json
import math
import uuid
from collections import defaultdict

import bmesh
import bpy
from bpy.props import FloatProperty, StringProperty
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from . import selection, shell_tools


TOOL = "shell_panel_seat"
RESIZE = "shell_panel_seat_resize"
TOOLS = {TOOL, RESIZE}
_META = "hsw_panel_seat"
_DIMENSIONS = {
    "gap": ("Seam Gap", 0.15),
    "cover_thickness": ("Cover Thickness at Datum", 0.6),
    "seat_depth": ("Seat Depth at Datum", 0.6),
    "ledge_width": ("Seat Ledge Width", 0.4),
    "floor_drop": ("Drop Below Ledge", 0.25),
    "base_thickness": ("Base Thickness", 0.6),
}


def catalog():
    dimensions = {
        key: {
            "type": "number", "label": label, "default": default,
            "description": "Positive dimension in the guide's applied Blender units",
        }
        for key, (label, default) in _DIMENSIONS.items()
    }
    return {
        TOOL: {
            "label": "Create Cover and Fixed Seat",
            "required": ["selector", "cover", "base", *_DIMENSIONS],
            "optional": ["collection"], "read_only": False, "parameters": dimensions,
            "description": "Split a single open local-Z heightfield at one convex interior face boundary. "
                           "Save both closed assembly parts while retaining the input as a guide.",
        },
        RESIZE: {
            "label": "Resize Cover and Fixed Seat",
            "required": ["cover", "base"], "optional": list(_DIMENSIONS),
            "read_only": False, "parameters": dimensions,
            "description": "Update at least one dimension from the same guide. Keep both object identities.",
        },
    }


def _digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _signature(mesh, generated=False):
    data = [
        [tuple(vertex.co) for vertex in mesh.vertices],
        [tuple(edge.vertices) for edge in mesh.edges],
        [tuple(face.vertices) for face in mesh.polygons],
    ]
    if generated:
        data.extend([
            [material.name if material else None for material in mesh.materials],
            [(face.material_index, face.use_smooth) for face in mesh.polygons],
            [(attribute.name, attribute.domain, attribute.data_type) for attribute in mesh.attributes
             if not attribute.name.startswith((".hide_", ".select_"))],
            sorted(mesh.keys()),
        ])
    return _digest(data)


def _dimensions(values, epsilon):
    for key in _DIMENSIONS:
        number = values[key]
        if type(number) not in (int, float) or not epsilon < number <= 1e6:
            raise ValueError(f"{key} must be above coordinate precision and at most 1,000,000 BU")
    if values["ledge_width"] - values["gap"] <= epsilon:
        raise ValueError("ledge_width must exceed gap so the cover overlaps the ledge")
    if values["seat_depth"] < values["cover_thickness"]:
        raise ValueError("seat_depth must reach at least cover_thickness")
    return {key: float(values[key]) for key in _DIMENSIONS}


def _scene_mesh(obj):
    return (
        obj is not None and obj.type == "MESH" and obj.mode == "OBJECT"
        and bpy.context.view_layer.objects.get(obj.name) == obj
    )


def _guide(obj, creating):
    if not _scene_mesh(obj) or (creating and not obj.visible_get()) or obj.modifiers or obj.data.shape_keys:
        raise ValueError("Choose an Object Mode mesh guide without modifiers or shape keys")
    if not all(math.isfinite(value) for row in obj.matrix_world for value in row):
        raise ValueError("Guide transform must be finite")
    matrix = obj.matrix_world.to_3x3()
    axes = matrix.col
    if abs(matrix.determinant() - 1) > 1e-5 or any(
        abs(axes[i].dot(axes[j]) - float(i == j)) > 1e-5 for i in range(3) for j in range(3)
    ):
        raise ValueError("Apply guide scale and remove shear or reflection before measuring dimensions")
    mesh = obj.data
    if not mesh.polygons or len(mesh.polygons) > 20000 or len(mesh.vertices) > 50000:
        raise ValueError("Use a control cage with 1–20,000 faces and at most 50,000 vertices")
    points = [vertex.co.copy() for vertex in mesh.vertices]
    scale = max(abs(value) for point in points for value in point)
    if not math.isfinite(scale) or not all(math.isfinite(v) for point in points for v in point):
        raise ValueError("Guide coordinates must be finite")
    epsilon = max(1e-7, 3e-7 * scale)
    return mesh, points, epsilon


def _boundary(faces, label):
    """Recover one directed disk boundary and reject loose or pinched topology."""
    edges = defaultdict(list)
    touches = defaultdict(set)
    for face_index, face in enumerate(faces):
        if len(face) < 3 or len(set(face)) != len(face):
            raise ValueError(f"{label} contains a degenerate face")
        for first, second in zip(face, (*face[1:], face[0])):
            edges[tuple(sorted((first, second)))].append((first, second, face_index))
            touches[first].add(face_index)
    adjacent = defaultdict(set)
    fans = defaultdict(set)
    following, arriving = {}, set()
    for uses in edges.values():
        first, second, face_index = uses[0]
        if len(uses) == 1:
            if first in following or second in arriving:
                raise ValueError(f"{label} boundary branches at a vertex")
            following[first] = second
            arriving.add(second)
        elif len(uses) == 2 and uses[1][:2] == (second, first):
            neighbor = uses[1][2]
            adjacent[face_index].add(neighbor)
            adjacent[neighbor].add(face_index)
            for vertex in (first, second):
                fans[vertex, face_index].add(neighbor)
                fans[vertex, neighbor].add(face_index)
        else:
            raise ValueError(f"{label} has nonmanifold or inconsistently wound edges")
    if not following or set(following) != arriving or len(following) > 1024:
        raise ValueError(f"{label} needs one closed boundary of at most 1024 edges")

    def connected(start, neighbors):
        visited, pending = set(), [start]
        while pending:
            current = pending.pop()
            if current not in visited:
                visited.add(current)
                pending.extend(neighbors(current) - visited)
        return visited

    if len(touches) - len(edges) + len(faces) != 1 or len(connected(0, adjacent.__getitem__)) != len(faces):
        raise ValueError(f"{label} must be a connected disk")
    for vertex, faces_at_vertex in touches.items():
        if len(connected(next(iter(faces_at_vertex)), lambda face: fans[vertex, face])) != len(faces_at_vertex):
            raise ValueError(f"{label} has a disconnected face fan")
    start = min(following)
    ordered = [start]
    while (next_index := following[ordered[-1]]) != start:
        ordered.append(next_index)
    if len(ordered) != len(following):
        raise ValueError(f"{label} must have a single boundary")
    return ordered, set(touches), set(edges)


def _cross(a, b, c):
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)


def _edge_normal(first, second, epsilon):
    direction = second - first
    if direction.length <= epsilon:
        raise ValueError("A projected boundary edge is too short")
    return Vector((-direction.y, direction.x)) / direction.length


def _ring_edges(ring, epsilon):
    return [(first, second, _edge_normal(first, second, epsilon))
            for first, second in zip(ring, (*ring[1:], ring[0]))]


def _simple_outline(ring, epsilon):
    edges = _ring_edges(ring, epsilon)
    for index, (a, b, normal) in enumerate(edges):
        for c, d, other_normal in edges[index + 2: -1 if index == 0 else None]:
            c_side, d_side = normal.dot(c - a), normal.dot(d - a)
            a_side, b_side = other_normal.dot(a - c), other_normal.dot(b - c)
            if (
                min(c_side, d_side) <= epsilon and max(c_side, d_side) >= -epsilon
                and min(a_side, b_side) <= epsilon and max(a_side, b_side) >= -epsilon
                and all(max(a[u], b[u]) >= min(c[u], d[u]) - epsilon
                        and max(c[u], d[u]) >= min(a[u], b[u]) - epsilon for u in range(2))
            ):
                raise ValueError("Guide outline crosses or touches itself in local XY")


def _convex_outline(ring, epsilon):
    edges = _ring_edges(ring, epsilon)
    if any(normal.dot(point - start) < -epsilon for start, _, normal in edges for point in ring):
        raise ValueError("The opening needs one convex boundary in local XY")
    return edges


def _inset(ring, distance, epsilon):
    edges = _convex_outline(ring, epsilon)
    result = []
    for index, point in enumerate(ring):
        previous, current = edges[index - 1][2], edges[index][2]
        denominator = 1 + previous.dot(current)
        if denominator <= 1e-7:
            raise ValueError("A seam corner cannot be inset reliably")
        result.append(point + (previous + current) * (distance / denominator))
    _convex_outline(result, epsilon)
    if any((result[(index + 1) % len(ring)] - point).dot(end - start) <= epsilon * (end - start).length
           for index, (point, (start, end, _)) in enumerate(zip(result, edges))):
        raise ValueError("The requested width consumes a projected seam edge")
    if any(normal.dot(point - start) < distance - 3 * epsilon
           for start, _, normal in edges for point in result):
        raise ValueError("The requested width collapses the opening")
    return result


def _relocate_cover(points, indices, boundary, opening, cover, epsilon):
    destination = dict(zip(boundary, cover))
    origin = sum(cover, Vector((0, 0))) / len(cover)
    opening_edges = _ring_edges(opening, epsilon)
    for index in indices - set(boundary):
        direction = points[index].xy - origin
        if direction.length <= epsilon:
            destination[index] = points[index].xy
            continue
        limit, hit_edge = min(
            (normal.dot(origin - start) / -normal.dot(direction), edge_index)
            for edge_index, (start, end, normal) in enumerate(opening_edges)
            if normal.dot(direction) < -1e-12
        )
        start, end, _ = opening_edges[hit_edge]
        edge = end - start
        fraction = ((origin + direction * limit) - start).dot(edge) / edge.length_squared
        new_boundary = cover[hit_edge].lerp(cover[(hit_edge + 1) % len(cover)], fraction)
        destination[index] = origin.lerp(new_boundary, 1 / limit)
    return destination


class _Geometry:
    def __init__(self):
        self.points = []
        self.faces = []

    def add(self, points):
        start = len(self.points)
        self.points.extend(points)
        return list(range(start, len(self.points)))

    def ring(self, points, z):
        return self.add([(point.x, point.y, z) for point in points])

    def band(self, outer, inner, flip=False):
        for index, first in enumerate(outer):
            following = (index + 1) % len(outer)
            corners = (first, outer[following], inner[following], inner[index])
            self.faces.append(corners[::-1] if flip else corners)


def _build_parts(mesh, points, selected, params, epsilon):
    faces = [tuple(face.vertices) for face in mesh.polygons]
    outer, vertices, guide_edges = _boundary(faces, "Guide")
    if len(vertices) != len(points) or len(guide_edges) != len(mesh.edges):
        raise ValueError("Remove loose geometry from the guide")
    kept_faces = [face for index, face in enumerate(faces) if index not in selected]
    panel_faces = [faces[index] for index in sorted(selected)]
    seam, panel_vertices, _ = _boundary(panel_faces, "Selected patch")
    if set(seam).intersection(outer) or not kept_faces:
        raise ValueError("The selected patch must lie entirely inside the guide")
    if len(seam) * len(panel_vertices) > 5_000_000:
        raise ValueError("Reduce the selected cage density or use a shorter boundary")
    _simple_outline([points[index].xy for index in outer], epsilon)
    opening = [points[index].xy for index in seam]
    cover_perimeter = _inset(opening, params["gap"], epsilon)
    floor_perimeter = _inset(opening, params["ledge_width"], epsilon)

    mesh.calc_loop_triangles()
    panel_triangles = []
    for triangle in mesh.loop_triangles:
        a, b, c = (points[index] for index in triangle.vertices)
        normal = (b - a).cross(c - a)
        if normal.z <= 2 * epsilon * max((b - a).length, (c - a).length) or normal.z < 0.5 * normal.length:
            raise ValueError("Guide triangles must face local +Z without folds or steep walls")
        if triangle.polygon_index in selected:
            panel_triangles.append(tuple(triangle.vertices))

    coords = _relocate_cover(points, panel_vertices, seam, opening, cover_perimeter, epsilon)
    for triangle in panel_triangles:
        a, b, c = (coords[index] for index in triangle)
        if _cross(a, b, c) <= 2 * epsilon * max((b - a).length, (c - a).length):
            raise ValueError("Inset folds a cover cell. Reduce the gap or re-space the control cage")
    lookup = BVHTree.FromPolygons(points, panel_triangles, all_triangles=True)
    region_low = min(points[index].z for index in panel_vertices)
    origin_z = max(point.z for point in points) + max(1, abs(region_low))
    depth = region_low - params["seat_depth"]
    floor = depth - params["floor_drop"]
    bottom = min(floor, min(point.z for point in points)) - params["base_thickness"]
    cover_bottom = region_low - params["cover_thickness"]

    cover = _Geometry()
    cover_indices = sorted(panel_vertices)
    coordinates = []
    for index in cover_indices:
        point = coords[index]
        hit, _, _, _ = lookup.ray_cast(Vector((point.x, point.y, origin_z)), Vector((0, 0, -1)))
        if hit is None or hit.z < region_low - epsilon:
            raise ValueError("An inset cover sample falls outside the guide patch")
        coordinates.append((point.x, point.y, hit.z))
    top_ids = dict(zip(cover_indices, cover.add(coordinates)))
    cover.faces.extend(tuple(top_ids[index] for index in face) for face in panel_faces)
    underside = cover.ring(cover_perimeter, cover_bottom)
    cover.band([top_ids[index] for index in seam], underside, flip=True)
    cover.faces.append(tuple(reversed(underside)))

    base = _Geometry()
    exterior_indices = sorted({index for face in kept_faces for index in face})
    exterior_ids = dict(zip(exterior_indices, base.add(points[index] for index in exterior_indices)))
    base.faces.extend(tuple(exterior_ids[index] for index in face) for face in kept_faces)
    ledge_outer = base.ring(opening, depth)
    ledge_inner = base.ring(floor_perimeter, depth)
    floor_ring = base.ring(floor_perimeter, floor)
    base.band([exterior_ids[index] for index in seam], ledge_outer)
    base.band(ledge_outer, ledge_inner)
    base.band(ledge_inner, floor_ring)
    base.faces.append(tuple(floor_ring))
    bottom_ids = base.add([(point.x, point.y, bottom) for point in points])
    base.band([exterior_ids[index] for index in outer], [bottom_ids[index] for index in outer], flip=True)
    base.faces.extend(tuple(bottom_ids[index] for index in reversed(face)) for face in faces)
    return cover, base, seam, region_low


def _make_mesh(name, geometry, epsilon):
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        mesh.from_pydata(geometry.points, [], geometry.faces, shade_flat=True)
        if mesh.validate():
            raise ValueError("Generated mesh required topology repair")
        mesh.update()
        bm.from_mesh(mesh)
        if (
            any(not edge.is_manifold or not edge.is_contiguous for edge in bm.edges)
            or any(not vertex.is_manifold for vertex in bm.verts)
            or any(not math.isfinite(face.calc_area()) or face.calc_area() <= epsilon ** 2 for face in bm.faces)
            or not math.isfinite(volume := bm.calc_volume(signed=True)) or volume <= epsilon ** 3
        ):
            raise ValueError("Generated mesh is not a positive closed solid")
        return mesh
    except Exception:
        bpy.data.meshes.remove(mesh)
        raise
    finally:
        bm.free()


def _seat_record(item):
    try:
        data = json.loads(item[_META])
        required = {"schema", "assembly", "role", "guide", "guide_signature", "selector", "dimensions", "signature"}
        if not isinstance(data, dict) or required - data.keys() or data["schema"] != 1:
            raise ValueError
        if not isinstance(data["dimensions"], dict) or data["dimensions"].keys() != _DIMENSIONS.keys():
            raise ValueError
        return data
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ValueError("Choose managed cover and base objects from the same seat") from None


def _managed_part(name, role):
    item = bpy.data.objects.get(name) if isinstance(name, str) else None
    if not _scene_mesh(item) or item.library or item.override_library or not item.is_editable:
        raise ValueError(f"{role} must be an editable mesh object in the active view layer")
    mesh = item.data
    metadata = _seat_record(item)
    if (
        metadata["role"] != role or mesh.users != 1 or mesh.library or not mesh.is_editable
        or mesh.shape_keys or mesh.has_custom_normals or mesh.animation_data
        or item.modifiers or item.vertex_groups
        or _signature(mesh, generated=True) != metadata["signature"]
    ):
        raise ValueError(f"{role} was edited or shared. Create a new seat from the guide instead")
    return item, metadata


def execute(obj, tool, params):
    """Create new assembly parts or replace two verified generated meshes."""
    if tool not in TOOLS or not isinstance(params, dict):
        raise ValueError("Pass a panel seat tool ID and a parameter object")
    description = catalog()[tool]
    required = set(description["required"])
    if required - params.keys() or params.keys() - required - set(description["optional"]):
        raise ValueError(f"{tool} received missing or unexpected fields")
    mesh, points, epsilon = _guide(obj, creating=tool == TOOL)
    signature = _signature(mesh)
    current = {}
    collection = None
    if tool == TOOL:
        names = [shell_tools._new_name(params[key]) for key in ("cover", "base")]
        if names[0] == names[1]:
            raise ValueError("Cover and base need distinct object names")
        collection = shell_tools._destination(obj, params.get("collection"))
        selector = params["selector"]
        size = _dimensions(params, epsilon)
        assembly = uuid.uuid4().hex
    else:
        if not set(params).intersection(_DIMENSIONS):
            raise ValueError("Specify at least one dimension to resize")
        cover, cover_meta = _managed_part(params["cover"], "cover")
        base, base_meta = _managed_part(params["base"], "base")
        if (
            cover_meta["assembly"] != base_meta["assembly"] or cover_meta["dimensions"] != base_meta["dimensions"]
            or cover_meta["selector"] != base_meta["selector"]
            or any(record["guide"] != obj.name or record["guide_signature"] != signature
                   for record in (cover_meta, base_meta))
        ):
            raise ValueError("The selected assembly or its guide changed")
        current = {cover: cover[_META], base: base[_META]}
        names = [cover.name, base.name]
        selector = cover_meta["selector"]
        size = _dimensions({**cover_meta["dimensions"], **params}, epsilon)
        assembly = cover_meta["assembly"]

    selected = set(shell_tools._selected_faces(obj, selector))
    cover_shape, base_shape, seam, datum = _build_parts(mesh, points, selected, size, epsilon)
    meshes, created, swapped = [], [], []
    try:
        for name, geometry in zip(names, (cover_shape, base_shape)):
            meshes.append(_make_mesh(name, geometry, epsilon))
        metadata = {
            "schema": 1, "assembly": assembly, "guide": obj.name, "guide_signature": signature,
            "selector": selector, "dimensions": size,
        }
        parts = []
        for role, name, data in zip(("cover", "base"), names, meshes):
            if tool == TOOL:
                item = bpy.data.objects.new(name, data)
                created.append(item)
                collection.objects.link(item)
                item.matrix_world = obj.matrix_world
            else:
                item = bpy.data.objects[name]
                swapped.append((item, item.data))
                item.data = data
            item[_META] = json.dumps({**metadata, "role": role, "signature": _signature(data, generated=True)})
            parts.append({"role": role, "object": item.name, "vertices": len(data.vertices),
                          "faces": len(data.polygons)})
    except Exception:
        for item in reversed(created):
            bpy.data.objects.remove(item, do_unlink=True)
        for item, previous in reversed(swapped):
            item.data = previous
            item[_META] = current[item]
        for data in meshes:
            bpy.data.meshes.remove(data)
        raise
    for item, previous in swapped:
        name = previous.name
        if previous.users == 0:
            bpy.data.meshes.remove(previous)
            item.data.name = name
    return {
        "tool": tool, "guide": obj.name, "assembly": assembly, "parts": parts,
        "seam_source_vertices": seam, "dimensions": size, "datum_local_z": datum,
        "assembly_axis_local": [0, 0, 1],
        "design_seat_clearance": size["seat_depth"] - size["cover_thickness"],
        "design_ledge_overlap": size["ledge_width"] - size["gap"],
        "construction_guide_exclude_from_assembly": obj.name,
    }


def _property_dimensions():
    return {
        key: FloatProperty(name=label, default=default, min=0.0, precision=4)
        for key, (label, default) in _DIMENSIONS.items()
    }


class HSW_OT_panel_seat(bpy.types.Operator):
    bl_idname = "hard_surface.shell_panel_seat"
    bl_label = "Create Cover and Fixed Seat"
    bl_options = {"REGISTER", "UNDO"}

    cover: StringProperty(name="Cover", default="Cover")
    base: StringProperty(name="Fixed Seat", default="Fixed Seat")
    collection: StringProperty(name="Collection")
    __annotations__.update(_property_dimensions())

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == "MESH" and context.mode == "OBJECT"

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        try:
            params = {
                "cover": self.cover, "base": self.base, "selector": shell_tools._active_faces(context.object),
                **{key: getattr(self, key) for key in _DIMENSIONS},
            }
            if self.collection:
                params["collection"] = self.collection
            execute(context.object, TOOL, params)
        except (TypeError, ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class HSW_OT_panel_seat_resize(bpy.types.Operator):
    bl_idname = "hard_surface.shell_panel_seat_resize"
    bl_label = "Resize Cover and Fixed Seat"
    bl_options = {"REGISTER", "UNDO"}

    guide: StringProperty(options={"HIDDEN"})
    cover: StringProperty(options={"HIDDEN"})
    base: StringProperty(options={"HIDDEN"})
    __annotations__.update(_property_dimensions())

    @classmethod
    def poll(cls, context):
        return context.object is not None and _META in context.object and context.mode == "OBJECT"

    def invoke(self, context, _event):
        try:
            data = _seat_record(context.object)
            parts = defaultdict(list)
            for item in context.view_layer.objects:
                if _META in item:
                    try:
                        record = _seat_record(item)
                    except ValueError:
                        continue
                    if record["assembly"] == data["assembly"]:
                        parts[record["role"]].append(item.name)
            if len(parts["cover"]) != 1 or len(parts["base"]) != 1:
                raise ValueError("Choose an assembly with one managed cover and one managed base")
            self.guide, self.cover, self.base = data["guide"], parts["cover"][0], parts["base"][0]
            for key, value in data["dimensions"].items():
                setattr(self, key, value)
        except (KeyError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, _context):
        try:
            execute(
                bpy.data.objects.get(self.guide), RESIZE,
                {"cover": self.cover, "base": self.base, **{key: getattr(self, key) for key in _DIMENSIONS}},
            )
        except (TypeError, ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


def register():
    bpy.utils.register_class(HSW_OT_panel_seat)
    bpy.utils.register_class(HSW_OT_panel_seat_resize)


def unregister():
    bpy.utils.unregister_class(HSW_OT_panel_seat_resize)
    bpy.utils.unregister_class(HSW_OT_panel_seat)
