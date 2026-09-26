from PySide6.QtWidgets import (
    QMainWindow, QToolBar, QSpinBox, QPushButton, QLabel, QMessageBox
)

from canvas_view import SectionCanvas
from controller_bridge import ControllerBridge
import region_clustering as clustering

_DEFAULT_TARGET_REGION_SIZE = 75
_DEFAULT_FOCUS_POINTS_PER_REGION = 4


class RegionDesignerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Region && Focus Point Designer")
        self.resize(1200, 800)

        self.canvas = SectionCanvas(self)
        self.setCentralWidget(self.canvas)

        self.bridge = ControllerBridge()
        self.bridge.imageReady.connect(self._on_image_ready)
        self.bridge.commandError.connect(self._on_command_error)

        self._build_toolbar()

        self.bridge.connect_to_controller()

    def _build_toolbar(self):
        toolbar = QToolBar("Tools", self)
        self.addToolBar(toolbar)

        toolbar.addWidget(QLabel(" Brush radius (sections): "))
        self.brush_spin = QSpinBox()
        self.brush_spin.setRange(0, 25)
        self.brush_spin.setValue(self.canvas.brush_radius)
        self.brush_spin.valueChanged.connect(self._on_brush_radius_changed)
        toolbar.addWidget(self.brush_spin)

        toolbar.addSeparator()

        snap_btn = QPushButton("Snap Image")
        snap_btn.clicked.connect(self._on_snap_clicked)
        toolbar.addWidget(snap_btn)

        toolbar.addSeparator()

        toolbar.addWidget(QLabel(" Target sections/region: "))
        self.region_size_spin = QSpinBox()
        self.region_size_spin.setRange(1, 2000)
        self.region_size_spin.setValue(_DEFAULT_TARGET_REGION_SIZE)
        toolbar.addWidget(self.region_size_spin)

        toolbar.addWidget(QLabel(" Focus points/region: "))
        self.focus_points_spin = QSpinBox()
        self.focus_points_spin.setRange(1, 50)
        self.focus_points_spin.setValue(_DEFAULT_FOCUS_POINTS_PER_REGION)
        toolbar.addWidget(self.focus_points_spin)

        compile_btn = QPushButton("Compile Regions")
        compile_btn.clicked.connect(self._on_compile_regions_clicked)
        toolbar.addWidget(compile_btn)

    def _on_brush_radius_changed(self, value: int):
        self.canvas.brush_radius = value

    def _on_snap_clicked(self):
        self.bridge.request_snap()

    def _on_image_ready(self, image_path: str):
        self.canvas.set_background_image(image_path)

    def _on_command_error(self, message: str):
        QMessageBox.warning(self, "Controller error", message)

    def _on_compile_regions_clicked(self):
        sections = list(self.canvas.painted)
        if not sections:
            QMessageBox.information(self, "Compile Regions", "Paint an area first.")
            return

        target_size = self.region_size_spin.value()
        region_of = clustering.assign_regions(sections, target_size)

        by_region: dict[int, list[tuple[int, int]]] = {}
        for section, region_id in region_of.items():
            by_region.setdefault(region_id, []).append(section)

        self.canvas.apply_regions(region_of)
        self.canvas.clear_focus_points()

        num_points = self.focus_points_spin.value()
        for region_id, region_sections in by_region.items():
            for row, col in clustering.place_focus_points(region_sections, num_points):
                self.canvas.add_focus_point(region_id, row, col)

    def closeEvent(self, event):
        self.bridge.shutdown()
        super().closeEvent(event)
