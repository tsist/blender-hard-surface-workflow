"""Viewport preview and explicit commit for one managed surface sweep."""
from dataclasses import dataclass
import json
from time import monotonic
import textwrap

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty
from mathutils import Vector

from . import surface_sweep

_TEXT = "hsw_surface_sweep_text"
_DEBOUNCE = 0.18
_ERRORS = (ValueError, RuntimeError, ReferenceError, TypeError, KeyError)


@dataclass
class _Preview:
    scope: tuple
    target_ids: tuple = ()
    recipe_key: str = ""
    report: dict | None = None
    message: str = ""
    geometry: tuple | None = None
    batch: tuple | None = None
    state: str = ""


_states = {}
_pending = {}
_draft_outputs = {}
_suspended = {}
_draw_handle = None


def _managed(active):
    return surface_sweep.find_output(active) if active is not None else None


def _scope(scene, layer, focus):
    if focus is None:
        return ()
    return (scene.as_pointer(), layer.as_pointer(), scene.frame_current,
            scene.frame_subframe, focus.as_pointer())


def _request(context, managed):
    if managed is not None:
        target, params = surface_sweep.params_from_controls(managed)
        return target, params
    target = context.active_object
    if target is None or target.type != "MESH":
        raise ValueError("请选择母壳网格或者已提交的扫掠与站位控件。")
    name = getattr(context.window_manager, _TEXT)
    document = bpy.data.texts.get(name)
    if document is None:
        raise ValueError("请先选择保存 JSON 配方的文本数据块。")
    try:
        params = json.loads(document.as_string())
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON 第 {error.lineno} 行、第 {error.colno} 列：{error.msg}") from error
    if not isinstance(params, dict):
        raise ValueError("扫掠配方须为 JSON 对象。")
    if "op" in params:
        if (params.get("op"), params.get("tool"), params.get("object")) != (
            "hard_surface.stack", surface_sweep.TOOL, target.name
        ):
            raise ValueError("配方动作或对象与当前活动母壳不一致。")
        params = params.get("params")
        if not isinstance(params, dict):
            raise ValueError("params 须为 JSON 对象。")
    return target, params


def _key(params):
    return json.dumps(params, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _focus(context):
    active = context.active_object
    output = _managed(active)
    return output, _scope(context.scene, context.view_layer, output or active)


def _target_ids(target):
    return target.as_pointer(), target.data.as_pointer()


def _remember_output(scene, output):
    _draft_outputs[(scene.as_pointer(), output.as_pointer())] = (scene, output)


def _forget_output(scene, output):
    if output is not None:
        _draft_outputs.pop((scene.as_pointer(), output.as_pointer()), None)


def _redraw(window):
    if window and window.screen:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _set_state(context, state):
    window = context.window
    _states[window.as_pointer()] = state
    _pending.pop(window.as_pointer(), None)
    _redraw(window)


def _error(context, scope, error):
    _set_state(context, _Preview(scope, message=str(error), state="ERROR"))


def _edge_indices(faces):
    edges = set()
    for face in faces:
        for a, b in zip(face, (*face[1:], face[0]), strict=True):
            edges.add((a, b) if a < b else (b, a))
    return sorted(edges)


def _preview(context, managed, scope):
    if managed is not None:
        _remember_output(context.scene, managed)
    try:
        target, params = _request(context, managed)
        key = _key(params)
        candidate = surface_sweep.preview_geometry(target, params)
        state = _Preview(scope, _target_ids(target), key,
                         candidate["report"], geometry=(candidate["vertices"], _edge_indices(candidate["faces"])),
                         state="PREVIEW")
    except _ERRORS as error:
        _error(context, scope, error)
        return str(error)
    _set_state(context, state)
    return ""


def _can_operate(context):
    return context.mode == "OBJECT" and context.active_object is not None


class HSW_OT_surface_sweep_preview(bpy.types.Operator):
    bl_idname = "hard_surface.surface_sweep_preview"
    bl_label = "预览贴面扫掠"
    bl_description = "根据文本配方或命名控件求值，仅在视口显示候选"

    @classmethod
    def poll(cls, context):
        return _can_operate(context)

    def execute(self, context):
        managed, scope = _focus(context)
        error = _preview(context, managed, scope)
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        return {"FINISHED"}


class HSW_OT_surface_sweep_commit(bpy.types.Operator):
    bl_idname = "hard_surface.surface_sweep_commit"
    bl_label = "提交贴面扫掠"
    bl_description = "提交当前已预览的配方，使控件、几何和配方记录同步"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _can_operate(context)

    def execute(self, context):
        managed, scope = _focus(context)
        try:
            state = _states.get(context.window.as_pointer())
            target, params = _request(context, managed)
            if (state is None or state.scope != scope or state.state != "PREVIEW"
                    or state.target_ids != _target_ids(target) or state.recipe_key != _key(params)):
                raise ValueError("当前输入尚未通过预览。请先预览，再提交。")
            if "preview_signature" in state.report:
                params = {**params, "expected_preview_signature": state.report["preview_signature"]}
            report = surface_sweep.execute(target, surface_sweep.TOOL, params)
        except _ERRORS as error:
            _error(context, scope, error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        _forget_output(context.scene, managed)
        _set_state(context, _Preview(scope, report=report, state="COMMITTED"))
        return {"FINISHED"}


class HSW_OT_surface_sweep_cancel(bpy.types.Operator):
    bl_idname = "hard_surface.surface_sweep_cancel"
    bl_label = "取消扫掠预览"
    bl_description = "恢复到上次提交的控件位置并清除预览"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return _can_operate(context)

    def execute(self, context):
        managed, scope = _focus(context)
        try:
            if managed is not None:
                surface_sweep.restore_controls(managed)
                context.view_layer.update()
        except _ERRORS as error:
            _error(context, scope, error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        _forget_output(context.scene, managed)
        _set_state(context, _Preview(scope, message="已清除候选预览。", state="CANCELLED"))
        return {"FINISHED"}


def _number(layout, report, key, label):
    value = report.get(key)
    if type(value) in (int, float):
        layout.label(text=f"{label} {value:.4f} mm")


def _draw_receipt(layout, state):
    if state.message:
        icon = "ERROR" if state.state == "ERROR" else "INFO"
        for index, line in enumerate(textwrap.wrap(state.message, 32)):
            layout.label(text=line, icon=icon if index == 0 else "NONE")
    if state.report:
        report = state.report
        labels = {"PREVIEW": "候选预览", "CREATED": "已生成", "UPDATED": "已更新", "UNCHANGED": "几何无变化"}
        status = report.get("status")
        layout.label(text=labels.get(status, str(status or "求值完成")))
        dimensions = report.get("measured_dimensions_mm")
        if isinstance(dimensions, dict):
            for key, label in (("width", "宽"), ("height", "高"), ("outer_diameter", "外径"),
                               ("inner_diameter", "内径"), ("wall", "壁厚")):
                value = dimensions.get(key)
                if type(value) in (int, float):
                    _number(layout, dimensions, key, label)
                elif (isinstance(value, (list, tuple)) and len(value) == 2
                        and all(type(number) in (int, float) for number in value)):
                    layout.label(text=f"{label} {value[0]:.4f}–{value[1]:.4f} mm")
        _number(layout, report, "dimension_error_mm", "最大尺寸误差")
        _number(layout, report, "min_clearance_mm", "最小净空")
        for key, label in (("shell_intersections", "壳体相交"), ("self_intersections", "自交")):
            if type(report.get(key)) is int:
                layout.label(text=f"{label} {report[key]}")
        if isinstance(report.get("stations"), list):
            layout.label(text=f"测量或诊断站位 {len(report['stations'])}")


class HSW_PT_surface_sweep(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_hsw_surface_sweep"
    bl_label = "贴面扫掠"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"

    def draw(self, context):
        layout = self.layout
        active = context.active_object
        if not _can_operate(context):
            layout.label(text="请在对象模式下选择母壳或扫掠控件。")
            return
        managed, scope = _focus(context)
        if managed is None:
            if active.type != "MESH":
                layout.label(text="请选择网格母壳。")
                return
            layout.prop_search(context.window_manager, _TEXT, bpy.data, "texts")
        else:
            layout.label(text=managed.name)
            layout.label(text="移动命名站位以预览，确认后提交。")
        state = _states.get(context.window.as_pointer())
        current = state is not None and state.scope == scope
        row = layout.row(align=True)
        row.operator(HSW_OT_surface_sweep_preview.bl_idname, text="预览")
        commit = row.row(align=True)
        commit.enabled = current and state.state == "PREVIEW"
        commit.operator(HSW_OT_surface_sweep_commit.bl_idname, text="提交")
        row.operator(HSW_OT_surface_sweep_cancel.bl_idname, text="取消")
        if current:
            _draw_receipt(layout, state)


def _draw_overlay():
    import gpu
    from gpu_extras.batch import batch_for_shader

    context = bpy.context
    if not _can_operate(context) or not context.space_data.overlay.show_overlays:
        return
    state = _states.get(context.window.as_pointer())
    if state is None or state.geometry is None or state.scope != _focus(context)[1]:
        return
    if state.batch is None:
        vertices, edges = state.geometry
        if not edges:
            return
        shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
        state.batch = (shader, batch_for_shader(shader, "LINES", {"pos": vertices}, indices=edges))
    depth = gpu.state.depth_test_get()
    mask = gpu.state.depth_mask_get()
    try:
        gpu.state.depth_test_set("LESS_EQUAL")
        gpu.state.depth_mask_set(False)
        shader, batch = state.batch
        shader.bind()
        shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
        shader.uniform_float("lineWidth", 2.0)
        shader.uniform_float("color", (0.14, 0.8, 1.0, 1.0))
        batch.draw(shader)
    finally:
        gpu.state.depth_test_set(depth)
        gpu.state.depth_mask_set(mask)


def _run_pending():
    now = monotonic()
    windows = {window.as_pointer(): window for wm in bpy.data.window_managers for window in wm.windows}
    for key, (scope, earliest) in list(_pending.items()):
        window = windows.get(key)
        if window is None or window.screen is None:
            _pending.pop(key, None)
            _states.pop(key, None)
            continue
        if earliest > now:
            continue
        _pending.pop(key, None)
        area = next((area for area in window.screen.areas if area.type == "VIEW_3D"), None)
        if area is None:
            continue
        region = next((region for region in area.regions if region.type == "WINDOW"), None)
        with bpy.context.temp_override(window=window, area=area, region=region):
            context = bpy.context
            if _can_operate(context):
                managed, current = _focus(context)
                if managed is not None and scope == current:
                    _preview(context, managed, scope)
    return max(0.025, min(when for _, when in _pending.values()) - monotonic()) if _pending else None


def _has_draft_motion(output, transforms):
    controls = [item for item in transforms if _managed(item) == output]
    if not controls:
        return False
    try:
        recipe = surface_sweep._record(output)["recipe"]
        positions = {item["name"]: Vector(item["position_mm"]) for item in recipe["stations"]}
        return any((item.location / surface_sweep.MM - positions[item[surface_sweep.STATION_KEY]]).length
                   > surface_sweep.COORD_EPS_MM for item in controls)
    except _ERRORS:
        return True


@persistent
def _notice_updates(scene, depsgraph):
    if _suspended:
        return
    changed = set()
    transforms = []
    for update in depsgraph.updates:
        item = update.id.original
        if item is None:
            continue
        if update.is_updated_geometry or update.is_updated_transform:
            changed.add(item.as_pointer())
        if update.is_updated_transform and isinstance(item, bpy.types.Object) and item.type == "EMPTY":
            transforms.append(item)
    if not changed:
        return
    manager = bpy.context.window_manager
    if manager is None:
        return
    for window in manager.windows:
        if window.scene != scene:
            continue
        key = window.as_pointer()
        with bpy.context.temp_override(window=window, area=None, region=None):
            active = window.view_layer.objects.active
            output = _managed(active)
            current = _scope(scene, window.view_layer, output or active)
            state = _states.get(key)
            if state and state.scope == current and changed.intersection(state.target_ids):
                _states[key] = _Preview(current, message="母壳已变化，请重新预览。", state="STALE")
                _redraw(window)
            if output is not None and _has_draft_motion(output, transforms):
                _remember_output(scene, output)
                _states[key] = _Preview(current, message="正在等待控件稳定，随后刷新预览。", state="PENDING")
                _pending[key] = (current, monotonic() + _DEBOUNCE)
                _redraw(window)
    if _pending and not bpy.app.timers.is_registered(_run_pending):
        bpy.app.timers.register(_run_pending, first_interval=_DEBOUNCE)


@persistent
def _resume_after_save(*_):
    for control, location in _suspended.values():
        try:
            control.location = location
        except ReferenceError:
            continue
    _suspended.clear()


@persistent
def _stage_committed_for_save(*_):
    manager = bpy.context.window_manager
    if manager is not None:
        for window in manager.windows:
            with bpy.context.temp_override(window=window, area=None, region=None):
                output = _managed(window.view_layer.objects.active)
                if output is not None:
                    _remember_output(window.scene, output)
    for key, (scene, output) in list(_draft_outputs.items()):
        try:
            with bpy.context.temp_override(scene=scene):
                # Use the same signed record and exact control ownership as the core.
                record = surface_sweep._record(output)
                controls = surface_sweep._controls(output, record)
                for station in record["recipe"]["stations"]:
                    control = controls[station["name"]]
                    committed = Vector(station["position_mm"]) * surface_sweep.MM
                    if control.location == committed:
                        continue
                    _suspended[control.as_pointer()] = (control, control.location.copy())
                    control.location = committed
        except _ERRORS as error:
            _draft_outputs.pop(key, None)
            print(f"Hard Surface Workflow: could not isolate surface sweep drafts in the save: {error}")


@persistent
def _clear_runtime(*_):
    _resume_after_save()
    _states.clear()
    _pending.clear()
    _draft_outputs.clear()
    if bpy.app.timers.is_registered(_run_pending):
        bpy.app.timers.unregister(_run_pending)


def _text_changed(_manager, context):
    if context.window:
        _states.pop(context.window.as_pointer(), None)
        _redraw(context.window)


_CLASSES = (HSW_OT_surface_sweep_preview, HSW_OT_surface_sweep_commit,
            HSW_OT_surface_sweep_cancel, HSW_PT_surface_sweep)
_RESET_HANDLERS = (bpy.app.handlers.load_pre, bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre)
_SAVE_HANDLERS = ((bpy.app.handlers.save_pre, _stage_committed_for_save),
                  (bpy.app.handlers.save_post, _resume_after_save),
                  (bpy.app.handlers.save_post_fail, _resume_after_save))


def register():
    global _draw_handle
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    setattr(bpy.types.WindowManager, _TEXT, StringProperty(
        name="扫掠 JSON 配方", options={"SKIP_SAVE"}, update=_text_changed))
    if not bpy.app.background:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_overlay, (), "WINDOW", "POST_VIEW")
        bpy.app.handlers.depsgraph_update_post.append(_notice_updates)
    for handler in _RESET_HANDLERS:
        handler.append(_clear_runtime)
    for handlers, callback in _SAVE_HANDLERS:
        handlers.append(callback)


def unregister():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
        _draw_handle = None
    if _notice_updates in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_notice_updates)
    for handler in _RESET_HANDLERS:
        if _clear_runtime in handler:
            handler.remove(_clear_runtime)
    for handlers, callback in _SAVE_HANDLERS:
        if callback in handlers:
            handlers.remove(callback)
    _clear_runtime()
    delattr(bpy.types.WindowManager, _TEXT)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
