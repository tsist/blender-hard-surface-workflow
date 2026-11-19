"""Explicit local repairs for applied hard-surface geometry."""

from math import atan2, cos, hypot, isfinite, pi, tan

import bmesh
from mathutils import Vector
from mathutils.geometry import intersect_line_line

from . import core, selection


REFIT = "fillet_strip_refit"
CLEANUP = "boolean_seam_cleanup"
MAX_REPAIR_VERTICES = 4096
PLAIN_ATTRIBUTES = {
    "position", ".edge_verts", ".corner_vert", ".corner_edge", ".select_vert",
    ".select_edge", ".select_poly", ".hide_vert", ".hide_edge", ".hide_poly",
    "material_index", "sharp_face", "sharp_edge", "uv_seam",
}
REQUIRED = {
    REFIT: ("topology_sha256", "sections", "width"),
    CLEANUP: ("topology_sha256", "pairs", "max_distance"),
}
OPTIONAL = {
    REFIT: ("protected_vertices", "circular_axis"),
    CLEANUP: ("protected_vertices",),
}


def catalog():
    return {
        REFIT: {
            "label": "Refit Applied Fillet Strip",
            "description": "Refit a straight fillet or a closed circular hole rim from ordered cross-sections and both support faces. Width sets the tangent setback and keeps the existing segment count.",
            "required": list(REQUIRED[REFIT]),
            "optional": list(OPTIONAL[REFIT]),
            "read_only": False,
            "parameters": {
                "topology_sha256": "Topology fingerprint of the source mesh from model inspect --flow-selection",
                "sections": "In axial order, [{arc: [side-A-through-side-B vertex indices], outer: [adjacent side-A vertex, adjacent side-B vertex]}]. At least two stations, using the same arc segment count.",
                "width": "Positive object-local distance on each support plane, from the virtual corner to the new tangent point",
                "protected_vertices": "Optional vertex indices that may not move",
                "circular_axis": "Optional {origin: local xyz BU, direction: local xyz} for a closed planar-panel to coaxial inner-cylinder rim. Supply a full ring of at least six stations without duplicating the first, with arc and outer points ordered panel side first.",
            },
        },
        CLEANUP: {
            "label": "Clean Applied Boolean Seam",
            "description": "Collapse explicitly paired short seam edges toward one retained side. The retained coordinates and surviving face winding are verified.",
            "required": list(REQUIRED[CLEANUP]),
            "optional": list(OPTIONAL[CLEANUP]),
            "read_only": False,
            "parameters": {
                "topology_sha256": "Topology fingerprint of the source mesh from model inspect --flow-selection",
                "pairs": "Ordered [[keep, remove], ...] across a seam. Both sides must form connected edge chains and pairs must form actual manifold edges.",
                "max_distance": "Positive maximum length of each collapsed edge in object-local units",
                "protected_vertices": "Optional vertex indices that may not be removed",
            },
        },
    }


def _positive(value, label):
    if type(value) not in {int, float} or not 0 < value < float("inf"):
        raise ValueError(f"{label} must be positive and finite")
    return value


def _vertices(mesh, indices, length, label):
    if not isinstance(indices, (list, tuple)) or not length[0] <= len(indices) <= length[1]:
        raise ValueError(f"{label} expects between {length[0]} and {length[1]} vertex indices")
    if any(type(index) is not int or not 0 <= index < len(mesh.verts) for index in indices):
        raise ValueError(f"Invalid vertex index in {label}")
    if len(set(indices)) != len(indices):
        raise ValueError(f"Repeated vertex index in {label}")
    vertices = [mesh.verts[index] for index in indices]
    if any(vertex.hide or not all(map(isfinite, vertex.co)) for vertex in vertices):
        raise ValueError(f"{label} contains a hidden or non-finite vertex")
    return vertices


def _edge(mesh, first, second):
    edge = mesh.edges.get((first, second))
    if edge is None or edge.hide:
        raise ValueError(f"Expected a visible connecting edge at {first.index}, {second.index}")
    return edge


def _quad(mesh, corners):
    face = mesh.faces.get(corners)
    if face is None or face.hide or len(face.verts) != 4:
        raise ValueError("The fillet and its support rows must form a connected quad strip")
    if any(len(edge.link_faces) > 2 or (len(edge.link_faces) == 2 and not edge.is_contiguous)
           for edge in face.edges):
        raise ValueError("Fillet strip contains an edge with ambiguous face winding")
    return face


def _validate_triangles(mesh, changed_faces):
    for triangle in mesh.calc_loop_triangles():
        face = triangle[0].face
        if face in changed_faces:
            a, b, c = (loop.vert.co for loop in triangle)
            if (b - a).cross(c - a).dot(face.normal) <= 1e-14:
                raise ValueError("A changed face would contain a degenerate or reversed triangle")


def _cross_section(arc, outer):
    a, b = arc[0].co, arc[-1].co
    p, q = (vertex.co for vertex in outer)
    span = max((p - a).length, (q - b).length, (a - b).length)
    tolerance = max(span * 1e-5, 1e-8)
    intersection = intersect_line_line(a, p, b, q)
    if intersection is None or (intersection[0] - intersection[1]).length > tolerance:
        raise ValueError("The two support edges must intersect in a common cross-section plane")
    corner = (intersection[0] + intersection[1]) * 0.5
    first, last = a - corner, b - corner
    previous_width = first.length
    if min(previous_width, last.length) <= tolerance or abs(previous_width - last.length) > tolerance:
        raise ValueError("The existing circular strip must have equal setbacks on both sides")
    u, v = first.normalized(), last.normalized()
    angle = u.angle(v)
    if not 0.087 < angle < pi - 0.087 or (p - a).dot(u) <= tolerance or (q - b).dot(v) <= tolerance:
        raise ValueError("Give distinct support directions pointing away from the virtual corner")
    axis = u.cross(v).normalized()
    center = corner + (u + v).normalized() * (previous_width / cos(angle / 2))
    radial = (a - center).normalized()
    radius = previous_width * tan(angle / 2)
    last_turn = -1.0
    for vertex in arc:
        point = vertex.co - center
        if abs(point.dot(axis)) > tolerance or abs(point.length - radius) > tolerance:
            raise ValueError("Existing strip vertices must form a circular arc in the support plane")
        turn = radial.angle(point)
        if (turn <= last_turn or turn > pi - angle + 1e-4
                or radial.cross(point).dot(axis) > tolerance):
            raise ValueError("Existing arc vertices are not ordered from side A to side B")
        last_turn = turn
    return corner, u, v, angle, axis, tolerance, previous_width


def _axis_vector(values, label):
    if (not isinstance(values, (list, tuple)) or len(values) != 3
            or any(type(value) not in (int, float) or not -1e8 <= value <= 1e8 for value in values)):
        raise ValueError(f"Expected three finite local coordinates for circular_axis.{label}")
    return Vector(values)


def _circular_station(arc, outer, origin, axis):
    panel, wall = (vertex.co - origin for vertex in outer)
    top = panel.dot(axis)
    wall_height = wall.dot(axis)
    panel_tangent = arc[0].co - origin
    radial = panel_tangent - panel_tangent.dot(axis) * axis
    span = max((vertex.co - arc[0].co).length for vertex in arc + outer)
    radius = (wall - wall_height * axis).length
    epsilon = max(span * 1e-5, radius * 1e-6, 1e-8)
    if radius <= epsilon or radial.length <= epsilon or abs(top - wall_height) <= epsilon:
        raise ValueError("Circular rim needs a positive radius and distinct planar and cylindrical supports")
    radial.normalize()
    axial = axis if wall_height > top else -axis
    corner = origin + top * axis + radius * radial

    def profile(point):
        offset = point - corner
        r, h = offset.dot(radial), offset.dot(axial)
        if (offset - r * radial - h * axial).length > epsilon:
            raise ValueError("Circular rim stations must lie in radial planes of the supplied axis")
        return r, h

    panel_outer, panel_h = profile(outer[0].co)
    wall_r, wall_outer = profile(outer[1].co)
    positions = [profile(vertex.co) for vertex in arc]
    width_r, tangent_h = positions[0]
    tangent_r, width_h = positions[-1]
    if (min(width_r, width_h) <= epsilon or max(abs(panel_h), abs(wall_r), abs(tangent_h), abs(tangent_r)) > epsilon
            or panel_outer <= width_r + epsilon or wall_outer <= width_h + epsilon):
        raise ValueError("Order each circular section from the planar panel tangent to the inner cylindrical wall")
    tolerance = epsilon / min(width_r, width_h)
    previous_angle = -1.0
    for r, h in positions:
        u, v = (width_r - r) / width_r, (width_h - h) / width_h
        angle = atan2(u, v)
        if (abs(hypot(u, v) - 1) > tolerance or min(u, v) < -tolerance or angle <= previous_angle):
            raise ValueError("The applied rim must have a single ordered quarter-round section")
        previous_angle = angle
    return (corner, radial, axial, pi / 2, axis, epsilon, width_h), radius, top, width_r


def _circular_sections(sections, spec):
    if not isinstance(spec, dict) or set(spec) != {"origin", "direction"}:
        raise ValueError("circular_axis requires origin and direction")
    origin = _axis_vector(spec["origin"], "origin")
    axis = _axis_vector(spec["direction"], "direction")
    if axis.length <= 1e-10:
        raise ValueError("Circular rim axis direction must be nonzero")
    axis.normalize()
    stations = [_circular_station(arc, outer, origin, axis) for arc, outer in sections]
    row0, radius, top, width_r = stations[0]
    for row, current_radius, current_top, current_width in stations:
        tolerance = max(row0[5], row[5])
        if (max(abs(current_radius - radius), abs(current_top - top), abs(current_width - width_r),
                abs(row[-1] - row0[-1])) > tolerance or (row[2] - row0[2]).length > 1e-5):
            raise ValueError("Circular rim must be coplanar, coaxial and constant-width")

    directions = [row[1] for row, *_ in stations]
    sweeps = [atan2(a.cross(b).dot(axis), a.dot(b))
              for a, b in zip(directions, (*directions[1:], directions[0]))]
    if (min(abs(angle) for angle in sweeps) < 1e-5 or max(abs(angle) for angle in sweeps) >= pi / 2
            or any(angle * sweeps[0] <= 0 for angle in sweeps)
            or abs(abs(sum(sweeps)) - 2 * pi) > 1e-4):
        raise ValueError("Supply every circular station once, in a single full turn")

    # On regular facets a stock applied Bevel measures width perpendicular to
    # the side edge. The radial corner setback is longer by sec(half a sector).
    width_h = row0[-1]
    epsilon = row0[5]
    if abs(width_r - width_h) > epsilon:
        step = 2 * pi / len(sections)
        if (any(abs(abs(angle) - step) > 1e-5 for angle in sweeps)
                or abs(width_r - width_h / cos(step / 2)) > epsilon):
            raise ValueError("Expected equal rim setbacks or the stock Bevel setback on regular circular facets")
    return [item[0] for item in stations], {
        "circular_sectors": len(sections),
        "circular_hole_radius": radius,
        "previous_radial_setbacks": [item[3] for item in stations],
    }


def _straight_sections(sections):
    geometry = [_cross_section(arc, outer) for arc, outer in sections]
    origin, reference_u, reference_v, _, axis, tolerance, _ = geometry[0]
    if (geometry[-1][0] - origin).dot(axis) < 0:
        axis = -axis
    previous_station = float("-inf")
    for corner, u, v, _, _, current_tol, _ in geometry:
        eps = max(tolerance, current_tol)
        current = corner - origin
        station = current.dot(axis)
        if ((current - station * axis).length > eps or station - previous_station <= eps
                or (u - reference_u).length > 1e-4 or (v - reference_v).length > 1e-4):
            raise ValueError("Use consecutive perpendicular sections on one straight fillet axis")
        previous_station = station
    return geometry


def _refit(mesh, params, protected):
    width = _positive(params["width"], "width")
    source = params["sections"]
    circular = "circular_axis" in params
    minimum_sections = 6 if circular else 2
    if not isinstance(source, (list, tuple)) or not minimum_sections <= len(source) <= MAX_REPAIR_VERTICES // 5:
        if circular:
            raise ValueError("Provide one full ordered ring containing at least six sections")
        raise ValueError("Provide at least two ordered cross-sections")
    sections = []
    for row in source:
        if not isinstance(row, dict) or set(row) != {"arc", "outer"}:
            raise ValueError("Each cross-section requires arc and outer vertex lists")
        sections.append((_vertices(mesh, row["arc"], (3, 33), "arc"),
                         _vertices(mesh, row["outer"], (2, 2), "outer")))
    row_size = len(sections[0][0])
    all_vertices = [vertex for arc, outer in sections for vertex in arc + outer]
    if (len(all_vertices) > MAX_REPAIR_VERTICES or len(set(all_vertices)) != len(all_vertices)
            or any(len(arc) != row_size for arc, _ in sections)):
        raise ValueError("Sections must have equal segment counts, unique vertices, and fit the repair limit")
    strip_faces = set()
    support_faces = set()
    adjacent = sections[1:] + ([sections[0]] if circular else [])
    for (left, left_outer), (right, right_outer) in zip(sections, adjacent):
        for index in range(row_size - 1):
            strip_faces.add(_quad(mesh, [left[index], left[index + 1], right[index + 1], right[index]]))
        for column, side in ((0, 0), (-1, 1)):
            support_faces.add(_quad(mesh, [left[column], left_outer[side], right_outer[side], right[column]]))

    if circular:
        row_geometry, circular_report = _circular_sections(sections, params["circular_axis"])
    else:
        row_geometry = _straight_sections(sections)
        circular_report = {}
    changed_faces = strip_faces | support_faces
    new_coords = {}
    for row_number, ((arc, outer), section) in enumerate(zip(sections, row_geometry)):
        corner, u, v, angle, axis, current_tol, _ = section
        eps = max(row_geometry[0][5], current_tol)
        if width <= eps or any((outer[i].co - corner).dot(side) <= width + eps for i, side in enumerate((u, v))):
            raise ValueError("Requested setback would cross the adjacent support boundary or collapse the fillet")
        a, b = corner + width * u, corner + width * v
        center = corner + (u + v).normalized() * (width / cos(angle / 2))
        start, end = (a - center).normalized(), (b - center).normalized()
        radius = width * tan(angle / 2)
        for index, vertex in enumerate(arc):
            extra_faces = set(vertex.link_faces) - strip_faces - support_faces
            if extra_faces and (circular or row_number not in (0, len(sections) - 1) or any(
                face.hide or len(face.verts) > 128 or any(
                    abs((point.co - corner).dot(axis)) > eps for point in face.verts
                ) for face in extra_faces
            )):
                raise ValueError("A fillet vertex participates in geometry outside the strip or planar end caps")
            changed_faces.update(extra_faces)
            point = a if index == 0 else b if index == row_size - 1 else (
                center + radius * start.slerp(end, index / (row_size - 1))
            )
            if point != vertex.co and vertex in protected:
                raise ValueError("Requested width would move a protected vertex")
            if not all(map(isfinite, point)):
                raise ValueError("Rebuilt fillet would have a non-finite position")
            new_coords[vertex] = point

    normals = {face: face.normal.copy() for face in changed_faces}
    moved = sorted(vertex.index for vertex, point in new_coords.items() if vertex.co != point)
    for vertex, point in new_coords.items():
        vertex.co = point
    mesh.normal_update()
    if any(face.calc_area() <= 1e-14 or face.normal.dot(normal) <= 0 for face, normal in normals.items()):
        raise ValueError("Rebuilt strip or adjacent faces would fold or collapse")
    _validate_triangles(mesh, changed_faces)
    return {
        "width": width, "segments": row_size - 1, "stations": len(sections),
        "strip_faces": len(strip_faces), "side_faces": len(support_faces),
        "previous_widths": [row[-1] for row in row_geometry],
        "circular_radii": [width * tan(row[3] / 2) for row in row_geometry],
        "moved_vertices": moved,
        "protected_vertices": sorted(vertex.index for vertex in protected),
        **circular_report,
    }


def _cycle(indices):
    first = indices.index(min(indices))
    return tuple(indices[first:] + indices[:first])


def _face_properties(face):
    return face.material_index, face.smooth, face.hide


def _cleanup(obj, mesh, params, protected, old_index):
    maximum = _positive(params["max_distance"], "max_distance")
    raw = params["pairs"]
    if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= MAX_REPAIR_VERTICES // 2:
        raise ValueError("Provide one or more ordered seam vertex pairs")
    pairs = [_vertices(mesh, pair, (2, 2), "seam pair") for pair in raw]
    vertices = {vertex for pair in pairs for vertex in pair}
    if len(vertices) != 2 * len(pairs):
        raise ValueError("Each vertex may participate in only one seam pair")
    if obj.data.uv_layers or any(attribute.name not in PLAIN_ATTRIBUTES for attribute in obj.data.attributes):
        raise ValueError("Seam cleanup requires a mesh without UVs or custom mesh attributes")
    distances = []
    for keep, remove in pairs:
        edge = _edge(mesh, keep, remove)
        local = [edge for edge in remove.link_edges]
        if (remove in protected or not keep.is_manifold or not remove.is_manifold or not edge.is_manifold
                or any(link.hide or link.seam or not link.smooth or not link.is_contiguous for link in local)
                or any(face.hide for vertex in (keep, remove) for face in vertex.link_faces)):
            raise ValueError("A seam pair touches a protected, marked, hidden or nonmanifold element")
        distance = (keep.co - remove.co).length
        if distance > maximum:
            raise ValueError(f"Seam edge {keep.index}, {remove.index} exceeds max_distance")
        distances.append(distance)
    for (a, b), (c, d) in zip(pairs, pairs[1:]):
        _edge(mesh, a, c)
        _edge(mesh, b, d)

    deform = mesh.verts.layers.deform.active
    targets = {remove: keep for keep, remove in pairs}
    if deform is not None and any(remove[deform].items() for remove in targets):
        raise ValueError("Refusing to discard a seam vertex with vertex-group weights")
    remap = {remove.index: keep.index for remove, keep in targets.items()}
    retained_ids = set(remap.values())
    coordinates = {}
    for vertex in mesh.verts:
        vertex[old_index] = vertex.index
        if vertex not in targets:
            weights = dict(vertex[deform].items()) if deform is not None else {}
            coordinates[vertex.index] = (vertex.co.copy(), weights)

    # Match surviving faces by their original ordered vertices. Only seam-edge
    # duplicates can collapse; the remainder must preserve winding and material.
    expected_faces = {}
    removed_faces = 0
    affected_faces = {face for remove in targets for face in remove.link_faces}
    for face in mesh.faces:
        ids = [remap.get(vertex.index, vertex.index) for vertex in face.verts]
        ids = [value for i, value in enumerate(ids) if value != ids[i - 1]]
        if len(ids) < 3:
            removed_faces += 1
            continue
        if len(set(ids)) != len(ids):
            raise ValueError("Seam repair would leave a face with repeated nonadjacent vertices")
        key = _cycle(ids)
        if key in expected_faces:
            raise ValueError("Seam repair would merge two distinct surviving faces")
        expected_faces[key] = (_face_properties(face), face.normal.copy(), face in affected_faces)

    frozen_edges = {
        frozenset(vertex.index for vertex in edge.verts): (edge.seam, edge.smooth, edge.hide)
        for edge in mesh.edges if all(vertex not in targets for vertex in edge.verts)
    }
    expected_edges = {
        frozenset(remap.get(vertex.index, vertex.index) for vertex in edge.verts) for edge in mesh.edges
    }
    expected_edges = {edge for edge in expected_edges if len(edge) == 2}
    bmesh.ops.weld_verts(mesh, targetmap=targets)
    mesh.normal_update()
    mesh.verts.index_update()
    lookup = {vertex[old_index]: vertex for vertex in mesh.verts}
    if len(mesh.verts) != len(coordinates) or lookup.keys() != coordinates.keys():
        raise ValueError("Seam repair would alter additional vertices")
    for index, vertex in lookup.items():
        coord, weights = coordinates[index]
        if vertex.co != coord or (deform is not None and dict(vertex[deform].items()) != weights):
            raise ValueError("Seam repair would alter a retained vertex coordinate or weight")
    actual_faces = {}
    for face in mesh.faces:
        key = _cycle([vertex[old_index] for vertex in face.verts])
        if key not in expected_faces:
            raise ValueError("Seam repair would change surviving face winding or connectivity")
        properties, normal, affected = expected_faces[key]
        if (_face_properties(face) != properties or
                (affected and (face.calc_area() <= 1e-14 or face.normal.dot(normal) <= 0))):
            raise ValueError("Seam repair would reverse or collapse an adjacent face or alter its attributes")
        actual_faces[key] = face
    if len(mesh.faces) != len(expected_faces) or actual_faces.keys() != expected_faces.keys():
        raise ValueError("Seam repair would remove additional faces")
    _validate_triangles(mesh, {actual_faces[key] for key in actual_faces if expected_faces[key][2]})
    actual_edges = {
        frozenset(vertex[old_index] for vertex in edge.verts): edge for edge in mesh.edges
    }
    if expected_edges != actual_edges.keys() or any(
            (actual_edges[key].seam, actual_edges[key].smooth, actual_edges[key].hide) != flags
            for key, flags in frozen_edges.items()):
        raise ValueError("Seam repair would remove or alter an uncollapsed edge")
    if any(not edge.is_manifold or not edge.is_contiguous
           for index in retained_ids for edge in lookup[index].link_edges):
        raise ValueError("Seam repair would leave an open, flipped or nonmanifold junction")
    protected_ids = set(coordinates).intersection(vertex[old_index] for vertex in protected)
    mapping = {index: lookup[remap.get(index, index)].index
               for index in sorted(protected_ids | remap.keys() | retained_ids)}
    mesh.verts.layers.int.remove(old_index)
    return {
        "collapsed_edges": len(pairs), "removed_faces": removed_faces,
        "max_collapsed_distance": max(distances), "vertex_index_map": {str(i): v for i, v in mapping.items()},
    }


def execute(obj, tool, params):
    """Build and validate a junction repair on a private BMesh before committing."""
    required = set(REQUIRED.get(tool, ()))
    if not required or not isinstance(params, dict) or not required <= params.keys() or (
        params.keys() - required - set(OPTIONAL[tool])
    ):
        raise ValueError(f"Invalid parameters for {tool}")
    core._available(obj)
    if obj.mode != "OBJECT":
        raise ValueError("Apply fillet or Boolean seam repairs in Object Mode")
    if obj.data.has_custom_normals:
        raise ValueError("Applied geometry repair requires an input without custom split normals")
    if selection.topology_hash(obj) != params["topology_sha256"]:
        raise ValueError("Stale topology_sha256; resolve the current mesh before repairing")
    data = obj.data
    working = bmesh.new()
    try:
        working.from_mesh(data)
        old_index = working.verts.layers.int.new("hsw_seam_vertex_identity") if tool == CLEANUP else None
        working.verts.ensure_lookup_table()
        working.normal_update()
        protected = set(_vertices(working, params.get("protected_vertices", []),
                                  (0, MAX_REPAIR_VERTICES), "protected_vertices"))
        if tool == REFIT:
            report = _refit(working, params, protected)
            coordinates = [coordinate for vertex in working.verts for coordinate in vertex.co]
            data.vertices.foreach_set("co", coordinates)
        else:
            report = _cleanup(obj, working, params, protected, old_index)
            working.to_mesh(data)
        data.update()
        return {
            "tool": tool, "object": obj.name, **report,
            "source_topology_sha256": params["topology_sha256"],
            "topology_sha256": selection.topology_hash(obj),
        }
    finally:
        working.free()
