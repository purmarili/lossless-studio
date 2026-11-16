"""Filmstrip with draggable in/out handles and a playhead (times in source seconds)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme

HANDLE_W = 11


class TrimBar(QWidget):
    range_changed = Signal(float, float)      # while dragging
    range_committed = Signal(float, float)    # on release
    seek_requested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(64)
        self.setMaximumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.duration = 0.0
        self.t_in = 0.0
        self.t_out = 0.0
        self.playhead = 0.0
        self.strip: QPixmap | None = None
        self.drag: str | None = None
        self.hover: str | None = None
        self.min_len = 0.2

    # ---------------------------------------------------------------- state
    def set_clip(self, duration: float, t_in: float, t_out: float, strip: QPixmap | None):
        self.duration = max(duration, 0.001)
        self.t_in, self.t_out = t_in, t_out
        self.strip = strip
        self.playhead = t_in
        self.update()

    def set_range(self, t_in: float, t_out: float):
        self.t_in, self.t_out = t_in, t_out
        self.update()

    def set_strip(self, strip: QPixmap | None):
        self.strip = strip
        self.update()

    def set_playhead(self, t: float):
        self.playhead = t
        self.update()

    # ------------------------------------------------------------- geometry
    def _track(self) -> QRectF:
        return QRectF(HANDLE_W, 6, max(10, self.width() - 2 * HANDLE_W), self.height() - 12)

    def _x(self, t: float) -> float:
        r = self._track()
        return r.left() + r.width() * (t / self.duration if self.duration else 0)

    def _t(self, x: float) -> float:
        r = self._track()
        return min(self.duration, max(0.0, (x - r.left()) / r.width() * self.duration))

    def _hit(self, x: float) -> str:
        if abs(x - (self._x(self.t_in) - HANDLE_W / 2)) <= HANDLE_W:
            return "in"
        if abs(x - (self._x(self.t_out) + HANDLE_W / 2)) <= HANDLE_W:
            return "out"
        return "seek"

    # ---------------------------------------------------------------- paint
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._track()
        clip = QPainterPath()
        clip.addRoundedRect(r, 6, 6)
        p.fillPath(clip, QColor("#0b0d11"))
        if self.strip and not self.strip.isNull():
            p.save()
            p.setClipPath(clip)
            p.drawPixmap(r, self.strip, QRectF(self.strip.rect()))
            p.restore()
        if not self.duration:
            return
        xi, xo = self._x(self.t_in), self._x(self.t_out)
        shade = QColor(7, 8, 10, 190)
        p.save()
        p.setClipPath(clip)
        p.fillRect(QRectF(r.left(), r.top(), xi - r.left(), r.height()), shade)
        p.fillRect(QRectF(xo, r.top(), r.right() - xo, r.height()), shade)
        p.restore()
        # selection frame + handles
        accent = QColor(theme.WARN)
        p.setPen(QPen(accent, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(xi, r.top() + 1, xo - xi, r.height() - 2))
        for x, side in ((xi - HANDLE_W, "in"), (xo, "out")):
            hr = QRectF(x, r.top(), HANDLE_W, r.height())
            path = QPainterPath()
            path.addRoundedRect(hr, 4, 4)
            col = QColor(accent).lighter(115) if self.hover == side or self.drag == side else accent
            p.fillPath(path, col)
            p.setPen(QPen(QColor("#1a1406"), 1.5))
            cx = hr.center().x()
            p.drawLine(QPointF(cx - 1.5, hr.center().y() - 7), QPointF(cx - 1.5, hr.center().y() + 7))
            p.drawLine(QPointF(cx + 1.5, hr.center().y() - 7), QPointF(cx + 1.5, hr.center().y() + 7))
        # playhead
        xp = self._x(self.playhead)
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawLine(QPointF(xp, 1), QPointF(xp, self.height() - 1))
        p.setBrush(QColor("#ffffff"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(xp, 3), 3.5, 3.5)

    # ---------------------------------------------------------------- mouse
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or not self.duration:
            return
        self.drag = self._hit(e.position().x())
        self._drag_to(e.position().x())

    def mouseMoveEvent(self, e):
        if self.drag:
            self._drag_to(e.position().x())
        else:
            h = self._hit(e.position().x())
            self.hover = h if h != "seek" else None
            self.setCursor(Qt.CursorShape.SizeHorCursor if self.hover else Qt.CursorShape.PointingHandCursor)
            self.update()

    def mouseReleaseEvent(self, e):
        if self.drag in ("in", "out"):
            self.range_committed.emit(self.t_in, self.t_out)
        self.drag = None
        self.update()

    def leaveEvent(self, _):
        self.hover = None
        self.update()

    def _drag_to(self, x: float):
        t = self._t(x if self.drag == "seek" else (x + HANDLE_W / 2 if self.drag == "in" else x - HANDLE_W / 2))
        if self.drag == "in":
            self.t_in = min(t, self.t_out - self.min_len)
            self.t_in = max(0.0, self.t_in)
            self.range_changed.emit(self.t_in, self.t_out)
            self.seek_requested.emit(self.t_in)
        elif self.drag == "out":
            self.t_out = max(t, self.t_in + self.min_len)
            self.t_out = min(self.duration, self.t_out)
            self.range_changed.emit(self.t_in, self.t_out)
            self.seek_requested.emit(self.t_out)
        else:
            self.seek_requested.emit(min(max(t, self.t_in), self.t_out))
        self.update()
