"""Timeline preview: plays the whole edit (main track, photos, freeze frames,
picture-in-picture, captions, music, fades) with one master clock.

Video uses Qt Multimedia (hardware decoding). Geometry (rotation, framing,
zoom / Ken Burns), captions and their animations, picture-in-picture and fades
are applied live. Colour edits, effects and the HDR -> SDR conversion of the
"plays everywhere" formats can't be shown live, so when paused an exact frame
is rendered by FFmpeg with the very same filters the export uses.
"""

from __future__ import annotations

import itertools
from pathlib import Path

from PySide6.QtCore import (QElapsedTimer, QObject, QPointF, QRectF, QRunnable, QSizeF, Qt, QThreadPool, QTimer,
                            QUrl, Signal)
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPixmap, QRadialGradient, QTransform
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (QFrame, QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsRectItem,
                               QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from .. import render
from ..project import OverlayItem, Project, Segment, TextItem, ease
from .textimg import text_image, typewriter_count, typewriter_timing


def fmt_time(t: float) -> str:
    t = max(0.0, t)
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:04.1f}"


class _StillSignals(QObject):
    done = Signal(int, object)


class _StillTask(QRunnable):
    def __init__(self, sig, rid, seg, project, t_local, width):
        super().__init__()
        self.sig, self.rid, self.seg, self.project, self.t_local, self.width = sig, rid, seg, project, t_local, width

    def run(self):
        img = None
        try:
            data = render.preview_frame(self.seg, self.project, self.t_local, self.width)
            if data:
                q = QImage()
                if q.loadFromData(data, "PNG"):
                    img = q
        except Exception:
            img = None
        self.sig.done.emit(self.rid, img)


class _View(QGraphicsView):
    def __init__(self, scene, owner):
        super().__init__(scene)
        self.owner = owner

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.owner._fit()

    def mousePressEvent(self, e):
        self.owner.toggle_play()


class _Deck:
    """A media player + video item used for one source file at a time."""

    def __init__(self, parent_item, owner):
        self.player = QMediaPlayer(owner)
        self.audio = QAudioOutput(owner)
        self.player.setAudioOutput(self.audio)
        self.item = QGraphicsVideoItem(parent_item)
        self.item.setAspectRatioMode(Qt.AspectRatioMode.IgnoreAspectRatio)
        self.player.setVideoOutput(self.item)
        self.path: Path | None = None
        self.pending: float | None = None
        self.player.mediaStatusChanged.connect(self._status)

    def load(self, path: Path, t: float):
        if self.path != path:
            self.path = path
            self.pending = t
            # stopped first: pause() after loading then shows the first frame
            self.player.stop()
            self.player.setSource(QUrl.fromLocalFile(str(path)))
        else:
            self.seek(t)

    def ready(self) -> bool:
        return self.player.mediaStatus() in (QMediaPlayer.MediaStatus.LoadedMedia,
                                             QMediaPlayer.MediaStatus.BufferedMedia,
                                             QMediaPlayer.MediaStatus.BufferingMedia,
                                             QMediaPlayer.MediaStatus.EndOfMedia)

    def _status(self, st):
        if self.pending is not None and self.ready():
            t, self.pending = self.pending, None
            self.seek(t)

    def seek(self, t: float, force: bool = False):
        if not self.ready():
            self.pending = t
            return
        ms = int(max(0.0, t) * 1000)
        if force and ms == self.player.position():
            # Qt renders no new frame when seeking to the current position
            # (e.g. a preloaded, hidden player that is now shown)
            self.player.setPosition(ms + 1)
        self.player.setPosition(ms)
        if self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            self.player.pause()

    def pos(self) -> float:
        return self.player.position() / 1000.0

    def playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())
        self.path = None


class Preview(QWidget):
    time_changed = Signal(float)              # timeline seconds (during playback)
    play_state_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.canvas = (1920, 1080)
        self.cover = False
        self.background = "black"
        self.t = 0.0
        self.playing = False
        self.clock = QElapsedTimer()
        self.clock_t0 = 0.0
        self._req = itertools.count(1)
        self._still_rid = 0
        self._still_key = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.still_sig = _StillSignals()
        self.still_sig.done.connect(self._on_still)
        self._pix_cache: dict[str, QPixmap] = {}
        self._text_cache: dict[tuple, tuple[QPixmap, int, int]] = {}

        self.scene = QGraphicsScene(self)
        self.scene.setBackgroundBrush(QColor("#07080a"))
        self.canvas_item = QGraphicsRectItem()
        self.canvas_item.setBrush(QColor("#000000"))
        self.canvas_item.setPen(Qt.PenStyle.NoPen)
        self.canvas_item.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemClipsChildrenToShape, True)
        self.scene.addItem(self.canvas_item)
        self.decks = [_Deck(self.canvas_item, self), _Deck(self.canvas_item, self)]
        self.cur = 0                                   # deck showing the current segment
        self.photo = QGraphicsPixmapItem(self.canvas_item)
        self.photo.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.still = QGraphicsPixmapItem(self.canvas_item)
        self.still.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.still.hide()
        self.vignette = QGraphicsRectItem(self.canvas_item)
        self.vignette.setPen(Qt.PenStyle.NoPen)
        self.vignette.hide()
        self.ovl_items: dict[str, tuple] = {}          # overlay id -> (deck|None, item)
        self.text_items: dict[str, QGraphicsPixmapItem] = {}
        self.fader = QGraphicsRectItem(self.canvas_item)
        self.fader.setBrush(QColor("#000000"))
        self.fader.setPen(Qt.PenStyle.NoPen)
        self.fader.setZValue(50)
        self.fader.hide()
        self.audio_players: dict[str, tuple[QMediaPlayer, QAudioOutput]] = {}

        self.view = _View(self.scene, self)
        self.view.setObjectName("previewView")
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.view.setMinimumHeight(140)
        self.view.setCursor(Qt.CursorShape.PointingHandCursor)

        self.play_btn = QPushButton("▶")
        self.play_btn.setObjectName("icon")
        self.play_btn.setToolTip("Play / pause (Space)")
        self.play_btn.clicked.connect(self.toggle_play)
        self.time_label = QLabel("0:00.0 / 0:00.0")
        self.time_label.setObjectName("timeLabel")
        self.message = QLabel("")
        self.message.setObjectName("hint")
        self.message.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(10)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.time_label)
        controls.addStretch(1)
        controls.addWidget(self.message)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.view, 1)
        lay.addLayout(controls)

        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._tick)
        self.still_timer = QTimer(self)
        self.still_timer.setSingleShot(True)
        self.still_timer.setInterval(280)
        self.still_timer.timeout.connect(self._request_still)

    # ============================================================= public
    def set_project(self, project: Project | None):
        self.project = project
        self.refresh()

    def set_canvas(self, width: int, height: int, cover: bool, background: str = "black"):
        self.canvas, self.cover, self.background = (max(2, width), max(2, height)), cover, background
        self._text_cache.clear()
        self.refresh()

    def refresh(self):
        """Re-apply everything at the current time (after an edit)."""
        W, H = self.canvas
        self.canvas_item.setRect(QRectF(0, 0, W, H))
        self.scene.setSceneRect(QRectF(0, 0, W, H))
        self.fader.setRect(QRectF(0, 0, W, H))
        self.vignette.setRect(QRectF(0, 0, W, H))
        self._fit()
        if self.project is None or not self.project.segments:
            for d in self.decks:
                d.item.hide()
            self.photo.hide()
            self._sync_layers(0.0, force=True)
            self.time_label.setText("")
            return
        self.t = min(self.t, self.project.duration)
        self._render_at(self.t, seek=True)
        self._schedule_still()

    def seek(self, t: float):
        if self.project is None or not self.project.segments:
            self.t = 0.0
            return
        self.t = min(max(0.0, t), self.project.duration)
        if self.playing:
            self.clock_t0 = self.t
            self.clock.restart()
        self.still.hide()
        self._render_at(self.t, seek=True)
        self._schedule_still()

    def position(self) -> float:
        return self.t

    def toggle_play(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if self.project is None or not self.project.segments:
            return
        if self.t >= self.project.duration - 0.05:
            self.t = 0.0
        self.playing = True
        self.still.hide()
        self.clock_t0 = self.t
        self.clock.restart()
        self._render_at(self.t, seek=True)
        self.timer.start()
        self.play_btn.setText("❚❚")
        self.play_state_changed.emit(True)

    def pause(self):
        if not self.playing:
            return
        self.playing = False
        self.timer.stop()
        for d in self.decks:
            if d.playing():
                d.player.pause()
        for oid, (deck, _) in self.ovl_items.items():
            if deck and deck.playing():
                deck.player.pause()
        for pl, _ in self.audio_players.values():
            pl.pause()
        self.play_btn.setText("▶")
        self._render_at(self.t, seek=True)
        self._schedule_still()
        self.play_state_changed.emit(False)

    def is_playing(self) -> bool:
        return self.playing

    def shutdown(self):
        self.timer.stop()
        for d in self.decks:
            d.stop()
        for deck, _ in self.ovl_items.values():
            if deck:
                deck.stop()
        for pl, _ in self.audio_players.values():
            pl.stop()
        self.pool.clear()

    # ============================================================ internals
    def _fit(self):
        self.view.fitInView(self.canvas_item, Qt.AspectRatioMode.KeepAspectRatio)

    def _tick(self):
        if not self.playing or self.project is None:
            return
        self.t = self.clock_t0 + self.clock.elapsed() / 1000.0
        if self.t >= self.project.duration:
            self.t = self.project.duration
            self.pause()
            self.time_changed.emit(self.t)
            return
        self._render_at(self.t, seek=False)
        self.time_changed.emit(self.t)

    def _pixmap(self, path) -> QPixmap:
        key = str(path)
        if key not in self._pix_cache:
            self._pix_cache[key] = QPixmap(key)
        return self._pix_cache[key]

    def _place(self, item, seg: Segment, w: float, h: float, tl: float):
        """Position a main-track item (unrotated size w x h) on the canvas."""
        W, H = self.canvas
        rot = seg.edit.rotate % 360
        fw, fh = (h, w) if rot in (90, 270) else (w, h)
        differs = abs(fw / fh - W / H) > 0.01
        s = (max if self.cover and differs else min)(W / fw, H / fh)
        tf = seg.edit.transform_at(tl / max(seg.length, 1e-3))
        z = max(1.0, tf.zoom)
        t = QTransform()
        # zoom around the point chosen by x/y (same maths as the export's crop)
        t.translate(W / 2 - tf.x * (W * (z - 1) / 2), H / 2 - tf.y * (H * (z - 1) / 2))
        t.scale(z, z)
        t.rotate(rot)
        if seg.edit.flip:
            t.scale(-1, 1)
        t.translate(-w * s / 2, -h * s / 2)
        if isinstance(item, QGraphicsVideoItem):
            item.setSize(QSizeF(w * s, h * s))
            item.setTransform(t)
        else:
            pm = item.pixmap()
            k = (w * s) / max(1, pm.width())
            t2 = QTransform()
            t2.scale(k, k)
            item.setTransform(t2 * t)

    def _render_at(self, t: float, seek: bool):
        p = self.project
        i, tl = p.segment_at(t)
        seg = p.segments[i]
        # ---- main track ----
        if seg.kind == "image":
            for d in self.decks:
                d.item.hide()
                if d.playing():
                    d.player.pause()
            pm = self._pixmap(seg.clip.render_path or seg.clip.path)
            self.photo.setPixmap(pm)
            self.photo.show()
            self._place(self.photo, seg, *seg.clip.v.display_size, tl)
        else:
            self.photo.hide()
            deck = self.decks[self.cur]
            other = self.decks[1 - self.cur]
            if deck.path != seg.clip.path and other.path == seg.clip.path:
                self.cur = 1 - self.cur
                deck, other = other, deck
            src = seg.src_time(tl)
            newly_shown = not deck.item.isVisible()
            if deck.path != seg.clip.path:
                deck.load(seg.clip.path, src)
                seek = True
            deck.item.show()
            other.item.hide()
            if other.playing():
                other.player.pause()
            self._place(deck.item, seg, *seg.clip.v.display_size, tl)
            moving = self.playing and seg.kind == "video" and not seg.edit.reverse
            deck.player.setPlaybackRate(seg.speed if moving else 1.0)
            deck.audio.setMuted(seg.edit.muted or not moving)
            deck.audio.setVolume(min(1.0, seg.edit.volume))
            if moving:
                if seek or abs(deck.pos() - src) > 0.2:
                    deck.seek(src)
                if not deck.playing() and deck.ready():
                    deck.player.play()
            else:
                if deck.playing():
                    deck.player.pause()
                step = 0.07 if self.playing else 0.0
                if seek or newly_shown or abs(deck.pos() - src) > step:
                    deck.seek(src, force=newly_shown or seek)
            # preload the next clip in the other deck
            if i + 1 < len(p.segments):
                nxt = p.segments[i + 1]
                if nxt.kind != "image" and nxt.clip.path != seg.clip.path and other.path != nxt.clip.path:
                    other.load(nxt.clip.path, nxt.src_time(0))
        # blur effect (approximate live)
        cur_item = self.photo if seg.kind == "image" else self.decks[self.cur].item
        if seg.edit.blur > 1e-3:
            eff = cur_item.graphicsEffect() or QGraphicsBlurEffect()
            eff.setBlurRadius(seg.edit.blur * 30)
            cur_item.setGraphicsEffect(eff)
        elif cur_item.graphicsEffect():
            cur_item.setGraphicsEffect(None)
        # vignette (approximate live)
        if seg.edit.vignette > 1e-3:
            W, H = self.canvas
            g = QRadialGradient(QPointF(W / 2, H / 2), max(W, H) * 0.72)
            g.setColorAt(0.45, QColor(0, 0, 0, 0))
            g.setColorAt(1.0, QColor(0, 0, 0, int(230 * seg.edit.vignette)))
            self.vignette.setBrush(QBrush(g))
            self.vignette.show()
        else:
            self.vignette.hide()
        # ---- fades ----
        a = 0.0
        L = seg.length
        fi = max(seg.edit.fade_in, render.FADE if (p.fade_in and i == 0) else 0)
        fo = max(seg.edit.fade_out, render.FADE if (p.fade_out and i == len(p.segments) - 1) else 0)
        if fi > 0 and tl < fi:
            a = max(a, 1 - tl / fi)
        if fo > 0 and tl > L - fo:
            a = max(a, (tl - (L - fo)) / fo)
        self.fader.setOpacity(min(1.0, a))
        self.fader.setVisible(a > 0.001)
        self._sync_layers(t, force=seek)
        self.time_label.setText(f"{fmt_time(t)} / {fmt_time(p.duration)}")

    # ---------------------------------------------------------- layers
    def _sync_layers(self, t: float, force: bool):
        p = self.project
        live = set()
        if p is not None:
            for it in p.overlays:
                if it.start <= t < it.end:
                    live.add(it.id)
                    self._show_overlay(it, t, force)
            for it in p.texts:
                if it.active and it.start <= t < it.end:
                    live.add(it.id)
                    self._show_text(it, t)
            self._sync_audio(t, force)
        for oid, (deck, item) in list(self.ovl_items.items()):
            if oid not in live:
                item.hide()
                if deck and deck.playing():
                    deck.player.pause()
        for tid, item in list(self.text_items.items()):
            if tid not in live:
                item.hide()
        if p is not None:
            known = {o.id for o in p.overlays}
            for oid in [k for k in self.ovl_items if k not in known]:
                deck, item = self.ovl_items.pop(oid)
                if deck:
                    deck.stop()
                    self.scene.removeItem(deck.item)
                else:
                    self.scene.removeItem(item)

    def _show_overlay(self, it: OverlayItem, t: float, force: bool):
        W, H = self.canvas
        entry = self.ovl_items.get(it.id)
        if entry is None or (entry[0] is None) != (it.kind == "image"):
            if it.kind == "image":
                item = QGraphicsPixmapItem(self.canvas_item)
                item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
                item.setPixmap(self._pixmap(it.clip.render_path or it.clip.path))
                entry = (None, item)
            else:
                deck = _Deck(self.canvas_item, self)
                entry = (deck, deck.item)
            entry[1].setZValue(10)
            self.ovl_items[it.id] = entry
        deck, item = entry
        w, h = it.clip.v.display_size if it.clip.v else (16, 9)
        ow = W * it.scale
        oh = ow * h / max(1, w)
        x, y = W * it.x - ow / 2, H * it.y - oh / 2
        if deck is None:
            pm = item.pixmap()
            item.setScale(ow / max(1, pm.width()))
            item.setPos(x, y)
        else:
            item.setSize(QSizeF(ow, oh))
            item.setPos(x, y)
            src = it.src_in + (t - it.start)
            if deck.path != it.clip.path:
                deck.load(it.clip.path, src)
            deck.audio.setMuted(it.muted or not self.playing)
            deck.audio.setVolume(min(1.0, it.volume))
            if self.playing:
                if force or abs(deck.pos() - src) > 0.25:
                    deck.seek(src)
                if not deck.playing() and deck.ready():
                    deck.player.play()
            else:
                if deck.playing():
                    deck.player.pause()
                if force or abs(deck.pos() - src) > 0.02:
                    deck.seek(src)
        item.setOpacity(it.opacity)
        item.show()

    def _text_pixmap(self, it: TextItem, visible: int | None):
        W, H = self.canvas
        k = min(1.0, 1280 / max(W, H))
        key = (it.id, it.text, it.style, it.size, it.color, it.bold, it.position, round(it.x, 3), round(it.y, 3),
               visible, W, H)
        if key not in self._text_cache:
            if len(self._text_cache) > 300:
                self._text_cache.clear()
            img, x, y = text_image(it, max(2, int(W * k)), max(2, int(H * k)), visible)
            self._text_cache[key] = (QPixmap.fromImage(img), x, y)
        return self._text_cache[key], k

    def _show_text(self, it: TextItem, t: float):
        W, H = self.canvas
        item = self.text_items.get(it.id)
        if item is None:
            item = QGraphicsPixmapItem(self.canvas_item)
            item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            item.setZValue(20)
            self.text_items[it.id] = item
        local = t - it.start
        visible = None
        if it.anim_in == "typewriter":
            steps, step = typewriter_timing(it)
            n = typewriter_count(it)
            k_step = int(local / step) + 1
            if k_step <= steps:
                visible = round(n * k_step / steps)
        (pm, x, y), k = self._text_pixmap(it, visible)
        item.setPixmap(pm)
        ad = max(0.05, min(it.anim_dur, it.duration / 2))
        pin = ease(local / ad)
        pout = ease((local - (it.duration - ad)) / ad)
        alpha, scale, dx, dy = 1.0, 1.0, 0.0, 0.0
        if it.anim_in in ("fade", "slide_up", "slide_left", "pop"):
            alpha *= pin
        if it.anim_out in ("fade", "slide_down", "pop"):
            alpha *= 1 - pout
        if it.anim_in == "pop":
            scale *= 0.6 + 0.4 * pin
        if it.anim_out == "pop":
            scale *= 1 - 0.4 * pout
        if it.anim_in == "slide_up":
            dy += H * 0.05 * (1 - pin)
        if it.anim_in == "slide_left":
            dx -= W * 0.08 * (1 - pin)
        if it.anim_out == "slide_down":
            dy += H * 0.05 * pout
        w, h = pm.width() / k, pm.height() / k
        cx, cy = x / k + w / 2 + dx, y / k + h / 2 + dy
        item.setScale(scale / k)
        item.setPos(cx - w * scale / 2, cy - h * scale / 2)
        item.setOpacity(max(0.0, min(1.0, alpha)))
        item.show()

    def _sync_audio(self, t: float, force: bool):
        p = self.project
        known = set()
        for a in p.audios:
            known.add(a.id)
            length = p.audio_length(a)
            active = a.start <= t < a.start + length
            pl = self.audio_players.get(a.id)
            if pl is None:
                if not active:
                    continue
                player, out = QMediaPlayer(self), QAudioOutput(self)
                player.setAudioOutput(out)
                player.setSource(QUrl.fromLocalFile(str(a.path)))
                pl = (player, out)
                self.audio_players[a.id] = pl
            player, out = pl
            if not active or not self.playing:
                if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                    player.pause()
                continue
            local = t - a.start
            vol = a.volume
            if a.fade_in > 0 and local < a.fade_in:
                vol *= local / a.fade_in
            if a.fade_out > 0 and local > length - a.fade_out:
                vol *= max(0.0, (length - local) / a.fade_out)
            out.setVolume(min(1.0, vol))
            src = a.src_in + local
            if a.media_duration > 0 and a.kind == "music" and a.loop:
                src = a.src_in + (local % max(0.5, a.media_duration - a.src_in))
            if force or abs(player.position() / 1000 - src) > 0.25:
                player.setPosition(int(src * 1000))
            if player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                player.play()
        for aid in [k for k in self.audio_players if k not in known]:
            pl, _ = self.audio_players.pop(aid)
            pl.stop()

    # ------------------------------------------------------------- still
    def _needs_still(self, seg: Segment) -> bool:
        p = self.project
        sdr = p.preset in ("1080p", "720p") and seg.kind != "image" and \
            seg.clip.v.color_transfer in render.HDR_TRANSFERS
        e = seg.edit
        W, H = self.canvas
        if seg.clip.v:
            dw, dh = seg.clip.v.display_size
            if e.rotate % 180 == 90:
                dw, dh = dh, dw
            bars = abs(dw / max(dh, 1) - W / H) > 0.01 and not self.cover
        else:
            bars = False
        return e.has_color or e.sharpen > 0 or e.blur > 0 or e.vignette > 0 or sdr or \
            (bars and self.background == "blur")

    def _schedule_still(self):
        self.still.hide()
        if self.project is None or not self.project.segments or self.playing:
            return
        i, tl = self.project.segment_at(self.t)
        seg = self.project.segments[i]
        if self._needs_still(seg):
            self._still_key = (i, tl)
            self.still_timer.start()
            self.message.setText("Rendering exact colours…")
        else:
            self.message.setText("")

    def _request_still(self):
        if self.playing or self.project is None or self._still_key is None:
            return
        i, tl = self._still_key
        if i >= len(self.project.segments):
            return
        seg = self.project.segments[i]
        rid = next(self._req)
        self._still_rid = rid
        width = int(min(1280, self.canvas[0]))
        snap = self.project.copy()
        self.pool.start(_StillTask(self.still_sig, rid, snap.segments[i], snap, tl, width))

    def _on_still(self, rid, img):
        if rid != self._still_rid or self.playing:
            return
        if img is None:
            self.message.setText("")
            return
        W, H = self.canvas
        pm = QPixmap.fromImage(img)
        self.still.setPixmap(pm)
        self.still.setScale(W / max(1, pm.width()))
        self.still.setZValue(5)
        self.still.show()
        self.message.setText("Exact export colours")
