"""Ports surface_calc's per-point curve fit + candidate-peak selection, and its
region-level averaging step, to operate on raw (z_values, metric_values) arrays
instead of file-parsed Section objects fed by parse_scan_record.

See Scan_UI/surface_calc/surface_fitting.py:10-84 (Focus_Fitting) and :97-110
(Surface_Fitting.get_valid_focuses) for the originals this is a direct port of -
same math, same two-Lorentzian mixture model, same "is_right" ambiguity-resolution
heuristic. Kept free of Qt/plotting so it's independently testable.
"""
import numpy as np
from scipy.optimize import curve_fit


def _lorentzian(z, amp, cen, wid):
    return amp / (1 + ((z - cen) ** 2) * wid)


def _mixture_model(z, a1, c1, w1, a2, c2, w2):
    return _lorentzian(z, a1, c1, w1) + _lorentzian(z, a2, c2, w2)


class FocusFit:
    """One point's fitted curve. z_opt is provisional until finalize_region() runs:
    an 'is_right' point's z_opt isn't set by fit() alone - it needs the region's
    average peak_offset from the resolved ('not is_right') points first."""

    def __init__(self, row: float, col: float, z_values, metric_values):
        self.row = row
        self.col = col
        self.z = np.asarray(z_values, dtype=float)
        self.f = np.asarray(metric_values, dtype=float)

        self.max_loc = float(self.z[np.argmax(self.f)])
        self.params = None
        self.peak_locs = None
        self.is_right = False
        self.peak_offset = None
        self.z_opt = None

    def fit(self):
        temp_f = (self.f - self.f.min()) / (self.f.max() - self.f.min()) + 0.3
        init_params = [0.9, self.z[int(len(self.z) * 0.6)], 1e5,
                        0.9, self.z[int(len(self.z) * 0.4)], 1e5]
        try:
            popt, _ = curve_fit(_mixture_model, self.z, temp_f, p0=init_params, maxfev=100000)
        except RuntimeError:
            popt = init_params

        self.params = popt
        self.peak_locs = [min(popt[1], popt[4]), max(popt[1], popt[4])]
        self._check_focus_preference()

    def _check_focus_preference(self):
        if abs(self.peak_locs[0] - self.peak_locs[1]) < 0.4:
            self.peak_offset = 0.0
            self.z_opt = self.max_loc
            self.is_right = False
        elif self.max_loc > (self.peak_locs[0] + self.peak_locs[1]) / 2:
            self.is_right = True
        else:
            self.peak_offset = abs(self.max_loc - self.peak_locs[0])
            self.z_opt = self.max_loc
            self.is_right = False

    def curve_preview(self, num_points: int = 500):
        """z/metric samples of the fitted mixture model curve, for plotting."""
        z_vals = np.linspace(self.z[0], self.z[-1], num_points)
        return z_vals, _mixture_model(z_vals, *self.params)


def finalize_region(fits: list[FocusFit]) -> None:
    """Ports Surface_Fitting.get_valid_focuses(): resolves any 'is_right' point's
    z_opt using the average peak_offset of the region's resolved points."""
    resolved = [f for f in fits if not f.is_right]
    if not resolved:
        return
    avg_peak_offset = sum(f.peak_offset for f in resolved) / len(resolved)
    for f in fits:
        if f.is_right:
            f.z_opt = f.peak_locs[0] + avg_peak_offset
