"""Render the app icon to assets/icon.ico (used for the .exe)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QGuiApplication  # noqa: E402

from lossless_merger.ui.theme import app_icon_image  # noqa: E402

app = QGuiApplication(["x", "-platform", "offscreen"])
out = ROOT / "assets" / "icon.ico"
out.parent.mkdir(exist_ok=True)
if not app_icon_image(256).save(str(out), "ICO"):
    # Fallback: PNG renamed is accepted by PyInstaller if Pillow is present
    app_icon_image(256).save(str(out.with_suffix(".png")), "PNG")
    sys.exit("ICO writer not available; wrote PNG instead")
print(f"Wrote {out}")
