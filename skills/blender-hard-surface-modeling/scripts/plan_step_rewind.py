"""Select the state before a recorded model.prepare operation. Emit a public CLI request."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.dont_write_bytecode = True
TOOLS = Path(__file__).resolve().parents[4] / "tools"
sys.path[:0] = [str(TOOLS / "blenderctl"), str(TOOLS)]
from protocol import Failure, digest, validate

HARD_SURFACE_OPERATIONS = {"mesh.flow", "hard_surface.stack"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def current_addon_version():
    from hard_surface_workflow_addon import bl_info
    return ".".join(map(str, bl_info["version"]))


def recorded_addon_version(job, result):
    version = (result.get("data") or {}).get("addon_version")
    report = job / "hard-surface-report.json"
    if version is None and report.is_file():
        # A failed job can still have applied some operations and written this report.
        version = read(report).get("addon_version")
    return version or "unknown"


def write_new(path, request):
    """Publish one complete request. Replanning the identical request is allocation-free."""
    payload = (json.dumps(request, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if path.suffix.lower() != ".json":
        raise ValueError("The request output must be a new JSON file")
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise ValueError(f"Different output already exists: {path}")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        if os.name == "nt":
            os.rename(tmp, path)  # Windows never replaces an existing destination here.
        else:
            os.link(tmp, path)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise
    finally:
        tmp.unlink(missing_ok=True)


def plan(job, index, current):
    request = read(job / "request.json")
    result = read(job / "result.json")
    status = read(job / "status.json")
    if not (request["command"] == result["command"] == "model.prepare"
            and result["job_id"] == status["job_id"] == job.name
            and Path(result["artifacts"]["job_directory"]).resolve() == job
            and Path(result["artifacts"]["request"]).resolve() == job / "request.json"
            and status["state"] in ("succeeded", "failed")
            and result["ok"] is (status["state"] == "succeeded")):
        raise ValueError("Expected a terminal model.prepare job with matching identity and status")
    params = request["params"]
    if not params.get("file") or not params.get("expected_sha256"):
        raise ValueError("This job has no saved parent anchor; use its recorded initial construction recipe")
    source = Path(params["file"])
    anchor_sha = params["expected_sha256"]
    if not source.is_absolute() or digest(source) != anchor_sha:
        raise ValueError("Parent .blend does not match the recorded file SHA")
    if status.get("source_sha256_before", anchor_sha) != anchor_sha:
        raise ValueError("Job status disagrees with the parent anchor")
    manifest = params["manifest"]
    operations = manifest["operations"]
    action_path = job / "model-actions.json"
    actions = read(action_path) if action_path.exists() else []
    if len(actions) > len(operations) or any(
        row.get("index") != i or row.get("op") != operations[i]["op"]
        or row.get("status") != "applied_in_candidate_memory" for i, row in enumerate(actions)
    ):
        raise ValueError("Action log does not match the recorded operation order")
    if result["ok"]:
        data = result["data"]
        change = read(job / "model-change.json")
        if not (change["operations"] == operations and change["context"] == manifest["context"]
                and data.get("source_sha256") == anchor_sha and data.get("source_unchanged") is True
                and data.get("input_hashes", {}).get(str(source)) == anchor_sha
                and data.get("reopen") == "pass" and len(actions) == len(operations) == data["operations"]):
            raise ValueError("Saved operation values, context or source differ from the successful job receipt")
        for filename, expected in data.get("input_hashes", {}).items():
            if Path(filename) != source and digest(Path(filename)) != expected:
                raise ValueError(f"Recorded external input changed: {filename}")
    if current is not None and (not result["ok"] or digest(current) != result["data"]["candidate_sha256"]):
        raise ValueError("Current disk state differs from the job candidate; inspect later edits before branching")
    summary = {
        "job": job.name, "source": str(source), "source_sha256": anchor_sha,
        "blender": read(job / "launch.json")["blender"], "cli_version": result["cli_version"],
        "recorded_addon_version": recorded_addon_version(job, result),
        "current_addon_version": current_addon_version(),
        "completed_operations": len(actions), "context": manifest["context"],
        "steps": [{"index": i, "operation": op["op"], "object": op.get("object"), "tool": op.get("tool")}
                  for i, op in enumerate(operations)],
    }
    if index is None:
        return summary, None
    if not 0 <= index < len(operations) or index > len(actions):
        raise ValueError("Use the zero-based index of the first unwanted operation, after a recorded prefix")
    prefix = operations[:index]
    recorded_version = summary["recorded_addon_version"]
    current_version = summary["current_addon_version"]
    if (recorded_version not in {"unknown", current_version}
            and any(op["op"] in HARD_SURFACE_OPERATIONS for op in prefix)):
        raise ValueError(f"Prefix requires Hard Surface Workflow {recorded_version}; "
                         f"the local implementation is {current_version}")
    # The saved context may select objects that are created later. Inspect the original
    # saved scene at index zero; a nonempty prefix retains its exact recorded context.
    next_params = {"file": str(source), "expected_sha256": anchor_sha}
    if index:
        next_params["manifest"] = {**manifest, "operations": prefix}
    next_request = {"schema_version": "1.0", "command": "model.prepare" if index else "model.inspect",
                    "params": next_params}
    validate(next_request)
    summary.update(first_excluded=summary["steps"][index], retained_operations=index,
                   target="replayed_prefix_candidate" if index else "original_disk_anchor")
    return summary, next_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path, help="Original job directory, including its recorded receipts")
    parser.add_argument("--before-index", type=int, help="Zero-based index of the first operation to exclude")
    parser.add_argument("--current", type=Path, help="Check that this .blend still matches the old job output")
    parser.add_argument("--output", type=Path, help="Write the public request here; existing content is protected")
    args = parser.parse_args()
    if (args.before_index is None) != (args.output is None):
        parser.error("Use --before-index and --output together, or omit both to list the steps")
    job = args.job.resolve()
    if args.output and job in args.output.resolve().parents:
        parser.error("Keep the new request outside the original job directory")
    summary, request = plan(job, args.before_index, args.current)
    if request is not None:
        write_new(args.output, request)
        summary["request"] = str(args.output.resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (Failure, OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
