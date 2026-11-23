"""JSON selection contract, shared by Blender and host-side clients."""

DOMAINS = {"VERT": "verts", "EDGE": "edges", "FACE": "faces"}
SELECT_MODES = {"VERT": (True, False, False), "EDGE": (False, True, False), "FACE": (False, False, True)}
TYPE_DOMAIN = {
    "edge_loop": "EDGE", "edge_ring": "EDGE", "face_loop": "FACE",
    "vertex_loop": "VERT", "vertex_group": "VERT",
}
INDEX = {"type": "integer", "minimum": 0}
EDGE_SEED = {"oneOf": [INDEX, {"type": "array", "items": INDEX, "minItems": 2, "maxItems": 2, "uniqueItems": True}]}
FINGERPRINT = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
GROUP_NAME = {"type": "string", "minLength": 1}
FACE_DIRECTION = {
    "type": "object", "additionalProperties": False, "required": ["seed_face", "direction_edge"],
    "properties": {"seed_face": INDEX, "direction_edge": EDGE_SEED},
}
LOOP_KINDS = ("edge_loop", "edge_ring", "vertex_loop", "face_loop")
SAMPLING = {
    "type": "object", "additionalProperties": False, "required": ["start", "step"],
    "properties": {
        "start": INDEX, "toward": INDEX, "step": {"type": "integer", "minimum": 1},
        "phase": INDEX, "limit": {"type": "integer", "minimum": 1},
    },
}
POINT = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}


def _selector_shape(kind, fields, required=(), alternatives=()):
    domain_values = [TYPE_DOMAIN[kind]] if kind in TYPE_DOMAIN else list(DOMAINS)
    branch = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "kind": {"const": kind},
            "domain": {"enum": domain_values},
            "topology_sha256": FINGERPRINT,
            **fields,
        },
        "required": ["kind", *required],
    }
    if alternatives:
        branch["oneOf"] = [{"required": list(keys)} for keys in alternatives]
    return branch


_SEED_FIELDS = {
    "seed_edge": EDGE_SEED,
    "seed_edges": {"type": "array", "items": EDGE_SEED, "minItems": 1},
}
_SELECTORS = {
    "indices": _selector_shape("indices", {"indices": {"type": "array", "items": INDEX, "uniqueItems": True}},
                               required=("domain", "indices")),
    "current": _selector_shape("current", {}, required=("domain",)),
    "box": _selector_shape("box", {"space": {"enum": ["LOCAL", "WORLD"]}, "minimum": POINT, "maximum": POINT},
                           required=("domain", "space", "minimum", "maximum")),
    **{kind: _selector_shape(kind, {**_SEED_FIELDS, "sampling": SAMPLING}, alternatives=(("seed_edge",), ("seed_edges",)))
       for kind in ("edge_loop", "edge_ring", "vertex_loop")},
    "face_loop": _selector_shape(
        "face_loop", {**FACE_DIRECTION["properties"],
                      "seeds": {"type": "array", "items": FACE_DIRECTION, "minItems": 1}, "sampling": SAMPLING},
        alternatives=(("seed_face", "direction_edge"), ("seeds",)),
    ),
    "vertex_group": _selector_shape(
        "vertex_group", {"group": GROUP_NAME, "groups": {"type": "array", "items": GROUP_NAME, "minItems": 1},
                         "min_weight": {"type": "number", "minimum": 0, "maximum": 1}},
        alternatives=(("group",), ("groups",)),
    ),
}
def _public_shape(branch):
    # Emit complete alternatives so both draft-2020-12 validators and the compact
    # host-side validator enforce the same required fields and exclusive seed forms.
    if "oneOf" not in branch:
        return branch
    alternatives = [entry["required"] for entry in branch["oneOf"]]
    shared_keys = set(branch["properties"]).difference(*alternatives)
    return {"oneOf": [
        {"type": "object", "additionalProperties": False,
         "required": branch["required"] + fields,
         "properties": {k: v for k, v in branch["properties"].items() if k in shared_keys or k in fields}}
        for fields in alternatives
    ]}


SELECTOR_SCHEMA = {"oneOf": [_public_shape(branch) for branch in _SELECTORS.values()]}


