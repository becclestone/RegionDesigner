"""Entry point for the paintbrush region/focus-point designer.

Run directly (`python region_designer_main.py`) from within this directory -
Python adds a script's own directory to sys.path automatically, so the sibling
Constants/, Utilities/, and IsMsgPy/ packages resolve without extra setup.
"""
import sys

from PySide6.QtWidgets import QApplication

from main_window import RegionDesignerWindow


def main():
    app = QApplication(sys.argv)
    window = RegionDesignerWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
