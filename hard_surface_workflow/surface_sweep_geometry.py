"""Geometry for a swept section on a named side of an evaluated shell."""
import hashlib
import math
import struct

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from .geometry_audit import _surface_crossings

MM = 0.001
EPS = 1e-5


def target_geometry(target, direction, outward, min_cos):
    graph = bpy.context.evaluated_depsgraph_get()
    evaluated = target.evaluated_get(graph)
    mesh = evaluated.to_mesh()
    try:
        if not mesh.polygons or len(mesh.polygons) > 160000:
            raise ValueError("The evaluated target must have 1–160000 faces")
        matrix = evaluated.matrix_world
        determinant = matrix.to_3x3().determinant()
        if not math.isfinite(determinant) or determinant <= 1e-12:
            raise ValueError("Target needs a finite, positive world transform")
        vertices = [(matrix @ vertex.co) / MM for vertex in mesh.vertices]
        faces = [tuple(face.vertices) for face in mesh.polygons]
        mesh.calc_loop_triangles()
        triangles = [tuple(triangle.vertices) for triangle in mesh.loop_triangles]
        normal_matrix = matrix.to_3x3().inverted().transposed()
        normals = [(normal_matrix @ face.normal).normalized() for face in mesh.polygons]
    finally:
        evaluated.to_mesh_clear()
    digest = hashlib.sha256()
    digest.update(np.asarray(vertices, dtype="<f4").tobytes())
    for face in faces:
        digest.update(struct.pack("<I", len(face)))
        digest.update(np.asarray(face, dtype="<u4").tobytes())
    digest.update(np.asarray(matrix, dtype="<f4").tobytes())
    # Island membership is evaluated only among faces eligible for the installation side.
    # A narrow gap between disconnected panels cannot pass by making the jump threshold larger.
    parent = list(range(len(faces)))
    owners = {}

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for face_id, (face, normal) in enumerate(zip(faces, normals, strict=True)):
        if normal.dot(outward) < min_cos or normal.dot(direction) >= -EPS:
            continue
        for a, b in zip(face, face[1:] + face[:1]):
            edge = tuple(sorted((a, b)))
            if edge in owners:
                parent[root(face_id)] = root(owners[edge])
            else:
                owners[edge] = face_id
    return (BVHTree.FromPolygons(vertices, faces), vertices, triangles, [root(i) for i in parent],
            normals, digest.hexdigest())


def _corner_points(station, before, here, after):
    inbound = (here - before).normalized()
    outbound = (after - here).normalized()
    angle = inbound.angle(outbound)
    if angle < 1e-5:
        return [(here, station, True)]
    mode = station["corner"]
    if mode == "MITER":
        if angle > math.radians(175):
            raise ValueError(f"{station['name']}: miter direction is degenerate")
        return [(here, station, True)]
    if mode == "STRAIGHT":
        raise ValueError(f"{station['name']}: a straight station cannot turn")
    setback = station["radius_mm"] * math.tan(angle / 2)
    if not math.isfinite(setback) or setback >= min((here - before).length, (after - here).length):
        raise ValueError(f"{station['name']}: neighboring spans cannot fit the requested radius")
    axis = inbound.cross(outbound).normalized()
    entry = here - setback * inbound
    center = entry + station["radius_mm"] * axis.cross(inbound)
    samples = max(4, math.ceil(math.degrees(angle) / 7.5))
    return [(center + Matrix.Rotation(angle * index / samples, 3, axis) @ (entry - center),
             station, False) for index in range(samples + 1)]


def guide_samples(stations, pitch):
    points = [Vector(station["position_mm"]) for station in stations]
    for index, (before, after) in enumerate(zip(points, points[1:])):
        if (after - before).length <= EPS:
            raise ValueError(f"{stations[index]['name']} / {stations[index+1]['name']}: coincident stations")
    chunks = [[(points[0], stations[0], True)]]
    for index in range(1, len(stations)-1):
        chunks.append(_corner_points(stations[index], *points[index-1:index+2]))
    chunks.append([(points[-1], stations[-1], True)])
    result = []
    for index, chunk in enumerate(chunks):
        if result:
            previous = result[-1][0]
            reach = chunk[0][0] - previous
            if reach.dot((points[index] - points[index-1]).normalized()) <= EPS:
                raise ValueError(f"{stations[index-1]['name']} / {stations[index]['name']}: rounded sections overlap")
            count = max(1, math.ceil(reach.length / pitch))
            label = f"{stations[index-1]['name']} / {stations[index]['name']}"
            result.extend((previous + (step / count) * reach, label, None) for step in range(1, count))
        result.extend((point, station["name"], station if is_node else None)
                      for point, station, is_node in chunk)
    if len(result) > 1024:
        raise ValueError("The swept path exceeds 1024 samples; split the run or increase sampling distance")
    return result


def path_hits(guide, source, recipe, direction, outward, min_cos):
    tree, _, _, islands, normals, _ = source
    hits = []
    for origin, label, control in guide:
        location, _, polygon, distance = tree.ray_cast(origin, direction, recipe["max_distance_mm"])
        if location is None:
            raise ValueError(f"{label}: projection missed the target within {recipe['max_distance_mm']} mm")
        normal = normals[polygon]
        if normal.dot(outward) < min_cos or normal.dot(direction) >= -EPS:
            raise ValueError(f"{label}: ray hit face {polygon} outside the designated installation side")
        if hits:
            last, last_normal, last_polygon, _, previous, _ = hits[-1]
            step = (location - last).length
            if (step <= EPS or step > recipe["max_jump_mm"] or
                    islands[last_polygon] != islands[polygon] or normal.dot(last_normal) < min_cos):
                raise ValueError(f"{previous} → {label}: discontinuous surface between faces "
                                 f"{last_polygon} and {polygon} (step {step:.6g} mm)")
        hits.append((location, normal, polygon, distance, label, control))
    return hits


def profile_section(recipe):
    profile = recipe["profile"]
    if profile["type"] == "RECTANGLE":
        half = profile["width_mm"] / 2
        top = profile["height_mm"]
        return [(-half, 0), (half, 0), (half, top), (-half, top)], []
    radius = profile["outer_diameter_mm"] / 2
    inner = radius - profile["wall_mm"]
    sides = profile["sides"]
    directions = [(math.cos(math.tau * i / sides), math.sin(math.tau * i / sides)) for i in range(sides)]
    return ([(x * radius, y * radius + radius) for x, y in directions],
            [(x * inner, y * inner + radius) for x, y in directions])


def _miter_spans(hits, recipe):
    """Leave room for the inside of each joint before tessellating the connecting faces."""
    profile = recipe["profile"]
    half = profile.get("width_mm", profile.get("outer_diameter_mm")) / 2
    named = {station["name"] for station in recipe["stations"]}
    omit = set()
    for index, (point, _, _, _, label, control) in enumerate(hits[1:-1], 1):
        if not control or control["corner"] != "MITER":
            continue
        incoming = (point - hits[index-1][0]).normalized()
        outgoing = (hits[index+1][0] - point).normalized()
        turn = incoming.angle(outgoing)
        setback = half * math.tan(min(turn, math.radians(control["miter_limit_deg"])) / 2)
        for sign in (-1, 1):
            previous, distance = point, 0.0
            for j in range(index+sign, len(hits) if sign > 0 else -1, sign):
                location = hits[j][0]
                distance += (location - previous).length
                if distance > setback + EPS:
                    break
                if hits[j][4] in named:
                    raise ValueError(f"{label} / {hits[j][4]}: the miter needs more adjacent span")
                omit.add(j)
                previous = location
    return [hit for i, hit in enumerate(hits) if i not in omit]


def sweep_rings(hits, recipe):
    outer_profile, inner_profile = profile_section(recipe)
    outer, inner, diagnostics = [], [], []
    max_error = 0.0
    for index, (point, normal, polygon, distance, label, control) in enumerate(hits):
        previous = hits[max(0, index-1)][0]
        following = hits[min(len(hits)-1, index+1)][0]
        inbound = (point - previous).normalized()
        outbound = (following - point).normalized()
        tangent = (inbound + outbound).normalized()
        up = (normal - normal.dot(tangent) * tangent).normalized()
        across = up.cross(tangent).normalized()
        if min(tangent.length_squared, across.length_squared, up.length_squared) < .99:
            raise ValueError(f"{label}: the surface and path do not define a usable section frame")
        if diagnostics and up.dot(Vector(diagnostics[-1]["up"])) <= 0:
            raise ValueError(f"{label}: the section frame flips at the previous interval")
        lateral, section_projection = 1.0, 1.0
        if control and control["corner"] == "MITER":
            a = (inbound - up * inbound.dot(up)).normalized()
            b = (outbound - up * outbound.dot(up)).normalized()
            angle = a.angle(b) if min(a.length_squared, b.length_squared) > .99 else 0.0
            limit = math.radians(control["miter_limit_deg"])
            lateral = 1 / math.cos(min(angle, limit) / 2)
            section_projection = math.cos(angle / 2)
            width = recipe["profile"].get("width_mm", recipe["profile"].get("outer_diameter_mm"))
            max_error = max(max_error, width * (1 - lateral * section_projection))
            if max_error > recipe["dimensional_tolerance_mm"]:
                raise ValueError(f"{label}: miter limit makes the effective width miss its tolerance "
                                 f"({max_error:.6g} mm error)")
        base = point + up * recipe["gap_mm"]
        outer.append([base + lateral * x * across + y * up for x, y in outer_profile])
        if inner_profile:
            inner.append([base + lateral * x * across + y * up for x, y in inner_profile])
        diagnostics.append({"station": label, "is_control": control is not None, "face": polygon,
                            "position_mm": list(point), "ray_mm": distance, "up": list(up),
                            "tangent": list(tangent), "miter_scale": lateral,
                            "width_projection": section_projection})
    return outer, inner, diagnostics, max_error


def _width_and_height(outer, inner, diagnostics):
    minima, maxima = {}, {}
    for i, (ring, row) in enumerate(zip(outer, diagnostics, strict=True)):
        scale = row["width_projection"]
        if not inner:
            section = {"width": [(ring[1] - ring[0]).length * scale],
                       "height": [(ring[3] - ring[0]).length]}
        else:
            center_ring = inner[i]
            half = len(ring) // 2
            across = (ring[0] - ring[half]).normalized()

            def projected_length(vector):
                return (vector + (scale-1) * vector.dot(across) * across).length

            diameters = {name: [points[j] - points[j+half] for j in range(half)]
                         for name, points in (("outer_diameter", ring), ("inner_diameter", center_ring))}
            walls = [outside - inside for outside, inside in zip(ring, center_ring, strict=True)]
            section = {}
            for name, vectors in (*diameters.items(), ("wall", walls)):
                last = -vectors[0] if name != "wall" else vectors[0]
                pairs = zip(vectors, [*vectors[1:], last], strict=True)
                section[name] = [projected_length(point) for a, b in pairs for point in (a, a.lerp(b, .5))]
        for name, values in section.items():
            low, high = min(values), max(values)
            minima[name] = min(minima.get(name, low), low)
            maxima[name] = max(maxima.get(name, high), high)
    return {name: [minima[name], maxima[name]] for name in minima}


def _surfaces(outer, inner):
    n = len(outer[0])
    total = n * len(outer)
    outer_faces = []
    for ring in range(len(outer)-1):
        for index in range(n):
            a = ring * n + index
            b = ring * n + (index+1) % n
            outer_faces.append((a,b,b+n,a+n))
    # Make end closures directly so the live output always has the intended section.
    if not inner:
        return outer_faces + [tuple(reversed(range(n))), tuple(range(total - n, total))]
    inner_faces = [tuple(index + total for index in reversed(face)) for face in outer_faces]
    ends = []
    for index in range(n):
        next_index = (index + 1) % n
        ends.append((next_index, index, index + total, next_index + total))
        last = total - n
        ends.append((last + index, last + next_index, last + next_index + total, last + index + total))
    return outer_faces + inner_faces + ends


def _triangle_indices(faces):
    return [(face[0], face[index], face[index+1])
            for face in faces for index in range(1, len(face)-1)]


def _clearance(outer, diagnostics, source, recipe):
    tree = source[0]
    is_pipe = recipe["profile"]["type"] == "PIPE"
    minimum, maximum_error = math.inf, 0.0
    for i, (first, second) in enumerate(zip(outer, outer[1:])):
        label = diagnostics[i]["station"]
        # Sample the same triangulated strips as used in the explicit collision pass.
        for span in (0.0, .25, .5, .75, 1.0):
            values = []
            edge_count = len(first) if is_pipe else 1
            for index in range(edge_count):
                next_index = (index + 1) % len(first)
                a, b = first[index], first[next_index]
                c, d = second[next_index], second[index]
                for side in (0.0, .5, 1.0):
                    if side >= span:
                        point = a + side * (b - a) + span * (c - b)
                    else:
                        point = a + span * (d - a) + side * (c - d)
                    hit, normal, _, distance = tree.find_nearest(point)
                    if hit is None:
                        raise ValueError(f"{label}: cannot measure the shell clearance")
                    value = distance if (point - hit).dot(normal) >= 0 else -distance
                    values.append(value)
            gap = min(values)
            if gap + EPS < recipe["min_clearance_mm"]:
                raise ValueError(f"{label}: clearance {gap:.6g} mm is below {recipe['min_clearance_mm']} mm")
            error = abs(gap - recipe["gap_mm"]) if is_pipe else max(abs(v - recipe["gap_mm"]) for v in values)
            if error > recipe["gap_tolerance_mm"]:
                raise ValueError(f"{label}: gap error {error:.6g} mm exceeds tolerance")
            minimum, maximum_error = min(minimum, gap), max(maximum_error, error)
    return minimum, maximum_error


def _check_crossings(vertices, faces, source, diagnostics, profile_count):
    positions = np.asarray(vertices, dtype=np.float64)
    triangles = _triangle_indices(faces)
    geometry = positions[np.asarray(triangles)]
    shell = np.asarray(source[1], dtype=np.float64)[np.asarray(source[2])]
    lower, upper = positions.min(axis=0), positions.max(axis=0)
    nearby = ((shell.max(axis=1) >= lower-EPS) & (shell.min(axis=1) <= upper+EPS)).all(axis=1)
    checks = [("self", _surface_crossings(geometry))]
    if np.any(nearby):
        checks.append(("shell", _surface_crossings(geometry, shell[nearby])))
    for label, report in checks:
        if report["status"] != "clear" or report["coplanar"]["status"] != "clear":
            pairs = report.get("samples") or report["coplanar"].get("samples")
            index = triangles[pairs[0]["triangle_pair"][0]][0] if pairs else 0
            name = diagnostics[(index // profile_count) % len(diagnostics)]["station"]
            raise ValueError(f"{name}: {label} collision or overlap check did not clear")
        if report["degenerate_triangles_excluded"]:
            raise ValueError("A sweep or nearby shell triangle is degenerate")


def solve(target, recipe):
    direction = Vector(recipe["ray_direction"])
    outward = Vector(recipe["outward_normal"])
    min_cos = math.cos(math.radians(recipe["max_normal_angle_deg"]))
    source = target_geometry(target, direction, outward, min_cos)
    guide = guide_samples(recipe["stations"], recipe["sampling_mm"])
    hits = _miter_spans(path_hits(guide, source, recipe, direction, outward, min_cos), recipe)
    outer, inner, diagnostics, dimension_error = sweep_rings(hits, recipe)
    sizes = _width_and_height(outer, inner, diagnostics)
    profile = recipe["profile"]
    expected = {"width": profile["width_mm"], "height": profile["height_mm"]} if not inner else {
        "outer_diameter": profile["outer_diameter_mm"],
        "inner_diameter": profile["outer_diameter_mm"] - 2 * profile["wall_mm"], "wall": profile["wall_mm"]}
    dimension_error = max(dimension_error, max(abs(value - expected[name])
                                               for name, limits in sizes.items() for value in limits))
    if dimension_error > recipe["dimensional_tolerance_mm"]:
        raise ValueError(f"Section sampling exceeds the dimensional tolerance ({dimension_error:.6g} mm)")
    faces = _surfaces(outer, inner)
    vertices = [point for ring in outer + inner for point in ring]
    clearance, gap_error = _clearance(outer, diagnostics, source, recipe)
    _check_crossings(vertices, faces, source, diagnostics, len(outer[0]))
    return {"vertices": [tuple(v * MM) for v in vertices], "faces": faces, "target_sha256": source[-1],
            "report": {"stations": diagnostics, "min_clearance_mm": clearance, "gap_error_mm": gap_error,
                       "dimension_error_mm": dimension_error, "self_intersections": 0, "shell_intersections": 0,
                       "measured_dimensions_mm": sizes,
                       "ring_count": len(outer), "profile_count": len(outer[0]), "has_inner_wall": bool(inner)}}
