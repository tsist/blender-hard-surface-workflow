"""Bounded topology operations and geometric checks on the core's edit mesh."""

import bmesh
import bpy
import numpy as np
from mathutils import Vector

from . import geometry_audit


MAX_LOCAL_CORNERS = 20000
MAX_STRIP_FACES = 4096
RELATIVE_AREA_EPS = 1e-7


def _index(mesh, domain, index):
    elements = getattr(mesh, domain)
    elements.ensure_lookup_table()
    if not 0 <= index < len(elements):
        raise ValueError(f"Source {domain} index {index} is out of range")
    element = elements[index]
    if element.hide:
        raise ValueError(f"Source {domain} index {index} is hidden")
    return element


def _edge(a, b):
    return next((edge for edge in a.link_edges if edge.other_vert(a) == b), None)


def _face_scale(face):
    return max((edge.calc_length() for edge in face.edges), default=0.0)


def _check_faces(faces, label):
    for face in faces:
        scale = _face_scale(face)
        if not scale or face.calc_area() <= scale * scale * RELATIVE_AREA_EPS:
            raise ValueError(f"{label} would create or retain a collapsed face")
        if any(edge.calc_length() <= scale * 1e-7 for edge in face.edges):
            raise ValueError(f"{label} would create or retain a collapsed edge")


def _check_local_intersections(mesh, faces, label, *, changed=None):
    if sum(len(face.verts) for face in faces) > MAX_LOCAL_CORNERS:
        raise ValueError(f"{label} exceeds the local triangle-check budget of {MAX_LOCAL_CORNERS} corners")
    if not faces:
        return
    vertices = list({vertex for face in faces for vertex in face.verts})
    indexes = {vertex: index for index, vertex in enumerate(vertices)}
    positions = np.asarray([tuple(vertex.co) for vertex in vertices], dtype=float)
    if not np.isfinite(positions).all():
        raise ValueError(f"{label} has nonfinite coordinates")
    scale = max(_face_scale(face) for face in faces)
    if not scale:
        raise ValueError(f"{label} has no usable local geometric scale")
    local_loops = [triangle for triangle in mesh.calc_loop_triangles() if triangle[0].face in faces]
    if not local_loops:
        raise ValueError(f"{label} has no usable triangles")
    scaled = (positions - positions[0]) / scale
    triangles = scaled[[tuple(indexes[loop.vert] for loop in triangle) for triangle in local_loops]]
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    area = np.linalg.norm(normals, axis=1)
    usable = area > 1e-14
    active = np.asarray([changed is None or triangle[0].face in changed for triangle in local_loops])[usable]
    triangles, normals = triangles[usable], normals[usable] / area[usable, None]
    if not len(triangles):
        raise ValueError(f"{label} has no usable triangles")
    lo, hi = triangles.min(axis=1), triangles.max(axis=1)
    leaves = geometry_audit._blocks(triangles.mean(axis=1), lo, hi)
    box_pairs = tested = 0
    for first, last in geometry_audit._candidate_pairs(leaves, lo, hi):
        box_pairs += len(first)
        active_pairs = active[first] | active[last]
        first, last = first[active_pairs], last[active_pairs]
        report = geometry_audit._classify_pairs(triangles[first], triangles[last], normals[first], normals[last],
                                                  geometry_audit.MAX_CANDIDATES - tested)
        if box_pairs > geometry_audit.MAX_BOX_CANDIDATES or report is None:
            raise ValueError(f"{label} could not check all local overlaps within the pair budget")
        candidates, crossings, _, coplanar = report
        tested += candidates
        if len(crossings) or len(coplanar):
            raise ValueError(f"{label} would produce a local crossing or coplanar overlap")


def _inset(mesh, faces, settings, native):
    if sum(len(face.verts) for face in faces) > MAX_LOCAL_CORNERS:
        raise ValueError(f"Inset selection exceeds the local trial budget of {MAX_LOCAL_CORNERS} corners")
    if settings["use_individual"] and settings["use_edge_rail"]:
        raise ValueError("Inset Edge Rail requires region mode")
    mesh.normal_update()
    _check_faces(faces, "Inset input")
    vertex_coords = {vertex: tuple(vertex.co) for vertex in mesh.verts}
    initial = {face: (face.normal.copy(), tuple(face.verts)) for face in mesh.faces}
    status = native()
    if status != "FINISHED":
        return {"status": status}
    mesh.normal_update()
    moved = {vertex for vertex in mesh.verts if vertex not in vertex_coords or tuple(vertex.co) != vertex_coords[vertex]}
    changed = {face for face in mesh.faces if face not in initial or tuple(face.verts) != initial[face][1]
               or any(vertex in moved for vertex in face.verts)}
    if not changed:
        return {"status": status, "inset_trial": {"checked_faces": 0, "status": "no_geometry_change"}}
    _check_faces(changed, "Inset")
    if any(face in initial and face.normal.dot(initial[face][0]) <= 0.0 for face in changed):
        raise ValueError("Inset would reverse an existing face normal")
    local = changed | {other for face in changed for edge in face.edges for other in edge.link_faces}
    _check_local_intersections(mesh, local, "Inset", changed=changed)
    return {"status": status, "inset_trial": {"checked_faces": len(changed), "edge_neighbors": len(local - changed),
                                             "status": "clear", "scope": "changed faces and their shared-edge neighbors",
                                             "relative_overlap_tolerance": geometry_audit.DIST_EPS_MM}}


def _one_face(faces, label):
    if len(faces) != 1:
        raise ValueError(f"{label} requires exactly one selected source face")
    return faces[0]


def _indexed(mesh):
    for elements in (mesh.verts, mesh.edges, mesh.faces):
        elements.index_update()


def _select_output(mesh, mode, elements):
    for collection in (mesh.faces, mesh.edges, mesh.verts):
        for element in collection:
            element.select = False
    bpy.context.tool_settings.mesh_select_mode = (False, mode == "EDGE", mode == "FACE")
    mesh.select_mode = {mode}
    mesh.select_history.clear()
    for element in elements:
        element.select_set(True)
    mesh.select_flush_mode()


def _split_face(obj, mesh, selected, settings):
    face = _one_face(selected, "Split Face")
    first, last = (_index(mesh, "verts", index) for index in settings["vertices"])
    if any(vertex not in face.verts for vertex in (first, last)) or _edge(first, last):
        raise ValueError("Split Face needs two non-adjacent corners of the selected face without an existing edge")
    if any(vertex.hide for vertex in face.verts):
        raise ValueError("Split Face requires all source corners to be visible")
    if not 4 <= len(face.verts) <= 256:
        raise ValueError("Split Face supports a polygon of 4 to 256 corners")
    face.normal_update()
    _check_faces((face,), "Split Face input")
    normal = face.normal.copy()
    scale = _face_scale(face)
    points = [vertex.co - first.co for vertex in face.verts]
    if max(abs(point.dot(normal)) for point in points) > scale * 1e-6:
        raise ValueError("Split Face needs a planar source face")
    edges = [next_point - point for point, next_point in zip(points, points[1:] + points[:1])]
    turns = [first_edge.cross(last_edge).dot(normal)
             for first_edge, last_edge in zip(edges, edges[1:] + edges[:1])]
    if min(turns) <= scale * scale * 1e-8:
        raise ValueError("Split Face needs a strictly convex source face")
    for edge, point in zip(edges, points):
        if min(edge.cross(other - point).dot(normal) for other in points) < -scale * scale * 1e-7:
            raise ValueError("Split Face needs a simple convex source boundary")
    new_face, loop = bmesh.utils.face_split(face, first, last, use_exist=False)
    cut = loop.edge
    if set(cut.verts) != {first, last} or set(cut.link_faces) != {face, new_face}:
        raise RuntimeError("Split Face did not create the expected shared edge")
    _check_faces((face, new_face), "Split Face")
    _select_output(mesh, "EDGE", [cut])
    _indexed(mesh)
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
    return {"source_vertices": settings["vertices"], "cut_edge": cut.index,
            "result_faces": [face.index, new_face.index], "cut_edge_users": len(cut.link_faces)}


def _opposite_station(face, incoming, station):
    if face.hide or len(face.verts) != 4 or any(vertex.hide for vertex in face.verts):
        raise ValueError("Loop Cut encountered a hidden or non-quad face; end the strip at an open boundary")
    if any(edge.hide for edge in face.edges):
        raise ValueError("Loop Cut encountered a hidden edge in the quad strip")
    opposite = next(edge for edge in face.edges if not any(vertex in incoming.verts for vertex in edge.verts))
    side_edges = [next(edge for edge in face.edges if edge != incoming and vertex in edge.verts) for vertex in station]
    return opposite, tuple(side.other_vert(vertex) for side, vertex in zip(side_edges, station))


def _loop_cut_plan(mesh, face, direction_vertices):
    first, last = (_index(mesh, "verts", index) for index in direction_vertices)
    seed = _edge(first, last)
    if seed is None or seed not in face.edges:
        raise ValueError("Loop Cut direction vertices must form an edge of the selected seed face")
    opposite, other_station = _opposite_station(face, seed, (first, last))
    stations = {seed: (first, last), opposite: other_station}
    strip = {face: (seed, opposite)}
    closed = False
    for start in (seed, opposite):
        current_face, incoming = face, start
        while True:
            if len(incoming.link_faces) == 1:
                break
            if len(incoming.link_faces) != 2:
                raise ValueError("Loop Cut encountered a non-manifold crossed edge")
            neighbor = next(f for f in incoming.link_faces if f != current_face)
            if neighbor in strip:
                if neighbor != face or incoming == start:
                    raise ValueError("Loop Cut encountered a strip that revisits a face through a different branch")
                closed = True
                break
            if len(strip) >= MAX_STRIP_FACES:
                raise ValueError(f"Loop Cut exceeds the budget of {MAX_STRIP_FACES} quad faces")
            following, station = _opposite_station(neighbor, incoming, stations[incoming])
            if following in stations and stations[following] != station:
                raise ValueError("Loop Cut cannot propagate a consistent direction around the strip")
            stations[following] = station
            strip[neighbor] = (incoming, following)
            current_face, incoming = neighbor, following
        if closed:
            break
    return strip, stations, closed


def loop_cut_scope(mesh, seed_face_index, params):
    """Resolve exactly the faces that will be split before core establishes protection."""
    from . import mesh_tools

    settings = mesh_tools._parameters(mesh_tools.TOOLS["native.loop_cut_slide"], params)
    seed = _index(mesh, "faces", seed_face_index)
    strip, _, _ = _loop_cut_plan(mesh, seed, settings["direction_vertices"])
    return sorted(face.index for face in strip)


def _cut_strip(obj, mesh, selected, settings):
    seed = _one_face(selected, "Loop Cut")
    strip, stations, closed = _loop_cut_plan(mesh, seed, settings["direction_vertices"])
    _check_faces(strip, "Loop Cut input")
    factor = settings["factor"]
    source_faces = sorted(face.index for face in strip)
    split_vertices = {}
    station_results = []
    for edge, (first, last) in stations.items():
        selected_station = [first.index, last.index]
        intended = first.co.lerp(last.co, factor)
        _, vertex = bmesh.utils.edge_split(edge, first, factor)
        if (vertex.co - intended).length > max((first.co - last.co).length, 1e-12) * 1e-6:
            raise RuntimeError("Loop Cut did not place a vertex at its specified station")
        split_vertices[edge] = vertex
        station_results.append((selected_station, vertex))
    cut_edges = []
    result_faces = list(strip)
    for face, (incoming, outgoing) in strip.items():
        first, last = split_vertices[incoming], split_vertices[outgoing]
        new_face, loop = bmesh.utils.face_split(face, first, last, use_exist=False)
        cut = loop.edge
        if set(cut.verts) != {first, last} or len(cut.link_faces) != 2 or not all(len(f.verts) == 4 for f in cut.link_faces):
            raise RuntimeError("Loop Cut did not produce two quads with the expected common edge")
        result_faces.append(new_face)
        cut_edges.append(cut)
    _check_faces(result_faces, "Loop Cut")
    _select_output(mesh, "EDGE", cut_edges)
    _indexed(mesh)
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
    return {"source_faces": source_faces, "closed": closed, "factor": factor,
            "stations": [{"source_vertices": pair, "cut_vertex": vertex.index} for pair, vertex in station_results],
            "cut_edges": [edge.index for edge in cut_edges]}


def _selected_edge_graph(edges):
    graph = {}
    for edge in edges:
        for vertex in edge.verts:
            graph.setdefault(vertex, []).append(edge)
    return graph


def _flange(obj, mesh, selected, settings):
    if any(len(edge.link_faces) != 1 or any(vertex.hide for vertex in edge.verts) for edge in selected):
        raise ValueError("Flange requires visible free edges, each with exactly one existing parent face")
    graph = _selected_edge_graph(selected)
    start = _index(mesh, "verts", settings["start_vertex"])
    if len(graph.get(start, ())) != 1 or any(len(edges) > 2 for edges in graph.values()):
        raise ValueError("Flange start_vertex must be an endpoint of one unbranched open edge chain")
    chain = [start]
    seen_edges = set()
    while options := [edge for edge in graph[chain[-1]] if edge not in seen_edges]:
        edge = options[0]
        seen_edges.add(edge)
        chain.append(edge.other_vert(chain[-1]))
    if len(seen_edges) != len(selected):
        raise ValueError("Flange requires one connected open edge chain")
    offsets = settings["offsets"]
    if len(offsets) != 3 * len(chain):
        raise ValueError("Flange needs one local XYZ offset for each ordered chain vertex")
    station_offsets = [Vector(offsets[index:index + 3]) for index in range(0, len(offsets), 3)]
    if any(offset.length == 0.0 for offset in station_offsets):
        raise ValueError("Flange station offsets must be nonzero")
    parents = {edge.link_faces[0] for edge in selected}
    if any(face.hide for face in parents):
        raise ValueError("Flange cannot extrude from a hidden parent face")
    original_ids = {vertex: vertex.index for vertex in chain}
    generated = bmesh.ops.extrude_edge_only(mesh, edges=list(selected))["geom"]
    faces = [item for item in generated if isinstance(item, bmesh.types.BMFace)]
    new_vertices = {item for item in generated if isinstance(item, bmesh.types.BMVert)}
    mapped = {}
    for vertex in chain:
        matches = [edge.other_vert(vertex) for edge in vertex.link_edges if edge.other_vert(vertex) in new_vertices]
        if len(matches) != 1:
            raise RuntimeError("Flange could not map a station to a unique new vertex")
        mapped[vertex] = matches[0]
    if len(set(mapped.values())) != len(chain) or len(faces) != len(selected):
        raise RuntimeError("Flange extrusion did not produce one shared station vertex per source vertex")
    for vertex, offset in zip(chain, station_offsets):
        new_vertex = mapped[vertex]
        new_vertex.co = vertex.co + offset
        if (new_vertex.co - vertex.co - offset).length > offset.length * 1e-4:
            raise ValueError("Flange station position is outside usable mesh coordinate precision")
    outer_edges = [_edge(mapped[first], mapped[last]) for first, last in zip(chain, chain[1:])]
    if not all(edge and len(edge.link_faces) == 1 for edge in outer_edges):
        raise RuntimeError("Flange failed to create a continuous free outer edge")
    common_edges = [_edge(vertex, mapped[vertex]) for vertex in chain[1:-1]]
    if not all(edge and len(edge.link_faces) == 2 and edge.is_contiguous for edge in common_edges):
        raise ValueError("Flange segments do not share consistently oriented station edges")
    if not all(len(edge.link_faces) == 2 and edge.is_contiguous for edge in selected):
        raise ValueError("Flange would have a mismatched winding along the original boundary")
    _check_faces(faces, "Flange")
    _check_local_intersections(mesh, set(faces) | parents, "Flange")
    _select_output(mesh, "EDGE", outer_edges)
    _indexed(mesh)
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
    return {"stations": [{"source_vertex": original_ids[vertex], "outer_vertex": mapped[vertex].index,
                          "offset": list(offset)} for vertex, offset in zip(chain, station_offsets)],
            "outer_edges": [edge.index for edge in outer_edges], "flange_faces": [face.index for face in faces],
            "shared_station_edges": [edge.index for edge in common_edges],
            "uv": "Existing face corners retained; unwrap the new flange faces as needed."}


def _bridge_boundaries(edges, settings):
    if settings["type"] != "SINGLE" or settings["use_merge"] or any(len(edge.link_faces) > 1 for edge in edges):
        return None
    graph = _selected_edge_graph(edges)
    if not graph or any(len(linked) > 2 for linked in graph.values()):
        return None
    components = []
    remaining = set(graph)
    while remaining:
        if len(components) == 2:
            return None
        vertex = min(remaining, key=lambda item: item.index)
        pending = [vertex]
        component = set()
        while pending:
            vertex = pending.pop()
            if vertex in remaining:
                remaining.remove(vertex)
                component.update(graph[vertex])
                pending.extend(edge.other_vert(vertex) for edge in graph[vertex])
        components.append(component)
    if len(components) != 2 or len(components[0]) != len(components[1]):
        return None
    degrees = [sum(len(graph[vertex]) == 1 for vertex in {v for edge in part for v in edge.verts}) for part in components]
    if degrees[0] != degrees[1]:
        return None
    return components


def _bridge_pairs(boundaries, original_faces, original_indices, max_steps):
    first, last = boundaries
    mapping = {}
    for start in first:
        edge, end_vertices = start, tuple(start.verts)
        from_vertices = end_vertices
        visited = set()
        for _ in range(max_steps):
            active = [face for face in edge.link_faces if face not in original_faces and face not in visited]
            if len(active) != 1:
                return None
            face = active[0]
            visited.add(face)
            if len(face.verts) != 4 or face.hide or any(vertex.hide for vertex in face.verts) or any(edge.hide for edge in face.edges):
                return None
            edge, end_vertices = _opposite_station(face, edge, end_vertices)
            if edge in last:
                for source, target in zip(from_vertices, end_vertices):
                    if mapping.setdefault(source, target) != target:
                        return None
                break
        else:
            return None
    if len(set(mapping.values())) != len(mapping):
        return None
    return sorted([original_indices[first], original_indices[last]] for first, last in mapping.items())


def _bridge(mesh, selected, settings, native):
    corners = settings["corner_vertices"]
    boundaries = _bridge_boundaries(selected, settings)
    if corners:
        if len(corners) != 2 or boundaries is None:
            raise ValueError("Bridge corner verification needs two equally sized open or closed boundaries in SINGLE mode without merge")
        vertex_sets = [{vertex.index for edge in part for vertex in edge.verts} for part in boundaries]
        if not any(corners[0] in a and corners[1] in b for a, b in (vertex_sets, vertex_sets[::-1])):
            raise ValueError("Bridge corners must identify one vertex on each boundary")
    originals = set(mesh.faces) if boundaries else set()
    original_indices = {vertex: vertex.index for part in boundaries for edge in part for vertex in edge.verts} if boundaries else {}
    status = native()
    pairs = (_bridge_pairs(boundaries, originals, original_indices, settings["number_cuts"] + 1)
             if status == "FINISHED" and boundaries else None)
    if corners and (pairs is None or corners not in pairs and corners[::-1] not in pairs):
        raise ValueError("Bridge twist did not pair the requested corner vertices")
    return {"status": status, "source_vertex_pairs": pairs or [], "requested_corner_vertices": corners,
            "pairing_status": "measured" if pairs is not None else "unverified_for_this_bridge"}


def run(tool, obj, mesh, selected, settings, native):
    details = {"status": "FINISHED"}
    if tool == "native.inset":
        details = _inset(mesh, selected, settings, native)
    elif tool == "native.split_face":
        details.update(_split_face(obj, mesh, selected, settings))
    elif tool == "native.loop_cut_slide":
        details.update(_cut_strip(obj, mesh, selected, settings))
    elif tool == "native.flange":
        details.update(_flange(obj, mesh, selected, settings))
    elif tool == "native.bridge":
        details = _bridge(mesh, selected, settings, native)
    else:
        raise ValueError(f"Unknown topology tool {tool!r}")
    return {"tool": tool, **details}
