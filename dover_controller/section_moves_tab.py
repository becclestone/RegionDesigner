"""Section Moves tab - ports DOVER_UI's "Section Moves" tab (DOVER_UI/Windows/
main_window_layouts.py:133-227, main_window.py:722-772). Moves the stage to a named
position (corner/scan-start-end/focus-start-end) of a given master-grid row/col.
"""
from PySide6.QtWidgets import QButtonGroup, QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget

from Constants import implementation_constants as ic, project_constants as pc
from controller_bridge import ControllerBridge
from Utilities.time_utilities import execution_time_stamp


class SectionMovesTab(QWidget):
    def __init__(self, bridge: ControllerBridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window

        layout = QVBoxLayout(self)

        section_group = QGroupBox(" Section ")
        section_layout = QVBoxLayout(section_group)
        row_row = QHBoxLayout()
        row_row.addWidget(QLabel("Row (0-20):"))
        self.row_spin = QSpinBox()
        self.row_spin.setRange(0, 20)
        row_row.addWidget(self.row_spin, 1)
        row_row.addWidget(QLabel("Master row:"))
        self.master_row_label = QLabel("0")
        row_row.addWidget(self.master_row_label)
        section_layout.addLayout(row_row)

        col_row = QHBoxLayout()
        col_row.addWidget(QLabel("Column (0-55):"))
        self.col_spin = QSpinBox()
        self.col_spin.setRange(0, 55)
        col_row.addWidget(self.col_spin, 1)
        col_row.addWidget(QLabel("Master column:"))
        self.master_col_label = QLabel("0")
        col_row.addWidget(self.master_col_label)
        section_layout.addLayout(col_row)
        layout.addWidget(section_group)

        # Master row/col readouts mirror DOVER_UI's own display of row/col plus the
        # Calibration tab's path-grid offset (handle_section_moves_tab_event:758-763),
        # but this tab has no reach into the Calibration tab's offset state - showing
        # just the row/col here is a presentational simplification, not a functional
        # loss, since move_to_section below sends row/col directly either way.
        self.row_spin.valueChanged.connect(lambda v: self.master_row_label.setText(str(v)))
        self.col_spin.valueChanged.connect(lambda v: self.master_col_label.setText(str(v)))

        position_group = QGroupBox(" Position selector ")
        position_layout = QVBoxLayout(position_group)
        self._position_buttons = QButtonGroup(self)
        self._position_value_for_button: dict[QRadioButton, str] = {}
        for value in pc.SECTION_POSITIONS:
            rb = QRadioButton(pc.POSITION_LABELS[value])
            self._position_buttons.addButton(rb)
            self._position_value_for_button[rb] = value
            if value == pc.SCAN_START_R:
                rb.setChecked(True)
            position_layout.addWidget(rb)
        offset_row = QHBoxLayout()
        offset_row.addWidget(QLabel("Y Bias offset (0-7):"))
        self.scan_offset_spin = QSpinBox()
        self.scan_offset_spin.setRange(0, 7)
        offset_row.addWidget(self.scan_offset_spin)
        offset_row.addStretch(1)
        position_layout.addLayout(offset_row)
        layout.addWidget(position_group)

        execute_btn = QPushButton("Execute move")
        execute_btn.clicked.connect(self._on_execute_clicked)
        layout.addWidget(execute_btn)

        layout.addWidget(QLabel("Section moves log:"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        clear_log_btn = QPushButton("Clear log")
        clear_log_btn.clicked.connect(self.log.clear)
        layout.addWidget(clear_log_btn)

        bridge.replyReceived.connect(self._on_reply)

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(execution_time_stamp() + text)

    def _current_position_mode(self) -> str:
        checked = self._position_buttons.checkedButton()
        return self._position_value_for_button[checked]

    def _on_execute_clicked(self) -> None:
        self.window.set_motion_locked(True)
        row, col = self.row_spin.value(), self.col_spin.value()
        position_mode = self._current_position_mode()
        log_line = f"Moving to section: ({row}, {col}) position: {pc.POSITION_LABELS[position_mode]}"
        if position_mode in (pc.SCAN_START_R, pc.SCAN_END_R):
            log_line += f"      scan offset: {self.scan_offset_spin.value()}"
        self._append_log(log_line)
        self.bridge.move_to_section(row, col, position_mode, self.scan_offset_spin.value())

    def _on_reply(self, reply_type: str, payload: dict) -> None:
        if reply_type != ic.cSECTION_MOVE_MSG:
            return
        # Payload shape confirmed against DOVER_UI/MessageHandler.py's
        # handle_section_move_reply_msg: [x_before, x_after, y_before, y_after].
        pos = payload[ic.cPOSITIONS]
        self._append_log(f"x: {pos[0]} -> {pos[1]}   y: {pos[2]} -> {pos[3]}")
        self.window.set_motion_locked(False)
