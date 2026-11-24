"""Pick reference-image landmarks in the Image Editor for face-flow receipts."""

from dataclasses import dataclass
from hashlib import file_digest
from math import floor
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
import gpu
from gpu_extras.batch import batch_for_shader


@dataclass(frozen=True)
class _ImagePoint:
    image_uid: int
    path: Path
    signature: tuple
    sha256: str
    size: tuple
    pixel: tuple


_points = {}
_pickers = {}
_draw_handle = None


def _source(image):
    if image is None:
        raise ValueError("Open a static external reference image in the Image Editor")
    if image.source != "FILE" or image.is_multiview or image.use_multiview:
        raise ValueError("Use a single external still image")
    if image.is_dirty or image.packed_file is not None or image.packed_files:
        raise ValueError("Use the external original image without unsaved edits or packing")
    if not image.filepath:
        raise ValueError("The reference image needs an external file path")
    size = tuple(image.size)
    if min(size) < 1:
        raise ValueError("The loaded reference image has no readable pixels")
    path = Path(bpy.path.abspath(image.filepath, library=image.library)).resolve(strict=True)
    if not path.is_file():
        raise ValueError("The reference source is not a file")
    return path, size


def _signature(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _hash_source(path, expected):
    if _signature(path) != expected:
        raise ValueError("The source file changed. Reload it and pick the landmark again")
    with path.open("rb") as source:
        digest = file_digest(source, "sha256").hexdigest()
    if _signature(path) != expected:
        raise ValueError("The source file changed while it was being read")
    return digest


def _window_key(context):
    return context.window.as_pointer() if context.window is not None else None


def point_for(context):
    """Return a copy of the current window's valid reference-image evidence."""
    record = _points.get(_window_key(context))
    if record is None:
        return None
    image = next((entry for entry in bpy.data.images if entry.session_uid == record.image_uid), None)
    try:
        path, size = _source(image)
        if path != record.path or size != record.size or _signature(path) != record.signature:
            return None
    except (OSError, ValueError, ReferenceError):
        return None
    return {
        "file": str(record.path), "sha256": record.sha256,
        "size_px": list(record.size), "pixel_xy": list(record.pixel),
    }


def _pixel_at(region, x, y, size):
    u, v = region.view2d.region_to_view(x, y)
    if not (0.0 <= u < 1.0 and 0.0 <= v < 1.0):
        return None
    width, height = size
    return floor(u * width), height - 1 - floor(v * height)


def _marker_at(region, size, pixel):
    width, height = size
    x, y = pixel
    return region.view2d.view_to_region((x + 0.5) / width, 1.0 - (y + 0.5) / height, clip=False)


def _tag_views(window):
    if window and window.screen:
        for area in window.screen.areas:
            if area.type in {"IMAGE_EDITOR", "VIEW_3D"}:
                area.tag_redraw()


def _inside_region(region, x, y):
    return region.width > 1 and region.height > 1 and (
        region.x <= x < region.x + region.width and region.y <= y < region.y + region.height
    )


def _image_region(area, x, y):
    for region in area.regions:
        if region.type != "WINDOW" and _inside_region(region, x, y):
            return None
    return next(
        (region for region in area.regions if region.type == "WINDOW" and _inside_region(region, x, y)),
        None,
    )


class HSW_OT_reference_image_pick(bpy.types.Operator):
    bl_idname = "hsw.reference_image_pick"
    bl_label = "Pick Reference Landmark"
    bl_description = "Click a pixel of the original reference image and bind it to this window's face-flow checks"

    @classmethod
    def poll(cls, context):
        return (
            context.window is not None and context.area is not None
            and context.area.type == "IMAGE_EDITOR" and context.space_data.ui_mode == "VIEW"
            and _window_key(context) not in _pickers
        )

    def invoke(self, context, event):
        image = context.space_data.image
        try:
            self._path, self._size = _source(image)
            self._signature = _signature(self._path)
        except (OSError, ValueError, ReferenceError) as error:
            self.report({"WARNING"}, str(error))
            return {"CANCELLED"}
        self._image_uid = image.session_uid
        self._area = context.area.as_pointer()
        self._window = _window_key(context)
        _pickers[self._window] = self
        context.window.cursor_modal_set("CROSSHAIR")
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        self.report({"INFO"}, "Click the reference landmark. Esc or right click cancels")
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        if _pickers.get(self._window) is self:
            _pickers.pop(self._window)
            if _window_key(context) == self._window:
                context.window.cursor_modal_restore()
                _tag_views(context.window)

    def cancel(self, context):
        self._finish(context)

    def modal(self, context, event):
        if _pickers.get(self._window) is not self:
            return {"CANCELLED"}
        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            self._finish(context)
            return {"CANCELLED"}
        area = context.area
        if (
            _window_key(context) != self._window or area is None or area.as_pointer() != self._area
            or area.type != "IMAGE_EDITOR" or context.space_data.ui_mode != "VIEW"
        ):
            self._finish(context)
            return {"CANCELLED"}
        if event.type != "LEFTMOUSE" or event.value != "PRESS":
            return {"PASS_THROUGH"}
        region = _image_region(area, event.mouse_x, event.mouse_y)
        if region is None:
            return {"PASS_THROUGH"}
        pixel = _pixel_at(region, event.mouse_x - region.x, event.mouse_y - region.y, self._size)
        if pixel is None:
            self.report({"WARNING"}, "Click within the original image")
            return {"RUNNING_MODAL"}
        try:
            image = context.space_data.image
            path, size = _source(image)
            if image.session_uid != self._image_uid or path != self._path or size != self._size:
                raise ValueError("The open reference changed. Start the landmark pick again")
            digest = _hash_source(path, self._signature)
        except (OSError, ValueError, ReferenceError) as error:
            self.report({"WARNING"}, str(error))
            self._finish(context)
            return {"CANCELLED"}
        _points[self._window] = _ImagePoint(image.session_uid, path, self._signature, digest, size, pixel)
        self._finish(context)
        return {"FINISHED"}


class HSW_OT_reference_image_clear(bpy.types.Operator):
    bl_idname = "hsw.reference_image_clear"
    bl_label = "Clear Reference Landmark"
    bl_description = "Clear the face-flow reference landmark in this window"

    @classmethod
    def poll(cls, context):
        return _window_key(context) in _points

    def execute(self, context):
        _points.pop(_window_key(context), None)
        _tag_views(context.window)
        return {"FINISHED"}


class HSW_PT_reference_image(bpy.types.Panel):
    bl_idname = "HSW_PT_reference_image"
    bl_label = "Face Flow Reference"
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Hard Surface"

    @classmethod
    def poll(cls, context):
        return context.space_data.ui_mode == "VIEW"

    def draw(self, context):
        layout = self.layout
        key = _window_key(context)
        if key in _pickers:
            layout.label(text="Click the landmark on the image")
            layout.label(text="Esc or right click cancels")
        else:
            layout.operator(HSW_OT_reference_image_pick.bl_idname)
        record = _points.get(key)
        if record is not None:
            layout.operator(HSW_OT_reference_image_clear.bl_idname)
            point = point_for(context)
            if point is None:
                layout.label(text="Reference changed. Pick again", icon="ERROR")
            else:
                x, y = point["pixel_xy"]
                layout.label(text=record.path.name)
                layout.label(text=f"Pixel ({x}, {y}), top-left origin")
        layout.label(text="Record source, time and stage in 3D View")


def _draw_marker():
    context = bpy.context
    record = _points.get(_window_key(context))
    if record is None or context.region is None:
        return
    image = context.space_data.image
    if (
        image is None or image.session_uid != record.image_uid
        or image.source != "FILE" or image.is_dirty or tuple(image.size) != record.size
        or point_for(context) is None
    ):
        return
    x, y = _marker_at(context.region, record.size, record.pixel)
    if not (0 <= x < context.region.width and 0 <= y < context.region.height):
        return
    lines = [(x - 9, y), (x + 9, y), (x, y - 9), (x, y + 9)]
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINES", {"pos": lines})
    width = gpu.state.line_width_get()
    try:
        shader.bind()
        for size, color in ((5, (0.04, 0.04, 0.04, 1.0)), (2, (1.0, 0.73, 0.12, 1.0))):
            gpu.state.line_width_set(size)
            shader.uniform_float("color", color)
            batch.draw(shader)
    finally:
        gpu.state.line_width_set(width)


@persistent
def _reset(_unused=None):
    manager = bpy.context.window_manager
    if manager is not None:
        for window in manager.windows:
            if window.as_pointer() in _pickers:
                window.cursor_modal_restore()
    _pickers.clear()
    _points.clear()


_CLASSES = (HSW_OT_reference_image_pick, HSW_OT_reference_image_clear, HSW_PT_reference_image)


def register():
    global _draw_handle
    if _draw_handle is not None:
        return
    registered = []
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
        _draw_handle = bpy.types.SpaceImageEditor.draw_handler_add(_draw_marker, (), "WINDOW", "POST_PIXEL")
        bpy.app.handlers.load_pre.append(_reset)
    except Exception:
        if _draw_handle is not None:
            bpy.types.SpaceImageEditor.draw_handler_remove(_draw_handle, "WINDOW")
            _draw_handle = None
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise


def unregister():
    global _draw_handle
    if _draw_handle is None:
        return
    _reset()
    bpy.app.handlers.load_pre.remove(_reset)
    bpy.types.SpaceImageEditor.draw_handler_remove(_draw_handle, "WINDOW")
    _draw_handle = None
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
