"""Ports surface_calc's per-point curve fit to operate on raw (z_values,
metric_values) arrays instead of file-parsed Section objects fed by
parse_scan_record.

See Scan_UI/surface_calc/surface_fitting.py:10-84 (Focus_Fitting) for the
original this is a port of - same math, same two-Lorentzian mixture model.
Unlike the original (and unlike this module's own earlier version), the
picked focus is always the left/near peak (get_peaks()[0]) - no is_right
ambiguity heuristic or region-level averaging - and the max-sharpness sample
is used only as a fallback for when the curve fit itself fails. Kept free of
Qt/plotting so it's independently testable.
"""
import numpy as np
from scipy.optimize import curve_fit


def _lorentzian(z, amp, cen, wid):
    return amp / (1 + ((z - cen) ** 2) * wid)


def _mixture_model(z, a1, c1, w1, a2, c2, w2):
    return _lorentzian(z, a1, c1, w1) + _lorentzian(z, a2, c2, w2)


class FocusFit:
    """One point's fitted curve. z_opt is the left (near) peak of the fitted
    two-Lorentzian mixture, or max_loc if the curve fit failed."""

    def __init__(self, row: float, col: float, z_values, metric_values):
        self.row = row
        self.col = col
        self.z = np.asarray(z_values, dtype=float)
        self.f = np.asarray(metric_values, dtype=float)

        self.max_loc = float(self.z[np.argmax(self.f)])
        self.params = None
        self.peak_locs = None
        self.z_opt = None

    def normalized_f(self):
        """f rescaled into the same [0.3, 1.3]-ish range the fit is performed in,
        so measured points and the fitted curve can be plotted on one axis."""
        return (self.f - self.f.min()) / (self.f.max() - self.f.min()) + 0.3

    def fit(self):
        temp_f = self.normalized_f()
        init_params = [0.9, self.z[int(len(self.z) * 0.6)], 1e5,
                        0.9, self.z[int(len(self.z) * 0.4)], 1e5]
        try:
            popt, _ = curve_fit(_mixture_model, self.z, temp_f, p0=init_params, maxfev=100000)
        except RuntimeError:
            # Curve fit failed to converge - fall back to the raw max-sharpness
            # sample rather than trusting an unconverged/init-guess curve.
            self.params = None
            self.peak_locs = None
            self.z_opt = self.max_loc
            return

        self.params = popt
        self.peak_locs = self.get_peaks()
        self.z_opt = self.peak_locs[0]

    def get_peaks(self):
        return [min(self.params[1], self.params[4]), max(self.params[1], self.params[4])]

    def curve_preview(self, num_points: int = 500):
        """z/metric samples of the fitted mixture model curve, for plotting."""
        z_vals = np.linspace(self.z[0], self.z[-1], num_points)
        return z_vals, _mixture_model(z_vals, *self.params)

    def to_dict(self) -> dict:
        """JSON-serializable snapshot of the fine stage's sweep + fit, for
        autofocus_log.py. Only meaningful once fit() has already run."""
        return {
            "z_values": self.z.tolist(),
            "metric_values": self.f.tolist(),
            "params": [float(x) for x in self.params] if self.params is not None else None,
            "peak_locs": self.peak_locs,
            "max_loc": self.max_loc,
            "z_opt": self.z_opt,
        }
