"""Render a single review board from evaluated geometry and fixed view directions."""

from contextlib import contextmanager
from itertools import product
import math
import os
from pathlib import Path
import tempfile

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty
from bpy.props import IntProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ExportHelper
from mathutils import Matrix, Vector
import numpy as np


DEFAULT_VIEWS = {
    "2x2": ("TOP", "LEFT", "REAR", "FRONT_3Q"),
    "3x3": ("TOP", "LEFT", "REAR", "RIGHT", "BOTTOM", "FRONT", "FRONT_3Q", "REAR_3Q", "WIREFRAME"),
}
DIRECTIONS = {
    "TOP": (0, 0, 1), "BOTTOM": (0, 0, -1),
    "LEFT": (-1, 0, 0), "RIGHT": (1, 0, 0),
    "FRONT": (0, 1, 0), "REAR": (0, -1, 0),
    "FRONT_3Q": (-0.85, 1, 0.78), "REAR_3Q": (0.85, -1, 0.78), "TOP_3Q": (0, -0.48, 1),
    "WIREFRAME": (-0.85, 1, 0.78),
}
ALIASES = {"HERO": "FRONT_3Q", "WIRE": "WIREFRAME"}
LABELS = {"FRONT_3Q": "FRONT THREE-QUARTER", "REAR_3Q": "REAR THREE-QUARTER",
          "TOP_3Q": "TOP THREE-QUARTER", "WIREFRAME": "CONTROL CAGE"}
SHADINGS = ("CLAY", "MATERIAL", "SCENE")
DISPLAYS = ("DEFAULT", "SOLID", "CONTROL_CAGE", "EVALUATED_WIRE")
WIRE_DISPLAYS = ("CONTROL_CAGE", "EVALUATED_WIRE")
GEOMETRY = {"MESH", "CURVE", "SURFACE", "FONT"}
SETTINGS_NAME = "hsw_review_grid"
WIRE_EDGE_BUDGET = 50_000
EVALUATED_WIRE_EDGE_BUDGET = 100_000


def _integer(value, key, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{key} must be an integer between {low} and {high}")
    return value


def _options(params, output_path):
    if not isinstance(params, dict):
        raise ValueError("Review grid settings must be a JSON object")
    unknown = set(params) - {"output", "layout", "objects", "collection", "views", "shading", "display",
                             "projection", "cell_size", "margin", "samples", "framing", "subdivision_level"}
    if unknown:
        raise ValueError(f"Unknown review grid settings: {', '.join(sorted(unknown))}")
    layout = params.get("layout", "2x2")
    shading = params.get("shading", "CLAY")
    projection = params.get("projection", "ORTHO")
    display = params.get("display", "DEFAULT")
    if not isinstance(layout, str) or layout not in DEFAULT_VIEWS:
        raise ValueError("layout must be 2x2 or 3x3")
    if shading not in SHADINGS or projection not in ("ORTHO", "PERSP"):
        raise ValueError("Use CLAY/MATERIAL/SCENE shading and ORTHO/PERSP projection")
    if display not in DISPLAYS:
        raise ValueError("display must be DEFAULT, SOLID, CONTROL_CAGE or EVALUATED_WIRE")
    if "subdivision_level" in params:
        if display != "EVALUATED_WIRE":
            raise ValueError("subdivision_level requires display EVALUATED_WIRE")
        _integer(params["subdivision_level"], "subdivision_level", 0, 3)

    defaults = DEFAULT_VIEWS[layout]
    if layout == "3x3" and display != "DEFAULT":
        defaults = (*defaults[:-1], "TOP_3Q")
    views = params.get("views", defaults)
    if not isinstance(views, (list, tuple)) or len(views) != len(DEFAULT_VIEWS[layout]):
        raise ValueError(f"{layout} requires exactly {len(DEFAULT_VIEWS[layout])} views")
    camera_requests = []
    for view in views:
        if isinstance(view, str):
            preset = ALIASES.get(view.upper(), view.upper())
            if preset not in DIRECTIONS:
                raise ValueError(f"Unknown review direction {view!r}")
            camera_requests.append(preset)
        elif isinstance(view, dict) and set(view) == {"camera"} and isinstance(view["camera"], str) and view["camera"]:
            camera_requests.append({"camera": view["camera"]})
        else:
            raise ValueError('Each view must be a preset or {"camera": "Camera Name"}')

    margin = params.get("margin", 0.12)
    if type(margin) not in (int, float) or not 0.03 <= margin <= 0.5:
        raise ValueError("margin must be between 0.03 and 0.5")
    destination = output_path if output_path is not None else params.get("output")
    if not isinstance(destination, (str, os.PathLike)):
        raise ValueError("Supply an absolute PNG output path")
    path = Path(destination)
    if not path.is_absolute() or path.suffix.lower() != ".png" or not path.parent.is_dir():
        raise ValueError("The PNG output must be an absolute path in an existing directory")
    if path.exists() or path.is_symlink():
        raise ValueError(f"A file already exists at {path}")
    return layout, shading, display, projection, camera_requests, margin, path, (
        _integer(params.get("cell_size", 512), "cell_size", 128, 1024),
        _integer(params.get("samples", 24), "samples", 1, 128),
    )


def _fixed_framing(params, projection):
    if "framing" not in params:
        return None
    fixed = params["framing"]
    if not isinstance(fixed, dict) or set(fixed) != {"center", "ortho_scale"}:
        raise ValueError("framing needs exactly center and ortho_scale")
    if projection != "ORTHO":
        raise ValueError("Fixed framing is available for ORTHO presets")
    center, scale = fixed["center"], fixed["ortho_scale"]
    if (not isinstance(center, (tuple, list)) or len(center) != 3
            or not all(type(x) in (int, float) and -1e9 <= x <= 1e9 for x in center)):
        raise ValueError("framing.center must contain three finite model coordinates within ±1e9")
    camera_scale = bpy.types.Camera.bl_rna.properties.get("ortho_scale")
    if type(scale) not in (int, float) or not max(camera_scale.hard_min, 1e-5) <= scale <= 1e9:
        raise ValueError("framing.ortho_scale must be a positive Blender camera scale within range")
    return Vector(center), float(scale)


def _visible_object_names(collection):
    if collection.hide_render:
        return set()
    names = set(collection.objects.keys())
    for child in collection.children:
        names.update(_visible_object_names(child))
    return names


def _targets(scene, params, layer):
    renderable = _visible_object_names(scene.collection)
    names, group = params.get("objects"), params.get("collection")
    explicit = names is not None or group is not None
    if names is not None and group is not None:
        raise ValueError("Choose either objects or collection")
    if names is not None:
        if not isinstance(names, (list, tuple)) or not names or not all(isinstance(s, str) and s for s in names):
            raise ValueError("objects must be a nonempty list of object names")
        if len(set(names)) != len(names):
            raise ValueError("objects contains a duplicate name")
        missing = [name for name in names if scene.objects.get(name) is None]
        if missing:
            raise ValueError(f"Objects outside scene {scene.name!r}: {missing}")
        available = [scene.objects[name] for name in names]
    elif group is not None:
        if not isinstance(group, str) or not group:
            raise ValueError("collection must be an exact collection name")
        collection = bpy.data.collections.get(group)
        if collection is None or group not in {item.name for item in scene.collection.children_recursive}:
            raise ValueError(f"Collection {group!r} is outside scene {scene.name!r}")
        available = list(collection.all_objects)
    else:
        available = list(scene.objects)
    objects = []
    for obj in available:
        if obj.type in GEOMETRY and obj.name in renderable and obj.name in layer.objects and not obj.hide_render:
            if obj.visible_get(view_layer=layer):
                objects.append(obj)
                continue
        if names is not None:
            raise ValueError(f"Cannot evaluate {obj.name!r} as visible, renderable geometry")
    if not objects:
        scope = "requested targets" if explicit else "active view layer"
        raise ValueError(f"No visible model geometry in the {scope}")
    return objects


def _rotation(direction):
    back = Vector(direction).normalized()
    up = Vector((0, back.z, 0)) if abs(back.z) == 1.0 else Vector((0, 0, 1))
    right = up.cross(back).normalized()
    up = back.cross(right).normalized()
    return Matrix((right, up, back)).transposed()


def _bounds(copies):
    corners = [obj.matrix_world @ Vector(point) for obj in copies for point in obj.bound_box]
    minimum = Vector(min(point[axis] for point in corners) for axis in range(3))
    maximum = Vector(max(point[axis] for point in corners) for axis in range(3))
    if (not all(math.isfinite(value) for point in (minimum, maximum) for value in point)
            or (maximum - minimum).length <= 1e-9):
        raise ValueError("Target bounds are empty or invalid")
    return minimum, maximum


def _model_copy(source, depsgraph, render_scene, create, *, cage=False):
    evaluated = source.evaluated_get(depsgraph)
    if cage:
        mesh = create(source.data.copy())
    else:
        mesh = create(bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph))
    if not mesh.vertices:
        if cage:
            return None
        raise ValueError(f"{source.name!r} evaluates to an empty mesh")
    obj = create(bpy.data.objects.new(source.name, mesh))
    render_scene.collection.objects.link(obj)
    obj.matrix_world = evaluated.matrix_world.copy()
    obj.color = source.color
    # Slots linked at object level may differ from the ones stored in the geometric datablock.
    for slot, material_slot in zip(obj.material_slots, evaluated.material_slots):
        if material_slot.link == "OBJECT":
            slot.link = "OBJECT"
            slot.material = material_slot.material
    return obj


def _material(name, color, create):
    material = create(bpy.data.materials.new(name))
    material.diffuse_color = color
    return material


def _subdivision_models(scene, layer, targets, models, render_scene, create, level):
    pairs = [(source, [mod.name for mod in source.modifiers if mod.type == "SUBSURF" and mod.show_viewport])
             for source in targets]
    overrides = [(source, names) for source, names in pairs if names]
    if not overrides:
        raise ValueError("subdivision_level requires an enabled Subdivision Surface modifier on a target")
    if scene.render.use_simplify and scene.render.simplify_subdivision < level:
        raise ValueError("Scene Simplify caps viewport subdivision below subdivision_level")

    # Keep modifier dependencies in the source view layer. Only the temporary
    # objects own edited modifiers. Their input mesh datablocks remain read-only.
    copies = []
    replacements = {}
    try:
        for source, names in overrides:
            item = source.copy()
            copies.append((source, item, names))
            scene.collection.objects.link(item)
            for name in names:
                item.modifiers[name].levels = level
        layer.update()
        graph = bpy.context.evaluated_depsgraph_get()
        for source, item, names in copies:
            evaluated = item.evaluated_get(graph)
            if any(evaluated.modifiers[name].levels != level for name in names):
                raise ValueError(f"Subdivision override could not be preserved during evaluation for {source.name!r}")
            replacements[source] = _model_copy(item, graph, render_scene, create)
    finally:
        bpy.data.batch_remove(ids=[item for _, item, _ in copies])
        layer.update()
    return [replacements.get(source, model) for source, model in zip(targets, models)], {
        source.name: names for source, names in overrides
    }


def _wire_geometry(surface, render_scene, create, radius, clay, dark):
    surface.data.materials.clear()
    surface.data.materials.append(clay)
    # The surface occludes its back edges. The stroke follows the exact input
    # mesh edges, including edges without adjacent faces.
    curve = create(bpy.data.curves.new("Review mesh edges", "CURVE"))
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = radius
    curve.bevel_resolution = 0
    curve.materials.append(dark)
    vertices = [surface.matrix_world @ vertex.co for vertex in surface.data.vertices]
    for edge in surface.data.edges:
        line = curve.splines.new("POLY")
        line.points.add(1)
        for point, vertex in zip(line.points, edge.vertices):
            point.co = (*vertices[vertex], 1.0)
    wire = create(bpy.data.objects.new("Review edges", curve))
    render_scene.collection.objects.link(wire)
    return [surface, wire]


def _wire_selection(sources, evaluated, display):
    # Favor the larger complete control cages. Generated graphics or fine repeating
    # details can otherwise consume the entire line budget in a legacy board.
    evaluated_wire = display == "EVALUATED_WIRE"
    budget = EVALUATED_WIRE_EDGE_BUDGET if evaluated_wire else WIRE_EDGE_BUDGET
    count_key = "evaluated_edges" if evaluated_wire else "control_edges"
    by_scale = sorted(zip(sources, evaluated), key=lambda pair: (-pair[1].dimensions.length, pair[0].name))
    included, omitted, selected = [], [], []
    edge_count = 0
    for source, model in by_scale:
        mesh = model.data if evaluated_wire else source.data if source.type == "MESH" else None
        edges = len(mesh.edges) if mesh is not None else 0
        item = {"object": source.name, count_key: edges, "bounds_diagonal": model.dimensions.length}
        if mesh is None:
            omitted.append({**item, "reason": "NO_MESH_CONTROL_CAGE"})
        elif not edges:
            reason = "NO_EVALUATED_EDGES" if evaluated_wire else "NO_CONTROL_EDGES"
            omitted.append({**item, "reason": reason})
        elif edge_count + edges > budget:
            omitted.append({**item, "reason": "EDGE_BUDGET"})
        else:
            edge_count += edges
            included.append(item)
            selected.append((source, model))
    if display != "DEFAULT" and omitted:
        reasons = ", ".join(f"{item['object']}: {item['reason']} ({item[count_key]} edges)" for item in omitted)
        raise ValueError(f"{display} cannot include all requested targets within {budget:,} edges: {reasons}")
    mode = display
    if display == "DEFAULT":
        mode = ("PARTIAL_CONTROL_CAGE" if omitted else "CONTROL_CAGE") if included else "EVALUATED_SOLID"
    return selected, {"mode": mode, "edge_budget": budget, "displayed_edges": edge_count,
                      "included": included, "omitted": omitted, "omitted_display": "EVALUATED_SOLID"}


def _setup_render(scene, shading, cell, samples):
    settings = scene.render
    settings.engine = "CYCLES" if shading == "SCENE" else "BLENDER_WORKBENCH"
    settings.resolution_x = cell
    settings.resolution_y = cell
    settings.resolution_percentage = 100
    settings.image_settings.file_format = "PNG"
    settings.image_settings.color_mode = "RGB"
    settings.film_transparent = False
    settings.use_file_extension = True
    scene.view_settings.view_transform = "AgX" if shading == "SCENE" else "Standard"
    if shading == "SCENE":
        scene.cycles.device = "CPU"
        scene.cycles.samples = samples
        scene.cycles.use_denoising = True
        settings.threads_mode = "FIXED"
        settings.threads = min(8, os.cpu_count() or 2)
    scene.display.render_aa = "8"
    display = scene.display.shading
    display.light = "STUDIO"
    display.color_type = "SINGLE" if shading == "CLAY" else "MATERIAL"
    display.single_color = (0.68, 0.7, 0.73)
    display.background_type = "WORLD"
    display.show_shadows = False
    display.show_cavity = True
    display.cavity_type = "BOTH"
    display.curvature_ridge_factor = 1.1
    display.curvature_valley_factor = 0.85
    display.show_specular_highlight = True


def _set_lighting(source_scene, source_layer, depsgraph, scene, center, span, create):
    scene.world = source_scene.world
    renderable = _visible_object_names(source_scene.collection)
    lights = [obj for obj in source_layer.objects if obj.type == "LIGHT" and not obj.hide_render
              and obj.name in renderable]
    if lights or scene.world is not None:
        for source in lights:
            evaluated = source.evaluated_get(depsgraph)
            light = create(bpy.data.objects.new("Review light", evaluated.data.copy()))
            create(light.data)
            scene.collection.objects.link(light)
            light.matrix_world = evaluated.matrix_world.copy()
        return
    scene.world = create(bpy.data.worlds.new("Review environment"))
    scene.world.use_nodes = True
    background = scene.world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.48, 0.52, 0.63, 1.0)
    background.inputs["Strength"].default_value = 0.35
    for offset, strength, size in (((-0.65, 0.4, 1.3), 20, 1.1), ((1.0, -0.5, 0.4), 9, 0.75),
                                    ((0.1, -0.5, -1.0), 7, 1.05)):
        light_data = create(bpy.data.lights.new("Review softbox", "AREA"))
        light_data.energy = strength * span ** 2
        light_data.shape = "DISK"
        light_data.size = size * span
        light = create(bpy.data.objects.new("Review softbox", light_data))
        scene.collection.objects.link(light)
        light.location = center + span * Vector(offset)
        light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()


def _make_cameras(source_scene, depsgraph, scene, views, projection, minimum, maximum, margin, fixed, create):
    center = fixed[0] if fixed else (minimum + maximum) * 0.5
    relative_corners = [Vector(point) - center for point in product(*zip(minimum, maximum))]
    rotations = {view: _rotation(DIRECTIONS[view]) for view in views if isinstance(view, str)}
    matrices = [matrix.transposed() for matrix in rotations.values()]
    span = (maximum - minimum).length
    # One fit for the most demanding requested preset gives orthographic tiles the same world scale.
    width = fixed[1] if fixed else 2 * max(
        (max(abs(point.x), abs(point.y)) for matrix in matrices for point in (matrix @ p for p in relative_corners)),
        default=span,
    ) * (1 + 2 * margin)
    reach = max(point.length for point in relative_corners)
    cameras = []
    for view in views:
        if isinstance(view, dict):
            source = source_scene.objects.get(view["camera"])
            if source is None or source.type != "CAMERA":
                raise ValueError(f"Camera {view['camera']!r} is outside the scene or has the wrong type")
            evaluated = source.evaluated_get(depsgraph)
            camera_data = create(evaluated.data.copy())
            matrix_world = evaluated.matrix_world.copy()
            preset, label = "CAMERA", source.name
        else:
            rotation = rotations[view]
            camera_data = create(bpy.data.cameras.new("Review camera"))
            camera_data.type = projection
            camera_data.lens = 52
            camera_data.sensor_width = 36
            camera_data.ortho_scale = width
            camera_data.dof.use_dof = False
            camera_data.clip_start = max(1e-5, span / 10000)
            camera_data.clip_end = max(1.0, span * 30, reach * 4)
            distance = max(span * 2.4, reach * 1.5)
            if projection == "PERSP":
                distance = 0.5 * width / math.tan(camera_data.angle * 0.5) + span * 0.5
            matrix_world = rotation.to_4x4()
            matrix_world.translation = center + distance * rotation.col[2].to_3d()
            preset, label = view, LABELS.get(view, view)
        camera = create(bpy.data.objects.new(f"Review {label}", camera_data))
        scene.collection.objects.link(camera)
        camera.matrix_world = matrix_world
        cameras.append((camera, preset, label))
    return cameras, center, width


# Compact five-column ASCII glyphs for labels in Blender's background mode.
_GLYPHS = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789- _?.", (
    (14,17,17,31,17,17,17),(30,17,17,30,17,17,30),(14,17,16,16,16,17,14),
    (30,17,17,17,17,17,30),(31,16,16,30,16,16,31),(31,16,16,30,16,16,16),
    (14,17,16,23,17,17,15),(17,17,17,31,17,17,17),(14,4,4,4,4,4,14),
    (7,2,2,2,18,18,12),(17,18,20,24,20,18,17),(16,16,16,16,16,16,31),
    (17,27,21,21,17,17,17),(17,25,21,19,17,17,17),(14,17,17,17,17,17,14),
    (30,17,17,30,16,16,16),(14,17,17,17,21,18,13),(30,17,17,30,20,18,17),
    (15,16,16,14,1,1,30),(31,4,4,4,4,4,4),(17,17,17,17,17,17,14),
    (17,17,17,17,10,10,4),(17,17,17,21,21,27,17),(17,17,10,4,10,17,17),
    (17,17,10,4,4,4,4),(31,1,2,4,8,16,31),
    (14,17,19,21,25,17,14),(4,12,4,4,4,4,14),(14,17,1,2,4,8,31),
    (30,1,1,14,1,1,30),(2,6,10,18,31,2,2),(31,16,16,30,1,1,30),
    (14,16,16,30,17,17,14),(31,1,2,4,8,8,8),(14,17,17,14,17,17,14),
    (14,17,17,15,1,1,14),(0,0,0,31,0,0,0),(0,0,0,0,0,0,0),
    (0,0,0,0,0,0,31),(14,17,1,2,4,0,4),(0,0,0,0,0,6,6),
)))


def _label(image, x, y, text, scale, width):
    for pos, char in enumerate(text.upper()[: max(1, (width - 8) // (6 * scale))]):
        for row, bits in enumerate(_GLYPHS.get(char, _GLYPHS["?"])):
            for col in range(5):
                if bits & (16 >> col):
                    left = x + (pos * 6 + col) * scale
                    top = y + row * scale
                    image[top:top + scale, left:left + scale, :3] = (0.85, 0.88, 0.92)


def _read_tile(path, cell):
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        if tuple(image.size) != (cell, cell) or image.channels != 4:
            raise RuntimeError(f"Unexpected render tile shape {tuple(image.size)}/{image.channels}")
        pixels = np.empty((cell, cell, 4), dtype=np.float32)
        image.pixels.foreach_get(pixels.ravel())
        return pixels[::-1]
    finally:
        bpy.data.images.remove(image)


def _board(side, cell):
    scale = 2 if cell >= 300 else 1
    heading = 7 * scale + 12
    gap = max(5, cell // 80)
    width = side * cell + (side + 1) * gap
    height = side * (cell + heading) + (side + 1) * gap
    array = np.empty((height, width, 4), dtype=np.float32)
    array[:] = (0.105, 0.12, 0.143, 1.0)
    return array, heading, gap, scale


def _write_board(array, path, staging):
    height, width, _ = array.shape
    image = bpy.data.images.new("Hard Surface review board", width, height, alpha=False)
    try:
        image.file_format = "PNG"
        image.colorspace_settings.name = "sRGB"
        image.pixels.foreach_set(np.ascontiguousarray(array[::-1]).ravel())
        image.filepath_raw = str(staging)
        image.save()
        # Publish atomically without overwriting a file created after preflight.
        os.link(staging, path)
    finally:
        bpy.data.images.remove(image)


@contextmanager
def _temporary_scene():
    created = []
    def create(datablock):
        created.append(datablock)
        return datablock
    try:
        yield create(bpy.data.scenes.new("Hard Surface Review")), create
    finally:
        bpy.data.batch_remove(ids=created)


def produce(scene, params, *, output_path=None):
    """Render four or nine views into a single PNG using Blender's current dependency graph."""
    layout, shading, display_mode, projection, views, margin, path, (cell, samples) = _options(params, output_path)
    fixed = _fixed_framing(params, projection)
    if not isinstance(scene, bpy.types.Scene):
        raise ValueError("A Blender scene is required")
    if bpy.context.mode != "OBJECT":
        raise ValueError("Enter Object Mode to evaluate and render a stable review grid")
    layer = bpy.context.view_layer if bpy.context.scene == scene else scene.view_layers[0]
    targets = _targets(scene, params, layer)

    side = int(layout[0])
    board, heading, gap, label_scale = _board(side, cell)
    tiles = []
    wire_report = None
    subdivision_report = None
    with _temporary_scene() as (render_scene, create), tempfile.TemporaryDirectory(prefix=".hsw-grid-", dir=path.parent) as folder:
        folder = Path(folder)
        solid_shading = "CLAY" if display_mode in WIRE_DISPLAYS else shading
        _setup_render(render_scene, solid_shading, cell, samples)
        render_scene.frame_set(scene.frame_current, subframe=scene.frame_subframe)
        with bpy.context.temp_override(scene=scene, view_layer=layer):
            depsgraph = bpy.context.evaluated_depsgraph_get()
            models = [_model_copy(obj, depsgraph, render_scene, create) for obj in targets]
            render_scene.view_layers[0].update()
            minimum, maximum = _bounds(models)
            span = (maximum - minimum).length
            if solid_shading == "SCENE":
                _set_lighting(scene, layer, depsgraph, render_scene, (minimum+maximum)*0.5, span, create)
            else:
                render_scene.world = create(bpy.data.worlds.new("Review background"))
                render_scene.world.color = (0.15, 0.16, 0.18)
            cameras, center, width = _make_cameras(
                scene, depsgraph, render_scene, views, projection, minimum, maximum, margin, fixed, create,
            )
            wires = []
            wire_pairs = []
            if display_mode in WIRE_DISPLAYS or (display_mode == "DEFAULT" and "WIREFRAME" in views):
                wire_models = models
                if "subdivision_level" in params:
                    level = params["subdivision_level"]
                    wire_models, overrides = _subdivision_models(scene, layer, targets, models, render_scene, create, level)
                    subdivision_report = {"mode": "OVERRIDE", "level": level, "objects": overrides}
                elif display_mode == "EVALUATED_WIRE":
                    subdivision_report = {"mode": "VIEWPORT"}
                wire_pairs, wire_report = _wire_selection(targets, wire_models, display_mode)
            if wire_pairs:
                clay = _material("Review wire surface", (0.61, 0.66, 0.73, 1), create)
                dark = _material("Review wire lines", (0.018, 0.03, 0.05, 1), create)
                stroke = width / max(200, cell * 2.2)
                if display_mode == "EVALUATED_WIRE":
                    # A subpixel radius keeps adjacent L2 lines distinguishable.
                    radius_px = max(0.24, min(0.43, cell / (9 * math.sqrt(wire_report["displayed_edges"]))))
                    stroke = width * radius_px / cell
                wire_report["stroke_radius_world"] = stroke
                for source, model in wire_pairs:
                    surface = model if display_mode == "EVALUATED_WIRE" else _model_copy(
                        source, depsgraph, render_scene, create, cage=True,
                    )
                    wires.extend(_wire_geometry(surface, render_scene, create, stroke, clay, dark))
            wire_sources = {source for source, _ in wire_pairs}
        base_engine = render_scene.render.engine
        display = render_scene.display.shading
        base_color = display.color_type
        tile_path = folder / "tile.png"
        for index, (camera, preset, label) in enumerate(cameras):
            legacy_wire = display_mode == "DEFAULT" and preset == "WIREFRAME"
            render_display = wire_report["mode"] if legacy_wire or display_mode in WIRE_DISPLAYS else "SOLID"
            wire = render_display in (*WIRE_DISPLAYS, "PARTIAL_CONTROL_CAGE")
            if legacy_wire:
                label = {"PARTIAL_CONTROL_CAGE": "CAGE AND SOLIDS", "EVALUATED_SOLID": "EVALUATED SOLID"}.get(render_display, label)
            elif preset == "WIREFRAME":
                label = LABELS["FRONT_3Q"]
            render_scene.render.engine = "BLENDER_WORKBENCH" if wire else base_engine
            display.color_type = "MATERIAL" if wire else base_color
            display.light = "FLAT" if wire else "STUDIO"
            display.show_cavity = not wire
            display.show_specular_highlight = not wire
            for obj, source in zip(models, targets):
                obj.hide_render = wire and source in wire_sources
            for obj in wires:
                obj.hide_render = not wire
            render_scene.camera = camera
            render_scene.render.filepath = str(tile_path)
            status = bpy.ops.render.render(write_still=True, scene=render_scene.name)
            if "FINISHED" not in status or not tile_path.is_file():
                raise RuntimeError(f"Could not render tile {index + 1} ({label})")
            row, col = divmod(index, side)
            x = col * (cell + gap) + gap
            y = row * (cell + heading + gap) + gap
            _label(board, x + 4, y + 6, label, label_scale, cell)
            board[y + heading:y + heading + cell, x:x + cell] = _read_tile(tile_path, cell)
            tile_path.unlink()
            tiles.append({"label": label, "preset": preset, "projection": camera.data.type, "display": render_display,
                          "rect": [x, y + heading, cell, cell], "ortho_scale": camera.data.ortho_scale
                          if camera.data.type == "ORTHO" else None,
                          "camera_position": list(camera.matrix_world.translation)})
        _write_board(board, path, folder / "board.png")
    geometry = {
        "DEFAULT": "evaluated viewport meshes; WIREFRAME shows budgeted whole unsubdivided mesh cages",
        "SOLID": "evaluated viewport meshes",
        "CONTROL_CAGE": "whole unsubdivided mesh cages at evaluated object transforms",
        "EVALUATED_WIRE": "actual evaluated mesh edges at evaluated object transforms",
    }
    return {"output": str(path), "layout": layout, "views": tiles, "shading": shading, "display": display_mode,
            "image_size": [int(board.shape[1]), int(board.shape[0])],
            "objects": [obj.name for obj in targets], "view_layer": layer.name,
            "bounds": {"minimum": list(minimum), "maximum": list(maximum)},
            "framing": {"center": list(center), "ortho_scale": width} if projection == "ORTHO" else None,
            "framing_mode": "FIXED" if fixed else "AUTO",
            "geometry": geometry[display_mode],
            **({"evaluated_subdivision": subdivision_report} if subdivision_report else {}),
            **({"wireframe": wire_report} if wire_report else {})}


class HSW_PG_review_grid(bpy.types.PropertyGroup):
    layout: EnumProperty(name="网格", items=[(v, v, "") for v in DEFAULT_VIEWS], default="2x2")
    display: EnumProperty(name="几何显示", items=[("DEFAULT", "默认混合", "实体与预设控制笼"),
                                                   ("SOLID", "全格实体", "显示当前视口修改器求值"),
                                                   ("CONTROL_CAGE", "全格控制笼", "显示基础网格的真实边线"),
                                                   ("EVALUATED_WIRE", "全格求值线框", "显示修改器求值后网格的真实边线")])
    override_subdivision: BoolProperty(name="临时指定细分级别", description="只在临时对象上覆盖已启用的细分曲面修改器")
    subdivision_level: IntProperty(name="细分级别", min=0, max=3, default=2)
    shading: EnumProperty(name="外观", items=[("CLAY", "白模", "Workbench 浅灰白模"),
                                            ("MATERIAL", "材质色", "Workbench 材质视口颜色"),
                                            ("SCENE", "场景材质", "Cycles 实际材质与灯光")])
    projection: EnumProperty(name="机位", items=[("ORTHO", "正交", "统一世界比例"),
                                                ("PERSP", "透视", "统一焦距与距离")])
    target: EnumProperty(name="对象范围", items=[("SCENE", "场景", "活动视图层的可渲染模型"),
                                                ("SELECTED", "已选", "已选模型对象"),
                                                ("COLLECTION", "集合", "指定集合中的模型")])
    collection: PointerProperty(name="模型集合", type=bpy.types.Collection)
    views: StringProperty(name="自定义方向", description="逗号分隔 4 或 9 个方向，现有相机写作 camera:相机名")
    cell_size: IntProperty(name="每格像素", min=128, max=1024, default=512)
    fixed_framing: BoolProperty(name="固定正交取景", description="跨版本复用中心与世界尺度，避免自动缩放")
    frame_center: FloatVectorProperty(name="取景中心", size=3, min=-1e9, max=1e9, subtype="TRANSLATION")
    ortho_scale: FloatProperty(name="取景宽度", min=1e-5, max=1e9, default=10, subtype="DISTANCE")


class HSW_OT_review_grid(bpy.types.Operator, ExportHelper):
    bl_idname = "hard_surface.review_grid"
    bl_label = "导出多视角 PNG"
    bl_description = "对同一组模型渲染四宫格或九宫格"
    filename_ext = ".png"
    filter_glob: StringProperty(default="*.png", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.mode == "OBJECT"

    def execute(self, context):
        settings = getattr(context.scene, SETTINGS_NAME)
        request = {"layout": settings.layout, "shading": settings.shading, "projection": settings.projection,
                   "display": settings.display, "cell_size": settings.cell_size, "output": bpy.path.abspath(self.filepath)}
        if settings.display == "EVALUATED_WIRE" and settings.override_subdivision:
            request["subdivision_level"] = settings.subdivision_level
        if settings.target == "COLLECTION":
            if settings.collection is None:
                self.report({"ERROR"}, "请选择模型集合")
                return {"CANCELLED"}
            request["collection"] = settings.collection.name
        elif settings.target == "SELECTED":
            request["objects"] = [obj.name for obj in context.selected_objects if obj.type in GEOMETRY]
        if settings.views.strip():
            entries = [entry.strip() for entry in settings.views.split(",")]
            request["views"] = [{"camera": entry.split(":", 1)[1].strip()} if entry.lower().startswith("camera:")
                                else entry for entry in entries]
        if settings.projection == "ORTHO" and settings.fixed_framing:
            request["framing"] = {"center": list(settings.frame_center), "ortho_scale": settings.ortho_scale}
        try:
            result = produce(context.scene, request)
        except (ValueError, RuntimeError, OSError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"多视角 PNG 已保存至 {result['output']}")
        return {"FINISHED"}


class VIEW3D_PT_hsw_review_grid(bpy.types.Panel):
    bl_label = "多视角检查"
    bl_idname = "VIEW3D_PT_hsw_review_grid"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"

    def draw(self, context):
        settings = getattr(context.scene, SETTINGS_NAME)
        layout = self.layout
        for prop in ("layout", "display"):
            layout.prop(settings, prop)
        if settings.display not in WIRE_DISPLAYS:
            layout.prop(settings, "shading")
        if settings.display == "EVALUATED_WIRE":
            layout.prop(settings, "override_subdivision")
            if settings.override_subdivision:
                layout.prop(settings, "subdivision_level")
        layout.prop(settings, "projection")
        layout.prop(settings, "target")
        if settings.target == "COLLECTION":
            layout.prop(settings, "collection")
        layout.prop(settings, "cell_size")
        layout.prop(settings, "views")
        if settings.projection == "ORTHO":
            layout.prop(settings, "fixed_framing")
            if settings.fixed_framing:
                layout.column(align=True).prop(settings, "frame_center")
                layout.prop(settings, "ortho_scale")
        layout.operator(HSW_OT_review_grid.bl_idname, icon="RENDER_STILL")


_CLASSES = (HSW_PG_review_grid, HSW_OT_review_grid, VIEW3D_PT_hsw_review_grid)
_registered = []


def register():
    if _registered:
        return
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _registered.append(cls)
        setattr(bpy.types.Scene, SETTINGS_NAME, PointerProperty(type=HSW_PG_review_grid))
    except Exception:
        unregister()
        raise


def unregister():
    if _registered and hasattr(bpy.types.Scene, SETTINGS_NAME):
        delattr(bpy.types.Scene, SETTINGS_NAME)
    while _registered:
        bpy.utils.unregister_class(_registered.pop())
