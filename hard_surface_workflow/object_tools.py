"""Public object-mode dispatch shared by the CLI and catalog exporter."""
import bpy
from . import draw_tools, fillet_tools, profiles, reference_contour, seat_tools, shell_tools, stack_tools, surface_tools
from . import surface_sweep


def catalog():
    result = {**stack_tools.catalog(), **profiles.catalog(), **shell_tools.catalog(), **reference_contour.catalog(),
              **surface_tools.catalog(), **surface_sweep.catalog(), **seat_tools.catalog(),
              **fillet_tools.catalog(), **draw_tools.catalog()}
    settings = {}
    for kind, (class_name, _) in stack_tools.NATIVE.items():
        properties = getattr(bpy.types, class_name).bl_rna.properties
        fields = {}
        for name in stack_tools.RNA_FIELDS[kind]:
            prop = properties[name]
            field = {'type':prop.type, 'description':prop.description}
            if prop.type in {'INT', 'FLOAT'}:
                field['min'] = prop.hard_min
                field['max'] = min(prop.hard_max, stack_tools.MAX_VALUES.get((kind,name), prop.hard_max))
                field['unit'] = prop.unit
            if prop.type == 'ENUM':
                field['choices'] = [item.identifier for item in prop.enum_items if item.identifier]
                field['flag_array'] = prop.is_enum_flag
            if getattr(prop,'is_array',False):
                field['length'] = prop.array_length
            if prop.type == 'POINTER':
                field['type'] = 'object_name_or_null'
                field['object_types'] = sorted(stack_tools.OBJECT_TYPES.get((kind,name), ()))
            fields[name] = field
        settings[kind] = fields
    for key in ('stack_add', 'stack_update'):
        result[key]['settings_contract'] = settings
    return result


def execute(obj, tool, params):
    if tool == surface_sweep.TOOL:
        return surface_sweep.execute(obj, tool, params)
    if tool == draw_tools.TOOL:
        return draw_tools.execute(obj, tool, params)
    if tool in surface_tools.TOOLS:
        return surface_tools.execute(obj, tool, params)
    if tool in seat_tools.TOOLS:
        return seat_tools.execute(obj, tool, params)
    if tool in (fillet_tools.REFIT, fillet_tools.CLEANUP):
        return fillet_tools.execute(obj, tool, params)
    if tool == reference_contour.TOOL:
        return reference_contour.execute(obj, tool, params)
    if tool in profiles.TOOLS:
        return profiles.execute(obj, tool, params)
    if tool == "shell_extract":
        return shell_tools.execute(obj, tool, params)
    return stack_tools.execute(obj, tool, params)
