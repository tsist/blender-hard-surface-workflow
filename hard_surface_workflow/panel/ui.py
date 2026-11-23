"""View3D sidebar drawing for the hard surface workflow."""

import textwrap

import bmesh
import bpy

from . import settings as state


_STATUS_ICONS = {
    "INFO": "INFO",
    "SUCCESS": "CHECKMARK",
    "WARNING": "ERROR",
    "ERROR": "ERROR",
}


def _wrap(text, width=50, limit=None):
    lines = []
    for paragraph in str(text).splitlines() or [""]:
        wrapped = textwrap.wrap(
            paragraph, width=width, break_long_words=True, break_on_hyphens=False
        )
        lines.extend(wrapped or [""])
    if limit is not None and len(lines) > limit:
        lines = lines[:limit] + ["…"]
    return lines


def _panel_settings(context):
    return state.get_settings(context)


def _display_target(context, settings):
    obj = getattr(settings, "target_object", None) if settings is not None else None
    if obj is None:
        obj = getattr(context, "active_object", None)
    if obj is not None and getattr(obj, "type", None) == "MESH":
        return obj
    return None


def _active_element_index(target, element_type):
    if target is None or getattr(target, "mode", None) != "EDIT":
        return None
    try:
        bm = bmesh.from_edit_mesh(target.data)
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.edges.index_update()
        bm.faces.index_update()
        history = getattr(bm, "select_history", None)
        active = getattr(history, "active", None) if history is not None else None
        if isinstance(active, element_type) and getattr(active, "is_valid", False):
            return active.index
        sequence = bm.edges if element_type is bmesh.types.BMEdge else bm.faces
        selected = [element for element in sequence if element.select]
        if len(selected) == 1:
            return selected[0].index
    except Exception:
        return None
    return None


def _select_mode_text(context):
    try:
        mode = context.tool_settings.mesh_select_mode
    except Exception:
        return ""
    labels = []
    if len(mode) > 0 and mode[0]:
        labels.append("顶点")
    if len(mode) > 1 and mode[1]:
        labels.append("边")
    if len(mode) > 2 and mode[2]:
        labels.append("面")
    return " / ".join(labels)


def _draw_status(layout, settings):
    status = (getattr(settings, "status", "") or "").strip()
    if not status:
        return
    kind = getattr(settings, "status_kind", "INFO")
    box = layout.box()
    box.alert = kind == "ERROR"
    lines = _wrap(status, width=52, limit=8)
    icon = _STATUS_ICONS.get(kind, "INFO")
    for index, line in enumerate(lines):
        box.label(text=line, icon=icon if index == 0 else "NONE")


def _draw_result(layout, settings):
    text = (getattr(settings, "last_result", "") or "").strip()
    if not text:
        return
    box = layout.box()
    box.label(text="最近一次执行结果", icon="INFO")
    for line in _wrap(text, width=58, limit=10):
        box.label(text=line)


def _draw_catalog_error(layout):
    if not state.CATALOG_ERROR:
        return
    box = layout.box()
    box.alert = True
    for line in _wrap("目录问题：" + state.CATALOG_ERROR, width=52, limit=5):
        box.label(text=line, icon="ERROR")


def _draw_target(layout, context, settings):
    target = _display_target(context, settings)
    box = layout.box()
    box.label(text="目标网格", icon="OBJECT_DATA")
    row = box.row(align=True)
    row.prop(settings, "target_object", text="")
    row.operator("hard_surface.use_active_object", text="", icon="MESH_DATA")

    if target is not None:
        mode_text = {
            "EDIT": "编辑模式",
            "OBJECT": "对象模式",
        }.get(target.mode, target.mode)
        box.label(text="{} · {}".format(target.name, mode_text), icon="MESH_DATA")
    else:
        active = getattr(context, "active_object", None)
        if active is not None:
            box.label(
                text="活动对象不是网格：{}".format(active.name),
                icon="INFO",
            )
        else:
            box.label(text="请选择或激活一个网格对象。", icon="INFO")

    row = box.row(align=True)
    if target is not None and target.mode == "EDIT":
        row.operator("hard_surface.exit_edit", text="返回对象模式", icon="OBJECT_DATA")
    else:
        row.operator("hard_surface.enter_edit", text="进入编辑模式", icon="PLAY")
    if getattr(context, "mode", "") == "EDIT_MESH":
        box.label(text="当前处于 Edit Mode。", icon="INFO")


def _draw_tool_browser(layout, settings):
    box = layout.box()
    box.label(text="工具", icon="TOOL_SETTINGS")
    box.prop(settings, "category", text="分类")
    box.prop(settings, "tool_search", text="搜索", icon="VIEWZOOM")

    descriptor = state.descriptor(getattr(settings, "tool", ""))
    current_label = descriptor["label"] if descriptor is not None else "选择工具"
    box.menu("HSW_MT_tool_menu", text="工具：{}".format(current_label))
    if descriptor is None:
        if state.CATALOG:
            box.label(text="请从上方工具菜单选择工具。", icon="INFO")
        else:
            box.label(text="没有可用工具；请检查 core.catalog()。", icon="INFO")
        return

    box.separator()
    box.label(text=descriptor["label"], icon="MESH_DATA")
    box.label(text="分类：{}".format(descriptor.get("_category_label", "")))
    description = descriptor.get("description", "")
    if description:
        for line in _wrap(description, width=54, limit=4):
            box.label(text=line)
    domains = descriptor.get("domains") or []
    domain_text = " / ".join(
        "{} {}".format(domain, state.DOMAIN_LABELS.get(domain, domain))
        for domain in domains
    ) or "未声明"
    box.label(text="适用域：{}".format(domain_text))
    if descriptor.get("topology_changes"):
        box.label(text="此工具会改变拓扑。", icon="ERROR")
    else:
        box.label(text="不改变拓扑。", icon="INFO")
    tool_context = descriptor.get("context")
    if tool_context and tool_context != "SINGLE_EDIT_MESH":
        box.label(text="上下文：{}".format(tool_context), icon="INFO")
    if tool_context == "EXPLICIT_GUIDE_FRAME":
        box.label(text="命名标注/蜡笔帧维护工具；不修改网格几何。", icon="INFO")
    box.label(text="ID：{}".format(descriptor["tool"]))


def _draw_parameter(box, params, binding):
    if binding.kind == "const":
        row = box.row()
        row.label(text=binding.label)
        row.label(text=str(binding.default))
        return

    if binding.kind == "scalar":
        if binding.null_prop:
            row = box.row(align=True)
            row.prop(params, binding.null_prop, text="{}：空值".format(binding.label))
            if getattr(params, binding.null_prop):
                row.label(text="None")
                return
        if binding.item_type == "boolean":
            box.prop(params, binding.prop, text=binding.label)
        elif binding.item_type in ("integer", "number"):
            box.prop(params, binding.prop, text=binding.label, slider=True)
        else:
            box.prop(params, binding.prop, text=binding.label)
        return

    if binding.kind == "flags":
        box.label(text="{}:".format(binding.label))
        if len(binding.choices) <= 4:
            column = box.column(align=True)
            for prop, _value, label, _description in binding.choices:
                column.prop(params, prop, text=label)
        else:
            grid = box.grid_flow(
                row_major=True, columns=2, even_columns=True,
                even_rows=True, align=True,
            )
            for prop, _value, label, _description in binding.choices:
                grid.prop(params, prop, text=label)
        return

    if binding.kind == "fixed":
        box.label(text="{}:".format(binding.label))
        column = box.column(align=True)
        for index, (prop, _default) in enumerate(binding.items):
            if binding.item_type in ("integer", "number"):
                column.prop(
                    params, prop, text="{} [{}]".format(binding.label, index + 1),
                    slider=True,
                )
            else:
                column.prop(
                    params, prop, text="{} [{}]".format(binding.label, index + 1)
                )
        return

    if binding.kind == "json":
        row = box.row()
        row.use_property_split = False
        row.prop(params, binding.prop, text=binding.label)
        box.label(text="特殊/大数组参数按 JSON 文本绑定。", icon="INFO")


def _draw_parameters(layout, context, settings):
    descriptor = state.descriptor(getattr(settings, "tool", ""))
    if descriptor is None:
        layout.label(text="请选择一个工具。", icon="INFO")
        return
    params = getattr(settings, "params", None)
    if params is None:
        layout.label(text="工具参数状态未初始化。", icon="ERROR")
        return
    bindings = state.iter_bindings(settings.tool)
    if not bindings:
        layout.label(text="此工具没有参数。", icon="INFO")
        return

    layout.use_property_split = True
    layout.use_property_decorate = False
    box = layout.box()
    for binding in bindings:
        _draw_parameter(box, params, binding)
    layout.separator()
    layout.operator("hard_surface.reset_params", text="重置参数", icon="FILE_REFRESH")


class HSW_MT_selection_menu(bpy.types.Menu):
    bl_idname = "HSW_MT_selection_menu"
    bl_label = "硬表面选取"

    def draw(self, context):
        layout = self.layout
        for source, label, _ in state.SELECTOR_SOURCES:
            if source != "current":
                layout.operator("hard_surface.preview_selection", text=label).source = source
        layout.separator()
        layout.operator("hard_surface.preview_selection")
        layout.operator("hard_surface.pick_path_direction")


def _draw_select_menu(self, context):
    self.layout.menu(HSW_MT_selection_menu.bl_idname)


def _draw_selection(layout, context, settings):
    target = _display_target(context, settings)
    box = layout.box()
    box.label(text="选区来源", icon="RESTRICT_SELECT_OFF")
    box.prop(settings, "selector_source", text="来源")

    source = settings.selector_source
    if source == "current":
        box.prop(settings, "current_domain", text="解释为")
        mode_text = _select_mode_text(context)
        if mode_text:
            box.label(text="当前选择模式：{}".format(mode_text), icon="INFO")
    elif source in ("edge_loop", "edge_ring", "vertex_loop"):
        box.prop(settings, "seed_edge", text="种子边")
        if (settings.seed_edge or "").strip():
            box.label(text="使用显式种子边。", icon="INFO")
        else:
            active_edge = _active_element_index(target, bmesh.types.BMEdge)
            if active_edge is None:
                box.label(text="未找到活动边；请点选一条边或填写种子边。", icon="INFO")
            else:
                box.label(text="当前活动边：{}".format(active_edge), icon="INFO")
        box.label(text="留空种子边时使用活动边。", icon="INFO")
    elif source == "face_loop":
        box.prop(settings, "seed_face", text="种子面（-1=活动）")
        box.prop(settings, "direction_edge", text="方向边")
        if settings.seed_face < 0:
            active_face = _active_element_index(target, bmesh.types.BMFace)
            if active_face is None:
                box.label(text="未找到活动面；请点选一个四边面。", icon="INFO")
            else:
                box.label(text="当前活动面：{}".format(active_face), icon="INFO")
        box.label(text="方向边可填边索引或两端点索引。", icon="INFO")
    elif source == "indices":
        box.prop(settings, "indices_domain", text="域")
        box.prop(settings, "explicit_indices", text="索引")
        box.label(text="例：3,0,1 或 0-5 或 [3,0,1]；顺序会保留。", icon="INFO")
    elif source == "box":
        for prop in ("box_domain", "box_space", "box_minimum", "box_maximum"):
            box.prop(settings, prop)
        box.label(text="边和面的所有顶点须在范围内。", icon="INFO")

    if source in ("edge_loop", "edge_ring", "vertex_loop", "face_loop"):
        box.prop(settings, "sample_path")
        if settings.sample_path:
            box.operator("hard_surface.pick_path_direction", text="读取点选顺序")
            row = box.row(align=True)
            row.prop(settings, "sample_start")
            row.prop(settings, "sample_toward", text="下一站（-1=端点）")
            row = box.row(align=True)
            row.prop(settings, "sample_step")
            row.prop(settings, "sample_phase")
            box.prop(settings, "sample_limit", text="最大遍历站数（0=全路径）")

    row = box.row(align=True)
    row.operator("hard_surface.preview_selection", text="预览并高亮", icon="RESTRICT_SELECT_OFF")
    row.operator("hard_surface.clear_preview", text="清除", icon="CANCEL")

    if settings.preview_json:
        info = layout.box()
        domain = state.DOMAIN_LABELS.get(
            settings.preview_domain, settings.preview_domain
        )
        info.label(
            text="已锁定：{} 个{}元素".format(settings.preview_count, domain),
            icon="CHECKMARK",
        )
        if settings.preview_object_name:
            info.label(text="对象：{}".format(settings.preview_object_name))
        info.label(text="拓扑指纹：{}…".format(settings.preview_hash[:12]))
        info.prop(settings, "show_indices", text="显示索引")
        if settings.show_indices and settings.preview_indices:
            for line in _wrap(settings.preview_indices, width=58, limit=8):
                info.label(text=line)
        if settings.preview_order:
            info.label(text="选择顺序（ACTIVE / 连接使用）：")
            for line in _wrap(settings.preview_order, width=58, limit=6):
                info.label(text=line)
        info.label(text="执行会复用此范围；修改来源后自动清除。", icon="INFO")
    else:
        if settings.needs_preview:
            warning = layout.box()
            warning.alert = True
            for index, line in enumerate(_wrap(
                    "需要重新预览：上次预览已失效、目标变化或拓扑已改变。",
                    width=52, limit=3)):
                warning.label(text=line, icon="ERROR" if index == 0 else "NONE")
        else:
            layout.label(text="先预览选区，再执行；首次执行会自动预览。", icon="INFO")


def _draw_run(layout, context, settings):
    descriptor = state.descriptor(getattr(settings, "tool", ""))
    target = _display_target(context, settings)
    box = layout.box()
    box.label(text="执行", icon="PLAY")
    box.prop(settings, "repeat", text="核心重复次数", slider=True)
    box.prop(settings, "use_protect", text="保护顶点组")
    if settings.use_protect:
        if target is not None:
            box.prop_search(
                settings, "protect_group", target, "vertex_groups", text="顶点组"
            )
            group_name = (settings.protect_group or "").strip()
            if not group_name:
                box.label(text="请选择一个顶点组。", icon="INFO")
            elif target.vertex_groups.get(group_name) is None:
                box.label(text="目标对象不存在该顶点组。", icon="ERROR")
        else:
            box.prop(settings, "protect_group", text="顶点组")
    box.prop(settings, "allow_expansion", text="允许扩大选区")

    if descriptor is not None:
        if descriptor.get("topology_changes"):
            box.label(text="执行后拓扑会改变，需要重新预览。", icon="INFO")
        label = descriptor.get("label") or descriptor["tool"]
        column = box.column()
        column.scale_y = 1.35
        column.operator(
            "hard_surface.execute",
            text="执行：{}".format(label),
            icon="PLAY",
        )
    else:
        box.label(text="请选择工具。", icon="INFO")
    if settings.needs_preview and not settings.preview_json:
        box.label(text="需先预览并确认选区。", icon="ERROR")
    box.label(text="执行前会锁定/高亮当前范围。", icon="INFO")


def _draw_main(layout, context):
    settings = _panel_settings(context)
    if settings is None:
        layout.label(text="Hard Surface 面板状态未初始化。", icon="ERROR")
        return
    _draw_status(layout, settings)
    _draw_catalog_error(layout)
    _draw_target(layout, context, settings)
    _draw_tool_browser(layout, settings)
    _draw_result(layout, settings)


class VIEW3D_PT_hard_surface(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_hard_surface"
    bl_label = "Hard Surface"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"

    def draw(self, context):
        _draw_main(self.layout, context)


class HSW_MT_tool_menu(bpy.types.Menu):
    bl_idname = "HSW_MT_tool_menu"
    bl_label = "选择工具"

    def draw(self, context):
        settings = _panel_settings(context)
        if settings is None:
            self.layout.label(text="面板状态未初始化。", icon="ERROR")
            return
        current = state.descriptor(getattr(settings, "tool", ""))
        tools = state.available_tools(
            getattr(settings, "category", state.ALL_CATEGORIES),
            getattr(settings, "tool_search", ""),
        )
        if current is not None and current not in tools:
            operator = self.layout.operator(
                "hard_surface.choose_tool",
                text="当前 · {}".format(current["label"]),
                icon="CHECKMARK",
            )
            operator.tool_id = current["tool"]
            self.layout.separator()
        if not tools:
            self.layout.label(text="没有匹配的工具。", icon="INFO")
            return
        for descriptor in tools:
            operator = self.layout.operator(
                "hard_surface.choose_tool",
                text=descriptor["label"],
                icon="CHECKMARK" if (
                    current is not None and descriptor["tool"] == current["tool"]
                ) else "NONE",
            )
            operator.tool_id = descriptor["tool"]


class VIEW3D_PT_hard_surface_parameters(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_hard_surface_parameters"
    bl_label = "工具参数"
    bl_parent_id = "VIEW3D_PT_hard_surface"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"
    bl_order = 1

    def draw(self, context):
        settings = _panel_settings(context)
        if settings is None:
            self.layout.label(text="面板状态未初始化。", icon="ERROR")
            return
        _draw_parameters(self.layout, context, settings)


class VIEW3D_PT_hard_surface_selection(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_hard_surface_selection"
    bl_label = "选区与预览"
    bl_parent_id = "VIEW3D_PT_hard_surface"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"
    bl_order = 2

    def draw(self, context):
        settings = _panel_settings(context)
        if settings is None:
            self.layout.label(text="面板状态未初始化。", icon="ERROR")
            return
        _draw_selection(self.layout, context, settings)


class VIEW3D_PT_hard_surface_run(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_hard_surface_run"
    bl_label = "执行选项"
    bl_parent_id = "VIEW3D_PT_hard_surface"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"
    bl_order = 3

    def draw(self, context):
        settings = _panel_settings(context)
        if settings is None:
            self.layout.label(text="面板状态未初始化。", icon="ERROR")
            return
        _draw_run(self.layout, context, settings)


CLASSES = (
    HSW_MT_tool_menu,
    HSW_MT_selection_menu,
    VIEW3D_PT_hard_surface,
    VIEW3D_PT_hard_surface_parameters,
    VIEW3D_PT_hard_surface_selection,
    VIEW3D_PT_hard_surface_run,
)


_menu_registered = False


def register():
    global _menu_registered
    for cls in CLASSES:
        existing = getattr(bpy.types, cls.__name__, None)
        if existing is not None:
            state._unregister_class(existing)
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_select_edit_mesh.append(_draw_select_menu)
    _menu_registered = True


def unregister():
    global _menu_registered
    if _menu_registered:
        bpy.types.VIEW3D_MT_select_edit_mesh.remove(_draw_select_menu)
        _menu_registered = False
    for cls in reversed(CLASSES):
        state._unregister_class(cls)
