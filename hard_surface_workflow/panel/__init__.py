"""Hard Surface Workflow sidebar package.

The parent add-on owns ``core``.  This package imports it only at runtime
(``from .. import core``) while loading the catalog or executing an operator,
so the panel can be registered after ``core.register()`` without a module-level
dependency cycle.
"""

_registered = False


def register():
    """Register the dynamic state, operators and View3D sidebar panels."""
    global _registered
    if _registered:
        return
    from . import settings, operators, ui

    settings.install()
    try:
        operators.register()
        try:
            ui.register()
        except Exception:
            ui.unregister()
            raise
    except Exception:
        operators.unregister()
        settings.uninstall()
        raise
    _registered = True


def unregister():
    """Undo :func:`register` in reverse order; safe to call repeatedly."""
    global _registered
    from . import settings, operators, ui

    ui.unregister()
    operators.unregister()
    settings.uninstall()
    _registered = False


__all__ = ("register", "unregister")

