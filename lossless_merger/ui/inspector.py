"""Edit panel: shows the controls for whatever is selected on the timeline."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QSlider,
                               QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from ..media import fmt_duration
from ..project import (BACKGROUNDS, CANVASES, LOOKS, PRESETS, SPEEDS, TEXT_ANIMS_IN, TEXT_ANIMS_OUT,
                       TEXT_POSITIONS, TEXT_STYLES, TRANSITIONS, AudioItem, OverlayItem, Project, Segment,
                       TextItem, Transform)
from .preview import fmt_time

TEXT_COLORS = [("#ffffff", "White"), ("#ffd84d", "Yellow"), ("#111111", "Black"), ("#7c8cff", "Blue"),
               ("#ff6b8b", "Pink"), ("#5ad1c6", "Teal"), ("#ff8a3d", "Orange")]


def _label(text, name="muted"):
    lab = QLabel(text)
    lab.setObjectName(name)
    lab.setWordWrap(True)
    return lab


def _chip(text, checkable=True):
    b = QPushButton(text.replace("&", "&&"))     # '&' would become a keyboard mnemonic
    b.setObjectName("chip")
    b.setCheckable(checkable)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class _Slider(QWidget):
    """Label + slider + value text. Values are floats (int slider / scale)."""
    moved = Signal(float)

    def __init__(self, title, lo, hi, scale, fmt):
        super().__init__()
        self.scale, self.fmt = scale, fmt
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.title = QLabel(title)
        self.title.setMinimumWidth(86)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(lo, hi)
        self.value = QLabel("")
        self.value.setObjectName("valueLabel")
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.title)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.value)
        self.slider.valueChanged.connect(self._changed)

    def _changed(self, v):
        self.value.setText(self.fmt(v / self.scale))
        if not self.slider.signalsBlocked():
            self.moved.emit(v / self.scale)

    def set(self, value: float):
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(value * self.scale)))
        self.slider.blockSignals(False)
        self.value.setText(self.fmt(value))


def _spin(lo, hi, suffix=" s", decimals=1, special=None):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSuffix(suffix)
    s.setSingleStep(0.1)
    if special:
        s.setSpecialValueText(special)
    return s


def _scroll(w: QWidget) -> QScrollArea:
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.Shape.NoFrame)
    sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    sc.setWidget(w)
    return sc


def _page():
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(4, 12, 12, 8)
    lay.setSpacing(10)
    return w, lay


class _Base(QWidget):
    changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.loading = False
        self.project: Project | None = None

    def emit(self, key):
        if not self.loading:
            self.changed.emit(key)


# ============================================================== segment
class SegmentPage(_Base):
    def __init__(self):
        super().__init__()
        self.seg: Segment | None = None
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        self.tabs = tabs
        tabs.addTab(_scroll(self._basic()), "Clip")
        tabs.addTab(_scroll(self._look()), "Look")
        tabs.addTab(_scroll(self._effects()), "Effects")
        tabs.addTab(_scroll(self._zoom()), "Zoom")
        tabs.addTab(_scroll(self._audio()), "Audio")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.title = _label("", "sectionTitle")
        lay.addWidget(self.title)
        lay.addWidget(tabs, 1)

    def _set(self, key, value, reload=False):
        if self.loading or self.seg is None:
            return
        setattr(self.seg.edit, key, value)
        if reload:
            self.load(self.seg, self.project)
        else:
            self._info()
        self.emit(key)

    # ---- tabs ----
    def _basic(self):
        w, lay = _page()
        self.info = _label("", "muted")
        lay.addWidget(self.info)
        self.still_row = QWidget()
        sr = QHBoxLayout(self.still_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.addWidget(QLabel("Show for"))
        self.still_len = _spin(0.3, 600)
        self.still_len.valueChanged.connect(self._still_len)
        sr.addWidget(self.still_len)
        sr.addStretch(1)
        lay.addWidget(self.still_row)
        self.speed_box = QWidget()
        sb = QVBoxLayout(self.speed_box)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.addWidget(_label("Speed", "sectionTitle"))
        row = QGridLayout()
        row.setSpacing(5)
        self.speed_group = QButtonGroup(self)
        for i, s in enumerate(SPEEDS):
            b = _chip(f"{s:g}×")
            b.setProperty("speed", s)
            self.speed_group.addButton(b)
            b.clicked.connect(lambda _=False, s=s: self._set("speed", s))
            row.addWidget(b, i // 5, i % 5)
        sb.addLayout(row)
        self.reverse = QCheckBox("Play backwards (reverse)")
        self.reverse.toggled.connect(lambda c: self._set("reverse", c))
        sb.addWidget(self.reverse)
        lay.addWidget(self.speed_box)
        lay.addWidget(_label("Trim by dragging the clip's edges on the timeline, or press I / O at the "
                             "playhead. S splits, F adds a freeze frame. Trimming stays lossless: only "
                             "about a second at each cut is re-encoded.", "hint"))
        lay.addStretch(1)
        return w

    def _look(self):
        w, lay = _page()
        grid = QGridLayout()
        grid.setSpacing(6)
        self.look_group = QButtonGroup(self)
        for i, (key, (label, *_)) in enumerate(LOOKS.items()):
            b = _chip(label)
            b.setProperty("look", key)
            b.clicked.connect(lambda _=False, k=key: self._look_preset(k))
            self.look_group.addButton(b)
            grid.addWidget(b, i // 4, i % 4)
        lay.addLayout(grid)
        self.bright = _Slider("Brightness", -50, 50, 100, lambda v: f"{v * 100:+.0f}")
        self.contrast = _Slider("Contrast", 50, 200, 100, lambda v: f"{v * 100:.0f} %")
        self.sat = _Slider("Saturation", 0, 200, 100, lambda v: f"{v * 100:.0f} %")
        self.warm = _Slider("Warmth", -100, 100, 100, lambda v: f"{v * 100:+.0f}")
        for s, key in ((self.bright, "brightness"), (self.contrast, "contrast"),
                       (self.sat, "saturation"), (self.warm, "warmth")):
            s.moved.connect(lambda v, k=key: self._set(k, v))
            lay.addWidget(s)
        row = QHBoxLayout()
        rl, rr, fl = QPushButton("⟲  Rotate left"), QPushButton("⟳  Rotate right"), QPushButton("⇋  Mirror")
        rl.clicked.connect(lambda: self._set("rotate", (self.seg.edit.rotate - 90) % 360))
        rr.clicked.connect(lambda: self._set("rotate", (self.seg.edit.rotate + 90) % 360))
        fl.clicked.connect(lambda: self._set("flip", not self.seg.edit.flip))
        for b in (rl, rr, fl):
            row.addWidget(b)
        row.addStretch(1)
        reset = QPushButton("Reset")
        reset.setObjectName("ghost")
        reset.clicked.connect(self._reset_look)
        row.addWidget(reset)
        lay.addLayout(row)
        lay.addWidget(_label("Exact colours are shown when the preview is paused. HDR stays HDR.", "hint"))
        lay.addStretch(1)
        return w

    def _effects(self):
        w, lay = _page()
        self.sharpen = _Slider("Sharpen", 0, 100, 100, lambda v: f"{v * 100:.0f}")
        self.vignette = _Slider("Vignette", 0, 100, 100, lambda v: f"{v * 100:.0f}")
        self.blur = _Slider("Blur", 0, 100, 100, lambda v: f"{v * 100:.0f}")
        self.fade_in = _Slider("Fade in", 0, 30, 10, lambda v: f"{v:.1f} s")
        self.fade_out = _Slider("Fade out", 0, 30, 10, lambda v: f"{v:.1f} s")
        for s, key in ((self.sharpen, "sharpen"), (self.vignette, "vignette"), (self.blur, "blur"),
                       (self.fade_in, "fade_in"), (self.fade_out, "fade_out")):
            s.moved.connect(lambda v, k=key: self._set(k, v))
            lay.addWidget(s)
        self.stab = QCheckBox("Stabilize shaky footage")
        self.stab.toggled.connect(lambda c: self._set("stabilize", c))
        lay.addWidget(self.stab)
        lay.addWidget(_label("Fades go from / to black and include the sound. Stabilizing analyses the "
                             "clip first (slow for 4K), zooms in slightly, and works in 8-bit.", "hint"))
        lay.addStretch(1)
        return w

    def _zoom(self):
        w, lay = _page()
        lay.addWidget(_label("Framing", "sectionTitle"))
        self.z0 = _Slider("Zoom", 100, 300, 100, lambda v: f"{v * 100:.0f} %")
        self.x0 = _Slider("Left ↔ right", -100, 100, 100, lambda v: f"{v * 100:+.0f}")
        self.y0 = _Slider("Up ↕ down", -100, 100, 100, lambda v: f"{v * 100:+.0f}")
        for s, f in ((self.z0, "zoom"), (self.x0, "x"), (self.y0, "y")):
            s.moved.connect(lambda v, f=f: self._tf("tf", f, v))
            lay.addWidget(s)
        self.kb = QCheckBox("Animate to a second framing (Ken Burns)")
        self.kb.toggled.connect(self._toggle_kb)
        lay.addWidget(self.kb)
        self.end_box = QWidget()
        eb = QVBoxLayout(self.end_box)
        eb.setContentsMargins(0, 0, 0, 0)
        self.z1 = _Slider("End zoom", 100, 300, 100, lambda v: f"{v * 100:.0f} %")
        self.x1 = _Slider("End ↔", -100, 100, 100, lambda v: f"{v * 100:+.0f}")
        self.y1 = _Slider("End ↕", -100, 100, 100, lambda v: f"{v * 100:+.0f}")
        for s, f in ((self.z1, "zoom"), (self.x1, "x"), (self.y1, "y")):
            s.moved.connect(lambda v, f=f: self._tf("tf_end", f, v))
            eb.addWidget(s)
        lay.addWidget(self.end_box)
        row = QHBoxLayout()
        for label, a, b in (("Slow zoom in", Transform(1.0), Transform(1.25)),
                            ("Slow zoom out", Transform(1.25), Transform(1.0)),
                            ("Pan →", Transform(1.25, -1, 0), Transform(1.25, 1, 0)),
                            ("Pan ←", Transform(1.25, 1, 0), Transform(1.25, -1, 0))):
            btn = _chip(label, checkable=False)
            btn.clicked.connect(lambda _=False, a=a, b=b: self._kb_preset(a, b))
            row.addWidget(btn)
        row.addStretch(1)
        lay.addLayout(row)
        reset = QPushButton("Reset framing")
        reset.setObjectName("ghost")
        reset.clicked.connect(lambda: self._kb_preset(Transform(), None))
        lay.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addStretch(1)
        return w

    def _audio(self):
        w, lay = _page()
        self.volume = _Slider("Volume", 0, 200, 100, lambda v: f"{v * 100:.0f} %")
        self.volume.moved.connect(lambda v: self._set("volume", v))
        lay.addWidget(self.volume)
        self.mute = QCheckBox("Mute")
        self.mute.toggled.connect(lambda c: self._set("muted", c))
        self.denoise = QCheckBox("Reduce background noise")
        self.denoise.toggled.connect(lambda c: self._set("denoise", c))
        lay.addWidget(self.mute)
        lay.addWidget(self.denoise)
        self.afi = _Slider("Fade in", 0, 50, 10, lambda v: f"{v:.1f} s")
        self.afo = _Slider("Fade out", 0, 50, 10, lambda v: f"{v:.1f} s")
        self.afi.moved.connect(lambda v: self._set("audio_fade_in", v))
        self.afo.moved.connect(lambda v: self._set("audio_fade_out", v))
        lay.addWidget(self.afi)
        lay.addWidget(self.afo)
        lay.addWidget(_label("Changing the sound mixes it into one AAC track (extra tracks such as spatial "
                             "audio are left out); the picture stays lossless.", "hint"))
        lay.addStretch(1)
        return w

    # ---- actions ----
    def _still_len(self, v):
        if self.loading or self.seg is None:
            return
        self.seg.still_len = v
        self._info()
        self.emit("still_len")

    def _look_preset(self, key):
        if self.seg is None:
            return
        e = self.seg.edit
        e.look = key
        e.brightness, e.contrast, e.saturation, e.warmth = 0.0, 1.0, 1.0, 0.0
        self.load(self.seg, self.project)
        self.emit("look")

    def _reset_look(self):
        if self.seg is None:
            return
        e = self.seg.edit
        e.look, e.brightness, e.contrast, e.saturation, e.warmth = "none", 0.0, 1.0, 1.0, 0.0
        e.rotate, e.flip = 0, False
        self.load(self.seg, self.project)
        self.emit("look.reset")

    def _tf(self, which, field, v):
        if self.loading or self.seg is None:
            return
        tf = getattr(self.seg.edit, which)
        if tf is None:
            return
        setattr(tf, field, v)
        self.emit("zoom")

    def _toggle_kb(self, on):
        if self.loading or self.seg is None:
            return
        e = self.seg.edit
        e.tf_end = Transform(e.tf.zoom * 1.2 if on else 1, e.tf.x, e.tf.y) if on else None
        self.load(self.seg, self.project)
        self.emit("zoom")

    def _kb_preset(self, a, b):
        if self.seg is None:
            return
        self.seg.edit.tf, self.seg.edit.tf_end = a, b
        self.load(self.seg, self.project)
        self.emit("zoom")

    # ---- loading ----
    def _info(self):
        s = self.seg
        if s is None:
            return
        if s.is_still:
            what = "Photo" if s.kind == "image" else f"Freeze frame at {fmt_time(s.still_at)}"
            self.info.setText(f"{what}  ·  {fmt_time(s.length)} on the timeline")
        else:
            self.info.setText(f"Uses {fmt_time(s.src_in)} – {fmt_time(s.src_out)} of {fmt_duration(s.clip.duration)}"
                              f"   ·   {fmt_time(s.length)} on the timeline")

    def load(self, seg: Segment, project: Project):
        self.seg, self.project = seg, project
        self.loading = True
        try:
            e = seg.edit
            self.title.setText(seg.clip.name)
            self._info()
            self.still_row.setVisible(seg.is_still)
            self.still_len.setValue(seg.still_len)
            video = seg.kind == "video"
            self.speed_box.setVisible(video)
            self.tabs.setTabEnabled(4, video)
            for b in self.speed_group.buttons():
                b.setChecked(abs(b.property("speed") - e.speed) < 1e-6)
            self.reverse.setChecked(e.reverse)
            for b in self.look_group.buttons():
                b.setChecked(b.property("look") == e.look)
            self.bright.set(e.brightness)
            self.contrast.set(e.contrast)
            self.sat.set(e.saturation)
            self.warm.set(e.warmth)
            self.sharpen.set(e.sharpen)
            self.vignette.set(e.vignette)
            self.blur.set(e.blur)
            self.fade_in.set(e.fade_in)
            self.fade_out.set(e.fade_out)
            self.stab.setChecked(e.stabilize)
            self.stab.setEnabled(video)
            self.z0.set(e.tf.zoom)
            self.x0.set(e.tf.x)
            self.y0.set(e.tf.y)
            self.kb.setChecked(e.tf_end is not None)
            self.end_box.setVisible(e.tf_end is not None)
            if e.tf_end is not None:
                self.z1.set(e.tf_end.zoom)
                self.x1.set(e.tf_end.x)
                self.y1.set(e.tf_end.y)
            self.volume.set(e.volume)
            self.volume.setEnabled(not e.muted)
            self.mute.setChecked(e.muted)
            self.denoise.setChecked(e.denoise)
            self.afi.set(e.audio_fade_in)
            self.afo.set(e.audio_fade_out)
        finally:
            self.loading = False


# ============================================================ transition
class TransitionPage(_Base):
    to_all = Signal()

    def __init__(self):
        super().__init__()
        self.seg: Segment | None = None
        w, lay = _page()
        lay.addWidget(_label("Transition", "sectionTitle"))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.group = QButtonGroup(self)
        for i, (key, label) in enumerate(TRANSITIONS.items()):
            b = _chip(label)
            b.setProperty("key", key)
            b.clicked.connect(lambda _=False, k=key: self._set("transition", k))
            self.group.addButton(b)
            grid.addWidget(b, i // 3, i % 3)
        lay.addLayout(grid)
        self.dur = _Slider("Duration", 2, 20, 10, lambda v: f"{v:.1f} s")
        self.dur.moved.connect(lambda v: self._set("transition_dur", v))
        lay.addWidget(self.dur)
        allb = QPushButton("Use for all clips")
        allb.clicked.connect(self.to_all)
        lay.addWidget(allb, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(_label("Clips overlap during a transition, so the video gets a little shorter. Only "
                             "the overlap is re-encoded; the sound crossfades too.", "hint"))
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(w))

    def _set(self, key, value):
        if self.loading or self.seg is None:
            return
        setattr(self.seg.edit, key, value)
        self.dur.setEnabled(self.seg.edit.transition != "none")
        self.emit("transition")

    def load(self, seg: Segment, project: Project):
        self.seg, self.project = seg, project
        self.loading = True
        for b in self.group.buttons():
            b.setChecked(b.property("key") == seg.edit.transition)
        self.dur.set(seg.edit.transition_dur)
        self.dur.setEnabled(seg.edit.transition != "none")
        self.loading = False


# ================================================================= text
class TextPage(_Base):
    style_to_all = Signal()

    def __init__(self):
        super().__init__()
        self.item: TextItem | None = None
        w, lay = _page()
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("Type your text…")
        self.edit.setMaximumHeight(70)
        self.edit.textChanged.connect(lambda: self._set("text", self.edit.toPlainText()))
        lay.addWidget(self.edit)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.style_group = QButtonGroup(self)
        for i, (key, label) in enumerate(TEXT_STYLES.items()):
            b = _chip(label)
            b.setProperty("key", key)
            b.clicked.connect(lambda _=False, k=key: self._set("style", k))
            self.style_group.addButton(b)
            grid.addWidget(b, i // 3, i % 3)
        lay.addLayout(grid)
        row = QHBoxLayout()
        self.color_group = QButtonGroup(self)
        for col, name in TEXT_COLORS:
            b = QPushButton("")
            b.setCheckable(True)
            b.setToolTip(name)
            b.setFixedSize(24, 24)
            b.setProperty("color", col)
            b.setStyleSheet(f"QPushButton {{ background:{col}; border:2px solid #2c3240; border-radius:12px; }}"
                            f"QPushButton:checked {{ border:2px solid #ffffff; }}")
            b.clicked.connect(lambda _=False, c=col: self._set("color", c))
            self.color_group.addButton(b)
            row.addWidget(b)
        row.addSpacing(10)
        self.bold = QCheckBox("Bold")
        self.bold.toggled.connect(lambda c: self._set("bold", c))
        row.addWidget(self.bold)
        row.addStretch(1)
        lay.addLayout(row)
        self.size = _Slider("Size", 2, 20, 1, lambda v: f"{v:.0f}")
        self.size.moved.connect(lambda v: self._set("size", v))
        lay.addWidget(self.size)
        prow = QHBoxLayout()
        self.pos_group = QButtonGroup(self)
        for key, label in list(TEXT_POSITIONS.items()) + [("custom", "Custom")]:
            b = _chip(label)
            b.setProperty("key", key)
            b.clicked.connect(lambda _=False, k=key: self._set("position", k, reload=True))
            self.pos_group.addButton(b)
            prow.addWidget(b)
        prow.addStretch(1)
        lay.addLayout(prow)
        self.px = _Slider("Horizontal", 0, 100, 100, lambda v: f"{v * 100:.0f} %")
        self.py = _Slider("Vertical", 0, 100, 100, lambda v: f"{v * 100:.0f} %")
        self.px.moved.connect(lambda v: self._set("x", v))
        self.py.moved.connect(lambda v: self._set("y", v))
        lay.addWidget(self.px)
        lay.addWidget(self.py)
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Starts at"))
        self.start = _spin(0, 36000)
        self.start.valueChanged.connect(lambda v: self._set("start", v))
        trow.addWidget(self.start)
        trow.addWidget(QLabel("for"))
        self.dur = _spin(0.2, 36000)
        self.dur.valueChanged.connect(lambda v: self._set("duration", v))
        trow.addWidget(self.dur)
        trow.addStretch(1)
        lay.addLayout(trow)
        arow = QHBoxLayout()
        arow.addWidget(QLabel("In"))
        self.ain = QComboBox()
        for k, v in TEXT_ANIMS_IN.items():
            self.ain.addItem(v, k)
        self.ain.currentIndexChanged.connect(lambda _: self._set("anim_in", self.ain.currentData()))
        arow.addWidget(self.ain, 1)
        arow.addWidget(QLabel("Out"))
        self.aout = QComboBox()
        for k, v in TEXT_ANIMS_OUT.items():
            self.aout.addItem(v, k)
        self.aout.currentIndexChanged.connect(lambda _: self._set("anim_out", self.aout.currentData()))
        arow.addWidget(self.aout, 1)
        lay.addLayout(arow)
        allb = QPushButton("Apply this style to all texts")
        allb.clicked.connect(self.style_to_all)
        lay.addWidget(allb, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(_label("Any language works (ქართული, Русский, emoji). Move or trim the text on the "
                             "timeline to change when it shows.", "hint"))
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(w))

    def _set(self, key, value, reload=False):
        if self.loading or self.item is None:
            return
        if key == "text" and self.item.text == value:
            return
        if key == "position" and value == "custom":
            self.item.x, self.item.y = self.item.center()
        setattr(self.item, key, value)
        if reload:
            self.load(self.item, self.project)
        self.emit("text." + key)

    def load(self, item: TextItem, project: Project):
        self.item, self.project = item, project
        self.loading = True
        if self.edit.toPlainText() != item.text:
            self.edit.setPlainText(item.text)
        for b in self.style_group.buttons():
            b.setChecked(b.property("key") == item.style)
        for b in self.color_group.buttons():
            b.setChecked(b.property("color") == item.color)
        for b in self.pos_group.buttons():
            b.setChecked(b.property("key") == item.position)
        self.bold.setChecked(item.bold)
        self.size.set(item.size)
        custom = item.position == "custom"
        self.px.setVisible(custom)
        self.py.setVisible(custom)
        self.px.set(item.x)
        self.py.set(item.y)
        self.start.setValue(item.start)
        self.dur.setValue(item.duration)
        self.ain.setCurrentIndex(max(0, self.ain.findData(item.anim_in)))
        self.aout.setCurrentIndex(max(0, self.aout.findData(item.anim_out)))
        self.loading = False


# ============================================================== overlay
class OverlayPage(_Base):
    def __init__(self):
        super().__init__()
        self.item: OverlayItem | None = None
        w, lay = _page()
        self.title = _label("", "sectionTitle")
        lay.addWidget(self.title)
        row = QHBoxLayout()
        for label, x, y, s in (("↖", 0.22, 0.24, 0.35), ("↗", 0.78, 0.24, 0.35), ("↙", 0.22, 0.76, 0.35),
                               ("↘", 0.78, 0.76, 0.35), ("Centre", 0.5, 0.5, 0.5), ("Full", 0.5, 0.5, 1.0)):
            b = _chip(label, checkable=False)
            b.clicked.connect(lambda _=False, x=x, y=y, s=s: self._place(x, y, s))
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.scale = _Slider("Size", 5, 100, 100, lambda v: f"{v * 100:.0f} %")
        self.px = _Slider("Horizontal", 0, 100, 100, lambda v: f"{v * 100:.0f} %")
        self.py = _Slider("Vertical", 0, 100, 100, lambda v: f"{v * 100:.0f} %")
        self.op = _Slider("Opacity", 5, 100, 100, lambda v: f"{v * 100:.0f} %")
        for s, k in ((self.scale, "scale"), (self.px, "x"), (self.py, "y"), (self.op, "opacity")):
            s.moved.connect(lambda v, k=k: self._set(k, v))
            lay.addWidget(s)
        self.vol = _Slider("Volume", 0, 200, 100, lambda v: f"{v * 100:.0f} %")
        self.vol.moved.connect(lambda v: self._set("volume", v))
        lay.addWidget(self.vol)
        self.mute = QCheckBox("Mute this overlay")
        self.mute.toggled.connect(lambda c: self._set("muted", c))
        lay.addWidget(self.mute)
        lay.addWidget(_label("Picture-in-picture is drawn on top of the main video. Only the seconds it "
                             "covers are re-encoded.", "hint"))
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(w))

    def _place(self, x, y, s):
        if self.item is None:
            return
        self.item.x, self.item.y, self.item.scale = x, y, s
        self.load(self.item, self.project)
        self.emit("overlay.place")

    def _set(self, key, value):
        if self.loading or self.item is None:
            return
        setattr(self.item, key, value)
        self.emit("overlay." + key)

    def load(self, item: OverlayItem, project: Project):
        self.item, self.project = item, project
        self.loading = True
        self.title.setText(f"Overlay — {item.clip.name}")
        self.scale.set(item.scale)
        self.px.set(item.x)
        self.py.set(item.y)
        self.op.set(item.opacity)
        self.vol.set(item.volume)
        video = item.kind == "video" and bool(item.clip.audio)
        self.vol.setVisible(video)
        self.mute.setVisible(video)
        self.mute.setChecked(item.muted)
        self.loading = False


# ================================================================ audio
class AudioPage(_Base):
    def __init__(self):
        super().__init__()
        self.item: AudioItem | None = None
        w, lay = _page()
        self.title = _label("", "sectionTitle")
        lay.addWidget(self.title)
        self.vol = _Slider("Volume", 0, 200, 100, lambda v: f"{v * 100:.0f} %")
        self.fi = _Slider("Fade in", 0, 100, 10, lambda v: f"{v:.1f} s")
        self.fo = _Slider("Fade out", 0, 100, 10, lambda v: f"{v:.1f} s")
        for s, k in ((self.vol, "volume"), (self.fi, "fade_in"), (self.fo, "fade_out")):
            s.moved.connect(lambda v, k=k: self._set(k, v))
            lay.addWidget(s)
        self.duck = QCheckBox("Lower the music automatically when people talk (ducking)")
        self.duck.toggled.connect(lambda c: self._set("duck", c))
        self.loop = QCheckBox("Repeat until the end of the video")
        self.loop.toggled.connect(lambda c: self._set("loop", c))
        lay.addWidget(self.duck)
        lay.addWidget(self.loop)
        self.kind = QComboBox()
        for k, v in (("music", "Music"), ("voice", "Voice-over"), ("sound", "Sound effect")):
            self.kind.addItem(v, k)
        self.kind.currentIndexChanged.connect(lambda _: self._set("kind", self.kind.currentData(), True))
        krow = QHBoxLayout()
        krow.addWidget(QLabel("Type"))
        krow.addWidget(self.kind, 1)
        lay.addLayout(krow)
        lay.addWidget(_label("Move or trim the block on the timeline to change where it plays.", "hint"))
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(w))

    def _set(self, key, value, reload=False):
        if self.loading or self.item is None:
            return
        setattr(self.item, key, value)
        if reload:
            self.load(self.item, self.project)
        self.emit("audio." + key)

    def load(self, item: AudioItem, project: Project):
        self.item, self.project = item, project
        self.loading = True
        self.title.setText(item.name)
        self.vol.set(item.volume)
        self.fi.set(item.fade_in)
        self.fo.set(item.fade_out)
        music = item.kind == "music"
        self.duck.setVisible(music)
        self.loop.setVisible(music)
        self.duck.setChecked(item.duck)
        self.loop.setChecked(item.loop)
        self.kind.setCurrentIndex(max(0, self.kind.findData(item.kind)))
        self.loading = False


# ============================================================== project
class ProjectPage(_Base):
    def __init__(self):
        super().__init__()
        w, lay = _page()
        lay.addWidget(_label("Export settings", "sectionTitle"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.addWidget(QLabel("Format"), 0, 0)
        self.preset = QComboBox()
        for key, p in PRESETS.items():
            self.preset.addItem(p.label, key)
        self.preset.currentIndexChanged.connect(lambda _: self._set("preset", self.preset.currentData()))
        grid.addWidget(self.preset, 0, 1)
        self.preset_hint = _label("", "hint")
        grid.addWidget(self.preset_hint, 1, 1)
        grid.addWidget(QLabel("Frame shape"), 2, 0)
        self.canvas = QComboBox()
        for key, (label, _) in CANVASES.items():
            self.canvas.addItem(label, key)
        self.canvas.currentIndexChanged.connect(lambda _: self._set("canvas", self.canvas.currentData()))
        grid.addWidget(self.canvas, 2, 1)
        grid.addWidget(QLabel("Empty space"), 3, 0)
        self.bg = QComboBox()
        for key, label in BACKGROUNDS.items():
            self.bg.addItem(label, key)
        self.bg.currentIndexChanged.connect(lambda _: self._set("background", self.bg.currentData()))
        grid.addWidget(self.bg, 3, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        row = QHBoxLayout()
        self.fade_in = QCheckBox("Fade in from black")
        self.fade_out = QCheckBox("Fade out to black")
        self.fade_in.toggled.connect(lambda c: self._set("fade_in", c))
        self.fade_out.toggled.connect(lambda c: self._set("fade_out", c))
        row.addWidget(self.fade_in)
        row.addWidget(self.fade_out)
        row.addStretch(1)
        lay.addLayout(row)
        self.summary = _label("", "muted")
        lay.addWidget(self.summary)
        lay.addWidget(_label("Select a clip, text, overlay or sound on the timeline to edit it. Click a ◆ "
                             "between two clips to add a transition.", "hint"))
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(w))

    def _set(self, key, value):
        if self.loading or self.project is None:
            return
        setattr(self.project, key, value)
        if key == "preset":
            self.preset_hint.setText(PRESETS[value].hint)
        self.emit("project." + key)

    def load(self, project: Project):
        self.project = project
        self.loading = True
        self.preset.setCurrentIndex(max(0, self.preset.findData(project.preset)))
        self.preset_hint.setText(PRESETS[project.preset].hint)
        self.canvas.setCurrentIndex(max(0, self.canvas.findData(project.canvas)))
        self.bg.setCurrentIndex(max(0, self.bg.findData(project.background)))
        self.fade_in.setChecked(project.fade_in)
        self.fade_out.setChecked(project.fade_out)
        self.loading = False


# ============================================================ container
class Inspector(QStackedWidget):
    changed = Signal(str)
    transition_to_all = Signal()
    text_style_to_all = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project_page = ProjectPage()
        self.segment_page = SegmentPage()
        self.transition_page = TransitionPage()
        self.text_page = TextPage()
        self.overlay_page = OverlayPage()
        self.audio_page = AudioPage()
        for p in (self.project_page, self.segment_page, self.transition_page, self.text_page,
                  self.overlay_page, self.audio_page):
            self.addWidget(p)
            p.changed.connect(self.changed)
        self.transition_page.to_all.connect(self.transition_to_all)
        self.text_page.style_to_all.connect(self.text_style_to_all)

    def show_for(self, kind: str, obj, project: Project):
        if kind == "segment" and obj is not None:
            self.segment_page.load(obj, project)
            self.setCurrentWidget(self.segment_page)
        elif kind == "transition" and obj is not None:
            self.transition_page.load(obj, project)
            self.setCurrentWidget(self.transition_page)
        elif kind == "text" and obj is not None:
            self.text_page.load(obj, project)
            self.setCurrentWidget(self.text_page)
        elif kind == "overlay" and obj is not None:
            self.overlay_page.load(obj, project)
            self.setCurrentWidget(self.overlay_page)
        elif kind == "audio" and obj is not None:
            self.audio_page.load(obj, project)
            self.setCurrentWidget(self.audio_page)
        else:
            self.project_page.load(project)
            self.setCurrentWidget(self.project_page)

    def set_summary(self, text: str):
        self.project_page.summary.setText(text)
