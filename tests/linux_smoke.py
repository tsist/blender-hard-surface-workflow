"""Blender-side checks; run via linux-smoke.sh, not the system Python."""
import importlib
import os
from pathlib import Path
import sys
import tomllib

import bpy


MODULE = "bl_ext.user_default.hard_surface_workflow"
OBJECT_NAME = "HSW Linux Smoke Quad"
CORNERS = {(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (2.0, 2.0, 0.0), (0.0, 2.0, 0.0)}
FIRST, LAST = (0.0, 0.0, 0.0), (2.0, 2.0, 0.0)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def installed_extension():
    require(
        tuple(bpy.app.version) == (5, 3, 0),
        f"Expected Blender 5.3.0, found {bpy.app.version_string}",
    )
    require(MODULE in bpy.context.preferences.addons, f"{MODULE} not enabled after cold start")
    # Do not manually import/enable the package as a fallback: verify startup registration.
    module = sys.modules.get(MODULE)
    require(module is not None, f"{MODULE} not loaded after cold start")
    package = Path(module.__file__).resolve()
    profile = Path(os.environ["BLENDER_USER_RESOURCES"]).resolve()
    require(package.is_relative_to(profile), f"Loaded extension outside the isolated profile: {package}")
    with package.with_name("blender_manifest.toml").open("rb") as stream:
        version = tomllib.load(stream).get("version")
    require(version == "0.3.5", f"Expected extension version 0.3.5, found {version!r}")
    require(hasattr(bpy.types.Scene, "hsw_settings"), "Hard Surface scene settings are not registered")
    print(f"Registered {MODULE} {version} (Blender {bpy.app.version_string})")


def check_mesh():
    obj = bpy.context.scene.objects.get(OBJECT_NAME)
    require(obj and obj.type == "MESH", f"Expected saved mesh object {OBJECT_NAME!r}")
    mesh = obj.data
    counts = (len(mesh.vertices), len(mesh.edges), len(mesh.polygons))
    require(counts == (4, 5, 2), f"Expected 4 vertices, 5 edges, 2 faces; got {counts}")
    coords = [tuple(v.co) for v in mesh.vertices]
    require(set(coords) == CORNERS, f"Source corner positions changed: {coords}")
    diagonal = frozenset((FIRST, LAST))
    require(
        sum(frozenset(coords[i] for i in edge.vertices) == diagonal for edge in mesh.edges) == 1,
        "Expected exactly one diagonal between opposite source corners",
    )
    expected_faces = {diagonal | {corner} for corner in CORNERS - diagonal}
    actual_faces = {frozenset(coords[i] for i in face.vertices) for face in mesh.polygons}
    require(actual_faces == expected_faces, f"Expected two triangles sharing the diagonal; got {actual_faces}")
    print(f"Verified mesh counts {counts} and expected shared diagonal")


def split(output):
    mesh = bpy.data.meshes.new(OBJECT_NAME)
    mesh.from_pydata([FIRST, (2, 0, 0), LAST, (0, 2, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new(OBJECT_NAME, mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.update()
    core = importlib.import_module(f"{MODULE}.core")
    result = core.execute(obj, "native.split_face", {"kind": "indices", "domain": "FACE", "indices": [0]},
                          {"vertices": [0, 2]})
    require(result.get("status") == "FINISHED", f"Split Face did not finish: {result}")
    check_mesh()
    require(bpy.ops.wm.save_as_mainfile(filepath=str(output)) == {"FINISHED"}, f"Save failed: {output}")


def main():
    phase, filename = sys.argv[sys.argv.index("--") + 1:]
    installed_extension()
    if phase == "split":
        split(filename)
    elif phase == "reopen":
        require(Path(bpy.data.filepath).resolve() == Path(filename).resolve(), f"Did not reopen {filename}")
        check_mesh()
    else:
        raise ValueError(f"Unknown smoke test phase: {phase}")


if __name__ == "__main__":
    main()
