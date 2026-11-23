"""World-space strict triangle crossings and local assembly intervals, in millimetres."""

import math

import bpy
import numpy as np

from . import precision_inspect


MAX_TRIANGLES = 300_000
MAX_CANDIDATES = 500_000
MAX_BOX_CANDIDATES = 4_000_000
BARY_EPS = 1e-6
DIST_EPS_MM = 1e-5


def _positions(obj, layer, scale, limit):
    target = obj if layer == "SOURCE" else obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = target.data if layer == "SOURCE" else target.to_mesh()
    try:
        mesh.calc_loop_triangles()
        count = len(mesh.loop_triangles)
        if count > limit or not count:
            raise ValueError(f"{obj.name}/{layer}: {count} triangles outside the requested budget 1..{limit}")
        vertices = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        triangles = np.empty(count * 3, dtype=np.int32)
        mesh.vertices.foreach_get("co", vertices)
        mesh.loop_triangles.foreach_get("vertices", triangles)
        matrix = np.asarray(target.matrix_world, dtype=np.float64)
        points = (vertices.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]) * scale
        if not np.isfinite(points).all():
            raise ValueError(f"{obj.name}: non-finite world coordinates")
        return points, triangles.reshape(-1, 3)
    finally:
        if layer == "EVALUATED":
            target.to_mesh_clear()


def _strict(a, b):
    """Möller–Trumbore, with both segment ends and all triangle edges excluded."""
    e1, e2 = b[:, 1] - b[:, 0], b[:, 2] - b[:, 0]
    samples = np.zeros((len(a), 3))
    counts = np.zeros(len(a), dtype=np.int32)
    for index in range(3):
        start = a[:, index]
        direction = a[:, (index + 1) % 3] - start
        h = np.cross(direction, e2)
        det = np.einsum("ij,ij->i", e1, h)
        valid = np.abs(det) > 1e-13
        inv = 1 / np.where(valid, det, 1.)
        offset = start - b[:, 0]
        side = np.cross(offset, e1)
        u = inv * np.einsum("ij,ij->i", offset, h)
        v = inv * np.einsum("ij,ij->i", direction, side)
        t = inv * np.einsum("ij,ij->i", e2, side)
        hit = valid & (u > BARY_EPS) & (v > BARY_EPS) & (u + v < 1 - BARY_EPS) & (t > BARY_EPS) & (t < 1 - BARY_EPS)
        samples[hit] += start[hit] + t[hit, None] * direction[hit]
        counts += hit
    return samples, counts


def _blocks(centers, minimum, maximum):
    # Each leaf pair can allocate at most 256² candidates, even if all triangles coincide.
    work = [np.arange(len(centers))]
    leaves = []
    while work:
        ids = work.pop()
        if len(ids) > 256:
            axis = np.ptp(centers[ids], axis=0).argmax()
            split = len(ids) // 2
            order = np.argpartition(centers[ids, axis], split)
            work.extend((ids[order[:split]], ids[order[split:]]))
            continue
        leaves.append((ids, minimum[ids].min(axis=0), maximum[ids].max(axis=0)))
    return leaves


def _box_pairs(a, b, minimum, maximum, same_leaf):
    # Triangle BVH overlap filters out coplanar triangles. Use bounds alone here.
    lower_a, upper_a = minimum[a], maximum[a]
    lower_b, upper_b = minimum[b], maximum[b]
    overlaps = (upper_a[:, None, 0] + DIST_EPS_MM >= lower_b[:, 0]) & (
        lower_a[:, None, 0] - DIST_EPS_MM <= upper_b[:, 0])
    if same_leaf:
        overlaps = np.triu(overlaps, k=1)
    left, right = np.nonzero(overlaps)
    remaining = np.all((upper_a[left, 1:] + DIST_EPS_MM >= lower_b[right, 1:]) &
                       (lower_a[left, 1:] - DIST_EPS_MM <= upper_b[right, 1:]), axis=1)
    return a[left[remaining]], b[right[remaining]]


def _plane_slice(vertices, distances, direction):
    """Projection of a triangle's intersection with a plane on their common line."""
    next_distances = np.roll(distances, -1, axis=1)
    edges = np.roll(vertices, -1, axis=1) - vertices
    crossing = ((distances < 0) & (next_distances > 0)) | ((distances > 0) & (next_distances < 0))
    fraction = distances / np.where(crossing, distances - next_distances, 1.)
    intersections = vertices + fraction[..., None] * edges
    endpoints = np.concatenate((vertices, intersections), axis=1)
    enabled = np.concatenate((distances == 0, crossing), axis=1)
    along_line = np.einsum("ijk,ik->ij", endpoints, direction)
    lower = np.where(enabled, along_line, np.inf)
    upper = np.where(enabled, along_line, -np.inf)
    minimum = lower.argmin(axis=1)
    row = np.arange(len(lower))
    return lower[row, minimum], upper.max(axis=1), endpoints[row, minimum]


def _inside_edge_axes(first, second, normals):
    """Separating-axis test in the triangles' plane, excluding zero-width contacts."""
    edges = np.roll(first, -1, axis=1) - first
    inward = np.cross(normals[:, None], edges)
    offset = second[:, None] - first[:, :, None]
    reaches = np.einsum("ijkl,ijl->ijk", offset, inward).max(axis=2)
    return np.all(reaches > DIST_EPS_MM * np.linalg.norm(edges, axis=2), axis=1)


def _classify_pairs(first, second, normal_a, normal_b, remaining_pairs):
    origin = first[:, 0].copy()
    first = first - origin[:, None]
    second = second - origin[:, None]
    to_b = np.einsum("ijk,ik->ij", first - second[:, :1], normal_b)
    to_a = np.einsum("ijk,ik->ij", second, normal_a)
    line = np.cross(normal_a, normal_b)
    length = np.linalg.norm(line, axis=1)
    straddles_a = (to_a.min(axis=1) < -DIST_EPS_MM) & (to_a.max(axis=1) > DIST_EPS_MM)
    straddles_b = (to_b.min(axis=1) < -DIST_EPS_MM) & (to_b.max(axis=1) > DIST_EPS_MM)
    transverse = np.flatnonzero(straddles_a & straddles_b & (length > 1e-12))
    coplanar = np.flatnonzero((length <= BARY_EPS) &
                              (np.abs(to_a).max(axis=1) <= DIST_EPS_MM) &
                              (np.abs(to_b).max(axis=1) <= DIST_EPS_MM))
    candidates = len(transverse) + len(coplanar)
    if candidates > remaining_pairs:
        return None

    direction = line[transverse] / length[transverse, None]
    lo_a, hi_a, base = _plane_slice(first[transverse], to_b[transverse], direction)
    lo_b, hi_b, _ = _plane_slice(second[transverse], to_a[transverse], direction)
    start, end = np.maximum(lo_a, lo_b), np.minimum(hi_a, hi_b)
    intersects = end - start > DIST_EPS_MM
    middle = (start[intersects] + end[intersects]) * 0.5
    shift = middle - lo_a[intersects]
    strict = transverse[intersects]
    centers = origin[strict] + base[intersects] + shift[:, None] * direction[intersects]

    overlap = _inside_edge_axes(first[coplanar], second[coplanar], normal_a[coplanar]) & (
        _inside_edge_axes(second[coplanar], first[coplanar], normal_b[coplanar]))
    return candidates, strict, centers, coplanar[overlap]


def _candidate_pairs(leaves, minimum, maximum, right_leaves=None):
    self_check = right_leaves is None
    peers = leaves if self_check else right_leaves
    if not leaves or not peers:
        return
    lower = np.array([leaf[1] for leaf in peers])
    upper = np.array([leaf[2] for leaf in peers])
    for i, (left_ids, low, high) in enumerate(leaves):
        start = i if self_check else 0
        neighbors = np.flatnonzero(np.all((upper[start:] + DIST_EPS_MM >= low) &
                                          (lower[start:] - DIST_EPS_MM <= high), axis=1)) + start
        for j in neighbors:
            a, b = _box_pairs(left_ids, peers[j][0], minimum, maximum, self_check and i == j)
            if len(a):
                yield a, b


def _surface_crossings(faces, other_faces=None):
    left_count = len(faces)
    if other_faces is not None:
        faces = np.concatenate((faces, other_faces))
    normals = np.cross(faces[:, 1] - faces[:, 0], faces[:, 2] - faces[:, 0])
    area2 = np.linalg.norm(normals, axis=1)
    usable = np.flatnonzero(area2 > 1e-14)
    degenerate = len(faces) - len(usable)
    faces, normals = faces[usable], normals[usable] / area2[usable, None]
    minimum, maximum = faces.min(axis=1), faces.max(axis=1)
    centers = faces.mean(axis=1)
    split = len(usable) if other_faces is None else int(np.searchsorted(usable, left_count))
    leaves = _blocks(centers[:split], minimum[:split], maximum[:split]) if split else []
    right_leaves = None
    if other_faces is not None:
        right_leaves = [(ids + split, low, high)
                        for ids, low, high in _blocks(centers[split:], minimum[split:], maximum[split:])]
        usable[split:] -= left_count
    count = overlap_count = tested = box_candidates = 0
    bounds_min = bounds_max = None
    samples = []
    overlap_samples = []
    budget_scope = None
    for a, b in _candidate_pairs(leaves, minimum, maximum, right_leaves):
        if box_candidates + len(a) > MAX_BOX_CANDIDATES:
            budget_scope = "bounds_overlap"
            break
        box_candidates += len(a)
        classified = _classify_pairs(faces[a], faces[b], normals[a], normals[b], MAX_CANDIDATES - tested)
        if classified is None:
            budget_scope = "triangle_overlap"
            break
        candidates, strict, centers, coplanar = classified
        tested += candidates
        if len(strict):
            lo, hi = centers.min(axis=0), centers.max(axis=0)
            bounds_min = lo if bounds_min is None else np.minimum(bounds_min, lo)
            bounds_max = hi if bounds_max is None else np.maximum(bounds_max, hi)
            count += len(strict)
            for index, pt in zip(strict[:24 - len(samples)], centers):
                samples.append({"triangle_pair": [int(usable[a[index]]), int(usable[b[index]])],
                                "point_mm": pt.tolist()})
        overlap_count += len(coplanar)
        for index in coplanar[:24 - len(overlap_samples)]:
            overlap_samples.append({"triangle_pair": [int(usable[a[index]]), int(usable[b[index]])]})
    if budget_scope is not None:
        return {"status": "unverified_pair_budget", "strict_hits_lower_bound": count,
                "tested_pairs": tested, "bounds_overlap_pairs": box_candidates, "budget_scope": budget_scope,
                "samples": samples, "degenerate_triangles_excluded": degenerate,
                "coplanar": {"status": "unverified_pair_budget", "overlapping_pairs_lower_bound": overlap_count,
                             "samples": overlap_samples}}
    return {"status": "found" if count else "clear", "strict_hits": count, "tested_pairs": tested,
            "bounds_overlap_pairs": box_candidates,
            "hit_center_bounds_mm": [bounds_min.tolist(), bounds_max.tolist()] if count else None, "samples": samples,
            "degenerate_triangles_excluded": degenerate,
            "coplanar": {"status": "found" if overlap_count else "clear", "overlapping_pairs": overlap_count,
                         "samples": overlap_samples}}


def _self_crossings(points, triangles):
    return _surface_crossings(points[triangles])


def _closed(triangles):
    pairs = triangles[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    ordered = np.sort(pairs, axis=1)
    _, inverse, counts = np.unique(ordered, axis=0, return_inverse=True, return_counts=True)
    if np.any(counts != 2) or np.any(pairs[:, 0] == pairs[:, 1]):
        return False
    orientation = np.where(pairs[:, 0] < pairs[:, 1], 1, -1)
    return bool(np.all(np.bincount(inverse.ravel(), weights=orientation) == 0))


def _intervals(faces, origin, direction):
    a, b, c = faces.transpose(1, 0, 2)
    e1, e2 = b - a, c - a
    normal = np.cross(e1, e2)
    area = np.linalg.norm(normal, axis=1)
    if np.any(area <= 1e-14):
        return {"status": "degenerate_triangles"}
    h = np.cross(direction, e2)
    det = np.einsum("ij,ij->i", e1, h)
    transverse = np.abs(det) > area * 1e-12
    offset = origin - a
    plane_distance = np.einsum("ij,ij->i", offset, normal) / area
    coplanar = ~transverse & (np.abs(plane_distance) <= DIST_EPS_MM)
    if coplanar.any():
        side = np.cross(normal[coplanar] / area[coplanar, None], direction)
        distances = np.einsum("ijk,ik->ij", faces[coplanar] - origin, side)
        if np.any((distances.min(axis=1) <= DIST_EPS_MM) & (distances.max(axis=1) >= -DIST_EPS_MM)):
            return {"status": "ambiguous_boundary"}
    inv = 1 / np.where(transverse, det, 1.)
    side = np.cross(offset, e1)
    u = inv * np.einsum("ij,ij->i", offset, h)
    v = inv * (side @ direction)
    w = 1 - u - v
    inside = transverse & (np.minimum(np.minimum(u, v), w) >= -1e-9)
    if not inside.any():
        return {"status": "miss"}
    if np.any(inside & (np.minimum(np.minimum(u, v), w) <= 1e-9)):
        return {"status": "ambiguous_boundary"}
    distances = (inv * np.einsum("ij,ij->i", e2, side))[inside]
    order = distances.argsort()
    crossings = distances[order]
    signs = np.sign(det[inside][order])
    if len(crossings) % 2 or np.any(np.diff(crossings) <= DIST_EPS_MM) or np.any(signs[:-1] == signs[1:]):
        return {"status": "ambiguous_crossings"}
    return {"status": "ok", "intervals_mm": crossings.reshape(-1, 2).tolist()}


def _relation(a, b):
    if a["status"] != "ok" or b["status"] != "ok":
        return {"status": "unverified"}
    spans = [min(a1, b1) - max(a0, b0) for a0, a1 in a["intervals_mm"] for b0, b1 in b["intervals_mm"]]
    best = max(spans)
    return {"status": "overlap" if best > DIST_EPS_MM else "gap" if best < -DIST_EPS_MM else "contact",
            "max_overlap_mm": max(0., best), "nearest_gap_mm": max(0., -best)}


def run(params):
    scene = bpy.context.scene
    scale = scene.unit_settings.scale_length * 1000
    allowed = {"objects", "representations", "self_intersection", "max_triangles", "probes", "pairs", "measurements"}
    if not isinstance(params, dict) or "objects" not in params or params.keys() - allowed:
        raise ValueError("Specify objects and recognized geometry audit options")
    names = params["objects"]
    layers = params.get("representations", ["SOURCE", "EVALUATED"])
    probes = params.get("probes", [])
    check = params.get("self_intersection", True)
    limit = params.get("max_triangles", MAX_TRIANGLES)
    pairs = params.get("pairs", [])
    measurements = params.get("measurements", [])
    if (not isinstance(names, list) or not 1 <= len(names) <= 16 or not all(isinstance(n, str) and n for n in names)
            or not isinstance(layers, list) or not 1 <= len(layers) <= 2 or any(l not in ("SOURCE", "EVALUATED") for l in layers)
            or type(check) is not bool or not isinstance(probes, list) or len(probes) > 128
            or type(limit) is not int or not 1 <= limit <= MAX_TRIANGLES):
        raise ValueError("Invalid objects, representations, triangle budget or probe list")
    if not check and not (probes or pairs or measurements):
        raise ValueError("Select self_intersection, an object pair, or at least one probe or measurement")
    if (len(set(names)) != len(names) or len(set(layers)) != len(layers)
            or not math.isfinite(scale) or scale <= 0):
        raise ValueError("Expected unique object names and representations, and a positive scene unit")
    if (not isinstance(pairs, list) or len(pairs) > 32
            or any(not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(name, str) for name in pair)
                   or pair[0] == pair[1] or not set(pair) <= set(names) for pair in pairs)
            or len({frozenset(pair) for pair in pairs}) != len(pairs)):
        raise ValueError("pairs requires distinct, unique pairs of listed object names")
    measured = precision_inspect.parse(measurements, names)
    records = {}
    rays = []
    for probe in probes:
        if not isinstance(probe, dict) or set(probe) != {"objects", "point_mm", "direction"}:
            raise ValueError("Each probe requires objects, point_mm and direction")
        pair = probe["objects"]
        if (not isinstance(pair, list) or not 1 <= len(pair) <= 2 or not all(isinstance(n, str) for n in pair)
                or any(not isinstance(probe[k], list) or len(probe[k]) != 3 or
                       any(type(v) not in (int, float) or not math.isfinite(v) for v in probe[k])
                       for k in ("point_mm", "direction"))):
            raise ValueError("Expected one or two probe names and two finite xyz vectors")
        direction = np.array(probe["direction"], dtype=float)
        length = np.linalg.norm(direction)
        if len(set(pair)) != len(pair) or not set(pair) <= set(names) or not math.isfinite(length) or length <= 1e-12:
            raise ValueError("Probe objects must be distinct selected objects and direction must be nonzero")
        rays.append({**probe, "direction": (direction / length).tolist(), "representations": {layer: {"objects": {}} for layer in layers}})
    pair_meshes = {}
    paired = {name for pair in pairs for name in pair}
    for name in names:
        obj = bpy.context.view_layer.objects.get(name)
        if obj is None or obj.type != "MESH":
            raise ValueError(f"Expected mesh in the active view layer: {name}")
        records[name] = {}
        for layer in layers:
            points, tris = _positions(obj, layer, scale, limit)
            row = {"triangles": len(tris)}
            records[name][layer] = row
            if check:
                row["self_intersection"] = _self_crossings(points, tris)
            if name in paired:
                pair_meshes[name, layer] = points, tris
            selected = [ray for ray in rays if name in ray["objects"]]
            pending = [item for item in measured if item["object"] == name]
            if selected or pending:
                closed = _closed(tris)
            if pending:
                answers = precision_inspect.measure(points, tris, closed, pending)
                for item, answer in zip(pending, answers):
                    item["representations"][layer] = answer
            if selected:
                faces = points[tris]
                row["closed_oriented_edges"] = closed
                for ray in selected:
                    hit = _intervals(faces, np.asarray(ray["point_mm"]), np.asarray(ray["direction"])) if closed else {"status": "open_or_nonmanifold"}
                    ray["representations"][layer]["objects"][name] = hit
    for ray in rays:
        if len(ray["objects"]) == 2:
            for results in ray["representations"].values():
                a, b = (results["objects"][name] for name in ray["objects"])
                results["relation"] = _relation(a, b)
    result = {"version": "1.0", "read_only": True, "frame": scene.frame_current,
            "scene": scene.name, "view_layer": bpy.context.view_layer.name,
            "unit_system": scene.unit_settings.system, "mm_per_bu": scale,
            "objects": records, "probes": rays,
            "coverage": "Per-object transverse triangle interior crossings and separate coplanar area overlaps. "
                        "Point, edge and tangential contacts and degenerate triangles are excluded. "
                        "Tests use millimetre tolerances and bounded candidate counts. Occupancy intervals are local "
                        "to each stated line and pose and require a closed, oriented mesh."}
    if "pairs" in params:
        def faces(name, layer):
            points, tris = pair_meshes[name, layer]
            return points[tris]

        result["pairs"] = [{"objects": [left, right], "representations": {
            layer: _surface_crossings(faces(left, layer), faces(right, layer)) for layer in layers
        }} for left, right in pairs]
        result["pair_coverage"] = ("Specified cross-object surface pairs only. Strict crossings and coplanar area are separate. "
                                   "Containment, clearance and swept motion require their own checks.")
    if "measurements" in params:
        result["measurements"] = measured
        result["measurement_coverage"] = ("Bore dimensions and through or remaining wall apply at the stated stations and finite sampled rays. "
                                          "Wall thickness is local to the measured surface normal and search bound.")
    return result
