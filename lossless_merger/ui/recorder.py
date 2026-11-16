"""Voice-over recording from the microphone."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import (QAudioInput, QMediaCaptureSession, QMediaDevices, QMediaFormat,
                                  QMediaRecorder)

from ..appdirs import sub


class VoiceRecorder(QObject):
    finished = Signal(object)          # Path of the recording, or None on failure
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = QMediaCaptureSession(self)
        self.input = QAudioInput(self)
        self.session.setAudioInput(self.input)
        self.rec = QMediaRecorder(self)
        self.session.setRecorder(self.rec)
        fmt = QMediaFormat()
        fmt.setFileFormat(QMediaFormat.FileFormat.MPEG4Audio)
        fmt.setAudioCodec(QMediaFormat.AudioCodec.AAC)
        self.rec.setMediaFormat(fmt)
        self.rec.setQuality(QMediaRecorder.Quality.HighQuality)
        self.rec.recorderStateChanged.connect(self._state)
        self.rec.errorOccurred.connect(lambda _e, msg: self.failed.emit(msg))
        self.path: Path | None = None

    @staticmethod
    def has_microphone() -> bool:
        return bool(QMediaDevices.audioInputs())

    def start(self) -> Path:
        self.input.setDevice(QMediaDevices.defaultAudioInput())
        self.path = sub("voiceovers") / f"voice_{datetime.now():%Y%m%d_%H%M%S}.m4a"
        self.rec.setOutputLocation(QUrl.fromLocalFile(str(self.path)))
        self.rec.record()
        return self.path

    def stop(self):
        self.rec.stop()

    def recording(self) -> bool:
        return self.rec.recorderState() == QMediaRecorder.RecorderState.RecordingState

    def _state(self, st):
        if st == QMediaRecorder.RecorderState.StoppedState and self.path is not None:
            loc = self.rec.actualLocation().toLocalFile()
            path = Path(loc) if loc else self.path
            self.path = None
            self.finished.emit(path if path.exists() else None)
