"""Dover Controller window - ports DOVER_UI's "Dover Controller" window
(DOVER_UI/Windows/main_window.py + main_window_layouts.py) into RegionDesigner, so
RegionDesigner no longer needs DOVER_UI running for raw hardware control (manual jog,
section moves, calibration, focus jog, autofocus, MEMS, lasers, system config). One
QTabWidget hosting the 8 tabs below, each its own file/widget class - for the same
reason DOVER_UI itself split main_window_layouts.py out of main_window.py, the tab
logic IS the business logic here, unlike RegionDesignerWindow's own toolbar (which is
thin glue over separately-modularized workers like region_scan.py/autofocus_client.py).

Singleton, owned by RegionDesignerWindow (constructor-injected the SAME ControllerBridge
RegionDesignerWindow itself uses - there is exactly one RabbitMQ connection/sender-
signature ("dover_ui") per process, so this never creates its own). Hidden rather than
destroyed on close, since it holds real in-memory hardware/UI state (MEMS running flag,
OXXIUS log, focus-curve figure, slider positions) that would otherwise need re-syncing
(discover_stages/discover_mems/request_internal_config) on every reopen.

Unlike DOVER_UI's own MainWindow/PathWindow/DemoWindowA (separate FreeSimpleGUI windows
glued together by a shared process-local queue + notify_partners/send_msg_to_window),
this window and RegionDesignerWindow are two Qt widgets in the same QApplication/event
loop - ordinary Qt signals (calibrationLoaded below) replace that whole mechanism.
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QPushButton, QStatusBar, QTabWidget, QVBoxLayout, QWidget

from controller_bridge import ControllerBridge
from goji_scheduler import GojiScheduler
from Utilities.time_utilities import execution_time_stamp

from .autofocus_tab import AutofocusTab
from .calibration_tab import CalibrationTab
from .focus_tab import FocusAdjustmentTab
from .lasers_tab import LasersTab
from .manual_moves_tab import ManualMovesTab
from .mems_tab import MemsTab
from .section_moves_tab import SectionMovesTab
from .system_config_tab import SystemConfigTab


class DoverControllerWindow(QMainWindow):
    # Re-emitted from CalibrationTab's own calibrationLoaded (the loaded file path) -
    # RegionDesignerWindow connects this to the same handler its own "Load
    # Calibration..." toolbar button uses, so loading a calibration file from either
    # window updates both - otherwise the operator could load different calibration
    # files in the two windows and get silently inconsistent section_to_absolute_xy
    # math between the canvas and this window's manual-jog readouts. Not a concern
    # DOVER_UI ever had (it never had two calibration entry points).
    calibrationLoaded = Signal(str)

    def __init__(self, bridge: ControllerBridge, goji_scheduler: GojiScheduler, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dover Controller")
        self.resize(1150, 780)
        self.bridge = bridge
        self._in_motion = False
        self._did_initial_sync = False

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        top_row = QHBoxLayout()
        connect_btn = QPushButton("Connect")
        connect_btn.setToolTip(
            "Discovers the stage and MEMS driver and requests the current internal "
            "config from every module - mirrors DOVER_UI's \"Connect\" button."
        )
        connect_btn.clicked.connect(self._on_connect_clicked)
        top_row.addWidget(connect_btn)

        reset_stage_btn = QPushButton("Reset Stage")
        reset_stage_btn.setToolTip("Moves the stage to its load position.")
        reset_stage_btn.clicked.connect(self.bridge.reset_stage)
        top_row.addWidget(reset_stage_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setToolTip("Re-enables every tab (in case a dropped reply left one locked out).")
        reset_btn.clicked.connect(lambda: self.set_motion_locked(False))
        top_row.addWidget(reset_btn)

        top_row.addStretch(1)
        layout.addLayout(top_row)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self.manual_moves_tab = ManualMovesTab(bridge, self)
        self.section_moves_tab = SectionMovesTab(bridge, self)
        self.calibration_tab = CalibrationTab(bridge, self)
        self.focus_tab = FocusAdjustmentTab(bridge, self)
        self.autofocus_tab = AutofocusTab(bridge, self)
        self.mems_tab = MemsTab(bridge, self)
        self.lasers_tab = LasersTab(bridge, goji_scheduler, self)
        self.system_config_tab = SystemConfigTab(bridge, self)

        self.tabs.addTab(self.manual_moves_tab, "Manual Moves")
        self.tabs.addTab(self.section_moves_tab, "Section Moves")
        self.tabs.addTab(self.calibration_tab, "Calibration")
        self.tabs.addTab(self.focus_tab, "Focus")
        self.tabs.addTab(self.autofocus_tab, "Autofocus")
        self.tabs.addTab(self.mems_tab, "MEMS")
        self.tabs.addTab(self.lasers_tab, "Lasers")
        self.tabs.addTab(self.system_config_tab, "System Config")

        self.status_label = QLabel()
        self.setStatusBar(QStatusBar())
        self.statusBar().addWidget(self.status_label, 1)

        self.calibration_tab.calibrationLoaded.connect(self.calibrationLoaded)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._did_initial_sync:
            # Mirrors DOVER_UI's cINTERNAL_CONFIG_VALUES_MSG being sent proactively by
            # each module "on startup - UI may not be running yet" (implementation_
            # constants.py:308-310) - requesting it once here means the System Config
            # tab reflects the backend's real settings the first time the operator
            # opens this window, rather than showing default/unknown values until they
            # happen to click Connect.
            self._did_initial_sync = True
            self.bridge.request_internal_config()

    def send_quit_on_exit(self) -> None:
        """Called from RegionDesignerWindow.closeEvent - see SystemConfigTab's own
        send_quit_on_exit docstring."""
        self.system_config_tab.send_quit_on_exit()

    def _on_connect_clicked(self):
        self.bridge.discover_stages()
        self.bridge.discover_mems()
        self.bridge.request_internal_config()
        self.update_status_bar("Connect requested.")

    def update_status_bar(self, text: str) -> None:
        self.status_label.setText(execution_time_stamp() + text)

    def set_motion_locked(self, locked: bool) -> None:
        """Mirrors DOVER_UI's disable_tabs()/enable_tabs(): while a jog/section-move/
        focus-adjustment motion is in flight, every OTHER tab is locked out so a
        colliding command can't be sent from elsewhere while the stage is moving.
        Simpler than DOVER_UI's own per-source-tab subset locking (it only disables
        some tab combinations depending on which tab started the motion) - locking
        every other tab is strictly safer and enable_tabs() already unlocks
        everything regardless of what was locked, so nothing depends on the subset
        being narrower than this."""
        self._in_motion = locked
        current = self.tabs.currentWidget()
        for i in range(self.tabs.count()):
            self.tabs.setTabEnabled(i, (not locked) or self.tabs.widget(i) is current)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
