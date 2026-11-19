"""Native operator panels for hard-surface modifiers."""

from math import tau

import bpy
from bpy.props import BoolProperty, BoolVectorProperty, EnumProperty, FloatProperty, FloatVectorProperty
from bpy.props import IntProperty, StringProperty

from . import stack_tools


NATIVE_ITEMS = [(kind, kind.replace("_", " ").title(), "")
                for kind in stack_tools.NATIVE if kind != "BOOLEAN"]
AXES = [(axis, axis, "") for axis in "XYZ"]
CURVE_AXES = [(f"{sign}_{x}", f"{'+' if sign == 'POS' else '−'}{x}", "")
              for sign in ("POS", "NEG") for x in "XYZ"]
BOOL_MODES = [(mode, mode.title(), "") for mode in ("DIFFERENCE", "UNION", "INTERSECT")]
_UI_FIELDS = {
    "BEVEL": [("width", "distance", "Width"), ("segments", "segments", "Segments")],
    "SOLIDIFY": [("thickness", "distance", "Thickness"), ("offset", "offset", "Offset")],
    "MIRROR": [("use_axis", "axes", "Axes"), ("use_clip", "flag", "Clipping")],
    "SUBSURF": [("levels", "subdivision", "Viewport Levels"), ("render_levels", "render_levels", "Render Levels")],
    "ARRAY": [("count", "count", "Count"), ("constant_offset_displace", "vector", "Local Offset")],
    "SCREW": [("axis", "axis", "Axis"), ("angle", "angle", "Angle"), ("steps", "steps", "Steps")],
    "WELD": [("merge_threshold", "positive_distance", "Merge Threshold"), ("mode", "weld_mode", "Mode")],
    "WEIGHTED_NORMAL": [("weight", "weight", "Weight"), ("keep_sharp", "flag", "Keep Sharp")],
    "SHRINKWRAP": [("target", "reference_name", "Surface"), ("offset", "distance", "Offset")],
    "LATTICE": [("object", "reference_name", "Lattice"), ("strength", "strength", "Strength")],
    "SIMPLE_DEFORM": [("deform_method", "method", "Method"), ("deform_axis", "axis", "Axis"),
                      ("factor", "factor", "Factor / Radians")],
    "CURVE": [("object", "reference_name", "Curve"), ("deform_axis", "curve_axis", "Axis")],
    "DECIMATE": [("decimate_type", "decimate_mode", "Mode"), ("ratio", "ratio", "Ratio"),
                 ("angle_limit", "limit_angle", "Angle Limit"), ("iterations", "iterations", "Iterations")],
    "TRIANGULATE": [("quad_method", "quad_method", "Quad Method"), ("ngon_method", "ngon_method", "N-gon Method"),
                    ("min_vertices", "min_vertices", "Minimum Vertices")],
    "DATA_TRANSFER": [("object", "reference_name", "Source Mesh"), ("mix_factor", "ratio", "Mix Factor")],
}
_TRANSFER_DATA = {
    "GROUPS": ("vert", "verts", "VGROUP_WEIGHTS"),
    "UV": ("loop", "loops", "UV"),
    "NORMAL": ("loop", "loops", "CUSTOM_NORMAL"),
    "SHARP": ("edge", "edges", "SHARP_EDGE"),
}


def _mesh_active(context):
    obj = context.object
    return obj is not None and obj.type == "MESH" and obj.mode == "OBJECT"


def _call(operator, context, tool, params, message=None):
    try:
        outcome = stack_tools.execute(context.object, tool, params)
    except (ValueError, TypeError, RuntimeError) as error:
        operator.report({"ERROR"}, str(error))
        return {"CANCELLED"}
    if message:
        operator.report({"INFO"}, message(outcome) if callable(message) else message)
    return {"FINISHED"}


class HSW_OT_stack_params(bpy.types.Operator):
    bl_idname = "hard_surface.stack_params"
    bl_label = "Native Modifier"
    bl_options = {"REGISTER", "UNDO"}

    action: EnumProperty(items=[("ADD", "Add", ""), ("UPDATE", "Update", "")], options={"HIDDEN"})
    modifier_name: StringProperty(name="Name")
    modifier_type: EnumProperty(name="Type", items=NATIVE_ITEMS)
    distance: FloatProperty(name="Distance", default=0.01, subtype="DISTANCE", unit="LENGTH")
    positive_distance: FloatProperty(name="Threshold", default=0.001, min=0, subtype="DISTANCE", unit="LENGTH")
    offset: FloatProperty(name="Offset", default=-1, min=-1, max=1)
    angle: FloatProperty(name="Angle", default=tau, unit="ROTATION", subtype="ANGLE")
    segments: IntProperty(name="Segments", default=3, min=1, max=32)
    count: IntProperty(name="Count", default=3, min=1, max=1000)
    steps: IntProperty(name="Steps", default=32, min=3, max=512)
    subdivision: IntProperty(name="Subdivisions", default=2, min=0, max=5)
    render_levels: IntProperty(name="Render Levels", default=2, min=0, max=5)
    flag: BoolProperty(name="Enabled", default=True)
    axes: BoolVectorProperty(name="Axes", size=3, default=(True, False, False), subtype="XYZ")
    vector: FloatVectorProperty(name="Local Offset", size=3, default=(1, 0, 0), subtype="TRANSLATION", unit="LENGTH")
    axis: EnumProperty(name="Axis", items=AXES, default="Z")
    curve_axis: EnumProperty(name="Curve Axis", items=CURVE_AXES, default="POS_X")
    method: EnumProperty(name="Method", items=[(item, item.title(), "") for item in ("TWIST", "BEND", "TAPER", "STRETCH")])
    weld_mode: EnumProperty(name="Mode", items=[("ALL", "All", ""), ("CONNECTED", "Connected", "")])
    reference_name: StringProperty(name="Reference Object")
    strength: FloatProperty(name="Strength", default=1, min=0, max=1)
    factor: FloatProperty(name="Factor", default=0.25)
    weight: IntProperty(name="Weight", min=1, max=100, default=50)
    expected_index: IntProperty(options={"HIDDEN"}, default=-1)
    ratio: FloatProperty(name="Factor", min=0, max=1, default=0.5)
    limit_angle: FloatProperty(name="Angle Limit", min=0, max=tau, default=0.0873, unit="ROTATION", subtype="ANGLE")
    iterations: IntProperty(name="Iterations", min=0, max=32, default=1)
    min_vertices: IntProperty(name="Minimum Vertices", min=3, max=1000, default=4)
    decimate_mode: EnumProperty(name="Mode", items=[(x, x.title(), "") for x in ("COLLAPSE", "UNSUBDIV", "DISSOLVE")])
    quad_method: EnumProperty(name="Quad Method", items=[(x, x.replace("_", " ").title(), "") for x in (
        "BEAUTY", "FIXED", "FIXED_ALTERNATE", "SHORTEST_DIAGONAL", "LONGEST_DIAGONAL")])
    ngon_method: EnumProperty(name="N-gon Method", items=[("BEAUTY", "Beauty", ""), ("CLIP", "Clip", "")])
    transfer_data: EnumProperty(name="Data", items=[("GROUPS", "Vertex Groups", ""), ("UV", "UVs", ""),
                                                    ("NORMAL", "Custom Normals", ""), ("SHARP", "Sharp Edges", "")])

    @classmethod
    def poll(cls, context):
        return _mesh_active(context)

    def invoke(self, context, event):
        if self.action == "UPDATE":
            mod = context.object.modifiers.get(self.modifier_name)
            if mod is None or mod.type not in _UI_FIELDS:
                self.report({"ERROR"}, "Choose a supported modifier")
                return {"CANCELLED"}
            self.modifier_type = mod.type
            self.expected_index = context.object.modifiers.find(mod.name)
            for rna, ui, _ in _UI_FIELDS[mod.type]:
                value = getattr(mod, rna)
                if ui == "reference_name":
                    value = value.name if value else ""
                setattr(self, ui, value)
        return context.window_manager.invoke_props_dialog(self)

    def _fields(self):
        fields = _UI_FIELDS[self.modifier_type]
        if self.modifier_type != "DECIMATE":
            return fields
        setting = {"COLLAPSE": "ratio", "DISSOLVE": "limit_angle", "UNSUBDIV": "iterations"}[self.decimate_mode]
        return [field for field in fields if field[1] in ("decimate_mode", setting)]

    def draw(self, context):
        layout = self.layout
        if self.action == "ADD":
            layout.prop(self, "modifier_name")
            layout.prop(self, "modifier_type")
        else:
            layout.label(text=self.modifier_name)
        for _, ui, label in self._fields():
            if ui == "reference_name":
                layout.prop_search(self, ui, context.scene, "objects", text=label)
            else:
                layout.prop(self, ui, text=label)
        if self.action == "ADD" and self.modifier_type == "DATA_TRANSFER":
            layout.prop(self, "transfer_data")

    def execute(self, context):
        kind = self.modifier_type
        values = {}
        for rna, ui, _ in self._fields():
            value = getattr(self, ui)
            if ui == "reference_name" and not value:
                self.report({"ERROR"}, "Choose the required reference object")
                return {"CANCELLED"}
            if ui in {"axes", "vector"}:
                value = list(value)
            values[rna] = value
        if kind == "ARRAY" and self.action == "ADD":
            values.update(use_constant_offset=True, use_relative_offset=False)
        if kind == "SCREW" and self.action == "ADD":
            values["render_steps"] = values["steps"]
        if kind == "DATA_TRANSFER" and self.action == "ADD":
            data, flags, transfer = _TRANSFER_DATA[self.transfer_data]
            values.update({f"use_{data}_data": True, f"data_types_{flags}": [transfer], f"{data}_mapping": "TOPOLOGY",
                           "mix_mode": "REPLACE"})
        if self.action == "UPDATE":
            params = {"name": self.modifier_name, "expected_type": kind, "settings": values}
            if self.expected_index >= 0:
                params["expected_index"] = self.expected_index
            return _call(self, context, "stack_update", params)
        return _call(self, context, "stack_add", {"name": self.modifier_name or kind.replace("_", " ").title(),
                                                 "type": kind, "settings": values})


class HSW_OT_stack_boolean(bpy.types.Operator):
    bl_idname = "hard_surface.stack_boolean"
    bl_label = "Editable Boolean"
    bl_options = {"REGISTER", "UNDO"}

    action: EnumProperty(items=[(item, item.title(), "") for item in ("BATCH", "REBIND", "SLICE", "CONSUMERS")],
                         options={"HIDDEN"})
    cutter_name: StringProperty(name="Cutter")
    modifier_name: StringProperty(name="Modifier", default="Hard Surface Boolean")
    operation: EnumProperty(name="Operation", items=BOOL_MODES, default="DIFFERENCE")
    use_selection: BoolProperty(name="Use Selected Target Meshes", default=False)
    difference_name: StringProperty(name="Difference Output")
    intersection_name: StringProperty(name="Intersect Output")
    collection_name: StringProperty(name="Collection")
    expected_cutter: StringProperty(options={"HIDDEN"})
    expected_index: IntProperty(options={"HIDDEN"}, default=-1)

    @classmethod
    def poll(cls, context):
        return _mesh_active(context)

    def invoke(self, context, event):
        if self.action == "REBIND":
            mod = context.object.modifiers.get(self.modifier_name)
            if mod is None or mod.type != "BOOLEAN" or mod.operand_type != "OBJECT":
                self.report({"ERROR"}, "Choose an object Boolean layer")
                return {"CANCELLED"}
            self.cutter_name = mod.object.name if mod.object else ""
            self.expected_cutter = mod.object.name if mod.object else ""
            self.expected_index = context.object.modifiers.find(mod.name)
        if self.action == "SLICE":
            self.difference_name = context.object.name[:40] + " Difference"
            self.intersection_name = context.object.name[:40] + " Intersection"
            self.modifier_name = "Hard Surface Slice"
        if self.action == "CONSUMERS":
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop_search(self, "cutter_name", context.scene, "objects")
        if self.action == "REBIND":
            layout.label(text=f"{self.modifier_name}  •  Previous {self.expected_cutter or 'None'}")
        if self.action == "BATCH":
            layout.prop(self, "modifier_name")
            layout.prop(self, "operation")
            layout.prop(self, "use_selection")
        if self.action == "SLICE":
            layout.prop(self, "difference_name")
            layout.prop(self, "intersection_name")
            layout.prop_search(self, "collection_name", bpy.data, "collections")

    def execute(self, context):
        if self.action == "CONSUMERS":
            def describe(result):
                names = [f"{row['object']} / {row['modifier']}" for row in result["consumers"]]
                return ", ".join(names) if names else "No Boolean consumers"
            return _call(self, context, "boolean_consumers", {}, describe)
        if not self.cutter_name:
            self.report({"ERROR"}, "Choose a cutter object")
            return {"CANCELLED"}
        params = {"cutter": self.cutter_name, "name": self.modifier_name}
        if self.action == "REBIND":
            params["expected_cutter"] = self.expected_cutter or None
            if self.expected_index >= 0:
                params["expected_index"] = self.expected_index
            return _call(self, context, "boolean_rebind", params)
        if self.action == "SLICE":
            params.update(difference_name=self.difference_name, intersection_name=self.intersection_name)
            if self.collection_name:
                params["collection"] = self.collection_name
            return _call(self, context, "boolean_slice", params, "Two editable slice outputs created")
        params["operation"] = self.operation
        if self.use_selection:
            params["targets"] = sorted({context.object.name} | {obj.name for obj in context.selected_objects
                                                                if obj.type == "MESH" and obj.name != self.cutter_name})
        return _call(self, context, "boolean_batch", params)


class HSW_OT_stack_preset(bpy.types.Operator):
    bl_idname = "hard_surface.stack_preset"
    bl_label = "Array or Revolve"
    bl_options = {"REGISTER", "UNDO"}

    tool: EnumProperty(items=[("array_linear", "Linear Array", ""), ("array_radial", "Radial Array", ""),
                             ("revolve", "Revolve", "")], options={"HIDDEN"})
    modifier_name: StringProperty(name="Name")
    count: IntProperty(name="Count", min=2, max=1000, default=4)
    vector: FloatVectorProperty(name="Local Coordinates", size=3, default=(0, 0, 0),
                               subtype="TRANSLATION", unit="LENGTH")
    axis: EnumProperty(name="Axis", items=AXES, default="Z")
    angle: FloatProperty(name="Angle", default=tau, unit="ROTATION", subtype="ANGLE")
    steps: IntProperty(name="Steps", min=3, max=512, default=32)

    @classmethod
    def poll(cls, context):
        return _mesh_active(context)

    def invoke(self, context, event):
        if not self.modifier_name:
            self.modifier_name = dict(array_linear="Linear Array", array_radial="Radial Array",
                                      revolve="Revolve")[self.tool]
        if self.tool == "array_linear":
            self.vector = (1, 0, 0)
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "modifier_name")
        if self.tool in {"array_linear", "array_radial"}:
            layout.prop(self, "count")
            layout.prop(self, "vector", text="Local Offset" if self.tool == "array_linear" else "Local Pivot")
        if self.tool != "array_linear":
            layout.prop(self, "axis")
            layout.prop(self, "angle")
        if self.tool == "revolve":
            layout.prop(self, "steps")

    def execute(self, context):
        params = {"name": self.modifier_name}
        if self.tool == "array_linear":
            params.update(count=self.count, offset=list(self.vector))
        elif self.tool == "array_radial":
            params.update(count=self.count, axis=self.axis, pivot=list(self.vector), angle=self.angle)
        else:
            params.update(axis=self.axis, angle=self.angle, steps=self.steps)
        return _call(self, context, self.tool, params)


class HSW_OT_stack_quick(bpy.types.Operator):
    bl_idname = "hard_surface.stack_quick"
    bl_label = "Reorder or Toggle Modifier"
    bl_options = {"REGISTER", "UNDO"}

    action: EnumProperty(items=[(item, item.title(), "") for item in ("UP", "DOWN", "VIEWPORT", "RENDER")])
    modifier_name: StringProperty()

    @classmethod
    def poll(cls, context):
        return _mesh_active(context)

    def execute(self, context):
        stack = context.object.modifiers
        index = stack.find(self.modifier_name)
        if index < 0:
            return {"CANCELLED"}
        if self.action in {"UP", "DOWN"}:
            destination = index + (-1 if self.action == "UP" else 1)
            return _call(self, context, "stack_reorder",
                         {"name": self.modifier_name, "index": destination, "expected_order": list(stack.keys())})
        flag = self.action.lower()
        return _call(self, context, "stack_toggle",
                     {"name": self.modifier_name, flag: not getattr(stack[index], "show_" + flag), "expected_index": index})


class VIEW3D_PT_hard_surface_stack(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_hard_surface_stack"
    bl_label = "Editable Modifier Stack"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return _mesh_active(context)

    def draw(self, context):
        layout = self.layout
        layout.operator("hard_surface.stack_params", text="Add Modifier", icon="ADD").action = "ADD"
        layout.operator("hard_surface.shell_extract", text="Extract Selected Faces")
        boolean = layout.box()
        boolean.label(text="Boolean")
        for action, label in (("BATCH", "Add Boolean"), ("SLICE", "Slice Into Two Copies"),
                              ("CONSUMERS", "Find Consumers of Active Cutter")):
            boolean.operator("hard_surface.stack_boolean", text=label).action = action
        array = layout.box()
        for tool, label in (("array_linear", "Linear Array"), ("array_radial", "Radial Array"), ("revolve", "Revolve")):
            array.operator("hard_surface.stack_preset", text=label).tool = tool
        for index, modifier in enumerate(context.object.modifiers):
            entry = layout.box()
            row = entry.row(align=True)
            row.label(text=modifier.name)
            if modifier.type in _UI_FIELDS:
                options = row.operator("hard_surface.stack_params", text="", icon="PREFERENCES")
                options.action, options.modifier_name = "UPDATE", modifier.name
            if modifier.type == "BOOLEAN" and modifier.operand_type == "OBJECT":
                options = entry.operator("hard_surface.stack_boolean", text="Replace Cutter")
                options.action, options.modifier_name = "REBIND", modifier.name
            for action, icon in (("VIEWPORT", "HIDE_OFF" if modifier.show_viewport else "HIDE_ON"),
                                 ("RENDER", "RESTRICT_RENDER_OFF" if modifier.show_render else "RESTRICT_RENDER_ON"),
                                 ("UP", "TRIA_UP"), ("DOWN", "TRIA_DOWN")):
                button = row.row(align=True)
                button.enabled = (action != "UP" or index > 0) and (action != "DOWN" or index < len(context.object.modifiers)-1)
                options = button.operator("hard_surface.stack_quick", text="", icon=icon)
                options.action, options.modifier_name = action, modifier.name


_CLASSES = (HSW_OT_stack_params, HSW_OT_stack_boolean, HSW_OT_stack_preset,
            HSW_OT_stack_quick, VIEW3D_PT_hard_surface_stack)


def register():
    added = []
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            added.append(cls)
    except Exception:
        for cls in reversed(added):
            bpy.utils.unregister_class(cls)
        raise


def unregister():
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
