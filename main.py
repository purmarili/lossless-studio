"""Lossless Video Merger - entry point."""

import sys
from pathlib import Path


def main() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QGuiApplication
    from PySide6.QtWidgets import QApplication

    from lossless_merger import APP_NAME, APP_VERSION
    from lossless_merger.ui import theme
    from lossless_merger.ui.main_window import MainWindow

    if sys.platform == "win32":
        try:  # own taskbar icon/group instead of python.exe's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LosslessVideoMerger.App")
        except Exception:
            pass

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(theme.STYLESHEET)
    app.setWindowIcon(theme.app_icon())

    win = MainWindow()
    win.show()
    # Optional: open a folder passed on the command line (or dropped on the exe)
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_dir():
        win.load_folder(Path(sys.argv[1]))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
