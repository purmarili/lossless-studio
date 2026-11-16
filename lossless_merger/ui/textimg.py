"""Caption images. Drawn with Qt so any script (Georgian, Cyrillic, emoji)
renders with the system fonts; the same drawing is shown in the preview and
burned into the export. Images are tight around the text and placed by the
renderer, so animations (pop, slide) only move a small picture."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath,
                           QPainterPathStroker, QPen)

from ..project import TextItem

FONT = "Segoe UI"


@dataclass
class _Layout:
    lines: list[str]
    widths: list[float]
    font: QFont
    px: float
    line_h: float
    ascent: float
    pad_x: float
    pad_y: float
    margin: float              # extra room for outlines / glow
    w: int
    h: int


def _font(item: TextItem, H: int) -> tuple[QFont, float]:
    px = max(6.0, H * item.size / 100.0)
    f = QFont(FONT)
    f.setPixelSize(int(round(px)))
    weight = QFont.Weight.Black if item.style == "bold" else (
        QFont.Weight.Bold if item.bold else QFont.Weight.Medium)
    f.setWeight(weight)
    return f, px


def _wrap(text: str, fm: QFontMetricsF, max_w: float) -> list[str]:
    lines = []
    for para in text.split("\n"):
        words = para.split(" ")
        cur = ""
        for w in words:
            trial = (cur + " " + w) if cur else w
            if fm.horizontalAdvance(trial) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def layout(item: TextItem, W: int, H: int) -> _Layout:
    font, px = _font(item, H)
    text = item.text.strip()
    if item.style == "bold":
        text = text.upper()
    fm = QFontMetricsF(font)
    lines = _wrap(text, fm, W * 0.86)
    widths = [fm.horizontalAdvance(ln) for ln in lines]
    line_h = fm.height() * 1.05
    pad_x = px * (0.45 if item.style in ("box", "subtitle") else 0.15)
    pad_y = px * (0.22 if item.style in ("box", "subtitle") else 0.08)
    margin = px * {"neon": 0.6, "outline": 0.2, "bold": 0.2, "shadow": 0.12}.get(item.style, 0.05)
    w = int(max(widths or [1]) + 2 * (pad_x + margin)) + 2
    h = int(line_h * len(lines) + 2 * (pad_y + margin)) + 2
    return _Layout(lines, widths, font, px, line_h, fm.ascent(), pad_x, pad_y, margin, w, h)


def _paths(lay: _Layout, visible: int | None = None) -> list[QPainterPath]:
    """One path per line (only the first *visible* characters for typewriter)."""
    out, left = [], visible
    for i, (ln, lw) in enumerate(zip(lay.lines, lay.widths)):
        if left is not None:
            ln = ln[:max(0, left)]
            left -= len(lay.lines[i]) + 1
        x = (lay.w - lw) / 2
        y = lay.margin + lay.pad_y + i * lay.line_h + lay.ascent
        p = QPainterPath()
        if ln:
            p.addText(QPointF(x, y), lay.font, ln)
        out.append(p)
    return out


def _draw(item: TextItem, lay: _Layout, visible: int | None = None) -> QImage:
    img = QImage(max(2, lay.w), max(2, lay.h), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(item.color)
    px = lay.px
    paths = _paths(lay, visible)
    style = item.style
    if style == "box":
        full = QRectF(lay.margin, lay.margin, lay.w - 2 * lay.margin, lay.h - 2 * lay.margin)
        bg = QPainterPath()
        bg.addRoundedRect(full, px * 0.3, px * 0.3)
        p.fillPath(bg, QColor(0, 0, 0, 160) if color.lightness() > 110 else QColor(255, 255, 255, 220))
    elif style == "subtitle":
        for i, lw in enumerate(lay.widths):
            if not lay.lines[i]:
                continue
            r = QRectF((lay.w - lw) / 2 - lay.pad_x * 0.6, lay.margin + lay.pad_y * 0.5 + i * lay.line_h,
                       lw + lay.pad_x * 1.2, lay.line_h)
            p.fillRect(r, QColor(0, 0, 0, 140))
    for path in paths:
        if path.isEmpty():
            continue
        if style == "shadow":
            p.fillPath(path.translated(px * 0.05, px * 0.06), QColor(0, 0, 0, 170))
        elif style in ("outline", "bold"):
            st = QPainterPathStroker()
            st.setWidth(px * (0.16 if style == "outline" else 0.12))
            st.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.fillPath(st.createStroke(path), QColor(0, 0, 0, 230))
            if style == "bold":
                p.fillPath(path.translated(px * 0.04, px * 0.06), QColor(0, 0, 0, 150))
        elif style == "neon":
            for width, alpha in ((px * 0.55, 40), (px * 0.32, 70), (px * 0.16, 140)):
                p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), alpha), width,
                              Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            p.setPen(Qt.PenStyle.NoPen)
        fill = QColor("#ffffff") if style == "neon" else color
        p.fillPath(path, QBrush(fill))
    p.end()
    return img


def placement(item: TextItem, W: int, H: int, w: int, h: int) -> tuple[int, int]:
    cx, cy = item.center()
    x = int(round(cx * W - w / 2))
    y = int(round(cy * H - h / 2))
    return max(0, min(W - w, x)), max(0, min(H - h, y))


def text_image(item: TextItem, W: int, H: int, visible: int | None = None) -> tuple[QImage, int, int]:
    """(image, x, y) of the caption on a W x H canvas."""
    lay = layout(item, W, H)
    img = _draw(item, lay, visible)
    x, y = placement(item, W, H, img.width(), img.height())
    return img, x, y


def typewriter_count(item: TextItem) -> int:
    return len(item.text.strip())


def typewriter_timing(item: TextItem) -> tuple[int, float]:
    """(number of steps, seconds per step)."""
    n = max(1, typewriter_count(item))
    total = min(max(0.4, 0.06 * n), 2.0, item.duration * 0.6)
    steps = min(n, 40)
    return steps, total / steps


def png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)


def text_render(item: TextItem, W: int, H: int):
    """Caption for the exporter (render.TextRender)."""
    from .. import render
    lay = layout(item, W, H)
    img = _draw(item, lay)
    x, y = placement(item, W, H, img.width(), img.height())
    steps, step_dur = [], 0.05
    if item.anim_in == "typewriter":
        n_steps, step_dur = typewriter_timing(item)
        n = typewriter_count(item)
        for k in range(1, n_steps + 1):
            steps.append(png_bytes(_draw(item, lay, visible=round(n * k / n_steps) - (1 if k == n_steps else 0))))
    return render.TextRender(png_bytes(img), x, y, img.width(), img.height(), steps, step_dur)
