"""Panel state and descriptor-driven Blender property bindings.

The parent package owns the operations.  This module only turns the JSON-like
descriptors returned by ``core.catalog()`` into real Blender properties so the
sidebar can offer native widgets instead of a free-form JSON box.
"""

from copy import deepcopy
import hashlib
import json
import math

import bpy


DOMAINS = ("VERT", "EDGE", "FACE")
DOMAIN_LABELS = {"VERT": "顶点", "EDGE": "边", "FACE": "面"}
SELECTOR_SOURCES = (
    ("current", "当前 Edit 选择", "使用当前编辑模式的真实选择"),
    ("edge_loop", "边循环 Loop", "从活动边或给定种子边追踪连续边"),
    ("edge_ring", "边环 Ring", "沿四边面的对边追踪边环"),
    ("vertex_loop", "顶点循环", "读取边循环上的顶点"),
    ("face_loop", "面带", "由种子四边面及其方向边确定面带"),
    ("indices", "显式索引列表", "直接给出元素索引"),
    ("box", "坐标范围", "选取三轴范围内的顶点、完整边或完整面"),
)
ALL_CATEGORIES = "__all__"
NO_TOOL = "__none__"

CATALOG = {}
CATALOG_ERROR = ""
CATEGORIES = []
BINDINGS = {}

_PARAM_CLASS = None
_SETTINGS_CLASS = None
_STATE_REGISTERED = False

_CATEGORY_NAMES = {
    "native": "网格工具",
    "looptools": "LoopTools",
    "edgeflow": "EdgeFlow",
    "align": "对齐工具",
    "array": "阵列工具",
    "edit_mesh_tools": "编辑网格工具",
    "form": "形体工具",
    "support": "支撑工具",
    "vertex": "顶点工具",
    "stack": "堆栈",
    "other": "其他",
}
_CATEGORY_ORDER = {"native": 0, "looptools": 1, "edgeflow": 2, "stack": 3, "other": 90}
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1


class Binding:
    """How one descriptor parameter maps to one or more Blender properties."""

    __slots__ = (
        "name", "prop", "kind", "item_type", "label", "description",
        "default", "value_default", "null_prop", "null_default",
        "choices", "items", "json_default", "spec",
    )

    def __init__(self, name, prop, kind, *, item_type=None, label=None,
                 description="", default=None, value_default=None,
                 null_prop=None, null_default=False, choices=None, items=None,
                 json_default="", spec=None):
        self.name = name
        self.prop = prop
        self.kind = kind
        self.item_type = item_type
        self.label = label or _pretty(name)
        self.description = description or self.label
        self.default = default
        self.value_default = value_default
        self.null_prop = null_prop
        self.null_default = bool(null_default)
        self.choices = list(choices or ())
        self.items = list(items or ())
        self.json_default = json_default
        self.spec = spec or {}


def _pretty(text):
    raw = str(text).replace("_", " ").replace(".", " ").strip()
    return " ".join(part.capitalize() for part in raw.split()) or str(text)


def _text(value, fallback=""):
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return fallback


def _tag_redraw(context):
    try:
        area = getattr(context, "area", None)
        if area is not None:
            area.tag_redraw()
    except Exception:
        pass


def _load_catalog():
    errors = []
    try:
        from .. import core
    except Exception as exc:
        return {}, "无法在运行时导入父级 core：{}".format(exc)
    try:
        raw = core.catalog()
    except Exception as exc:
        return {}, "core.catalog() 调用失败：{}".format(exc)
    if not isinstance(raw, dict):
        return {}, "core.catalog() 必须返回 dict"

    result = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            errors.append("忽略非法工具标识 {!r}".format(key))
            continue
        if not isinstance(value, dict):
            errors.append("忽略 {}：descriptor 不是 dict".format(key))
            continue
        desc = dict(value)
        declared = desc.get("tool")
        if declared is not None and declared != key:
            errors.append("{}：descriptor.tool={!r} 与键不一致，按键处理".format(key, declared))
        desc["tool"] = key
        desc["label"] = _text(desc.get("label") or desc.get("name"), _pretty(key))
        desc["description"] = _text(desc.get("description"), "")

        parameters = desc.get("parameters")
        if parameters is None:
            parameters = {}
        elif not isinstance(parameters, dict):
            errors.append("{}：parameters 不是 dict".format(key))
            parameters = {}
        desc["parameters"] = parameters

        domains = desc.get("domains")
        if isinstance(domains, (list, tuple)):
            desc["domains"] = [str(domain) for domain in domains if str(domain) in DOMAINS]
        else:
            desc["domains"] = []
        desc["topology_changes"] = bool(desc.get("topology_changes", False))

        category = desc.get("category")
        if not isinstance(category, str) or not category:
            category = key.split(".", 1)[0] if "." in key else "other"
        category_label = desc.get("category_label")
        if not isinstance(category_label, str) or not category_label:
            category_label = _CATEGORY_NAMES.get(category, _pretty(category))
        desc["_category"] = category
        desc["_category_label"] = category_label
        result[key] = desc

    if not result and not errors:
        errors.append("core.catalog() 未返回任何工具")
    return result, "；".join(errors)


def _category_sort_key(category):
    return (_CATEGORY_ORDER.get(category, 50), _CATEGORY_NAMES.get(category, _pretty(category)).lower())


def _build_categories(catalog):
    labels = {}
    for desc in catalog.values():
        labels.setdefault(desc["_category"], desc["_category_label"])
    return sorted(labels.items(), key=lambda item: _category_sort_key(item[0]))


def available_tools(category=ALL_CATEGORIES, search=""):
    """Return descriptors filtered by category and an all-token search."""
    query = (search or "").strip().lower()
    tokens = query.split()
    order = {category_id: index for index, (category_id, _label) in enumerate(CATEGORIES)}
    result = []
    for tool_id, desc in CATALOG.items():
        if category not in (None, "", ALL_CATEGORIES) and desc["_category"] != category:
            continue
        haystack = " ".join((
            tool_id, desc["label"], desc["_category_label"], desc["description"],
        )).lower()
        if tokens and any(token not in haystack for token in tokens):
            continue
        result.append(desc)
    result.sort(key=lambda desc: (
        order.get(desc["_category"], 50),
        desc["_category_label"].lower(),
        desc["label"].lower(),
        desc["tool"],
    ))
    return result


def _default_tool_id():
    tools = available_tools()
    return tools[0]["tool"] if tools else NO_TOOL


def _category_items():
    """Static category list; a callback here is unnecessary and can recurse."""
    items = [(ALL_CATEGORIES, "全部", "显示所有可用工具")]
    items.extend((category_id, label, "工具分类：{}".format(label))
                 for category_id, label in CATEGORIES)
    return items


def _on_target_update(self, context):
    invalidate_preview(
        self, "目标对象已更改，请重新预览选区。", require_preview=True
    )
    _tag_redraw(context)


def _on_category_update(self, context):
    matches = available_tools(self.category, self.tool_search)
    match_ids = {desc["tool"] for desc in matches}
    if matches and self.tool not in match_ids:
        self.tool = matches[0]["tool"]
    elif not matches and self.tool != NO_TOOL:
        self.tool = NO_TOOL
    _tag_redraw(context)


def _on_search_update(self, context):
    _tag_redraw(context)


def _on_tool_update(self, context):
    self.last_result = ""
    desc = CATALOG.get(getattr(self, "tool", ""))
    set_status(
        self,
        "已选择工具：{}".format(desc["label"]) if desc is not None else "",
        "INFO",
    )
    _tag_redraw(context)


def _on_selector_update(self, context):
    invalidate_preview(
        self, "选区来源已更改，请重新预览选区。", require_preview=True
    )
    _tag_redraw(context)


def _mesh_poll(self, obj):
    return getattr(obj, "type", None) == "MESH"


def _enum_choices(spec):
    raw = spec.get("enum")
    labels = spec.get("enum_labels") if isinstance(spec.get("enum_labels"), dict) else {}
    descriptions = spec.get("enum_descriptions") if isinstance(spec.get("enum_descriptions"), dict) else {}
    pairs = []
    if isinstance(raw, dict):
        for value, label in raw.items():
            pairs.append((value, label, descriptions.get(value, "")))
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                value = item.get("value", item.get("id"))
                label = item.get("label", item.get("name", labels.get(value, value)))
                description = item.get("description", descriptions.get(value, ""))
            else:
                value = item
                label = labels.get(item, item)
                description = descriptions.get(item, "")
            if value is not None:
                pairs.append((value, label, description))
    result = []
    seen = set()
    for value, label, description in pairs:
        value = _text(value)
        if not value or value in seen:
            continue
        seen.add(value)
        result.append((value, _text(label, value), _text(description, "")))
    return result


def _finite_float(value, fallback):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return result if math.isfinite(result) else fallback


def _int32(value, fallback):
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return max(_INT32_MIN, min(_INT32_MAX, result))


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _json_default(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return "null"


def _next_prop(base, used, suffix=""):
    candidate = "{}{}".format(base, suffix)
    index = 1
    while candidate in used:
        index += 1
        candidate = "{}_{}".format(base, index)
    used.add(candidate)
    return candidate


def _fixed_count(spec, default_list):
    try:
        min_items = int(spec.get("min_items", spec.get("minItems", 0)) or 0)
    except (TypeError, ValueError, OverflowError):
        min_items = 0
    raw_max = spec.get("max_items", spec.get("maxItems"))
    max_items = None
    if raw_max is not None:
        try:
            max_items = int(raw_max)
        except (TypeError, ValueError, OverflowError):
            max_items = None
    if max_items is not None and max_items <= 0:
        max_items = None
    if max_items is not None and max_items <= 8:
        if min_items == max_items or len(default_list) == max_items:
            return max(max_items, 1)
    if max_items is None and default_list and len(default_list) <= 8:
        return max(len(default_list), min_items, 1)
    if min_items > 0 and not default_list and min_items <= 8 and max_items == min_items:
        return min_items
    return None


def _make_binding(tool_id, name, spec, annotations, used):
    if not isinstance(spec, dict):
        spec = {"type": "object", "default": None, "description": "特殊参数"}
    spec = dict(spec)
    kind = _text(spec.get("type"), "string").lower()
    kind = {"int": "integer", "float": "number", "bool": "boolean"}.get(kind, kind)
    label = _text(spec.get("label") or spec.get("name"), _pretty(name))
    description = _text(spec.get("description"), label)
    default = spec.get("default", spec.get("const"))
    if "const" in spec and default is None:
        default = spec.get("const")
    nullable = bool(spec.get("nullable", False))
    base = "p_" + hashlib.sha1(
        ("{}\0{}".format(tool_id, name)).encode("utf-8")
    ).hexdigest()[:12]

    if "const" in spec:
        return Binding(
            name, base, "const", label=label, description=description,
            default=deepcopy(default), spec=spec,
        )

    if kind in ("integer", "number", "boolean", "string"):
        null_prop = None
        null_default = nullable and default is None
        if null_default:
            null_prop = _next_prop(base, used, "_null")
            annotations[null_prop] = bpy.props.BoolProperty(
                name="{}：空值".format(label),
                description="勾选后向 core 传递 None。",
                default=True,
            )
        prop = _next_prop(base, used)
        value_default = None

        if kind == "boolean":
            value_default = bool(default) if default is not None else False
            annotations[prop] = bpy.props.BoolProperty(
                name=label, description=description, default=value_default,
            )
        elif kind == "integer":
            minimum = spec.get("min", spec.get("minimum"))
            maximum = spec.get("max", spec.get("maximum"))
            value_default = _int32(default, 0)
            minimum = _int32(minimum, _INT32_MIN) if minimum is not None else None
            maximum = _int32(maximum, _INT32_MAX) if maximum is not None else None
            if minimum is not None and maximum is not None and minimum > maximum:
                minimum, maximum = maximum, minimum
            if minimum is not None:
                value_default = max(minimum, value_default)
            if maximum is not None:
                value_default = min(maximum, value_default)
            keywords = {"name": label, "description": description, "default": value_default}
            if minimum is not None:
                keywords["min"] = minimum
            if maximum is not None:
                keywords["max"] = maximum
            annotations[prop] = bpy.props.IntProperty(**keywords)
        elif kind == "number":
            minimum = spec.get("min", spec.get("minimum"))
            maximum = spec.get("max", spec.get("maximum"))
            value_default = _finite_float(default, 0.0)
            minimum = _finite_float(minimum, None) if minimum is not None else None
            maximum = _finite_float(maximum, None) if maximum is not None else None
            if minimum is not None and maximum is not None and minimum > maximum:
                minimum, maximum = maximum, minimum
            if minimum is not None:
                value_default = max(minimum, value_default)
            if maximum is not None:
                value_default = min(maximum, value_default)
            keywords = {"name": label, "description": description, "default": value_default}
            if minimum is not None:
                keywords["min"] = minimum
            if maximum is not None:
                keywords["max"] = maximum
            precision = spec.get("precision")
            if precision is not None:
                try:
                    keywords["precision"] = max(0, min(6, int(precision)))
                except (TypeError, ValueError, OverflowError):
                    pass
            step = spec.get("step")
            if step is not None:
                try:
                    keywords["step"] = max(1, min(1000, int(step)))
                except (TypeError, ValueError, OverflowError):
                    pass
            annotations[prop] = bpy.props.FloatProperty(**keywords)
        elif kind == "string":
            choices = _enum_choices(spec)
            if tool_id == "native.bevel" and name == "affect":
                # Append rather than reorder so saved enum values keep their indices.
                choices.append(("AUTO", "自动", "随实际顶点或边选区决定倒角作用域"))
                default = "AUTO"
                description = "随实际顶点或边选区决定倒角作用域，也可显式指定。"
                spec.update(enum=[value for value, _, _ in choices],
                            default=default, description=description)
            if choices:
                value_default = _text(default, choices[0][0])
                if value_default not in {choice[0] for choice in choices}:
                    value_default = choices[0][0]
                annotations[prop] = bpy.props.EnumProperty(
                    name=label, description=description,
                    items=[(value, choice_label, choice_description)
                           for value, choice_label, choice_description in choices],
                    default=value_default,
                )
            else:
                value_default = _text(default, "")
                annotations[prop] = bpy.props.StringProperty(
                    name=label, description=description, default=value_default,
                )

        return Binding(
            name, prop, "scalar", item_type=kind, label=label,
            description=description, default=default,
            value_default=value_default, null_prop=null_prop,
            null_default=null_default, spec=spec,
        )

    # FloatProperty stores float32. World anchors need to carry the typed
    # decimal coordinates through to the core's storage precision check.
    world_anchor = tool_id == "align.reference" and name in {"source_world", "target_world", "target_end_world"}
    if kind == "array" and not world_anchor:
        choices = _enum_choices(spec)
        items_spec = spec.get("items")
        if not choices and isinstance(items_spec, dict):
            choices = _enum_choices(items_spec)
        default_list = _as_list(default)

        if choices:
            choice_bindings = []
            for index, (value, choice_label, choice_description) in enumerate(choices):
                prop = _next_prop(base, used, "_f{}".format(index))
                selected = value in default_list or value in {str(item) for item in default_list}
                annotations[prop] = bpy.props.BoolProperty(
                    name=choice_label, description=choice_description or choice_label,
                    default=selected,
                )
                choice_bindings.append((prop, value, choice_label, choice_description))
            return Binding(
                name, base, "flags", item_type="string", label=label,
                description=description, default=default_list,
                choices=choice_bindings, spec=spec,
            )

        item_type = ""
        if isinstance(items_spec, dict):
            item_type = _text(items_spec.get("type"), "").lower()
            item_type = {"int": "integer", "float": "number", "bool": "boolean"}.get(item_type, item_type)
        count = _fixed_count(spec, default_list)
        if count and item_type in ("integer", "number", "string"):
            item_bindings = []
            normalized_defaults = []
            for index in range(count):
                raw_default = default_list[index] if index < len(default_list) else None
                if item_type == "integer":
                    value_default = _int32(raw_default, 0)
                elif item_type == "number":
                    value_default = _finite_float(raw_default, 0.0)
                else:
                    value_default = _text(raw_default, "")
                prop = _next_prop(base, used, "_i{}".format(index))
                item_label = "{} [{}]".format(label, index + 1)
                if item_type == "integer":
                    annotations[prop] = bpy.props.IntProperty(
                        name=item_label, description=description, default=value_default,
                    )
                elif item_type == "number":
                    keywords = {"name": item_label, "description": description,
                                "default": value_default}
                    minimum = items_spec.get("min", items_spec.get("minimum")) if isinstance(items_spec, dict) else None
                    maximum = items_spec.get("max", items_spec.get("maximum")) if isinstance(items_spec, dict) else None
                    minimum = _finite_float(minimum, None) if minimum is not None else None
                    maximum = _finite_float(maximum, None) if maximum is not None else None
                    if minimum is not None:
                        keywords["min"] = minimum
                    if maximum is not None:
                        keywords["max"] = maximum
                    annotations[prop] = bpy.props.FloatProperty(**keywords)
                else:
                    annotations[prop] = bpy.props.StringProperty(
                        name=item_label, description=description, default=value_default,
                    )
                item_bindings.append((prop, value_default))
                normalized_defaults.append(value_default)
            return Binding(
                name, base, "fixed", item_type=item_type, label=label,
                description=description, default=normalized_defaults,
                items=item_bindings, spec=spec,
            )

    # JSON preserves precise coordinates and variable array shapes.
    prop = _next_prop(base, used)
    json_text = _json_default(default)
    annotations[prop] = bpy.props.StringProperty(
        name=label,
        description="{}（以 JSON 文本输入）。".format(description),
        default=json_text,
    )
    return Binding(
        name, prop, "json", label=label, description=description,
        default=deepcopy(default), json_default=json_text, spec=spec,
    )


def _make_param_class(catalog):
    annotations = {}
    bindings = {}
    used = set()
    for tool_id in sorted(catalog):
        tool_bindings = []
        parameters = catalog[tool_id].get("parameters", {})
        for name, spec in parameters.items():
            if not isinstance(name, str) or not name:
                continue
            try:
                binding = _make_binding(tool_id, name, spec, annotations, used)
            except Exception:
                binding = None
            if binding is not None:
                tool_bindings.append(binding)
        bindings[tool_id] = tool_bindings
    annotations["hswparam_version"] = bpy.props.IntProperty(default=1)
    cls = type(
        "HSW_PG_tool_parameters",
        (bpy.types.PropertyGroup,),
        {"__annotations__": annotations, "__module__": __name__},
    )
    return cls, bindings


def _make_empty_param_class():
    annotations = {"hswparam_version": bpy.props.IntProperty(default=1)}
    return type(
        "HSW_PG_tool_parameters",
        (bpy.types.PropertyGroup,),
        {"__annotations__": annotations, "__module__": __name__},
    )


def _make_settings_class(param_class):
    annotations = {
        "target_object": bpy.props.PointerProperty(
            type=bpy.types.Object,
            name="目标网格",
            description="留空时自动使用当前活动网格对象。",
            poll=_mesh_poll,
            update=_on_target_update,
        ),
        "params": bpy.props.PointerProperty(
            type=param_class,
            name="工具参数",
        ),
        "category": bpy.props.EnumProperty(
            name="分类",
            description="按工具分类筛选下拉列表。",
            items=_category_items(),
            default=ALL_CATEGORIES,
            update=_on_category_update,
        ),
        "tool_search": bpy.props.StringProperty(
            name="搜索工具",
            description="用空格分隔多个关键词，匹配名称、标识和说明。",
            default="",
            update=_on_search_update,
        ),
        "tool": bpy.props.StringProperty(
            name="工具",
            description="core.catalog() 提供的工具。",
            default=_default_tool_id(),
            update=_on_tool_update,
        ),
        "selector_source": bpy.props.EnumProperty(
            name="选区来源",
            description="决定如何把当前编辑选择转换为 core 选区。",
            items=SELECTOR_SOURCES,
            default="current",
            update=_on_selector_update,
        ),
        "current_domain": bpy.props.EnumProperty(
            name="当前选择的域",
            description="自动时按当前编辑选择模式取顶点/边/面。",
            items=(
                ("AUTO", "自动", "跟随当前编辑选择模式"),
                ("VERT", "顶点", "把当前选择解释为顶点"),
                ("EDGE", "边", "把当前选择解释为边"),
                ("FACE", "面", "把当前选择解释为面"),
            ),
            default="AUTO",
            update=_on_selector_update,
        ),
        "indices_domain": bpy.props.EnumProperty(
            name="显式索引的域",
            items=(
                ("VERT", "顶点", "顶点索引"),
                ("EDGE", "边", "边索引"),
                ("FACE", "面", "面索引"),
            ),
            default="EDGE",
            update=_on_selector_update,
        ),
        "seed_edge": bpy.props.StringProperty(
            name="种子边",
            description="留空使用活动边；可填边索引或两端点索引（如 3 或 0,1）。",
            default="",
            update=_on_selector_update,
        ),
        "seed_face": bpy.props.IntProperty(
            name="种子面",
            description="-1 表示活动面，否则使用给定面索引。",
            default=-1,
            min=-1,
            max=_INT32_MAX,
            update=_on_selector_update,
        ),
        "direction_edge": bpy.props.StringProperty(
            name="方向边",
            description="面循环所需的显式方向边索引，或两端点索引（如 8 或 4,5）。",
            default="",
            update=_on_selector_update,
        ),
        "explicit_indices": bpy.props.StringProperty(
            name="索引",
            description="逗号/空格分隔或 [] JSON 数组；支持 3-7 闭区间；填写顺序会保留给 ACTIVE 操作。",
            default="",
            update=_on_selector_update,
        ),
        "box_domain": bpy.props.EnumProperty(
            name="元素", items=[(key, label, "") for key, label in DOMAIN_LABELS.items()],
            default="VERT", update=_on_selector_update,
        ),
        "box_space": bpy.props.EnumProperty(
            name="坐标", items=(("LOCAL", "物体局部", "源笼坐标"), ("WORLD", "世界", "应用对象世界变换后的坐标")),
            default="LOCAL", update=_on_selector_update,
        ),
        "box_minimum": bpy.props.FloatVectorProperty(name="XYZ 下界", size=3, update=_on_selector_update),
        "box_maximum": bpy.props.FloatVectorProperty(name="XYZ 上界", size=3, update=_on_selector_update),
        "sample_path": bpy.props.BoolProperty(name="沿路径取样", update=_on_selector_update),
        "sample_start": bpy.props.IntProperty(name="起点索引", min=-1, default=-1, update=_on_selector_update),
        "sample_toward": bpy.props.IntProperty(name="下一站索引", min=-1, default=-1, update=_on_selector_update),
        "sample_step": bpy.props.IntProperty(name="步长", min=1, default=2, update=_on_selector_update),
        "sample_phase": bpy.props.IntProperty(name="起始偏移", min=0, update=_on_selector_update),
        "sample_limit": bpy.props.IntProperty(name="最大遍历站数", min=0, update=_on_selector_update),
        "repeat": bpy.props.IntProperty(
            name="重复次数",
            description="传给 core.execute 的 repeat 参数。",
            default=1,
            min=1,
            max=64,
            soft_max=16,
        ),
        "use_protect": bpy.props.BoolProperty(
            name="保护顶点组",
            description="执行时把指定顶点组作为 protect 传给 core.execute。",
            default=False,
        ),
        "protect_group": bpy.props.StringProperty(
            name="顶点组",
            description="目标对象上已存在的顶点组名称。",
            default="",
        ),
        "allow_expansion": bpy.props.BoolProperty(
            name="允许扩大选区",
            description="传给 core.execute 的 allow_expansion。",
            default=False,
        ),
        "show_indices": bpy.props.BoolProperty(
            name="显示索引",
            description="在面板显示预览选区索引摘要。",
            default=True,
        ),
        "preview_json": bpy.props.StringProperty(default=""),
        "preview_source": bpy.props.StringProperty(default=""),
        "preview_object_name": bpy.props.StringProperty(default=""),
        "preview_object_uid": bpy.props.IntProperty(default=0, min=0),
        "preview_mesh_name": bpy.props.StringProperty(default=""),
        "preview_mesh_uid": bpy.props.IntProperty(default=0, min=0),
        "preview_domain": bpy.props.StringProperty(default=""),
        "preview_hash": bpy.props.StringProperty(default=""),
        "preview_count": bpy.props.IntProperty(default=0, min=0),
        "preview_indices": bpy.props.StringProperty(default=""),
        "preview_order": bpy.props.StringProperty(default=""),
        "needs_preview": bpy.props.BoolProperty(default=False),
        "status": bpy.props.StringProperty(default=""),
        "status_kind": bpy.props.StringProperty(default="INFO"),
        "last_result": bpy.props.StringProperty(default=""),
        "object_context": bpy.props.StringProperty(default=""),
    }
    return type(
        "HSW_PG_panel_settings",
        (bpy.types.PropertyGroup,),
        {"__annotations__": annotations, "__module__": __name__},
    )


def _remove_scene_pointer():
    try:
        bpy.types.Scene.bl_rna.properties.get("hsw_settings")
    except Exception:
        pass
    try:
        delattr(bpy.types.Scene, "hsw_settings")
    except Exception:
        pass


def _unregister_class(cls):
    if cls is None:
        return
    try:
        bpy.utils.unregister_class(cls)
    except Exception:
        pass


def _unregister_state_classes():
    global _SETTINGS_CLASS, _PARAM_CLASS
    _unregister_class(_SETTINGS_CLASS)
    _unregister_class(_PARAM_CLASS)
    # Also clean up classes left behind by a failed or hot-reloaded install.
    for name in ("HSW_PG_panel_settings", "HSW_PG_tool_parameters"):
        cls = getattr(bpy.types, name, None)
        if cls is not None:
            _unregister_class(cls)
    _SETTINGS_CLASS = None
    _PARAM_CLASS = None


def install():
    """Load the catalog and register the dynamic scene property group."""
    global CATALOG, CATALOG_ERROR, CATEGORIES, BINDINGS
    global _PARAM_CLASS, _SETTINGS_CLASS, _STATE_REGISTERED
    if _STATE_REGISTERED:
        return

    _remove_scene_pointer()
    _unregister_state_classes()

    CATALOG, CATALOG_ERROR = _load_catalog()
    CATEGORIES = _build_categories(CATALOG)

    try:
        param_class, bindings = _make_param_class(CATALOG)
        bpy.utils.register_class(param_class)
    except Exception as exc:
        CATALOG_ERROR = "{}；参数属性注册失败：{}".format(
            CATALOG_ERROR, exc
        ) if CATALOG_ERROR else "参数属性注册失败：{}".format(exc)
        bindings = {}
        param_class = _make_empty_param_class()
        bpy.utils.register_class(param_class)
    BINDINGS = bindings
    _PARAM_CLASS = param_class

    settings_class = _make_settings_class(param_class)
    bpy.utils.register_class(settings_class)
    _SETTINGS_CLASS = settings_class
    bpy.types.Scene.hsw_settings = bpy.props.PointerProperty(type=settings_class)
    _STATE_REGISTERED = True


def uninstall():
    """Remove only the state registered by :func:`install`."""
    global CATALOG, CATALOG_ERROR, CATEGORIES, BINDINGS, _STATE_REGISTERED
    _remove_scene_pointer()
    _unregister_state_classes()
    CATALOG = {}
    CATALOG_ERROR = ""
    CATEGORIES = []
    BINDINGS = {}
    _STATE_REGISTERED = False


def get_settings(context):
    scene = getattr(context, "scene", None)
    return getattr(scene, "hsw_settings", None) if scene is not None else None


def descriptor(tool_id):
    return CATALOG.get(tool_id)


def iter_bindings(tool_id):
    return list(BINDINGS.get(tool_id, ()))


def _parse_json_value(raw, label):
    text = (raw or "").strip()
    if not text:
        raise ValueError("参数“{}”的 JSON 为空。".format(label))
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "参数“{}”的 JSON 无效：第 {} 行第 {} 列：{}".format(
                label, exc.lineno, exc.colno, exc.msg
            )
        ) from None


def collect_params(settings, tool_id):
    """Read Blender widgets into a JSON-compatible core parameter dict."""
    bindings = BINDINGS.get(tool_id)
    if bindings is None:
        if tool_id in CATALOG:
            # A malformed descriptor may have prevented the dynamic class from
            # registering; let core.apply its own defaults instead of blocking.
            return {}
        raise ValueError("工具 {!r} 不在当前面板目录中。".format(tool_id))
    params = getattr(settings, "params", None)
    if params is None:
        raise ValueError("工具参数状态未初始化。")
    result = {}
    for binding in bindings:
        if binding.kind == "const":
            result[binding.name] = deepcopy(binding.default)
        elif binding.kind == "scalar":
            if binding.null_prop and getattr(params, binding.null_prop):
                result[binding.name] = None
            else:
                value = getattr(params, binding.prop)
                if tool_id == "native.bevel" and binding.name == "affect" and value == "AUTO":
                    continue
                result[binding.name] = value
        elif binding.kind == "flags":
            result[binding.name] = [
                value for prop, value, _label, _description in binding.choices
                if getattr(params, prop)
            ]
        elif binding.kind == "fixed":
            result[binding.name] = [
                getattr(params, prop) for prop, _default in binding.items
            ]
        elif binding.kind == "json":
            result[binding.name] = _parse_json_value(
                getattr(params, binding.prop), binding.label
            )
        else:
            raise ValueError("参数“{}”的绑定类型未知。".format(binding.label))
    return result


def reset_tool_params(settings, tool_id):
    """Restore every widget of one tool to descriptor defaults."""
    params = getattr(settings, "params", None)
    if params is None:
        raise ValueError("工具参数状态未初始化。")
    for binding in BINDINGS.get(tool_id, ()):
        try:
            if binding.kind == "scalar":
                if binding.null_prop:
                    setattr(params, binding.null_prop, binding.null_default)
                setattr(params, binding.prop, binding.value_default)
            elif binding.kind == "flags":
                selected = {str(item) for item in (binding.default or ())}
                for prop, value, _label, _description in binding.choices:
                    setattr(params, prop, value in selected)
            elif binding.kind == "fixed":
                for prop, value in binding.items:
                    setattr(params, prop, value)
            elif binding.kind == "json":
                setattr(params, binding.prop, binding.json_default)
        except Exception:
            continue


def format_indices(indices, limit=120):
    items = [str(index) for index in indices[:limit]]
    if len(indices) > limit:
        items.append("… (+{})".format(len(indices) - limit))
    return ", ".join(items)


def _selection_history(value):
    if "selection_history" not in value:
        return None
    history = value.get("selection_history")
    if not isinstance(history, (list, tuple)):
        return None
    if any(type(index) is not int or index < 0 for index in history):
        return None
    if len(set(history)) != len(history):
        return None
    return list(history)


def valid_resolved(value):
    if not isinstance(value, dict):
        return False
    if value.get("domain") not in DOMAINS:
        return False
    indices = value.get("indices")
    if not isinstance(indices, (list, tuple)):
        return False
    if any(type(index) is not int or index < 0 for index in indices):
        return False
    if len(set(indices)) != len(indices):
        return False
    if "selection_history" in value:
        history = _selection_history(value)
        if history is None or not set(history) <= set(indices):
            return False
    fingerprint = value.get("topology_sha256")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        return False
    return all(character in "0123456789abcdef" for character in fingerprint)


def explicit_selector(resolved):
    """Turn a preview result into a stable explicit-index selector."""
    if not valid_resolved(resolved):
        raise ValueError("预览结果不完整，无法锁定选区。")
    indices = list(resolved["indices"])
    history = _selection_history(resolved)
    order = indices
    if (history is not None and len(history) == len(indices)
            and set(history) == set(indices)):
        order = history
    return {
        "kind": "indices",
        "domain": resolved["domain"],
        "indices": list(order),
        "topology_sha256": resolved["topology_sha256"],
    }


def cached_preview(settings):
    raw = getattr(settings, "preview_json", "")
    if not raw:
        return None
    try:
        resolved = json.loads(raw)
    except Exception:
        invalidate_preview(settings, "选区预览缓存损坏，请重新预览。")
        return None
    if not valid_resolved(resolved):
        invalidate_preview(settings, "选区预览缓存不完整，请重新预览。")
        return None
    return resolved


def store_preview(settings, resolved, source=None, object_name="",
                  object_uid=0, mesh_name="", mesh_uid=0):
    """Persist a resolved preview so execute can reuse the exact indices."""
    if not valid_resolved(resolved):
        raise ValueError("core 返回的预览缺少 domain/indices/topology_sha256。")
    indices = sorted({int(index) for index in resolved["indices"]})
    history = _selection_history(resolved)
    value = {
        "domain": resolved["domain"],
        "indices": indices,
        "topology_sha256": resolved["topology_sha256"],
    }
    if history is not None:
        value["selection_history"] = list(history)
    settings.preview_json = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    settings.preview_source = json.dumps(
        source, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ) if source is not None else ""
    settings.preview_object_name = object_name or ""
    settings.preview_object_uid = int(object_uid or 0)
    settings.preview_mesh_name = mesh_name or ""
    settings.preview_mesh_uid = int(mesh_uid or 0)
    settings.preview_domain = value["domain"]
    settings.preview_hash = value["topology_sha256"]
    settings.preview_count = len(indices)
    settings.preview_indices = format_indices(indices)
    settings.needs_preview = False
    settings.preview_order = ""
    if (history is not None and len(history) == len(indices)
            and set(history) == set(indices) and history != indices):
        settings.preview_order = format_indices(history)
    return value


def invalidate_preview(settings, message=None, require_preview=False):
    settings.preview_json = ""
    settings.preview_source = ""
    settings.preview_object_name = ""
    settings.preview_object_uid = 0
    settings.preview_mesh_name = ""
    settings.preview_mesh_uid = 0
    settings.preview_domain = ""
    settings.preview_hash = ""
    settings.preview_count = 0
    settings.preview_indices = ""
    settings.preview_order = ""
    if require_preview:
        settings.needs_preview = True
    if message:
        set_status(settings, message, "INFO")


def set_status(settings, message, kind="INFO"):
    settings.status = _text(message, "")[:4000]
    settings.status_kind = kind if kind in {"INFO", "SUCCESS", "WARNING", "ERROR"} else "INFO"
