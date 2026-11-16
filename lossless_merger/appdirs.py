"""Per-user folders for caches, speech models and voice-over recordings."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / "LosslessVideoMerger"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sub(name: str) -> Path:
    d = data_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d
