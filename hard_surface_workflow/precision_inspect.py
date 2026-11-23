"""Local bore sections, bounded through tests and wall-normal measurements in world millimetres."""

import math

import numpy as np
from mathutils.bvhtree import BVHTree


EPS = 1e-5
MAX_TRIANGLE_RAY_TESTS = 24_000_000


def _scalar(value, label, *, positive=False):
    if type(value) not in (float, int) or not -1e9 <= value <= 1e9 or (positive and value <= EPS):
        raise ValueError(f"{label} must be a finite millimetre value in range")
    return float(value)


def _vector(value, label):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{label} must be an xyz vector")
    return [_scalar(axis, label) for axis in value]


def parse(measurements, names):
    if not isinstance(measurements, list) or len(measurements) > 32:
        raise ValueError("measurements must be an array of at most 32 bore or wall requests")
    result = []
    for item in measurements:
        keys = {"kind", "object", "point_mm"}
        if not isinstance(item, dict) or not keys <= item.keys() or item["object"] not in names:
            raise ValueError("Each measurement needs a kind, a listed object and point_mm")
        normalized = {**item, "point_mm": _vector(item["point_mm"], "point_mm"), "representations": {}}
        if item["kind"] == "BORE":
            keys |= {"direction", "stations_mm", "max_radius_mm", "axial_window_mm"}
            if not keys <= item.keys() or item.keys() - keys - {"angular_samples", "through_radius_mm"}:
                raise ValueError("BORE needs direction, stations_mm, max_radius_mm and axial_window_mm")
            direction = np.array(_vector(item["direction"], "direction"))
            length = np.linalg.norm(direction)
            if length <= 1e-12:
                raise ValueError("BORE direction must be nonzero")
            maximum = _scalar(item["max_radius_mm"], "max_radius_mm", positive=True)
            width = item.get("angular_samples", 16)
            radius = _scalar(item.get("through_radius_mm", 0), "through_radius_mm")
            stations, window = item["stations_mm"], item["axial_window_mm"]
            if (not isinstance(stations, list) or not 1 <= len(stations) <= 16
                    or not isinstance(window, list) or len(window) != 2):
                raise ValueError("BORE needs 1–16 stations and a two-value axial window")
            stations = [_scalar(value, "stations_mm") for value in stations]
            start, end = (_scalar(value, "axial_window_mm") for value in window)
            if (stations != sorted(set(stations)) or start >= end or not all(start < s < end for s in stations)
                    or type(width) is not int or not 8 <= width <= 64 or width % 2 or not 0 <= radius < maximum):
                raise ValueError("BORE requires ordered interior stations, 8–64 even angular samples and a bounded radius")
            normalized.update(direction=(direction / length).tolist(), stations_mm=stations,
                              max_radius_mm=maximum, axial_window_mm=[start, end],
                              angular_samples=width, through_radius_mm=radius)
        elif item["kind"] == "WALL":
            if item.keys() != keys | {"max_search_mm", "max_thickness_mm"}:
                raise ValueError("WALL needs max_search_mm and max_thickness_mm")
            for field in ("max_search_mm", "max_thickness_mm"):
                normalized[field] = _scalar(item[field], field, positive=True)
        else:
            raise ValueError("Unknown measurement kind (expected BORE or WALL)")
        result.append(normalized)
    return result


class _Solid:
    def __init__(self, points, triangles):
        self.points = points
        self.triangles = triangles
        self.faces = points[triangles]
        self.first = self.faces[:, 0]
        self.edge_a = self.faces[:, 1] - self.first
        self.edge_b = self.faces[:, 2] - self.first
        raw_normals = np.cross(self.edge_a, self.edge_b)
        self.area2 = np.linalg.norm(raw_normals, axis=1)
        self.normals = raw_normals / np.where(self.area2 > 0, self.area2, 1)[:, None]
        self.tree = None
        self.tree_origin = points[0]

    def line(self, origin, direction):
        """Get oriented surface hits, collapsing coincident hits on triangulation seams."""
        det = -np.einsum("ij,j->i", self.normals, direction)
        transversal = np.abs(det) > 1e-12
        offset = origin - self.first
        coplanar = ~transversal & (np.abs(np.einsum("ij,ij->i", offset, self.normals)) <= EPS)
        if coplanar.any():
            side = np.cross(self.normals[coplanar], direction)
            distances = np.einsum("ijk,ik->ij", self.faces[coplanar] - origin, side)
            if np.any((distances.min(axis=1) <= EPS) & (distances.max(axis=1) >= -EPS)):
                return None
        h = np.cross(direction, self.edge_b)
        inv = 1 / np.where(transversal, det * self.area2, 1.)
        cross = np.cross(offset, self.edge_a)
        u = inv * np.einsum("ij,ij->i", offset, h)
        v = inv * np.einsum("ij,j->i", cross, direction)
        inside = transversal & (np.minimum(np.minimum(u, v), 1 - u - v) >= -1e-9)
        indices = np.flatnonzero(inside)
        values = (inv * np.einsum("ij,ij->i", self.edge_b, cross))[indices]
        if not len(indices):
            return []
        order = values.argsort()
        distances = values[order]
        signs = np.sign(det[indices[order]])
        grouped = []
        split = np.flatnonzero(np.diff(distances) > EPS) + 1
        for group in np.split(np.arange(len(order)), split):
            if np.any(signs[group] != (1 if len(grouped) % 2 == 0 else -1)):
                return None
            grouped.append(float(distances[group].mean()))
        return np.reshape(grouped, (-1, 2)).tolist() if len(grouped) % 2 == 0 else None

    def nearest(self, point, maximum):
        if self.tree is None:
            relative = self.points - self.tree_origin
            self.tree = BVHTree.FromPolygons(relative.tolist(), self.triangles.tolist(), all_triangles=True)
        query = tuple(point - self.tree_origin)
        anchor, _, index, distance = self.tree.find_nearest(query, maximum)
        if anchor is None or distance > maximum:
            return None, "surface_not_found"
        neighbors = self.tree.find_nearest_range(query, distance + EPS)
        normal = self.normals[index]
        if any(np.dot(normal, self.normals[other]) < 1 - 1e-6
               or (np.linalg.norm(np.asarray(other_point) - np.asarray(anchor)) > 2 * EPS)
               for other_point, _, other, other_distance in neighbors if abs(other_distance - distance) <= EPS):
            return None, "ambiguous_surface_normal"
        return (np.asarray(anchor) + self.tree_origin, normal, index, distance), "ok"


def _radial_distances(solid, seed, bearings, maximum):
    distances = []
    for bearing in bearings:
        intervals = solid.line(seed, bearing)
        if intervals is None:
            return None, "ambiguous_radial_boundary"
        if any(start <= EPS and end >= -EPS for start, end in intervals):
            return None, "seed_in_material_or_on_boundary"
        entries = [start for start, _ in intervals if EPS < start <= maximum + EPS]
        if not entries:
            return None, "radial_boundary_outside_search"
        distances.append(entries[0])
    return np.asarray(distances), "measured"


def _section(solid, seed, bearings, maximum):
    distances, status = _radial_distances(solid, seed, bearings, maximum)
    result = {"seed_mm": seed.tolist()}
    if distances is None:
        return {**result, "status": status}
    half = len(bearings) // 2
    forward, reverse = np.split(distances, 2)
    offset, _, _, _ = np.linalg.lstsq(bearings[:half], (forward - reverse) / 2, rcond=None)
    center = seed + offset
    result.update(center_estimate_mm=center.tolist(), sampled_min_seed_clearance_diameter_mm=2 * float(distances.min()),
                  seed_radial_distances_mm=distances.tolist())
    if np.linalg.norm(offset) > EPS:
        distances, status = _radial_distances(solid, center, bearings, maximum)
        if distances is None:
            return {**result, "status": "estimated_center_unverified", "reason": status}
    forward, reverse = np.split(distances, 2)
    return {**result, "status": "measured", "sampled_min_opposed_chord_mm": float(np.min(forward + reverse)),
            "sampled_min_center_clearance_diameter_mm": 2 * float(distances.min()),
            "center_radial_distances_mm": distances.tolist()}


def _axis_paths(solid, point, normal, bearings, radius, window):
    start, end = window
    offsets = [np.zeros(3)]
    if radius:
        offsets.extend(bearings * radius)
    observations = []
    for offset in offsets:
        hits = solid.line(point + offset, normal)
        if hits is None:
            observations.append({"offset_mm": offset.tolist(), "status": "ambiguous_boundary"})
            continue
        walls = [[max(a, start), min(b, end)] for a, b in hits if min(b, end) - max(a, start) > EPS]
        observations.append({"offset_mm": offset.tolist(), "status": "blocked" if walls else "clear",
                             "remaining_material_intervals_mm": walls,
                             "remaining_material_mm": sum(b - a for a, b in walls)})
    blocked = [row for row in observations if row["status"] == "blocked"]
    unknown = [row for row in observations if row["status"] == "ambiguous_boundary"]
    status = "blocked_in_samples" if blocked else "unverified_boundary" if unknown else "clear_on_samples"
    return {"status": status, "ray_count": len(observations), "blocked_rays": len(blocked),
            "ambiguous_rays": len(unknown), "center_ray": observations[0],
            "maximum_sampled_remaining_material_mm": max((row["remaining_material_mm"] for row in observations
                                                          if row["status"] != "ambiguous_boundary"), default=None),
            "other_blocked_or_ambiguous_rays": [row for row in observations[1:] if row["status"] != "clear"]}


def _bore(solid, item):
    normal = np.array(item["direction"])
    guide = np.eye(3)[np.argmin(np.abs(normal))]
    first = np.cross(normal, guide)
    first /= np.linalg.norm(first)
    second = np.cross(normal, first)
    theta = math.tau * np.arange(item["angular_samples"]) / item["angular_samples"]
    bearings = np.cos(theta)[:, None] * first + np.sin(theta)[:, None] * second
    point = np.array(item["point_mm"])
    sections = [{"station_mm": station, **_section(solid, point + station * normal, bearings, item["max_radius_mm"])}
                for station in item["stations_mm"]]
    path = _axis_paths(solid, point, normal, bearings, item["through_radius_mm"], item["axial_window_mm"])
    bounded = [row for row in sections if row["status"] == "measured"]
    status = "measured" if len(bounded) == len(sections) else "section_unverified"
    if status != "measured" and path["status"] == "clear_on_samples":
        path["status"] = "clear_rays_bore_unverified"
    return {"status": status, "sections": sections, "axial_clearance": path,
            "angular_zero_direction": first.tolist(), "angular_quarter_turn_direction": second.tolist()}


def _wall(solid, item):
    nearest, status = solid.nearest(np.array(item["point_mm"]), item["max_search_mm"])
    if nearest is None:
        return {"status": status}
    surface, normal, index, offset = nearest
    result = {"surface_point_mm": surface.tolist(), "outward_normal": normal.tolist(),
              "triangle": index, "snap_distance_mm": offset}
    intervals = solid.line(surface, -normal)
    if intervals is None:
        return {**result, "status": "ambiguous_normal_ray"}
    local = [end for start, end in intervals if abs(start) <= 4 * EPS and end > EPS]
    if len(local) != 1:
        return {**result, "status": "inward_ray_not_anchored"}
    if local[0] > item["max_thickness_mm"] + EPS:
        return {**result, "status": "opposite_wall_outside_search"}
    return {**result, "status": "measured", "thickness_mm": local[0],
            "opposite_point_mm": (surface - local[0] * normal).tolist()}


def measure(points, triangles, closed, items):
    if not closed:
        return [{"status": "open_or_nonmanifold"} for _ in items]
    solid = _Solid(points, triangles)
    if np.any(solid.area2 <= 1e-14):
        return [{"status": "degenerate_triangles"} for _ in items]
    results = []
    budget = MAX_TRIANGLE_RAY_TESTS
    for item in items:
        if item["kind"] == "WALL":
            rays = 1
        else:
            angular = item["angular_samples"]
            rays = 2 * len(item["stations_mm"]) * angular + 1 + (angular if item["through_radius_mm"] else 0)
        cost = rays * len(triangles)
        if cost > budget:
            results.append({"status": "unverified_measurement_budget", "requested_triangle_ray_tests": cost})
            continue
        budget -= cost
        results.append(_wall(solid, item) if item["kind"] == "WALL" else _bore(solid, item))
    return results
