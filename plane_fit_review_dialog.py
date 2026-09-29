"""Multi-point plane-fit review, styled like focus_review_dialog.FocusReviewDialog's
own clickable point list: browse every point in a set (a region's, or the
global one) one at a time, see its focus curve plus how far the current plane
predicts its Z from what was actually chosen, and toggle whether it's used in
plane fitting. Selecting a point highlights its marker in red on the canvas
(see focus_point_item.FocusPointItem.set_highlighted) so it can be found on
the sample.

Kept free of any exclusion/plane-fitting logic of its own - all of that lives
in main_window and is supplied here via plain callables, since what "excluded"
or "the current plane" even means differs for a region point vs. a global one.
Non-modal (like FocusReviewDialog), so the canvas stays visible/interactive
alongside it while browsing."""
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem, QLabel, QPushButton


class PlaneFitReviewDialog(QDialog):
    def __init__(self, title: str, items: list, get_info, on_toggle, on_selection_changed, parent=None):
        """
        items: FocusPointItem list to review, in display order.
        get_info(item) -> dict: "label" (str, for the list entry), "excluded"
            (bool), and either "fit" (a focus_fitting.FocusFit, plus optional
            "z"/"plane_z") to plot, or "fit": None plus "unavailable" (str
            reason) if there's nothing to show yet.
        on_toggle(item): called when Include/Exclude is pressed for the
            current item - the caller applies the change (and should call
            refresh() afterward so the list/plot pick it up).
        on_selection_changed(item_or_None): called with the newly selected
            item, or None once nothing is selected or the dialog has closed -
            the caller uses this to highlight/un-highlight canvas markers.
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 650)
        self.items = items
        self._get_info = get_info
        self._on_toggle = on_toggle
        self._on_selection_changed = on_selection_changed

        root = QHBoxLayout(self)

        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self._on_row_changed)
        root.addWidget(self.list_widget, 1)

        right = QVBoxLayout()
        root.addLayout(right, 3)

        self.figure = Figure(figsize=(5, 3))
        self.canvas = FigureCanvasQTAgg(self.figure)
        right.addWidget(self.canvas)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        right.addWidget(self.error_label)

        self.toggle_btn = QPushButton()
        self.toggle_btn.clicked.connect(self._on_toggle_clicked)
        right.addWidget(self.toggle_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        right.addWidget(close_btn)

        self._populate_list()
        if self.items:
            self.list_widget.setCurrentRow(0)
        else:
            self._render(-1)

    def _populate_list(self):
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for item in self.items:
            self.list_widget.addItem(QListWidgetItem(self._get_info(item)["label"]))
        self.list_widget.blockSignals(False)

    def refresh(self):
        """Rebuilds every list label and redraws the current selection - call
        after a toggle, or after re-fitting the plane, so both stay current."""
        current_row = self.list_widget.currentRow()
        self._populate_list()
        if 0 <= current_row < len(self.items):
            self.list_widget.setCurrentRow(current_row)
            self._render(current_row)  # setCurrentRow(same row) won't re-fire currentRowChanged
        elif self.items:
            self.list_widget.setCurrentRow(0)
        else:
            self._render(-1)

    def current_item(self):
        row = self.list_widget.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def select_item(self, item):
        """Scrolls/selects the given item, if it's in this dialog's list - used
        so shift-clicking a marker opens straight to it. No-op otherwise."""
        try:
            row = self.items.index(item)
        except ValueError:
            return
        self.list_widget.setCurrentRow(row)

    def _on_row_changed(self, row: int):
        self._render(row)
        self._on_selection_changed(self.current_item())

    def _render(self, row: int):
        item = self.items[row] if 0 <= row < len(self.items) else None
        self.figure.clear()

        if item is None:
            self.canvas.draw_idle()
            self.error_label.setText("")
            self.toggle_btn.setEnabled(False)
            return

        info = self._get_info(item)
        fit = info.get("fit")
        if fit is None:
            self.canvas.draw_idle()
            self.error_label.setText(info.get("unavailable", "Nothing to show for this point yet."))
            self.toggle_btn.setEnabled(False)
            return

        ax = self.figure.add_subplot(111)
        ax.plot(fit.z, fit.normalized_f(), "o", label="measured (normalized)")
        if fit.params is not None:
            z_vals, curve_vals = fit.curve_preview()
            ax.plot(z_vals, curve_vals, "-", label="fit")
        z_opt = info.get("z")
        z_opt = z_opt if z_opt is not None else fit.z_opt
        if z_opt is not None:
            ax.axvline(z_opt, color="red", linestyle="--", label="chosen Z")
        plane_z = info.get("plane_z")
        if plane_z is not None:
            ax.axvline(plane_z, color="tab:blue", linestyle=":", label="plane fit Z")
        ax.set_xlabel("Z (mm)")
        ax.set_ylabel("normalized focus metric")
        ax.legend()
        self.canvas.draw_idle()

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

        self.toggle_btn.setEnabled(True)
        excluded = info.get("excluded", False)
        self.toggle_btn.setText("Include in plane fit" if excluded else "Exclude from plane fit")

    def _on_toggle_clicked(self):
        item = self.current_item()
        if item is not None:
            self._on_toggle(item)

    def closeEvent(self, event):
        self._on_selection_changed(None)
        super().closeEvent(event)
