"""Loading photos (upright, as phones store them) for the timeline."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtGui import QImageIOHandler, QImageReader

from ..appdirs import sub
from ..media import Clip, image_clip

FFMPEG_READS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def load_photo(path: Path) -> Clip:
    """Clip for a photo. EXIF orientation is applied (an upright PNG copy is
    cached when needed, because FFmpeg ignores the orientation tag)."""
    path = Path(path)
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    img = reader.read()
    if img.isNull():
        c = image_clip(path, 16, 9)
        c.error = f"Can't read this picture: {reader.errorString()}"
        return c
    w, h = img.width(), img.height()
    render_path = path
    rotated = reader.transformation() != QImageIOHandler.Transformation.TransformationNone
    if rotated or path.suffix.lower() not in FFMPEG_READS:
        st = path.stat()
        key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}".encode("utf-8")).hexdigest()[:20]
        render_path = sub("cache") / f"photo_{key}.png"
        if not render_path.exists():
            img.save(str(render_path), "PNG")
    return image_clip(path, w, h, render_path)
