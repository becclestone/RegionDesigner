"""Sections -> regions -> per-region focus points.

Adapts the clustering approach in Scan_UI/San_Path_Planning/region_planner_V2.py
(get_even_clusters: KMeans + serpentine center-sort + balanced linear-sum-assignment)
rather than reimplementing it. Pure data in/out - no Qt or controller dependency.
"""
import numpy as np
from sklearn.cluster import KMeans
from scipy.spatial.distance import cdist
from scipy.optimize import linear_sum_assignment

Section = tuple[int, int]

# Physical aspect-ratio weighting: one row step is ~0.48mm, one col step ~0.18mm
# (Constants/project_constants.py cSECTION_HEIGHT/WIDTH_TO_OVERLAP_MM). Matches the
# 5:2 weighting already used in region_planner_V2.gen_region_path.
ROW_SCALE = 5.0
COL_SCALE = 2.0


def _scale(sections: np.ndarray) -> np.ndarray:
    return sections * (ROW_SCALE, COL_SCALE)


def _get_even_clusters(xy_scaled: np.ndarray, cluster_size: int) -> np.ndarray:
    """Direct port of region_planner_V2.get_even_clusters: balanced-size clusters,
    sorted into serpentine (boustrophedon) order across cluster centers."""
    n_clusters = int(np.ceil(len(xy_scaled) / cluster_size))
    kmeans = KMeans(n_clusters, n_init=10)
    kmeans.fit(xy_scaled)
    centers = kmeans.cluster_centers_

    centers_sorted = []
    row_buckets = (centers[:, 1] - centers[:, 1].min()) // (np.sqrt(cluster_size) * ROW_SCALE / COL_SCALE)
    for bucket in np.unique(row_buckets):
        bucket_inds = np.where(row_buckets == bucket)[0]
        bucket_centers = centers[bucket_inds]
        if int(bucket) % 2 == 0:
            bucket_centers = bucket_centers[bucket_centers[:, 0].argsort()]
        else:
            bucket_centers = bucket_centers[bucket_centers[:, 0].argsort()][::-1]
        centers_sorted.append(bucket_centers)
    centers = np.vstack(centers_sorted)

    centers = centers.reshape(-1, 1, xy_scaled.shape[-1]).repeat(cluster_size, 1).reshape(-1, xy_scaled.shape[-1])
    distance_matrix = cdist(xy_scaled, centers)
    clusters = linear_sum_assignment(distance_matrix)[1] // cluster_size
    return clusters


def assign_regions(sections: list[Section], target_region_size: int) -> dict[Section, int]:
    """Clusters painted sections into roughly-even-sized, contiguous regions."""
    if not sections:
        return {}

    xy = np.array(sections, dtype=float)
    xy_scaled = _scale(xy)
    cluster_size = max(1, min(target_region_size, len(sections)))
    labels = _get_even_clusters(xy_scaled, cluster_size)
    return {sections[i]: int(labels[i]) for i in range(len(sections))}


def place_focus_points(region_sections: list[Section], num_points: int) -> list[Section]:
    """Divides a region's area into num_points sub-areas (KMeans) and returns, for
    each sub-area, the actual painted section nearest its centroid."""
    if not region_sections:
        return []

    num_points = max(1, min(num_points, len(region_sections)))
    if num_points >= len(region_sections):
        return list(region_sections)

    xy = np.array(region_sections, dtype=float)
    xy_scaled = _scale(xy)

    kmeans = KMeans(n_clusters=num_points, n_init=10).fit(xy_scaled)

    used: set[int] = set()
    points: list[Section] = []
    for center in kmeans.cluster_centers_:
        dists = np.sum((xy_scaled - center) ** 2, axis=1)
        for idx in np.argsort(dists):
            idx = int(idx)
            if idx not in used:
                used.add(idx)
                points.append(region_sections[idx])
                break
    return points
