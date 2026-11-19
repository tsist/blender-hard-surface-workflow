"""World-space strict triangle crossings and local assembly intervals, in millimetres."""

import math

import bpy
import numpy as np
from mathutils.bvhtree import BVHTree


MAX_TRIANGLES = 300_000
MAX_CANDIDATES = 500_000
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


def _blocks(points, triangles):
    # A pair of leaves can allocate at most 256² BVH pairs even on coincident geometry.
    centers = points[triangles].mean(axis=1)
    work = [np.arange(len(triangles))]
    leaves = []
    while work:
        ids = work.pop()
        if len(ids) > 256:
            axis = np.ptp(centers[ids], axis=0).argmax()
            split = len(ids) // 2
            order = np.argpartition(centers[ids, axis], split)
            work.extend((ids[order[:split]], ids[order[split:]]))
            continue
        coords = points[triangles[ids]].reshape(-1, 3)
        tree = BVHTree.FromPolygons(coords.tolist(), np.arange(len(coords)).reshape(-1, 3).tolist(), all_triangles=True)
        leaves.append((ids, tree, coords.min(axis=0), coords.max(axis=0)))
    return leaves


def _self_crossings(points, triangles):
    leaves = _blocks(points, triangles)
    lower = np.array([leaf[2] for leaf in leaves])
    upper = np.array([leaf[3] for leaf in leaves])
    count = tested = 0
    bounds_min = bounds_max = None
    samples = []
    for i, (left_ids, tree, low, high) in enumerate(leaves):
        neighbors = np.flatnonzero(np.all((upper[i:] >= low) & (lower[i:] <= high), axis=1)) + i
        for j in neighbors:
            other_ids, other_tree = leaves[j][:2]
            pairs = np.asarray(tree.overlap(other_tree), dtype=np.int32).reshape(-1, 2)
            if i == j:
                pairs = pairs[pairs[:, 0] < pairs[:, 1]]
            a, b = left_ids[pairs[:, 0]], other_ids[pairs[:, 1]]
            isolated = ~(triangles[a, :, None] == triangles[b, None, :]).any(axis=(1, 2))
            a, b = a[isolated], b[isolated]
            tested += len(a)
            if tested > MAX_CANDIDATES:
                return {"status": "unverified_pair_budget", "strict_hits_lower_bound": count, "samples": samples}
            if not len(a):
                continue
            first, second = points[triangles[a]], points[triangles[b]]
            pa, na = _strict(first, second)
            pb, nb = _strict(second, first)
            mask = na + nb > 0
            if mask.any():
                centers = (pa + pb)[mask] / (na + nb)[mask, None]
                lo, hi = centers.min(axis=0), centers.max(axis=0)
                bounds_min = lo if bounds_min is None else np.minimum(bounds_min, lo)
                bounds_max = hi if bounds_max is None else np.maximum(bounds_max, hi)
                count += int(mask.sum())
                for ta, tb, pt in zip(a[mask][:24-len(samples)], b[mask][:24-len(samples)], centers):
                    samples.append({"triangle_pair": [int(ta), int(tb)], "point_mm": pt.tolist()})
    return {"status": "found" if count else "clear", "strict_hits": count, "tested_pairs": tested,
            "hit_center_bounds_mm": [bounds_min.tolist(), bounds_max.tolist()] if count else None, "samples": samples}


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
    allowed = {"objects", "representations", "self_intersection", "max_triangles", "probes"}
    if not isinstance(params, dict) or "objects" not in params or params.keys() - allowed:
        raise ValueError("Specify objects and recognized geometry audit options")
    names = params["objects"]
    layers = params.get("representations", ["SOURCE", "EVALUATED"])
    probes = params.get("probes", [])
    check = params.get("self_intersection", True)
    limit = params.get("max_triangles", MAX_TRIANGLES)
    if (not isinstance(names, list) or not 1 <= len(names) <= 16 or not all(isinstance(n, str) and n for n in names)
            or not isinstance(layers, list) or not 1 <= len(layers) <= 2 or any(l not in ("SOURCE", "EVALUATED") for l in layers)
            or type(check) is not bool or not isinstance(probes, list) or len(probes) > 128
            or type(limit) is not int or not 1 <= limit <= MAX_TRIANGLES):
        raise ValueError("Invalid objects, representations, triangle budget or probe list")
    if not check and not probes:
        raise ValueError("Select self_intersection or at least one assembly probe")
    if (len(set(names)) != len(names) or len(set(layers)) != len(layers)
            or not math.isfinite(scale) or scale <= 0):
        raise ValueError("Expected unique object names and representations, and a positive scene unit")
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
            selected = [ray for ray in rays if name in ray["objects"]]
            if selected:
                closed = _closed(tris)
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
    return {"version": "1.0", "read_only": True, "frame": scene.frame_current,
            "scene": scene.name, "view_layer": bpy.context.view_layer.name,
            "unit_system": scene.unit_settings.system, "mm_per_bu": scale,
            "objects": records, "probes": rays,
            "coverage": "Nonadjacent strict interior crossings only. Intervals are local to each stated line and pose. Coplanar, shared-vertex and tangential self-contacts are excluded."}
