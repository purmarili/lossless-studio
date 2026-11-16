"""Background work: probing, thumbnails and the merge itself."""

from __future__ import annotations

import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThread, Signal
from PySide6.QtGui import QImage

from .. import merger
from ..media import Clip, probe, thumbnail_png


class Bus(QObject):
    """Lives in the GUI thread; worker threads emit through it (queued)."""
    probed = Signal(int, object)              # generation, Clip
    thumbnail = Signal(int, str, object)      # generation, clip id, QImage | None
    filmstrip = Signal(str, object)           # clip id, QImage | None
    waveform = Signal(str, object)            # audio path, QImage | None


class ImportSignals(QObject):
    done = Signal(int, int, object)           # batch id, index, Clip


class ImportTask(QRunnable):
    """Read one video (ffprobe) or photo (Qt, upright) for an import batch."""

    def __init__(self, sig: ImportSignals, batch: int, index: int, path: Path):
        super().__init__()
        self.sig, self.batch, self.index, self.path = sig, batch, index, path

    def run(self):
        from ..media import IMAGE_EXTENSIONS
        try:
            if self.path.suffix.lower() in IMAGE_EXTENSIONS:
                from .mediaimport import load_photo
                clip = load_photo(self.path)
            else:
                clip = probe(self.path)
        except Exception as exc:  # never let a worker die silently
            clip = Clip(path=self.path, size=0, error=str(exc))
        self.sig.done.emit(self.batch, self.index, clip)


class WaveTask(QRunnable):
    def __init__(self, bus: Bus, path: Path):
        super().__init__()
        self.bus, self.path = bus, path

    def run(self):
        from ..media import waveform_png
        img = None
        data = waveform_png(self.path)
        if data:
            q = QImage()
            if q.loadFromData(data, "PNG"):
                img = q
        self.bus.waveform.emit(str(self.path), img)


class FilmstripTask(QRunnable):
    def __init__(self, bus: Bus, clip: Clip):
        super().__init__()
        self.bus, self.clip = bus, clip

    def run(self):
        from ..media import filmstrip_png
        img = None
        w, h = self.clip.v.display_size if self.clip.v else (16, 9)
        # enough tiles to fill a ~1100 px bar at 54 px height without stretching
        count = max(6, min(40, round(1100 / (54 * (w / max(h, 1))))))
        data = filmstrip_png(self.clip.path, self.clip.duration, count=count)
        if data:
            q = QImage()
            if q.loadFromData(data, "PNG"):
                img = q
        self.bus.filmstrip.emit(self.clip.id, img)


class ProbeTask(QRunnable):
    def __init__(self, bus: Bus, gen: int, path: Path):
        super().__init__()
        self.bus, self.gen, self.path = bus, gen, path

    def run(self):
        try:
            clip = probe(self.path)
        except Exception as exc:  # never let a worker die silently
            clip = Clip(path=self.path, size=0, error=str(exc))
        self.bus.probed.emit(self.gen, clip)


class ThumbTask(QRunnable):
    def __init__(self, bus: Bus, gen: int, clip: Clip):
        super().__init__()
        self.bus, self.gen, self.clip = bus, gen, clip

    def run(self):
        img = None
        src = (self.clip.render_path or self.clip.path) if self.clip.is_image else self.clip.path
        data = thumbnail_png(src, self.clip.duration)
        if data:
            q = QImage()
            if q.loadFromData(data, "PNG"):
                img = q
        self.bus.thumbnail.emit(self.gen, self.clip.id, img)


class MergeThread(QThread):
    progress = Signal(float, object, str)     # fraction, speed, status
    succeeded = Signal(object)                # MergeResult
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, clips: list[Clip], output: Path, target=None, smart=None, job=None):
        """target: EncodeTarget for a full re-encode; smart: (convert_ids, fps,
        method, reference) to re-encode only some clips. Neither: lossless.
        job: callable(on_progress, cancel_event) -> MergeResult (edited export)."""
        super().__init__()
        self.clips, self.output, self.target, self.smart = clips, output, target, smart
        self.job = job
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        cb = lambda f, s, status: self.progress.emit(f, s, status)  # noqa: E731
        try:
            if self.job is not None:
                res = self.job(cb, self.cancel_event)
            elif self.smart is not None:
                ids, fps, method, ref = self.smart
                res = merger.merge_smart(self.clips, self.output, ids, fps, method, cb,
                                         self.cancel_event, ref)
            elif self.target is None:
                res = merger.merge_lossless(self.clips, self.output, cb, self.cancel_event)
            else:
                res = merger.merge_reencode(self.clips, self.output, self.target, cb, self.cancel_event)
            self.succeeded.emit(res)
        except merger.Cancelled:
            self.cancelled.emit()
        except merger.MergeError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=3)}")
