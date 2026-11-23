"""Measure two static Blender bodies along selected world-axis lines.

Oppositely directed edge pairs establish edge closure only. The measured lines
give local interval evidence. Review assembly intent against actual sections.
"""

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path


EPS_MM = 0.00001
OBJ_ROUNDING_MM = 0.00051


class Unsupported(ValueError):
    """An input is outside the evaluated OBJ contract."""


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_geometry(receipt_path, names):
    receipt = read_json(receipt_path)
    if not isinstance(receipt, dict) or receipt.get("command") != "exchange.export" or receipt.get("ok") is not True:
        raise Unsupported("需要成功的公共 exchange.export 回执。")
    report = receipt.get("data")
    settings = report.get("settings") if isinstance(report, dict) else None
    if not isinstance(settings, dict):
        raise Unsupported("需要公共导出的完整 JSON 回执，压缩摘要缺少导出设置。")
    if (
        report["source_unchanged"] is not True
        or (report["format"], report["mode"]) != ("OBJ", "STATIC")
        or settings["frame_start"] != settings["frame_end"]
        or len(settings["objects"]) != 2
        or set(settings["objects"]) != set(names)
    ):
        raise Unsupported("需要成功的双对象、单帧 STATIC OBJ 公共导出回执，且两个名称与清单完全一致。")

    artifacts = receipt["artifacts"]
    obj_path = receipt_path.parent / artifacts["export.obj"]
    native_path = receipt_path.parent / artifacts["exchange-before.json"]
    output, = report["outputs"]
    if file_sha256(obj_path) != output["sha256"]:
        raise Unsupported("OBJ 的 SHA-256 与公共回执不符。")
    native, = read_json(native_path)
    if native["frame"] != settings["frame_start"]:
        raise Unsupported("求值记录的帧与静态导出回执不符。")
    native_vertices = [tuple(float(value) * 1000 for value in row) for row in native["vertices"]]

    obj_names = {name.replace(" ", "_"): name for name in names}
    if len(obj_names) != 2:
        raise Unsupported("两个对象名在 OBJ 中归一为同一名称。")
    faces = {}
    vertex_index = 0
    max_delta = 0.0
    with obj_path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            fields = line.split()
            if not fields:
                continue
            tag, *values = fields
            if tag == "o":
                name = obj_names[" ".join(values)]
                if name in faces:
                    raise Unsupported("OBJ 出现重复对象块。")
                faces[name] = []
            elif tag == "v":
                x, up, back = map(float, values)
                nx, ny, nz = native_vertices[vertex_index]
                coordinates = (1000 * x, -1000 * back, 1000 * up, nx, ny, nz)
                if not all(map(math.isfinite, coordinates)):
                    raise Unsupported("几何包含非有限坐标。")
                component_deltas = (abs(1000 * x - nx), abs(-1000 * back - ny), abs(1000 * up - nz))
                max_delta = max(max_delta, *component_deltas)
                vertex_index += 1
            elif tag == "f":
                if len(values) != 3 or not faces:
                    raise Unsupported("OBJ 必须在具名对象块中提供三角面。")
                faces[name].append(tuple(int(value.split("/", 1)[0]) - 1 for value in values))

    if vertex_index != len(native_vertices) or max_delta > OBJ_ROUNDING_MM:
        raise Unsupported("OBJ 的顶点顺序、轴向或尺度与公共导出前记录不符。")
    if set(faces) != set(names) or {obj["name"] for obj in native["objects"]} != set(names):
        raise Unsupported("OBJ 对象块与导出前的对象记录不一致。")

    start = 0
    geometry = {}
    for obj in native["objects"]:
        name = obj["name"]
        stop = start + obj["vertices"]
        indices = faces[name]
        if len(indices) != obj["triangles"] or any(
            index < start or index >= stop for triangle in indices for index in triangle
        ):
            raise Unsupported(f"对象 {name} 的三角面或顶点索引偏移与导出前记录不符。")
        geometry[name] = (
            [tuple(native_vertices[index] for index in triangle) for triangle in indices]
            if closed_oriented_edges(indices)
            else None
        )
        start = stop
    if start != vertex_index:
        raise Unsupported("导出对象的顶点总数不一致。")
    return receipt, native["frame"], geometry, max_delta


def closed_oriented_edges(triangles):
    edges = {}
    for a, b, c in triangles:
        if a == b or b == c or c == a:
            return False
        for first, second in ((a, b), (b, c), (c, a)):
            edge = min(first, second), max(first, second)
            count, direction = edges.get(edge, (0, 0))
            edges[edge] = count + 1, direction + (1 if first < second else -1)
    return bool(edges) and all(value == (2, 0) for value in edges.values())


def segment_distance(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    px, py = point[0] - start[0], point[1] - start[1]
    length_squared = dx * dx + dy * dy
    t = min(1.0, max(0.0, (px * dx + py * dy) / length_squared)) if length_squared else 0.0
    return math.hypot(px - t * dx, py - t * dy)


def crossings(triangles, point, axis):
    if triangles is None:
        return {"status": "unsupported_nonclosed_or_inconsistently_oriented", "crossings_mm": []}

    u, v = (axis + 1) % 3, (axis + 2) % 3
    pu, pv = point[u], point[v]
    hits = []
    ambiguous = []
    for a, b, c in triangles:
        if not (
            min(a[u], b[u], c[u]) - EPS_MM <= pu <= max(a[u], b[u], c[u]) + EPS_MM
            and min(a[v], b[v], c[v]) - EPS_MM <= pv <= max(a[v], b[v], c[v]) + EPS_MM
        ):
            continue
        dx, dy = b[u] - a[u], b[v] - a[v]
        ex, ey = c[u] - a[u], c[v] - a[v]
        determinant = dx * ey - dy * ex
        opposite_lengths = math.hypot(ex - dx, ey - dy), math.hypot(ex, ey), math.hypot(dx, dy)
        if abs(determinant) <= EPS_MM * max(opposite_lengths):
            projected = [(vertex[u], vertex[v]) for vertex in (a, b, c)]
            if any(
                segment_distance((pu, pv), projected[i], projected[(i + 1) % 3]) <= 2 * EPS_MM
                for i in range(3)
            ):
                ambiguous.append((min(a[axis], b[axis], c[axis]), max(a[axis], b[axis], c[axis])))
            continue

        px, py = pu - a[u], pv - a[v]
        wb = (px * ey - py * ex) / determinant
        wc = (dx * py - dy * px) / determinant
        wa = 1 - wb - wc
        edge_distance = min(
            weight * abs(determinant) / length
            for weight, length in zip((wa, wb, wc), opposite_lengths)
        )
        if edge_distance < -EPS_MM:
            continue
        position = wa * a[axis] + wb * b[axis] + wc * c[axis]
        if edge_distance <= EPS_MM:
            ambiguous.append((position, position))
            continue
        hits.append((position, 1 if determinant > 0 else -1))

    hits.sort()
    heights = [position for position, _ in hits]
    result = {"status": "no_hit", "crossings_mm": heights}
    if ambiguous:
        result.update(
            status="unresolved_tangent_edge_or_degenerate_projection",
            ambiguous_axis_range_mm=[min(span[0] for span in ambiguous), max(span[1] for span in ambiguous)],
        )
    elif len(heights) % 2:
        result["status"] = "unresolved_odd_crossings"
    elif any(
        next_height - height <= EPS_MM or next_direction == direction
        for (height, direction), (next_height, next_direction) in zip(hits, hits[1:])
    ):
        result["status"] = "unresolved_crossing_order"
    elif heights:
        result.update(status="measured", intervals_mm=list(zip(heights[::2], heights[1::2])))
    return result


def covers(outer, inner):
    return all(
        any(low - EPS_MM <= a and b <= high + EPS_MM for low, high in outer)
        for a, b in inner
    )


def compare(first, second):
    statuses = first["status"], second["status"]
    if any(status not in ("measured", "no_hit") for status in statuses):
        return {"relation": "inconclusive"}
    if "no_hit" in statuses:
        return {"relation": "no_hit"}

    a_intervals, b_intervals = first["intervals_mm"], second["intervals_mm"]
    overlap = []
    contact = []
    gaps = []
    for (a_low, a_high) in a_intervals:
        for (b_low, b_high) in b_intervals:
            low, high = max(a_low, b_low), min(a_high, b_high)
            if high > low + EPS_MM:
                overlap.append([low, high])
            elif low > high + EPS_MM:
                gaps.append((low - high, [high, low]))
            else:
                contact.append(sorted((low, high)))

    if overlap:
        a_covers_b = covers(a_intervals, b_intervals)
        b_covers_a = covers(b_intervals, a_intervals)
        relation = "overlap"
        if a_covers_b and b_covers_a:
            relation = "coincident_intervals_within_tolerance"
        elif a_covers_b:
            relation = "a_contains_b_on_line"
        elif b_covers_a:
            relation = "b_contains_a_on_line"
    elif contact:
        relation = "contact_or_within_tolerance"
    else:
        relation = "separated"

    widths = [high - low for low, high in overlap]
    result = {
        "relation": relation,
        "overlap_intervals_mm": overlap,
        "max_overlap_mm": max(widths, default=0.0),
        "total_overlap_mm": sum(widths),
    }
    if relation == "separated":
        result["nearest_gap_mm"], result["nearest_gap_range_mm"] = min(gaps)
    elif overlap:
        result["nearest_gap_mm"] = 0.0
    if contact:
        result["contact_candidate_ranges_mm"] = contact
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("receipt", type=Path, help="Public blenderctl exchange export result.json")
    parser.add_argument("--object-a", required=True)
    parser.add_argument("--object-b", required=True)
    parser.add_argument("--axis", choices=("X", "Y", "Z"), required=True)
    parser.add_argument(
        "--point", metavar=("X", "Y", "Z"), nargs=3, type=float, action="append", required=True,
        help="World millimetres. Repeats allowed. The coordinate on the chosen axis is ignored.",
    )
    args = parser.parse_args()
    try:
        names = args.object_a, args.object_b
        if names[0] == names[1] or not all(math.isfinite(value) for point in args.point for value in point):
            raise Unsupported("两个对象名须不同，测点坐标须为有限数值。")
        receipt, frame, geometry, delta = read_geometry(args.receipt, names)
        axis = "XYZ".index(args.axis)
        samples = []
        for point in args.point:
            first, second = (crossings(geometry[name], point, axis) for name in names)
            samples.append({"point_mm": point, "a": first, "b": second, **compare(first, second)})
        result = {
            "status": "ok", "object_a": names[0], "object_b": names[1], "frame": frame,
            "axis": args.axis, "tolerance_mm": EPS_MM, "source_sha256": receipt["data"]["source_sha256"],
            "export_job_id": receipt["job_id"], "obj_coordinate_delta_mm": delta, "samples": samples,
        }
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        result = {"status": "unsupported_input", "error": str(error)}
        print(str(error), file=sys.stderr)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
