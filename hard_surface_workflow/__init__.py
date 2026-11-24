"""Hard Surface Workflow for Blender."""

SUPPORTED_BLENDER = (5, 3, 0)

bl_info = {
    "name": "Hard Surface Workflow", "author": "Blender AI",
    "version": (0, 3, 6), "blender": SUPPORTED_BLENDER,
    "location": "View3D > Sidebar > Hard Surface", "category": "Mesh",
}
_registered = []


def require_supported_blender():
    import bpy

    if tuple(bpy.app.version) != SUPPORTED_BLENDER:
        raise RuntimeError("Hard Surface Workflow requires Blender 5.3.0")


def register():
    require_supported_blender()
    from . import core, draw_tools, face_flow_panel, face_flow_reference_panel, panel, profiles, recipe_panel, reference_contour, review_grid, seat_tools, shell_tools, stack_panel
    if _registered:
        return
    try:
        for module in (core, face_flow_reference_panel, face_flow_panel, panel, shell_tools, seat_tools,
                       stack_panel, profiles, reference_contour, review_grid, recipe_panel, draw_tools):
            module.register()
            _registered.append(module)
    except Exception:
        unregister()
        raise


def unregister():
    errors = []
    while _registered:
        module = _registered.pop()
        try:
            module.unregister()
        except RuntimeError as exc:
            errors.append(f"{module.__name__}: {exc}")
    if errors:
        raise RuntimeError("\n".join(errors))
