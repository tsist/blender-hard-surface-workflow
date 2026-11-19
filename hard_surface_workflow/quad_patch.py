"""Rebuild one explicitly marked quadrilateral face patch in a working mesh."""

from copy import deepcopy
from math import isfinite

import bmesh
from mathutils.bvhtree import BVHTree

from .mesh_tools import _edit_selection, _parameters, definition, index_array


TOOL = "patch.quad_grid"
MAX_PATCH_CELLS = 4096
TOOLS = {TOOL: definition(
    TOOL, "四边面补片",
    "保留原边界，用四边 Coons 插值替换单一连通盘形选区。对边分段数一致。",
    ("FACE",), True,
    {"corners": {**index_array("四个原拓扑边界角点的顶点索引，顺序不限", 4, required=True), "minItems": 4}},
)}
TOOLS[TOOL]["repeat"] = "一次替换所选盘形区域。拓扑改变后按新索引重新定位。"

_PLAIN_ATTRIBUTES = {
    "position", ".edge_verts", ".corner_vert", ".corner_edge", ".select_vert", ".select_edge", ".select_poly",
    ".hide_vert", ".hide_edge", ".hide_poly", "material_index", "sharp_face", "sharp_edge", "uv_seam",
}


def describe(tool):
    if tool not in TOOLS:
        raise ValueError(f"Unknown patch tool: {tool!r}")
    return deepcopy(TOOLS[tool])


def _boundary(selected):
    edges = {edge for face in selected for edge in face.edges}
    vertices = {vertex for edge in edges for vertex in edge.verts}
    if any(edge.hide or len(edge.link_faces) > 2 for edge in edges) or any(vertex.hide for vertex in vertices):
        raise ValueError("Patch contains hidden elements or an edge with more than two faces")
    rim = {edge for edge in edges if sum(face in selected for face in edge.link_faces) == 1}
    pending = [next(iter(selected))]
    seen = set(pending)
    while pending:
        face = pending.pop()
        for edge in face.edges:
            for neighbor in edge.link_faces:
                if neighbor in selected and neighbor not in seen:
                    seen.add(neighbor)
                    pending.append(neighbor)
    if seen != selected or len(vertices) - len(edges) + len(selected) != 1:
        raise ValueError("Select one connected disk of faces without holes")
    boundary = {}
    for edge in rim:
        for vertex in edge.verts:
            boundary.setdefault(vertex, []).append(edge)
    if not boundary or any(len(value) != 2 for value in boundary.values()):
        raise ValueError("The selected faces must have one simple closed boundary")
    interior = vertices - boundary.keys()
    if any(not vertex.is_manifold or any(face not in selected for face in vertex.link_faces) for vertex in interior):
        raise ValueError("Patch interior has ambiguous or external vertex connections")
    return boundary, edges - rim, interior


def _walk_rails(boundary, corners):
    lookup = {vertex.index: vertex for vertex in boundary}
    if len(corners) != 4 or len(set(corners)) != 4 or any(index not in lookup for index in corners):
        raise ValueError("Name exactly four distinct corners on the selection boundary")
    first = lookup[corners[0]]
    ordered = [first]
    used = {first}
    leg_edges = []
    previous, vertex = None, first
    while True:
        edge = next(edge for edge in boundary[vertex] if edge != previous)
        leg_edges.append(edge)
        vertex = edge.other_vert(vertex)
        if vertex == first:
            break
        if vertex in used:
            raise ValueError("Boundary crosses itself topologically")
        used.add(vertex)
        ordered.append(vertex)
        previous = edge
    if used != set(boundary):
        raise ValueError("Patch has more than one boundary loop")
    breaks = [i for i, vertex in enumerate(ordered) if vertex.index in corners] + [len(ordered)]
    closed = ordered + [first]
    sides = [closed[start:stop+1] for start, stop in zip(breaks, breaks[1:])]
    segments = [len(side)-1 for side in sides]
    if segments[0] != segments[2] or segments[1] != segments[3]:
        raise ValueError(f"Opposite boundary sides require equal segment counts, observed {segments}")
    if segments[0]*segments[1] > MAX_PATCH_CELLS:
        raise ValueError(f"Quad grid would exceed the {MAX_PATCH_CELLS} cell work limit")
    rails = (sides[0], sides[1], list(reversed(sides[2])), list(reversed(sides[3])))
    return rails, ordered, leg_edges, segments


def _orientation(selected, boundary, edges):
    outside, inside = set(), set()
    for vertex, edge in zip(boundary, edges):
        other = edge.other_vert(vertex)
        for face in edge.link_faces:
            forward = next(loop.link_loop_next.vert == other for loop in face.loops if loop.vert == vertex)
            if face in selected:
                inside.add(forward)
            else:
                outside.add(forward)
    if len(outside) > 1 or not outside and len(inside) != 1:
        raise ValueError("Cannot infer a consistent patch winding from the boundary")
    return next(iter(outside)) if outside else not next(iter(inside))


def _check_attributes(obj, mesh, selected, interior, interior_edges):
    if obj.data.uv_layers or obj.data.has_custom_normals:
        raise ValueError("Patch reconstruction requires a mesh without UV or custom normal data")
    attributes = [attr.name for attr in obj.data.attributes
                  if attr.name not in _PLAIN_ATTRIBUTES and not attr.name.startswith("hs_id_")]
    if attributes:
        raise ValueError(f"Patch reconstruction would discard custom mesh attributes: {', '.join(attributes)}")
    if any(edge.seam or not edge.smooth for edge in interior_edges):
        raise ValueError("Repair interior contains seam or sharp-edge markings")
    layers = mesh.verts.layers.deform.active
    if layers is not None and any(vertex[layers].items() for vertex in interior):
        raise ValueError("Interior vertices carry vertex-group weights")
    face_properties = {(face.material_index, face.smooth) for face in selected}
    if len(face_properties) != 1:
        raise ValueError("Selected faces must have one material and smoothing setting")
    return next(iter(face_properties))


def _coons(rails):
    top, right, bottom, left = [[vertex.co for vertex in rail] for rail in rails]
    rows, cols = len(left), len(top)
    points = []
    for row in range(rows):
        v = row / (rows - 1)
        for column in range(cols):
            u = column / (cols - 1)
            corners = top[0].lerp(top[-1], u).lerp(bottom[0].lerp(bottom[-1], u), v)
            point = top[column].lerp(bottom[column], v) + left[row].lerp(right[row], u) - corners
            if row == 0 or row == rows - 1:
                point = (top if row == 0 else bottom)[column]
            elif column == 0 or column == cols - 1:
                point = (left if column == 0 else right)[row]
            points.append(point.copy())
    return points, rows, cols


def _validate_surface(points, rows, cols):
    triangles = []
    for row in range(rows - 1):
        for column in range(cols - 1):
            a = row*cols + column
            b, c, d = a+1, a+cols+1, a+cols
            face_a = (points[b]-points[a]).cross(points[c]-points[a])
            face_b = (points[c]-points[a]).cross(points[d]-points[a])
            if min(face_a.length_squared, face_b.length_squared) <= 1e-26 or face_a.dot(face_b) <= 0:
                raise ValueError("A rebuilt cell would be degenerate, concave or folded")
            triangles.extend(((a, b, c), (a, c, d)))
    if any(not all(isfinite(coordinate) for coordinate in point) for point in points):
        raise ValueError("The Coons surface contains a non-finite coordinate")
    tree = BVHTree.FromPolygons(points, triangles, all_triangles=True)
    if any(not set(triangles[a]).intersection(triangles[b]) for a, b in tree.overlap(tree)):
        raise ValueError("The rebuilt grid would self-intersect")


def _replace(mesh, selected, interior_edges, interior, rails, points, rows, cols, reverse, material, smooth):
    top, right, bottom, left = rails
    for face in selected:
        mesh.faces.remove(face)
    for edge in interior_edges:
        mesh.edges.remove(edge)
    for vertex in interior:
        if vertex.link_edges:
            raise ValueError("Old interior vertex retained an unexpected connection")
        mesh.verts.remove(vertex)
    mesh.select_history.clear()
    grid = []
    for row in range(rows):
        if row in (0, rows-1):
            grid.append(top if row == 0 else bottom)
            continue
        middle = [mesh.verts.new(points[row*cols+column]) for column in range(1, cols-1)]
        grid.append([left[row], *middle, right[row]])
    rebuilt = []
    for i in range(rows-1):
        for j in range(cols-1):
            corners = [grid[i][j], grid[i][j+1], grid[i+1][j+1], grid[i+1][j]]
            face = mesh.faces.new(corners[::-1] if reverse else corners)
            face.material_index = material
            face.smooth = smooth
            face.select_set(True)
            rebuilt.append(face)
    if any(not vert.link_faces for ring in grid for vert in ring):
        raise ValueError("Rebuilt quad grid produced an unconnected vertex")
    mesh.normal_update()
    return rebuilt


def run(tool, params=None, *, allow_expansion=False, protected_indices=()):
    """Call on the live scratch edit mesh. The shared core owns commit and rollback."""
    settings = _parameters(describe(tool), params)
    obj, mesh, _, faces = _edit_selection(TOOLS[tool])
    selected = set(faces)
    if not selected:
        raise ValueError("Select the faces authorized for replacement")
    boundary, interior_edges, interior = _boundary(selected)
    if any(vertex.index in protected_indices for vertex in interior):
        raise ValueError("A protected interior vertex would be removed")
    material, smooth = _check_attributes(obj, mesh, selected, interior, interior_edges)
    rails, ordered, edges, segments = _walk_rails(boundary, settings["corners"])
    reverse = _orientation(selected, ordered, edges)
    points, rows, cols = _coons(rails)
    _validate_surface(points, rows, cols)
    old_faces, old_interior = len(selected), len(interior)
    exact_boundary = [(vertex, vertex.co.copy(), len(edge.link_faces)) for vertex, edge in zip(ordered, edges)]
    rebuilt = _replace(mesh, selected, interior_edges, interior, rails, points, rows, cols, reverse, material, smooth)
    for (vertex, source, adjacency), edge in zip(exact_boundary, edges):
        if vertex.co != source or not edge.is_valid or len(edge.link_faces) != adjacency or (
            adjacency == 2 and not edge.is_contiguous
        ):
            raise ValueError("Boundary identity, position, or surrounding-face winding changed")
    mesh.select_flush_mode()
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
    return {"tool": tool, "status": "FINISHED", "removed_faces": old_faces,
            "removed_interior_vertices": old_interior, "new_quad_faces": len(rebuilt),
            "new_interior_vertices": (rows-2)*(cols-2),
            "boundary_segments": segments, "fixed_boundary_vertices": len(ordered), "winding_check": "pass"}


def register():
    """The shared panel discovers this provider through its catalog."""


def unregister():
    """This provider owns no RNA classes."""
