"""Read-only viewport controls for reference-guided face-flow inspection."""

from dataclasses import dataclass, field
import json

import bmesh
import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy_extras import view3d_utils
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from .face_flow import inspect_face_flow, mesh_snapshot
from .face_flow_reference_panel import point_for
from .face_flow_scan import DEFAULT_SETTINGS


_SETTINGS = "hsw_face_flow"
_REFERENCE_FIELDS = ("source", "stage", "region", "landmark")
_MAX_PICK_FACES = 250_000
_PICK_DISTANCE = 12.0
_PICK_MARGIN = 2.0
_LAYERS = (
    ("CONTROL_CAGE", "源笼", "读取可编辑网格"),
    ("EVALUATED", "实际求值", "读取当前视口启用的修改器结果"),
)
_KINDS = (
    ("EDGE_LOOP", "边循环", "沿相连边追踪"),
    ("EDGE_RING", "边环", "通过四边面追踪对边"),
    ("FACE_STRIP", "面带", "以一张面与方向边追踪连续面带"),
)
_ELEMENTS = {"VERT": "点", "EDGE": "边", "FACE": "面"}
_MAX_EVALUATED_WIRE_EDGES = 100_000
_DRAW_COLORS = {
    "wire": (0.15, 0.17, 0.19, 1.0),
    "faces": (0.14, 0.55, 0.85, 0.75),
    "path": (0.12, 0.87, 1.0, 1.0),
    "candidates": (0.84, 0.35, 1.0, 1.0),
    "seed": (1.0, 0.76, 0.18, 1.0),
    "stops": (1.0, 0.26, 0.19, 1.0),
}


@dataclass
class _State:
    scope: tuple = ()
    layer: str = ""
    kind: str = ""
    mesh_sha256: str = ""
    seed: dict | None = None
    face_anchor: int | None = None
    report: dict | None = None
    primitives: dict = field(default_factory=dict)
    batches: list | None = None
    dirty: bool = False
    status: str = ""


_states = {}
_modal = {}
_registered = []
_draw_handle = None


def _settings(context):
    return getattr(context.window_manager, _SETTINGS)


def _target(context):
    obj = context.active_object
    if obj is None or obj.type != "MESH":
        raise ValueError("请选择活动网格。")
    space = context.space_data
    viewport = space if isinstance(space, bpy.types.SpaceView3D) else None
    if not obj.visible_get(view_layer=context.view_layer, viewport=viewport):
        raise ValueError("活动网格在当前视口不可见。")
    if _settings(context).layer == "EVALUATED" and context.mode != "OBJECT":
        raise ValueError("实际求值检查请先切换到对象模式。")
    return obj


def _scope(context, obj):
    return (
        context.scene.as_pointer(), context.view_layer.as_pointer(),
        obj.as_pointer(), obj.data.as_pointer(), context.scene.frame_current,
        context.scene.frame_subframe, context.mode, context.scene.unit_settings.scale_length,
    )


def _redraw(window):
    if window and window.screen:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _clear_data(state, message):
    state.scope = ()
    state.mesh_sha256 = ""
    state.seed = None
    state.face_anchor = None
    state.report = None
    state.primitives.clear()
    state.batches = None
    state.dirty = False
    state.status = message


def _state_for(context, obj=None):
    state = _states.setdefault(context.window.as_pointer(), _State())
    obj = obj if obj is not None else context.active_object
    if state.scope and (obj is None or obj.type != "MESH"
                        or state.scope != _scope(context, obj)
                        or state.layer != _settings(context).layer
                        or state.kind != _settings(context).kind):
        _clear_data(state, "对象、模式、帧或网格层已变更，请重新定位。")
    return state


def _refresh_state(context, obj, state):
    snapshot = mesh_snapshot(context, obj, _settings(context).layer)
    if state.mesh_sha256 and snapshot.mesh_sha256 != state.mesh_sha256:
        _clear_data(state, "网格、隐藏状态或变换已变更，请重新定位。")
    state.dirty = False
    return snapshot


def _config_changed(self, context):
    if context.window is None:
        return
    for state in _states.values():
        if state.scope:
            _clear_data(state, "网格层或追踪方式已变更，请重新定位。")
    for window in context.window_manager.windows:
        _redraw(window)


def _clear_reference_result(state):
    if state.report is not None:
        state.report = None
        state.primitives = {group: value for group, value in state.primitives.items() if group == "seed"}
        state.batches = None
        state.status = "参考标注或检查参数已变更，请重新检查。"


def _reference_changed(self, context):
    if context.window is not None:
        for state in _states.values():
            _clear_reference_result(state)
        for window in context.window_manager.windows:
            _redraw(window)


def _reference_point_matches(state, point):
    reference = (state.report or {}).get("reference")
    return not reference or reference.get("image_point") == point


def _reference(settings):
    return {key: getattr(settings, key).strip() for key in _REFERENCE_FIELDS}


class HSW_PG_face_flow(bpy.types.PropertyGroup):
    layer: EnumProperty(name="网格层", items=_LAYERS, update=_config_changed)
    kind: EnumProperty(name="追踪方式", items=_KINDS, update=_config_changed)
    source: StringProperty(name="原参考", description="视频、文件或同工序原帧", maxlen=512, update=_reference_changed)
    stage: StringProperty(name="工序", description="参考中的步骤或时间码", maxlen=512, update=_reference_changed)
    region: StringProperty(name="部位", description="要核对的实际曲面或结构", maxlen=512, update=_reference_changed)
    landmark: StringProperty(name="地标", description="参考和本件可共同定位的特征", maxlen=512, update=_reference_changed)
    edge_index: IntProperty(name="边索引", default=-1, min=-1)
    face_index: IntProperty(name="面索引", default=-1, min=-1)
    max_steps: IntProperty(name="追踪步数上限", default=1024, min=1, max=4096, update=_reference_changed)
    show_scan_settings: BoolProperty(name="预扫阈值")
    max_elements: IntProperty(name="元素预算", min=1, default=DEFAULT_SETTINGS["max_elements"],
                              update=_reference_changed)
    max_candidates: IntProperty(name="候选上限", min=1, soft_max=500, default=DEFAULT_SETTINGS["max_candidates"],
                                update=_reference_changed)
    coincidence_mm: FloatProperty(name="重合容差 (mm)", min=1e-6, precision=6,
                                  default=DEFAULT_SETTINGS["coincidence_mm"], update=_reference_changed)
    max_angle_deg: FloatProperty(name="转角阈值 (°)", min=1e-6, max=180,
                                 default=DEFAULT_SETTINGS["max_angle_deg"], update=_reference_changed)
    max_spacing_ratio: FloatProperty(name="间距倍数", min=1.001,
                                     default=DEFAULT_SETTINGS["max_spacing_ratio"], update=_reference_changed)


def _selected_index(elements, active, element_type):
    active_only = isinstance(active, element_type) and active.select and not active.hide
    selected = []
    for index, element in enumerate(elements):
        if active_only and active == element:
            return index
        if element.select and not element.hide:
            selected.append(index)
    if len(selected) != 1:
        raise ValueError("请保留一个实选元素，或明确最后激活的元素。")
    return selected[0]


def _active_index(obj, domain):
    if obj.mode != "EDIT":
        raise ValueError("请在活动网格的编辑模式实选种子。")
    bm = bmesh.from_edit_mesh(obj.data)
    if domain == "FACE":
        return _selected_index(bm.faces, bm.faces.active, bmesh.types.BMFace)
    return _selected_index(bm.edges, bm.select_history.active, bmesh.types.BMEdge)


def _require_element(snapshot, domain, index):
    items = snapshot.edges if domain == "EDGE" else snapshot.faces
    hidden = snapshot.hidden_edges if domain == "EDGE" else snapshot.hidden_faces
    if not 0 <= index < len(items) or index in hidden:
        raise ValueError(f"无效或隐藏的{_ELEMENTS[domain]}索引 {index}。")
    if domain == "FACE" and any(edge in snapshot.hidden_edges for edge in snapshot.face_edges[index]):
        raise ValueError(f"面 {index} 的边界包含隐藏边。")
    if any(vertex in snapshot.hidden_verts for vertex in items[index]):
        raise ValueError(f"{_ELEMENTS[domain]} {index} 包含隐藏顶点。")


def _set_binding(context, obj, state, snapshot):
    state.scope = _scope(context, obj)
    state.layer = _settings(context).layer
    state.kind = _settings(context).kind
    state.mesh_sha256 = snapshot.mesh_sha256
    state.dirty = False


def _set_seed(context, obj, state, snapshot, edge, face, method):
    _require_element(snapshot, "EDGE", edge)
    settings = _settings(context)
    if settings.kind == "FACE_STRIP":
        if face is None:
            raise ValueError("请先确认种子面，再指定它的一条方向边。")
        _require_element(snapshot, "FACE", face)
        if edge not in snapshot.face_edges[face]:
            raise ValueError("方向边必须属于已选种子面。")
    seed = {"kind": settings.kind, "edge": edge, "method": method}
    if face is not None:
        seed["face"] = face
    _set_binding(context, obj, state, snapshot)
    state.seed, state.face_anchor, state.report = seed, face, None
    state.status = "种子已绑定实际网格，请核对高亮后追踪。"
    settings.edge_index = edge
    settings.face_index = face if face is not None else -1
    _set_primitives(state, snapshot)
    _redraw(context.window)


class HSW_OT_face_flow_capture(bpy.types.Operator):
    bl_idname = "hsw.face_flow_capture"
    bl_label = "记录面流种子"

    method: EnumProperty(items=(
        ("ACTIVE", "实选边", "使用已选活动边"),
        ("FACE", "实选面", "锁定已选活动面"),
        ("INDICES", "指定索引", "在当前快照验证并绑定手输索引"),
    ))

    def execute(self, context):
        try:
            obj = _target(context)
            state = _state_for(context, obj)
            settings = _settings(context)
            if self.method != "INDICES" and settings.layer != "CONTROL_CAGE":
                raise ValueError("实际求值层请点选或核对其独立索引。")
            snapshot = _refresh_state(context, obj, state)
            if self.method == "FACE":
                if settings.kind != "FACE_STRIP":
                    raise ValueError("只有面带需要先记录种子面。")
                face = _active_index(obj, "FACE")
                _require_element(snapshot, "FACE", face)
                _set_binding(context, obj, state, snapshot)
                state.face_anchor, state.seed, state.report = face, None, None
                state.status = f"种子面 {face} 已锁定，请实选它的方向边。"
                settings.face_index, settings.edge_index = face, -1
                _set_primitives(state, snapshot)
                _redraw(context.window)
            else:
                face = None
                if self.method == "ACTIVE":
                    edge = _active_index(obj, "EDGE")
                    if settings.kind == "FACE_STRIP":
                        face = state.face_anchor
                    method = "SOURCE_ACTIVE"
                else:
                    edge = settings.edge_index
                    if settings.kind == "FACE_STRIP":
                        face = settings.face_index
                    method = "EXPLICIT_INDICES"
                _set_seed(context, obj, state, snapshot, edge, face, method)
        except (ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


def _window_region(area):
    regions = [region for region in area.regions if region.type == "WINDOW"]
    if len(regions) != 1 or area.spaces.active.region_quadviews:
        raise ValueError("请退出四分视图后点选。")
    return regions[0]


def _screen_distance(point, left, right):
    delta = right - left
    if delta.length_squared < 1e-10:
        return None
    fraction = max(0.0, min(1.0, (point - left).dot(delta) / delta.length_squared))
    return (point - left - fraction * delta).length


def _edge_distance(snapshot, region, view, point, edge):
    screens = [view3d_utils.location_3d_to_region_2d(region, view, Vector(snapshot.world[vertex]))
               for vertex in snapshot.edges[edge]]
    if all(position is not None for position in screens):
        return _screen_distance(point, *screens)
    return None


def _nearest_edge(snapshot, region, view, point, face, *, required_edge=None):
    distances = []
    for index in snapshot.face_edges[face]:
        if index in snapshot.hidden_edges:
            continue
        distance = _edge_distance(snapshot, region, view, point, index)
        if distance is not None:
            distances.append((distance, index))
    distances.sort()
    if not distances or distances[0][0] > _PICK_DISTANCE:
        raise ValueError("光标离实际边太远，请放大后贴近边的中段点击。")
    distance, index = distances[0]
    if (len(distances) > 1 and distances[1][0] - distance <= _PICK_MARGIN
            and distances[1][0] <= _PICK_DISTANCE):
        raise ValueError("多条边距离过近，请放大、避开顶点，或核对索引。")
    if required_edge is not None and required_edge != index:
        raise ValueError("所选位置同时邻接不同方向边，请回到已点选面的可见侧。")
    return index


def _nearby_faces(tree, visible_faces, position, tolerance):
    return {visible_faces[index] for _, _, index, _ in tree.find_nearest_range(position, tolerance)}


def _verify_edge_pick(snapshot, tree, visible_faces, edge):
    endpoints = tuple(Vector(snapshot.world[index]) for index in snapshot.edges[edge])
    midpoint = (endpoints[0] + endpoints[1]) / 2
    tolerance = max(1e-6, midpoint.length * 2e-7)
    incident = set(snapshot.edge_faces[edge])
    nearby = _nearby_faces(tree, visible_faces, midpoint, tolerance) - incident
    others = {index for face in nearby for index in snapshot.face_edges[face]}
    for other in others:
        opposite = tuple(Vector(snapshot.world[index]) for index in snapshot.edges[other])
        if any(all((a - b).length <= tolerance for a, b in zip(endpoints, pair))
               for pair in (opposite, opposite[::-1])):
            raise ValueError("此处有同位分离边，请先核对实际边索引。")


def _pick_face(context, obj, region, view, point, tree, visible_faces):
    clip_end = context.space_data.clip_end
    origin = view3d_utils.region_2d_to_origin_3d(region, view, point, clamp=clip_end)
    direction = view3d_utils.region_2d_to_vector_3d(region, view, point)
    position, _, local_index, _ = tree.ray_cast(origin, direction, 2 * clip_end)
    if local_index is None:
        raise ValueError("没有命中实际求值表面，请从可见面内侧点击。")
    clip = view.perspective_matrix @ position.to_4d()
    if clip.w <= 0 or not -clip.w <= clip.z <= clip.w:
        raise ValueError("命中面位于视口裁切范围外，请调整视图。")
    # The snapshot supplies evaluated polygon IDs. Scene ray casting checks occlusion only.
    depsgraph = context.evaluated_depsgraph_get()
    tolerance = max(1e-6, 2e-7 * (position - origin).length, 2e-7 * position.length)
    cursor = origin
    for _ in range(16):
        remaining = (position - cursor).dot(direction) - tolerance
        if remaining <= 0:
            break
        hit, location, _, _, blocker, _ = context.scene.ray_cast(
            depsgraph, cursor, direction, distance=remaining)
        if not hit:
            break
        if blocker.original == obj or blocker.visible_get(
                view_layer=context.view_layer, viewport=context.space_data):
            raise ValueError("该求值表面被前方几何遮住，请转到可见侧。")
        cursor = location + tolerance * direction
    else:
        raise ValueError("无法从当前视角确认遮挡，请隔离目标或改用核过的索引。")
    primary = visible_faces[local_index]
    nearby = _nearby_faces(tree, visible_faces, position, tolerance)
    return (primary, *sorted(nearby - {primary}))


class HSW_OT_face_flow_pick(bpy.types.Operator):
    bl_idname = "hsw.face_flow_pick"
    bl_label = "在视口点选求值种子"

    def invoke(self, context, event):
        try:
            obj = _target(context)
            settings = _settings(context)
            if settings.layer != "EVALUATED":
                raise ValueError("源笼请在编辑模式实选。")
            if context.area is None or context.area.type != "VIEW_3D":
                raise ValueError("请从 3D 视图面板点选。")
            space = context.space_data
            if space.shading.type != "SOLID" or space.shading.show_xray:
                raise ValueError("请在实体视图关闭 X-Ray 后点选。")
            self.region = _window_region(context.area)
            self.view = space.region_3d
            if self.view.use_clip_planes:
                raise ValueError("请取消视口裁切区域后点选。")
            window_key = context.window.as_pointer()
            if window_key in _modal:
                raise ValueError("请先结束本窗口正在进行的点选。")
            self.snapshot = mesh_snapshot(context, obj, "EVALUATED")
            if len(self.snapshot.faces) > _MAX_PICK_FACES:
                raise ValueError("实际求值面的数量超过点选预算，请核对索引后绑定。")
            self.visible_faces = [index for index, corners in enumerate(self.snapshot.faces)
                                  if index not in self.snapshot.hidden_faces
                                  and not self.snapshot.hidden_verts.intersection(corners)]
            if not self.visible_faces:
                raise ValueError("实际求值层没有可用表面，请核对索引。")
            self.tree = BVHTree.FromPolygons(
                self.snapshot.world, [self.snapshot.faces[index] for index in self.visible_faces])
            self.window, self.area, self.object = context.window, context.area, obj
            self.scope, self.kind = _scope(context, obj), settings.kind
            self.face = None
            _modal[window_key] = self
            self._status(context)
            context.window_manager.modal_handler_add(self)
        except (ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def _status(self, context):
        tip = "先点击种子面中部" if self.kind == "FACE_STRIP" and self.face is None else "点击目标边中段"
        context.workspace.status_text_set(f"{tip} · Esc 或右键取消")

    def cancel(self, context):
        key = self.window.as_pointer()
        if _modal.get(key) is self:
            del _modal[key]
            if self.window.workspace:
                self.window.workspace.status_text_set(None)
        _redraw(self.window)

    def modal(self, context, event):
        if (event.type == "WINDOW_DEACTIVATE"
                or (event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS")
                or context.window != self.window
                or context.area != self.area
                or _modal.get(self.window.as_pointer()) is not self):
            self.cancel(context)
            return {"CANCELLED"}
        if event.type != "LEFTMOUSE" or event.value != "PRESS":
            return {"PASS_THROUGH"}
        point = Vector((event.mouse_x - self.region.x, event.mouse_y - self.region.y))
        if not 0 <= point.x < self.region.width or not 0 <= point.y < self.region.height:
            return {"RUNNING_MODAL"}
        try:
            obj = _target(context)
            settings = _settings(context)
            if (obj != self.object or self.scope != _scope(context, obj)
                    or settings.layer != "EVALUATED" or settings.kind != self.kind):
                raise RuntimeError("点选期间对象或检查上下文已变更，请重新开始。")
            snapshot = mesh_snapshot(context, obj, "EVALUATED")
            if snapshot.mesh_sha256 != self.snapshot.mesh_sha256:
                raise RuntimeError("点选期间实际求值网格已变更，请重新开始。")
            hit_faces = _pick_face(context, obj, self.region, self.view, point, self.tree, self.visible_faces)
            hit_face = hit_faces[0]
            if self.kind == "FACE_STRIP" and self.face is None:
                if len(hit_faces) != 1:
                    raise ValueError("命中重合表面，请核对实际面索引。")
                for edge in snapshot.face_edges[hit_face]:
                    distance = _edge_distance(snapshot, self.region, self.view, point, edge)
                    if distance is None or distance <= _PICK_MARGIN:
                        raise ValueError("靠近多面共边或面边界未完整投影，请点在种子面中部。")
                self.face = hit_face
                self._status(context)
                return {"RUNNING_MODAL"}
            edge = _nearest_edge(snapshot, self.region, self.view, point, hit_face)
            for nearby in hit_faces[1:]:
                _nearest_edge(snapshot, self.region, self.view, point, nearby, required_edge=edge)
            _verify_edge_pick(snapshot, self.tree, self.visible_faces, edge)
            if self.face is not None and self.face != hit_face:
                edge = _nearest_edge(snapshot, self.region, self.view, point, self.face, required_edge=edge)
            _set_seed(context, obj, _state_for(context, obj), snapshot, edge, self.face, "EVALUATED_VIEWPORT")
        except ValueError as error:
            self.report({"WARNING"}, str(error))
            return {"RUNNING_MODAL"}
        except RuntimeError as error:
            self.cancel(context)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.cancel(context)
        return {"FINISHED"}


def _mean(world, indices):
    points = [world[index] for index in indices]
    return tuple(sum(point[axis] for point in points) / len(points) for axis in range(3))


def _add_marker(primitives, mesh, group, domain, index):
    if type(index) is not int or index < 0:
        return
    lines, points = primitives[group]
    if domain == "EDGE" and index < len(mesh.edges):
        lines.extend(mesh.world[vertex] for vertex in mesh.edges[index])
        if group in {"stops", "candidates"}:
            points.append(_mean(mesh.world, mesh.edges[index]))
    elif domain == "VERT" and index < len(mesh.world):
        points.append(mesh.world[index])
    elif domain == "FACE" and index < len(mesh.faces):
        for edge in mesh.face_edges[index]:
            lines.extend(mesh.world[vertex] for vertex in mesh.edges[edge])
        if group in {"stops", "candidates"}:
            points.append(_mean(mesh.world, mesh.faces[index]))


def _set_primitives(state, mesh):
    primitives = {group: ([], []) for group in _DRAW_COLORS}
    if state.layer == "EVALUATED" and len(mesh.edges) <= _MAX_EVALUATED_WIRE_EDGES:
        wire, _ = primitives["wire"]
        for index, (start, end) in enumerate(mesh.edges):
            if index not in mesh.hidden_edges and start not in mesh.hidden_verts and end not in mesh.hidden_verts:
                wire.extend((mesh.world[start], mesh.world[end]))
    report = state.report or {}
    trace = report.get("trace")
    if trace:
        if state.kind != "EDGE_LOOP":
            for face in trace.get("faces", ()):
                _add_marker(primitives, mesh, "faces", "FACE", face)
        for edge in trace.get("edges", ()):
            _add_marker(primitives, mesh, "path", "EDGE", edge)
        for stop in trace.get("stops", ()):
            _add_marker(primitives, mesh, "stops", stop.get("domain"), stop.get("index"))
    for candidate in (report.get("scan") or {}).get("candidates", ()):
        domain = candidate.get("domain")
        _add_marker(primitives, mesh, "candidates", domain, candidate.get("index"))
        if candidate.get("kind") == "COINCIDENT_SPLIT_EDGES":
            for index in candidate["evidence"].get("edges", ()):
                if index != candidate.get("index"):
                    _add_marker(primitives, mesh, "candidates", "EDGE", index)
    if state.face_anchor is not None:
        _add_marker(primitives, mesh, "seed", "FACE", state.face_anchor)
    if state.seed is not None:
        _add_marker(primitives, mesh, "seed", "EDGE", state.seed["edge"])
    state.primitives = primitives
    state.batches = None


def _view_evidence(context):
    space = context.space_data
    if not isinstance(space, bpy.types.SpaceView3D) or space.region_3d is None:
        return None
    view = space.region_3d
    return {
        "shading": space.shading.type,
        "overlays": space.overlay.show_overlays,
        "wireframes": space.overlay.show_wireframes,
        "view_perspective": view.view_perspective,
        "view_matrix": [list(row) for row in view.view_matrix],
        "perspective_matrix": [list(row) for row in view.perspective_matrix],
    }


class HSW_OT_face_flow_run(bpy.types.Operator):
    bl_idname = "hsw.face_flow_run"
    bl_label = "检查面流"

    operation: EnumProperty(items=(
        ("TRACE", "追踪", "从已绑定种子追踪"),
        ("SCAN", "全网预扫", "列出待核实的候选和覆盖范围"),
    ))

    def execute(self, context):
        try:
            obj = _target(context)
            settings = _settings(context)
            state = _state_for(context, obj)
            snapshot = _refresh_state(context, obj, state)
            request = {"layer": settings.layer, "mesh_sha256": snapshot.mesh_sha256}
            reference = _reference(settings)
            if all(reference.values()):
                point = point_for(context)
                if point is not None:
                    reference["image_point"] = point
                request["reference"] = reference
            if self.operation == "TRACE":
                if state.seed is None:
                    raise ValueError("请先定位并绑定当前网格层的种子。")
                if not all(reference.values()):
                    raise ValueError("请填写原参考、工序、部位和地标。")
                request["trace"] = {key: value for key, value in state.seed.items() if key != "method"}
                request["trace"]["max_steps"] = settings.max_steps
            else:
                request["scan"] = {key: getattr(settings, key) for key in DEFAULT_SETTINGS}
            report = inspect_face_flow(context, obj, request)
            if report["identity"]["mesh_sha256"] != snapshot.mesh_sha256:
                raise RuntimeError("检查期间网格已变更，请重新定位。")
            evidence = _view_evidence(context)
            if evidence is not None:
                report["viewport_evidence"] = evidence
            if self.operation == "TRACE":
                report["seed_method"] = state.seed["method"]
            _set_binding(context, obj, state, snapshot)
            state.report = report
            _set_primitives(state, snapshot)
            if evidence is not None and settings.layer == "EVALUATED":
                evidence["actual_layer_wire"] = {
                    "mesh_edges": len(snapshot.edges),
                    "visible_edges": len(state.primitives["wire"][0]) // 2,
                    "edge_budget": _MAX_EVALUATED_WIRE_EDGES,
                    "status": "over_budget" if len(snapshot.edges) > _MAX_EVALUATED_WIRE_EDGES
                              else "shown" if evidence["overlays"] and evidence["wireframes"]
                              else "overlay_disabled",
                }
            state.status = ("追踪已记录，核对参考与停点。" if self.operation == "TRACE"
                            else "预扫已记录，请按实际结构核实候选。")
            _redraw(context.window)
        except (ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class HSW_OT_face_flow_copy(bpy.types.Operator):
    bl_idname = "hsw.face_flow_copy"
    bl_label = "复制检查 JSON"

    def execute(self, context):
        try:
            obj = _target(context)
            state = _state_for(context, obj)
            if state.report is not None:
                _refresh_state(context, obj, state)
                if not _reference_point_matches(state, point_for(context)):
                    _clear_reference_result(state)
            if state.report is None:
                raise ValueError("当前网格层没有可复制的有效报告。")
            context.window_manager.clipboard = json.dumps(
                state.report, ensure_ascii=False, allow_nan=False, indent=2)
        except (ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, "面流检查 JSON 已复制。")
        return {"FINISHED"}


class HSW_OT_face_flow_clear(bpy.types.Operator):
    bl_idname = "hsw.face_flow_clear"
    bl_label = "清除种子与高亮"

    def execute(self, context):
        state = _state_for(context)
        _clear_data(state, "已清除当前窗口的面流检查。")
        settings = _settings(context)
        settings.edge_index = settings.face_index = -1
        _redraw(context.window)
        return {"FINISHED"}


class HSW_OT_face_flow_wire(bpy.types.Operator):
    bl_idname = "hsw.face_flow_wire"
    bl_label = "打开实体线框叠加"

    @classmethod
    def poll(cls, context):
        return isinstance(context.space_data, bpy.types.SpaceView3D)

    def execute(self, context):
        space = context.space_data
        space.shading.type = "SOLID"
        space.shading.show_xray = False
        space.overlay.show_overlays = True
        space.overlay.show_wireframes = True
        space.overlay.wireframe_threshold = 1.0
        context.area.tag_redraw()
        return {"FINISHED"}


def _valid_visible_state(context):
    window = context.window
    space = context.space_data
    if window is None or not isinstance(space, bpy.types.SpaceView3D):
        return None
    state = _states.get(window.as_pointer())
    if state is None or not state.scope or state.dirty or not state.primitives:
        return None
    if state.report is not None and state.report.get("reference") and not _reference_point_matches(state, point_for(context)):
        return None
    obj = context.active_object
    if (obj is None or obj.type != "MESH" or state.scope != _scope(context, obj)
            or state.layer != _settings(context).layer
            or state.kind != _settings(context).kind):
        return None
    if not space.overlay.show_overlays or not obj.visible_get(
            view_layer=context.view_layer, viewport=space):
        return None
    return state


def _build_batches(state):
    import gpu
    from gpu_extras.batch import batch_for_shader

    line_shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    point_shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batches = []
    for group, (lines, points) in state.primitives.items():
        if lines:
            batches.append((group, True, line_shader,
                            batch_for_shader(line_shader, "LINES", {"pos": lines})))
        if points:
            batches.append((group, False, point_shader,
                            batch_for_shader(point_shader, "POINTS", {"pos": points})))
    return batches


def _draw_highlights():
    import gpu

    context = bpy.context
    state = _valid_visible_state(context)
    if state is None:
        return
    depth = gpu.state.depth_test_get()
    depth_mask = gpu.state.depth_mask_get()
    try:
        if state.batches is None:
            state.batches = _build_batches(state)
        gpu.state.depth_test_set("LESS_EQUAL")
        gpu.state.depth_mask_set(False)
        for group, is_line, shader, batch in state.batches:
            if group == "wire" and not context.space_data.overlay.show_wireframes:
                continue
            shader.bind()
            shader.uniform_float("color", _DRAW_COLORS[group])
            if is_line:
                shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
                width = 1.0 if group == "wire" else 2.0 if group == "faces" else 4.0
                shader.uniform_float("lineWidth", width)
            else:
                gpu.state.point_size_set(9.0)
            batch.draw(shader)
    finally:
        gpu.state.point_size_set(1.0)
        gpu.state.depth_test_set(depth)
        gpu.state.depth_mask_set(depth_mask)


def _check_pending():
    windows = {window.as_pointer(): window for wm in bpy.data.window_managers for window in wm.windows}
    for key, state in list(_states.items()):
        window = windows.get(key)
        if window is None:
            del _states[key]
            continue
        if not state.dirty:
            continue
        area = next((area for area in window.screen.areas if area.type == "VIEW_3D"), None)
        if area is None:
            continue
        region = next((region for region in area.regions if region.type == "WINDOW"), None)
        with bpy.context.temp_override(window=window, area=area, region=region):
            context = bpy.context
            _state_for(context)
            if state.scope:
                try:
                    _refresh_state(context, context.active_object, state)
                except (ValueError, RuntimeError, ReferenceError):
                    _clear_data(state, "无法复核当前网格，请重新定位。")
        _redraw(window)
    return None


@persistent
def _notice_update(scene, depsgraph):
    if not _states:
        return
    geometry_changes = set()
    for update in depsgraph.updates:
        if update.is_updated_geometry or update.is_updated_transform:
            item = update.id.original
            if item is not None:
                geometry_changes.add(item.as_pointer())
    dirty = False
    for state in _states.values():
        if not state.scope or state.scope[0] != scene.as_pointer():
            continue
        if geometry_changes and (state.layer == "EVALUATED"
                                 or geometry_changes.intersection(state.scope[2:4])):
            state.dirty = True
            dirty = True
    if dirty and not bpy.app.timers.is_registered(_check_pending):
        bpy.app.timers.register(_check_pending)


@persistent
def _drop_runtime(*args):
    for operator in list(_modal.values()):
        operator.cancel(bpy.context)
    _modal.clear()
    _states.clear()
    if bpy.app.timers.is_registered(_check_pending):
        bpy.app.timers.unregister(_check_pending)


def _draw_trace(layout, trace):
    box = layout.box()
    state_text = "闭合" if trace["closed"] else "未闭合"
    box.label(text=f"{state_text} · {trace['status']}")
    for key, text in (("edges", "边"), ("faces", "面")):
        indices = trace.get(key, ())
        if not indices:
            continue
        prefix = " → ".join(map(str, indices[:6]))
        suffix = " → …" if len(indices) > 6 else ""
        box.label(text=f"{len(indices)} {text} · {prefix}{suffix}")
    for stop in trace.get("stops", ()):
        element = _ELEMENTS.get(stop.get("domain"), "")
        box.label(text=f"{stop.get('side', '')} {element} {stop.get('index', '')} · {stop['reason']}")
    box.label(text="青色路径 · 橙色种子 · 红色停点")


def _draw_scan(layout, scan):
    box = layout.box()
    candidates = scan.get("candidates", ())
    box.label(text=f"候选 {len(candidates)} · {scan['status']}")
    for candidate in candidates[:4]:
        element = _ELEMENTS.get(candidate.get("domain"), "")
        box.label(text=f"{element} {candidate.get('index', '')} · {candidate['kind']}")
    omitted = scan.get("omitted_candidate_count", 0)
    if omitted:
        box.label(text=f"另有 {omitted} 个候选未显示")
    for reason in scan.get("coverage", {}).get("unverified", ())[:3]:
        box.label(text=f"未覆盖 · {reason}")
    box.label(text="紫色候选 · JSON 含依据与覆盖范围")


class HSW_PT_face_flow(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_hsw_face_flow"
    bl_label = "面流检查"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        state = _state_for(context)
        layout.prop(settings, "layer")
        reference = layout.column(align=True)
        for field_name in _REFERENCE_FIELDS:
            reference.prop(settings, field_name)
        point = point_for(context)
        if not _reference_point_matches(state, point):
            _clear_reference_result(state)
        if point:
            x, y = point["pixel_xy"]
            row = reference.row(align=True)
            row.label(text=f"参考图像素 ({x}, {y})", icon="IMAGE_DATA")
            row.operator("hsw.reference_image_clear", text="", icon="X")
        else:
            reference.label(text="可在图像编辑器点标参考地标", icon="IMAGE_DATA")
        layout.prop(settings, "kind")
        if settings.layer == "CONTROL_CAGE":
            if settings.kind == "FACE_STRIP":
                layout.operator(HSW_OT_face_flow_capture.bl_idname, text="锁定实选面").method = "FACE"
            layout.operator(HSW_OT_face_flow_capture.bl_idname, text="绑定实选边").method = "ACTIVE"
        else:
            pick = layout.row()
            pick.operator_context = "INVOKE_REGION_WIN"
            pick.operator(HSW_OT_face_flow_pick.bl_idname, icon="EYEDROPPER")
        indices = layout.row(align=True)
        if settings.kind == "FACE_STRIP":
            indices.prop(settings, "face_index")
        indices.prop(settings, "edge_index")
        layout.operator(HSW_OT_face_flow_capture.bl_idname, text="核对并绑定索引").method = "INDICES"
        if state.seed is not None:
            face = state.seed.get("face")
            prefix = f"面 {face} · " if face is not None else ""
            layout.label(text=f"已绑定 {prefix}边 {state.seed['edge']}")
        layout.prop(settings, "max_steps")
        run = layout.row(align=True)
        run.operator(HSW_OT_face_flow_run.bl_idname, text="追踪").operation = "TRACE"
        run.operator(HSW_OT_face_flow_run.bl_idname, text="全网预扫").operation = "SCAN"
        layout.prop(settings, "show_scan_settings", toggle=True)
        if settings.show_scan_settings:
            scan_settings = layout.column(align=True)
            for key in DEFAULT_SETTINGS:
                scan_settings.prop(settings, key)
        layout.operator(HSW_OT_face_flow_wire.bl_idname, icon="SHADING_SOLID")
        space = context.space_data
        if (state.primitives and (space.shading.type != "SOLID" or space.shading.show_xray
                                  or not space.overlay.show_overlays or not space.overlay.show_wireframes)):
            layout.label(text="检查图请开启实体线框叠加。", icon="ERROR")
        if state.dirty:
            layout.label(text="正在复核网格身份。", icon="TIME")
        elif state.status:
            layout.label(text=state.status, icon="INFO")
        if state.report is not None and not state.dirty:
            wire = state.report.get("viewport_evidence", {}).get("actual_layer_wire")
            if wire and wire["status"] == "over_budget":
                layout.label(text="实际求值线框超预算，请隔离目标后再核对。", icon="ERROR")
            if state.report.get("trace"):
                _draw_trace(layout, state.report["trace"])
            if state.report.get("scan"):
                _draw_scan(layout, state.report["scan"])
        row = layout.row(align=True)
        copy = row.row(align=True)
        copy.enabled = state.report is not None and not state.dirty
        copy.operator(HSW_OT_face_flow_copy.bl_idname, icon="COPYDOWN")
        row.operator(HSW_OT_face_flow_clear.bl_idname, icon="X")


_CLASSES = (
    HSW_PG_face_flow, HSW_OT_face_flow_capture, HSW_OT_face_flow_pick,
    HSW_OT_face_flow_run, HSW_OT_face_flow_copy, HSW_OT_face_flow_clear,
    HSW_OT_face_flow_wire, HSW_PT_face_flow,
)
_HANDLERS = (
    (bpy.app.handlers.load_pre, _drop_runtime),
    (bpy.app.handlers.undo_post, _drop_runtime),
    (bpy.app.handlers.redo_post, _drop_runtime),
    (bpy.app.handlers.depsgraph_update_post, _notice_update),
)


def register():
    global _draw_handle
    if _registered:
        return
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _registered.append(cls)
        setattr(bpy.types.WindowManager, _SETTINGS, PointerProperty(type=HSW_PG_face_flow, options={"SKIP_SAVE"}))
        for handlers, callback in _HANDLERS:
            handlers.append(callback)
        if not bpy.app.background:
            _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_highlights, (), "WINDOW", "POST_VIEW")
    except Exception:
        unregister()
        raise


def unregister():
    global _draw_handle
    _drop_runtime()
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
        _draw_handle = None
    for handlers, callback in _HANDLERS:
        if callback in handlers:
            handlers.remove(callback)
    if hasattr(bpy.types.WindowManager, _SETTINGS):
        delattr(bpy.types.WindowManager, _SETTINGS)
    while _registered:
        bpy.utils.unregister_class(_registered.pop())
