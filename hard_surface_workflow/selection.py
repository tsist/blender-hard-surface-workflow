"""Resolve exact mesh selections against the live topology without changing selection."""

from contextlib import contextmanager
import hashlib
import json
import re

import bmesh
import bpy


from .selector_spec import (
    DOMAINS, FACE_DIRECTION, FINGERPRINT, SELECT_MODES, SELECTOR_SCHEMA, TYPE_DOMAIN, _SELECTORS,
)


@contextmanager
def _mesh_access(obj):
    if obj is None or obj.type != "MESH":
        raise ValueError("A mesh object is required")
    in_edit = obj.mode == "EDIT"
    if in_edit:
        editing = bpy.context.objects_in_mode
        if len(editing) != 1 or editing[0] != obj:
            raise ValueError("Select exactly one mesh in Edit Mode")
        mesh = bmesh.from_edit_mesh(obj.data)
    elif obj.mode == "OBJECT":
        mesh = bmesh.new()
        mesh.from_mesh(obj.data)
    else:
        raise ValueError("Use Object Mode or single-object Edit Mode")
    try:
        for name in DOMAINS.values():
            sequence = getattr(mesh, name)
            sequence.ensure_lookup_table()
            sequence.index_update()
        yield mesh, in_edit
    finally:
        if not in_edit:
            mesh.free()


def _topology(mesh):
    contents = {
        "vertices": len(mesh.verts),
        "edges": [[v.index for v in edge.verts] for edge in mesh.edges],
        "faces": [[loop.vert.index for loop in face.loops] for face in mesh.faces],
    }
    serialized = json.dumps(contents, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def topology_hash(obj):
    """Fingerprint vertex count, ordered edges and ordered face corners."""
    with _mesh_access(obj) as (mesh, _):
        return _topology(mesh)


def _indexed(sequence, index, domain):
    if type(index) is not int or not 0 <= index < len(sequence):
        raise ValueError(f"{domain} index is invalid: {index!r}")
    return sequence[index]


def _visible(element):
    if element.hide:
        raise ValueError(f"Selection refers to a hidden element at index {element.index}")
    return element


def _edge(mesh, seed):
    if isinstance(seed, (list, tuple)) and len(seed) == 2:
        ends = tuple(_visible(_indexed(mesh.verts, index, "VERT")) for index in seed)
        if ends[0] == ends[1]:
            raise ValueError("Seed edge endpoints must differ")
        edge = mesh.edges.get(ends)
        if edge is None:
            raise ValueError(f"Seed endpoints do not form an edge: {seed!r}")
    else:
        edge = _indexed(mesh.edges, seed, "EDGE")
    _visible(edge)
    if len(edge.link_faces) > 2:
        raise ValueError(f"The seed is a non-manifold junction: edge {edge.index}")
    return edge


def _single_or_list(selector, singular, plural):
    if (singular in selector) == (plural in selector):
        raise ValueError(f"Give exactly one of {singular!r} and {plural!r}")
    items = [selector[singular]] if singular in selector else selector[plural]
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError(f"{plural} must contain at least one seed")
    return items


def _continue_loop(incoming, vertex):
    adjacent = list(vertex.link_edges)
    face_count = len(incoming.link_faces)
    if face_count == 0:
        if len(adjacent) == 2 and all(not edge.link_faces for edge in adjacent):
            return next(edge for edge in adjacent if edge != incoming)
        return None
    if face_count == 1:
        boundary = [edge for edge in adjacent if len(edge.link_faces) == 1]
        if len(boundary) == 2 and all(len(edge.link_faces) in (1, 2) for edge in adjacent):
            return next(edge for edge in boundary if edge != incoming)
        return None
    if len(adjacent) != 4 or len(vertex.link_faces) != 4:
        return None
    if any(len(edge.link_faces) != 2 for edge in adjacent) or any(len(face.verts) != 4 for face in vertex.link_faces):
        return None
    connected_faces = set(incoming.link_faces)
    opposite = [edge for edge in adjacent if edge != incoming and not connected_faces.intersection(edge.link_faces)]
    return opposite[0] if len(opposite) == 1 else None


def _edge_loop(seed):
    selected = {seed}
    for first_vertex in seed.verts:
        incoming, vertex = seed, first_vertex
        while True:
            _visible(vertex)
            following = _continue_loop(incoming, vertex)
            if following is None or following in selected:
                break
            selected.add(_visible(following))
            incoming, vertex = following, following.other_vert(vertex)
    return selected


def _opposite_quad_edge(face, edge):
    if len(face.verts) != 4:
        return None
    loop = next(loop for loop in face.loops if loop.edge == edge)
    return loop.link_loop_next.link_loop_next.edge


def _edge_ring(seed):
    selected = {seed}
    for initial_face in seed.link_faces:
        incoming, face = seed, initial_face
        while face is not None:
            _visible(face)
            following = _opposite_quad_edge(face, incoming)
            if following is None or following in selected or len(following.link_faces) > 2:
                break
            selected.add(_visible(following))
            incoming = following
            face = next((other for other in following.link_faces if other != face), None)
    return selected


def _face_loop(mesh, selector):
    if "seeds" in selector:
        if "seed_face" in selector or "direction_edge" in selector:
            raise ValueError("Give either one face direction or a list of face directions")
        seeds = selector["seeds"]
    else:
        seeds = [{key: selector.get(key) for key in FACE_DIRECTION["required"]}]
    if not isinstance(seeds, (list, tuple)) or not seeds:
        raise ValueError("seeds must contain at least one face direction")
    selected = set()
    for item in seeds:
        if not isinstance(item, dict) or set(item) != set(FACE_DIRECTION["required"]):
            raise ValueError("Each face-loop seed requires exactly seed_face and direction_edge")
        seed_face = _visible(_indexed(mesh.faces, item["seed_face"], "FACE"))
        seed_edge = _edge(mesh, item["direction_edge"])
        if seed_edge not in seed_face.edges or len(seed_face.verts) != 4:
            raise ValueError("direction_edge must be on the seed quad")
        strip = {seed_face}
        for start in (seed_edge, _opposite_quad_edge(seed_face, seed_edge)):
            incoming, face = start, seed_face
            while len(incoming.link_faces) == 2:
                _visible(incoming)
                following = next(other for other in incoming.link_faces if other != face)
                if following in strip or len(following.verts) != 4:
                    break
                strip.add(_visible(following))
                incoming, face = _opposite_quad_edge(following, incoming), following
        selected.update(strip)
    return selected


def _vertex_groups(obj, mesh, selector):
    names = _single_or_list(selector, "group", "groups")
    indices = set()
    for name in names:
        if not isinstance(name, str) or not name or obj.vertex_groups.get(name) is None:
            raise ValueError(f"Vertex group does not exist: {name!r}")
        indices.add(obj.vertex_groups[name].index)
    min_weight = selector.get("min_weight", 0.0)
    if type(min_weight) not in (int, float) or not 0 <= min_weight <= 1:
        raise ValueError("min_weight must be between 0 and 1")
    weights_layer = mesh.verts.layers.deform.active
    if weights_layer is None:
        return set()
    return {vertex for vertex in mesh.verts if any(group in indices and weight > 0 and weight >= min_weight
                                                  for group, weight in vertex[weights_layer].items())}


def _selector_domain(selector):
    if not isinstance(selector, dict) or not isinstance(selector.get("kind"), str):
        raise ValueError("Selector must be a JSON object with a kind")
    kind = selector["kind"]
    shape = _SELECTORS.get(kind)
    if shape is None:
        raise ValueError(f"Unknown selector kind: {kind!r}")
    unknown = set(selector).difference(shape["properties"])
    missing = set(shape["required"]).difference(selector)
    if unknown or missing:
        raise ValueError(f"Unexpected selector fields {sorted(unknown)} or missing fields {sorted(missing)}")
    domain = selector.get("domain", TYPE_DOMAIN.get(kind))
    if not isinstance(domain, str) or domain not in shape["properties"]["domain"]["enum"]:
        raise ValueError(f"{kind} cannot resolve selection domain {domain!r}")
    return kind, domain


def _check_fingerprint(expected, actual):
    if not isinstance(expected, str) or re.fullmatch(FINGERPRINT["pattern"], expected) is None:
        raise ValueError("topology_sha256 must be a lower-case SHA-256 hex digest")
    if expected != actual:
        raise ValueError(f"Stale topology_sha256: expected {expected}, current {actual}")


def resolve(obj, selector):
    """Return a JSON-serializable domain and stable indices; never change selection."""
    kind, domain = _selector_domain(selector)
    with _mesh_access(obj) as (mesh, _):
        fingerprint = _topology(mesh)
        if "topology_sha256" in selector:
            _check_fingerprint(selector["topology_sha256"], fingerprint)
        elements = getattr(mesh, DOMAINS[domain])
        if kind == "indices":
            indices = selector["indices"]
            if not isinstance(indices, (list, tuple)):
                raise ValueError("indices must be an array")
            selection = {_indexed(elements, index, domain) for index in indices}
            if len(selection) != len(indices):
                raise ValueError("Explicit selection indices must be unique")
        elif kind == "current":
            selection = {element for element in elements if element.select}
        elif kind == "vertex_group":
            selection = _vertex_groups(obj, mesh, selector)
        elif kind == "face_loop":
            selection = _face_loop(mesh, selector)
        else:
            seeds = _single_or_list(selector, "seed_edge", "seed_edges")
            edges = set()
            travel = _edge_ring if kind == "edge_ring" else _edge_loop
            for seed in seeds:
                edges.update(travel(_edge(mesh, seed)))
            selection = {v for edge in edges for v in edge.verts} if kind == "vertex_loop" else edges
        indices = sorted(_visible(element).index for element in selection)
        result = {"domain": domain, "indices": indices, "topology_sha256": fingerprint}
        if kind == "indices":
            order = list(selector["indices"])
        elif kind == "current":
            element_type = {"VERT": bmesh.types.BMVert, "EDGE": bmesh.types.BMEdge, "FACE": bmesh.types.BMFace}[domain]
            order = [el.index for el in mesh.select_history if isinstance(el, element_type) and el in selection]
        else:
            order = indices
        if order != indices:
            result["selection_history"] = order
        return result


def apply(obj, resolved):
    """Apply a previously resolved selection only if its original topology still matches."""
    if (not isinstance(resolved, dict) or not isinstance(resolved.get("domain"), str)
            or resolved["domain"] not in DOMAINS or not isinstance(resolved.get("indices"), (list, tuple))):
        raise ValueError("Resolved selection requires a valid domain and indices")
    domain = resolved["domain"]
    with _mesh_access(obj) as (mesh, in_edit):
        _check_fingerprint(resolved.get("topology_sha256"), _topology(mesh))
        if obj.data.users - int(obj.data.use_fake_user) > 1:
            raise ValueError("A shared mesh would affect the selection of other objects")
        targets = [_visible(_indexed(getattr(mesh, DOMAINS[domain]), index, domain)) for index in resolved["indices"]]
        if len(set(targets)) != len(targets):
            raise ValueError("Resolved selection indices must be unique")
        history = resolved.get("selection_history", resolved["indices"])
        if not isinstance(history, (list, tuple)) or any(type(i) is not int for i in history):
            raise ValueError("Selection history must contain integer indices")
        if len(set(history)) != len(history) or not set(history) <= set(resolved["indices"]):
            raise ValueError("Selection history must reference unique selected elements")
        if in_edit:
            bpy.context.tool_settings.mesh_select_mode = SELECT_MODES[domain]
        for name in reversed(list(DOMAINS.values())):
            for element in getattr(mesh, name):
                element.select = False
        mesh.select_history.clear()
        mesh.select_mode = {domain}
        for element in targets:
            element.select_set(True)
        for index in history:
            mesh.select_history.add(getattr(mesh, DOMAINS[domain])[index])
        mesh.select_flush_mode()
        if in_edit:
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        else:
            for domain_name, attribute in (("VERT", "vertices"), ("EDGE", "edges"), ("FACE", "polygons")):
                getattr(obj.data, attribute).foreach_set("select", [el.select for el in getattr(mesh, DOMAINS[domain_name])])
            obj.data.update()
    return resolved


def diagnose(obj, resolved):
    """Inspect the selected control mesh and its incident edges without changing it."""
    def summary(items):
        ids = sorted(item.index for item in items)
        return {"count": len(ids), "first_indices": ids[:32]}

    def split_fan(vertex):
        unseen = set(vertex.link_faces)
        if len(unseen) < 2:
            return False
        pending = [unseen.pop()]
        while pending:
            face = pending.pop()
            for edge in face.edges:
                if vertex in edge.verts:
                    neighbors = unseen.intersection(edge.link_faces)
                    unseen.difference_update(neighbors)
                    pending.extend(neighbors)
        return bool(unseen)

    with _mesh_access(obj) as (mesh, _):
        _check_fingerprint(resolved["topology_sha256"], _topology(mesh))
        elements = getattr(mesh, DOMAINS[resolved["domain"]])
        chosen = [elements[i] for i in resolved["indices"]]
        vertices = set(chosen) if resolved["domain"] == "VERT" else {v for item in chosen for v in item.verts}
        edges = {e for v in vertices for e in v.link_edges}
        faces = {f for v in vertices for f in v.link_faces}
        return {
            "scope": "selected vertices and their one-ring edges and faces; control mesh, local space",
            "vertices": len(vertices), "incident_edges": len(edges), "incident_faces": len(faces),
            "isolated_vertices": summary(v for v in vertices if not v.link_edges),
            "wire_edges": summary(e for e in edges if not e.link_faces),
            "open_boundary_edges": summary(e for e in edges if e.is_boundary),
            "edges_over_two_faces": summary(e for e in edges if len(e.link_faces) > 2),
            "inconsistent_winding_edges": summary(e for e in edges if e.is_manifold and not e.is_contiguous),
            "disconnected_face_fans": summary(v for v in vertices if split_fan(v)),
            "interior_quad_poles": summary(v for v in vertices if v.link_faces and len(v.link_edges) != 4
                                         and all(e.is_manifold for e in v.link_edges)
                                         and all(len(f.verts) == 4 for f in v.link_faces)),
            "zero_length_edges": summary(e for e in edges if e.calc_length() == 0),
            "zero_area_faces": summary(f for f in faces if f.calc_area() == 0),
        }
