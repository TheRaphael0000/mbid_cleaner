"""Entry point: python main.py"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from app.ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MBID Cleaner")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
