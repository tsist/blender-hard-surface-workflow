"""Trace visible loop and strip paths on a read-only indexed mesh snapshot."""

from collections import Counter


TRACE_KINDS = ("EDGE_LOOP", "EDGE_RING", "FACE_STRIP")
_TERMINALS = {"boundary", "pole", "non_quad", "loose_end"}


def _stop(reason, domain, index, **details):
    return {"reason": reason, "domain": domain, "index": index, **details}


def _index(elements, value):
    return type(value) is int and 0 <= value < len(elements)


def _edge_issue(mesh, edge):
    if edge in mesh.hidden_edges:
        return _stop("hidden_edge", "EDGE", edge)
    start, end = mesh.edges[edge]
    if start == end:
        return _stop("degenerate_edge", "EDGE", edge)
    for vertex in (start, end):
        if vertex in mesh.hidden_verts:
            return _stop("hidden_vertex", "VERT", vertex, edge=edge)
    return None


def _quad_issue(mesh, face):
    if face in mesh.hidden_faces:
        return _stop("hidden_face", "FACE", face)
    corners = mesh.faces[face]
    edges = mesh.face_edges[face]
    if len(corners) != len(set(corners)) or len(edges) != len(set(edges)):
        return _stop("degenerate_face", "FACE", face)
    if len(corners) != 4 or len(edges) != 4:
        return _stop("non_quad", "FACE", face, corners=len(corners))
    for edge in edges:
        issue = _edge_issue(mesh, edge)
        if issue:
            return issue
    return None


def _fan_issue(mesh, vertex, adjacent, faces):
    """Check that the local face fan is a single cycle or open chain."""
    neighbors = {face: [] for face in faces}
    corners = Counter()
    boundary = []
    for edge in adjacent:
        incident = mesh.edge_faces[edge]
        if len(incident) > 2:
            return _stop("non_manifold", "EDGE", edge, at_vertex=vertex)
        if not incident or any(face not in neighbors for face in incident):
            return _stop("ambiguous_fan", "VERT", vertex)
        corners.update(incident)
        if len(incident) == 1:
            boundary.append(edge)
        else:
            first, second = incident
            neighbors[first].append(second)
            neighbors[second].append(first)
    if len(boundary) not in (0, 2) or any(corners[face] != 2 for face in faces):
        return _stop("ambiguous_fan", "VERT", vertex, boundary_edges=boundary)

    pending = [faces[0]]
    reached = set()
    while pending:
        face = pending.pop()
        if face not in reached:
            reached.add(face)
            pending.extend(neighbors[face])
    if len(reached) != len(faces):
        return _stop("ambiguous_fan", "VERT", vertex)
    return None


def _next_loop_edge(mesh, incoming, vertex):
    adjacent = mesh.vert_edges[vertex]
    faces = mesh.vert_faces[vertex]
    if not faces:
        if len(adjacent) == 1:
            return None, _stop("loose_end", "VERT", vertex)
        if len(adjacent) == 2 and all(not mesh.edge_faces[edge] for edge in adjacent):
            return next(edge for edge in adjacent if edge != incoming), None
        return None, _stop("ambiguous_wire", "VERT", vertex, valence=len(adjacent))

    issue = _fan_issue(mesh, vertex, adjacent, faces)
    if issue:
        return None, issue
    boundary = [edge for edge in adjacent if len(mesh.edge_faces[edge]) == 1]
    if boundary and len(mesh.edge_faces[incoming]) == 2:
        return None, _stop("boundary", "VERT", vertex, boundary_edges=boundary)
    for face in faces:
        if face in mesh.hidden_faces:
            return None, _stop("hidden_face", "FACE", face, at_vertex=vertex)
        if len(mesh.faces[face]) != 4:
            return None, _stop("non_quad", "FACE", face, at_vertex=vertex, corners=len(mesh.faces[face]))

    if boundary:
        return next(edge for edge in boundary if edge != incoming), None
    if len(adjacent) != 4:
        return None, _stop("pole", "VERT", vertex, valence=len(adjacent))
    incoming_faces = set(mesh.edge_faces[incoming])
    candidates = [
        edge for edge in adjacent
        if edge != incoming and incoming_faces.isdisjoint(mesh.edge_faces[edge])
    ]
    if len(candidates) == 1:
        return candidates[0], None
    return None, _stop("ambiguous_direction", "VERT", vertex, candidates=candidates)


def _loop_step(mesh, incoming, vertex):
    following, issue = _next_loop_edge(mesh, incoming, vertex)
    if issue:
        return None, None, issue
    issue = _edge_issue(mesh, following)
    if issue:
        return None, None, issue
    start, end = mesh.edges[following]
    other = end if start == vertex else start
    step = {
        "edge": following,
        "from_vertex": vertex,
        "to_vertex": other,
        "adjacent_faces": sorted(mesh.edge_faces[following]),
    }
    return (following, other), step, None


def _ring_step(mesh, incoming, face):
    if face is None:
        count = len(mesh.edge_faces[incoming])
        reason = "loose_end" if count == 0 else "boundary" if count == 1 else "non_manifold"
        return None, None, _stop(reason, "EDGE", incoming, incident_faces=count)
    issue = _quad_issue(mesh, face)
    if issue:
        return None, None, {**issue, "at_edge": incoming}
    quad_edges = mesh.face_edges[face]
    following = quad_edges[(quad_edges.index(incoming) + 2) % 4]
    incident = mesh.edge_faces[following]
    other = next((index for index in incident if index != face), None) if len(incident) == 2 else None
    step = {"face": face, "from_edge": incoming, "to_edge": following}
    return (following, other), step, None


def _walk(mesh, stepper, start, visited, budget, side, terminal_domain):
    """Walk one side without crossing an already recorded edge a second time."""
    path = []
    state = start
    while True:
        following, step, issue = stepper(mesh, *state)
        if issue:
            return path, False, {**issue, "side": side}
        if len(path) >= budget:
            index = state[1] if terminal_domain == "VERT" else state[0]
            issue = _stop("step_budget", terminal_domain, index, next_edge=following[0])
            return path, False, {**issue, "side": side}
        if following == start:
            path.append(step)
            return path, True, None
        if following[0] in visited:
            issue = _stop("revisited_edge", "EDGE", following[0])
            return path, False, {**issue, "side": side}
        path.append(step)
        visited.add(following[0])
        state = following


def _reverse_loop(step):
    return {**step, "from_vertex": step["to_vertex"], "to_vertex": step["from_vertex"]}


def _reverse_ring(step):
    return {**step, "from_edge": step["to_edge"], "to_edge": step["from_edge"]}


def _trace_loop(mesh, edge, max_steps):
    start, end = mesh.edges[edge]
    seed_step = {
        "edge": edge, "from_vertex": start, "to_vertex": end,
        "adjacent_faces": sorted(mesh.edge_faces[edge]),
    }
    visited = {edge}
    forward, closed, forward_stop = _walk(mesh, _loop_step, (edge, end), visited, max_steps, "forward", "VERT")
    if closed:
        # The final step re-enters the seed edge. Preserve the cycle, omitting that duplicate.
        steps = [seed_step, *forward[:-1]]
        stops = []
    else:
        backward, _, backward_stop = _walk(
            mesh, _loop_step, (edge, start), visited, max_steps - len(forward), "backward", "VERT",
        )
        steps = [_reverse_loop(step) for step in reversed(backward)] + [seed_step, *forward]
        stops = [backward_stop, forward_stop]
    vertices = [step["from_vertex"] for step in steps]
    if not closed:
        vertices.append(steps[-1]["to_vertex"])
    faces = dict.fromkeys(face for step in steps for face in step["adjacent_faces"])
    return {
        "edges": [step["edge"] for step in steps], "faces": list(faces), "vertices": vertices,
        "closed": closed, "stops": stops, "steps": steps,
    }


def _trace_ring(mesh, edge, face, max_steps):
    incident = sorted(mesh.edge_faces[edge])
    forward_face = face if face is not None else next(iter(incident), None)
    backward_face = next((other for other in incident if other != forward_face), None)
    visited = {edge}
    forward, closed, forward_stop = _walk(
        mesh, _ring_step, (edge, forward_face), visited, max_steps, "forward", "EDGE",
    )
    if closed:
        steps = forward
        edges = [step["from_edge"] for step in steps]
        stops = []
    else:
        backward, _, backward_stop = _walk(
            mesh, _ring_step, (edge, backward_face), visited, max_steps - len(forward), "backward", "EDGE",
        )
        steps = [_reverse_ring(step) for step in reversed(backward)] + forward
        edges = [steps[0]["from_edge"], *(step["to_edge"] for step in steps)] if steps else [edge]
        stops = [backward_stop, forward_stop]
    vertices = dict.fromkeys(vertex for index in edges for vertex in mesh.edges[index])
    return {
        "edges": edges, "faces": [step["face"] for step in steps], "vertices": list(vertices),
        "closed": closed, "stops": stops, "steps": steps,
    }


def trace(mesh, kind, edge, face=None, max_steps=5000):
    """Report a visible path and its stopping evidence without mutating the mesh.

    The step budget counts transitions after the seed edge across both sides.
    A boundary edge may seed a loop along the boundary. A loop starting in the
    surface interior stops at the boundary instead of turning along it.
    """
    if kind not in TRACE_KINDS:
        raise ValueError(f"Unknown face-flow trace kind: {kind!r}")
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("max_steps must be a positive integer")
    seed = {"edge": edge}
    if kind == "FACE_STRIP":
        seed["face"] = face

    if not _index(mesh.edges, edge):
        issue = _stop("invalid_edge_index", "EDGE", edge)
    else:
        issue = _edge_issue(mesh, edge)
        if issue is None and len(mesh.edge_faces[edge]) > 2:
            issue = _stop("non_manifold", "EDGE", edge, incident_faces=len(mesh.edge_faces[edge]))
    if issue is None and kind == "FACE_STRIP":
        if not _index(mesh.faces, face):
            issue = _stop("invalid_face_index", "FACE", face)
        elif face not in mesh.edge_faces[edge] or edge not in mesh.face_edges[face]:
            issue = _stop("direction_edge_outside_face", "EDGE", edge, face=face)
        else:
            issue = _quad_issue(mesh, face)
    if issue:
        return {
            "kind": kind, "seed": seed, "status": "invalid_seed", "closed": False,
            "edges": [], "faces": [], "vertices": [], "steps": [], "stops": [{**issue, "side": "seed"}],
        }

    result = _trace_loop(mesh, edge, max_steps) if kind == "EDGE_LOOP" else _trace_ring(mesh, edge, seed.get("face"), max_steps)
    status = "ok" if all(stop["reason"] in _TERMINALS for stop in result["stops"]) else "incomplete"
    return {"kind": kind, "seed": seed, "status": status, **result}
