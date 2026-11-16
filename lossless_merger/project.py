"""The edit project, CapCut style.

* **Main track**: segments (a video clip, a photo or a freeze frame + edits),
  played one after another, optionally joined by transitions.
* **Text track**: captions / titles placed anywhere on the timeline.
* **Overlay track**: picture-in-picture videos and images.
* **Audio track**: music, voice-overs and sounds.
* Project settings: canvas shape, background, export format, fades.

Splitting a clip creates two segments that point at the same file, so the
original recordings are never modified.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .media import Clip


def _uid() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Choices offered in the UI
# --------------------------------------------------------------------------- #
SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)

TRANSITIONS = {            # FFmpeg xfade name -> label
    "none": "None",
    "fade": "Crossfade",
    "fadeblack": "Fade through black",
    "fadewhite": "Fade through white",
    "dissolve": "Dissolve",
    "slideleft": "Slide left",
    "slideright": "Slide right",
    "slideup": "Slide up",
    "wipeleft": "Wipe",
    "circleopen": "Circle open",
    "radial": "Radial",
    "smoothleft": "Smooth slide",
    "zoomin": "Zoom in",
    "pixelize": "Pixelize",
}

LOOKS = {                  # preset -> (label, brightness, contrast, saturation, warmth)
    "none": ("Original", 0.0, 1.0, 1.0, 0.0),
    "vivid": ("Vivid", 0.0, 1.08, 1.35, 0.0),
    "warm": ("Warm", 0.0, 1.0, 1.08, 0.5),
    "cool": ("Cool", 0.0, 1.0, 1.0, -0.5),
    "bw": ("Black & white", 0.0, 1.1, 0.0, 0.0),
    "faded": ("Faded", 0.04, 0.85, 0.75, 0.15),
    "drama": ("Dramatic", -0.02, 1.25, 0.85, 0.0),
    "golden": ("Golden hour", 0.02, 1.05, 1.15, 0.8),
}

CANVASES = {               # key -> (label, aspect w/h or None = like the footage)
    "original": ("Original shape", None),
    "16:9": ("16:9  Landscape (YouTube)", 16 / 9),
    "9:16": ("9:16  Vertical (Reels, TikTok, Shorts)", 9 / 16),
    "1:1": ("1:1  Square", 1.0),
    "4:5": ("4:5  Portrait (Instagram feed)", 4 / 5),
}

BACKGROUNDS = {"black": "Black bars", "blur": "Blurred video"}

TEXT_POSITIONS = {"top": "Top", "center": "Middle", "bottom": "Bottom"}
TEXT_STYLES = {
    "shadow": "Classic",
    "box": "Box",
    "outline": "Outline",
    "neon": "Neon",
    "subtitle": "Subtitle",
    "bold": "Headline",
}
TEXT_ANIMS_IN = {"none": "None", "fade": "Fade", "pop": "Pop", "slide_up": "Slide up",
                 "slide_left": "Slide in", "typewriter": "Typewriter"}
TEXT_ANIMS_OUT = {"none": "None", "fade": "Fade", "pop": "Pop", "slide_down": "Slide down"}

STILL_LENGTH = 4.0         # default length of a photo / freeze frame


# --------------------------------------------------------------------------- #
# Main track
# --------------------------------------------------------------------------- #
@dataclass
class Transform:
    """Zoom and framing on top of fitting the clip to the canvas."""
    zoom: float = 1.0            # 1 = fit/fill, up to 4
    x: float = 0.0               # -1..1: which part of the zoomed picture is shown
    y: float = 0.0

    def is_identity(self) -> bool:
        return abs(self.zoom - 1) < 1e-3

    def lerp(self, other: "Transform", u: float) -> "Transform":
        return Transform(self.zoom + (other.zoom - self.zoom) * u, self.x + (other.x - self.x) * u,
                         self.y + (other.y - self.y) * u)


def ease(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


@dataclass
class ClipEdit:
    start: float = 0.0           # in-point, seconds of the source clip
    end: float = 0.0             # out-point, 0 = end of clip
    speed: float = 1.0
    reverse: bool = False
    volume: float = 1.0          # 0..2
    muted: bool = False
    denoise: bool = False        # reduce background noise in the sound
    audio_fade_in: float = 0.0
    audio_fade_out: float = 0.0
    rotate: int = 0              # extra rotation (clockwise) 0/90/180/270
    flip: bool = False           # mirror horizontally
    look: str = "none"
    brightness: float = 0.0      # -0.5..0.5   (added to the look)
    contrast: float = 1.0        # 0.5..2
    saturation: float = 1.0      # 0..2
    warmth: float = 0.0          # -1..1
    sharpen: float = 0.0         # 0..1
    vignette: float = 0.0        # 0..1
    blur: float = 0.0            # 0..1
    stabilize: bool = False
    fade_in: float = 0.0         # picture fades in from black (seconds)
    fade_out: float = 0.0
    tf: Transform = field(default_factory=Transform)          # zoom/position (start)
    tf_end: Transform | None = None                           # set -> animated (Ken Burns)
    transition: str = "none"     # transition into the NEXT segment
    transition_dur: float = 0.6

    # -- groups of edits ------------------------------------------------------
    def color_params(self) -> tuple[float, float, float, float]:
        _, b, c, s, w = LOOKS.get(self.look, LOOKS["none"])
        return (b + self.brightness, c * self.contrast, s * self.saturation, max(-1.0, min(1.0, w + self.warmth)))

    @property
    def has_color(self) -> bool:
        b, c, s, w = self.color_params()
        return abs(b) > 1e-3 or abs(c - 1) > 1e-3 or abs(s - 1) > 1e-3 or abs(w) > 1e-3

    @property
    def has_transform(self) -> bool:
        return not self.tf.is_identity() or (self.tf_end is not None and not self.tf_end.is_identity()) \
            or (self.tf_end is not None and (abs(self.tf.x - self.tf_end.x) > 1e-3 or abs(self.tf.y - self.tf_end.y) > 1e-3))

    @property
    def has_effects(self) -> bool:
        return self.sharpen > 1e-3 or self.vignette > 1e-3 or self.blur > 1e-3 or self.stabilize

    @property
    def has_visual(self) -> bool:
        """Edits that change every frame of the clip (it must be re-encoded)."""
        return (self.rotate % 360 != 0 or self.flip or self.has_color or self.has_transform
                or self.has_effects or self.reverse or abs(self.speed - 1) > 1e-6)

    @property
    def has_audio_edit(self) -> bool:
        return (self.muted or abs(self.volume - 1) > 1e-3 or abs(self.speed - 1) > 1e-6 or self.reverse
                or self.denoise or self.audio_fade_in > 0 or self.audio_fade_out > 0
                or self.fade_in > 0 or self.fade_out > 0)

    def transform_at(self, u: float) -> Transform:
        """Zoom/position at relative time u (0..1) of the clip."""
        if self.tf_end is None:
            return self.tf
        return self.tf.lerp(self.tf_end, ease(u))


@dataclass
class Segment:
    clip: Clip
    edit: ClipEdit = field(default_factory=ClipEdit)
    id: str = field(default_factory=_uid)
    kind: str = "video"          # video | image | freeze
    still_at: float = 0.0        # freeze: source time of the frozen frame
    still_len: float = STILL_LENGTH

    @property
    def is_still(self) -> bool:
        return self.kind in ("image", "freeze")

    # -- timing ----------------------------------------------------------------
    @property
    def media_duration(self) -> float:
        return self.still_len if self.is_still else self.clip.duration

    @property
    def src_in(self) -> float:
        if self.is_still:
            return 0.0
        return max(0.0, min(self.edit.start, self.clip.duration))

    @property
    def src_out(self) -> float:
        if self.is_still:
            return self.still_len
        end = self.edit.end if self.edit.end > 0 else self.clip.duration
        return max(self.src_in, min(end, self.clip.duration))

    @property
    def speed(self) -> float:
        return 1.0 if self.is_still else (self.edit.speed or 1.0)

    @property
    def length(self) -> float:
        """Seconds this segment lasts in the final video (before transitions)."""
        return (self.src_out - self.src_in) / self.speed

    @property
    def trimmed(self) -> bool:
        return (not self.is_still) and (self.src_in > 0.01 or self.src_out < self.clip.duration - 0.01)

    @property
    def has_audio(self) -> bool:
        return self.kind == "video" and bool(self.clip.audio)

    @property
    def edited(self) -> bool:
        e = self.edit
        return (self.is_still or self.trimmed or e.has_visual or e.has_audio_edit or e.transition != "none"
                or e.fade_in > 0 or e.fade_out > 0)

    def src_time(self, t_local: float) -> float:
        """Source time shown at *t_local* seconds into the segment."""
        if self.kind == "freeze":
            return self.still_at
        if self.kind == "image":
            return 0.0
        t_local = min(max(t_local, 0.0), self.length)
        if self.edit.reverse:
            return self.src_out - t_local * self.speed
        return self.src_in + t_local * self.speed

    def local_time(self, t_src: float) -> float:
        if self.is_still:
            return 0.0
        if self.edit.reverse:
            return (self.src_out - t_src) / self.speed
        return (t_src - self.src_in) / self.speed

    def clone(self, new_id: bool = True) -> "Segment":
        s = copy.deepcopy(self, {id(self.clip): self.clip})
        if new_id:
            s.id = _uid()
        return s

    def split(self, t_local: float) -> tuple["Segment", "Segment"] | None:
        """Split at *t_local* seconds into the segment; None if too close to an end."""
        if not (0.1 < t_local < self.length - 0.1):
            return None
        a, b = self.clone(new_id=False), self.clone()
        a.edit.transition = "none"           # the cut point itself is a hard cut
        e = self.edit
        if self.is_still:
            a.still_len, b.still_len = t_local, self.still_len - t_local
        else:
            at = self.src_time(t_local)
            if e.reverse:                    # first part plays the end of the range
                a.edit.start, b.edit.end = at, at
            else:
                a.edit.end, b.edit.start = at, at
        # split zoom animation and fades between the halves
        if e.tf_end is not None:
            u = t_local / self.length
            mid = e.transform_at(u)
            a.edit.tf_end, b.edit.tf = copy.deepcopy(mid), copy.deepcopy(mid)
        a.edit.fade_out = a.edit.audio_fade_out = 0.0
        b.edit.fade_in = b.edit.audio_fade_in = 0.0
        return a, b


# --------------------------------------------------------------------------- #
# Other tracks
# --------------------------------------------------------------------------- #
@dataclass
class TextItem:
    text: str = "Your text"
    start: float = 0.0           # timeline seconds
    duration: float = 3.0
    style: str = "shadow"
    position: str = "bottom"     # top | center | bottom | custom
    x: float = 0.5               # custom position: centre, 0..1 of the canvas
    y: float = 0.8
    size: float = 6.0            # % of the frame height
    color: str = "#ffffff"
    bold: bool = True
    anim_in: str = "none"
    anim_out: str = "none"
    anim_dur: float = 0.4
    id: str = field(default_factory=_uid)

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def active(self) -> bool:
        return bool(self.text.strip()) and self.duration > 0.05

    def center(self) -> tuple[float, float]:
        if self.position == "custom":
            return self.x, self.y
        return 0.5, {"top": 0.12, "center": 0.5}.get(self.position, 0.86 if self.style != "subtitle" else 0.9)


@dataclass
class OverlayItem:
    """Picture-in-picture: a video or image on top of the main track."""
    clip: Clip
    kind: str = "video"          # video | image
    start: float = 0.0           # timeline seconds
    duration: float = 4.0
    src_in: float = 0.0
    x: float = 0.75              # centre, 0..1 of the canvas
    y: float = 0.28
    scale: float = 0.35          # width as a fraction of the canvas width
    opacity: float = 1.0
    volume: float = 1.0
    muted: bool = False
    id: str = field(default_factory=_uid)

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def has_audio(self) -> bool:
        return self.kind == "video" and bool(self.clip.audio) and not self.muted and self.volume > 0


@dataclass
class AudioItem:
    path: Path
    media_duration: float
    kind: str = "music"          # music | voice | sound
    start: float = 0.0           # timeline seconds
    src_in: float = 0.0
    duration: float = 0.0        # 0 = until the end of the video (music) / of the file
    volume: float = 0.8
    fade_in: float = 0.0
    fade_out: float = 1.5
    duck: bool = True            # lower this (music) while there is other sound
    loop: bool = True            # music repeats if shorter than the video
    id: str = field(default_factory=_uid)

    @property
    def name(self) -> str:
        return Path(self.path).name

    def length(self, project_duration: float) -> float:
        if self.duration > 0:
            return self.duration
        if self.kind == "music" and self.loop:
            return max(0.0, project_duration - self.start)
        return max(0.0, self.media_duration - self.src_in)


# --------------------------------------------------------------------------- #
# Export formats
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExportPreset:
    key: str
    label: str
    hint: str
    max_height: int | None = None    # shorter side limit (None = keep)
    codec: str | None = None         # None = like the footage
    sdr: bool = False                # convert HDR to normal (SDR) colours
    crf: int = 16
    max_fps: float | None = None
    audio_bitrate: str = "256k"


PRESETS = {
    "original": ExportPreset("original", "Original quality",
                             "Lossless wherever possible — only edited parts are re-encoded"),
    "4k": ExportPreset("4k", "4K HEVC — smaller file",
                       "Everything re-encoded to HEVC, HDR kept. About half the size.",
                       max_height=2160, codec="hevc", crf=21, max_fps=60),
    "1080p": ExportPreset("1080p", "1080p — plays everywhere",
                          "H.264, normal colours (SDR). For WhatsApp, Instagram, TVs, old PCs.",
                          max_height=1080, codec="h264", sdr=True, crf=19, max_fps=60,
                          audio_bitrate="192k"),
    "720p": ExportPreset("720p", "720p — small file",
                         "H.264, normal colours, 30 fps. Good for sending by message.",
                         max_height=720, codec="h264", sdr=True, crf=23, max_fps=30,
                         audio_bitrate="128k"),
}


@dataclass
class Project:
    segments: list[Segment] = field(default_factory=list)
    texts: list[TextItem] = field(default_factory=list)
    overlays: list[OverlayItem] = field(default_factory=list)
    audios: list[AudioItem] = field(default_factory=list)
    canvas: str = "original"
    background: str = "black"
    preset: str = "original"
    fade_in: bool = False            # fade from black at the start
    fade_out: bool = False           # fade to black at the end

    # -- state ---------------------------------------------------------------
    @property
    def edited(self) -> bool:
        return (any(s.edited for s in self.segments) or self.canvas != "original"
                or self.preset != "original" or self.fade_in or self.fade_out or self._has_splits()
                or bool(self.texts) or bool(self.overlays) or bool(self.audios))

    def _has_splits(self) -> bool:
        paths = [s.clip.id for s in self.segments]
        return len(paths) != len(set(paths))

    @property
    def clips(self) -> list[Clip]:
        return [s.clip for s in self.segments]

    # -- timing ----------------------------------------------------------------
    def transition_after(self, i: int) -> float:
        """Effective transition length between segment i and i+1 (0 = cut)."""
        if i >= len(self.segments) - 1 or i < 0:
            return 0.0
        a, b = self.segments[i], self.segments[i + 1]
        if a.edit.transition == "none":
            return 0.0
        # never longer than 40 % of either neighbour
        return max(0.0, min(a.edit.transition_dur, a.length * 0.4, b.length * 0.4))

    def starts(self) -> list[float]:
        """Timeline start of every segment."""
        out, t = [], 0.0
        for i, s in enumerate(self.segments):
            out.append(t)
            t += s.length - self.transition_after(i)
        return out

    @property
    def duration(self) -> float:
        return (sum(s.length for s in self.segments)
                - sum(self.transition_after(i) for i in range(len(self.segments))))

    def segment_at(self, t: float) -> tuple[int, float]:
        """(index, local time) of the segment shown at timeline time *t*.
        During a transition the incoming clip is shown from the midpoint."""
        starts = self.starts()
        if not starts:
            return -1, 0.0
        for i in range(len(starts) - 1, -1, -1):
            lead = self.transition_after(i - 1) / 2 if i > 0 else 0.0
            if t >= starts[i] + lead - 1e-9:
                return i, min(t - starts[i], self.segments[i].length)
        return 0, max(0.0, t)

    def audio_length(self, a: AudioItem) -> float:
        return min(a.length(self.duration), max(0.0, self.duration - a.start))

    # -- copies ------------------------------------------------------------------
    def _memo(self) -> dict:
        memo = {}
        for c in [s.clip for s in self.segments] + [o.clip for o in self.overlays]:
            memo[id(c)] = c
        return memo

    def snapshot(self):
        return copy.deepcopy(self, self._memo())

    def copy(self) -> "Project":
        """Independent copy (for rendering while the UI stays editable)."""
        return self.snapshot()

    def restore(self, snap: "Project") -> None:
        other = copy.deepcopy(snap, snap._memo())
        self.__dict__.update(other.__dict__)
