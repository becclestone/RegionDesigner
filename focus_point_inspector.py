"""Small popup for reviewing one already-fitted focus point (shift+click its
marker on the canvas - see canvas_view.SectionCanvas.focusPointShiftClicked)
and manually overriding whether it's used in a plane fit. The override this
sets is independent of, and takes priority over, the Global Focus Search's own
automatic RANSAC outlier flag (main_window._is_region_point_excluded /
_is_global_point_excluded) - so an operator can veto a false-positive flag, or
manually reject a point RANSAC considered fine (e.g. one that turns out to sit
on a hard-to-see fold or dust speck)."""
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton

import focus_fitting


class FocusPointInspectorDialog(QDialog):
    def __init__(
        self, fit: focus_fitting.FocusFit, z: float | None, currently_excluded: bool,
        plane_z: float | None = None, parent=None,
    ):
        """plane_z: the CURRENT plane fit's own Z prediction at this point's
        (row, col) - main_window.RegionDesignerWindow._on_focus_point_shift_clicked
        computes it via RegionPlaneFit.z_at, from whichever plane (this point's
        region, or the global one) was last fit. None if that plane hasn't been
        fit yet (or couldn't be, e.g. too few points)."""
        super().__init__(parent)
        self.setWindowTitle(f"Focus Point ({fit.row:.1f}, {fit.col:.1f})")
        self.resize(520, 480)
        # Toggled locally by _on_toggle; main_window reads this back once the
        # dialog closes rather than acting on it live, so closing without
        # touching the toggle is always a no-op.
        self.result_excluded = currently_excluded

        layout = QVBoxLayout(self)

        figure = Figure(figsize=(5, 3))
        canvas = FigureCanvasQTAgg(figure)
        layout.addWidget(canvas)
        ax = figure.add_subplot(111)
        ax.plot(fit.z, fit.normalized_f(), "o", label="measured (normalized)")
        if fit.params is not None:
            z_vals, curve_vals = fit.curve_preview()
            ax.plot(z_vals, curve_vals, "-", label="fit")
        z_opt = z if z is not None else fit.z_opt
        if z_opt is not None:
            ax.axvline(z_opt, color="red", linestyle="--", label="chosen Z")
        if plane_z is not None:
            # Drawn on the same measured-curve axis as chosen Z, rather than just
            # reported as a number, so the operator can see at a glance whether
            # the plane's prediction still lands near this point's own peak or
            # has drifted off it entirely (e.g. because it sits on a fold).
            ax.axvline(plane_z, color="tab:blue", linestyle=":", label="plane fit Z")
        ax.set_xlabel("Z (mm)")
        ax.set_ylabel("normalized focus metric")
        ax.legend()

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        if plane_z is not None and z_opt is not None:
            error = z_opt - plane_z
            self.error_label.setText(
                f"Plane fit error: {error:+.4f}mm  (chosen Z={z_opt:.4f}, plane predicts Z={plane_z:.4f})"
            )
        else:
            self.error_label.setText(
                "No current plane fit for this point yet - run Fit Region Plane (or Fit Global RANSAC "
                "Plane, for a global point) to compare this point's chosen Z against the plane."
            )
        layout.addWidget(self.error_label)

        self.status_label = QLabel()
        layout.addWidget(self.status_label)

        self.toggle_btn = QPushButton()
        self.toggle_btn.clicked.connect(self._on_toggle)
        layout.addWidget(self.toggle_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

        self._refresh_labels()

    def _refresh_labels(self):
        if self.result_excluded:
            self.status_label.setText("Currently EXCLUDED from plane fitting.")
            self.toggle_btn.setText("Include in plane fit")
        else:
            self.status_label.setText("Currently included in plane fitting.")
            self.toggle_btn.setText("Exclude from plane fit")

    def _on_toggle(self):
        self.result_excluded = not self.result_excluded
        self._refresh_labels()
