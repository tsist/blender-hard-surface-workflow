"""Hard Surface Workflow for Blender."""

bl_info = {
    "name": "Hard Surface Workflow", "author": "Blender AI",
    "version": (0, 3, 1), "blender": (5, 2, 0),
    "location": "View3D > Sidebar > Hard Surface", "category": "Mesh",
}
_registered = []


def register():
    from . import core, draw_tools, panel, profiles, recipe_panel, reference_contour, review_grid, seat_tools, shell_tools, stack_panel
    if _registered:
        return
    try:
        for module in (core, panel, shell_tools, seat_tools, stack_panel, profiles, reference_contour, review_grid, recipe_panel, draw_tools):
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
