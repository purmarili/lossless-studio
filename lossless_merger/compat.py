"""Decide whether a list of clips can be joined losslessly with stream copy.

FFmpeg's concat demuxer + ``-c copy`` simply appends the compressed packets of
each file. That only produces a valid file when every clip has the same
stream layout and the same codec parameters. Anything that would break
playback is reported as a *blocker*; things that usually work but may cause
minor glitches in some players are reported as *warnings*.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction

from .media import Clip, codec_label, fmt_fps, same_fps

BLOCKER = "blocker"
WARNING = "warning"
FPS_ISSUE = "Different frame rates"


def frame_rate_groups(clips: list[Clip]) -> dict[float, list[Clip]]:
    """{nominal fps: clips}; 59.94 and 60 count as the same rate, 30 and 60 don't."""
    groups: dict[float, list[Clip]] = {}
    for c in clips:
        if c.error or not c.v:
            continue
        r = c.v.nominal_fps
        key = next((k for k in groups if same_fps(k, r)), r)
        groups.setdefault(key, []).append(c)
    return groups


def highest_fps(clips: list[Clip]) -> float:
    return max(frame_rate_groups(clips) or {0.0: []})


def fps_fraction(fps: float) -> Fraction:
    """Exact rational for a (snapped) rate: 59.94 -> 60000/1001."""
    for ntsc in (24, 30, 60, 120):
        if abs(fps - ntsc * 1000 / 1001) < 0.005:
            return Fraction(ntsc * 1000, 1001)
    return Fraction(round(fps * 1000), 1000)


@dataclass
class ConformPlan:
    """Convert only the clips whose frame rate differs; copy the rest untouched."""
    fps: float
    reference: Clip                  # clip whose format the converted clips must match
    convert: list[Clip]              # clips that will be re-encoded

    def describe_counts(self, total: int) -> str:
        n = len(self.convert)
        return f"{n} of {total} clip{'s' if total != 1 else ''}"


def conform_plan(clips: list[Clip], fps: float | None = None) -> ConformPlan:
    """Which clips get re-encoded when converting everything to *fps* (default:
    the highest rate present). Includes clips whose video doesn't match anyway."""
    clips = [c for c in clips if not c.error]
    fps = fps or highest_fps(clips)
    fit = fit_plan(clips, fps)
    return ConformPlan(fps, fit.reference, [c for c in clips if c.id in fit.convert])


@dataclass
class Issue:
    severity: str
    title: str
    detail: str
    clip_ids: set[str] = field(default_factory=set)


@dataclass
class CompatReport:
    issues: list[Issue]

    @property
    def blockers(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == BLOCKER]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def lossless_possible(self) -> bool:
        return not self.blockers

    def clip_severity(self, clip_id: str) -> str | None:
        sev = None
        for i in self.issues:
            if clip_id in i.clip_ids:
                if i.severity == BLOCKER:
                    return BLOCKER
                sev = WARNING
        return sev


def _describe(groups: dict, fmt=str, limit: int = 3) -> str:
    """'3840×2160: a.mp4, b.mp4 (+2 more) · 1920×1080: c.mp4'"""
    parts = []
    for value, clips in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        names = [c.name for c in clips]
        shown = ", ".join(names[:limit])
        if len(names) > limit:
            shown += f" (+{len(names) - limit} more)"
        parts.append(f"{fmt(value)} — {shown}")
    return "\n".join(parts)


def _minority_ids(groups: dict) -> set[str]:
    """Clips that are not part of the largest group (they 'stand out')."""
    if len(groups) < 2:
        return set()
    biggest = max(groups.values(), key=len)
    return {c.id for g in groups.values() if g is not biggest for c in g}


def analyze(clips: list[Clip]) -> CompatReport:
    issues: list[Issue] = []
    clips = [c for c in clips if not c.error]
    if len(clips) < 2:
        return CompatReport(issues)

    def check(severity, title, explanation, key, fmt=str):
        groups = defaultdict(list)
        for c in clips:
            groups[key(c)].append(c)
        if len(groups) > 1:
            issues.append(Issue(severity, title,
                                explanation + "\n\n" + _describe(groups, fmt),
                                _minority_ids(groups)))
            return True
        return False

    # ---- Stream layout -------------------------------------------------- #
    check(BLOCKER, "Different number of video tracks",
          "Every clip must contain the same tracks to be joined without re-encoding.",
          lambda c: len(c.video), lambda n: f"{n} video track(s)")
    layout_ok = not check(
        BLOCKER, "Audio tracks differ",
        "Some clips have a different number of audio tracks (or no audio at all).",
        lambda c: len(c.audio), lambda n: f"{n} audio track(s)" if n else "no audio")

    # ---- Video ---------------------------------------------------------- #
    check(BLOCKER, "Different video codecs",
          "Clips are compressed with different video codecs.",
          lambda c: c.v.codec, codec_label)
    check(BLOCKER, "Different resolutions",
          "Clips were recorded at different resolutions.",
          lambda c: (c.v.width, c.v.height), lambda wh: f"{wh[0]}×{wh[1]}")
    check(BLOCKER, "Different pixel formats / bit depth",
          "Clips use different colour formats (for example 8-bit vs 10-bit or HDR vs SDR).",
          lambda c: c.v.pix_fmt)
    check(BLOCKER, "Interlaced and progressive clips mixed",
          "Clips use different scan types.",
          lambda c: "interlaced" if c.v.field_order not in ("", "progressive", "unknown") else "progressive")

    groups = frame_rate_groups(clips)
    if len(groups) > 1:
        issues.append(Issue(
            WARNING, FPS_ISSUE,
            "Frame timestamps are copied unchanged, so every clip keeps its own "
            "frame rate and nothing is lost. Most players handle this fine, but "
            "some editors expect one constant frame rate. You can also convert "
            "the slower clips when merging.\n\n" + _describe(groups, fmt_fps),
            _minority_ids(groups)))

    if not check(WARNING, "Different codec profiles",
                 "Clips use different encoder profiles. Playback usually works, "
                 "but some players may show glitches where clips meet.",
                 lambda c: c.v.profile):
        # Same codec/profile but different decoder configuration (SPS/PPS).
        check(WARNING, "Different encoder settings",
              "The clips' codec headers differ (different camera modes or devices). "
              "This is handled automatically: each clip's header is embedded in the "
              "video stream so every frame decodes exactly as before. For HEVC this "
              "adds a quick extra copy pass and needs temporary disk space.",
              lambda c: c.v.extradata_hash, lambda h: f"settings #{h[-6:]}" if h else "none")
    check(BLOCKER, "Different orientation",
          "Some clips are stored rotated differently (for example the front camera "
          "stores video turned on its side). A joined file can carry only one "
          "orientation flag, so these clips would play sideways.",
          lambda c: c.v.rotation, lambda r: f"stored rotated {r}°" if r else "stored upright")

    # ---- Audio ---------------------------------------------------------- #
    if layout_ok and clips[0].audio:
        for idx in range(len(clips[0].audio)):
            n = f" (track {idx + 1})" if len(clips[0].audio) > 1 else ""
            check(BLOCKER, "Different audio codecs" + n,
                  "Clips use different audio codecs.",
                  lambda c: c.audio[idx].codec, codec_label)
            check(BLOCKER, "Different audio sample rates" + n,
                  "Clips use different audio sample rates.",
                  lambda c: c.audio[idx].sample_rate, lambda r: f"{r / 1000:g} kHz")
            check(BLOCKER, "Different audio channel layouts" + n,
                  "Clips have a different number of audio channels.",
                  lambda c: (c.audio[idx].channels, c.audio[idx].channel_layout),
                  lambda t: t[1] or f"{t[0]} channels")

    issues.sort(key=lambda i: i.severity != BLOCKER)   # blockers first (stable)
    return CompatReport(issues)


# --------------------------------------------------------------------------- #
# Containers
# --------------------------------------------------------------------------- #
_MP4_VIDEO = {"h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video", "mjpeg"}
_MP4_AUDIO = {"aac", "mp3", "ac3", "eac3", "opus", "flac", "alac", "mp2"}
_MOV_VIDEO = _MP4_VIDEO | {"prores", "dnxhd", "rawvideo"}
_MOV_AUDIO = _MP4_AUDIO | {"pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be", "pcm_s32le", "pcm_f32le",
                           "apple_apac",   # iPhone spatial audio: MOV only
                           "amr_nb", "amr_wb", "pcm_mulaw", "pcm_alaw", "adpcm_ima_qt"}
_WEBM_VIDEO = {"vp8", "vp9", "av1"}
_WEBM_AUDIO = {"opus", "vorbis"}
# Codecs that only work in specific containers (everything else is fine in MKV/TS).
_MOV_ONLY_AUDIO = {"apple_apac"}

CONTAINER_RULES = {
    ".mp4": (_MP4_VIDEO, _MP4_AUDIO), ".m4v": (_MP4_VIDEO, _MP4_AUDIO),
    ".mov": (_MOV_VIDEO, _MOV_AUDIO),
    ".webm": (_WEBM_VIDEO, _WEBM_AUDIO),
}
AUDIO_NAMES = {"apple_apac": "Apple spatial audio (APAC)"}

OUTPUT_FORMATS = [  # (extension, label)
    (".mp4", "MP4 video"),
    (".mov", "QuickTime movie"),
    (".mkv", "Matroska video"),
    (".webm", "WebM video"),
    (".ts", "MPEG transport stream"),
    (".m2ts", "MPEG-2 TS (Blu-ray / AVCHD)"),
    (".avi", "AVI video"),
]


def container_problem(clip: Clip, ext: str) -> str | None:
    """Explain why *clip*'s codecs can't be stream-copied into *ext*, if so."""
    ext = ext.lower()
    rules = CONTAINER_RULES.get(ext)
    bad = []
    if rules:
        vset, aset = rules
        if clip.v and clip.v.codec not in vset:
            bad.append(f"{codec_label(clip.v.codec)} video")
        bad += [AUDIO_NAMES.get(a.codec, f"{codec_label(a.codec)} audio")
                for a in clip.audio if a.codec not in aset]
    elif ext != ".mov":
        bad += [AUDIO_NAMES.get(a.codec, a.codec) for a in clip.audio if a.codec in _MOV_ONLY_AUDIO]
    if bad:
        return f"{ext.upper().lstrip('.')} files cannot hold {' and '.join(sorted(set(bad)))} without re-encoding."
    return None


def compatible_container(clip: Clip) -> str:
    """A container that can hold this clip's codecs as they are."""
    # MP4-family sources (phones, Sony XAVC with PCM sound) stay in the QuickTime family
    order = (".mov", ".mkv", ".mp4") if clip.path.suffix.lower() in (".mp4", ".m4v", ".mov") else (".mkv", ".mov", ".mp4")
    return next((e for e in order if not container_problem(clip, e)), ".mkv")


def suggested_extension(clips: list[Clip]) -> str:
    """Keep the source container when all clips share one, otherwise pick a safe one."""
    exts = Counter(c.path.suffix.lower() for c in clips)
    ext = exts.most_common(1)[0][0]
    known = {e for e, _ in OUTPUT_FORMATS} | {".mts", ".m4v"}
    if len(exts) > 1 or ext not in known:
        ext = ".mp4"
    if container_problem(clips[0], ext):
        ext = compatible_container(clips[0])
    return ext


# --------------------------------------------------------------------------- #
# Fit plan: re-encode only the clips whose *video* doesn't match the others
# --------------------------------------------------------------------------- #
FIT_CODECS = {"hevc", "h264"}


def video_key(c: Clip) -> tuple:
    """Everything about the video stream that must be identical for stream copy."""
    v = c.v
    interlaced = v.field_order not in ("", "progressive", "unknown")
    return (v.codec, v.width, v.height, v.pix_fmt, v.rotation, interlaced)


def audio_key(c: Clip) -> tuple:
    return tuple((a.codec, a.sample_rate, a.channels) for a in c.audio)


@dataclass
class FitPlan:
    reference: Clip                 # format every converted clip is matched to
    convert: dict[str, list[str]]   # clip id -> reasons it must be re-encoded
    problem: str | None = None      # why fitting isn't possible (then: full re-encode)

    @property
    def feasible(self) -> bool:
        return self.problem is None


def fit_plan(clips: list[Clip], fps: float | None = None,
             weights: dict[str, float] | None = None) -> FitPlan:
    """Pick the dominant video format (by duration) and list the clips that differ.

    With *fps*, clips at another frame rate are converted too. *weights*
    (clip id -> seconds) overrides how much each clip counts, e.g. only the
    footage that could actually be stream-copied."""
    clips = [c for c in clips if not c.error]
    weight: Counter = Counter()
    for c in clips:
        w = weights.get(c.id, 0.0) if weights is not None else c.duration
        weight[video_key(c)] += max(w, 0.1)
    # most footage wins; on a tie prefer clips stored upright (no rotation flag)
    ref_key = max(weight, key=lambda k: (round(weight[k], 3), k[4] == 0))
    group = [c for c in clips if video_key(c) == ref_key]
    ref = next((c for c in group if fps and same_fps(c.v.nominal_fps, fps)), group[0])

    convert: dict[str, list[str]] = {}
    rv = ref.v
    for c in clips:
        v, why = c.v, []
        if v.codec != rv.codec:
            why.append(f"{codec_label(v.codec)} instead of {codec_label(rv.codec)}")
        if v.display_size != rv.display_size:
            why.append("different size {}×{}".format(*v.display_size))
        elif (v.width, v.height, v.rotation) != (rv.width, rv.height, rv.rotation):
            why.append("stored rotated" if v.rotation else "stored differently")
        if v.pix_fmt != rv.pix_fmt:
            why.append(f"{v.pix_fmt} colour format")
        if video_key(c)[5] != ref_key[5]:
            why.append("interlaced")
        if fps and not same_fps(v.nominal_fps, fps):
            why.append(f"{fmt_fps(v.fps)}")
        if why:
            convert[c.id] = why

    problem = None
    if rv.codec not in FIT_CODECS:
        problem = f"Only H.264 and HEVC clips can be matched ({codec_label(rv.codec)} found)."
    elif len({audio_key(c) for c in clips}) > 1:
        problem = "The clips' audio tracks differ, so the audio would have to be re-encoded too."
    elif any(len(c.video) != 1 for c in clips):
        problem = "Some clips contain more than one video track."
    return FitPlan(ref, convert, problem)


# --------------------------------------------------------------------------- #
# Re-encode target (only used after the user explicitly agrees)
# --------------------------------------------------------------------------- #
@dataclass
class EncodeTarget:
    width: int
    height: int
    fps: Fraction
    ten_bit: bool
    video_codec: str          # "h264" or "hevc"
    audio: bool
    sample_rate: int
    channels: int

    def describe(self) -> str:
        bits = "10-bit" if self.ten_bit else "8-bit"
        enc = "H.265/HEVC (x265, CRF 18)" if self.video_codec == "hevc" else "H.264 (x264, CRF 16)"
        audio = (f"AAC 320 kb/s, {self.sample_rate / 1000:g} kHz, {self.channels} ch"
                 if self.audio else "no audio")
        return (f"{self.width}×{self.height} · {fmt_fps(float(self.fps))} · {bits}\n"
                f"Video: {enc}, visually lossless quality\nAudio: {audio}")


def encode_target(clips: list[Clip]) -> EncodeTarget:
    """Resolution the majority of the footage (by duration) uses, at the highest
    frame rate present (slower clips get frames repeated, never dropped)."""
    clips = [c for c in clips if not c.error]
    weight = Counter()
    for c in clips:
        weight[c.v.display_size] += max(c.duration, 0.1)
    (w, h), _ = weight.most_common(1)[0]
    rate = fps_fraction(highest_fps(clips) or 30)
    ten_bit = any("10" in c.v.pix_fmt or "12" in c.v.pix_fmt for c in clips)
    codecs = Counter(c.v.codec for c in clips)
    vcodec = "hevc" if (ten_bit or codecs.most_common(1)[0][0] == "hevc") else "h264"
    with_audio = [c for c in clips if c.audio]
    sr = Counter(c.a.sample_rate for c in with_audio).most_common(1)[0][0] if with_audio else 48000
    ch = max((c.a.channels for c in with_audio), default=2)
    return EncodeTarget(w + w % 2, h + h % 2, rate, ten_bit, vcodec,
                        bool(with_audio), sr or 48000, min(ch, 8) or 2)
