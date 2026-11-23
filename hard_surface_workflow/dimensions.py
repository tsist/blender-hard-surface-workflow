"""Dimensional context for tools that operate in source mesh coordinates."""

import numpy as np


def local_scale_report(obj):
    # Singular values include shear from a rotated child of a scaled parent.
    scales = np.linalg.svd(np.asarray(obj.matrix_world.to_3x3()), compute_uv=False)
    low, high = float(scales[-1]), float(scales[0])
    return {"parameter_space": "LOCAL", "world_scale_range": [low, high],
            "nonuniform": low <= 0 or high - low > high * 1e-6}
