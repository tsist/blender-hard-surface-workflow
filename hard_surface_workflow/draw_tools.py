"""Draw closed surface guide paths and rebuild their native Boolean cutters."""

import hashlib
import json
import math

import bmesh
import bpy
from bpy.props import EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector
from mathutils.geometry import tessellate_polygon

from . import stack_tools


TOOL = "surface_path_cutter"
TOOL_OPERATOR = "hard_surface.draw_surface_path"
TOOL_UPDATE_OPERATOR = "hard_surface.update_surface_path_cutter"
_META = "hsw_surface_path_cutter"
_MAX_POINTS = 512
_MAX_TARGET_VERTICES = 350_000
_OPERATIONS = (
    ("CUTTER", "Cutter Only", "Save a closed cutter"),
    ("DIFFERENCE", "Exact Difference", "Add a named native Exact Boolean"),
)
_DIRECTIONS = (("VIEW", "View", "Freeze current view direction"),
               ("CUSTOM", "Custom", "Use a world vector"))
_HANDLES = set()


def catalog():
    return {
        TOOL: {
            "label": "Cutter from Surface Path", "required": ["curve", "name", "direction", "margin"],
            "optional": ["operation", "update"], "read_only": False, "live_update": False,
            "update_mode": "EXPLICIT_IN_PLACE",
            "description": "Project a closed 3D POLY outline and extend one closed prism through the evaluated target.",
        }
    }


def _name(value, label):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 63:
        raise ValueError(f"{label} needs a name of at most 63 UTF-8 bytes")
    return value


def _direction(values):
    if not isinstance(values, (tuple, list, Vector)) or len(values) != 3:
        raise ValueError("direction must contain three finite numbers")
    if any(type(item) not in (int, float) or not -1e10 <= item <= 1e10 for item in values):
        raise ValueError("direction must contain three finite numbers within ±1e10")
    vector = Vector(values)
    if vector.length <= 1e-10:
        raise ValueError("direction must be non-zero")
    return vector.normalized()


def _mesh_object(item):
    return (
        item is not None and item.type == "MESH" and item.mode == "OBJECT"
        and bpy.context.view_layer.objects.get(item.name) == item
    )


def _editable(item):
    return item.is_editable and not item.library and not item.override_library


def _validate_matrix(matrix):
    if not all(math.isfinite(value) for row in matrix for value in row):
        raise ValueError("Object transform must be finite")
    if abs(matrix.to_3x3().determinant()) <= 1e-12:
        raise ValueError("Object transform must be invertible")


def _points(curve):
    if (
        curve is None or curve.type != "CURVE" or curve.mode != "OBJECT"
        or bpy.context.view_layer.objects.get(curve.name) != curve or not _editable(curve)
        or len(curve.data.splines) != 1 or curve.data.dimensions != "3D"
        or curve.modifiers or curve.data.shape_keys
    ):
        raise ValueError("Choose a local Object Mode curve with one 3D POLY spline and no deformation")
    spline = curve.data.splines[0]
    if spline.type != "POLY" or not spline.use_cyclic_u or not 3 <= len(spline.points) <= _MAX_POINTS:
        raise ValueError(f"Source needs one cyclic 3D POLY spline with 3–{_MAX_POINTS} points")
    _validate_matrix(curve.matrix_world)
    return [curve.matrix_world @ point.co.xyz for point in spline.points]


def _area(a, b, c):
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)


def _segments(points, epsilon):
    count = len(points)
    for index in range(count):
        a, b = points[index], points[(index + 1) % count]
        length = (b - a).length
        if length <= epsilon:
            raise ValueError("Projected path has a zero-length edge")
        for next_index in range(index + 2, count - int(index == 0)):
            c, d = points[next_index], points[(next_index + 1) % count]
            first, second = _area(a, b, c) / length, _area(a, b, d) / length
            length_other = (d - c).length
            if length_other <= epsilon:
                raise ValueError("Projected path has a zero-length edge")
            third, fourth = _area(c, d, a) / length_other, _area(c, d, b) / length_other
            if (
                min(first, second) <= epsilon and max(first, second) >= -epsilon
                and min(third, fourth) <= epsilon and max(third, fourth) >= -epsilon
                and all(max(a[axis], b[axis]) >= min(c[axis], d[axis]) - epsilon
                        and max(c[axis], d[axis]) >= min(a[axis], b[axis]) - epsilon for axis in range(2))
            ):
                raise ValueError("Projected path self-intersects")


def _profile(points, direction):
    if not 3 <= len(points) <= _MAX_POINTS or not all(
        math.isfinite(coordinate) and abs(coordinate) <= 1e8
        for point in points for coordinate in point
    ):
        raise ValueError("Expected 3–512 finite path points within the world coordinate budget")
    origin = points[0]
    along = [point - origin for point in points]
    epsilon = max(1e-7, 1e-7 * max(point.length for point in along))
    world_axis = min(range(3), key=lambda index: abs(direction[index]))
    reference = Vector(tuple(float(index == world_axis) for index in range(3)))
    axis_u = (reference - reference.dot(direction) * direction).normalized()
    axis_v = direction.cross(axis_u)
    projected = [Vector((point.dot(axis_u), point.dot(axis_v), 0)) for point in along]
    if len(projected) > 3 and (projected[0] - projected[-1]).length <= epsilon:
        if (points[0] - points[-1]).length > epsilon:
            raise ValueError("Different path endpoints collapse in the chosen projection")
        projected.pop()
    _segments(projected, epsilon)
    while len(projected) > 3:
        for index, current in enumerate(projected):
            previous, following = projected[index - 1], projected[(index + 1) % len(projected)]
            incoming, outgoing = current - previous, following - current
            if abs(incoming.cross(outgoing).z) <= epsilon * max(incoming.length, outgoing.length):
                if incoming.dot(outgoing) <= 0:
                    raise ValueError("Projected path reverses on itself")
                projected.pop(index)
                break
        else:
            break
    area = sum(a.cross(b).z for a, b in zip(projected, (*projected[1:], projected[0]))) / 2
    if abs(area) <= epsilon * max(point.length for point in along):
        raise ValueError("The chosen projection collapses the path area")
    if area < 0:
        projected.reverse()
    triangles = []
    for a, b, c in tessellate_polygon([projected]):
        triangles.append((a, b, c) if _area(projected[a], projected[b], projected[c]) > 0 else (c, b, a))
    if len(triangles) != len(projected) - 2:
        raise ValueError("Blender could not tessellate the closed outline")
    return origin, axis_u, axis_v, projected, triangles, abs(area)


def _closed_volume(mesh, label):
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        volume = bm.calc_volume(signed=True)
        if (
            not bm.faces or not math.isfinite(volume) or volume <= 0
            or any(not edge.is_manifold or not edge.is_contiguous for edge in bm.edges)
            or any(not vertex.is_manifold for vertex in bm.verts)
            or any(face.calc_area() <= 0 for face in bm.faces)
        ):
            raise ValueError(f"{label} must have a closed, consistently wound positive-volume surface")
        return volume
    finally:
        bm.free()


def _evaluated(target, direction, origin, closed):
    bpy.context.view_layer.update()
    item = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
    matrix = item.matrix_world
    _validate_matrix(matrix)
    mesh = item.data
    if not 3 <= len(mesh.vertices) <= _MAX_TARGET_VERTICES or not mesh.polygons:
        raise ValueError(f"Evaluated target requires faces and 3–{_MAX_TARGET_VERTICES:,} vertices")
    positions = [(matrix @ vertex.co - origin).dot(direction) for vertex in mesh.vertices]
    if not all(math.isfinite(position) for position in positions):
        raise ValueError("Evaluated target has nonfinite coordinates")
    volume = _closed_volume(mesh, "Evaluated target") * abs(matrix.to_3x3().determinant()) if closed else None
    return min(positions), max(positions), volume


def _cutter_mesh(name, profile, direction, low, high, matrix):
    origin, axis_u, axis_v, points, triangles, _ = profile
    world_to_cutter = matrix.inverted()
    verts = [
        world_to_cutter @ (origin + axis_u * point.x + axis_v * point.y + direction * depth)
        for depth in (low, high) for point in points
    ]
    count = len(points)
    faces = [tuple(reversed(triangle)) for triangle in triangles]
    faces.extend(tuple(index + count for index in triangle) for triangle in triangles)
    faces.extend((index, (index + 1) % count, (index + 1) % count + count, index + count)
                 for index in range(count))
    mesh = bpy.data.meshes.new(name)
    try:
        mesh.from_pydata(verts, [], faces)
        if mesh.validate():
            raise ValueError("Generated prism required mesh repair")
        mesh.update()
        _closed_volume(mesh, "Generated cutter")
    except Exception:
        bpy.data.meshes.remove(mesh)
        raise
    return mesh


def _signature(item):
    mesh = item.data
    envelope = {
        "vertices": [tuple(vertex.co) for vertex in mesh.vertices],
        "edges": [(tuple(edge.vertices), edge.use_seam, edge.use_edge_sharp) for edge in mesh.edges],
        "faces": [(tuple(face.vertices), face.material_index, face.use_smooth) for face in mesh.polygons],
        "attributes": sorted((attribute.name, attribute.domain, attribute.data_type) for attribute in mesh.attributes
                             if not attribute.name.startswith((".select_", ".hide_"))),
    }
    return hashlib.sha256(json.dumps(envelope, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _record(cutter):
    try:
        record = json.loads(cutter[_META])
        fields = {"schema", "curve", "target", "operation", "modifier", "direction", "margin", "matrix", "signature"}
        if not isinstance(record, dict) or not fields <= record.keys() or record["schema"] != 1:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise ValueError("The object is not a managed surface-path cutter") from None
    return record


def _update_target(cutter, target, curve, operation):
    if not _mesh_object(cutter) or not _editable(cutter):
        raise ValueError("An editable managed cutter in the active view layer is required")
    record = _record(cutter)
    if (record["curve"], record["target"], record["operation"]) != (curve.name, target.name, operation):
        raise ValueError("Curve, target and operation must match the existing cutter")
    mesh = cutter.data
    _validate_matrix(cutter.matrix_world)
    if (
        mesh.users != 1 or not _editable(mesh) or cutter.parent or cutter.constraints or cutter.animation_data
        or cutter.modifiers or cutter.vertex_groups or cutter.material_slots or mesh.uv_layers
        or mesh.shape_keys or mesh.has_custom_normals or mesh.animation_data or mesh.keys()
        or _signature(cutter) != record["signature"]
        or any(abs(value - record["matrix"][row][column]) > 1e-6
               for row, line in enumerate(cutter.matrix_world) for column, value in enumerate(line))
    ):
        raise ValueError("The cutter mesh or pose was edited or shared. Keep it and create a separate cutter")
    modifier = target.modifiers.get(record["modifier"]) if operation == "DIFFERENCE" else None
    if operation == "DIFFERENCE" and (
        modifier is None or modifier.type != "BOOLEAN" or modifier.operation != "DIFFERENCE"
        or modifier.solver != "EXACT" or modifier.operand_type != "OBJECT" or modifier.object != cutter
        or target.modifiers[-1] != modifier or modifier.use_self or modifier.use_hole_tolerant
    ):
        raise ValueError("The managed Exact Boolean changed or has downstream modifiers")
    for consumer in stack_tools._boolean_consumers(cutter):
        if modifier is None or (consumer["object"], consumer["modifier"], consumer["operand_type"]) != (
            target.name, modifier.name, "OBJECT"
        ):
            raise ValueError(f"The cutter has an additional Boolean consumer {consumer['object']!r}")
    users = bpy.data.user_map(subset={cutter}).get(cutter, set())
    for consumer in users:
        if (isinstance(consumer, bpy.types.Collection) and consumer.objects.get(cutter.name) == cutter
                and consumer.user_of_id(cutter) == 1):
            continue
        if isinstance(consumer, bpy.types.Scene):
            structural_users = consumer.collection.user_of_id(cutter) + sum(
                layer.objects.get(cutter.name) == cutter for layer in consumer.view_layers
            )
            if structural_users and consumer.user_of_id(cutter) == structural_users:
                continue
        if modifier is not None and consumer == target and target.user_of_id(cutter) == 1:
            continue
        raise ValueError(f"The cutter has an additional consumer {consumer.name!r}. Create a separate cutter")
    linked_collections = {item for item in bpy.data.collections if item.all_objects.get(cutter.name) == cutter}
    for collection, consumers in bpy.data.user_map(subset=linked_collections).items():
        for consumer in consumers:
            if isinstance(consumer, bpy.types.Collection) and consumer.children.get(collection.name) == collection:
                continue
            if isinstance(consumer, bpy.types.Scene) and consumer.collection.children.get(collection.name) == collection:
                continue
            raise ValueError(f"The cutter collection has an additional consumer {consumer.name!r}")
    return modifier


def execute(target, tool, params):
    """Create or explicitly refresh a cutter without applying its Boolean."""
    if tool != TOOL or not isinstance(params, dict):
        raise ValueError("Expected a surface_path_cutter parameter object")
    required = {"curve", "name", "direction", "margin"}
    if required - params.keys() or params.keys() - required - {"operation", "update"}:
        raise ValueError("surface_path_cutter received missing or unexpected fields")
    if not _mesh_object(target) or not _editable(target):
        raise ValueError("Target must be a local editable Object Mode mesh")
    curve = bpy.data.objects.get(_name(params["curve"], "curve"))
    name = _name(params["name"], "name")
    direction = _direction(params["direction"])
    margin = params["margin"]
    if type(margin) not in (int, float) or not 1e-6 <= margin <= 1e6:
        raise ValueError("margin must be between 0.000001 and 1,000,000 world Blender units")
    operation, update = params.get("operation", "CUTTER"), params.get("update", False)
    if operation not in ("CUTTER", "DIFFERENCE") or type(update) is not bool:
        raise ValueError("operation must be CUTTER or DIFFERENCE, and update a Boolean")
    cutter = bpy.data.objects.get(name)
    if update != (cutter is not None):
        raise ValueError("Use an unused cutter name or explicitly update the existing managed object")
    bpy.context.view_layer.update()
    modifier = _update_target(cutter, target, curve, operation) if update else None
    profile = _profile(_points(curve), direction)
    origin = profile[0]
    previous_visibility = modifier.show_viewport if modifier else None
    if modifier:
        modifier.show_viewport = False
    try:
        minimum, maximum, before = _evaluated(target, direction, origin, operation == "DIFFERENCE")
    finally:
        if modifier:
            modifier.show_viewport = previous_visibility
            bpy.context.view_layer.update()

    matrix = cutter.matrix_world.copy() if update else Matrix.Translation(origin)
    low, high = minimum - margin, maximum + margin
    data = _cutter_mesh(name, profile, direction, low, high, matrix)
    previous_data = cutter.data if update else None
    added = None
    try:
        if update:
            cutter.data = data
        else:
            cutter = bpy.data.objects.new(name, data)
            bpy.context.scene.collection.objects.link(cutter)
            cutter.matrix_world = matrix
            cutter.display_type = "WIRE"
            cutter.hide_render = True
            if operation == "DIFFERENCE":
                added = target.modifiers.new(f"{name} Cut", "BOOLEAN")
                added.operation, added.solver, added.object = "DIFFERENCE", "EXACT", cutter
                modifier = added
        removed = None
        if modifier:
            modifier.show_viewport = True
            _, _, after = _evaluated(target, direction, origin, True)
            removed = before - after
            if removed <= max(1e-8, before * 1e-8):
                raise ValueError("Exact Difference did not remove measurable target volume")
        cutter[_META] = json.dumps({
            "schema": 1, "curve": curve.name, "target": target.name, "operation": operation,
            "modifier": modifier.name if modifier else "", "direction": list(direction), "margin": margin,
            "matrix": [tuple(row) for row in matrix], "signature": _signature(cutter),
        }, allow_nan=False)
    except Exception:
        if added:
            target.modifiers.remove(added)
            modifier = None
        if update:
            cutter.data = previous_data
        elif cutter:
            bpy.data.objects.remove(cutter, do_unlink=True)
        bpy.data.meshes.remove(data)
        raise
    finally:
        if update and modifier:
            modifier.show_viewport = previous_visibility
            bpy.context.view_layer.update()
    if previous_data is not None and previous_data.users == 0:
        old_name = previous_data.name
        bpy.data.meshes.remove(previous_data)
        data.name = old_name
    return {
        "tool": TOOL, "object": target.name, "curve": curve.name, "output": cutter.name,
        "updated": update, "live_update": False, "operation": operation,
        "modifier": modifier.name if modifier else None, "profile_area": profile[-1],
        "depth": high - low, "removed_world_volume": removed,
        "vertices": len(data.vertices), "faces": len(data.polygons),
    }


def _new_path(points, name):
    name = _name(name or "Surface Path", "path name")
    data = bpy.data.curves.new(name, "CURVE")
    data.dimensions = "3D"
    spline = data.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for control, point in zip(spline.points, points):
        control.co = (*point, 1)
    spline.use_cyclic_u = True
    curve = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(curve)
    curve.show_in_front = True
    return curve


def _path_preview(operator):
    if bpy.context.area != operator.area or len(operator.points) < 2:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINE_STRIP", {"pos": operator.points})
    shader.bind()
    shader.uniform_float("color", (0.18, 0.72, 1, 1))
    batch.draw(shader)


class HSW_OT_draw_surface_path(bpy.types.Operator):
    bl_idname = TOOL_OPERATOR
    bl_label = "Draw Surface Path"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(items=(("PATH", "Path", "Save a closed guide"),
                              ("CUTTER", "Cut", "Save a guide and cutter")), default="PATH")
    operation: EnumProperty(name="Cut Mode", items=_OPERATIONS, default="DIFFERENCE")
    path_name: StringProperty(name="Path", default="Surface Path")
    cutter_name: StringProperty(name="Cutter", default="")
    direction_mode: EnumProperty(items=_DIRECTIONS, default="VIEW")
    direction: FloatVectorProperty(name="World Direction", default=(0, 0, -1))
    margin: FloatProperty(name="Margin", default=0.25, min=1e-6, max=1e6)
    handle = None

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == "VIEW_3D" and _mesh_object(context.object)

    def invoke(self, context, _event):
        if not _editable(context.object) or not context.object.visible_get():
            self.report({"ERROR"}, "Choose a visible local mesh")
            return {"CANCELLED"}
        self.area, self.window = context.area, context.window
        self.region = next(region for region in self.area.regions if region.type == "WINDOW")
        self.view = context.space_data.region_3d
        self.target = context.object
        self.points = []
        try:
            vector = self.direction[:] if self.direction_mode == "CUSTOM" else (
                self.view.view_rotation @ Vector((0, 0, -1)))
            self.along = _direction(vector)
            self.handle = bpy.types.SpaceView3D.draw_handler_add(_path_preview, (self,), "WINDOW", "POST_VIEW")
            _HANDLES.add(self.handle)
            context.workspace.status_text_set("左键取点 · Backspace 回退 · Enter 保存 · Esc 取消")
            context.window_manager.modal_handler_add(self)
        except (ValueError, RuntimeError) as error:
            self.cancel(context)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        if self.handle in _HANDLES:
            bpy.types.SpaceView3D.draw_handler_remove(self.handle, "WINDOW")
            _HANDLES.remove(self.handle)
        if context.workspace:
            context.workspace.status_text_set(None)
        if context.area == self.area:
            self.area.tag_redraw()

    def _pick(self, context, event):
        x, y = event.mouse_x - self.region.x, event.mouse_y - self.region.y
        if not 0 <= x < self.region.width or not 0 <= y < self.region.height:
            return None
        target = self.target.evaluated_get(context.evaluated_depsgraph_get())
        inverse = target.matrix_world.inverted()
        origin = view3d_utils.region_2d_to_origin_3d(self.region, self.view, (x, y))
        world_ray = view3d_utils.region_2d_to_vector_3d(self.region, self.view, (x, y))
        hit, local, _, _ = target.ray_cast(inverse @ origin, inverse.to_3x3() @ world_ray)
        return target.matrix_world @ local if hit else None

    def _commit(self):
        _profile(self.points, self.along)
        curve = _new_path(self.points, self.path_name)
        try:
            if self.mode == "CUTTER":
                name = self.cutter_name or f"{curve.name} Cutter"
                execute(self.target, TOOL, {
                    "curve": curve.name, "name": name, "direction": list(self.along),
                    "margin": self.margin, "operation": self.operation,
                })
        except Exception:
            data = curve.data
            bpy.data.objects.remove(curve, do_unlink=True)
            bpy.data.curves.remove(data)
            raise
        return curve

    def modal(self, context, event):
        if event.type == "WINDOW_DEACTIVATE" or (event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS"):
            self.cancel(context)
            return {"CANCELLED"}
        if context.window != self.window or context.area != self.area or self.handle not in _HANDLES:
            self.cancel(context)
            return {"CANCELLED"}
        if event.value != "PRESS":
            return {"PASS_THROUGH"}
        try:
            if event.type == "LEFTMOUSE":
                point = self._pick(context, event)
                if point is not None and len(self.points) < _MAX_POINTS:
                    if not self.points or (self.points[-1] - point).length > 1e-7:
                        self.points.append(point)
                        self.area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "BACK_SPACE":
                if self.points:
                    self.points.pop()
                    self.area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type in {"RET", "NUMPAD_ENTER"}:
                curve = self._commit()
                self.cancel(context)
                for item in context.selected_objects:
                    item.select_set(False)
                curve.select_set(True)
                context.view_layer.objects.active = curve
                return {"FINISHED"}
        except (ValueError, RuntimeError, ReferenceError) as error:
            self.report({"WARNING"}, str(error))
            return {"RUNNING_MODAL"}
        return {"PASS_THROUGH"}


class HSW_OT_update_surface_path_cutter(bpy.types.Operator):
    bl_idname = TOOL_UPDATE_OPERATOR
    bl_label = "Update Surface Path Cutter"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        item = context.object
        return item is not None and context.mode == "OBJECT" and (item.type == "CURVE" or _META in item)

    def execute(self, context):
        try:
            cutter = context.object
            if cutter.type == "CURVE":
                matches = [item for item in context.view_layer.objects
                           if _META in item and _record(item)["curve"] == cutter.name]
                if len(matches) != 1:
                    raise ValueError("Select the specific managed cutter for this curve")
                cutter = matches[0]
            record = _record(cutter)
            execute(bpy.data.objects.get(record["target"]), TOOL, {
                key: record[key] for key in ("curve", "direction", "margin", "operation")
            } | {"name": cutter.name, "update": True})
        except (ValueError, RuntimeError, TypeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class HSW_PG_surface_path(bpy.types.PropertyGroup):
    operation: EnumProperty(name="Output", items=_OPERATIONS, default="DIFFERENCE")
    direction_mode: EnumProperty(name="Projection", items=_DIRECTIONS, default="VIEW")
    direction: FloatVectorProperty(name="World Direction", default=(0, 0, -1))
    margin: FloatProperty(name="End Margin (BU)", default=0.25, min=1e-6, max=1e6)
    cutter_name: StringProperty(name="Cutter Name")


class HSW_PT_surface_path(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_hsw_surface_path"
    bl_label = "Surface Path Cutter"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"

    def draw(self, context):
        settings = context.scene.hsw_surface_path
        for prop in ("operation", "direction_mode", "margin", "cutter_name"):
            self.layout.prop(settings, prop)
        if settings.direction_mode == "CUSTOM":
            self.layout.prop(settings, "direction")
        path = self.layout.operator(TOOL_OPERATOR, text="Draw Closed Path")
        path.mode, path.direction_mode, path.direction = "PATH", settings.direction_mode, settings.direction
        cutter = self.layout.operator(TOOL_OPERATOR, text="Draw Path and Cutter")
        cutter.mode, cutter.operation = "CUTTER", settings.operation
        cutter.direction_mode, cutter.direction = settings.direction_mode, settings.direction
        cutter.margin, cutter.cutter_name = settings.margin, settings.cutter_name
        self.layout.operator(TOOL_UPDATE_OPERATOR)


_CLASSES = (HSW_OT_draw_surface_path, HSW_OT_update_surface_path_cutter, HSW_PG_surface_path, HSW_PT_surface_path)


def register():
    registered = []
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
        bpy.types.Scene.hsw_surface_path = PointerProperty(type=HSW_PG_surface_path)
    except Exception:
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise


def unregister():
    for handle in list(_HANDLES):
        bpy.types.SpaceView3D.draw_handler_remove(handle, "WINDOW")
    _HANDLES.clear()
    del bpy.types.Scene.hsw_surface_path
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
