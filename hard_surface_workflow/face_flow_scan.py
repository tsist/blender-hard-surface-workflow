"""Read-only candidate scan of a hard-surface mesh snapshot."""

from collections import Counter, defaultdict
from itertools import product
from math import acos, degrees, dist, floor, isfinite
from numbers import Real


DEFAULT_SETTINGS = {
    "max_elements": 100_000,
    "max_candidates": 100,
    "coincidence_mm": 0.01,
    "max_angle_deg": 35.0,
    "max_spacing_ratio": 3.0,
}

_NEIGHBOURS = tuple(product((-1, 0, 1), repeat=3))
_MAX_PAIR_CHECKS = 250_000


def _limits(settings):
    limits = {key: settings.get(key, default) for key, default in DEFAULT_SETTINGS.items()}
    for key in ("max_elements", "max_candidates"):
        if type(limits[key]) is not int or limits[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("coincidence_mm", "max_angle_deg", "max_spacing_ratio"):
        value = limits[key]
        if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
            raise ValueError(f"{key} must be a finite number")
    if limits["coincidence_mm"] <= 0:
        raise ValueError("coincidence_mm must be positive")
    if not 0 < limits["max_angle_deg"] <= 180:
        raise ValueError("max_angle_deg must be in (0, 180]")
    if limits["max_spacing_ratio"] <= 1:
        raise ValueError("max_spacing_ratio must be greater than 1")
    limits["max_coincident_pair_checks"] = _MAX_PAIR_CHECKS
    return limits


def _opposite_pairs(mesh, incident_edges):
    """In a connected four-quad fan the opposite edges share no face."""
    remaining = list(incident_edges)
    pairs = []
    while remaining:
        first = remaining.pop(0)
        faces = set(mesh.edge_faces[first])
        opposite = [edge for edge in remaining if faces.isdisjoint(mesh.edge_faces[edge])]
        if len(opposite) != 1:
            return []
        second = opposite[0]
        remaining.remove(second)
        pairs.append((first, second))
    return pairs


def _fan_count(mesh, faces, edges):
    attached = {face: [] for face in faces}
    for edge in edges:
        neighbours = mesh.edge_faces[edge]
        if len(neighbours) == 2:
            first, second = neighbours
            attached[first].append(second)
            attached[second].append(first)
    count = 0
    unvisited = set(faces)
    while unvisited:
        count += 1
        pending = [unvisited.pop()]
        while pending:
            for neighbour in attached[pending.pop()]:
                if neighbour in unvisited:
                    unvisited.remove(neighbour)
                    pending.append(neighbour)
    return count


def _position(mesh, domain, index):
    if domain == "VERT":
        points = (mesh.world[index],)
    else:
        vertices = mesh.edges[index] if domain == "EDGE" else mesh.faces[index]
        points = [mesh.world[vertex] for vertex in vertices]
    if not points or any(not isfinite(value) for point in points for value in point):
        return None
    return [sum(axis) / len(points) for axis in zip(*points)]


def _route_measurement(mesh, vertex, edges, mm_per_unit):
    center = mesh.world[vertex]
    directions = []
    lengths = []
    for edge in edges:
        first, second = mesh.edges[edge]
        tip = mesh.world[second if first == vertex else first]
        length = dist(center, tip)
        if not isfinite(length) or length == 0:
            return None
        directions.append(tuple((end - start) / length for start, end in zip(center, tip)))
        lengths.append(length)
    # Both directions point outwards from the vertex. A straight continuation
    # has a dot product of -1 and a turn of 0 degrees.
    dot = sum(a * b for a, b in zip(*directions))
    return {
        "edges": list(edges),
        "turn_degrees": degrees(acos(max(-1.0, min(1.0, -dot)))),
        "edge_lengths_mm": [length * mm_per_unit for length in lengths],
        "spacing_ratio": max(lengths) / min(lengths),
    }


def _cross_spacing(mesh, edge, attached, mm_per_unit):
    first, second = mesh.edges[edge]
    start, end = mesh.world[first], mesh.world[second]
    axis = tuple(b - a for a, b in zip(start, end))
    length = dist(start, end)
    if length == 0:
        return None
    direction = tuple(value / length for value in axis)

    def width(point, corner):
        offset = tuple(b - a for a, b in zip(point, mesh.world[corner]))
        projection = sum(a * b for a, b in zip(direction, offset))
        perpendicular = tuple(a - projection * b for a, b in zip(offset, direction))
        return dist(perpendicular, (0.0, 0.0, 0.0)) * mm_per_unit

    widths = []
    opposite_edges = []
    for face in attached:
        corners = mesh.faces[face]
        if len(corners) != 4 or len(set(corners)) != 4:
            return None
        first_index = corners.index(first)
        step = 1 if corners[(first_index + 1) % 4] == second else -1
        opposite_a = corners[(first_index - step) % 4]
        opposite_b = corners[(first_index + 2 * step) % 4]
        across = (mesh.face_edges[face].index(edge) + 2) % 4
        opposite_edges.append(mesh.face_edges[face][across])
        widths.append([width(start, opposite_a), width(end, opposite_b)])
    paired = list(zip(*widths))
    if min(value for pair in paired for value in pair) <= length * mm_per_unit * 1e-10:
        return None
    return {
        "edge": edge, "faces": list(attached), "opposite_edges": opposite_edges,
        "endpoint_vertices": [first, second], "widths_mm_by_face": widths,
        "ratios_by_endpoint": [max(pair) / min(pair) for pair in paired],
    }


def _coincident_boundaries(mesh, boundary, tolerance, mm_per_unit, add, coverage):
    """Compare true endpoints after a bounded midpoint spatial search."""
    grid = defaultdict(list)
    pair_checks = 0
    indexed = 0
    invalid = 0
    budget_start = None
    # Doubling the cell width leaves headroom at exact tolerance/grid boundaries.
    cell_width = tolerance * 2.0
    for offset, edge in enumerate(boundary):
        a, b = mesh.edges[edge]
        start, end = mesh.world[a], mesh.world[b]
        normalized = tuple((0.5 * u + 0.5 * v) / cell_width for u, v in zip(start, end))
        if not all(isfinite(value) for value in normalized):
            invalid += 1
            continue
        key = tuple(floor(value) for value in normalized)
        x, y, z = key
        for dx, dy, dz in _NEIGHBOURS:
            for other in grid.get((x + dx, y + dy, z + dz), ()):
                if pair_checks == _MAX_PAIR_CHECKS:
                    budget_start = offset
                    break
                pair_checks += 1
                c, d = mesh.edges[other]
                if {a, b} == {c, d}:
                    continue
                other_start, other_end = mesh.world[c], mesh.world[d]
                direct = (dist(start, other_start), dist(end, other_end))
                reversed_pair = (dist(start, other_end), dist(end, other_start))
                gaps, matched = (
                    (direct, (c, d)) if max(direct) <= max(reversed_pair) else (reversed_pair, (d, c))
                )
                if max(gaps) <= tolerance:
                    add("COINCIDENT_SPLIT_EDGES", "EDGE", edge,
                        "Boundary edges match in space and use different vertex IDs.", {
                            "edges": [edge, other],
                            "vertices": [[a, b], list(matched)],
                            "shared_vertices": sorted({a, b}.intersection(matched)),
                            "endpoint_gaps_mm": [gap * mm_per_unit for gap in gaps],
                            "incident_faces": [list(mesh.edge_faces[edge]), list(mesh.edge_faces[other])],
                        })
            if budget_start is not None:
                break
        if budget_start is not None:
            break
        grid[key].append(edge)
        indexed += 1
    coverage.update({
        "eligible_boundary_edges": len(boundary),
        "indexed_boundary_edges": indexed,
        "unindexable_boundary_edges": invalid,
        "incomplete_boundary_edges": 0 if budget_start is None else len(boundary) - budget_start,
        "first_incomplete_edge": None if budget_start is None else boundary[budget_start],
        "candidate_pair_checks": pair_checks,
        "pair_budget_exhausted": budget_start is not None,
        "scope": "Visible one-face edges of this object and mesh layer, compared by both endpoints",
    })


def scan(mesh, settings):
    """Return review signals and observed coverage; acceptance stays with the reviewer."""
    limits = _limits(settings)
    counts = {"vertices": len(mesh.world), "edges": len(mesh.edges), "faces": len(mesh.faces)}
    coverage = {"mesh": counts, "element_count": sum(counts.values())}
    result = {
        "status": "complete",
        "review_required": True,
        "limits": limits,
        "coverage": coverage,
        "candidates": [],
        "candidate_counts": {},
        "omitted_candidate_count": 0,
    }
    if coverage["element_count"] > limits["max_elements"]:
        result["status"] = "skipped"
        coverage["unverified"] = ["element_budget"]
        coverage["scanned"] = dict.fromkeys(counts, 0)
        return result

    mm_per_unit = mesh.scale_length * 1000.0
    if not isfinite(mm_per_unit) or mm_per_unit <= 0:
        raise ValueError("Scene unit scale must be finite and positive")
    tolerance = limits["coincidence_mm"] / mm_per_unit
    if not isfinite(tolerance * 2.0) or tolerance <= 0:
        raise ValueError("coincidence_mm is out of range in scene units")

    hidden_vertices = set(mesh.hidden_verts)
    hidden_edges = set(mesh.hidden_edges)
    hidden_edges.update(index for index, edge in enumerate(mesh.edges) if hidden_vertices.intersection(edge))
    hidden_faces = set(mesh.hidden_faces)
    hidden_faces.update(index for index, edges in enumerate(mesh.face_edges) if hidden_edges.intersection(edges))
    coverage["hidden"] = {"vertices": len(hidden_vertices), "edges": len(hidden_edges), "faces": len(hidden_faces)}
    coverage["scanned"] = {key: count - coverage["hidden"][key] for key, count in counts.items()}
    coverage["hidden_neighbourhoods_skipped"] = {"vertices": 0, "edges": 0}
    coverage["route_measurements"] = {"checked_pairs": 0, "unmeasurable_pairs": 0}

    coverage["cross_spacing"] = {"checked_edges": 0, "unmeasurable_edges": 0}

    candidates_by_kind = defaultdict(list)
    candidate_counts = Counter()

    def add(kind, domain, index, reason, evidence):
        candidate_counts[kind] += 1
        # Keep a bounded sample per kind so large sets of ordinary transitions
        # cannot fill the receipt before a split seam or another kind is seen.
        if len(candidates_by_kind[kind]) == limits["max_candidates"]:
            return
        candidates_by_kind[kind].append({
            "kind": kind, "domain": domain, "index": index,
            "reason": reason, "evidence": evidence,
            "position_world": _position(mesh, domain, index),
        })

    for index, face in enumerate(mesh.faces):
        if index not in hidden_faces and len(face) != 4:
            add("NON_QUAD", "FACE", index, "Opposite quad edges are undefined on this face.",
                {"side_count": len(face)})

    boundary = []
    for index, attached in enumerate(mesh.edge_faces):
        if index in hidden_edges:
            continue
        if hidden_faces.intersection(attached):
            coverage["hidden_neighbourhoods_skipped"]["edges"] += 1
            continue
        if len(attached) == 1:
            boundary.append(index)
        elif len(attached) == 2 and all(len(mesh.faces[face]) == 4 for face in attached):
            measured = _cross_spacing(mesh, index, attached, mm_per_unit)
            if measured is None:
                coverage["cross_spacing"]["unmeasurable_edges"] += 1
                add("DEGENERATE_CROSS_SPACING", "EDGE", index,
                    "Adjacent quad widths could not be measured at this edge.", {"faces": list(attached)})
            else:
                coverage["cross_spacing"]["checked_edges"] += 1
                if max(measured["ratios_by_endpoint"]) > limits["max_spacing_ratio"]:
                    add("CROSS_SPACING_JUMP", "EDGE", index,
                        "Adjacent quad widths differ across this edge at one or both endpoints.", measured)
        elif len(attached) == 0:
            add("LOOSE_EDGE", "EDGE", index, "This edge has no incident surface face.", {})
        elif len(attached) > 2:
            add("NON_MANIFOLD_EDGE", "EDGE", index, "More than two faces meet at this edge.",
                {"faces": list(attached)})

    for index, incident_edges in enumerate(mesh.vert_edges):
        if index in hidden_vertices:
            continue
        incident_faces = mesh.vert_faces[index]
        if hidden_edges.intersection(incident_edges) or hidden_faces.intersection(incident_faces):
            coverage["hidden_neighbourhoods_skipped"]["vertices"] += 1
            continue
        if not incident_edges:
            add("LOOSE_VERTEX", "VERT", index, "No edge reaches this vertex.", {})
            continue
        boundary_edges = [edge for edge in incident_edges if len(mesh.edge_faces[edge]) == 1]
        if boundary_edges and len(boundary_edges) != 2:
            add("BOUNDARY_BRANCH", "VERT", index, "The incident one-face edges do not form a single through-path.",
                {"edges": boundary_edges})
        if any(len(mesh.edge_faces[edge]) not in (1, 2) for edge in incident_edges):
            continue
        fans = _fan_count(mesh, incident_faces, incident_edges)
        if fans > 1:
            add("DISCONNECTED_VERTEX_FAN", "VERT", index,
                "The incident face corners form separate fans at this vertex.",
                {"fan_count": fans, "faces": list(incident_faces)})
            continue
        if boundary_edges or any(len(mesh.faces[face]) != 4 for face in incident_faces):
            continue
        valence = len(incident_edges)
        if (valence, len(incident_faces)) != (4, 4):
            add("QUAD_POLE", "VERT", index, "A quad-only vertex has no regular four-edge through-route.",
                {"valence": valence, "face_count": len(incident_faces), "edges": list(incident_edges)})
            continue
        pairs = _opposite_pairs(mesh, incident_edges)
        if not pairs:
            add("AMBIGUOUS_QUAD_ROUTE", "VERT", index,
                "No unique opposite-edge pattern was found in this four-edge fan.",
                {"edges": list(incident_edges), "faces": list(incident_faces)})
        for edges in pairs:
            measured = _route_measurement(mesh, index, edges, mm_per_unit)
            if measured is None:
                coverage["route_measurements"]["unmeasurable_pairs"] += 1
                add("DEGENERATE_ROUTE", "VERT", index,
                    "A through-route includes a zero-length or non-finite edge.", {"edges": list(edges)})
                continue
            coverage["route_measurements"]["checked_pairs"] += 1
            if measured["turn_degrees"] > limits["max_angle_deg"]:
                add("DIRECTION_JUMP", "VERT", index,
                    "Consecutive edges of the quad route turn more than the configured angle.",
                    measured)
            if measured["spacing_ratio"] > limits["max_spacing_ratio"]:
                add("SPACING_JUMP", "VERT", index,
                    "Consecutive edge lengths along the quad route exceed the configured ratio.",
                    measured)

    coverage["coincidence"] = {}
    _coincident_boundaries(mesh, boundary, tolerance, mm_per_unit, add, coverage["coincidence"])

    unverified = []
    if any(coverage["hidden"].values()):
        unverified.append("hidden_geometry_and_touching_neighbourhoods")
    if coverage["route_measurements"]["unmeasurable_pairs"]:
        unverified.append("unmeasurable_through_routes")
    if coverage["cross_spacing"]["unmeasurable_edges"]:
        unverified.append("unmeasurable_cross_spacing")
    if coverage["coincidence"]["pair_budget_exhausted"]:
        unverified.append("coincident_boundary_pairs_after_budget")
    if coverage["coincidence"]["unindexable_boundary_edges"]:
        unverified.append("unindexable_boundary_edges")
    coverage["unverified"] = unverified
    coverage["scope_limits"] = [
        "Design intent of open boundaries requires a reference or section check.",
        "Coincidence compares one-face edges with matching endpoints in this mesh layer. "
        "Partial overlaps, unequal sampling, and seams between objects are outside this scan.",
        "Direction and spacing are sampled on opposite edges at regular interior four-quad fans. "
        "The lengths describe consecutive samples on those paths.",
        "Cross-spacing compares the widths of adjacent quads projected perpendicular to their shared edge. "
        "Intentional support bands can trigger it.",
    ]

    kinds = sorted(candidates_by_kind, key=lambda kind: (candidate_counts[kind], kind))
    for rank in range(limits["max_candidates"]):
        if len(result["candidates"]) >= limits["max_candidates"]:
            break
        for kind in kinds:
            group = candidates_by_kind[kind]
            if rank < len(group):
                result["candidates"].append(group[rank])
            if len(result["candidates"]) == limits["max_candidates"]:
                break
        else:
            if not any(rank + 1 < len(group) for group in candidates_by_kind.values()):
                break

    result["candidate_counts"] = dict(sorted(candidate_counts.items()))
    result["omitted_candidate_count"] = sum(candidate_counts.values()) - len(result["candidates"])
    if unverified or result["omitted_candidate_count"]:
        result["status"] = "partial"
    return result
