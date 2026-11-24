"""Plan a local mesh.positions rewind for an explicitly chosen saved descendant.

Run in background Blender with --disable-autoexec. The only file this script
creates is a previously absent, SHA-bound model.prepare request.
"""

import argparse
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
from tempfile import NamedTemporaryFile

import bpy

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "blenderctl"))
from model_contract import normalize
from modeling import bounded, topology
from protocol import Failure, SCHEMA, digest


def require(condition, message):
    if not condition:
        raise ValueError(message)


def absolute_path(value, description):
    path = Path(value)
    require(path.is_absolute(), f"{description} requires an absolute path")
    return path.resolve()


def blend_hash(path):
    require(path.suffix.lower() == ".blend" and path.is_file(), f"Missing .blend: {path}")
    return digest(path)


def read_job(job):
    request_path = job / "request.json"
    result_path = job / "result.json"
    with request_path.open(encoding="utf-8-sig") as stream:
        request = json.load(stream)
    with result_path.open(encoding="utf-8-sig") as stream:
        result = json.load(stream)

    require(isinstance(request, dict) and isinstance(result, dict)
            and request.get("schema_version") == result.get("schema_version") == SCHEMA
            and request.get("command") == result.get("command") == "model.prepare"
            and result.get("ok") is True and result.get("job_id") == job.name,
            "Expected one successful public model.prepare job")
    params, data = request["params"], result["data"]
    artifacts = result["artifacts"]
    require(absolute_path(artifacts["request"], "Recorded request") == request_path
            and absolute_path(artifacts["job_directory"], "Recorded job") == job,
            "Request/result provenance does not match the supplied job directory")
    manifest = normalize(params["manifest"])
    operations = manifest["operations"]
    require("initial_scene" not in manifest and len(operations) == data["operations"] == 1
            and operations[0]["op"] == "mesh.positions"
            and isinstance(operations[0]["selection"], dict)
            and operations[0]["data_scope"] in {"reject_shared", "single_user"}
            and data.get("context") == manifest["context"] and data.get("reopen") == "pass",
            "Expected one explicit-index mesh.positions operation on an existing local mesh")
    old_source = absolute_path(params["file"], "Recorded source")
    old_candidate = absolute_path(data["candidate"], "Recorded candidate")
    require(old_candidate == job / "model-candidate.blend" and old_candidate != old_source
            and data.get("source_unchanged") is True,
            "Recorded candidate or source identity does not match the successful job")
    source_sha = params["expected_sha256"]
    require(data.get("source_sha256") == source_sha == data.get("input_hashes", {}).get(str(old_source)),
            "Saved job does not bind the exact source to its successful result")
    require(blend_hash(old_source) == source_sha and blend_hash(old_candidate) == data["candidate_sha256"],
            "Recorded source or result candidate no longer matches its SHA-256")
    return operations[0], old_source, old_candidate, source_sha, data["candidate_sha256"]


def inspect_blend(path, expected_sha, object_name, expected_topology):
    bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False, use_scripts=False)
    require(tuple(bpy.data.version[:2]) == tuple(bpy.app.version[:2]),
            f"{path.name}: Blender major/minor differs from the running Blender")
    require(bpy.context.mode == "OBJECT" and not bpy.data.libraries
            and not bpy.utils.blend_paths(absolute=True, packed=False)
            and not bpy.data.texts,
            f"{path.name}: requires a self-contained object-mode project with no unresolved external dependencies or text blocks")
    obj = bpy.data.objects.get(object_name)
    require(obj is not None and obj.type == "MESH", f"{path.name}: missing mesh object {object_name!r}")
    mesh = obj.data
    modifiers = tuple(obj.modifiers)
    require(len(modifiers) <= 1 and all(modifier.type == "SUBSURF" for modifier in modifiers),
            f"{path.name}: only one native Subdivision Surface modifier is supported")
    require(not obj.library and not obj.override_library and not obj.animation_data
            and not mesh.library and not mesh.override_library and not mesh.animation_data
            and not mesh.shape_keys and not obj.vertex_groups and mesh.users == 1
            and not obj.constraints and obj.parent is None
            and obj.rigid_body is None and obj.rigid_body_constraint is None
            and obj.instance_type == "NONE",
            f"{path.name}: shared, linked, deformed, animated or dependent target cannot be verified")
    from bpy.types import Collection, Scene
    users = bpy.data.user_map(subset={obj, mesh, *bpy.data.collections,
                                    *(scene.collection for scene in bpy.data.scenes)})
    require(set(users.get(mesh, ())) == {obj},
            f"{path.name}: the target object or mesh has another data consumer")
    containers = [obj]
    seen = {obj}
    while containers:
        for owner in users.get(containers.pop(), ()):
            require(isinstance(owner, (Collection, Scene)),
                    f"{path.name}: the target or its collections have another data consumer")
            if isinstance(owner, Collection) and owner not in seen:
                seen.add(owner)
                containers.append(owner)
    require(all(scene.camera != obj for scene in bpy.data.scenes),
            f"{path.name}: the target has an unsupported scene relationship")
    bounded(mesh)
    require(topology(mesh) == expected_topology,
            f"{path.name}: indexed topology differs from the recorded operation")
    positions = [tuple(vertex.co) for vertex in mesh.vertices]
    require(all(math.isfinite(axis) for position in positions for axis in position),
            f"{path.name}: target mesh has non-finite coordinates")
    require(blend_hash(path) == expected_sha, f"{path.name}: file changed during read")

    scene, layer = bpy.context.scene, bpy.context.view_layer
    active = layer.objects.active
    context = {
        "scene": scene.name,
        "view_layer": layer.name,
        "frame": scene.frame_current,
        "mode": "OBJECT",
        "active_object": active.name if active else None,
        "selected_objects": sorted(item.name for item in layer.objects if item.select_get(view_layer=layer)),
    }
    require(scene.frame_subframe == 0, f"{path.name}: public prepare cannot preserve a saved subframe")
    require(active is None or active.name in context["selected_objects"],
            f"{path.name}: saved active object is outside the saved selection")
    require(all(layer.objects[name].visible_get(view_layer=layer) for name in context["selected_objects"]),
            f"{path.name}: saved selected object is hidden from the view layer")
    return mesh.name, positions, context


def float32(position):
    return struct.unpack("fff", struct.pack("fff", *position))


def publish_no_replace(path, value):
    content = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    temp_path = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            require(path.is_file() and path.read_bytes() == content,
                    f"Output already exists with different contents: {path}")
            return "request_already_present"
        return "request_created"
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def plan(args):
    job = absolute_path(args.job, "Job directory")
    current_path = absolute_path(args.current, "Current saved descendant")
    output = absolute_path(args.output, "Output request")
    require(output.suffix.lower() == ".json" and output.parent.is_dir(),
            "Output must be a JSON file path in an existing directory")
    op, old_source, old_candidate, source_sha, candidate_sha = read_job(job)
    require(output.parent != job and job not in output.parents,
            "Choose an output outside the original job directory")
    current_sha = blend_hash(current_path)
    selection = op["selection"]
    object_name, expected_topology = op["object"], selection["topology_sha256"]
    original_indices = selection["indices"]
    chosen = args.indices if args.indices is not None else original_indices
    require(len(set(chosen)) == len(chosen) and set(chosen) <= set(original_indices),
            "Each requested index must be unique and belong to the recorded operation")
    names_and_positions = [
        inspect_blend(path, sha, object_name, expected_topology)
        for path, sha in ((old_source, source_sha), (old_candidate, candidate_sha), (current_path, current_sha))
    ]
    source_mesh, before, _ = names_and_positions[0]
    after_mesh, after, _ = names_and_positions[1]
    current_mesh, current, context = names_and_positions[2]
    require(source_mesh == after_mesh == current_mesh, "Target object's mesh identity changed across the three files")
    require(max(original_indices) < len(before) and len(op["positions"]) == len(original_indices),
            "Original vertex indices and requested positions do not match the source")
    written = dict(zip(original_indices, map(float32, op["positions"]), strict=True))
    require(all(actual == written.get(index, before[index]) for index, actual in enumerate(after)),
            "The verified old result does not match exactly one recorded mesh.positions edit")

    restored = [i for i in chosen if current[i] == before[i]]
    pending = [i for i in chosen if current[i] == after[i] and before[i] != after[i]]
    conflicts = [
        {"index": i, "old_was": before[i], "step_wrote": after[i], "current": current[i]}
        for i in chosen if current[i] != before[i] and current[i] != after[i]
    ]
    report = {
        "object": object_name, "mesh": current_mesh, "topology_sha256": expected_topology,
        "current_file": str(current_path), "current_sha256": current_sha,
        "already_restored_indices": restored, "conflicts": conflicts,
    }
    if conflicts:
        return {"status": "conflict", **report}
    if not pending:
        return {"status": "already_restored", **report}

    for path, sha in ((old_source, source_sha), (old_candidate, candidate_sha), (current_path, current_sha)):
        require(blend_hash(path) == sha, f"{path.name}: file changed before request publication")
    manifest = normalize({
        "context": context,
        "operations": [{
            "op": "mesh.positions", "object": object_name, "data_scope": "reject_shared",
            "selection": {"indices": pending, "topology_sha256": expected_topology},
            "positions": [list(before[i]) for i in pending],
        }],
    })
    request = {
        "schema_version": SCHEMA, "command": "model.prepare",
        "params": {"file": str(current_path), "expected_sha256": current_sha, "manifest": manifest},
    }
    status = publish_no_replace(output, request)
    return {"status": status, **report, "request": str(output), "rewind_indices": pending}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, help="Absolute directory of the old successful CLI job")
    parser.add_argument("--current", required=True, help="Absolute path to the current saved descendant")
    parser.add_argument("--output", required=True, help="Absolute path for the new standard CLI request")
    parser.add_argument("--indices", nargs="+", type=int, help="Optional subset of the recorded indices")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    try:
        result = plan(args)
    except (Failure, OSError, KeyError, TypeError, ValueError, struct.error) as exc:
        result = {"status": "unverified", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    if result["status"] in {"conflict", "unverified"}:
        sys.exit(2)


if __name__ == "__main__":
    main()
