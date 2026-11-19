"""Shared GUI/CLI execution of bounded edit-mesh operations."""

from contextlib import contextmanager
import math
import time
import uuid

import bmesh
import bpy

from . import features, loop_tools, mesh_tools, quad_patch, selection


PROVIDERS = (loop_tools, mesh_tools, features, quad_patch)
MAX_VERTICES = 200000
MAX_CORNERS = 1000000
MAX_REPEAT = 64


def register():
    done = []
    try:
        for provider in PROVIDERS:
            provider.register()
            done.append(provider)
    except Exception:
        for provider in reversed(done):
            provider.unregister()
        raise


def unregister():
    for provider in reversed(PROVIDERS):
        provider.unregister()


def catalog():
    return {key: provider.describe(key) for provider in PROVIDERS for key in provider.TOOLS}


def _provider(tool):
    for provider in PROVIDERS:
        if tool in provider.TOOLS:
            return provider
    raise ValueError(f"Unknown hard-surface tool: {tool}")


def preview(obj, selector):
    return selection.resolve(obj, selector)


def select_region(obj, selector):
    return selection.apply(obj, preview(obj, selector))


def _mode(mode):
    if bpy.ops.object.mode_set(mode=mode) != {'FINISHED'}:
        raise RuntimeError(f"Could not enter {mode} mode")


def _available(obj):
    if obj is None or obj.type != 'MESH' or obj.mode not in {'EDIT', 'OBJECT'}:
        raise ValueError('Select an editable mesh in Object or Edit Mode')
    layer = bpy.context.view_layer
    if obj.name not in layer.objects or not obj.visible_get(view_layer=layer) or obj.hide_select:
        raise ValueError('The target must be visible and selectable in the active view layer')
    if bpy.context.mode != 'OBJECT' and (bpy.context.mode != 'EDIT_MESH' or
            len(bpy.context.objects_in_mode) != 1 or bpy.context.edit_object != obj):
        raise ValueError('Finish editing other objects before changing this mesh')
    data = obj.data
    if obj.library or obj.override_library or data.library or data.override_library:
        raise ValueError('Linked and overridden meshes require a local working copy')
    if data.shape_keys or data.animation_data or obj.animation_data:
        raise ValueError('Animated and shape-keyed meshes require a deformation-aware edit')
    if data.users - int(data.use_fake_user) > 1:
        raise ValueError('Shared mesh data: make a single-user working copy before applying')
    if len(data.vertices) > MAX_VERTICES or len(data.loops) > MAX_CORNERS:
        raise ValueError('Mesh exceeds the edit budget')


@contextmanager
def _working_mesh(obj):
    """Run operators on a private mesh while preserving object transforms and stack context."""
    layer = bpy.context.view_layer
    original_active = layer.objects.active
    selected = list(bpy.context.selected_objects)
    in_edit = obj.mode == 'EDIT'
    old_selection_mode = tuple(bpy.context.tool_settings.mesh_select_mode)
    final_selection_mode = old_selection_mode
    if in_edit:
        _mode('OBJECT')
    source = obj.data
    stage = None
    try:
        for other in selected:
            other.select_set(False)
        obj.select_set(True)
        layer.objects.active = obj
        stage = source.copy()
        obj.data = stage
        _mode('EDIT')
        yield source, stage
        if in_edit:
            final_selection_mode = tuple(bpy.context.tool_settings.mesh_select_mode)
    finally:
        if obj.mode == 'EDIT':
            _mode('OBJECT')
        obj.data = source
        if stage is not None and stage.users - int(stage.use_fake_user) == 0:
            bpy.data.meshes.remove(stage)
        obj.select_set(False)
        for other in selected:
            other.select_set(True)
        layer.objects.active = original_active
        bpy.context.tool_settings.mesh_select_mode = final_selection_mode
        if in_edit:
            _mode('EDIT')


def _mesh(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    for sequence in (bm.verts, bm.edges, bm.faces):
        sequence.ensure_lookup_table()
        sequence.index_update()
    return bm


def _elements(bm, resolved):
    return [getattr(bm, selection.DOMAINS[resolved['domain']])[i] for i in resolved['indices']]


def _vertices(bm, resolved):
    elements = _elements(bm, resolved)
    if resolved['domain'] == 'VERT':
        return set(elements)
    return {v for element in elements for v in element.verts}


def _cycle(ids):
    ids = tuple(ids)
    start = ids.index(min(ids))
    return ids[start:] + ids[:start]


def _weights(v, deform):
    return tuple(sorted(v[deform].items())) if deform else ()


def _protect_attributes(bm, selected, protections):
    domain = selected['domain']
    target = set(_elements(bm, selected))
    for region in protections:
        elements = set(_elements(bm, region))
        if domain == 'VERT':
            elements = _vertices(bm, region)
        elif domain == 'EDGE' and region['domain'] == 'FACE':
            elements = {edge for face in elements for edge in face.edges}
        if target.intersection(elements):
            raise ValueError('Selected attribute belongs to a protected element')


def _preflight_growth(bm, tool, settings):
    """Estimate large allocations before invoking native or vendored mesh operators."""
    growing = {'native.subdivide', 'native.bevel', 'native.bridge', 'looptools.bridge', 'looptools.loft',
               'array.linear', 'array.radial', 'form.lathe', 'form.hole'}
    if tool not in growing:
        return
    # Parameters outside their declared type/bounds are rejected by the provider before
    # it calls Blender. Keep this estimate independent of provider validation.
    def count(key, default):
        value = settings.get(key, default)
        return value if type(value) is int and value > 0 else default
    verts = sum(v.select for v in bm.verts)
    edges = sum(e.select for e in bm.edges)
    face_sizes = [len(f.verts) for f in bm.faces if f.select]
    corners = sum(len(f.verts) for f in bm.faces)
    if tool == 'native.subdivide':
        cuts = count('number_cuts', 1)
        quads = sum(n == 4 for n in face_sizes)
        tris = sum(n == 3 for n in face_sizes)
        extra = edges * cuts + quads * cuts ** 2 + tris * cuts * (cuts - 1) // 2
        new_corners = (4 * quads + 3 * tris) * ((cuts + 1) ** 2 - 1)
    elif tool == 'native.bevel':
        segments = count('segments', 1)
        extra = (edges + verts) * segments
        new_corners = edges * segments * 4
        if segments > 1 and settings.get('affect', 'EDGES') == 'EDGES':
            junctions = sum(v.select and sum(e.select for e in v.link_edges) >= 3 for v in bm.verts)
            extra += junctions * segments ** 2
    elif tool in {'array.linear', 'array.radial'}:
        copies = count('count', 2) - 1
        extra, new_corners = verts * copies, sum(face_sizes) * copies
    elif tool == 'form.hole':
        extra = len(face_sizes) * count('segments', 16)
        new_corners = extra + 2 * sum(face_sizes)
    elif tool == 'form.lathe':
        steps = count('steps', 16)
        extra, new_corners = verts * steps, 4 * edges * steps
    else:
        steps = count('segments', 1) if tool.startswith('looptools.') else 1 + count('number_cuts', 0)
        extra, new_corners = (edges // 2) * (steps - 1), 4 * (edges // 2) * steps
    if len(bm.verts) + extra > MAX_VERTICES or corners + new_corners > MAX_CORNERS:
        raise ValueError('Estimated result exceeds the edit budget before mesh allocation')


def _initial(bm, selected, protections, marker, allow_expansion):
    layer = bm.verts.layers.int.new(marker)
    deform = bm.verts.layers.deform.active
    for vertex in bm.verts:
        vertex[layer] = vertex.index + 1
    chosen = _vertices(bm, selected)
    pins = set()
    edges, faces = set(), set()
    for protected in protections:
        pins.update(_vertices(bm, protected))
        if protected['domain'] == 'EDGE':
            edges.update(tuple(sorted(v[layer] for v in e.verts)) for e in _elements(bm, protected))
        if protected['domain'] == 'FACE':
            faces.update(_cycle(v[layer] for v in f.verts) for f in _elements(bm, protected))
    fixed = pins | {v for v in bm.verts if v.hide or (not allow_expansion and v not in chosen)}
    locked = {v[layer]: (tuple(v.co), _weights(v, deform)) for v in fixed}
    if not allow_expansion:
        # Faces outside the selected vertices must survive with their winding intact.
        for face in bm.faces:
            if all(v not in chosen for v in face.verts):
                faces.add(_cycle(v[layer] for v in face.verts))
    return locked, {v[layer] for v in pins}, edges, faces


def _check_scope(bm, marker, locked, pins, required_edges, required_faces, coordinate_only):
    layer = bm.verts.layers.int.get(marker)
    if layer is None:
        raise ValueError('Tool discarded the source vertex correspondence')
    deform = bm.verts.layers.deform.active
    matched = set()
    for vertex in bm.verts:
        if not all(math.isfinite(c) for c in vertex.co):
            raise ValueError('Tool produced a non-finite coordinate')
        identity = vertex[layer]
        if identity not in locked:
            continue
        co, weights = locked[identity]
        if coordinate_only and identity in pins:
            vertex.co = co
        if tuple(vertex.co) == co and _weights(vertex, deform) == weights:
            matched.add(identity)
    missing = locked.keys() - matched
    if missing:
        sample = [i-1 for i in sorted(missing)[:12]]
        raise ValueError(f'Tool moved or removed {len(missing)} protected or out-of-selection vertices: {sample}')
    if required_edges:
        found = {tuple(sorted(v[layer] for v in e.verts)) for e in bm.edges}
        if not required_edges <= found:
            raise ValueError('A protected edge was removed')
    if required_faces:
        found = {_cycle(v[layer] for v in f.verts) for f in bm.faces}
        if not required_faces <= found:
            raise ValueError('A protected or out-of-selection face changed connectivity or winding')


def _commit(obj, source, stage, coordinates_only):
    if coordinates_only:
        coords = [0.0] * (3 * len(stage.vertices))
        stage.vertices.foreach_get('co', coords)
        source.vertices.foreach_set('co', coords)
        # Preserve original custom data and datablock identity on positional edits.
        for name in ('vertices', 'edges', 'polygons'):
            src_domain = getattr(stage, name)
            flags = [False] * len(src_domain)
            src_domain.foreach_get('select', flags)
            getattr(source, name).foreach_set('select', flags)
    else:
        output = bmesh.new()
        try:
            output.from_mesh(stage)
            output.to_mesh(source)
        finally:
            output.free()
    source.update()


def execute(obj, tool, selector, params=None, repeat=1, protect=None, allow_expansion=False):
    """Resolve once, run on a scratch mesh, verify scope, then commit to the original mesh ID."""
    _available(obj)
    provider = _provider(tool)
    spec = provider.describe(tool)
    if type(repeat) is not int or not 1 <= repeat <= MAX_REPEAT:
        raise ValueError(f'repeat must be an integer from 1 to {MAX_REPEAT}')
    if tool == 'patch.quad_grid' and repeat != 1:
        raise ValueError('Patch replacement requires one pass; select its new topology before another edit')
    if type(allow_expansion) is not bool:
        raise ValueError('allow_expansion must be boolean')
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise ValueError('params must be an object')
    selectors = [protect] if isinstance(protect, dict) else protect
    if selectors is not None and not isinstance(selectors, (list, tuple)):
        raise ValueError('protect must supply a selector or a list of selectors')
    if spec.get('context') == 'EXPLICIT_GUIDE_FRAME':
        if repeat != 1 or selectors or allow_expansion:
            raise ValueError('Named guide-frame maintenance requires a single operation')
        # The maintenance provider validates explicit guide identity, stroke count and confirmation.
        return provider.run(tool, params)
    selected = preview(obj, selector)
    converting_strokes = tool == 'looptools.gstretch' and not selected['indices'] and bool(params.get('strokes') or params.get('guide'))
    if selected['domain'] not in spec['domains'] or (not selected['indices'] and not converting_strokes):
        raise ValueError(f"{tool} requires a nonempty selection in {spec['domains']}")
    protections = [preview(obj, s) for s in selectors or []]
    effective = {key: field['default'] for key, field in spec['parameters'].items() if 'default' in field}
    effective.update(params)
    if tool == 'native.bevel' and 'affect' not in params:
        effective['affect'] = 'VERTICES' if selected['domain'] == 'VERT' else 'EDGES'
    start = time.perf_counter()
    marker = 'hs_id_' + uuid.uuid4().hex
    with _working_mesh(obj) as (source, stage):
        selection.apply(obj, selected)
        bm = _mesh(obj)
        if spec.get('effect') == 'attributes':
            _protect_attributes(bm, selected, protections)
        start_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
        original_coords = [tuple(v.co) for v in bm.verts]
        locked, pins, edges, faces = _initial(bm, selected, protections, marker, allow_expansion)
        bmesh.update_edit_mesh(stage, loop_triangles=False, destructive=False)
        results = []
        final_hash = selected['topology_sha256']
        for index in range(repeat):
            bm = _mesh(obj)
            if index and final_hash == selected['topology_sha256'] and spec.get('effect') != 'selection':
                selection.apply(obj, selected)
            if not any(v.select for v in bm.verts) and not (index == 0 and converting_strokes):
                raise ValueError(f'Pass {index+1} has no selected output from the preceding operation')
            _preflight_growth(bm, tool, effective)
            if tool in {'edgeflow.set_vertex_curve', 'patch.quad_grid'}:
                result = provider.run(tool, params, allow_expansion=allow_expansion, protected_indices={i-1 for i in pins})
            else:
                result = provider.run(tool, params)
            if not isinstance(result, dict) or result.get('status') != 'FINISHED':
                raise ValueError(f'{tool} did not finish on pass {index+1}: {result}')
            results.append(result)
            bm = _mesh(obj)
            if len(bm.verts) > MAX_VERTICES or sum(len(f.verts) for f in bm.faces) > MAX_CORNERS:
                raise ValueError('Result exceeds the edit budget')
            previous_hash, final_hash = final_hash, selection.topology_hash(obj)
            _check_scope(bm, marker, locked, pins, edges, faces, previous_hash == final_hash)
            bmesh.update_edit_mesh(stage, loop_triangles=False, destructive=False)
        layer = bm.verts.layers.int[marker]
        end_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
        # BMesh may copy the marker onto a newly extruded vertex. A surviving
        # source point at its original position must still count as stationary.
        survivors, stationary = set(), set()
        for vertex in bm.verts:
            identity = vertex[layer] - 1
            if 0 <= identity < len(original_coords):
                survivors.add(identity)
                if tuple(vertex.co) == original_coords[identity]:
                    stationary.add(identity)
        moved = sorted(survivors - stationary)
        bm.verts.layers.int.remove(layer)
        bmesh.update_edit_mesh(stage, loop_triangles=True, destructive=True)
        _mode('OBJECT')
        coords_only = (tool.startswith(('looptools.', 'edgeflow.', 'align.')) or spec.get('coordinates_only',False)) and final_hash == selected['topology_sha256']
        _commit(obj, source, stage, coords_only)
    return {'tool':tool, 'status':'FINISHED', 'selection':selected, 'repeat':repeat, 'parameters':params,
            'effective_parameters':effective, 'protected_vertices':len(pins), 'allow_expansion':allow_expansion,
            'moved_source_vertices':len(moved), 'moved_source_indices_first_512':moved[:512],
            'source_vertices_without_correspondence':len(original_coords)-len(survivors),
            'counts_before':start_counts, 'counts_after':end_counts, 'result_topology_sha256':final_hash,
            'passes':results, 'elapsed_seconds':time.perf_counter()-start}
