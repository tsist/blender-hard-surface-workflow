"""Run one named object operation from a Blender Text data-block."""

import json

import bpy
from bpy.props import EnumProperty, FloatProperty, FloatVectorProperty, StringProperty

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
    mode: EnumProperty(name="Inspect", items=[
        ("OBJECTS", "Selected meshes / JSON recipe", "Inspect selected meshes, or use a saved probe recipe"),
        ("PAIR", "Crossings between two meshes", "Compare the two selected evaluated surfaces"),
        ("BORE", "Bore at 3D Cursor", "Place the cursor inside the hole at the section to measure"),
        ("WALL", "Wall thickness at 3D Cursor", "Place the cursor near the face whose normal defines thickness"),
    ])
    bore_axis: FloatVectorProperty(name="Hole Axis (World XYZ)", size=3, default=(0, 0, 1))
    half_depth_mm: FloatProperty(name="Axial Half-Range (mm)", default=10, min=0.0001)
    search_mm: FloatProperty(name="Search Range (mm)", default=10, min=0.0001)
    through_radius_mm: FloatProperty(name="Through Test Radius (mm)", default=0, min=0)
    wall_limit_mm: FloatProperty(name="Max Wall Thickness (mm)", default=10, min=0.0001)

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "mode")
        if self.mode == "OBJECTS":
            layout.prop_search(self, "text_name", bpy.data, "texts")
            layout.label(text="Leave blank to check each selected mesh for crossings.")
        elif self.mode == "PAIR":
            layout.label(text="Select exactly two meshes to compare at the current frame.")
        else:
            layout.label(text="Active mesh, evaluated geometry, world millimetres.")
            layout.prop(self, "search_mm")
            if self.mode == "WALL":
                layout.prop(self, "wall_limit_mm")
            else:
                layout.prop(self, "bore_axis")
                layout.prop(self, "half_depth_mm")
                layout.prop(self, "through_radius_mm")

    def _request(self, context):
        names = sorted(o.name for o in context.selected_objects if o.type == "MESH")
        if self.mode == "OBJECTS":
            return json.loads(bpy.data.texts[self.text_name].as_string()) if self.text_name else {"objects": names}
        if self.mode == "PAIR":
            if len(names) != 2:
                raise ValueError("Select exactly two mesh objects")
            return {"objects": names, "representations": ["EVALUATED"], "self_intersection": False, "pairs": [names]}
        obj = context.object
        if not obj or obj.type != "MESH":
            raise ValueError("Choose the active mesh to measure")
        scene = context.scene
        point = list(scene.cursor.location * (scene.unit_settings.scale_length * 1000))
        measurement = {"kind": self.mode, "object": obj.name, "point_mm": point}
        if self.mode == "WALL":
            measurement.update(max_search_mm=self.search_mm, max_thickness_mm=self.wall_limit_mm)
        else:
            measurement.update(direction=list(self.bore_axis), stations_mm=[0], max_radius_mm=self.search_mm,
                               axial_window_mm=[-self.half_depth_mm, self.half_depth_mm],
                               through_radius_mm=self.through_radius_mm)
        return {"objects": [obj.name], "representations": ["EVALUATED"], "self_intersection": False,
                "measurements": [measurement]}

    def execute(self, context):
        try:
            result = geometry_audit.run(self._request(context))
        except (ValueError, KeyError, RuntimeError, TypeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        report = bpy.data.texts.new("Hard Surface Geometry Audit.json")
        report.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        summary = ""
        if self.mode == "PAIR":
            pair = result["pairs"][0]["representations"]["EVALUATED"]
            overlap = pair.get("coplanar", {})
            summary = (f"Crossings {pair.get('strict_hits', '?')} ({pair['status']}), "
                       f"coplanar pairs {overlap.get('overlapping_pairs', '?')} ({overlap.get('status', '?')}). ")
        elif self.mode in {"BORE", "WALL"}:
            measurement = result["measurements"][0]["representations"]["EVALUATED"]
            summary = measurement["status"] + ". "
            if self.mode == "WALL" and "thickness_mm" in measurement:
                summary = f"Wall {measurement['thickness_mm']:.6g} mm. "
            elif self.mode == "BORE" and measurement.get("sections"):
                section = measurement["sections"][0]
                diameter = section.get("sampled_min_center_clearance_diameter_mm")
                if diameter is not None:
                    summary = f"Sampled clearance diameter {diameter:.6g} mm. "
                axial = measurement.get("axial_clearance", {})
                summary += f"Axial test {axial.get('status', 'unverified')}. "
        self.report({"INFO"}, f"{summary}Text Editor: {report.name}")
        return {"FINISHED"}


def register():
    bpy.utils.register_class(HSW_OT_object_recipe)
    bpy.utils.register_class(HSW_OT_geometry_audit)
    bpy.utils.register_class(HSW_PT_product_modeling)


def unregister():
    bpy.utils.unregister_class(HSW_PT_product_modeling)
    bpy.utils.unregister_class(HSW_OT_geometry_audit)
    bpy.utils.unregister_class(HSW_OT_object_recipe)
