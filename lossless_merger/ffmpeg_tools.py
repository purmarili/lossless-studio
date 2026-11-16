"""Locating and running the FFmpeg / FFprobe binaries.

All processes are started with argument lists (never through a shell), so
paths containing Unicode characters (Georgian, Russian, ...) or spaces are
passed to FFmpeg unchanged via CreateProcessW.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

# Prevent a console window from flashing up for every FFmpeg call when the
# app runs as a windowed executable.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FFmpegNotFound(RuntimeError):
    pass


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        meipass = Path(getattr(sys, "_MEIPASS", exe_dir))
        dirs += [meipass / "ffmpeg", exe_dir / "ffmpeg", exe_dir]
    project_root = Path(__file__).resolve().parent.parent
    dirs += [project_root / "ffmpeg", project_root / "bin"]
    return dirs


@lru_cache(maxsize=None)
def find_binary(name: str) -> str:
    """Return the full path of ``ffmpeg`` / ``ffprobe``.

    Search order: bundled copy, project ``ffmpeg`` folder, then ``PATH``.
    An explicit override can be given with ``LVM_FFMPEG_DIR``.
    """
    exe = f"{name}.exe" if os.name == "nt" else name
    override = os.environ.get("LVM_FFMPEG_DIR")
    dirs = ([Path(override)] if override else []) + _candidate_dirs()
    for d in dirs:
        p = d / exe
        if p.is_file():
            return str(p)
    found = shutil.which(name)
    if found:
        return found
    raise FFmpegNotFound(
        f"{exe} was not found. Place FFmpeg in the 'ffmpeg' folder next to the "
        f"application or add it to PATH."
    )


def whisper_path() -> str | None:
    """whisper-cli (speech to text) shipped in the ``whisper`` folder, if present."""
    roots = [Path(__file__).resolve().parent.parent]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        roots = [exe_dir, Path(getattr(sys, "_MEIPASS", exe_dir))] + roots
    for r in roots:
        p = r / "whisper" / "whisper-cli.exe"
        if p.is_file():
            return str(p)
    return shutil.which("whisper-cli")


def ffmpeg_path() -> str:
    return find_binary("ffmpeg")


def ffprobe_path() -> str:
    return find_binary("ffprobe")


def run(args: list[str], timeout: float | None = None, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """Run a short-lived FFmpeg/FFprobe command and capture its output (bytes)."""
    return subprocess.run(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def popen(args: list[str], cwd: str | Path | None = None) -> subprocess.Popen:
    """Start a long-running FFmpeg process with piped stdout/stderr."""
    return subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
