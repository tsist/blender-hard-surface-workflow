"""Extract calibrated image silhouettes as Blender viewport reference curves."""
import json
from math import ceil, floor, isfinite
from pathlib import Path

import bpy
import numpy as np
from bpy.props import BoolProperty, BoolVectorProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from mathutils import Matrix, Vector


TOOL = "reference_contour"
VIEWS = {
    "TOP": ((1, 0, 0), (0, 1, 0)),
    "BOTTOM": ((-1, 0, 0), (0, 1, 0)),
    "LEFT": ((0, -1, 0), (0, 0, 1)),
    "RIGHT": ((0, 1, 0), (0, 0, 1)),
    "REAR": ((1, 0, 0), (0, 0, 1)),
    "FRONT": ((-1, 0, 0), (0, 0, 1)),
}
BACKGROUNDS = ("AUTO", "LIGHT", "DARK", "ALPHA")
MAX_PIXELS = 24_000_000
DEFAULTS = {
    "center": (0, 0, 0), "anchor": (0.5, 0.5), "flip": (False, False),
    "roi": (0, 0, 1, 1), "seed": None, "calibration_rect_px": None,
    "background": "AUTO", "threshold": None, "simplify_px": 1.5, "bridge_px": 0,
    "show_image": False, "pack_image": True, "anchor_to_object": False, "preview_only": False,
}
OPTIONAL = (*DEFAULTS, "collection")


def catalog():
    return {TOOL: {
        "label": "Calibrated Reference Contour",
        "required": ["image", "name", "view", "size"],
        "optional": list(OPTIONAL),
        "read_only": False,
        "description": "Extract one connected subject's outer silhouette and place a viewport guide. Set preview_only for a read-only measurement.",
        "parameters": {
            "image": "An absolute path to a local still image.",
            "view": {name: {"right_world": u, "up_world": v} for name, (u, v) in VIEWS.items()},
            "size": "Positive [width, height] in Blender units. Use null for one dimension to retain image proportions.",
            "center": "World point [x, y, z] where the calibration anchor lands.",
            "anchor": "Normalized [right, up] inside the calibration frame, after flips. Default: [.5, .5].",
            "anchor_to_object": "Add the target object's world origin to center.",
            "flip": "[horizontal, vertical] in the chosen view.",
            "roi": "[left, top, right, bottom], normalized from the source image's upper left.",
            "seed": "Optional [u,v] inside the foreground, normalized from the source image's upper left. Default: keep the largest connected subject.",
            "calibration_rect_px": "[left, top, right, bottom] using source pixel boundaries, or null to use the contour bounds.",
            "background": list(BACKGROUNDS),
            "threshold": "Normalized alpha or luminance contrast. Defaults to .5 for alpha and .16 for luminance.",
            "bridge_px": "Square closing radius (0–8 pixels) to bridge narrow mask gaps.",
            "simplify_px": "Contour approximation distance in source pixels, default 1.5.",
            "show_image": "Create a co-registered Blender Image Empty.",
            "pack_image": "Pack the optional image into the blend.",
            "preview_only": "Return calibration and points without adding any scene objects.",
            "collection": "Destination in the current scene. Default: create or use Hard Surface References.",
        },
    }}


def _numbers(value, length, name, minimum, maximum, nullable=False):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{name} expects {length} values")
    for component in value:
        if nullable and component is None:
            continue
        if type(component) not in (int, float) or not isfinite(component) or not minimum <= component <= maximum:
            raise ValueError(f"{name} expects finite values in [{minimum}, {maximum}]")
    return tuple(value)


def _settings(tool, params):
    required = {"image", "name", "view", "size"}
    if tool != TOOL or not isinstance(params, dict) or required - params.keys() or params.keys() - required - set(OPTIONAL):
        raise ValueError("reference_contour expects image, name, view, size and known options")
    settings = {**DEFAULTS, **params}
    image, name, view, background = (settings[key] for key in ("image", "name", "view", "background"))
    path = Path(image) if isinstance(image, str) else None
    if path is None or not path.is_absolute() or not path.is_file():
        raise ValueError("Choose an existing absolute path to a local image")
    settings["image"] = str(path.resolve())
    if not isinstance(name, str) or not name.strip() or len(name.encode("utf-8")) > 56:
        raise ValueError("Give the guide a nonempty name of at most 56 UTF-8 bytes")
    if not isinstance(view, str) or view not in VIEWS or background not in BACKGROUNDS:
        raise ValueError("Select a known orthographic view and background mode")
    for flag in ("show_image", "pack_image", "anchor_to_object", "preview_only"):
        if type(settings[flag]) is not bool:
            raise ValueError(f"{flag} must be boolean")
    if not isinstance(settings["flip"], (list, tuple)) or len(settings["flip"]) != 2 or any(
        type(axis) is not bool for axis in settings["flip"]
    ):
        raise ValueError("flip expects two booleans")
    if type(settings["bridge_px"]) is not int or not 0 <= settings["bridge_px"] <= 8:
        raise ValueError("bridge_px must be an integer from 0 to 8")
    settings["size"] = _numbers(settings["size"], 2, "size", 1e-6, 1e6, nullable=True)
    if settings["size"] == (None, None):
        raise ValueError("A measured width or height is required")
    for key, length, minimum, maximum in (
        ("center", 3, -1e6, 1e6), ("anchor", 2, 0, 1), ("roi", 4, 0, 1),
    ):
        settings[key] = _numbers(settings[key], length, key, minimum, maximum)
    if settings["seed"] is not None:
        settings["seed"] = _numbers(settings["seed"], 2, "seed", 0, 1)
    if settings["calibration_rect_px"] is not None:
        settings["calibration_rect_px"] = _numbers(settings["calibration_rect_px"], 4, "calibration_rect_px", 0, 1e6)
    if settings["threshold"] is not None:
        _numbers([settings["threshold"]], 1, "threshold", 0.001, 1)
    _numbers([settings["simplify_px"]], 1, "simplify_px", 0, 100)
    return settings


def _pixels(image):
    width, height = image.size
    if width * height == 0 or width * height > MAX_PIXELS:
        raise ValueError("The decoded image is empty or exceeds 24 million pixels")
    rgba = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(rgba)
    return rgba.reshape(height, width, 4)[::-1]


def _mask(rgba, mode, threshold):
    alpha = rgba[:, :, 3]
    border_alpha = np.concatenate((alpha[0], alpha[-1], alpha[:, 0], alpha[:, -1]))
    if mode == "AUTO" and np.mean(border_alpha < 0.99) >= 0.5:
        mode = "ALPHA"
    if mode == "ALPHA":
        threshold = 0.5 if threshold is None else threshold
        return alpha >= threshold, {"background": mode, "threshold": threshold}

    light = rgba[:, :, :3] @ np.array((0.2126, 0.7152, 0.0722), dtype=np.float32)
    samples = np.concatenate((light[0], light[-1], light[:, 0], light[:, -1]))
    background = float(np.median(samples))
    threshold = 0.16 if threshold is None else threshold
    if mode == "LIGHT":
        mask = light <= background - threshold
    elif mode == "DARK":
        mask = light >= background + threshold
    else:
        mask = np.abs(light - background) >= threshold
    return mask, {"background": mode, "threshold": threshold, "border_luminance": background}


def _span_filter(mask, radius, grow):
    """Square closing uses separable boolean dilation and erosion."""
    result = mask
    operation = np.logical_or if grow else np.logical_and
    border = (not grow)
    for axis, length in enumerate(mask.shape):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (radius, radius)
        expanded = np.pad(result, pad, constant_values=border)
        result = expanded[radius:radius + length].copy() if axis == 0 else expanded[:, radius:radius + length].copy()
        for index in range(2 * radius + 1):
            if index == radius:
                continue
            values = expanded[index:index + length] if axis == 0 else expanded[:, index:index + length]
            operation(result, values, out=result)
    return result


def _components(mask, seed):
    """Label four-connected foreground from row intervals, avoiding pixel-by-pixel Python flood fill."""
    parents, runs, previous = [], [], []
    seed_run = None

    def root(index):
        while index != parents[index]:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    deltas = np.diff(np.pad(mask, ((0, 0), (1, 1))).astype(np.int8), axis=1)
    rows, transitions = np.nonzero(deltas)
    if len(transitions) > 800_000:
        raise ValueError("Mask has too many fragments; raise the threshold, choose an ROI or close narrow gaps")
    cursor = 0
    for y, count in enumerate(np.bincount(rows, minlength=mask.shape[0])):
        current = []
        prior_index = 0
        for left, right in transitions[cursor:cursor + count].reshape(-1, 2):
            start, end = int(left), int(right)
            index = len(parents)
            parents.append(index)
            runs.append((y, start, end))
            current.append((start, end, index))
            while prior_index < len(previous) and previous[prior_index][1] <= start:
                prior_index += 1
            other_index = prior_index
            while other_index < len(previous) and previous[other_index][0] < end:
                _, _, other = previous[other_index]
                parents[root(index)] = root(other)
                other_index += 1
            if seed is not None and y == seed[1] and start <= seed[0] < end:
                seed_run = index
        cursor += count
        previous = current
    if not runs:
        raise ValueError("No foreground in this region; check background mode and threshold")
    if seed is not None and seed_run is None:
        raise ValueError("The seed falls outside the retained foreground")
    identifiers = [root(i) for i in range(len(runs))]
    weights = {}
    for identifier, (_, start, end) in zip(identifiers, runs):
        weights[identifier] = weights.get(identifier, 0) + end - start
    chosen = root(seed_run) if seed_run is not None else max(weights, key=weights.get)
    component = np.zeros_like(mask)
    for identifier, (y, start, end) in zip(identifiers, runs):
        if identifier == chosen:
            component[y, start:end] = True
    return component, {"component_count": len(weights), "selected_area_px": weights[chosen], "foreground_area_px": sum(weights.values())}


def _area(points):
    right = np.roll(points, -1, axis=0)
    return float(np.sum(points[:, 0] * right[:, 1] - right[:, 0] * points[:, 1])) * 0.5


def _outline(component):
    inside = np.pad(component, 1)
    pixels = inside[1:-1, 1:-1]
    edge_definitions = (
        (inside[:-2, 1:-1], (0, 0), (1, 0)),
        (inside[1:-1, 2:], (1, 0), (1, 1)),
        (inside[2:, 1:-1], (1, 1), (0, 1)),
        (inside[1:-1, :-2], (0, 1), (0, 0)),
    )
    edges, following = {}, {}
    for direction, (neighbour, start_offset, end_offset) in enumerate(edge_definitions):
        ys, xs = np.nonzero(pixels & ~neighbour)
        for y, x in zip(ys.tolist(), xs.tolist()):
            start = (x + start_offset[0], y + start_offset[1])
            end = (x + end_offset[0], y + end_offset[1])
            edges[start, direction] = end
    for edge, end in edges.items():
        _, direction = edge
        # At pixel corners, follow clockwise turns before straight or counterclockwise paths.
        following[edge] = next((end, (direction + turn) % 4) for turn in (1, 0, -1, 2)
                               if (end, (direction + turn) % 4) in edges)
    contour = None
    largest = 0
    for edge in edges:
        if edge not in following:
            continue
        start = edge
        ring = []
        while edge in following:
            ring.append(edge[0])
            edge = following.pop(edge)
        if edge != start:
            raise ValueError("An ambiguous mask boundary could not be traced")
        points = np.asarray(ring, dtype=np.float64)
        area = _area(points)
        if area > largest:
            contour, largest = points, area
    if contour is None or largest < 4:
        raise ValueError("The selected silhouette is too small")
    return contour, largest


def _simplify(points, tolerance):
    # Preserve the extrema used for calibration, including on nearly flat edges.
    ends = sorted({0, *(int(points[:, axis].argmin()) for axis in (0, 1)),
                   *(int(points[:, axis].argmax()) for axis in (0, 1)), len(points)})
    ring = np.vstack((points, points[0]))
    keep = set(ends[:-1])
    pending = list(zip(ends, ends[1:]))
    while pending:
        first, last = pending.pop()
        if last - first < 2:
            continue
        origin = ring[first]
        edge = ring[last] - origin
        offset = ring[first + 1:last] - origin
        magnitude = float(edge @ edge)
        if magnitude:
            projection = np.clip(offset @ edge / magnitude, 0, 1)
            offset -= projection[:, None] * edge
        distances = np.einsum("ij,ij->i", offset, offset)
        peak = int(distances.argmax())
        if distances[peak] > tolerance * tolerance:
            index = first + 1 + peak
            keep.add(index)
            pending.extend(((first, index), (index, last)))
    result = points[sorted(keep)]
    return result if len(result) >= 3 else points


def _extract(rgba, settings):
    height, width = rgba.shape[:2]
    l, t, r, b = settings["roi"]
    if l >= r or t >= b:
        raise ValueError("ROI must have increasing left/right and top/bottom coordinates")
    x0, y0, x1, y1 = floor(l*width), floor(t*height), ceil(r*width), ceil(b*height)
    full_mask, report = _mask(rgba, settings["background"], settings["threshold"])
    mask = full_mask[y0:y1, x0:x1]
    radius = settings["bridge_px"]
    if radius:
        mask = _span_filter(_span_filter(mask, radius, grow=True), radius, grow=False)
    seed = settings["seed"]
    if seed is not None:
        seed = (min(int(seed[0]*width), width-1)-x0, min(int(seed[1]*height), height-1)-y0)
        if not (0 <= seed[0] < x1-x0 and 0 <= seed[1] < y1-y0):
            raise ValueError("The seed must be inside the ROI")
    subject, components = _components(mask, seed)
    outline, area = _outline(subject)
    outline += (x0, y0)
    bounds = [float(x) for x in (*outline.min(axis=0), *outline.max(axis=0))]
    touches = bool(subject[0].any() or subject[-1].any() or subject[:, 0].any() or subject[:, -1].any())
    if components["selected_area_px"] >= 0.98 * subject.size:
        raise ValueError("Foreground fills the ROI; include background around the silhouette")
    points = _simplify(outline, settings["simplify_px"])
    if len(points) > 30_000:
        raise ValueError("The contour has more than 30,000 points; increase simplify_px or adjust threshold and ROI")
    report.update(components, silhouette_area_px=area, raw_point_count=len(outline), point_count=len(points),
                  roi_px=[x0, y0, x1, y1], touches_roi_border=touches, silhouette_bounds_px=bounds)
    return points, report


def _map(points, report, settings, dimensions, obj):
    width, height = dimensions
    bounds = settings["calibration_rect_px"] or report["silhouette_bounds_px"]
    x0, y0, x1, y1 = bounds
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("Calibration rectangle must have positive size within the image")
    span = np.asarray((x1-x0, y1-y0), dtype=np.float64)
    scales_x, scales_y = (None if value is None else value / extent for value, extent in zip(settings["size"], span))
    scales = np.asarray([scales_y if scales_x is None else scales_x, scales_x if scales_y is None else scales_y])
    direction = np.asarray((-1 if settings["flip"][0] else 1, 1 if settings["flip"][1] else -1))
    initial = np.asarray((x1 if settings["flip"][0] else x0, y0 if settings["flip"][1] else y1))
    pixel_origin = initial + direction * np.asarray(settings["anchor"]) * span
    local = (points - pixel_origin) * direction * scales
    center = Vector(settings["center"])
    if settings["anchor_to_object"]:
        if obj is None or bpy.context.view_layer.objects.get(obj.name) != obj:
            raise ValueError("Anchoring requires an active object in the current View Layer")
        center += obj.matrix_world.translation
    u, v = (Vector(axis) for axis in VIEWS[settings["view"]])
    transform = Matrix((u, v, u.cross(v))).transposed().to_4x4()
    transform.translation = center
    world = np.asarray(center) + local @ np.asarray((u, v))
    low, high = world.min(axis=0), world.max(axis=0)
    calibration = {
        "view": settings["view"], "image_size_px": [width, height], "calibration_rect_px": list(bounds),
        "reference_size_bu": (span * scales).tolist(), "scale_bu_per_pixel": scales.tolist(),
        "scale_x_over_y": float(scales[0] / scales[1]),
        "anchor_normalized": list(settings["anchor"]), "anchor_px": pixel_origin.tolist(),
        "anchor_world": list(center), "axis_u_world": list(u), "axis_v_world": list(v), "flip": list(settings["flip"]),
        "curve_world_min": low.tolist(), "curve_world_max": high.tolist(),
        "curve_world_dimensions": (high-low).tolist(),
        "source_pixel_origin": "upper-left pixel boundary",
        "pixel_right_world": (np.asarray(u) * direction[0] * scales[0]).tolist(),
        "pixel_down_world": (np.asarray(v) * direction[1] * scales[1]).tolist(),
    }
    image_origin = (np.asarray((0, height)) - pixel_origin) * direction * scales
    image_scale = (float(direction[0]*scales[0]), float(-direction[1]*scales[1]))
    return local, world, transform, calibration, image_origin, image_scale


def _destination(name):
    scene = bpy.context.scene.collection
    specified = name is not None
    name = name if specified else "Hard Surface References"
    if not isinstance(name, str) or not name.strip() or len(name.encode("utf-8")) > 63:
        raise ValueError("The destination must be a valid collection name")
    collection = scene if name == scene.name else bpy.data.collections.get(name)
    if collection is None and specified:
        raise ValueError(f"Collection does not exist: {name}")
    if collection is not None and (not collection.is_editable or collection.library or collection.override_library
                                   or (collection != scene and collection not in scene.children_recursive)):
        raise ValueError("The guide destination must be editable and belong to the active scene")
    return collection, name


def _create(image, settings, points, transform, calibration, image_origin, image_scale):
    name = settings["name"]
    image_name = name + " Image"
    if bpy.data.objects.get(name) or (settings["show_image"] and bpy.data.objects.get(image_name)):
        raise ValueError("A reference object already uses this name")
    collection, collection_name = _destination(settings.get("collection"))
    made_collection = collection is None
    data = curve = empty = None
    try:
        if made_collection:
            collection = bpy.data.collections.new(collection_name)
            bpy.context.scene.collection.children.link(collection)
        data = bpy.data.curves.new(name, "CURVE")
        data.dimensions = "3D"
        spline = data.splines.new("POLY")
        spline.points.add(len(points)-1)
        homogeneous = np.ones((len(points), 4), dtype=np.float32)
        homogeneous[:, 0:2] = points
        homogeneous[:, 2] = 0
        spline.points.foreach_set("co", homogeneous.ravel())
        spline.use_cyclic_u = True
        curve = bpy.data.objects.new(name, data)
        curve.matrix_world = transform
        curve.hide_render = True
        curve.show_in_front = True
        curve.color = (0.15, 0.86, 0.72, 1)
        curve["hsw_reference_calibration"] = json.dumps(calibration, ensure_ascii=False)
        curve["hsw_reference_source"] = settings["image"]
        collection.objects.link(curve)
        if settings["show_image"]:
            if settings["pack_image"]:
                image.pack()
            empty = bpy.data.objects.new(image_name, None)
            empty.empty_display_type = "IMAGE"
            empty.data = image
            matrix = transform.copy()
            matrix.translation = transform @ Vector((*image_origin, 0))
            empty.matrix_world = matrix
            # Use a unit display rectangle and put the requested size into the transform.
            # Blender clamps empty_display_size to 1,000 even for larger source images.
            empty.empty_display_size = 1
            longest = max(image.size)
            empty.scale = (image_scale[0]*longest, image_scale[1]*longest, 1)
            empty.empty_image_offset = (0, 0)
            empty.empty_image_depth = "BACK"
            empty.use_empty_image_alpha = True
            empty.color = (1, 1, 1, 0.55)
            empty.hide_render = True
            collection.objects.link(empty)
    except Exception:
        for item in (empty, curve):
            if item is not None:
                bpy.data.objects.remove(item, do_unlink=True)
        if data is not None:
            bpy.data.curves.remove(data)
        if made_collection and collection is not None:
            bpy.data.collections.remove(collection)
        raise
    return {"outline": curve.name, "image": empty.name if empty else None,
            "image_packed": bool(empty and image.packed_file), "collection": collection.name,
            "hide_render": True}


def execute(obj, tool, params):
    """Measure a silhouette and optionally add a curve and calibrated Image Empty."""
    settings = _settings(tool, params)
    image = bpy.data.images.load(settings["image"], check_existing=False)
    try:
        points, segmentation = _extract(_pixels(image), settings)
        local, world, transform, calibration, image_origin, image_scale = _map(points, segmentation, settings, tuple(image.size), obj)
        display = None if settings["preview_only"] else _create(image, settings, local, transform, calibration, image_origin, image_scale)
        return {"tool": tool, "source": settings["image"], "calibration": calibration, "segmentation": segmentation,
                "contour_pixels": points.tolist(), "contour_world": world.tolist(), "display": display}
    finally:
        if image.users == 0:
            bpy.data.images.remove(image)


class HSW_OT_reference_contour(bpy.types.Operator):
    bl_idname = "hard_surface.reference_contour"
    bl_label = "Import Reference Contour"
    bl_options = {"REGISTER", "UNDO"}

    image: StringProperty(name="Image", subtype="FILE_PATH")
    name: StringProperty(name="Guide name", default="Reference Contour")
    view: EnumProperty(name="View", items=[(axis, axis.title(), "") for axis in VIEWS])
    width: FloatProperty(name="Width (BU)", default=100, min=0)
    height: FloatProperty(name="Height (BU)", default=0, min=0)
    center: FloatVectorProperty(name="Anchor world", size=3)
    anchor: FloatVectorProperty(name="Anchor in image", size=2, default=(0.5, 0.5), min=0, max=1)
    flip: BoolVectorProperty(name="Flip H / V", size=2)
    use_anchor_object: BoolProperty(name="Offset relative to active object", default=False)
    background: EnumProperty(name="Background", items=[(mode, mode.title(), "") for mode in BACKGROUNDS])
    custom_threshold: BoolProperty(name="Manual threshold")
    threshold: FloatProperty(name="Threshold", default=0.5, min=0.001, max=1)
    use_roi: BoolProperty(name="Limit image region")
    roi: FloatVectorProperty(name="ROI L, T, R, B", size=4, default=(0, 0, 1, 1), min=0, max=1)
    use_seed: BoolProperty(name="Choose component by seed")
    seed: FloatVectorProperty(name="Seed from top left", size=2, default=(0.5, 0.5), min=0, max=1)
    use_calibration_rect: BoolProperty(name="Use known calibration rectangle")
    calibration_rect: FloatVectorProperty(name="Calibration L, T, R, B (px)", size=4, min=0)
    bridge: IntProperty(name="Close gaps (px)", min=0, max=8)
    simplify: FloatProperty(name="Simplify (px)", default=1.5, min=0, max=100)
    show_image: BoolProperty(name="Add aligned image", default=False)
    pack_image: BoolProperty(name="Pack image", default=True)
    advanced: BoolProperty(name="Calibration and mask options")

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "image")
        layout.prop(self, "name")
        layout.prop(self, "view")
        row = layout.row(align=True)
        row.prop(self, "width")
        row.prop(self, "height")
        layout.label(text="Use zero for one dimension to retain image proportions")
        layout.prop(self, "center")
        layout.prop(self, "show_image")
        if self.show_image:
            layout.prop(self, "pack_image")
        layout.prop(self, "advanced")
        if self.advanced:
            for key in ("anchor", "flip", "use_anchor_object", "background", "custom_threshold"):
                layout.prop(self, key)
            if self.custom_threshold:
                layout.prop(self, "threshold")
            for flag, key in (("use_roi", "roi"), ("use_seed", "seed"), ("use_calibration_rect", "calibration_rect")):
                layout.prop(self, flag)
                if getattr(self, flag):
                    layout.prop(self, key)
            layout.prop(self, "bridge")
            layout.prop(self, "simplify")

    def execute(self, context):
        params = {
            "image": bpy.path.abspath(self.image), "name": self.name, "view": self.view,
            "size": [self.width or None, self.height or None], "center": list(self.center),
            "anchor": list(self.anchor), "flip": list(self.flip), "anchor_to_object": self.use_anchor_object,
            "background": self.background, "threshold": self.threshold if self.custom_threshold else None,
            "roi": list(self.roi) if self.use_roi else DEFAULTS["roi"], "seed": list(self.seed) if self.use_seed else None,
            "calibration_rect_px": list(self.calibration_rect) if self.use_calibration_rect else None,
            "bridge_px": self.bridge, "simplify_px": self.simplify, "show_image": self.show_image, "pack_image": self.pack_image,
        }
        try:
            report = execute(context.object, TOOL, params)
        except (RuntimeError, ValueError, OSError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        count = report["segmentation"]["point_count"]
        message = f"Placed {count} outline points"
        if report["segmentation"]["touches_roi_border"]:
            message += " | silhouette touches ROI boundary"
        self.report({"INFO"}, message)
        return {"FINISHED"}


class HSW_PT_reference_contour(bpy.types.Panel):
    bl_label = "Reference Contours"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hard Surface"

    def draw(self, context):
        self.layout.operator(HSW_OT_reference_contour.bl_idname, icon="IMAGE_REFERENCE")


_CLASSES = (HSW_OT_reference_contour, HSW_PT_reference_contour)


def register():
    registered = []
    try:
        for cls in _CLASSES:
            if not cls.is_registered:
                bpy.utils.register_class(cls)
                registered.append(cls)
    except Exception:
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise


def unregister():
    for cls in reversed(_CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
