"""Per-region review dialog: shows each focus point's fitted curve, lets the user
confirm a Z via a real 3-capture comparison scan - extendable one more Z at a time
above or below that spread if the optimum still isn't in view - or override
manually, and won't hand back results until every point has an assigned Z.
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
import confirmation_scan
from confirmation_scan import ConfirmationScanWorker

_AUTO_SEARCH_MAX_EXTRA = 3  # on top of the initial 3-point spread, so 6 images total
_AUTO_SEARCH_LABEL = "Auto Search for Max Contrast (≤3 extra images)"


class FocusReviewDialog(QDialog):
    def __init__(
        self, bridge, calibration, region_id: int, fits: list[focus_fitting.FocusFit], parent=None,
        title: str | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title or f"Review Focus Points - Region {region_id}")
        self.resize(1000, 650)
        self.bridge = bridge
        self.calibration = calibration
        self.fits = fits
        self.confirmed_z: dict[int, float] = {}
        self.confirmed_source: dict[int, str] = {}  # index -> "scan"/"manual"/"default", parallel to confirmed_z
        self._scan_worker = None
        self._scan_captures: list[confirmation_scan.ScanCapture] = []  # currently DISPLAYED point's captures, sorted by z
        self._captures_by_index: dict[int, list[confirmation_scan.ScanCapture]] = {}  # every point's captures, survives switching points
        self._scan_index: int | None = None  # which point the running/last scan or auto-search chain actually belongs to - NOT necessarily the currently displayed one, so browsing other points mid-scan can't mix their images together
        self._scan_busy = False  # a confirm/extend scan worker is running right now
        self._auto_search_active = False  # an auto-search chain is in progress (see _continue_auto_search)
        self._auto_search_extra_used = 0
        # "Auto Run Autofocus for Region" mode (see set_auto_confirm): once the
        # region's autofocus finishes, automatically Auto-Search-confirm every
        # point in turn and pick each one's highest-contrast capture, instead of
        # waiting for the operator to do it point by point.
        self._auto_confirm_region = False
        self._auto_finish_region = False
        self._auto_confirm_skip: set[int] = set()  # indices with nothing to search from (no z_opt) or a fully failed scan
        # True while the region's AutofocusSequenceWorker is still running on the
        # bridge - a confirmation scan must not run concurrently with it (the
        # bridge only allows one hardware-affecting call in flight at a time).
        self._collecting = True

        self._build_ui()
        self._populate_list()
        if self.fits:
            self.list_widget.setCurrentRow(0)
        self._update_collecting_ui()
        self._refresh_scan_controls()

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

        self.scan_z_label = QLabel("")
        right.addWidget(self.scan_z_label)

        self.confirm_btn = QPushButton("Confirm via Scan (Z, Z-1µm, Z+1µm)")
        self.confirm_btn.clicked.connect(self._on_confirm_via_scan)
        right.addWidget(self.confirm_btn)

        self.capture_row = QHBoxLayout()
        right.addLayout(self.capture_row)

        extend_row = QHBoxLayout()
        self.extend_down_btn = QPushButton("+ Add point below lowest Z")
        self.extend_down_btn.clicked.connect(lambda: self._on_extend_scan(-1))
        extend_row.addWidget(self.extend_down_btn)
        self.extend_up_btn = QPushButton("+ Add point above highest Z")
        self.extend_up_btn.clicked.connect(lambda: self._on_extend_scan(1))
        extend_row.addWidget(self.extend_up_btn)
        right.addLayout(extend_row)

        self.auto_search_btn = QPushButton(_AUTO_SEARCH_LABEL)
        self.auto_search_btn.clicked.connect(self._on_auto_search)
        right.addWidget(self.auto_search_btn)

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

    def set_auto_confirm(self, enabled: bool, auto_finish: bool = False):
        """Called by main_window right after construction, before the region's
        autofocus sequence starts. If enabled, finish_collecting() will drive
        every point through Auto Search and pick its best capture automatically;
        auto_finish additionally accepts the dialog once the last point is done,
        instead of leaving 'Done Reviewing This Region' for the operator."""
        self._auto_confirm_region = enabled
        self._auto_finish_region = auto_finish

    def add_fit(self, fit: focus_fitting.FocusFit):
        """Appends one point's just-fitted curve, called live as the region's
        autofocus sequence collects each point (see main_window's pointFitted
        handler) so the operator can review already-fitted points without
        waiting for the whole region to finish."""
        self.fits.append(fit)
        index = len(self.fits) - 1
        self.list_widget.addItem(QListWidgetItem(self._label_for(index)))
        if self.list_widget.currentRow() < 0:
            self.list_widget.setCurrentRow(index)

    def _update_collecting_ui(self):
        if self._collecting:
            self.accept_btn.setEnabled(False)
            self.accept_btn.setText("Collecting autofocus data...")
        elif not self.fits:
            self.accept_btn.setEnabled(False)
            self.accept_btn.setText("Autofocus failed - nothing to review")
        else:
            self.accept_btn.setEnabled(True)
            self.accept_btn.setText("Done Reviewing This Region")

    def finish_collecting(self):
        """Called once the region's AutofocusSequenceWorker has finished (all
        points attempted) - unblocks review/accept."""
        self._collecting = False
        self._update_collecting_ui()
        self._refresh_scan_controls()
        self._on_point_selected(self.list_widget.currentRow())

        if self._auto_confirm_region:
            if self.fits:
                self._auto_confirm_skip = set()
                self._advance_auto_confirm_region()
            elif self._auto_finish_region:
                # Nothing to review and nobody's there to close this dialog by
                # hand - reject so main_window's finished-signal handler (and any
                # fully-automated multi-region pipeline chained off it) doesn't
                # hang forever waiting for a signal that would otherwise never come.
                self.reject()

    def _label_for(self, index: int) -> str:
        fit = self.fits[index]
        mark = "OK" if index in self.confirmed_z else "..."
        return f"[{mark}] ({fit.row:.1f}, {fit.col:.1f})"

    def _refresh_list_labels(self):
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setText(self._label_for(i))

    def _on_point_selected(self, row: int):
        self._load_scan_session(row)
        if row < 0 or row >= len(self.fits):
            return
        fit = self.fits[row]

        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.plot(fit.z, fit.normalized_f(), "o", label="measured (normalized)")
        if fit.params is not None:
            z_vals, curve_vals = fit.curve_preview()
            ax.plot(z_vals, curve_vals, "-", label="fit")
        z_opt = self.confirmed_z.get(row, fit.z_opt)
        if z_opt is not None:
            ax.axvline(z_opt, color="red", linestyle="--", label="chosen Z")
        ax.set_xlabel("Z (mm)")
        ax.set_ylabel("normalized focus metric")
        ax.legend()
        self.canvas.draw_idle()

        if z_opt is None:
            self.z_label.setText("No Z available")
            self.scan_z_label.setText("")
        else:
            note = " (confirmed via scan)" if row in self.confirmed_z else " (algorithm pick, not yet confirmed)"
            self.z_label.setText(f"Chosen Z (focus peak) = {z_opt:.4f}{note}")
            # The controller adds z_offset_correction on top of the given Z for every
            # real scan move (not for the focus sweep itself) - see
            # ProcessingTask.cpp's scan_position(), which is where the optical
            # contrast-vs-sharpness shift this offset compensates for actually
            # applies. This is what physically gets scanned, shown for reference only.
            scan_z = z_opt + self.calibration.z_offset_correction
            self.scan_z_label.setText(f"Actual scan Z (with z_offset) = {scan_z:.4f}")
            self.manual_z_spin.setValue(z_opt)

    def _clear_capture_row(self):
        while self.capture_row.count():
            item = self.capture_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _load_scan_session(self, row: int):
        """Switching points - restores this point's previously-captured comparison
        images (if any), instead of dropping them. A scan/auto-search chain still
        running for a DIFFERENT point (self._scan_index) is untouched by this -
        it keeps running in the background and its results still land on the
        right point (see _on_capture_ready) - so browsing other points mid-scan
        is safe and doesn't interrupt it."""
        self._scan_captures = list(self._captures_by_index.get(row, []))
        self._clear_capture_row()
        for c in self._scan_captures:
            self.capture_row.addWidget(self._build_capture_panel(row, c))
        is_active_here = self._auto_search_active and row == self._scan_index
        self.auto_search_btn.setText("Auto searching..." if is_active_here else _AUTO_SEARCH_LABEL)
        self._refresh_scan_controls()

    def _clear_scan_session(self, index: int):
        """Discards this point's previous comparison captures - called right
        before starting a brand new Confirm via Scan/Auto Search sequence for it."""
        self._captures_by_index.pop(index, None)
        self._scan_captures = []
        self._auto_search_active = False
        self.auto_search_btn.setText(_AUTO_SEARCH_LABEL)
        self._clear_capture_row()
        self._refresh_scan_controls()

    def _refresh_scan_controls(self):
        can_scan = not self._collecting and not self._scan_busy
        self.confirm_btn.setEnabled(can_scan)
        self.auto_search_btn.setEnabled(can_scan)
        has_captures = bool(self._scan_captures)
        self.extend_down_btn.setEnabled(can_scan and has_captures)
        self.extend_up_btn.setEnabled(can_scan and has_captures)

    def _on_confirm_via_scan(self):
        if self._collecting:
            QMessageBox.information(
                self, "Confirm via Scan",
                "Autofocus is still collecting this region's points - wait for it to finish before confirming via scan.",
            )
            return
        index = self.list_widget.currentRow()
        if index < 0:
            return
        fit = self.fits[index]
        chosen_z = self.confirmed_z.get(index, fit.z_opt)
        if chosen_z is None:
            QMessageBox.warning(self, "Confirm via Scan", "No candidate Z to confirm yet.")
            return

        self._clear_scan_session(index)
        self._start_scan(index, fit, confirmation_scan.initial_z_values(chosen_z))

    def _on_extend_scan(self, direction: int):
        """direction=+1 adds one more point one step above the highest Z captured
        so far for this point, -1 adds one below the lowest - for when the true
        optimum turns out to lie outside the initial Z, Z-1um, Z+1um spread."""
        if self._collecting or self._scan_busy or not self._scan_captures:
            return
        index = self.list_widget.currentRow()
        if index < 0:
            return
        fit = self.fits[index]
        zs = [c.z for c in self._scan_captures]
        edge_z = max(zs) if direction > 0 else min(zs)
        next_z = edge_z + direction * confirmation_scan.MICRON_MM
        self._start_scan(index, fit, [next_z])

    def _on_auto_search(self):
        """Runs the initial 3-point spread, then automatically keeps extending
        toward whichever edge holds the highest contrast - one point at a time -
        until the max is bracketed by its neighbors (the true peak is in view) or
        _AUTO_SEARCH_MAX_EXTRA extra images have been taken (6 total)."""
        if self._collecting:
            QMessageBox.information(
                self, "Auto Search",
                "Autofocus is still collecting this region's points - wait for it to finish before searching.",
            )
            return
        if self._scan_busy:
            return
        index = self.list_widget.currentRow()
        if index < 0:
            return
        fit = self.fits[index]
        chosen_z = self.confirmed_z.get(index, fit.z_opt)
        if chosen_z is None:
            QMessageBox.warning(self, "Auto Search", "No candidate Z to search from yet.")
            return

        self._clear_scan_session(index)
        self._auto_search_active = True
        self._auto_search_extra_used = 0
        self.auto_search_btn.setText("Auto searching...")
        self._start_scan(index, fit, confirmation_scan.initial_z_values(chosen_z))

    def _continue_auto_search(self) -> bool:
        """Returns True if another extension scan was started, False if the
        search stopped (peak bracketed, budget spent, or nothing to go on).
        Operates on self._scan_index (the point the chain actually belongs to),
        not whatever point happens to be displayed - the operator may have
        clicked elsewhere in the list while this was running."""
        index = self._scan_index
        if index is None:
            return False
        captures = self._captures_by_index.get(index, [])
        if self._auto_search_extra_used >= _AUTO_SEARCH_MAX_EXTRA or not captures:
            return False

        best_i = max(range(len(captures)), key=lambda i: captures[i].contrast_score)
        if 0 < best_i < len(captures) - 1:
            return False  # highest contrast already has a lower neighbor on each side - peak's in view

        direction = 1 if best_i == len(captures) - 1 else -1
        next_z = captures[best_i].z + direction * confirmation_scan.MICRON_MM
        self._auto_search_extra_used += 1
        self._start_scan(index, self.fits[index], [next_z])
        return True

    def _start_scan(self, index: int, fit, z_values: list[float]):
        self._scan_index = index
        self._scan_busy = True
        self._refresh_scan_controls()

        # fit.row/fit.col are LOCAL section coordinates (see autofocus_client.py);
        # run_single_section_scan needs FINAL master-grid row/col, i.e. offset by
        # this calibration's own master-grid address (see stage_calibration.py's
        # section_to_absolute_xy/shifted_anchor_for_focus, which do the same add).
        master_row = int(round(self.calibration.offset_row + fit.row))
        master_col = int(round(self.calibration.offset_col + fit.col))
        worker = ConfirmationScanWorker(self.bridge, self.calibration, row=master_row, col=master_col,
                                         z_values=z_values)
        worker.captureReady.connect(lambda capture, idx=index: self._on_capture_ready(idx, capture))
        worker.captureFailed.connect(self._on_capture_failed)
        worker.sequenceFinished.connect(self._on_scan_sequence_finished)
        self._scan_worker = worker  # keep alive for the duration of the sequence
        worker.start()

    def _on_scan_sequence_finished(self):
        self._scan_busy = False
        if self._auto_search_active:
            if self._continue_auto_search():
                return
            self._auto_search_active = False
            self.auto_search_btn.setText(_AUTO_SEARCH_LABEL)
            if self._auto_confirm_region:
                self._auto_pick_best_and_continue()
                return
        self._refresh_scan_controls()

    def _auto_pick_best_and_continue(self):
        """Called once a point's Auto Search chain (started by
        _advance_auto_confirm_region) has stopped - picks that point's
        highest-contrast capture as its confirmed Z, same as clicking 'Pick this
        Z' on it, then moves auto-confirm on to the next point. Uses
        self._scan_index rather than the currently displayed row, since the
        operator may have clicked to a different point while this was running."""
        index = self._scan_index
        if index is not None:
            captures = self._captures_by_index.get(index, [])
            if captures:
                best = max(captures, key=lambda c: c.contrast_score)
                self._on_pick_z(index, best.z)
            else:
                # every capture for this point failed - nothing to pick from;
                # leave it unconfirmed rather than looping on it forever.
                self._auto_confirm_skip.add(index)
        self._advance_auto_confirm_region()

    def _advance_auto_confirm_region(self):
        """Moves 'Auto Run Autofocus for Region' on to the next not-yet-confirmed
        point, or finishes up once every point has been handled."""
        next_index = next(
            (i for i in range(len(self.fits)) if i not in self.confirmed_z and i not in self._auto_confirm_skip),
            None,
        )
        if next_index is None:
            self._auto_confirm_region = False
            if self._auto_finish_region:
                self._on_accept()
            else:
                self._refresh_scan_controls()
            return

        if self.fits[next_index].z_opt is None:
            # nothing to search from for this point - skip it rather than stalling.
            self._auto_confirm_skip.add(next_index)
            self._advance_auto_confirm_region()
            return

        self.list_widget.setCurrentRow(next_index)
        self._on_auto_search()

    def _on_capture_ready(self, index: int, capture):
        """A capture always belongs to the point the scan was actually started
        for (index, bound at _start_scan time) - never to whatever point the
        operator happens to be looking at right now, which may have changed
        while this scan was running on real hardware."""
        captures = self._captures_by_index.setdefault(index, [])
        captures.append(capture)
        captures.sort(key=lambda c: c.z)

        if index != self.list_widget.currentRow():
            return  # not the point currently on screen - leave the visible gallery alone
        self._scan_captures = captures
        self._clear_capture_row()
        for c in captures:
            self.capture_row.addWidget(self._build_capture_panel(index, c))
        self._refresh_scan_controls()

    def _build_capture_panel(self, index: int, capture) -> QWidget:
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
        return panel

    def _on_capture_failed(self, z: float, message: str):
        if self._auto_confirm_region:
            # A modal box here would silently block the whole unattended region
            # run waiting for a click nobody's there to give - _continue_auto_search
            # and _auto_pick_best_and_continue already cope with a capture missing
            # from _captures_by_index, so just note it and let the chain carry on.
            self.z_label.setText(f"Auto Search: capture at Z={z:.4f} failed ({message}) - continuing")
            return
        QMessageBox.warning(self, "Confirmation scan failed", f"Z={z:.4f}: {message}")

    def _on_pick_z(self, index: int, z: float):
        self.confirmed_z[index] = z
        self.confirmed_source[index] = "scan"
        self._refresh_list_labels()
        if index == self.list_widget.currentRow():
            self._on_point_selected(index)

    def _on_manual_apply(self):
        index = self.list_widget.currentRow()
        if index < 0:
            return
        self.confirmed_z[index] = self.manual_z_spin.value()
        self.confirmed_source[index] = "manual"
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
                self.confirmed_source[i] = "default"
        self.accept()

    def result_focus_points(self) -> list[tuple[float, float, float]]:
        """[(row, col, z), ...] for every point - only meaningful after accept()."""
        return [(self.fits[i].row, self.fits[i].col, self.confirmed_z[i]) for i in range(len(self.fits))]

    def confirmed_z_with_source(self) -> dict[int, tuple[float, str]]:
        """focus_index -> (confirmed Z, how it was confirmed: 'scan'/'manual'/
        'default') - only meaningful after accept(). Used by main_window to
        fill in autofocus_log.json's confirmed_z/confirmed_source fields."""
        return {i: (self.confirmed_z[i], self.confirmed_source[i]) for i in range(len(self.fits))}

    def captures_by_index(self) -> dict[int, list[confirmation_scan.ScanCapture]]:
        """Every point's confirmation-scan captures, keyed by point index - used
        by main_window to archive each focus point's run folders once the
        region is confirmed (see scan_archive.archive_focus_point)."""
        return self._captures_by_index
