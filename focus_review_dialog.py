"""Per-region review dialog: shows each focus point's fitted curve, lets the user
confirm a Z via a real 3-capture comparison scan (or override manually), and won't
hand back results until every point has an assigned Z.
"""
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
import tifffile
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem, QLabel,
    QPushButton, QWidget, QMessageBox, QDoubleSpinBox, QGroupBox
)

import focus_fitting
import image_utils
from confirmation_scan import ConfirmationScanWorker


class FocusReviewDialog(QDialog):
    def __init__(self, bridge, region_id: int, fits: list[focus_fitting.FocusFit], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Review Focus Points - Region {region_id}")
        self.resize(1000, 650)
        self.bridge = bridge
        self.fits = fits
        self.confirmed_z: dict[int, float] = {}
        self._scan_worker = None

        self._build_ui()
        self._populate_list()
        if self.fits:
            self.list_widget.setCurrentRow(0)

    def _build_ui(self):
        root = QHBoxLayout(self)

        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self._on_point_selected)
        root.addWidget(self.list_widget, 1)

        right = QVBoxLayout()
        root.addLayout(right, 3)

        self.figure = Figure(figsize=(5, 3))
        self.canvas = FigureCanvasQTAgg(self.figure)
        right.addWidget(self.canvas)

        self.z_label = QLabel("")
        right.addWidget(self.z_label)

        self.confirm_btn = QPushButton("Confirm via Scan (Z, Z-1µm, Z+1µm)")
        self.confirm_btn.clicked.connect(self._on_confirm_via_scan)
        right.addWidget(self.confirm_btn)

        self.capture_row = QHBoxLayout()
        right.addLayout(self.capture_row)

        manual_box = QGroupBox("Manual override")
        manual_layout = QHBoxLayout(manual_box)
        self.manual_z_spin = QDoubleSpinBox()
        self.manual_z_spin.setDecimals(4)
        self.manual_z_spin.setRange(-10.0, 10.0)
        self.manual_z_spin.setSingleStep(0.001)
        manual_layout.addWidget(self.manual_z_spin)
        manual_apply_btn = QPushButton("Use this Z")
        manual_apply_btn.clicked.connect(self._on_manual_apply)
        manual_layout.addWidget(manual_apply_btn)
        right.addWidget(manual_box)

        self.accept_btn = QPushButton("Done Reviewing This Region")
        self.accept_btn.clicked.connect(self._on_accept)
        right.addWidget(self.accept_btn)

    def _populate_list(self):
        for i in range(len(self.fits)):
            self.list_widget.addItem(QListWidgetItem(self._label_for(i)))

    def _label_for(self, index: int) -> str:
        fit = self.fits[index]
        mark = "OK" if index in self.confirmed_z else "..."
        return f"[{mark}] ({fit.row:.1f}, {fit.col:.1f})"

    def _refresh_list_labels(self):
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setText(self._label_for(i))

    def _on_point_selected(self, row: int):
        self._clear_capture_row()
        if row < 0 or row >= len(self.fits):
            return
        fit = self.fits[row]

        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.plot(fit.z, fit.f, "o", label="measured")
        if fit.params is not None:
            z_vals, curve_vals = fit.curve_preview()
            ax.plot(z_vals, curve_vals, "-", label="fit")
        z_opt = self.confirmed_z.get(row, fit.z_opt)
        if z_opt is not None:
            ax.axvline(z_opt, color="red", linestyle="--", label="chosen Z")
        ax.set_xlabel("Z (mm)")
        ax.set_ylabel("focus metric")
        ax.legend()
        self.canvas.draw_idle()

        if z_opt is None:
            self.z_label.setText("No Z available")
        else:
            note = " (confirmed via scan)" if row in self.confirmed_z else " (algorithm pick, not yet confirmed)"
            self.z_label.setText(f"Chosen Z = {z_opt:.4f}{note}")
            self.manual_z_spin.setValue(z_opt)

    def _clear_capture_row(self):
        while self.capture_row.count():
            item = self.capture_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _on_confirm_via_scan(self):
        index = self.list_widget.currentRow()
        if index < 0:
            return
        fit = self.fits[index]
        chosen_z = self.confirmed_z.get(index, fit.z_opt)
        if chosen_z is None:
            QMessageBox.warning(self, "Confirm via Scan", "No candidate Z to confirm yet.")
            return

        self.confirm_btn.setEnabled(False)
        self._clear_capture_row()

        worker = ConfirmationScanWorker(self.bridge, row=int(round(fit.row)), col=int(round(fit.col)),
                                         chosen_z=chosen_z)
        worker.captureReady.connect(lambda capture, idx=index: self._on_capture_ready(idx, capture))
        worker.captureFailed.connect(self._on_capture_failed)
        worker.sequenceFinished.connect(lambda: self.confirm_btn.setEnabled(True))
        self._scan_worker = worker  # keep alive for the duration of the sequence
        worker.start()

    def _on_capture_ready(self, index: int, capture):
        image = tifffile.imread(capture.image_path)
        pixmap = image_utils.float_image_to_pixmap(image)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        img_label = QLabel()
        img_label.setPixmap(pixmap.scaledToWidth(220, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(img_label)
        layout.addWidget(QLabel(f"Z={capture.z:.4f}\ncontrast (p99)={capture.contrast_score:.4f}"))
        pick_btn = QPushButton("Pick this Z")
        pick_btn.clicked.connect(lambda _, idx=index, z=capture.z: self._on_pick_z(idx, z))
        layout.addWidget(pick_btn)
        self.capture_row.addWidget(panel)

    def _on_capture_failed(self, z: float, message: str):
        QMessageBox.warning(self, "Confirmation scan failed", f"Z={z:.4f}: {message}")

    def _on_pick_z(self, index: int, z: float):
        self.confirmed_z[index] = z
        self._refresh_list_labels()
        self._on_point_selected(index)

    def _on_manual_apply(self):
        index = self.list_widget.currentRow()
        if index < 0:
            return
        self.confirmed_z[index] = self.manual_z_spin.value()
        self._refresh_list_labels()
        self._on_point_selected(index)

    def _on_accept(self):
        missing = [i for i in range(len(self.fits)) if i not in self.confirmed_z]
        if missing:
            reply = QMessageBox.question(
                self, "Unconfirmed points",
                f"{len(missing)} point(s) have not been confirmed via scan or manual override "
                f"(they'll use the algorithm's automatic pick). Continue anyway?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            for i in missing:
                self.confirmed_z[i] = self.fits[i].z_opt
        self.accept()

    def result_focus_points(self) -> list[tuple[float, float, float]]:
        """[(row, col, z), ...] for every point - only meaningful after accept()."""
        return [(self.fits[i].row, self.fits[i].col, self.confirmed_z[i]) for i in range(len(self.fits))]
