"""Small styled dialogs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QRadioButton, QScrollArea, QVBoxLayout, QWidget)

from .. import compat
from . import theme


class ChoiceDialog(QDialog):
    """Title, explanation, optional list of issues and a row of choice buttons.

    ``buttons`` is a list of (key, label, style) with style in
    {"primary", "danger", "normal"}. ``exec_choice()`` returns the chosen key
    or None when the dialog was closed.
    """

    def __init__(self, parent, title: str, text: str, buttons: list[tuple[str, str, str]],
                 issues: list[compat.Issue] | None = None, footer: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self.choice = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(12)

        t = QLabel(title)
        t.setObjectName("dialogTitle")
        t.setWordWrap(True)
        lay.addWidget(t)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setObjectName("muted")
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(body)

        if issues:
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(8)
            for issue in issues:
                bl.addWidget(self._issue_card(issue))
            bl.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(box)
            scroll.setMinimumHeight(min(360, 120 * len(issues)))
            lay.addWidget(scroll, 1)

        if footer:
            f = QLabel(footer)
            f.setWordWrap(True)
            f.setObjectName("muted")
            lay.addWidget(f)

        row = QHBoxLayout()
        row.addStretch(1)
        for key, label, style in buttons:
            b = QPushButton(label)
            if style in ("primary", "danger"):
                b.setObjectName(style)
            b.clicked.connect(lambda _=False, k=key: self._choose(k))
            row.addWidget(b)
            if style == "primary":
                b.setDefault(True)
        lay.addSpacing(4)
        lay.addLayout(row)

    @staticmethod
    def _issue_card(issue: compat.Issue) -> QWidget:
        frame = QFrame()
        frame.setObjectName("panel")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(14, 10, 14, 12)
        fl.setSpacing(4)
        color = theme.ERROR if issue.severity == compat.BLOCKER else theme.WARN
        mark = "✕" if issue.severity == compat.BLOCKER else "!"
        head = QLabel(f"<span style='color:{color}'>{mark}</span>&nbsp;&nbsp;{issue.title}")
        head.setObjectName("issueTitle")
        fl.addWidget(head)
        detail = QLabel(issue.detail)
        detail.setObjectName("issueText")
        detail.setWordWrap(True)
        detail.setTextFormat(Qt.TextFormat.PlainText)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        fl.addWidget(detail)
        return frame

    def _choose(self, key):
        self.choice = key
        self.accept()

    def exec_choice(self):
        self.exec()
        return self.choice


class FrameRateDialog(QDialog):
    """Clips have different frame rates: keep them (lossless) or convert the slow ones."""

    KEEP = "keep"

    def __init__(self, parent, clips, plan, other_issues, default: str, can_convert: bool):
        super().__init__(parent)
        from ..media import fmt_duration, fmt_fps
        self.setWindowTitle("Different frame rates")
        self.setMinimumWidth(600)
        self.choice = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(12)

        t = QLabel("Your clips have different frame rates")
        t.setObjectName("dialogTitle")
        lay.addWidget(t)
        groups = compat.frame_rate_groups(clips)
        summary = "   ·   ".join(f"{fmt_fps(r)}: {len(m)} clip{'s' if len(m) != 1 else ''}"
                                for r, m in sorted(groups.items(), reverse=True))
        s = QLabel(summary)
        s.setObjectName("issueTitle")
        lay.addWidget(s)
        why = QLabel(
            "Why? iPhones (and many Android phones) lower the frame rate automatically in dim "
            "light when “Auto FPS” is on (Settings › Camera › Record Video). Fewer frames per "
            "second let each frame collect more light, so the video is brighter and less "
            "noisy. Turn Auto FPS off if you always want the full 60 fps.")
        why.setWordWrap(True)
        why.setObjectName("muted")
        lay.addWidget(why)

        conv_dur = sum(c.duration for c in plan.convert)
        n = len(plan.convert)
        target = fmt_fps(plan.fps)
        options = [
            (self.KEEP, "Keep original frame rates — lossless (recommended)",
             "Every clip is copied untouched. Slower clips simply play at their own rate; "
             "modern players and phones handle this without any problem."),
            (DUPLICATE_KEY, f"Convert to {target} — repeat frames",
             f"Only {n} clip{'s' if n != 1 else ''} ({fmt_duration(conv_dur)}) "
             f"{'are' if n != 1 else 'is'} re-encoded, at very high quality, matching your {target} clips "
             "(same codec, bit depth and HDR). Frames are shown twice/three times, so motion "
             "looks exactly like the original. Your other clips stay bit-exact."),
            (BLEND_KEY, f"Convert to {target} — blend frames",
             "Same as above, but the in-between frames are blended from their neighbours. "
             "Motion looks smoother; fast movement can show slight ghosting."),
        ]
        self.buttons = {}
        group = QButtonGroup(self)
        for key, label, desc in options:
            card = QFrame()
            card.setObjectName("panel")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 10, 14, 12)
            cl.setSpacing(4)
            rb = QRadioButton(label)
            rb.setObjectName("issueTitle")
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setObjectName("issueText")
            d.setContentsMargins(26, 0, 0, 0)
            cl.addWidget(rb)
            cl.addWidget(d)
            enabled = key == self.KEEP or can_convert
            rb.setEnabled(enabled)
            d.setEnabled(enabled)
            group.addButton(rb)
            self.buttons[key] = rb
            lay.addWidget(card)
        (self.buttons.get(default) if self.buttons[default].isEnabled() else self.buttons[self.KEEP]).setChecked(True)

        notes = []
        if not can_convert:
            notes.append("Conversion is available for H.264 and HEVC footage only.")
        else:
            notes.append("Converting is not lossless for the converted clips and is slow for 4K "
                         "(usually slower than real time on the CPU). Audio is never re-encoded.")
        if other_issues:
            notes.append("Also note: " + "; ".join(i.title.lower() for i in other_issues) + ".")
        foot = QLabel("\n".join(notes))
        foot.setWordWrap(True)
        foot.setObjectName("muted")
        lay.addWidget(foot)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        go = QPushButton("Continue")
        go.setObjectName("primary")
        go.setDefault(True)
        go.clicked.connect(self._accept)
        row.addWidget(cancel)
        row.addWidget(go)
        lay.addLayout(row)

    def _accept(self):
        self.choice = next(k for k, b in self.buttons.items() if b.isChecked())
        self.accept()

    def exec_choice(self):
        self.exec()
        return self.choice


DUPLICATE_KEY = "duplicate"
BLEND_KEY = "blend"


def ask(parent, title, text, buttons, issues=None, footer=None):
    return ChoiceDialog(parent, title, text, buttons, issues, footer).exec_choice()


def inform(parent, title, text):
    ask(parent, title, text, [("ok", "OK", "primary")])
