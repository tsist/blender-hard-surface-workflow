"""Shared input contract for the face-flow panel and saved-file inspection."""

import math
from pathlib import Path
import re

LAYERS = ("CONTROL_CAGE", "EVALUATED")
KINDS = ("EDGE_LOOP", "EDGE_RING", "FACE_STRIP")
REFERENCE_FIELDS = ("source", "stage", "region", "landmark")
MAX_ELEMENTS = 600_000
MAX_LOOPS = 1_200_000
MAX_STEPS = 4096
DEFAULT_STEPS = 512
SCAN_LIMITS = {
    "max_elements": (100_000, 1, MAX_ELEMENTS, int),
    "max_candidates": (100, 1, 1000, int),
    "coincidence_mm": (0.01, 1e-6, 1000.0, float),
    "max_angle_deg": (35.0, 1.0, 180.0, float),
    "max_spacing_ratio": (3.0, 1.01, 1000.0, float),
}


def _object(properties, required=()):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(required)}


HASH = {"type": "string", "minLength": 64, "maxLength": 64, "pattern": "^[0-9a-f]{64}$"}
REFERENCE_TEXT = {"type": "string", "minLength": 1, "maxLength": 512}
INDEX = {"type": "integer", "minimum": 0, "maximum": MAX_ELEMENTS - 1}
TRACE_SCHEMA = {"oneOf": [
    _object({"kind": {"enum": list(KINDS[:2])}, "edge": INDEX,
             "max_steps": {"type": "integer", "minimum": 1, "maximum": MAX_STEPS,
                           "default": DEFAULT_STEPS}}, ("kind", "edge")),
    _object({"kind": {"const": "FACE_STRIP"}, "edge": INDEX, "face": INDEX,
             "max_steps": {"type": "integer", "minimum": 1, "maximum": MAX_STEPS,
                           "default": DEFAULT_STEPS}}, ("kind", "edge", "face")),
]}
IMAGE_POINT = _object({
    "file": {"type": "string", "minLength": 1, "maxLength": 4096},
    "sha256": HASH,
    "size_px": {"type": "array", "minItems": 2, "maxItems": 2,
                "items": {"type": "integer", "minimum": 1, "maximum": 100_000}},
    "pixel_xy": {"type": "array", "minItems": 2, "maxItems": 2,
                 "items": {"type": "integer", "minimum": 0, "maximum": 99_999}},
}, ("file", "sha256", "size_px", "pixel_xy"))
PROPERTIES = {
    "layer": {"enum": list(LAYERS)},
    "reference": _object({**dict.fromkeys(REFERENCE_FIELDS, REFERENCE_TEXT),
                          "image_point": IMAGE_POINT}, REFERENCE_FIELDS),
    "trace": TRACE_SCHEMA,
    "scan": _object({
        key: {"type": "integer" if kind is int else "number", "minimum": low,
              "maximum": high, "default": default}
        for key, (default, low, high, kind) in SCAN_LIMITS.items()
    }),
    "mesh_sha256": HASH,
}


def _fields(value, permitted, required, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(required) - value.keys() or value.keys() - set(permitted):
        raise ValueError(f"{label} has missing or unknown fields")


def _number(value, low, high, kind, label):
    allowed = (int,) if kind is int else (int, float)
    if type(value) not in allowed or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{label} must be between {low} and {high}")
    return kind(value)


def _image_point(point):
    _fields(point, IMAGE_POINT["properties"], IMAGE_POINT["required"], "reference.image_point")
    path, sha = point["file"], point["sha256"]
    if not isinstance(path, str) or not 1 <= len(path) <= 4096 or "\x00" in path or not Path(path).is_absolute():
        raise ValueError("reference.image_point.file must identify the static still")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("reference.image_point.sha256 must be a lowercase SHA-256")
    size, pixel = point["size_px"], point["pixel_xy"]
    for values, label in ((size, "size_px"), (pixel, "pixel_xy")):
        if not isinstance(values, (tuple, list)) or len(values) != 2 or any(type(v) is not int for v in values):
            raise ValueError(f"reference.image_point.{label} must have two integer values")
    if not all(1 <= total <= 100_000 and 0 <= coordinate < total
               for coordinate, total in zip(pixel, size)):
        raise ValueError("reference.image_point.pixel_xy is outside the still")
    return {"file": path, "sha256": sha, "size_px": list(size), "pixel_xy": list(pixel)}


def normalize(request):
    _fields(request, PROPERTIES, ("layer",), "face_flow")
    if request["layer"] not in LAYERS:
        raise ValueError("layer must be CONTROL_CAGE or EVALUATED")
    if not request.keys() & {"trace", "scan"}:
        raise ValueError("face_flow requires trace, scan, or both")
    normalized = {"layer": request["layer"]}
    if "reference" in request:
        reference = request["reference"]
        _fields(reference, (*REFERENCE_FIELDS, "image_point"), REFERENCE_FIELDS, "reference")
        for key in REFERENCE_FIELDS:
            value = reference[key]
            if not isinstance(value, str) or len(value) > 512 or not value.strip():
                raise ValueError(f"reference.{key} must contain 1..512 characters")
        normalized["reference"] = {key: reference[key].strip() for key in REFERENCE_FIELDS}
        if "image_point" in reference:
            normalized["reference"]["image_point"] = _image_point(reference["image_point"])
    if "trace" in request:
        if "reference" not in normalized:
            raise ValueError("Trace requires reference source, stage, region and landmark")
        trace = request["trace"]
        _fields(trace, ("kind", "edge", "face", "max_steps"), ("kind", "edge"), "trace")
        kind = trace["kind"]
        if kind not in KINDS:
            raise ValueError("trace.kind must be EDGE_LOOP, EDGE_RING or FACE_STRIP")
        if ("face" in trace) != (kind == "FACE_STRIP"):
            raise ValueError("trace.face is required only for FACE_STRIP")
        normalized["trace"] = {
            "kind": kind,
            "edge": _number(trace["edge"], 0, MAX_ELEMENTS - 1, int, "trace.edge"),
            "max_steps": _number(trace.get("max_steps", DEFAULT_STEPS), 1, MAX_STEPS, int, "trace.max_steps"),
        }
        if "face" in trace:
            normalized["trace"]["face"] = _number(trace["face"], 0, MAX_ELEMENTS - 1, int, "trace.face")
    if "scan" in request:
        _fields(request["scan"], SCAN_LIMITS, (), "scan")
        normalized["scan"] = {
            key: _number(request["scan"].get(key, default), low, high, kind, f"scan.{key}")
            for key, (default, low, high, kind) in SCAN_LIMITS.items()
        }
    if "mesh_sha256" in request:
        expected = request["mesh_sha256"]
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("mesh_sha256 must be a lowercase SHA-256")
        normalized["mesh_sha256"] = expected
    return normalized
