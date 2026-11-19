"""Extract control-mesh faces as an independent, editable cover panel."""

import json
from math import isfinite

import bmesh
import bpy
from bpy.props import IntProperty, StringProperty
from mathutils import Matrix

from . import selection


TOOL = "shell_extract"


def catalog():
    return {TOOL: {"label": "Extract Cover Panel", "required": ["name", "selector"], "optional": ["collection"],
                   "read_only": False}}


def _new_name(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 63:
        raise ValueError("A nonempty panel name of at most 63 UTF-8 bytes is required")
    if bpy.data.objects.get(value) is not None:
        raise ValueError(f"The panel object already exists: {value!r}")
    return value


def _destination(obj, name):
    root = bpy.context.scene.collection
    if name is None:
        if len(obj.users_collection) != 1:
            raise ValueError("Specify the destination collection for a source in multiple collections")
        collection = obj.users_collection[0]
    elif isinstance(name, str) and name:
        collection = root if name == root.name else bpy.data.collections.get(name)
    else:
        raise ValueError("Destination collection must be a collection name")
    if collection is None or (collection != root and collection not in root.children_recursive):
        raise ValueError("The destination collection must belong to the current scene")
    if collection.library or collection.override_library or not collection.is_editable:
        raise ValueError("The destination collection is read-only")
    return collection


def _selected_faces(obj, selector):
    if (not isinstance(selector, dict) or selector.get("kind") != "indices"
            or selector.get("domain") != "FACE" or "topology_sha256" not in selector):
        raise ValueError("Give an explicit FACE indices selector with topology_sha256")
    result = selection.resolve(obj, selector)
    if not result["indices"]:
        raise ValueError("Choose at least one visible face to extract")
    return result["indices"]


def _edit_copy(obj):
    if obj.mode == "EDIT":
        return bmesh.from_edit_mesh(obj.data).copy()
    working = bmesh.new()
    try:
        working.from_mesh(obj.data)
    except Exception:
        working.free()
        raise
    return working


def _trim(working, face_indices):
    working.faces.index_update()
    keep = set(face_indices)
    if not keep <= {face.index for face in working.faces}:
        raise ValueError("Selected faces are absent from the source topology")
    for face in list(working.faces):
        if face.index not in keep:
            working.faces.remove(face)
    for edge in list(working.edges):
        if not edge.link_faces:
            working.edges.remove(edge)
    for vertex in list(working.verts):
        if not vertex.link_edges:
            working.verts.remove(vertex)
    working.select_history.clear()
    for face in working.faces:
        face.select_set(True)
    working.normal_update()


def _copy_slot_and_group_settings(source, destination):
    for old, new in zip(source.material_slots, destination.material_slots):
        new.link = old.link
        new.material = old.material
    for old in source.vertex_groups:
        new = destination.vertex_groups.get(old.name)
        if new is None:
            new = destination.vertex_groups.new(name=old.name)
        if new.index != old.index:
            raise ValueError("The panel could not reproduce the source vertex-group indices")
        new.lock_weight = old.lock_weight


def _place(destination, world):
    destination.matrix_world = world
    bpy.context.view_layer.update()
    actual = destination.matrix_world
    if max(abs(actual[i][j] - world[i][j]) for i in range(4) for j in range(4)) < 1e-8:
        return False
    # Standalone objects store TRS. Retain any inherited affine shear in the local control mesh.
    residual = actual.inverted() @ world
    destination.data.transform(residual)
    destination.data.update()
    identity = Matrix.Identity(4)
    return max(abs(residual[i][j] - identity[i][j]) for i in range(4) for j in range(4)) >= 1e-8


def execute(obj, tool, params):
    """Create a panel without editing the source's data, stack or mesh selection."""
    if tool != TOOL or not isinstance(params, dict) or not {"name", "selector"} <= params.keys() or (
        params.keys() - {"name", "selector", "collection"}
    ):
        raise ValueError("shell_extract expects name, selector, and optionally collection")
    if obj is None or obj.type != "MESH" or obj.mode not in {"OBJECT", "EDIT"}:
        raise ValueError("Choose a source mesh in Object Mode or single-object Edit Mode")
    if bpy.context.view_layer.objects.get(obj.name) != obj or not obj.visible_get():
        raise ValueError("The source must be visible in the current View Layer")
    if obj.data.shape_keys or obj.data.has_custom_normals:
        raise ValueError("Panel extraction requires a control mesh without shape keys or custom normals")
    name = _new_name(params["name"])
    collection = _destination(obj, params.get("collection"))
    face_indices = _selected_faces(obj, params["selector"])
    bpy.context.view_layer.update()
    world = obj.matrix_world.copy()
    if not all(isfinite(value) for row in world for value in row) or abs(world.determinant()) < 1e-12:
        raise ValueError("The source transform must be finite and invertible")
    working = _edit_copy(obj)
    output = None
    data = None
    try:
        _trim(working, face_indices)
        data = obj.data.copy()
        data.name = name
        working.to_mesh(data)
        output = bpy.data.objects.new(name, data)
        _copy_slot_and_group_settings(obj, output)
        collection.objects.link(output)
        correction = _place(output, world)
        return {
            "tool": TOOL, "object": obj.name, "output": output.name, "mesh": data.name,
            "collection": collection.name, "source_face_indices": face_indices,
            "vertices": len(data.vertices), "faces": len(data.polygons),
            "materials": [slot.material.name if slot.material else None for slot in output.material_slots],
            "uv_layers": list(data.uv_layers.keys()), "vertex_groups": list(output.vertex_groups.keys()),
            "world_transform_residual_baked": correction, "extraction": "control_mesh",
        }
    except Exception:
        if output is not None:
            bpy.data.objects.remove(output, do_unlink=True)
        if data is not None:
            bpy.data.meshes.remove(data)
        raise
    finally:
        working.free()


def _active_faces(obj):
    current = selection.resolve(obj, {"kind": "current", "domain": "FACE"})
    return {"kind": "indices", "domain": "FACE", "indices": current["indices"],
            "topology_sha256": current["topology_sha256"]}


class HSW_OT_shell_extract(bpy.types.Operator):
    bl_idname = "hard_surface.shell_extract"
    bl_label = "Extract Selected Faces"
    bl_options = {"REGISTER", "UNDO"}

    output_name: StringProperty(name="Cover Panel", default="Cover Panel")
    collection_name: StringProperty(name="Collection")
    face_count: IntProperty(options={"HIDDEN"})
    prepared_selector: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == "MESH" and context.object.mode in {"OBJECT", "EDIT"}

    def invoke(self, context, _event):
        try:
            selector = _active_faces(context.object)
            self.face_count = len(selector["indices"])
            if self.face_count == 0:
                raise ValueError("Choose faces in Edit Mode")
            self.prepared_selector = json.dumps(selector)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, _context):
        self.layout.label(text=f"{self.face_count} selected faces")
        self.layout.prop(self, "output_name")
        self.layout.prop_search(self, "collection_name", bpy.data, "collections")

    def execute(self, context):
        try:
            selector = json.loads(self.prepared_selector) if self.prepared_selector else _active_faces(context.object)
            params = {"name": self.output_name, "selector": selector}
            if self.collection_name:
                params["collection"] = self.collection_name
            result = execute(context.object, TOOL, params)
        except (ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Created {result['output']} with {result['faces']} faces")
        return {"FINISHED"}


def register():
    bpy.utils.register_class(HSW_OT_shell_extract)


def unregister():
    bpy.utils.unregister_class(HSW_OT_shell_extract)
