"""Sidebar operators backed exclusively by the parent package's core API."""

import json
import re
import traceback

import bmesh
import bpy

from . import settings as state
from ..selector_spec import DOMAINS, LOOP_KINDS, SELECT_MODES, TYPE_DOMAIN


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_INDEX_SPLIT = re.compile(r"[\s,;]+")


def _core():
    try:
        from .. import core
    except Exception as exc:
        raise RuntimeError("无法在运行时导入父级 core：{}".format(exc)) from exc
    for name in ("preview", "execute", "select_region"):
        if not callable(getattr(core, name, None)):
            raise RuntimeError("父级 core 缺少 {}() 接口。".format(name))
    return core


def _settings(context):
    return state.get_settings(context)


def _redraw(context):
    state._tag_redraw(context)


def _set_status(settings, message, kind="INFO"):
    state.set_status(settings, message, kind)


def _view_layer_object(context, obj):
    view_layer = getattr(context, "view_layer", None)
    if view_layer is None or obj is None:
        return None
    try:
        return view_layer.objects.get(obj.name)
    except Exception:
        return None


def _target_object(context, settings):
    """Resolve the pinned target or the active mesh without switching modes."""
    obj = getattr(settings, "target_object", None) if settings is not None else None
    if obj is None:
        obj = getattr(context, "active_object", None)
    if obj is None:
        raise ValueError("请选择一个网格对象，或把目标网格固定到面板。")
    if getattr(obj, "type", None) != "MESH":
        raise ValueError("目标对象不是网格：{}".format(getattr(obj, "name", "?")))
    if _view_layer_object(context, obj) is None:
        raise ValueError("目标网格不在当前视图层：{}".format(obj.name))
    if (getattr(obj, "library", None) is not None
            or getattr(obj.data, "library", None) is not None
            or getattr(obj, "override_library", None) is not None
            or getattr(obj.data, "override_library", None) is not None):
        raise ValueError("目标网格来自链接库，不能在当前文件中编辑。")
    try:
        if obj.hide_viewport or obj.hide_get() or not obj.visible_get():
            raise ValueError("目标网格在视图中不可见，请先取消隐藏。")
    except ValueError:
        raise
    except Exception:
        pass
    if getattr(obj, "hide_select", False):
        raise ValueError("目标网格禁止选择，请先解除对象的可选状态。")
    return obj


def _poll_target(context):
    try:
        settings = _settings(context)
        if settings is None:
            return False
        _target_object(context, settings)
        return True
    except Exception:
        return False


def _capture_object_context(context):
    view_layer = context.view_layer
    active = view_layer.objects.active
    selected = []
    for obj in getattr(context, "selected_objects", ()):
        try:
            if _view_layer_object(context, obj) is not None:
                selected.append(obj.name)
        except Exception:
            continue
    return {
        "active": active.name if active is not None else None,
        "selected": selected,
    }


def _store_object_context(settings, context, target):
    if settings is None or target is None:
        return
    if getattr(target, "mode", "OBJECT") != "EDIT":
        settings.object_context = json.dumps(
            _capture_object_context(context), ensure_ascii=False, sort_keys=True
        )


def _restore_object_context(settings, context, target):
    if settings is None:
        return
    raw = settings.object_context
    settings.object_context = ""
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    view_layer = context.view_layer
    for obj in list(getattr(context, "selected_objects", ())):
        try:
            obj.select_set(False)
        except Exception:
            continue
    selected_names = payload.get("selected")
    selected_names = selected_names if isinstance(selected_names, list) else []
    restored = False
    for name in selected_names:
        obj = view_layer.objects.get(name) if isinstance(name, str) else None
        if obj is None:
            continue
        try:
            obj.select_set(True)
            restored = True
        except Exception:
            continue
    active_name = payload.get("active")
    active = view_layer.objects.get(active_name) if isinstance(active_name, str) else None
    if active is not None:
        try:
            view_layer.objects.active = active
        except Exception:
            pass
    elif target is not None and _view_layer_object(context, target) is not None:
        view_layer.objects.active = target
    if not restored and target is not None and _view_layer_object(context, target) is not None:
        try:
            target.select_set(True)
        except Exception:
            pass


def _ensure_edit_target(context, target):
    """Make ``target`` the only object in Edit Mode; return whether we entered."""
    context_mode = getattr(context, "mode", "OBJECT")
    if context_mode not in ("OBJECT", "EDIT_MESH"):
        raise ValueError("请先回到对象模式，再进入目标网格的 Edit。")
    view_layer = context.view_layer
    edit_objects = [obj for obj in view_layer.objects if getattr(obj, "mode", None) == "EDIT"]
    if target.mode == "EDIT" and len(edit_objects) == 1 and edit_objects[0] == target:
        if view_layer.objects.active != target:
            view_layer.objects.active = target
        return False

    if edit_objects:
        if target.mode == "EDIT":
            raise ValueError("只支持单对象编辑；请先结束多对象 Edit 后再执行。")
        raise ValueError("请先结束其他对象的编辑模式，再切换到目标网格。")

    for obj in list(getattr(context, "selected_objects", ())):
        try:
            obj.select_set(False)
        except Exception:
            continue
    try:
        target.select_set(True)
    except Exception as exc:
        raise RuntimeError("无法选中目标网格：{}".format(exc)) from exc
    view_layer.objects.active = target
    bpy.ops.object.mode_set(mode="EDIT")
    if target.mode != "EDIT":
        raise RuntimeError("无法进入目标网格的编辑模式。")
    return True


def _active_element(obj, element_type):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.index_update()
    bm.faces.index_update()
    history = getattr(bm, "select_history", None)
    active = getattr(history, "active", None) if history is not None else None
    if isinstance(active, element_type) and active.is_valid and active.select and not active.hide:
        return active.index
    sequence = bm.edges if element_type is bmesh.types.BMEdge else bm.faces
    selected = [element for element in sequence if element.select and not element.hide]
    if len(selected) == 1:
        return selected[0].index
    return None


def _parse_int_token(token):
    token = token.strip()
    if not token:
        raise ValueError("索引不能为空。")
    try:
        value = int(token, 10)
    except (TypeError, ValueError):
        raise ValueError("索引必须是整数：{!r}".format(token)) from None
    if value < 0:
        raise ValueError("索引不能为负数：{}".format(value))
    return value


def _parse_edge_seed(text, label):
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("["):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("{} JSON 无效：{}".format(label, exc.msg)) from None
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("{} JSON 必须是两个端点索引的数组。".format(label))
        return [_parse_int_token(str(item)) for item in value]
    parts = [part for part in re.split(r"[\s,;]+", text) if part]
    if len(parts) == 1:
        return _parse_int_token(parts[0])
    if len(parts) == 2:
        return [_parse_int_token(parts[0]), _parse_int_token(parts[1])]
    raise ValueError("{}必须是边索引或两个端点索引。".format(label))


def _parse_indices(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("请填写显式索引，例如 3,0,1 或 0-5；顺序会保留给 ACTIVE 操作。")
    values = []
    if text.startswith("["):
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("索引 JSON 无效：第 {} 行第 {} 列：{}".format(
                exc.lineno, exc.colno, exc.msg
            )) from None
        if not isinstance(raw, list):
            raise ValueError("索引 JSON 必须是数组。")
        for item in raw:
            if type(item) is not int:
                raise ValueError("索引 JSON 只能包含整数。")
            if item < 0:
                raise ValueError("索引不能为负数：{}".format(item))
            values.append(item)
    else:
        for token in _INDEX_SPLIT.split(text):
            if not token:
                continue
            if "-" in token:
                start_text, end_text = token.split("-", 1)
                start = _parse_int_token(start_text)
                end = _parse_int_token(end_text)
                if end < start:
                    raise ValueError("索引区间终点小于起点：{}".format(token))
                if end - start > 1000000:
                    raise ValueError("索引区间过大：{}".format(token))
                values.extend(range(start, end + 1))
            else:
                values.append(_parse_int_token(token))
    if not values:
        raise ValueError("显式索引列表为空。")
    result = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _current_domain(context, settings):
    domain = getattr(settings, "current_domain", "AUTO")
    if domain in state.DOMAINS:
        return domain
    try:
        select_mode = context.tool_settings.mesh_select_mode
    except Exception:
        select_mode = (True, False, False)
    domains = [name for name, enabled in zip(state.DOMAINS, select_mode) if enabled]
    if len(domains) != 1:
        raise ValueError("请选择一个编辑域（顶点、边或面），或在面板明确指定域。")
    return domains[0]


def _build_selector(context, settings, obj, source=None):
    source = source or getattr(settings, "selector_source", "current")
    if source == "current":
        return {"kind": "current", "domain": _current_domain(context, settings)}
    if source == "indices":
        return {
            "kind": "indices",
            "domain": getattr(settings, "indices_domain", "EDGE"),
            "indices": _parse_indices(settings.explicit_indices),
        }
    if source == "box":
        return {"kind": "box", "domain": settings.box_domain, "space": settings.box_space,
                "minimum": list(settings.box_minimum), "maximum": list(settings.box_maximum)}
    if source in ("edge_loop", "edge_ring", "vertex_loop"):
        seed = _parse_edge_seed(settings.seed_edge, "种子边")
        if seed is None:
            active = _active_element(obj, bmesh.types.BMEdge)
            if active is None:
                raise ValueError(
                    "没有活动边：请在 Edit 模式点选一条边，或在面板填写种子边索引。"
                )
            seed = active
        selector = {"kind": source, "seed_edge": seed}
    elif source == "face_loop":
        direction = _parse_edge_seed(settings.direction_edge, "方向边")
        if direction is None:
            raise ValueError("面循环必须填写显式方向边索引。")
        seed_face = int(getattr(settings, "seed_face", -1))
        if seed_face < 0:
            seed_face = _active_element(obj, bmesh.types.BMFace)
            if seed_face is None:
                raise ValueError(
                    "没有活动面：请在 Edit 模式点选一个四边面，或填写种子面索引。"
                )
        selector = {"kind": source, "seed_face": seed_face, "direction_edge": direction}
    else:
        raise ValueError("未知选区来源：{!r}".format(source))
    if getattr(settings, "sample_path", False):
        if settings.sample_start < 0:
            raise ValueError("请填写取样起点索引，或在目标域点选起点与相邻下一站后读取。")
        sampling = {"start": settings.sample_start, "step": settings.sample_step, "phase": settings.sample_phase}
        if settings.sample_toward >= 0:
            sampling["toward"] = settings.sample_toward
        if settings.sample_limit:
            sampling["limit"] = settings.sample_limit
        selector["sampling"] = sampling
    return selector


def _validate_resolved(resolved):
    if not state.valid_resolved(resolved):
        raise RuntimeError("core 返回的选区缺少合法的 domain/indices/topology_sha256。")
    indices = list(resolved["indices"])
    if len(set(indices)) != len(indices):
        raise RuntimeError("core 返回的选区索引有重复。")
    if not _HEX64.match(resolved["topology_sha256"]):
        raise RuntimeError("core 返回的 topology_sha256 不是小写 SHA-256。")
    result = {
        "domain": resolved["domain"],
        "indices": indices,
        "topology_sha256": resolved["topology_sha256"],
    }
    if "selection_history" in resolved:
        result["selection_history"] = list(resolved["selection_history"])
    if "sampling" in resolved:
        result["sampling"] = resolved["sampling"]
    return result


def _preview_then_select(context, core, obj, selector):
    preview = core.preview(obj, selector)
    resolved = _validate_resolved(preview)
    selected = core.select_region(obj, selector)
    resolved = _validate_resolved(selected)
    if obj.mode != "EDIT":
        _ensure_edit_target(context, obj)
    return resolved


def _object_identity(obj):
    return {
        "object_name": getattr(obj, "name", ""),
        "object_uid": int(getattr(obj, "session_uid", 0) or 0),
        "mesh_name": getattr(obj.data, "name", ""),
        "mesh_uid": int(getattr(obj.data, "session_uid", 0) or 0),
    }


def _store_preview_for_object(settings, resolved, source, obj):
    identity = _object_identity(obj)
    return state.store_preview(
        settings,
        resolved,
        source=source,
        object_name=identity["object_name"],
        object_uid=identity["object_uid"],
        mesh_name=identity["mesh_name"],
        mesh_uid=identity["mesh_uid"],
    )


def _preview_identity_matches(settings, obj):
    cached_object_uid = int(getattr(settings, "preview_object_uid", 0) or 0)
    cached_mesh_uid = int(getattr(settings, "preview_mesh_uid", 0) or 0)
    current_object_uid = int(getattr(obj, "session_uid", 0) or 0)
    current_mesh_uid = int(getattr(obj.data, "session_uid", 0) or 0)
    if cached_object_uid:
        if cached_object_uid != current_object_uid:
            return False
    elif settings.preview_object_name != getattr(obj, "name", ""):
        return False
    if cached_mesh_uid:
        if cached_mesh_uid != current_mesh_uid:
            return False
    elif settings.preview_mesh_name != getattr(obj.data, "name", ""):
        return False
    return True


def _locked_selection(context, core, obj, settings):
    """Reuse a displayed preview exactly, or preview once when none exists.

    A stale cache is never re-resolved and executed in the same click: that
    could silently change a loop.  The cache is cleared and the operator is
    cancelled so the user can inspect the new range first.
    """
    had_preview = bool(getattr(settings, "preview_json", ""))
    cached = state.cached_preview(settings)
    if had_preview and cached is None:
        state.invalidate_preview(settings, None, require_preview=True)
        raise ValueError("选区预览缓存无效，已取消执行；请重新预览选区。")
    if cached is not None:
        if not _preview_identity_matches(settings, obj):
            state.invalidate_preview(settings, None, require_preview=True)
            raise ValueError(
                "面板预览绑定的目标对象/网格已变化，已取消执行；请重新预览选区。"
            )
        explicit = state.explicit_selector(cached)
        try:
            source = json.loads(settings.preview_source or "{}")
            if not isinstance(source, dict):
                raise ValueError("选区来源缓存损坏")
            recheck_source = source.get("kind") == "box" or "sampling" in source
            _validate_resolved(core.preview(obj, explicit))
            if recheck_source:
                fresh = _validate_resolved(core.preview(obj, source))
                if state.explicit_selector(fresh) != explicit:
                    raise ValueError("坐标范围或路径取样的结果已变化")
        except Exception as exc:
            state.invalidate_preview(settings, None, require_preview=True)
            raise ValueError(
                "选区预览已过期（拓扑可能已改变），已取消执行；"
                "请重新预览选区后再执行。原因：{}".format(exc)
            ) from exc
        try:
            applied = _validate_resolved(core.select_region(obj, explicit))
            if obj.mode != "EDIT":
                _ensure_edit_target(context, obj)
        except Exception as exc:
            state.invalidate_preview(settings, None, require_preview=True)
            raise ValueError(
                "无法恢复已预览的选区，已取消执行；请重新预览选区。"
            ) from exc
        checked = _store_preview_for_object(settings, applied, source if recheck_source else explicit, obj)
        if not _preview_identity_matches(settings, obj):
            state.invalidate_preview(settings, None, require_preview=True)
            raise ValueError(
                "选区预览的目标已变化，已取消执行；请重新预览选区。"
            )
        return state.explicit_selector(checked), checked

    selector = _build_selector(context, settings, obj)
    resolved = _preview_then_select(context, core, obj, selector)
    checked = _store_preview_for_object(settings, resolved, selector, obj)
    if not _preview_identity_matches(settings, obj):
        state.invalidate_preview(settings, None, require_preview=True)
        raise ValueError("目标身份在预览期间发生变化，已取消执行；请重新预览选区。")
    return state.explicit_selector(checked), checked


def _snapshot_mesh(obj):
    if obj.mode == "EDIT":
        bm = bmesh.from_edit_mesh(obj.data)
    else:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
    try:
        return bm.copy()
    finally:
        if obj.mode != "EDIT":
            bm.free()


def _free_snapshot(snapshot):
    if snapshot is not None:
        try:
            snapshot.free()
        except Exception:
            pass


def _restore_snapshot(obj, snapshot):
    """Restore a BMesh snapshot and return (ok, error_message)."""
    error = ""
    try:
        if obj.mode == "EDIT":
            temporary = bpy.data.meshes.new("HSW_rollback")
            try:
                snapshot.to_mesh(temporary)
                bm = bmesh.from_edit_mesh(obj.data)
                bm.clear()
                bm.from_mesh(temporary)
                bmesh.update_edit_mesh(
                    obj.data, loop_triangles=False, destructive=True
                )
            finally:
                try:
                    bpy.data.meshes.remove(temporary)
                except Exception:
                    pass
        else:
            snapshot.to_mesh(obj.data)
            obj.data.update()
        return True, ""
    except Exception as exc:
        traceback.print_exc()
        error = "{}: {}".format(type(exc).__name__, exc)
        return False, error
    finally:
        _free_snapshot(snapshot)


def _result_error(result):
    if not isinstance(result, dict):
        return None
    if result.get("ok") is False or result.get("success") is False:
        error = result.get("error") or result.get("message")
        return _text_error(error) or "core.execute 报告失败。"
    error = result.get("error")
    if error not in (None, "", False):
        return _text_error(error)
    return None


def _text_error(value):
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)
    return str(value) if value not in (None, "", False) else ""


def _result_message(result, fallback):
    if isinstance(result, dict) and result.get("passes"):
        last = result["passes"][-1]
        if last.get("clear_diameters_local"):
            sizes = last["clear_diameters_local"]
            return f"孔心基准 {last['center_mode']}，成孔净通径 {min(sizes):.6g} 至 {max(sizes):.6g}（局部单位）。"
        if "source_vertex_pairs" in last:
            pairs = last["source_vertex_pairs"]
            if pairs:
                sample = "，".join(f"{a}→{b}" for a, b in pairs[:8])
                return f"桥接已验证 {len(pairs)} 对原边界顶点。配对 {sample}。"
            return "桥接完成；该输入的角点配对需从新边流复查。"
        if "unmatched_indices" in last and "ambiguous_indices" in last:
            return (f"对称匹配 {last['matched_vertices']} 点，未配对 {len(last['unmatched_indices'])} 点，"
                    f"对应不唯一 {len(last['ambiguous_indices'])} 点。")
        if "source_faces" in last and "factor" in last:
            return f"定向环切经过 {len(last['source_faces'])} 面，滑动比例 {last['factor']:.4g}。"
    if isinstance(result, dict) and result.get("tool") == "form.recess" and result.get("passes"):
        last = result["passes"][-1]
        if all(key in last for key in ("requested_depth", "measured_depth_min", "measured_depth_max")):
            return ("沉台末次请求深度 {requested_depth:.6g}，内盘实测 "
                    "{measured_depth_min:.6g} 至 {measured_depth_max:.6g}（局部单位）。").format(**last)
    for key in ("message", "summary", "detail"):
        value = result.get(key) if isinstance(result, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _result_warning(result):
    if isinstance(result, dict) and result.get("tool") == "vertex.connect":
        count = sum(row.get("wire_edges_across_faces", 0) for row in result.get("passes", []))
        if count:
            return f"已在已有面上建了 {count} 条独立边，原面完整。切开原面可用 J 或 K。"
    for key in ("warning", "warnings"):
        value = result.get(key) if isinstance(result, dict) else None
        if value not in (None, "", False, [], {}):
            return _text_error(value)
    return ""


def _result_text(result):
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    except Exception:
        text = repr(result)
    if len(text) > 12000:
        text = text[:12000] + "\n…（结果已截断）"
    return text


def _selection_phrase(resolved):
    domain = state.DOMAIN_LABELS.get(resolved["domain"], resolved["domain"])
    return "{} 个{}元素".format(len(resolved["indices"]), domain)


def _report_error(operator, settings, context, message):
    _set_status(settings, message, "ERROR")
    _redraw(context)
    operator.report({"ERROR"}, message)


class HSW_OT_use_active_object(bpy.types.Operator):
    bl_idname = "hard_surface.use_active_object"
    bl_label = "使用活动网格"
    bl_description = "把当前活动网格固定为面板目标"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        obj = getattr(context, "active_object", None)
        return obj is not None and getattr(obj, "type", None) == "MESH"

    def execute(self, context):
        settings = _settings(context)
        if settings is None:
            self.report({"ERROR"}, "面板状态未初始化。")
            return {"CANCELLED"}
        obj = context.active_object
        settings.target_object = obj
        _set_status(settings, "目标网格已固定为 {}".format(obj.name), "SUCCESS")
        _redraw(context)
        return {"FINISHED"}


class HSW_OT_choose_tool(bpy.types.Operator):
    bl_idname = "hard_surface.choose_tool"
    bl_label = "选择工具"
    bl_description = "把工具下拉菜单的选择写入面板状态"
    bl_options = {"REGISTER"}

    tool_id: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return _settings(context) is not None

    def execute(self, context):
        settings = _settings(context)
        if settings is None:
            self.report({"ERROR"}, "面板状态未初始化。")
            return {"CANCELLED"}
        descriptor = state.descriptor(self.tool_id)
        if descriptor is None:
            _report_error(
                self, settings, context,
                "工具不存在：{}".format(self.tool_id or "<empty>"),
            )
            return {"CANCELLED"}
        settings.tool = self.tool_id
        _redraw(context)
        return {"FINISHED"}


class HSW_OT_enter_edit(bpy.types.Operator):
    bl_idname = "hard_surface.enter_edit"
    bl_label = "进入编辑模式"
    bl_description = "选中目标网格并进入单对象 Edit Mode"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return _poll_target(context)

    def execute(self, context):
        settings = _settings(context)
        if settings is None:
            self.report({"ERROR"}, "面板状态未初始化。")
            return {"CANCELLED"}
        try:
            target = _target_object(context, settings)
            _store_object_context(settings, context, target)
            _ensure_edit_target(context, target)
            _set_status(
                settings,
                "已进入 {} 的编辑模式。".format(target.name),
                "SUCCESS",
            )
            _redraw(context)
            return {"FINISHED"}
        except (ValueError, RuntimeError) as exc:
            _report_error(self, settings, context, "进入编辑模式失败：{}".format(exc))
            return {"CANCELLED"}
        except Exception as exc:
            traceback.print_exc()
            _report_error(
                self, settings, context,
                "进入编辑模式失败：{}: {}".format(type(exc).__name__, exc),
            )
            return {"CANCELLED"}


class HSW_OT_exit_edit(bpy.types.Operator):
    bl_idname = "hard_surface.exit_edit"
    bl_label = "返回对象模式"
    bl_description = "退出编辑模式，并恢复进入前记住的对象选择"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        try:
            settings = _settings(context)
            target = _target_object(context, settings)
            return target.mode == "EDIT"
        except Exception:
            return False

    def execute(self, context):
        settings = _settings(context)
        if settings is None:
            self.report({"ERROR"}, "面板状态未初始化。")
            return {"CANCELLED"}
        try:
            target = _target_object(context, settings)
            if getattr(context, "mode", "") == "EDIT_MESH":
                active = context.view_layer.objects.active
                if active is not None and active.mode == "EDIT":
                    bpy.ops.object.mode_set(mode="OBJECT")
            if target.mode == "EDIT":
                context.view_layer.objects.active = target
                bpy.ops.object.mode_set(mode="OBJECT")
            _restore_object_context(settings, context, target)
            _set_status(settings, "已返回对象模式。", "SUCCESS")
            _redraw(context)
            return {"FINISHED"}
        except (ValueError, RuntimeError) as exc:
            _report_error(self, settings, context, "返回对象模式失败：{}".format(exc))
            return {"CANCELLED"}
        except Exception as exc:
            traceback.print_exc()
            _report_error(
                self, settings, context,
                "返回对象模式失败：{}: {}".format(type(exc).__name__, exc),
            )
            return {"CANCELLED"}


class HSW_OT_preview_selection(bpy.types.Operator):
    bl_idname = "hard_surface.preview_selection"
    bl_label = "硬表面：预览并高亮选区"
    bl_description = "解析当前选区来源，真实选中并高亮范围"
    bl_options = {"REGISTER", "UNDO"}
    source: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return _poll_target(context)

    def execute(self, context):
        settings = _settings(context)
        if settings is None:
            self.report({"ERROR"}, "面板状态未初始化。")
            return {"CANCELLED"}
        try:
            core = _core()
            target = _target_object(context, settings)
            if self.source and target != context.active_object:
                raise ValueError("快捷选取要求活动网格与面板目标一致。")
            _store_object_context(settings, context, target)
            _ensure_edit_target(context, target)
            selector = _build_selector(context, settings, target, self.source)
            resolved = _preview_then_select(context, core, target, selector)
            if self.source and settings.selector_source != self.source:
                settings.selector_source = self.source
            stored = _store_preview_for_object(settings, resolved, selector, target)
            count = len(stored["indices"])
            if count:
                message = "已选出 {}。".format(_selection_phrase(stored))
                if "sampling" in resolved:
                    path = resolved["sampling"]
                    shape = "闭环" if path["closed"] else "开链"
                    message += f" {shape} {path['path_length']} 站，{path['path_start']} → {path['path_end']}。"
                kind = "SUCCESS"
            else:
                guided = settings.tool == "looptools.gstretch" and core.converts_strokes(
                    settings.tool, state.collect_params(settings, settings.tool))
                message = "已确认空选区，执行将从指定描线生成边链。" if guided else "选区为空，请调整选区或提供 GStretch 描线。"
                kind = "SUCCESS" if guided else "WARNING"
            _set_status(settings, message, kind)
            _redraw(context)
            self.report({"INFO" if kind == "SUCCESS" else "WARNING"}, message)
            return {"FINISHED"}
        except (ValueError, RuntimeError) as exc:
            state.invalidate_preview(settings, None, require_preview=True)
            _report_error(self, settings, context, "选区预览失败：{}".format(exc))
            return {"CANCELLED"}
        except Exception as exc:
            traceback.print_exc()
            state.invalidate_preview(settings, None, require_preview=True)
            _report_error(
                self, settings, context,
                "选区预览失败：{}: {}".format(type(exc).__name__, exc),
            )
            return {"CANCELLED"}


class HSW_OT_pick_path_direction(bpy.types.Operator):
    bl_idname = "hard_surface.pick_path_direction"
    bl_label = "硬表面：读取路径起点与下一站"
    bl_description = "在目标域依次点选起点和下一站，再读取两者的真实索引"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        settings = _settings(context)
        return (context.mode == "EDIT_MESH" and len(context.objects_in_mode) == 1
                and settings is not None and settings.selector_source in LOOP_KINDS)

    def execute(self, context):
        settings = _settings(context)
        try:
            obj = _target_object(context, settings)
            domain = TYPE_DOMAIN[settings.selector_source]
            if obj != context.active_object or tuple(context.tool_settings.mesh_select_mode) != SELECT_MODES[domain]:
                raise ValueError("请在目标活动网格中切换到该路径的单一选取域。")
            mesh = bmesh.from_edit_mesh(obj.data)
            elements = getattr(mesh, DOMAINS[domain])
            elements.index_update()
            picked = {item for item in elements if item.select and not item.hide}
            ordered = [item.index for item in mesh.select_history if item in picked]
            if not 1 <= len(picked) <= 2 or len(ordered) != len(picked):
                raise ValueError("请先清除选择，再用鼠标依次点选一个端点，或起点与相邻的下一站。")
            settings.sample_start = ordered[0]
            settings.sample_toward = ordered[1] if len(ordered) == 2 else -1
            self.report({"INFO"}, f"已读取路径索引 {', '.join(map(str, ordered))}")
            return {"FINISHED"}
        except ValueError as exc:
            _report_error(self, settings, context, str(exc))
            return {"CANCELLED"}


class HSW_OT_execute(bpy.types.Operator):
    bl_idname = "hard_surface.execute"
    bl_label = "执行工具"
    bl_description = "先锁定/高亮选区，再调用 core.execute"
    # One successful execute is one undo step.  The panel does not split the
    # selection lock, the core call and the post-execute highlight into
    # separate undoable operations.
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _poll_target(context)

    def execute(self, context):
        settings = _settings(context)
        if settings is None:
            self.report({"ERROR"}, "面板状态未初始化。")
            return {"CANCELLED"}
        settings.last_result = ""
        if (getattr(settings, "needs_preview", False)
                and not getattr(settings, "preview_json", "")):
            message = "需要先预览并确认选区；已取消执行。"
            _report_error(self, settings, context, message)
            return {"CANCELLED"}
        desc = state.descriptor(getattr(settings, "tool", ""))
        if desc is None:
            message = "请选择一个可用工具。"
            if state.CATALOG_ERROR:
                message += " {}".format(state.CATALOG_ERROR)
            _report_error(self, settings, context, message)
            return {"CANCELLED"}

        label = desc.get("label") or desc.get("tool")
        core = None
        target = None
        snapshot = None
        committed = False
        try:
            core = _core()
            target = _target_object(context, settings)
            if target.data.users - int(target.data.use_fake_user) > 1:
                raise ValueError(
                    "目标网格数据被多个对象共享；请先设为单用户，避免影响其他对象。"
                )
            if target.data.shape_keys or target.data.animation_data or target.animation_data:
                raise ValueError(
                    "目标网格含形态键或动画；core 需要无变形的静态编辑网格。"
                )
            if (getattr(settings, "preview_json", "")
                    and not _preview_identity_matches(settings, target)):
                state.invalidate_preview(settings, None, require_preview=True)
                raise ValueError(
                    "面板预览绑定的目标对象/网格已变化，已取消执行；请重新预览选区。"
                )
            _store_object_context(settings, context, target)
            _ensure_edit_target(context, target)

            explicit, resolved = _locked_selection(context, core, target, settings)
            if not _preview_identity_matches(settings, target):
                state.invalidate_preview(settings, None, require_preview=True)
                raise ValueError(
                    "面板预览绑定的目标对象/网格已变化，已取消执行；请重新预览选区。"
                )
            params = state.collect_params(settings, settings.tool)
            if not resolved["indices"] and not core.converts_strokes(settings.tool, params):
                raise ValueError("选区为空，无法执行工具。")
            domains = list(desc.get("domains") or ())
            if domains and resolved["domain"] not in domains:
                raise ValueError(
                    "工具 {} 只接受 {} 选区，当前解析为 {}。".format(
                        label,
                        "/".join(domains),
                        state.DOMAIN_LABELS.get(resolved["domain"], resolved["domain"]),
                    )
                )

            protect = None
            if getattr(settings, "use_protect", False):
                protect_name = (settings.protect_group or "").strip()
                if not protect_name:
                    raise ValueError("已启用保护顶点组，但未填写顶点组名称。")
                if target.vertex_groups.get(protect_name) is None:
                    raise ValueError(
                        "目标对象不存在保护顶点组：{}".format(protect_name)
                    )
                protect = {"kind": "vertex_group", "group": protect_name}
            repeat = max(1, int(getattr(settings, "repeat", 1)))
            allow_expansion = bool(getattr(settings, "allow_expansion", False))

            snapshot = _snapshot_mesh(target)
            result = core.execute(
                target,
                settings.tool,
                explicit,
                params=params,
                repeat=repeat,
                protect=protect,
                allow_expansion=allow_expansion,
            )
            if result is None:
                result = {}
            if not isinstance(result, dict):
                raise RuntimeError(
                    "core.execute 必须返回 dict，实际为 {}。".format(type(result).__name__)
                )
            result_error = _result_error(result)
            if result_error:
                raise RuntimeError(result_error)

            committed = True
            _free_snapshot(snapshot)
            snapshot = None
            settings.last_result = _result_text(result)

            changed = bool(desc.get("topology_changes"))
            changed = changed or bool(result.get("topology_changed"))
            changed = changed or bool(result.get("topology_changes"))
            result_hash = result.get("result_topology_sha256")
            if isinstance(result_hash, str) and result_hash != resolved["topology_sha256"]:
                changed = True
            warning = _result_warning(result)
            message = _result_message(
                result,
                "{} 执行完成，选区为 {}。".format(label, _selection_phrase(resolved)),
            )
            kind = "SUCCESS"

            if changed:
                state.invalidate_preview(
                    settings, None, require_preview=True
                )
                message += " 拓扑已改变，请重新预览选区。"
            else:
                try:
                    core.select_region(target, explicit)
                    if target.mode != "EDIT":
                        _ensure_edit_target(context, target)
                except Exception as exc:
                    state.invalidate_preview(settings, None, require_preview=True)
                    message += " 选区无法保持高亮：{}".format(exc)
                    kind = "WARNING"
            if warning:
                message += " 警告：{}".format(warning)
                kind = "WARNING"
            _set_status(settings, message, kind)
            _redraw(context)
            self.report({"WARNING" if kind == "WARNING" else "INFO"}, message)
            return {"FINISHED"}
        except (ValueError, RuntimeError) as exc:
            if committed:
                message = "{} 已执行，但结果后处理失败：{}".format(label, exc)
                _set_status(settings, message, "WARNING")
                _redraw(context)
                self.report({"WARNING"}, message)
                return {"FINISHED"}
            rollback_note = ""
            if snapshot is not None:
                try:
                    if target is not None:
                        if target.mode != "EDIT":
                            _ensure_edit_target(context, target)
                        ok, restore_error = _restore_snapshot(target, snapshot)
                        snapshot = None
                        rollback_note = (
                            " 已回滚到执行前网格状态。"
                            if ok else " 回滚失败：{}".format(restore_error)
                        )
                except Exception as restore_exc:
                    traceback.print_exc()
                    rollback_note = " 回滚失败：{}: {}".format(
                        type(restore_exc).__name__, restore_exc
                    )
                    _free_snapshot(snapshot)
                    snapshot = None
            message = "{} 执行失败：{}{}".format(label, exc, rollback_note)
            _report_error(self, settings, context, message)
            return {"CANCELLED"}
        except Exception as exc:
            traceback.print_exc()
            if committed:
                message = "{} 已执行，但结果后处理失败：{}: {}".format(
                    label, type(exc).__name__, exc
                )
                _set_status(settings, message, "WARNING")
                _redraw(context)
                self.report({"WARNING"}, message)
                return {"FINISHED"}
            rollback_note = ""
            if snapshot is not None:
                try:
                    if target is not None:
                        if target.mode != "EDIT":
                            _ensure_edit_target(context, target)
                        ok, restore_error = _restore_snapshot(target, snapshot)
                        snapshot = None
                        rollback_note = (
                            " 已回滚到执行前网格状态。"
                            if ok else " 回滚失败：{}".format(restore_error)
                        )
                except Exception as restore_exc:
                    traceback.print_exc()
                    rollback_note = " 回滚失败：{}: {}".format(
                        type(restore_exc).__name__, restore_exc
                    )
                    _free_snapshot(snapshot)
                    snapshot = None
            _report_error(
                self, settings, context,
                "{} 执行失败：{}: {}{}".format(
                    label, type(exc).__name__, exc, rollback_note
                ),
            )
            return {"CANCELLED"}
        finally:
            if snapshot is not None:
                _free_snapshot(snapshot)


class HSW_OT_reset_params(bpy.types.Operator):
    bl_idname = "hard_surface.reset_params"
    bl_label = "重置工具参数"
    bl_description = "把当前工具的所有参数恢复为 descriptor 默认值"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        settings = _settings(context)
        return settings is not None and state.descriptor(getattr(settings, "tool", "")) is not None

    def execute(self, context):
        settings = _settings(context)
        if settings is None:
            self.report({"ERROR"}, "面板状态未初始化。")
            return {"CANCELLED"}
        descriptor = state.descriptor(settings.tool)
        if descriptor is None:
            _report_error(self, settings, context, "当前没有可重置的工具。")
            return {"CANCELLED"}
        try:
            state.reset_tool_params(settings, settings.tool)
            settings.last_result = ""
            _set_status(
                settings,
                "已重置 {} 的参数。".format(descriptor.get("label") or settings.tool),
                "SUCCESS",
            )
            _redraw(context)
            return {"FINISHED"}
        except Exception as exc:
            _report_error(self, settings, context, "重置参数失败：{}".format(exc))
            return {"CANCELLED"}


class HSW_OT_clear_preview(bpy.types.Operator):
    bl_idname = "hard_surface.clear_preview"
    bl_label = "清除预览"
    bl_description = "丢弃锁定的选区范围；下次执行前必须重新预览确认"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return _settings(context) is not None

    def execute(self, context):
        settings = _settings(context)
        if settings is None:
            self.report({"ERROR"}, "面板状态未初始化。")
            return {"CANCELLED"}
        had_preview = bool(getattr(settings, "preview_json", ""))
        if had_preview:
            state.invalidate_preview(
                settings,
                "预览已清除；请先重新预览并确认选区再执行。",
                require_preview=True,
            )
        else:
            state.set_status(settings, "当前没有可清除的预览。", "INFO")
        _redraw(context)
        return {"FINISHED"}


CLASSES = (
    HSW_OT_use_active_object,
    HSW_OT_choose_tool,
    HSW_OT_enter_edit,
    HSW_OT_exit_edit,
    HSW_OT_preview_selection,
    HSW_OT_pick_path_direction,
    HSW_OT_execute,
    HSW_OT_reset_params,
    HSW_OT_clear_preview,
)


def register():
    for cls in CLASSES:
        existing = getattr(bpy.types, cls.__name__, None)
        if existing is not None:
            state._unregister_class(existing)
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        state._unregister_class(cls)
