"""Run one named object operation from a Blender Text data-block."""

import json

import bpy
from bpy.props import StringProperty

from . import fillet_tools, geometry_audit, object_tools, surface_tools


class HSW_OT_object_recipe(bpy.types.Operator):
    bl_idname = "hard_surface.object_recipe"
    bl_label = "Run Hard Surface Recipe"
    bl_options = {"REGISTER", "UNDO"}

    tool: StringProperty(options={"HIDDEN"})
    text_name: StringProperty(name="JSON Text")

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and context.object is not None

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        self.layout.label(text=context.object.name)
        self.layout.prop_search(self, "text_name", bpy.data, "texts")
        self.layout.label(text="Use a JSON parameters object or a single hard_surface.stack operation.")

    def execute(self, context):
        document = bpy.data.texts.get(self.text_name)
        if document is None:
            self.report({"ERROR"}, "Select a JSON Text from the blend file")
            return {"CANCELLED"}
        try:
            params = json.loads(document.as_string())
            if not isinstance(params, dict):
                raise ValueError("Expected a JSON object")
            if "op" in params:
                if (params.get("op"), params.get("tool"), params.get("object")) != (
                    "hard_surface.stack", self.tool, context.object.name
                ):
                    raise ValueError("Operation, tool or active object does not match this recipe")
                params = params["params"]
            result = object_tools.execute(context.object, self.tool, params)
        except (ValueError, KeyError, RuntimeError, TypeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"{self.tool} — {result.get('output', context.object.name)}")
        return {"FINISHED"}


class HSW_PT_product_modeling(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_hsw_product_modeling"
    bl_label = "Product Surfaces and Fit"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"

    def draw(self, context):
        layout = self.layout
        layout.operator("hard_surface.shell_panel_seat", text="Create Cover and Fixed Seat")
        layout.operator("hard_surface.shell_panel_seat_resize", text="Resize Cover and Seat")
        for name, spec in {**surface_tools.catalog(), **fillet_tools.catalog()}.items():
            layout.operator("hard_surface.object_recipe", text=spec["label"]).tool = name
        layout.operator("hard_surface.geometry_audit")


class HSW_OT_geometry_audit(bpy.types.Operator):
    bl_idname = "hard_surface.geometry_audit"
    bl_label = "Inspect Geometry and Fit"
    bl_options = {"REGISTER", "UNDO"}

    text_name: StringProperty(name="Probe JSON Text")

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop_search(self, "text_name", bpy.data, "texts")
        self.layout.label(text="Leave blank to check selected mesh objects for crossings.")

    def execute(self, context):
        try:
            params = (json.loads(bpy.data.texts[self.text_name].as_string()) if self.text_name else
                      {"objects": [o.name for o in context.selected_objects if o.type == "MESH"]})
            result = geometry_audit.run(params)
        except (ValueError, KeyError, RuntimeError, TypeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        report = bpy.data.texts.new("Hard Surface Geometry Audit.json")
        report.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        self.report({"INFO"}, f"Report in Text Editor: {report.name}")
        return {"FINISHED"}


def register():
    bpy.utils.register_class(HSW_OT_object_recipe)
    bpy.utils.register_class(HSW_OT_geometry_audit)
    bpy.utils.register_class(HSW_PT_product_modeling)


def unregister():
    bpy.utils.unregister_class(HSW_PT_product_modeling)
    bpy.utils.unregister_class(HSW_OT_geometry_audit)
    bpy.utils.unregister_class(HSW_OT_object_recipe)
