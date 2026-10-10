"""System Config tab - ports DOVER_UI's "System" tab (DOVER_UI/Windows/
main_window_layouts.py:771-1221, main_window.py:140-165,262-294,1330-1554).

Omitted as dead/non-functional in the live app, same policy as elsewhere in
dover_controller/:
- "Scattering channel only" checkbox: present in the layout but
  handle_system_configuration_tab_events has no branch for it - toggling it does
  nothing in DOVER_UI either.
- "Stop TA"/"Start TA" buttons: their event branches are commented out
  (main_window.py:1453-1460) - visible but non-functional.
- The Test frame's "Repeat scans automatically" checkbox: it only ever
  notify_partners(REPEAT_SCANS) to DOVER_UI's own Image-Path/demo windows, telling
  THEM to auto-repeat the next scan - RegionDesigner's scan triggers (Scan Region /
  Send Scan Path to Controller / Auto Run All Regions) are a different mechanism
  with no such consumer, so this checkbox would be inert here.
- "Do not remove outliers" checkbox: a client-side-only flag DOVER_UI's own code
  reads elsewhere for ITS OWN outlier logic - RegionDesigner already has its own,
  separate RANSAC-outlier-exclusion toggle (main_window.py's "Exclude flagged
  outliers" checkbox) and no code path that would consult this one.
- Temperature trend icons (rising/falling/level PNGs): cosmetic only - the numeric
  current/max/min values carry the same information, shown as plain labels instead.
"""
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

from Constants import implementation_constants as ic
from controller_bridge import ControllerBridge
from Utilities import controller_commands

# Sensor key -> (row label, column index) - main_window.py:1547-1554 skips sensor 7
# ("display when sensor is connected" - never wired up), reproduced the same way here.
_TEMP_SENSORS = [
    (ic.cTEMP_SENSOR_1, "Power Supply"), (ic.cTEMP_SENSOR_2, "Amplitude"),
    (ic.cTEMP_SENSOR_3, "Telescope"), (ic.cTEMP_SENSOR_4, "Ambient"),
    (ic.cTEMP_SENSOR_5, "Stage"), (ic.cTEMP_SENSOR_6, "Optical breadboard"),
]

_FOCUS_NO, _FOCUS_NORMAL, _FOCUS_AUTO, _FOCUS_AUTO_INTERVAL, _FOCUS_CALCULATED = 1, 2, 3, 4, 5


class SystemConfigTab(QWidget):
    def __init__(self, bridge: ControllerBridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        # [running_min, running_max] - starting min high/max low so the first real
        # reading always replaces both, same seeding as DOVER_UI's own temp_value
        # dict (main_window.py:339-347: [current, max=-50.0, min=50.0]).
        self._temp_min_max: dict[str, list[float]] = {key: [50.0, -50.0] for key, _ in _TEMP_SENSORS}
        self._temp_labels: dict[str, tuple[QLabel, QLabel, QLabel]] = {}
        # Quit-on-exit checkboxes, consulted only by RegionDesignerWindow.closeEvent
        # via send_quit_on_exit() below - mirrors send_quit_event (main_window.py:234-248).
        self._quit_checkboxes: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        top_row = QHBoxLayout()
        top_row.addWidget(self._build_data_capture_group())
        top_row.addWidget(self._build_reconstruction_group())
        right_col = QVBoxLayout()
        right_col.addWidget(self._build_stitcher_group())
        right_col.addWidget(self._build_tiff_size_group())
        top_row.addLayout(right_col)
        layout.addLayout(top_row)

        mid_row = QHBoxLayout()
        mid_row.addWidget(self._build_quit_group())
        mid_row.addWidget(self._build_stop_now_group())
        right_col2 = QVBoxLayout()
        right_col2.addWidget(self._build_shutter_group())
        right_col2.addWidget(self._build_warmup_group())
        mid_row.addLayout(right_col2)
        layout.addLayout(mid_row)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self._build_motion_group())
        bottom_row.addWidget(self._build_focus_group())
        layout.addLayout(bottom_row)

        layout.addWidget(self._build_temperature_group())
        layout.addStretch(1)

        bridge.temperatureUpdated.connect(self._on_temperature_updated)
        bridge.broadcastReceived.connect(self._on_broadcast)

    def _on_broadcast(self, msg_type: str, payload: dict) -> None:
        if msg_type != ic.cINTERNAL_CONFIG_VALUES_MSG:
            return
        # Mirrors handle_internal_config_msg (main_window.py:1474-1524) - resyncs this
        # tab's checkboxes/radios to whatever each module actually has configured,
        # after request_internal_config() (sent once when the Dover Controller window
        # first opens, and from its "Connect" button). Each checkbox's own toggled
        # handler would otherwise immediately re-send the value it was just set to -
        # harmless (idempotent) but pointless - so updates here go through
        # blockSignals. cDETECTION_LASER_SCAN/FOCUS_POWER_LEVEL (also part of
        # CTL_TARGET's reply) belong to the Lasers tab's OXXIUS sliders, not this one -
        # not resynced here.
        sender = payload.get("_sender")
        if sender == ic.CTL_TARGET:
            self._set_checked(self.both_gage_cards_cb, payload.get(ic.cUSE_BOTH_GAGE_CARDS_PARAM))
            self._set_checked(self.delete_focus_bin_cb, payload.get(ic.cDELETE_FOCUS_BIN_FILES_PARAM))
            self._set_checked(self.use_compression_cb, payload.get(ic.cUSE_COMPRESSION_PARAM))
            self._set_checked(self.save_focus_cb, payload.get(ic.cSAVE_FOCUS_VALUES_PARAM))
            self._set_checked(self.focus_scan_only_cb, payload.get(ic.cFOCUS_SCAN_ONLY_PARAM))
            self._set_checked(self.plane_coeffs_only_cb, payload.get(ic.cPLANE_COEFFICIENTS_ONLY_PARAM))
            if ic.cOXXIUS_WARMUP_PARAM in payload:
                self._set_checked(self.warmup_cb, payload[ic.cOXXIUS_WARMUP_PARAM])
            self._set_checked(self.cmf_cb, payload.get(ic.cCMF_PARAM))
            self._set_checked(self.bis_cb, payload.get(ic.cBIS_PARAM))
        elif sender == ic.RECONSTRUCTION_TARGET:
            self._set_checked(self.delete_channels_cb, payload.get(ic.cDELETE_CHANNEL_FILES_PARAM))
            self._set_checked(self.multithreaded_cb, payload.get(ic.cMULTI_THREADING_PARAM))
            if ic.cTIFF_DATA_SIZE_PARAM in payload:
                rb = self._tiff16_rb if payload[ic.cTIFF_DATA_SIZE_PARAM] else self._tiff8_rb
                self._set_checked(rb, True)
        elif sender == ic.STITCHER_TARGET:
            self._set_checked(self.keep_section_images_cb, payload.get(ic.cKEEP_SECTION_IMAGE_FILES_PARAM))
        elif sender == ic.SAFE_MON_TARGET:
            self._set_checked(self.shutter_position_cb, payload.get(ic.cOVERRIDE_SHUTTER_POS_PARAM))
            self._set_checked(self.shutter_override_cb, payload.get(ic.cOVERRIDE_SHUTTER_PARAM))

    @staticmethod
    def _set_checked(widget, value) -> None:
        if value is None:
            return
        widget.blockSignals(True)
        widget.setChecked(bool(value))
        widget.blockSignals(False)

    # --- Reconstruction / Data Capture / Stitcher / Tiff size ---

    def _build_reconstruction_group(self) -> QGroupBox:
        group = QGroupBox(" Reconstruction ")
        layout = QVBoxLayout(group)
        batch_rb = QRadioButton("Batch")
        batch_rb.setEnabled(False)  # disabled in DOVER_UI's own layout too (main_window_layouts.py:775)
        layout.addWidget(batch_rb)
        single_file_rb = QRadioButton("Single file")
        single_file_rb.setChecked(True)
        single_file_rb.toggled.connect(lambda checked: checked and self.bridge.send_param(
            ic.cBATCH_PROCESSING_MSG, ic.CTL_TARGET, {ic.cBATCH_PROCESSING_PARAM: False}))
        layout.addWidget(single_file_rb)
        self.batch_rb, self.single_file_rb = batch_rb, single_file_rb

        delete_channels_cb = self._checkbox(layout, "Delete channel files after", True,
                                             ic.cDELETE_CHANNEL_FILES_MSG, ic.RECONSTRUCTION_TARGET, ic.cDELETE_CHANNEL_FILES_PARAM)
        self.delete_channels_cb = delete_channels_cb
        self.multithreaded_cb = self._checkbox(layout, "Multi-threaded Reconstruction", False,
                                                ic.cMULTI_THREADING_MSG, ic.RECONSTRUCTION_TARGET, ic.cMULTI_THREADING_PARAM)
        return group

    def _build_data_capture_group(self) -> QGroupBox:
        group = QGroupBox(" Data Capture ")
        layout = QVBoxLayout(group)
        self.both_gage_cards_cb = self._checkbox(layout, "Use both Gage cards", True,
                                                  ic.cUSE_BOTH_GAGE_CARDS_MSG, ic.CTL_TARGET, ic.cUSE_BOTH_GAGE_CARDS_PARAM)
        self.save_focus_cb = self._checkbox(layout, "Save Focus data to .csv", True,
                                             ic.cSAVE_FOCUS_VALUES_MSG, ic.CTL_TARGET, ic.cSAVE_FOCUS_VALUES_PARAM)
        self.focus_scan_only_cb = self._checkbox(layout, "Capture Focus data ONLY", False,
                                                  ic.cFOCUS_SCAN_ONLY_MSG, ic.CTL_TARGET, ic.cFOCUS_SCAN_ONLY_PARAM)
        self.plane_coeffs_only_cb = self._checkbox(layout, "Calculate plane coefficients ONLY", False,
                                                    ic.cPLANE_COEFFICIENTS_MSG, ic.CTL_TARGET, ic.cPLANE_COEFFICIENTS_ONLY_PARAM)
        self.delete_focus_bin_cb = self._checkbox(layout, "Delete Focus .bin files", True,
                                                   ic.cDELETE_FOCUS_BIN_FILES_MSG, ic.CTL_TARGET, ic.cDELETE_FOCUS_BIN_FILES_PARAM)
        self.use_compression_cb = self._checkbox(layout, "Use compression", True,
                                                  ic.cUSE_B_CARD_COMPRESSION_MSG, ic.CTL_TARGET, ic.cUSE_COMPRESSION_PARAM)
        return group

    def _build_stitcher_group(self) -> QGroupBox:
        group = QGroupBox(" Stitcher ")
        layout = QVBoxLayout(group)
        self.keep_section_images_cb = self._checkbox(layout, "Keep section image files", False,
                                                       ic.cKEEP_SECTION_IMAGE_FILES_MSG, ic.STITCHER_TARGET, ic.cKEEP_SECTION_IMAGE_FILES_PARAM)
        return group

    def _build_tiff_size_group(self) -> QGroupBox:
        group = QGroupBox(" Tiff data size ")
        layout = QHBoxLayout(group)
        self._tiff_buttons = QButtonGroup(self)
        self._tiff16_rb = QRadioButton("int16")
        self._tiff16_rb.setChecked(True)
        self._tiff8_rb = QRadioButton("int8")
        for rb in (self._tiff16_rb, self._tiff8_rb):
            self._tiff_buttons.addButton(rb)
            layout.addWidget(rb)
        self._tiff16_rb.toggled.connect(lambda checked: checked and self._on_tiff_size_changed(True))
        self._tiff8_rb.toggled.connect(lambda checked: checked and self._on_tiff_size_changed(False))
        return group

    def _on_tiff_size_changed(self, is_16_bit: bool) -> None:
        # Mirrors handle_tiff_size (main_window.py:262-269) - sent to both targets.
        payload = {ic.cTIFF_DATA_SIZE_PARAM: is_16_bit}
        self.bridge.send_param(ic.cTIFF_DATA_SIZE_MSG, ic.RECONSTRUCTION_TARGET, payload)
        self.bridge.send_param(ic.cTIFF_DATA_SIZE_MSG, ic.STITCHER_TARGET, payload)

    def _checkbox(self, layout: QVBoxLayout, label: str, default: bool, msg_type: str, target: str, param_key: str) -> QCheckBox:
        cb = QCheckBox(label)
        cb.setChecked(default)
        cb.toggled.connect(lambda checked: self.bridge.send_param(msg_type, target, {param_key: checked}))
        layout.addWidget(cb)
        return cb

    # --- On UI Quit / Stop Now ---

    def _build_quit_group(self) -> QGroupBox:
        group = QGroupBox(" On UI Quit ")
        layout = QVBoxLayout(group)
        for label, target in (
            ("Quit Controller", ic.CTL_TARGET), ("Quit Reconstructor", ic.RECONSTRUCTION_TARGET),
            ("Quit Image Capture", ic.IMAGE_CAPTURE_TARGET), ("Quit Safety Monitor", ic.SAFE_MON_TARGET),
            ("Quit Stitcher", ic.STITCHER_TARGET), ("Quit Stainer", ic.STAINER_TARGET),
            ("Quit Focus Calculation", ic.FOCUS_TARGET),
        ):
            cb = QCheckBox(label)
            layout.addWidget(cb)
            self._quit_checkboxes[target] = cb
        return group

    def send_quit_on_exit(self) -> None:
        """Mirrors send_quit_event (main_window.py:234-248) - called from
        RegionDesignerWindow.closeEvent."""
        for target, cb in self._quit_checkboxes.items():
            if cb.isChecked():
                self.bridge.send_quit(target)

    def _build_stop_now_group(self) -> QGroupBox:
        group = QGroupBox(" Stop Now ")
        layout = QVBoxLayout(group)
        for label, target in (
            ("Controller", ic.CTL_TARGET), ("Reconstructor", ic.RECONSTRUCTION_TARGET),
            ("Image Capture", ic.IMAGE_CAPTURE_TARGET), ("Safety Monitor", ic.SAFE_MON_TARGET),
            ("Stitcher", ic.STITCHER_TARGET), ("Stainer", ic.STAINER_TARGET),
            ("Focus Calc", ic.FOCUS_TARGET),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _checked=False, t=target: self.bridge.send_quit(t))
            layout.addWidget(btn)
        return group

    # --- Shutter / Laser warm up ---

    def _build_shutter_group(self) -> QGroupBox:
        group = QGroupBox(" Shutter ")
        layout = QVBoxLayout(group)
        self.shutter_override_cb = QCheckBox("Override")
        self.shutter_position_cb = QCheckBox("Shutter position (ON - shutter opened)")
        for cb in (self.shutter_override_cb, self.shutter_position_cb):
            cb.toggled.connect(self._on_shutter_changed)
            layout.addWidget(cb)
        return group

    def _on_shutter_changed(self, _checked: bool) -> None:
        # Mirrors handle_shutter_settings (main_window.py:272-279) - always sends both
        # checkboxes' current state together, to SAFE_MON_TARGET.
        payload = {
            ic.cOVERRIDE_SHUTTER_PARAM: self.shutter_override_cb.isChecked(),
            ic.cOVERRIDE_SHUTTER_POS_PARAM: self.shutter_position_cb.isChecked(),
        }
        self.bridge.send_param(ic.cOVERRIDE_SHUTTER_MSG, ic.SAFE_MON_TARGET, payload)

    def _build_warmup_group(self) -> QGroupBox:
        group = QGroupBox(" Laser warm up ")
        layout = QVBoxLayout(group)
        self.warmup_cb = QCheckBox("Warm up Oxxius laser before each scan")
        self.warmup_cb.setChecked(True)
        self.warmup_cb.toggled.connect(lambda checked: self.bridge.send_param(
            ic.cOXXIUS_WARMUP_MSG, ic.CTL_TARGET, {ic.cOXXIUS_WARMUP_PARAM: checked}))
        layout.addWidget(self.warmup_cb)
        return group

    # --- Motion / Focus ---

    def _build_motion_group(self) -> QGroupBox:
        group = QGroupBox(" Motion ")
        layout = QVBoxLayout(group)
        scan_btn = QPushButton("Move to Scan Position")
        scan_btn.clicked.connect(self._on_move_to_scan_clicked)
        layout.addWidget(scan_btn)
        load_btn = QPushButton("Move to Load Position")
        load_btn.clicked.connect(controller_commands.move_to_load_position)
        layout.addWidget(load_btn)
        snap_btn = QPushButton("Move to Snap Position")
        snap_btn.clicked.connect(self._on_move_to_snap_clicked)
        layout.addWidget(snap_btn)
        return group

    def _on_move_to_scan_clicked(self) -> None:
        self.bridge.safemon_motion()
        controller_commands.move_to_scan_position()

    def _on_move_to_snap_clicked(self) -> None:
        self.bridge.safemon_motion()
        controller_commands.move_to_snap_position_motion_only()

    def _build_focus_group(self) -> QGroupBox:
        group = QGroupBox(" Focus ")
        layout = QVBoxLayout(group)

        self._focus_buttons = QButtonGroup(self)
        self._focus_value_for_button: dict[QRadioButton, int] = {}

        no_focus_row = QHBoxLayout()
        no_focus_rb = QRadioButton("No Focus (use provided Z value)")
        no_focus_row.addWidget(no_focus_rb)
        no_focus_row.addWidget(QLabel("Focus Z value:"))
        self.focus_z_edit = QLineEdit("1.000")
        self.focus_z_edit.editingFinished.connect(self._send_focusing_params)
        no_focus_row.addWidget(self.focus_z_edit)
        layout.addLayout(no_focus_row)
        self._add_focus_radio(layout, no_focus_rb, _FOCUS_NO)

        self._add_focus_radio(layout, QRadioButton("Normal Focus (Fine)"), _FOCUS_NORMAL)
        self._add_focus_radio(layout, QRadioButton("Auto Focus (Coarse->Medium->Fine)"), _FOCUS_AUTO)

        interval_row = QHBoxLayout()
        interval_rb = QRadioButton("Auto Focus (M->F and C->M->F) every")
        interval_row.addWidget(interval_rb)
        self.focus_interval_spin = QSpinBox()
        self.focus_interval_spin.setRange(2, 10)
        self.focus_interval_spin.setValue(5)
        self.focus_interval_spin.valueChanged.connect(self._send_focusing_params)
        interval_row.addWidget(self.focus_interval_spin)
        interval_row.addWidget(QLabel("sections."))
        layout.addLayout(interval_row)
        self._add_focus_radio(layout, interval_rb, _FOCUS_AUTO_INTERVAL, add_to_layout=False)

        calculated_rb = QRadioButton("Calculated Z plane (M->F and C->M->F every 5 sections)")
        calculated_rb.setChecked(True)
        self._add_focus_radio(layout, calculated_rb, _FOCUS_CALCULATED)

        self.bis_cb = QCheckBox("Alternate use of normal & bis focusing params")
        self.bis_cb.setChecked(True)
        self.bis_cb.toggled.connect(lambda checked: self.bridge.send_param(ic.cBIS_USE_MSG, ic.CTL_TARGET, {ic.cBIS_PARAM: checked}))
        layout.addWidget(self.bis_cb)
        self.cmf_cb = QCheckBox("Always use full CMF when calculating focus (override)")
        self.cmf_cb.toggled.connect(lambda checked: self.bridge.send_param(ic.cCMF_USE_MSG, ic.CTL_TARGET, {ic.cCMF_PARAM: checked}))
        layout.addWidget(self.cmf_cb)

        return group

    def _add_focus_radio(self, layout: QVBoxLayout, rb: QRadioButton, value: int, add_to_layout: bool = True) -> None:
        self._focus_buttons.addButton(rb)
        self._focus_value_for_button[rb] = value
        rb.toggled.connect(lambda checked: checked and self._send_focusing_params())
        if add_to_layout:
            layout.addWidget(rb)

    def _send_focusing_params(self) -> None:
        # Mirrors send_focusing_params (main_window.py:140-164).
        checked = self._focus_buttons.checkedButton()
        focusing_type = self._focus_value_for_button.get(checked, _FOCUS_CALCULATED)
        try:
            z_plane = float(self.focus_z_edit.text())
        except ValueError:
            return
        payload = {
            ic.cFOCUSING_TYPE_PARAM: focusing_type,
            ic.cFOCUSING_Z_PLANE_PARAM: z_plane,
            ic.cFOCUSING_INTERVAL_PARAM: self.focus_interval_spin.value(),
        }
        self.bridge.send_param(ic.cFOCUSING_PARAMS_MSG, ic.CTL_TARGET, payload)

    # --- Temperature ---

    def _build_temperature_group(self) -> QGroupBox:
        group = QGroupBox(" Temperature ")
        grid = QGridLayout(group)
        grid.addWidget(QLabel("Sensor"), 0, 0)
        grid.addWidget(QLabel("Current"), 0, 1)
        grid.addWidget(QLabel("Max"), 0, 2)
        grid.addWidget(QLabel("Min"), 0, 3)
        for row, (key, label) in enumerate(_TEMP_SENSORS, start=1):
            grid.addWidget(QLabel(label), row, 0)
            current_lbl, max_lbl, min_lbl = QLabel("0.0"), QLabel("0.0"), QLabel("0.0")
            grid.addWidget(current_lbl, row, 1)
            grid.addWidget(max_lbl, row, 2)
            grid.addWidget(min_lbl, row, 3)
            self._temp_labels[key] = (current_lbl, max_lbl, min_lbl)
        return group

    def _on_temperature_updated(self, payload: dict) -> None:
        for key, _label in _TEMP_SENSORS:
            if key not in payload:
                continue
            t = float(payload[key])
            current_lbl, max_lbl, min_lbl = self._temp_labels[key]
            current_lbl.setText(str(t))
            bounds = self._temp_min_max[key]
            if t > bounds[1]:
                bounds[1] = t
                max_lbl.setText(str(t))
            if t < bounds[0]:
                bounds[0] = t
                min_lbl.setText(str(t))
