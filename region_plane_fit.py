"""Ports surface_calc's RANSAC plane fit across a region's confirmed focus points,
evaluated at every other section in the region, to get a smooth per-section Z.

See Scan_UI/surface_calc/surface_fitting.py:87-192 (Surface_Fitting.ransac_plane /
check_plane_error / get_z_from_plane / fill_focuses) for the original this ports -
same math, operating on plain (row, col, z) tuples instead of a dict of Section
objects built from a scan-record file.
"""
import numpy as np
from sklearn import linear_model


class RegionPlaneFit:
    def __init__(self, focus_points: list[tuple[float, float, float]]):
        """focus_points: [(row, col, z), ...] - user-confirmed focus values."""
        if len(focus_points) < 3:
            raise ValueError("Need at least 3 confirmed focus points to fit a plane.")
        self._xyz = np.array(focus_points, dtype=float)
        self.plane_coeff = self._fit()

    def _fit(self) -> np.ndarray:
        ransac = linear_model.RANSACRegressor()
        ransac.fit(self._xyz[:, 0:2], self._xyz[:, 2])
        a, b = ransac.estimator_.coef_
        d = ransac.estimator_.intercept_
        return np.array([a, b, d])

    def z_at(self, row: float, col: float) -> float:
        a, b, d = self.plane_coeff
        return row * a + col * b + d

    def residuals(self) -> list[float]:
        """Perpendicular distance of each input focus point from the fitted plane -
        diagnostic only, mirrors check_plane_error's printout."""
        a, b, d = self.plane_coeff
        denom = (a ** 2 + b ** 2 + 1) ** 0.5
        out = []
        for row, col, z in self._xyz:
            numerator = abs(-a * row - b * col + z - d)
            out.append(numerator / denom if denom else 0.0)
        return out

    def fill_sections(self, sections: list[tuple[int, int]]) -> dict[tuple[int, int], float]:
        """Final Z for every section in the region, including the focus points
        themselves (the original replaces even their own measured Z with the
        plane's fitted value, since the robust plane is more trustworthy than any
        single noisy point)."""
        return {(row, col): self.z_at(row, col) for row, col in sections}
