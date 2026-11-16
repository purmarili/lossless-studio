"""CapCut-style multi-track timeline.

Tracks from top to bottom: text, overlays (picture-in-picture), main video,
audio. Clips are blocks you can select, trim at their edges, drag to reorder
(main track) or move in time (other tracks, with snapping). A playhead runs
through all tracks; Ctrl+wheel zooms, wheel scrolls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QScrollBar, QSizePolicy, QWidget

from ..project import TRANSITIONS, AudioItem, OverlayItem, Project, Segment, TextItem
from . import theme

GUTTER = 38
RULER_H = 24
TEXT_H = 26
OVL_H = 34
MAIN_H = 64
AUDIO_H = 30
GAP = 6
EDGE = 7               # px grab zone for trimming

CLIP_MIME = "application/x-lvm-clip"   # drag from the media bin: clip ids, one per line

TEXT_COL = "#c77dff"
OVL_COL = "#3fb7a7"
AUDIO_COL = "#4f8cff"
VOICE_COL = "#f2a541"


@dataclass
class _Hit:
    kind: str            # segment | text | overlay | audio | junction | ruler | empty
    id: str = ""
    part: str = "body"   # body | left | right
    index: int = -1


def _lanes(items, start, end) -> dict[str, int]:
    lanes: list[float] = []
    out = {}
    for it in sorted(items, key=start):
        for i, last in enumerate(lanes):
            if start(it) >= last - 1e-6:
                lanes[i] = end(it)
                out[it.id] = i
                break
        else:
            lanes.append(end(it))
            out[it.id] = len(lanes) - 1
    return out


class Timeline(QWidget):
    playhead_changed = Signal(float)          # user moved the playhead
    selection_changed = Signal(str, str)      # kind, id ("" = nothing)
    editing = Signal()                        # live update during a drag
    edited = Signal(str)                      # committed change (undo point)
    request_split = Signal()
    request_delete = Signal()
    request_duplicate = Signal()
    media_dropped = Signal(object, str, object)   # items, "ids" | "paths", destination

    def __init__(self, thumb: Callable[[str], QPixmap | None], strip: Callable[[str], QPixmap | None],
                 wave: Callable[[str], QPixmap | None], parent=None):
        super().__init__(parent)
        self.thumb_for, self.strip_for, self.wave_for = thumb, strip, wave
        self.project: Project | None = None
        self.pps = 40.0                       # pixels per second
        self.view_start = 0.0                 # seconds at the left edge
        self.playhead = 0.0
        self.sel = ("", "")
        self.drag: dict | None = None
        self.hover: _Hit | None = None
        self.drop_hint: tuple | None = None
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.hbar = QScrollBar(Qt.Orientation.Horizontal, self)
        self.hbar.valueChanged.connect(self._on_scroll)
        self._layout_rows()

    # ================================================================ model
    def set_project(self, project: Project | None):
        self.project = project
        self.refresh()

    def refresh(self):
        self._layout_rows()
        self._update_scrollbar()
        self.update()

    def select(self, kind: str, id_: str, emit: bool = False):
        self.sel = (kind, id_)
        self.update()
        if emit:
            self.selection_changed.emit(kind, id_)

    def set_playhead(self, t: float, follow: bool = False):
        self.playhead = max(0.0, t)
        if follow:
            w = self.width() - GUTTER
            x = (self.playhead - self.view_start) * self.pps
            if x > w * 0.85 or x < 0:
                self._scroll_to(max(0.0, self.playhead - (w * 0.2) / self.pps))
        self.update()

    def zoom_fit(self):
        if not self.project:
            return
        dur = max(1.0, self.project.duration)
        self.pps = max(2.0, min(400.0, (self.width() - GUTTER - 30) / dur))
        self.view_start = 0.0
        self.refresh()

    def zoom(self, factor: float, anchor_t: float | None = None):
        anchor_t = self.playhead if anchor_t is None else anchor_t
        ax = (anchor_t - self.view_start) * self.pps
        self.pps = max(2.0, min(600.0, self.pps * factor))
        self.view_start = max(0.0, anchor_t - ax / self.pps)
        self.refresh()

    # ============================================================= geometry
    def _layout_rows(self):
        p = self.project
        texts = p.texts if p else []
        ovls = p.overlays if p else []
        auds = p.audios if p else []
        dur = p.duration if p else 0
        self.text_lane = _lanes(texts, lambda t: t.start, lambda t: t.end)
        self.ovl_lane = _lanes(ovls, lambda o: o.start, lambda o: o.end)
        self.audio_lane = _lanes(auds, lambda a: a.start, lambda a: a.start + p.audio_length(a)) if p else {}
        n_text = max(1, max(self.text_lane.values(), default=-1) + 1)
        n_ovl = max(1, max(self.ovl_lane.values(), default=-1) + 1)
        n_aud = max(1, max(self.audio_lane.values(), default=-1) + 1)
        y = RULER_H + GAP
        self.y_text = y
        y += n_text * TEXT_H + GAP
        self.y_ovl = y
        y += n_ovl * OVL_H + GAP
        self.y_main = y
        y += MAIN_H + GAP
        self.y_audio = y
        y += n_aud * AUDIO_H + GAP
        self.content_h = y
        h = y + 14
        self.setMinimumHeight(h)
        self.setMaximumHeight(h)

    def _x(self, t: float) -> float:
        return GUTTER + (t - self.view_start) * self.pps

    def _t(self, x: float) -> float:
        return max(0.0, self.view_start + (x - GUTTER) / self.pps)

    def _update_scrollbar(self):
        dur = (self.project.duration if self.project else 0) + 10
        w = max(1, self.width() - GUTTER)
        total = int(dur * self.pps)
        self.hbar.blockSignals(True)
        self.hbar.setRange(0, max(0, total - w))
        self.hbar.setPageStep(w)
        self.hbar.setValue(int(self.view_start * self.pps))
        self.hbar.blockSignals(False)

    def _on_scroll(self, v):
        self.view_start = v / self.pps
        self.update()

    def _scroll_to(self, t: float):
        self.view_start = max(0.0, t)
        self._update_scrollbar()

    def resizeEvent(self, e):
        self.hbar.setGeometry(GUTTER, self.height() - 12, self.width() - GUTTER, 10)
        self._update_scrollbar()

    # rectangles ----------------------------------------------------------------
    def _seg_rects(self) -> list[QRectF]:
        p = self.project
        out = []
        for s, st in zip(p.segments, p.starts()):
            out.append(QRectF(self._x(st), self.y_main, s.length * self.pps, MAIN_H))
        return out

    def _text_rect(self, t: TextItem) -> QRectF:
        return QRectF(self._x(t.start), self.y_text + self.text_lane.get(t.id, 0) * TEXT_H + 2,
                      max(4.0, t.duration * self.pps), TEXT_H - 4)

    def _ovl_rect(self, o: OverlayItem) -> QRectF:
        return QRectF(self._x(o.start), self.y_ovl + self.ovl_lane.get(o.id, 0) * OVL_H + 2,
                      max(4.0, o.duration * self.pps), OVL_H - 4)

    def _audio_rect(self, a: AudioItem) -> QRectF:
        return QRectF(self._x(a.start), self.y_audio + self.audio_lane.get(a.id, 0) * AUDIO_H + 2,
                      max(4.0, self.project.audio_length(a) * self.pps), AUDIO_H - 4)

    def _junction_rect(self, i: int) -> QRectF:
        """Clickable marker between segment i and i+1."""
        p = self.project
        st = p.starts()
        end_i = st[i] + p.segments[i].length
        mid = (st[i + 1] + end_i) / 2
        x = self._x(mid)
        return QRectF(x - 9, self.y_main + MAIN_H / 2 - 9, 18, 18)

    # ================================================================ paint
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.SURFACE))
        if not self.project:
            return
        small = QFont(self.font())
        small.setPointSizeF(8.5)
        p.setFont(small)
        fm = QFontMetrics(small)
        W = self.width()
        # track backgrounds + icons
        rows = [(self.y_text, self.y_ovl - GAP, "T", "Text"), (self.y_ovl, self.y_main - GAP, "▣", "Overlay"),
                (self.y_main, self.y_main + MAIN_H, "▶", "Video"), (self.y_audio, self.content_h - GAP, "♪", "Audio")]
        for y0, y1, icon, name in rows:
            p.fillRect(QRectF(GUTTER, y0, W - GUTTER, y1 - y0), QColor("#12151a"))
            p.setPen(QColor(theme.FAINT))
            p.drawText(QRectF(0, y0, GUTTER, y1 - y0), Qt.AlignmentFlag.AlignCenter, icon)
        p.setClipRect(QRectF(GUTTER, 0, W - GUTTER, self.height()))
        self._paint_ruler(p, fm)
        self._paint_main(p, fm)
        for t in self.project.texts:
            self._paint_item(p, fm, self._text_rect(t), TEXT_COL, "T  " + t.text.replace("\n", " "),
                             ("text", t.id))
        for o in self.project.overlays:
            r = self._ovl_rect(o)
            self._paint_item(p, fm, r, OVL_COL, o.clip.name, ("overlay", o.id), self.thumb_for(o.clip.id))
        for a in self.project.audios:
            r = self._audio_rect(a)
            col = VOICE_COL if a.kind == "voice" else AUDIO_COL
            label = ("🎙 " if a.kind == "voice" else "♪ ") + a.name
            self._paint_item(p, fm, r, col, label, ("audio", a.id), wave=self.wave_for(str(a.path)))
        # empty-track hints
        p.setPen(QColor(theme.FAINT))
        if not self.project.texts:
            p.drawText(QRectF(GUTTER + 8, self.y_text, 400, TEXT_H), Qt.AlignmentFlag.AlignVCenter,
                       "Text — add captions with “+ Text” or Auto captions")
        if not self.project.overlays:
            p.drawText(QRectF(GUTTER + 8, self.y_ovl, 400, OVL_H), Qt.AlignmentFlag.AlignVCenter,
                       "Overlay — picture-in-picture videos and photos")
        if not self.project.audios:
            p.drawText(QRectF(GUTTER + 8, self.y_audio, 400, AUDIO_H), Qt.AlignmentFlag.AlignVCenter,
                       "Audio — music and voice-overs")
        if not self.project.segments:
            big = QFont(self.font())
            big.setPointSizeF(10.5)
            p.setFont(big)
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(GUTTER + 8, self.y_main, max(200, W - GUTTER - 16), MAIN_H),
                       Qt.AlignmentFlag.AlignCenter,
                       "Drag videos and photos here — or click  “＋ Add media”  (Ctrl+I)")
            p.setFont(small)
        # drop target while dragging files / media in
        if self.drop_hint is not None:
            kind, v = self.drop_hint
            col = QColor(theme.ACCENT_2)
            if kind == "index":
                x, y0, y1, label = self._insert_x(v), self.y_main - 4, self.y_main + MAIN_H + 4, "Insert here"
            elif kind == "overlay":
                x, y0, y1, label = self._x(v), self.y_ovl, self.y_main - GAP, "Picture-in-picture here"
            else:
                x, y0, y1, label = self._x(v), self.y_audio, self.content_h - GAP, "Audio here"
            p.setPen(QPen(col, 3))
            p.drawLine(QPointF(x, y0), QPointF(x, y1))
            lr = QRectF(x + 6, y0, fm.horizontalAdvance(label) + 12, 18)
            lp = QPainterPath()
            lp.addRoundedRect(lr, 4, 4)
            p.fillPath(lp, col)
            p.setPen(QColor("#0b0d12"))
            p.drawText(lr, Qt.AlignmentFlag.AlignCenter, label)
        # drop indicator for main-track reorder
        if self.drag and self.drag.get("mode") == "reorder" and self.drag.get("insert") is not None:
            x = self._insert_x(self.drag["insert"])
            p.setPen(QPen(QColor(theme.ACCENT), 3))
            p.drawLine(QPointF(x, self.y_main - 4), QPointF(x, self.y_main + MAIN_H + 4))
        # snap guide
        if self.drag and self.drag.get("snap") is not None:
            x = self._x(self.drag["snap"])
            p.setPen(QPen(QColor(theme.WARN), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, RULER_H), QPointF(x, self.content_h))
        # playhead
        x = self._x(self.playhead)
        p.setPen(QPen(QColor("#ffffff"), 1.6))
        p.drawLine(QPointF(x, 4), QPointF(x, self.content_h))
        tri = QPainterPath()
        tri.moveTo(x - 6, 2)
        tri.lineTo(x + 6, 2)
        tri.lineTo(x, 10)
        tri.closeSubpath()
        p.fillPath(tri, QColor("#ffffff"))

    def _paint_ruler(self, p: QPainter, fm: QFontMetrics):
        W = self.width()
        steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
        major = next((s for s in steps if s * self.pps >= 80), 600)
        minor = next((s for s in steps if s * self.pps >= 12 and s <= major), major)
        t0 = int(self.view_start / minor) * minor
        t = t0
        while self._x(t) < W:
            x = self._x(t)
            is_major = abs(t / major - round(t / major)) < 1e-6
            p.setPen(QColor(theme.MUTED if is_major else "#3a4150"))
            p.drawLine(QPointF(x, RULER_H - (9 if is_major else 4)), QPointF(x, RULER_H))
            if is_major:
                m, s = divmod(t, 60)
                label = f"{int(m)}:{s:04.1f}" if major < 1 else f"{int(m)}:{int(round(s)):02d}"
                p.drawText(QPointF(x + 3, RULER_H - 11), label)
            t = round(t + minor, 6)

    def _paint_main(self, p: QPainter, fm: QFontMetrics):
        proj = self.project
        rects = self._seg_rects()
        for i, (s, r) in enumerate(zip(proj.segments, rects)):
            if r.right() < GUTTER or r.left() > self.width():
                continue
            selected = self.sel == ("segment", s.id)
            path = QPainterPath()
            path.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
            p.save()
            p.setClipPath(path, Qt.ClipOperation.IntersectClip)
            p.fillRect(r, QColor("#1f2430"))
            self._paint_frames(p, s, r)
            # caption band
            band = QRectF(r.left(), r.bottom() - 17, r.width(), 17)
            g = QLinearGradient(band.topLeft(), band.bottomLeft())
            g.setColorAt(0, QColor(0, 0, 0, 0))
            g.setColorAt(1, QColor(0, 0, 0, 200))
            p.fillRect(band, g)
            p.setPen(QColor("#ffffff"))
            label = s.clip.name if s.kind != "freeze" else "❄ " + s.clip.name
            badges = []
            if abs(s.speed - 1) > 1e-6:
                badges.append(f"{s.speed:g}×")
            if s.edit.reverse:
                badges.append("⟲")
            if s.edit.muted:
                badges.append("🔇")
            text = "  ".join([label] + badges)
            p.drawText(band.adjusted(6, 0, -44, 0), Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(text, Qt.TextElideMode.ElideRight, int(max(0, r.width() - 50))))
            p.drawText(band.adjusted(0, 0, -5, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       f"{s.length:.1f}s")
            p.restore()
            pen = QPen(QColor(theme.ACCENT if selected else "#2c3240"), 2 if selected else 1)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            if selected:
                for hx in (r.left(), r.right() - EDGE):
                    hr = QRectF(hx, r.top(), EDGE, r.height())
                    hp = QPainterPath()
                    hp.addRoundedRect(hr, 3, 3)
                    p.fillPath(hp, QColor(theme.ACCENT))
        # junctions (transition markers)
        for i in range(len(proj.segments) - 1):
            jr = self._junction_rect(i)
            s = proj.segments[i]
            has = proj.transition_after(i) > 0
            selected = self.sel == ("transition", s.id)
            path = QPainterPath()
            c = jr.center()
            path.moveTo(c.x(), jr.top())
            path.lineTo(jr.right(), c.y())
            path.lineTo(c.x(), jr.bottom())
            path.lineTo(jr.left(), c.y())
            path.closeSubpath()
            p.fillPath(path, QColor(theme.ACCENT if (has or selected) else "#2a2f3b"))
            p.setPen(QPen(QColor("#ffffff" if selected else "#0b0d12"), 1.2))
            p.drawPath(path)
            p.setPen(QColor("#0b0d12" if has else theme.MUTED))
            p.drawText(jr, Qt.AlignmentFlag.AlignCenter, "⇄" if has else "+")

    def _paint_frames(self, p: QPainter, s: Segment, r: QRectF):
        strip = self.strip_for(s.clip.id) if s.kind == "video" else None
        thumb = self.thumb_for(s.clip.id)
        if strip is not None and not strip.isNull() and s.clip.duration > 0:
            d = s.clip.duration
            sx0 = strip.width() * (s.src_in / d)
            sx1 = strip.width() * (s.src_out / d)
            src = QRectF(sx0, 0, max(1.0, sx1 - sx0), strip.height())
            if s.edit.reverse:
                p.save()
                p.translate(r.right() + r.left(), 0)
                p.scale(-1, 1)
                p.drawPixmap(r, strip, src)
                p.restore()
            else:
                p.drawPixmap(r, strip, src)
        elif thumb is not None and not thumb.isNull():
            tw = r.height() * thumb.width() / max(1, thumb.height())
            x = r.left()
            while x < r.right():
                p.drawPixmap(QRectF(x, r.top(), tw, r.height()), thumb, QRectF(thumb.rect()))
                x += tw

    def _paint_item(self, p: QPainter, fm: QFontMetrics, r: QRectF, color: str, label: str, key: tuple,
                    thumb: QPixmap | None = None, wave: QPixmap | None = None):
        if r.right() < GUTTER or r.left() > self.width():
            return
        selected = self.sel == key
        base = QColor(color)
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        fill = QColor(base)
        fill.setAlpha(110 if not selected else 170)
        p.fillPath(path, fill)
        p.save()
        p.setClipPath(path, Qt.ClipOperation.IntersectClip)
        x = r.left() + 6
        if wave is not None and not wave.isNull():
            p.setOpacity(0.55)
            p.drawPixmap(r, wave, QRectF(wave.rect()))
            p.setOpacity(1.0)
        if thumb is not None and not thumb.isNull():
            tw = r.height() * thumb.width() / max(1, thumb.height())
            p.drawPixmap(QRectF(r.left(), r.top(), tw, r.height()), thumb, QRectF(thumb.rect()))
            x += tw
        p.setPen(QColor("#ffffff"))
        p.drawText(QRectF(x, r.top(), r.right() - x - 4, r.height()), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(label, Qt.TextElideMode.ElideRight, int(max(0, r.right() - x - 6))))
        p.restore()
        p.setPen(QPen(QColor("#ffffff") if selected else base, 1.5 if selected else 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        if selected:
            for hx in (r.left(), r.right() - 5):
                p.fillRect(QRectF(hx, r.top() + 3, 5, r.height() - 6), QColor("#ffffff"))

    # ============================================================== hit test
    def _hit(self, pos: QPointF) -> _Hit:
        x, y = pos.x(), pos.y()
        p = self.project
        if not p or x < GUTTER:
            return _Hit("empty")
        if y < RULER_H:
            return _Hit("ruler")

        def edge(r: QRectF) -> str:
            if r.width() > 3 * EDGE:
                if x - r.left() <= EDGE:
                    return "left"
                if r.right() - x <= EDGE:
                    return "right"
            return "body"

        if self.y_main <= y <= self.y_main + MAIN_H:
            for i in range(len(p.segments) - 1):
                if self._junction_rect(i).adjusted(-2, -2, 2, 2).contains(pos):
                    return _Hit("junction", p.segments[i].id, index=i)
            for i, (s, r) in enumerate(zip(p.segments, self._seg_rects())):
                if r.contains(pos):
                    return _Hit("segment", s.id, edge(r), i)
            return _Hit("empty")
        for t in p.texts:
            r = self._text_rect(t)
            if r.contains(pos):
                return _Hit("text", t.id, edge(r))
        for o in p.overlays:
            r = self._ovl_rect(o)
            if r.contains(pos):
                return _Hit("overlay", o.id, edge(r))
        for a in p.audios:
            r = self._audio_rect(a)
            if r.contains(pos):
                return _Hit("audio", a.id, edge(r))
        return _Hit("empty")

    def _item(self, kind: str, id_: str):
        p = self.project
        coll = {"text": p.texts, "overlay": p.overlays, "audio": p.audios, "segment": p.segments}.get(kind, [])
        return next((x for x in coll if x.id == id_), None)

    # ================================================================ mouse
    def mousePressEvent(self, e):
        if not self.project:
            return
        pos = e.position()
        if e.button() == Qt.MouseButton.RightButton:
            h = self._hit(pos)
            if h.kind in ("segment", "text", "overlay", "audio"):
                self.select(h.kind, h.id, emit=True)
                self._context_menu(e.globalPosition().toPoint(), h)
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        h = self._hit(pos)
        t = self._t(pos.x())
        if h.kind in ("ruler", "empty"):
            if h.kind == "empty" and self.sel != ("", ""):
                self.select("", "", emit=True)
            self.drag = {"mode": "scrub"}
            self._scrub(t)
            return
        if h.kind == "junction":
            self.select("transition", h.id, emit=True)
            return
        self.select(h.kind, h.id, emit=True)
        it = self._item(h.kind, h.id)
        d = {"mode": "trim" if h.part != "body" else ("reorder" if h.kind == "segment" else "move"),
             "kind": h.kind, "id": h.id, "part": h.part, "x0": pos.x(), "t0": t, "moved": False,
             "index": h.index, "insert": None, "snap": None}
        if h.kind == "segment":
            s: Segment = it
            d["orig"] = (s.edit.start, s.edit.end, s.still_len, s.src_in, s.src_out)
            if h.part == "body":
                self.drag = d
                # click without drag also moves the playhead into the clip
                self._scrub(t)
                return
        else:
            d["orig"] = (it.start, it.duration, getattr(it, "src_in", 0.0))
        self.drag = d

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self.drag:
            h = self._hit(pos)
            cur = Qt.CursorShape.ArrowCursor
            if h.kind in ("segment", "text", "overlay", "audio"):
                cur = Qt.CursorShape.SizeHorCursor if h.part != "body" else Qt.CursorShape.OpenHandCursor
            elif h.kind == "junction":
                cur = Qt.CursorShape.PointingHandCursor
            self.setCursor(cur)
            return
        d = self.drag
        t = self._t(pos.x())
        if d["mode"] == "scrub":
            self._scrub(t)
            return
        if abs(pos.x() - d["x0"]) < 3 and not d["moved"]:
            return
        d["moved"] = True
        dt = (pos.x() - d["x0"]) / self.pps
        if d["mode"] == "reorder":
            d["insert"] = self._insert_index(pos.x())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.update()
            return
        if d["kind"] == "segment":
            self._trim_segment(d, dt)
        else:
            self._edit_item(d, dt)
        self._layout_rows()
        self.update()
        self.editing.emit()

    def mouseReleaseEvent(self, e):
        d = self.drag
        self.drag = None
        if not d:
            return
        if d["mode"] == "reorder" and d["moved"] and d["insert"] is not None:
            segs = self.project.segments
            i = d["index"]
            j = d["insert"]
            if j not in (i, i + 1):
                s = segs.pop(i)
                segs.insert(j - 1 if j > i else j, s)
                self.refresh()
                self.edited.emit("order")
        elif d["mode"] in ("trim", "move") and d["moved"]:
            self.refresh()
            self.edited.emit(f"{d['kind']}.{d['mode']}")
        self.update()

    def mouseDoubleClickEvent(self, e):
        h = self._hit(e.position())
        if h.kind == "empty" or h.kind == "ruler":
            self.zoom_fit()

    def wheelEvent(self, e):
        dy = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.15 if dy > 0 else 1 / 1.15, self._t(e.position().x()))
        else:
            self._scroll_to(self.view_start - dy / 120 * 60 / self.pps)
            self.update()

    def _scrub(self, t: float):
        dur = self.project.duration if self.project else 0
        self.playhead = min(max(0.0, t), dur)
        self.update()
        self.playhead_changed.emit(self.playhead)

    def _context_menu(self, gpos, h: _Hit):
        m = QMenu(self)
        a_split = m.addAction("Split at playhead\tS")
        a_dup = m.addAction("Duplicate")
        m.addSeparator()
        a_del = m.addAction("Delete\tDel")
        act = m.exec(gpos)
        if act == a_split:
            self.request_split.emit()
        elif act == a_dup:
            self.request_duplicate.emit()
        elif act == a_del:
            self.request_delete.emit()

    # ------------------------------------------------------------ drops
    def _drop_dest(self, pos: QPointF) -> tuple:
        y, t = pos.y(), self._t(pos.x())
        if self.project is None:
            return ("index", 0)
        t = min(t, self.project.duration)
        if self.y_ovl - GAP / 2 <= y < self.y_main - GAP / 2:
            return ("overlay", t)
        if y >= self.y_audio - GAP / 2:
            return ("audio", t)
        return ("index", self._insert_index(pos.x()))

    @staticmethod
    def _accepts(md) -> bool:
        return md.hasUrls() or md.hasFormat(CLIP_MIME)

    def dragEnterEvent(self, e):
        if self.project is not None and self._accepts(e.mimeData()):
            e.acceptProposedAction()
            self.drop_hint = self._drop_dest(e.position())
            self.update()

    def dragMoveEvent(self, e):
        if self.project is not None and self._accepts(e.mimeData()):
            e.acceptProposedAction()
            self.drop_hint = self._drop_dest(e.position())
            self.update()

    def dragLeaveEvent(self, e):
        self.drop_hint = None
        self.update()

    def dropEvent(self, e):
        md = e.mimeData()
        dest = self._drop_dest(e.position())
        self.drop_hint = None
        self.update()
        if md.hasFormat(CLIP_MIME):
            ids = [x for x in bytes(md.data(CLIP_MIME)).decode("utf-8").split("\n") if x]
            e.acceptProposedAction()
            self.media_dropped.emit(ids, "ids", dest)
        elif md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if paths:
                e.acceptProposedAction()
                self.media_dropped.emit(paths, "paths", dest)

    # ------------------------------------------------------------ reorder
    def _insert_index(self, x: float) -> int:
        rects = self._seg_rects()
        for i, r in enumerate(rects):
            if x < r.center().x():
                return i
        return len(rects)

    def _insert_x(self, i: int) -> float:
        rects = self._seg_rects()
        if not rects:
            return GUTTER
        return rects[i].left() if i < len(rects) else rects[-1].right()

    # ------------------------------------------------------------ edits
    def _snap_points(self, exclude: str) -> list[float]:
        p = self.project
        pts = [0.0, self.playhead, p.duration]
        for s, st in zip(p.segments, p.starts()):
            pts += [st, st + s.length]
        for coll in (p.texts, p.overlays):
            for it in coll:
                if it.id != exclude:
                    pts += [it.start, it.end]
        for a in p.audios:
            if a.id != exclude:
                pts += [a.start, a.start + p.audio_length(a)]
        return pts

    def _snap(self, t: float, exclude: str, d: dict) -> float:
        thr = 8 / self.pps
        best = min(self._snap_points(exclude), key=lambda q: abs(q - t), default=None)
        if best is not None and abs(best - t) <= thr:
            d["snap"] = best
            return best
        d["snap"] = None
        return t

    def _trim_segment(self, d: dict, dt: float):
        s: Segment = self._item("segment", d["id"])
        if s is None:
            return
        start0, end0, still0, in0, out0 = d["orig"]
        sp = s.speed
        if s.is_still:
            if d["part"] == "right":
                s.still_len = max(0.3, still0 + dt)
            else:
                s.still_len = max(0.3, still0 - dt)
            return
        dur = s.clip.duration
        delta = dt * sp
        left = d["part"] == "left"
        if s.edit.reverse:
            left = not left
            delta = -delta
        if left:
            new_in = min(max(0.0, in0 + delta), out0 - 0.2)
            s.edit.start = new_in if new_in > 0.005 else 0.0
        else:
            new_out = max(min(dur, out0 + delta), in0 + 0.2)
            s.edit.end = 0.0 if new_out >= dur - 0.005 else new_out

    def _edit_item(self, d: dict, dt: float):
        it = self._item(d["kind"], d["id"])
        if it is None:
            return
        start0, dur0, in0 = d["orig"]
        p = self.project
        if d["mode"] == "move":
            new = self._snap(max(0.0, start0 + dt), it.id, d)
            # also snap the end
            end_snap = self._snap(new + (dur0 if dur0 > 0 else 0), it.id, d) - (dur0 if dur0 > 0 else 0)
            if d["snap"] is not None and dur0 > 0 and abs(end_snap - new) > 1e-9:
                new = end_snap
            it.start = max(0.0, new)
            return
        media = None
        if isinstance(it, OverlayItem) and it.kind == "video":
            media = it.clip.duration
        elif isinstance(it, AudioItem) and not (it.kind == "music" and it.loop):
            media = it.media_duration
        if isinstance(it, AudioItem) and dur0 <= 0:
            dur0 = p.audio_length(it)
        if d["part"] == "left":
            new_start = self._snap(min(max(0.0, start0 + dt), start0 + dur0 - 0.2), it.id, d)
            shift = new_start - start0
            if hasattr(it, "src_in"):
                if in0 + shift < 0:
                    shift = -in0
                    new_start = start0 + shift
                it.src_in = in0 + shift
            it.start = new_start
            it.duration = dur0 - shift
        else:
            new_end = self._snap(max(start0 + 0.2, start0 + dur0 + dt), it.id, d)
            dur = new_end - start0
            if media is not None:
                dur = min(dur, media - in0)
            it.duration = max(0.2, dur)
