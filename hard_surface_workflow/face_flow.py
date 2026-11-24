"""Read the real control cage or viewport-evaluated mesh for face-flow review."""

from dataclasses import dataclass
import hashlib
import json
import math

import bmesh
import bpy
from mathutils import Vector

from . import face_flow_spec as spec


class StaleSeed(ValueError):
    """The bound seed no longer belongs to the requested mesh snapshot."""


@dataclass(slots=True)
class MeshSnapshot:
    local: tuple
    world: tuple
    edges: tuple
    faces: tuple
    face_edges: tuple
    edge_faces: tuple
    vert_edges: tuple
    vert_faces: tuple
    hidden_verts: frozenset
    hidden_edges: frozenset
    hidden_faces: frozenset
    scale_length: float
    identity: dict

    @property
    def mesh_sha256(self):
        return self.identity["mesh_sha256"]

    def position(self, domain, index):
        if not isinstance(index, int) or index < 0:
            return None
        elements = {"VERT": self.world, "EDGE": self.edges, "FACE": self.faces}.get(domain, ())
        if index >= len(elements):
            return None
        if domain == "VERT":
            return self.world[index]
        points = tuple(self.world[vertex] for vertex in elements[index])
        return tuple(sum(point[axis] for point in points) / len(points) for axis in range(3))


def _budget(verts, edges, faces, loops):
    if verts + edges + faces > spec.MAX_ELEMENTS or loops > spec.MAX_LOOPS:
        raise ValueError(
            f"Mesh snapshot exceeds {spec.MAX_ELEMENTS} combined elements or {spec.MAX_LOOPS} face corners "
            f"(received {verts} vertices, {edges} edges, {faces} faces, {loops} corners)"
        )


def _read_edit(mesh):
    bm = bmesh.from_edit_mesh(mesh)
    verts, edges, faces = bm.verts, bm.edges, bm.faces
    _budget(len(verts), len(edges), len(faces), sum(len(face.verts) for face in faces))
    vertex_map = {vertex: i for i, vertex in enumerate(verts)}
    edge_map = {edge: i for i, edge in enumerate(edges)}
    return (
        tuple(tuple(vertex.co) for vertex in verts),
        tuple(tuple(vertex_map[vertex] for vertex in edge.verts) for edge in edges),
        tuple(tuple(vertex_map[loop.vert] for loop in face.loops) for face in faces),
        tuple(tuple(edge_map[loop.edge] for loop in face.loops) for face in faces),
        tuple(frozenset(i for i, element in enumerate(domain) if element.hide)
              for domain in (verts, edges, faces)),
    )


def _hidden(mesh, name):
    attribute = mesh.attributes.get(name)
    if attribute is None:
        return frozenset()
    return frozenset(i for i, item in enumerate(attribute.data) if item.value)


def _read_mesh(mesh):
    _budget(len(mesh.vertices), len(mesh.edges), len(mesh.polygons), len(mesh.loops))
    loops = mesh.loops
    hidden = tuple(_hidden(mesh, name) for name in (".hide_vert", ".hide_edge", ".hide_poly"))
    return (
        tuple(tuple(vertex.co) for vertex in mesh.vertices),
        tuple(tuple(edge.vertices) for edge in mesh.edges),
        tuple(tuple(face.vertices) for face in mesh.polygons),
        tuple(tuple(loops[i].edge_index for i in face.loop_indices) for face in mesh.polygons),
        hidden,
    )


def _adjacency(vertex_count, edges, faces, face_edges):
    vert_edges = [[] for _ in range(vertex_count)]
    vert_faces = [[] for _ in range(vertex_count)]
    edge_faces = [[] for _ in edges]
    for index, vertices in enumerate(edges):
        for vertex in vertices:
            vert_edges[vertex].append(index)
    for index, vertices in enumerate(faces):
        for vertex in vertices:
            vert_faces[vertex].append(index)
        for edge in face_edges[index]:
            edge_faces[edge].append(index)
    return tuple(tuple(tuple(item) for item in domain) for domain in (edge_faces, vert_edges, vert_faces))


def _digest(*sections):
    digest = hashlib.sha256()
    for section in sections:
        digest.update(json.dumps(section, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _modifier_stack(obj):
    return [
        {"name": modifier.name, "type": modifier.type, "viewport": modifier.show_viewport,
         "edit_mode": modifier.show_in_editmode, "render": modifier.show_render,
         **({"levels": modifier.levels, "render_levels": modifier.render_levels,
             "subdivision_type": modifier.subdivision_type} if modifier.type == "SUBSURF" else {})}
        for modifier in obj.modifiers
    ]


def mesh_snapshot(context, obj, layer):
    """Detach an index-preserving snapshot. Never triangulate or write the mesh."""
    if layer not in spec.LAYERS:
        raise ValueError("layer must be CONTROL_CAGE or EVALUATED")
    if obj is None or obj.type != "MESH" or context.view_layer.objects.get(obj.name) != obj:
        raise ValueError("Select a mesh object in the current view layer")
    if layer == "EVALUATED" and context.mode != "OBJECT":
        raise ValueError("Switch to Object Mode to read the actual evaluated viewport stack")
    if obj.mode not in {"OBJECT", "EDIT"}:
        raise ValueError("Face-flow inspection supports Object Mode or the live Edit Mode control cage")

    graph = context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(graph)
    matrix = evaluated.matrix_world.copy()
    if layer == "EVALUATED":
        if not evaluated.is_evaluated or not obj.visible_get(view_layer=context.view_layer):
            raise ValueError("The object is not evaluated as visible in the current viewport view layer")
        mesh = evaluated.to_mesh()
        if mesh is None:
            raise ValueError("The evaluated object did not yield a mesh")
        try:
            data = _read_mesh(mesh)
        finally:
            evaluated.to_mesh_clear()
    elif obj.mode == "EDIT":
        data = _read_edit(obj.data)
    else:
        data = _read_mesh(obj.data)

    points, edges, faces, face_edges, hidden = data
    edge_faces, vert_edges, vert_faces = _adjacency(len(points), edges, faces, face_edges)
    world = tuple(tuple(matrix @ Vector(point)) for point in points)
    scale = float(context.scene.unit_settings.scale_length)
    if not all(math.isfinite(value) for point in world for value in point):
        raise ValueError("The requested mesh has non-finite world coordinates")
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("The scene unit scale must be positive and finite")

    scene = context.scene
    build = bpy.app.build_hash.decode("ascii", errors="replace")
    ids = {
        "object": obj.name, "mesh": obj.data.name, "scene": scene.name,
        "object_library": obj.library.filepath if obj.library else None,
        "mesh_library": obj.data.library.filepath if obj.data.library else None,
        "view_layer": context.view_layer.name,
        "frame": scene.frame_current, "subframe": scene.frame_subframe, "layer": layer,
        "blender_version": bpy.app.version_string, "blender_build": build,
        "matrix_world": tuple(tuple(row) for row in matrix),
        "scale_length_meters_per_unit": scale,
    }
    topology = _digest(len(points), edges, faces, face_edges)
    snapshot_sha = _digest(ids, topology, points, tuple(tuple(sorted(mask)) for mask in hidden))
    identity = {
        **ids, "topology_sha256": topology, "mesh_sha256": snapshot_sha,
        "coordinates": "world Blender units; scan distances use scene-scaled millimetres",
        "counts": {"vertices": len(points), "edges": len(edges), "faces": len(faces)},
        "hidden": dict(zip(("vertices", "edges", "faces"), map(len, hidden))),
        "mode": context.mode,
        "mesh_origin": "depsgraph_viewport" if layer == "EVALUATED" else ("live_edit_bmesh" if obj.mode == "EDIT" else "mesh_datablock"),
        "modifier_stack": _modifier_stack(obj),
        "file": bpy.data.filepath or None,
        "file_dirty": bool(bpy.data.is_dirty),
    }
    return MeshSnapshot(points, world, edges, faces, face_edges, edge_faces, vert_edges, vert_faces,
                        *hidden, scale, identity)


def _trace_positions(snapshot, trace):
    trace["path_world"] = {
        "vertices": [snapshot.world[i] for i in trace["vertices"]],
        "edges": [tuple(snapshot.world[vertex] for vertex in snapshot.edges[i]) for i in trace["edges"]],
        "face_centers": [snapshot.position("FACE", i) for i in trace["faces"]],
    }
    for stop in trace["stops"]:
        stop["position_world"] = snapshot.position(stop["domain"], stop["index"])


def inspect_face_flow(context, obj, request):
    """Return reference-bound evidence for a single object and representation."""
    settings = spec.normalize(request)
    snapshot = mesh_snapshot(context, obj, settings["layer"])
    expected = settings.get("mesh_sha256")
    if expected is not None and expected != snapshot.mesh_sha256:
        raise StaleSeed("Stale face-flow snapshot. Recheck the reference landmark and register the seed again.")
    report = {
        "face_flow_report_version": "1.0",
        "read_only": True,
        "identity": snapshot.identity,
        "reference": settings.get("reference"),
        "verification_scope": "Path evidence and scan candidates for manual reference comparison.",
    }
    if "trace" in settings:
        from .face_flow_trace import trace

        seed = settings["trace"]
        path = trace(snapshot, seed["kind"], seed["edge"], seed.get("face"), seed["max_steps"])
        path["seed_world"] = {
            "edge": snapshot.position("EDGE", seed["edge"]),
            **({"face": snapshot.position("FACE", seed["face"])} if "face" in seed else {}),
        }
        _trace_positions(snapshot, path)
        report["trace"] = path
    if "scan" in settings:
        from .face_flow_scan import scan

        report["scan"] = scan(snapshot, settings["scan"])
    return report
