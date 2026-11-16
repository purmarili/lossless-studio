"""Choose how a 360° clip becomes a normal video (direction, zoom, tiny planet)."""

from __future__ import annotations

import itertools

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QDoubleSpinBox, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from .. import reframe
from ..media import Clip
from .inspector import _chip, _label, _Slider
from .preview import fmt_time


class _Sig(QObject):
    done = Signal(int, object)


class _Task(QRunnable):
    def __init__(self, sig, rid, clip, view, t):
        super().__init__()
        self.sig, self.rid, self.clip, self.view, self.t = sig, rid, clip, view, t

    def run(self):
        img = None
        data = reframe.preview_png(self.clip, self.view, self.t, 720)
        if data:
            q = QImage()
            if q.loadFromData(data, "PNG"):
                img = q
        self.sig.done.emit(self.rid, img)


class ReframeDialog(QDialog):
    def __init__(self, parent, clip: Clip, view: reframe.View360 | None = None, count: int = 1):
        super().__init__(parent)
        self.clip = clip
        self.view = view or reframe.View360()
        self.setWindowTitle("360° video")
        self.setMinimumWidth(760)
        self._ids = itertools.count(1)
        self._rid = 0
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.sig = _Sig()
        self.sig.done.connect(self._on_preview)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._request)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(10)
        t = QLabel(f"360° video — choose your view")
        t.setObjectName("dialogTitle")
        lay.addWidget(t)
        raw = clip.projection in ("dfisheye", "dfisheye2")
        lay.addWidget(_label(
            f"{clip.name} was filmed in 360°. Pick where to look; it becomes a normal video you can edit. "
            + ("The two lenses are stitched quickly here — Insta360 Studio stitches seams more precisely "
               "(its exported MP4s work here too)." if raw else ""), "muted"))
        self.image = QLabel("Rendering preview…")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumHeight(330)
        self.image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.image.setStyleSheet("background:#07080a; border-radius:10px;")
        lay.addWidget(self.image, 1)

        row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        for key, label in reframe.MODES.items():
            b = _chip(label)
            b.setProperty("key", key)
            b.setChecked(key == self.view.mode)
            b.clicked.connect(lambda _=False, k=key: self._set("mode", k))
            self.mode_group.addButton(b)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        self.time = _Slider("Frame", 0, max(1, int(clip.duration * 10)), 10, lambda v: fmt_time(v))
        self.time.set(min(1.0, clip.duration / 2))
        self.time.moved.connect(lambda _: self.timer.start())
        self.yaw = _Slider("Left ↔ right", -180, 180, 1, lambda v: f"{v:+.0f}°")
        self.pitch = _Slider("Down ↕ up", -90, 90, 1, lambda v: f"{v:+.0f}°")
        self.roll = _Slider("Tilt", -45, 45, 1, lambda v: f"{v:+.0f}°")
        self.fov = _Slider("Zoom (view)", 40, 140, 1, lambda v: f"{v:.0f}°")
        lay.addWidget(self.time)
        for s, key in ((self.yaw, "yaw"), (self.pitch, "pitch"), (self.roll, "roll"), (self.fov, "fov")):
            s.set(getattr(self.view, key))
            s.moved.connect(lambda v, k=key: self._set(k, v))
            lay.addWidget(s)

        self.lens_box = QWidget()
        lb = QHBoxLayout(self.lens_box)
        lb.setContentsMargins(0, 0, 0, 0)
        self.swap = QCheckBox("Swap front / back lens")
        self.swap.setChecked(self.view.swap)
        self.swap.toggled.connect(lambda c: self._set("swap", c))
        lb.addWidget(self.swap)
        lb.addSpacing(16)
        lb.addWidget(QLabel("Lens angle"))
        self.lens = QDoubleSpinBox()
        self.lens.setRange(180, 220)
        self.lens.setDecimals(0)
        self.lens.setSuffix("°")
        self.lens.setValue(self.view.lens_fov)
        self.lens.valueChanged.connect(lambda v: self._set("lens_fov", v))
        lb.addWidget(self.lens)
        lb.addStretch(1)
        self.lens_box.setVisible(raw)
        lay.addWidget(self.lens_box)

        self.all = QCheckBox(f"Use this view for all {count} 360° clips")
        self.all.setVisible(count > 1)
        lay.addWidget(self.all)
        self.size_note = _label("", "hint")
        lay.addWidget(self.size_note)

        brow = QHBoxLayout()
        brow.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        go = QPushButton("Convert to normal video")
        go.setObjectName("primary")
        go.setDefault(True)
        go.clicked.connect(self.accept)
        brow.addWidget(cancel)
        brow.addWidget(go)
        lay.addLayout(brow)
        self._update_note()
        self._request()

    def _set(self, key, value):
        setattr(self.view, key, value)
        self.fov.setEnabled(self.view.mode != "planet")
        self._update_note()
        self.timer.start()

    def _update_note(self):
        w, h = reframe.out_size(self.clip, self.view)
        self.size_note.setText(f"Result: {w}×{h} MP4 (very high quality). Your original file is not changed.")

    def _request(self):
        self._rid = next(self._ids)
        v = reframe.View360(**self.view.__dict__)
        self.pool.start(_Task(self.sig, self._rid, self.clip, v, self.time.slider.value() / 10))

    def _on_preview(self, rid, img):
        if rid != self._rid:
            return
        if img is None:
            self.image.setText("Preview not available")
            return
        pm = QPixmap.fromImage(img)
        self.image.setPixmap(pm.scaled(self.image.width(), self.image.height(), Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation))

    def result_view(self) -> tuple[reframe.View360, bool] | None:
        if self.exec() != QDialog.DialogCode.Accepted:
            return None
        return self.view, self.all.isChecked()
