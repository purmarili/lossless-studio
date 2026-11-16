"""Per-user folders for caches, speech models and voice-over recordings."""

from __future__ import annotations

import os
from pathlib import Path

FOLDER = "LosslessStudio"
OLD_FOLDER = "LosslessVideoMerger"          # name before the app was renamed


def data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
    d = base / FOLDER
    old = base / OLD_FOLDER
    if not d.exists() and old.exists():
        try:
            old.rename(d)                   # keep downloaded speech models, recordings …
        except OSError:
            pass
    d.mkdir(parents=True, exist_ok=True)
    return d


def sub(name: str) -> Path:
    d = data_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d
