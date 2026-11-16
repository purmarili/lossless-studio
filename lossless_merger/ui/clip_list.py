"""The reorderable list of clip cards."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QStyle, QStyledItemDelegate

from .. import compat
from ..media import Clip, codec_label, fmt_duration, fmt_fps, fmt_resolution, fmt_size
from . import theme

ID_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_H = 92
THUMB_W, THUMB_H = 112, 63


def edit_badges(seg) -> list[str]:
    """Short labels for the edits on a segment (shown on its card)."""
    from ..project import LOOKS, TRANSITIONS
    e, out = seg.edit, []
    if seg.trimmed:
        out.append("✂ trimmed")
    if abs(e.speed - 1) > 1e-6:
        out.append(f"{e.speed:g}×")
    if e.muted:
        out.append("muted")
    elif abs(e.volume - 1) > 1e-3:
        out.append(f"vol {e.volume * 100:.0f}%")
    if e.has_color:
        out.append(LOOKS.get(e.look, ("look",))[0] if e.look != "none" else "colour")
    if e.rotate % 360 or e.flip:
        out.append("rotated" if e.rotate % 360 else "mirrored")
    if e.reverse:
        out.append("reversed")
    if e.has_effects:
        out.append("effects")
    if e.transition != "none":
        out.append("⇄ " + TRANSITIONS.get(e.transition, e.transition).lower())
    return out


class ClipDelegate(QStyledItemDelegate):
    def __init__(self, lookup: Callable[[str], tuple[object | None, QPixmap | None, str | None]], parent=None):
        super().__init__(parent)
        self.lookup = lookup

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_H)

    def paint(self, p: QPainter, option, index):
        seg, thumb, severity = self.lookup(index.data(ID_ROLE))
        if seg is None:
            return
        clip: Clip = seg.clip
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        card = QRectF(option.rect).adjusted(2, 4, -8, -4)
        path = QPainterPath()
        path.addRoundedRect(card, 12, 12)
        p.fillPath(path, QColor(theme.CARD_HOVER if (hover or selected) else theme.CARD))
        if selected:
            p.setPen(QPen(QColor(theme.ACCENT), 1.5))
            p.drawPath(path)
        if severity or clip.error:
            color = theme.ERROR if (clip.error or severity == compat.BLOCKER) else theme.WARN
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(card.left() + 1, card.top() + 14, 3, card.height() - 28), 1.5, 1.5)

        x = int(card.left()) + 16
        cy = int(card.center().y())

        # Index number
        num_font = QFont(option.font)
        num_font.setPointSizeF(9.5)
        num_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(num_font)
        p.setPen(QColor(theme.FAINT))
        p.drawText(QRect(x, cy - 12, 28, 24), Qt.AlignmentFlag.AlignCenter, str(index.row() + 1))
        x += 36

        # Thumbnail
        trect = QRectF(x, cy - THUMB_H / 2, THUMB_W, THUMB_H)
        tpath = QPainterPath()
        tpath.addRoundedRect(trect, 8, 8)
        p.fillPath(tpath, QColor("#0b0d11"))
        if thumb is not None and not thumb.isNull():
            p.setClipPath(tpath)
            scaled = thumb.size().scaled(trect.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio)
            tx = trect.left() + (trect.width() - scaled.width()) / 2
            ty = trect.top() + (trect.height() - scaled.height()) / 2
            p.drawPixmap(QRectF(tx, ty, scaled.width(), scaled.height()), thumb, QRectF(thumb.rect()))
            p.setClipping(False)
        else:
            p.setPen(QColor(theme.FAINT))
            p.setFont(option.font)
            p.drawText(trect, Qt.AlignmentFlag.AlignCenter, "⚠" if clip.error else "···")
        x += THUMB_W + 18

        # Right column: (edited) duration + size
        right_w = 84
        rx = int(card.right()) - right_w - 14
        dur_font = QFont(option.font)
        dur_font.setPointSizeF(11.5)
        dur_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(dur_font)
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRect(rx, cy - 22, right_w, 22), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                   fmt_duration(seg.length) if not clip.error else "—")
        small = QFont(option.font)
        small.setPointSizeF(9)
        p.setFont(small)
        p.setPen(QColor(theme.MUTED))
        sub = f"of {fmt_duration(clip.duration)}" if abs(seg.length - clip.duration) > 0.5 else fmt_size(clip.size)
        p.drawText(QRect(rx, cy + 2, right_w, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, sub)

        # Middle column
        text_w = max(60, rx - x - 12)
        name_font = QFont(option.font)
        name_font.setPointSizeF(10.5)
        name_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(name_font)
        p.setPen(QColor(theme.TEXT))
        name = QFontMetrics(name_font).elidedText(clip.name, Qt.TextElideMode.ElideMiddle, text_w)
        p.drawText(QRect(x, cy - 32, text_w, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(small)
        badges = edit_badges(seg) if not clip.error else []
        if badges:
            date_line = "   ".join(badges)
            p.setPen(QColor(theme.WARN))
        elif clip.recorded:
            when = clip.recorded.astimezone().strftime("%d %b %Y, %H:%M:%S")
            date_line = f"{when}   ·   from {clip.date_source}"
            p.setPen(QColor(theme.MUTED))
        else:
            date_line = "Unknown date"
            p.setPen(QColor(theme.MUTED))
        p.drawText(QRect(x, cy - 9, text_w, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(small).elidedText(date_line, Qt.TextElideMode.ElideRight, text_w))

        if clip.error:
            meta, color = clip.error, theme.ERROR
        elif clip.is_image:
            meta, color = f"Photo   ·   {fmt_resolution(clip.v)}", theme.ACCENT_2
        else:
            v = clip.v
            parts = [fmt_resolution(v), fmt_fps(v.fps), codec_label(v.codec)]
            if v.pix_fmt and ("10" in v.pix_fmt or "12" in v.pix_fmt):
                parts.append("10-bit")
            if clip.a:
                parts.append(f"{codec_label(clip.a.codec)} {clip.a.sample_rate / 1000:g} kHz")
                if any(a.codec == "apple_apac" for a in clip.audio):
                    parts.append("Spatial audio")
                elif len(clip.audio) > 1:
                    parts.append(f"{len(clip.audio)} audio tracks")
            else:
                parts.append("no audio")
            meta, color = "   ·   ".join(parts), theme.ACCENT_2
        p.setPen(QColor(color))
        p.drawText(QRect(x, cy + 12, text_w, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(small).elidedText(meta, Qt.TextElideMode.ElideRight, text_w))
        p.restore()


TILE_W, TILE_H = 120, 104


class BinDelegate(QStyledItemDelegate):
    """Compact media tile: thumbnail with duration, name underneath, ✓ if used."""

    def __init__(self, lookup, used=None, parent=None):
        super().__init__(parent)
        self.lookup = lookup
        self.used = used or (lambda cid: False)

    def sizeHint(self, option, index):
        return QSize(TILE_W, TILE_H)

    def paint(self, p: QPainter, option, index):
        seg, thumb, severity = self.lookup(index.data(ID_ROLE))
        if seg is None:
            return
        clip = seg.clip
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(4, 4, -4, -4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        trect = QRectF(r.left(), r.top(), r.width(), r.width() * 9 / 16)
        path = QPainterPath()
        path.addRoundedRect(trect, 8, 8)
        p.fillPath(path, QColor("#0b0d11"))
        if thumb is not None and not thumb.isNull():
            p.setClipPath(path)
            scaled = thumb.size().scaled(trect.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding)
            tx = trect.left() + (trect.width() - scaled.width()) / 2
            ty = trect.top() + (trect.height() - scaled.height()) / 2
            p.drawPixmap(QRectF(tx, ty, scaled.width(), scaled.height()), thumb, QRectF(thumb.rect()))
            p.setClipping(False)
        small = QFont(option.font)
        small.setPointSizeF(8.5)
        p.setFont(small)
        badge = "Photo" if clip.is_image else ("⚠" if clip.error else fmt_duration(clip.duration))
        fm = QFontMetrics(small)
        bw = fm.horizontalAdvance(badge) + 10
        br = QRectF(trect.right() - bw - 4, trect.bottom() - 20, bw, 16)
        bp = QPainterPath()
        bp.addRoundedRect(br, 4, 4)
        p.fillPath(bp, QColor(0, 0, 0, 170))
        p.setPen(QColor("#ffffff"))
        p.drawText(br, Qt.AlignmentFlag.AlignCenter, badge)
        if clip.error:
            p.fillRect(QRectF(trect.left() + 6, trect.top() + 6, 8, 8), QColor(theme.ERROR))
        if self.used(clip.id):
            cr = QRectF(trect.left() + 5, trect.top() + 5, 18, 18)
            cp = QPainterPath()
            cp.addEllipse(cr)
            p.fillPath(cp, QColor(theme.ACCENT))
            p.setPen(QColor("#0b0d12"))
            p.drawText(cr, Qt.AlignmentFlag.AlignCenter, "✓")
        if selected or hover:
            p.setPen(QPen(QColor(theme.ACCENT if selected else "#3a4254"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        p.setPen(QColor(theme.TEXT if selected else theme.MUTED))
        p.drawText(QRectF(r.left(), trect.bottom() + 4, r.width(), 18),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(clip.name, Qt.TextElideMode.ElideMiddle, int(r.width())))
        p.restore()


class BinList(QListWidget):
    """Media library: tiles can be dragged onto the timeline."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QAbstractItemView, QListView
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setGridSize(QSize(TILE_W + 4, TILE_H + 4))
        self.setWrapping(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def mimeTypes(self):
        from .timeline import CLIP_MIME
        return [CLIP_MIME]

    def mimeData(self, items):
        from PySide6.QtCore import QMimeData
        from .timeline import CLIP_MIME
        md = QMimeData()
        md.setData(CLIP_MIME, "\n".join(i.data(ID_ROLE) for i in items).encode("utf-8"))
        return md

    def ids(self) -> list[str]:
        return [self.item(i).data(ID_ROLE) for i in range(self.count())]

    def selected_ids(self) -> list[str]:
        rows = sorted(self.row(i) for i in self.selectedItems())
        return [self.item(r).data(ID_ROLE) for r in rows]


class ClipList(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setDropIndicatorShown(True)
        self.verticalScrollBar().setSingleStep(24)

    def ids(self) -> list[str]:
        return [self.item(i).data(ID_ROLE) for i in range(self.count())]
