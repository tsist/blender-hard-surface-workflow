"""Geometry helpers for bounded edit-mesh positioning and symmetry recovery."""
import numpy as np
from mathutils.kdtree import KDTree


_EPS = 1e-12
_MAX_COORD = 1e12
_WORLD_STORAGE_TOLERANCE = 1e-5


def remember_neighborhood(vertices, *, guard_orientation=False):
    """Track usable incident geometry before a coordinate-only edit."""
    faces = {face for vert in vertices for face in vert.link_faces}
    edges = {edge for vert in vertices for edge in vert.link_edges}
    if guard_orientation:
        for face in faces:
            face.normal_update()
    return (
        [(face, face.calc_area(), face.normal.copy() if guard_orientation else None) for face in faces],
        [(edge, edge.calc_length()) for edge in edges],
    )


def check_neighborhood(before, operation):
    faces, edges = before
    for face, area, normal in faces:
        if area > _EPS and face.calc_area() <= max(_EPS, area * 1e-9):
            raise ValueError(f"{operation} would collapse an incident face (index {face.index})")
        if area > _EPS and normal is not None and normal.dot(face.normal) < -1e-6:
            raise ValueError(f"{operation} would reverse an incident face (index {face.index})")
    for edge, length in edges:
        if length > _EPS and edge.calc_length() <= max(_EPS, length * 1e-9):
            raise ValueError(f"{operation} would collapse an incident edge (index {edge.index})")


def _unit(vector, label):
    vector = np.asarray(vector, dtype=np.float64)
    length = np.linalg.norm(vector)
    if length <= _EPS:
        raise ValueError(f"{label} must define a nonzero direction")
    return vector / length


def _frame(normal, tangent, label):
    normal = _unit(normal, f"{label} normal")
    tangent = _unit(tangent, f"{label} tangent")
    tangent -= normal * tangent.dot(normal)
    length = np.linalg.norm(tangent)
    if length <= 1e-6:
        raise ValueError(f"{label} tangent is parallel to its normal")
    tangent /= length
    return np.column_stack((tangent, np.cross(normal, tangent), normal))


def position_reference(vertices, settings, world):
    """Map a selected patch by a single world-space rigid transform."""
    source = np.asarray(settings["source_world"], dtype=np.float64)
    target = np.asarray(settings["target_world"], dtype=np.float64)
    displacement = target - source
    mode = settings["mode"]
    if mode == "PLANE":
        normal = _unit(settings["target_direction"], "Working-plane normal")
        displacement = normal * (displacement.dot(normal) + settings["plane_offset"])
    elif mode == "EDGE":
        edge = np.asarray(settings["target_end_world"], dtype=np.float64) - target
        length_squared = edge.dot(edge)
        if length_squared <= _EPS**2:
            raise ValueError("Target edge endpoints must define a nonzero length")
        fraction = np.clip(-displacement.dot(edge) / length_squared, 0., 1.)
        displacement += fraction * edge
    elif mode == "DIRECTION":
        axis = _unit(settings["target_direction"], "Movement direction")
        displacement = axis * displacement.dot(axis)
    matrix = np.asarray(world, dtype=np.float64)
    linear, translation = matrix[:3, :3], matrix[:3, 3]
    original = np.asarray([vertex.co[:] for vertex in vertices], dtype=np.float64)
    adjustment = np.broadcast_to(displacement, original.shape)
    if mode == "FRAME":
        source_frame = _frame(settings["source_normal"], settings["source_tangent"], "Source frame")
        target_frame = _frame(settings["target_direction"], settings["target_tangent"], "Target frame")
        rotation = target_frame @ source_frame.T
        from_anchor = original @ linear.T - (source - translation)
        adjustment = adjustment + from_anchor @ (rotation - np.eye(3)).T
    try:
        local = original + np.linalg.solve(linear, adjustment.T).T
    except np.linalg.LinAlgError as exc:
        raise ValueError("Cannot position a mesh with a singular world transform") from exc
    if not np.isfinite(local).all() or np.any(np.abs(local) > _MAX_COORD):
        raise ValueError("Reference positioning produced an out-of-range local coordinate")
    # BMesh stores float32 coordinates. Compare displacements before adding large world origins.
    stored = local.astype(np.float32).astype(np.float64)
    actual_local_displacement = stored - original
    error = float(np.linalg.norm(actual_local_displacement @ linear.T - adjustment, axis=1).max())
    moved = int(np.count_nonzero(np.any(actual_local_displacement != 0, axis=1)))
    if error > _WORLD_STORAGE_TOLERANCE:
        raise ValueError(f"Reference positioning exceeds mesh storage precision: {error:.9g} BU error, "
                         f"tolerance {_WORLD_STORAGE_TOLERANCE:g} BU")
    if not moved and np.linalg.norm(adjustment, axis=1).max() > _EPS:
        raise ValueError("Reference positioning would lose the requested movement at mesh storage precision")
    prior = remember_neighborhood(vertices)
    for vertex, co in zip(vertices, stored, strict=True):
        vertex.co = co
    check_neighborhood(prior, "Reference positioning")
    return {
        "mode": mode,
        "source_anchor_world": source.tolist(),
        "mapped_anchor_world": (source + displacement).tolist(),
        "max_world_storage_error": error,
        "world_storage_tolerance": _WORLD_STORAGE_TOLERANCE,
        "moved_vertices": moved,
    }


def snap_symmetry(bm, vertices, settings):
    """Snap local target coordinates against a fixed side without rebuilding connectivity."""
    direction = settings["direction"]
    axis = "XYZ".index(direction[-1])
    source_sign = -1 if direction[0] == "-" else 1
    threshold = settings["threshold"]
    center_tolerance = min(threshold, settings["center_tolerance"])
    factor = settings["factor"]
    sources = [vert for vert in bm.verts if not vert.hide and vert.co[axis] * source_sign > center_tolerance]
    tree = KDTree(len(sources)) if sources else None
    if tree is not None:
        for index, vert in enumerate(sources):
            reflected = vert.co.copy()
            reflected[axis] *= -1
            tree.insert(reflected, index)
        tree.balance()

    pairs = {}
    ambiguous = set()
    unmatched = []
    centers = {}
    for vertex in vertices:
        distance = vertex.co[axis] * source_sign
        if abs(distance) <= center_tolerance:
            if settings["use_center"]:
                goal = vertex.co.copy()
                goal[axis] = 0.0
                centers[vertex] = goal
            continue
        if distance > 0.0:
            continue
        nearby = [item for item in tree.find_n(vertex.co, 2) if item[2] <= threshold] if tree is not None else []
        if not nearby:
            unmatched.append(vertex.index)
            continue
        if len(nearby) > 1:
            ambiguous.add(vertex.index)
            continue
        goal, source, _ = nearby[0]
        pairs.setdefault(source, []).append((vertex, goal))

    goals = dict(centers)
    matched = 0
    for candidates in pairs.values():
        if len(candidates) != 1:
            ambiguous.update(vertex.index for vertex, _ in candidates)
            continue
        vertex, goal = candidates[0]
        goals[vertex] = goal
        matched += 1

    prior = remember_neighborhood(goals)
    moved = 0
    for vertex, goal in goals.items():
        original = vertex.co.copy()
        vertex.co = original.lerp(goal, factor)
        moved += vertex.co != original
    check_neighborhood(prior, "Snap to Symmetry")
    return {
        "direction": direction,
        "factor": factor,
        "matched_vertices": matched,
        "center_vertices": len(centers),
        "moved_vertices": moved,
        "unmatched_indices": sorted(unmatched),
        "ambiguous_indices": sorted(ambiguous),
    }
