"""Colours, stylesheet and the programmatically drawn app icon."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap

BG = "#0e1014"
SURFACE = "#15181e"
CARD = "#1b1f27"
CARD_HOVER = "#212632"
BORDER = "#272c37"
TEXT = "#e8eaf0"
MUTED = "#8b92a3"
FAINT = "#5c6375"
ACCENT = "#7c8cff"
ACCENT_HOVER = "#909eff"
ACCENT_2 = "#5ad1c6"
OK = "#4cc38a"
WARN = "#f0b44c"
ERROR = "#f16b6b"

FONT_FAMILY = "Segoe UI Variable Text, Segoe UI, Noto Sans Georgian, Sylfaen, sans-serif"

STYLESHEET = f"""
* {{
    font-family: {FONT_FAMILY};
    font-size: 10pt;
    color: {TEXT};
}}
QMainWindow, QDialog, #central {{ background: {BG}; }}
QToolTip {{
    background: {CARD}; color: {TEXT}; border: 1px solid {BORDER};
    padding: 6px 8px; border-radius: 6px;
}}
#title {{ font-size: 17pt; font-weight: 600; }}
#subtitle {{ color: {MUTED}; }}
#muted {{ color: {MUTED}; }}
#folderLabel {{ color: {MUTED}; }}
#emptyTitle {{ font-size: 15pt; font-weight: 600; }}
#emptyText {{ color: {MUTED}; font-size: 10.5pt; }}
#summary {{ color: {MUTED}; }}
#dialogTitle {{ font-size: 13pt; font-weight: 600; }}
#issueTitle {{ font-weight: 600; }}
#issueText {{ color: {MUTED}; }}

QPushButton {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 7px 14px;
}}
QPushButton:hover {{ background: {CARD_HOVER}; border-color: #343b4a; }}
QPushButton:pressed {{ background: {SURFACE}; }}
QPushButton:disabled {{ color: {FAINT}; background: {SURFACE}; border-color: {SURFACE}; }}
QPushButton#primary {{
    background: {ACCENT}; border: none; color: #0b0d12; font-weight: 600;
    padding: 9px 22px;
}}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: #2a3050; color: #6d7393; }}
QPushButton#danger {{ color: {ERROR}; }}
QPushButton#ghost {{ background: transparent; border: none; color: {MUTED}; padding: 6px 10px; }}
QPushButton#ghost:hover {{ color: {TEXT}; background: {CARD}; }}
QPushButton#ghost:disabled {{ color: {FAINT}; background: transparent; }}

QCheckBox {{ color: {MUTED}; spacing: 7px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid #3a4150; background: {SURFACE};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QRadioButton {{ spacing: 10px; font-weight: 600; }}
QRadioButton:disabled {{ color: {FAINT}; }}
QRadioButton::indicator {{
    width: 14px; height: 14px; border-radius: 9px;
    border: 2px solid #4a5263; background: {SURFACE};
}}
QRadioButton::indicator:checked {{
    border-color: {ACCENT};
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
                                stop:0 {ACCENT}, stop:0.55 {ACCENT}, stop:0.6 {SURFACE}, stop:1 {SURFACE});
}}
QLabel:disabled {{ color: {FAINT}; }}

QListWidget {{
    background: transparent; border: none; outline: none;
}}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: #2c3240; border-radius: 3px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: #3a4254; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; height: 0;
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QProgressBar {{
    background: {CARD}; border: none; border-radius: 4px; height: 8px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{
    border-radius: 4px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2});
}}
#banner {{ border-radius: 10px; padding: 0; }}
#panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
#dropZone {{ border: 2px dashed #2b3140; border-radius: 16px; background: {SURFACE}; }}
#dropZone[active="true"] {{ border-color: {ACCENT}; }}
QMessageBox {{ background: {SURFACE}; }}
QMessageBox QLabel {{ color: {TEXT}; }}

/* ---- editor ---------------------------------------------------------- */
QSplitter::handle {{ background: transparent; width: 10px; }}
#editorPanel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
#previewView {{ background: #07080a; border: none; border-radius: 10px; }}
#timeLabel {{ color: {MUTED}; font-size: 9.5pt; }}
#sectionTitle {{ font-weight: 600; color: {TEXT}; }}
#hint {{ color: {FAINT}; font-size: 9pt; }}
#valueLabel {{ color: {MUTED}; min-width: 46px; }}
QTabWidget::pane {{ border: none; background: transparent; top: -1px; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: transparent; color: {MUTED}; padding: 7px 12px; margin-right: 2px;
    border: none; border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:disabled {{ color: {FAINT}; }}
QPushButton#chip {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; padding: 5px 12px;
    color: {MUTED};
}}
QPushButton#chip:hover {{ color: {TEXT}; }}
QPushButton#chip:checked {{ background: {ACCENT}; color: #0b0d12; border-color: {ACCENT}; font-weight: 600; }}
QPushButton#icon {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 18px;
    min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; padding: 0; font-size: 12pt; }}
QPushButton#icon:hover {{ background: {CARD_HOVER}; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QSlider::groove:horizontal:disabled {{ background: {SURFACE}; }}
QSlider::sub-page:horizontal:disabled {{ background: {BORDER}; }}
QSlider::handle:horizontal:disabled {{ background: {FAINT}; }}
QComboBox, QLineEdit, QPlainTextEdit, QDoubleSpinBox {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 5px 9px;
    selection-background-color: {ACCENT}; selection-color: #0b0d12;
}}
QComboBox:hover, QLineEdit:hover, QPlainTextEdit:hover, QDoubleSpinBox:hover {{ border-color: #3a4254; }}
QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus, QDoubleSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {CARD}; border: 1px solid {BORDER}; selection-background-color: {CARD_HOVER};
    selection-color: {TEXT}; outline: none; padding: 4px;
}}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}
QToolButton#addMedia {{
    background: {ACCENT}; color: #0b0d12; font-weight: 600; border: none; border-radius: 8px;
    padding: 6px 30px 6px 14px;
}}
QToolButton#addMedia:hover {{ background: {ACCENT_HOVER}; }}
QToolButton#addMedia::menu-button {{
    border: none; border-left: 1px solid rgba(0, 0, 0, 60); width: 22px;
    border-top-right-radius: 8px; border-bottom-right-radius: 8px;
}}
QToolButton#addMedia::menu-arrow {{ width: 9px; height: 9px; }}
QMenu {{ background: {CARD}; border: 1px solid {BORDER}; padding: 5px; border-radius: 8px; }}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 5px; color: {TEXT}; }}
QMenu::item:selected {{ background: {CARD_HOVER}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; }}
"""


def app_icon_image(size: int = 256) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 256
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor(ACCENT))
    grad.setColorAt(1, QColor(ACCENT_2))
    path = QPainterPath()
    path.addRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 56 * s, 56 * s)
    p.fillPath(path, grad)
    # two clips joining into one
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 110))
    p.drawRoundedRect(QRectF(46 * s, 70 * s, 76 * s, 116 * s), 14 * s, 14 * s)
    p.setBrush(QColor(255, 255, 255, 235))
    p.drawRoundedRect(QRectF(104 * s, 70 * s, 106 * s, 116 * s), 14 * s, 14 * s)
    tri = QPainterPath()
    tri.moveTo(QPointF(142 * s, 104 * s))
    tri.lineTo(QPointF(182 * s, 128 * s))
    tri.lineTo(QPointF(142 * s, 152 * s))
    tri.closeSubpath()
    p.fillPath(tri, QColor("#4b5bd6"))
    p.end()
    return img


def app_icon() -> QIcon:
    icon = QIcon()
    for sz in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(QPixmap.fromImage(app_icon_image(sz)))
    return icon
