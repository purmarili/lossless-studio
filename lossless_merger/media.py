"""Scanning folders, probing video files and working out recording times."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from . import ffmpeg_tools as ff

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mts", ".m2ts", ".ts",
    ".mpg", ".mpeg", ".3gp", ".3g2", ".wmv", ".flv", ".mxf", ".insv", ".lrv",
    ".vob", ".mod", ".tod", ".dv", ".ogv", ".asf", ".divx", ".f4v", ".m2t", ".360",
}
GOPRO_360_MESSAGE = ("GoPro MAX 360° file. Export it with the free GoPro Player app first "
                     "(choose a normal or 360° MP4), then add the exported file.")
# Low-resolution proxy files written by some cameras next to the real footage.
_SKIP_EXTENSIONS = {".lrv", ".thm"}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class VideoStream:
    codec: str
    profile: str
    level: int | None
    width: int
    height: int
    pix_fmt: str
    frame_rate: Fraction          # r_frame_rate (container/base rate)
    avg_frame_rate: Fraction
    field_order: str
    rotation: int                 # degrees, normalised to 0/90/180/270
    codec_tag: str
    extradata_hash: str
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    color_range: str = ""
    has_b_frames: int = 0

    @property
    def nominal_fps(self) -> float:
        """Frame rate snapped to the usual camera rates (VFR phones report e.g. 29.87)."""
        return snap_fps(self.fps)

    @property
    def display_size(self) -> tuple[int, int]:
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height

    @property
    def fps(self) -> float:
        fr = self.avg_frame_rate or self.frame_rate
        return float(fr) if fr else 0.0


@dataclass
class AudioStream:
    codec: str
    profile: str
    sample_rate: int
    channels: int
    channel_layout: str
    extradata_hash: str


@dataclass
class Clip:
    path: Path
    size: int
    duration: float = 0.0
    format_name: str = ""
    video: list[VideoStream] = field(default_factory=list)
    audio: list[AudioStream] = field(default_factory=list)
    recorded: datetime | None = None
    date_source: str = ""
    creation_time_tag: str | None = None
    error: str | None = None
    render_path: Path | None = None   # photos: upright PNG/JPEG FFmpeg reads instead of path
    # 360° footage: "" (normal) | "equirect" (stitched 360) | "dfisheye" (raw two-lens:
    # both lenses side by side in one picture) | "dfisheye2" (one stream per lens)
    projection: str = ""
    lens_pair: Path | None = None     # Insta360 ONE X style: second lens in a separate file

    @property
    def is_360(self) -> bool:
        return bool(self.projection)

    @property
    def id(self) -> str:
        return str(self.path)

    @property
    def is_image(self) -> bool:
        return bool(self.video) and self.video[0].codec == "image"

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def v(self) -> VideoStream | None:
        return self.video[0] if self.video else None

    @property
    def a(self) -> AudioStream | None:
        return self.audio[0] if self.audio else None


# --------------------------------------------------------------------------- #
# Folder scanning
# --------------------------------------------------------------------------- #
def scan_folder(folder: Path, recursive: bool = False) -> list[Path]:
    """Return all supported video files in *folder* (no file contents are read)."""
    folder = Path(folder)
    pattern = folder.rglob("*") if recursive else folder.iterdir()
    files = []
    for p in pattern:
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        ext = p.suffix.lower()
        if ext in VIDEO_EXTENSIONS and ext not in _SKIP_EXTENSIONS and not p.name.startswith("."):
            files.append(p)
    # Insta360 ONE X style: lens 2 lives in "..._10_..." next to "..._00_...":
    # it is used together with lens 1, not listed as a clip of its own
    names = {f.name for f in files}
    files = [f for f in files if not (f.suffix.lower() == ".insv" and "_10_" in f.name
                                      and f.name.replace("_10_", "_00_") in names)]
    return sorted(files, key=lambda p: str(p).lower())


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def _fraction(value: str | None) -> Fraction:
    if not value:
        return Fraction(0)
    try:
        if "/" in value:
            n, d = value.split("/", 1)
            return Fraction(int(n), int(d)) if int(d) else Fraction(0)
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def _rotation(stream: dict) -> int:
    rot = 0
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                rot = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    if not rot:
        try:
            rot = int(stream.get("tags", {}).get("rotate", 0))
        except (TypeError, ValueError):
            rot = 0
    return rot % 360


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def probe(path: Path, timeout: float = 60) -> Clip:
    """Read container and stream information with ffprobe (headers only)."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        return Clip(path=path, size=0, error=f"Cannot access file: {exc}")
    clip = Clip(path=path, size=size)
    try:
        proc = ff.run(
            [ff.ffprobe_path(), "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", "-show_data_hash", "SHA256",
             "-i", str(path)],
            timeout=timeout,
        )
    except Exception as exc:  # timeout, missing binary, ...
        clip.error = f"Could not analyse file: {exc}"
        return clip
    if proc.returncode != 0:
        msg = ff.decode(proc.stderr).strip().splitlines()
        clip.error = msg[-1] if msg else "ffprobe failed to read this file"
        return clip
    try:
        info = json.loads(ff.decode(proc.stdout))
    except json.JSONDecodeError:
        clip.error = "Unreadable ffprobe output"
        return clip

    fmt = info.get("format", {})
    clip.format_name = fmt.get("format_name", "")
    clip.duration = _float(fmt.get("duration"))
    durations = []
    for s in info.get("streams", []):
        ctype = s.get("codec_type")
        disp = s.get("disposition", {}) or {}
        if ctype == "video" and not disp.get("attached_pic"):
            clip.video.append(VideoStream(
                codec=s.get("codec_name", "unknown"),
                profile=s.get("profile", "") or "",
                level=s.get("level"),
                width=int(s.get("width", 0) or 0),
                height=int(s.get("height", 0) or 0),
                pix_fmt=s.get("pix_fmt", "") or "",
                frame_rate=_fraction(s.get("r_frame_rate")),
                avg_frame_rate=_fraction(s.get("avg_frame_rate")),
                field_order=s.get("field_order", "") or "",
                rotation=_rotation(s),
                codec_tag=s.get("codec_tag_string", "") or "",
                extradata_hash=s.get("extradata_hash", "") or "",
                color_primaries=s.get("color_primaries", "") or "",
                color_transfer=s.get("color_transfer", "") or "",
                color_space=s.get("color_space", "") or "",
                color_range=s.get("color_range", "") or "",
                has_b_frames=int(s.get("has_b_frames", 0) or 0),
            ))
            durations.append(_float(s.get("duration")))
        elif ctype == "audio":
            clip.audio.append(AudioStream(
                codec=s.get("codec_name", "unknown"),
                profile=s.get("profile", "") or "",
                sample_rate=int(_float(s.get("sample_rate"))),
                channels=int(s.get("channels", 0) or 0),
                channel_layout=s.get("channel_layout", "") or "",
                extradata_hash=s.get("extradata_hash", "") or "",
            ))
    if not clip.duration and durations:
        clip.duration = max(durations)
    if not clip.video:
        clip.error = "No video stream found"
    _detect_360(clip, info)

    clip.recorded, clip.date_source, clip.creation_time_tag = recording_time(path, info)
    return clip


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}


def image_clip(path: Path, width: int, height: int, render_path: Path | None = None) -> Clip:
    """A photo, described like a clip (codec "image", no audio, no duration)."""
    path = Path(path)
    try:
        size = path.stat().st_size
        mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        size, mtime = 0, None
    v = VideoStream(codec="image", profile="", level=None, width=width, height=height, pix_fmt="rgb24",
                    frame_rate=Fraction(0), avg_frame_rate=Fraction(0), field_order="progressive",
                    rotation=0, codec_tag="", extradata_hash="")
    return Clip(path=path, size=size, duration=0.0, format_name="image", video=[v], recorded=mtime,
                date_source="file date", render_path=render_path or path)


def probe_audio(path: Path) -> tuple[float, str | None]:
    """(duration, error) of an audio (or any media) file."""
    c = probe(path)
    if c.audio:
        return c.duration, None
    return 0.0, c.error or "No audio found in this file"


def _detect_360(clip: Clip, info: dict) -> None:
    """Recognise 360° footage (stitched equirectangular, or raw two-lens)."""
    ext = clip.path.suffix.lower()
    if ext == ".360":
        clip.error = GOPRO_360_MESSAGE
        return
    if clip.error or not clip.video:
        return
    for s in info.get("streams", []):
        if s.get("codec_type") != "video":
            continue
        for sd in s.get("side_data_list", []) or []:
            if "spherical" in str(sd.get("side_data_type", "")).lower():
                if str(sd.get("projection", "")).lower().startswith("equirect"):
                    clip.projection = "equirect"
                return
    if ext != ".insv":
        return
    v = clip.v
    if len(clip.video) >= 2:
        clip.projection = "dfisheye2"                 # X3 / X4: one stream per lens
    elif v.width >= 1.8 * v.height:
        clip.projection = "dfisheye"                  # both circles side by side
    else:
        pair = clip.path.with_name(clip.path.name.replace("_00_", "_10_"))
        if "_00_" in clip.path.name and pair.exists():
            clip.projection = "dfisheye2"             # ONE X: second lens in its own file
            clip.lens_pair = pair
        # else: a single lens only — used as a normal (fisheye-looking) video


# --------------------------------------------------------------------------- #
# Recording date / time
# --------------------------------------------------------------------------- #
_DATE_TAGS = (
    "com.apple.quicktime.creationdate",   # local time incl. offset (iPhone)
    "creation_time",
    "date",
    "date_recorded",
    "DATE_RECORDED",
)

# 2024-05-01 12-34-56 (OBS), VID_20240501_123456, PXL_20240501_123456789,
# 20240501-123456, 2024_0501_123456 (DJI), etc.
_FILENAME_PATTERNS = [
    re.compile(r"(?<!\d)(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[ _T-]+(\d{2})[-_.:h]?(\d{2})[-_.:m]?(\d{2})(?!\d{3,})"),
    re.compile(r"(?<!\d)(20\d{2})_(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"),
]


def _parse_date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    # "+0300" -> "+03:00"
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v)
    dt = None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        for f in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value[:19], f)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        # QuickTime/MP4 creation_time is defined as UTC; naive values are rare.
        dt = dt.replace(tzinfo=timezone.utc)
    # 1904/1970 epoch zeros and other placeholder dates are meaningless.
    if dt.year < 1995 or dt > datetime.now(timezone.utc) + timedelta(days=2):
        return None
    return dt


def _date_from_filename(name: str) -> datetime | None:
    for pat in _FILENAME_PATTERNS:
        m = pat.search(name)
        if not m:
            continue
        try:
            y, mo, d, h, mi, s = (int(x) for x in m.groups())
            # Filenames are written in the camera/PC's local time.
            return datetime(y, mo, d, h, mi, s).astimezone()
        except ValueError:
            continue
    return None


def recording_time(path: Path, info: dict) -> tuple[datetime, str, str | None]:
    """Best guess of when the clip was recorded.

    1. Container / stream metadata (creation_time, QuickTime creationdate)
    2. A timestamp embedded in the file name (OBS, Android, Pixel, DJI ...)
    3. The earlier of the file's creation and modification time
    """
    raw_creation = None
    tag_sources = [info.get("format", {}).get("tags", {}) or {}]
    tag_sources += [s.get("tags", {}) or {} for s in info.get("streams", [])]
    for key in _DATE_TAGS:
        for tags in tag_sources:
            for k, val in tags.items():
                if k.lower() == key.lower() and isinstance(val, str):
                    dt = _parse_date(val)
                    if dt:
                        if k.lower() == "creation_time":
                            raw_creation = val
                        return dt, "metadata", raw_creation

    dt = _date_from_filename(path.name)
    if dt:
        return dt, "file name", None

    st = os.stat(path)
    times = [st.st_mtime]
    birth = getattr(st, "st_birthtime", None) or (st.st_ctime if os.name == "nt" else None)
    if birth:
        times.append(birth)
    return datetime.fromtimestamp(min(times)).astimezone(), "file date", None


def sort_chronologically(clips: list[Clip]) -> list[Clip]:
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(clips, key=lambda c: (c.recorded or far_future, c.name.lower()))


# --------------------------------------------------------------------------- #
# Thumbnails
# --------------------------------------------------------------------------- #
def thumbnail_png(path: Path, duration: float, width: int = 320, timeout: float = 30) -> bytes | None:
    """Grab a single frame as PNG bytes (fast input seeking, nothing buffered)."""
    t = min(1.0, duration * 0.1) if duration else 0
    try:
        proc = ff.run(
            [ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-nostdin",
             "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-an", "-sn",
             "-vf", f"scale={width}:-2:flags=bilinear", "-f", "image2pipe",
             "-c:v", "png", "pipe:1"],
            timeout=timeout,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def filmstrip_png(path: Path, duration: float, count: int = 16, height: int = 54,
                  timeout: float = 120) -> bytes | None:
    """*count* evenly spaced frames side by side (only keyframes are decoded)."""
    if duration <= 0:
        return None
    rate = count / duration
    # long clips: decode keyframes only (fast); short clips have too few keyframes
    skip = ["-skip_frame", "nokey"] if duration > 12 else []
    try:
        proc = ff.run(
            [ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-nostdin", *skip,
             "-i", str(path), "-an", "-sn", "-dn",
             "-vf", f"fps={rate:.6f}:round=down,scale=-2:{height}:flags=bilinear,tile={count}x1",
             "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"],
            timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def waveform_png(path: Path, width: int = 1600, height: int = 56, timeout: float = 120) -> bytes | None:
    """Picture of an audio file's waveform (for the timeline)."""
    try:
        proc = ff.run(
            [ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-nostdin", "-i", str(path), "-vn",
             "-filter_complex", f"aformat=channel_layouts=mono,showwavespic=s={width}x{height}:colors=white",
             "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"], timeout=timeout)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
CODEC_NAMES = {
    "h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp9": "VP9", "vp8": "VP8",
    "prores": "ProRes", "mpeg2video": "MPEG-2", "mpeg4": "MPEG-4", "mjpeg": "MJPEG",
    "aac": "AAC", "mp3": "MP3", "ac3": "AC-3", "eac3": "E-AC-3", "opus": "Opus",
    "flac": "FLAC", "alac": "ALAC", "vorbis": "Vorbis",
}


def codec_label(codec: str) -> str:
    if codec.startswith("pcm_"):
        return "PCM"
    return CODEC_NAMES.get(codec, codec.upper())


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.2f} TB"


STANDARD_RATES = (23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60, 100, 119.88, 120, 240)


def snap_fps(fps: float) -> float:
    """Nearest standard rate if within 1.5 %, else the measured value."""
    if not fps:
        return 0.0
    best = min(STANDARD_RATES, key=lambda r: abs(r - fps))
    return float(best) if abs(best - fps) / best < 0.015 else fps


def same_fps(a: float, b: float) -> bool:
    return bool(a and b) and abs(a - b) / max(a, b) < 0.006


def fmt_fps(fps: float) -> str:
    if not fps:
        return "? fps"
    return f"{fps:.2f}".rstrip("0").rstrip(".") + " fps"


def fmt_resolution(v: VideoStream) -> str:
    w, h = v.display_size
    tag = {2160: " 4K", 4320: " 8K", 1440: " QHD", 1080: " FHD", 720: " HD"}.get(min(w, h), "")
    return f"{w}×{h}{tag}"
