"""Rendering an edited project: main track (trims, splits, speed, reverse,
look, effects, zoom, photos, freeze frames, transitions) plus text,
picture-in-picture and audio tracks, canvas and export formats.

The guiding rule is the same as for plain merging: only frames that really
change are re-encoded.

* Untouched clips are stream-copied as whole files.
* A trimmed clip is "smart cut": everything between the first and last
  keyframe inside the trim is stream-copied packet for packet; only the few
  frames from a cut to the next keyframe are re-encoded, matched to the
  original format.
* Where a caption or picture-in-picture covers part of an untouched clip, only
  that stretch is re-encoded (smart cut around it); the rest stays copied.
* Clips with visual edits, photos, freeze frames and transitions are rendered
  as separate small parts, so memory use stays flat. Reversed clips are
  rendered in short chunks for the same reason.
* Audio is copied (including Apple spatial audio) unless something changes the
  sound; then the whole soundtrack is mixed once and encoded to AAC.
"""

from __future__ import annotations

import json
import tempfile
import threading
from bisect import bisect_left
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from . import compat
from . import ffmpeg_tools as ff
from .media import Clip, probe
from .merger import (COMMON_TIMESCALE, COPY_WEIGHT, ENCODE_WEIGHT, MUXERS, Cancelled, MergeError,  # noqa: F401
                     MergeResult, ProgressFn, _concat_list, _embed_args, _finish, _inverse_rotation, _known,
                     _parts_dir, _remove_quietly, _Stages, _temp_output, audio_container,
                     needs_header_embedding, rewrap_audio_args, rewrap_video_args, stream_timebase)
from .project import CANVASES, PRESETS, Project, Segment

HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}
UNDECODABLE_AUDIO = {"apple_apac"}   # FFmpeg can copy Apple spatial audio but not decode it
FADE = 1.0                       # seconds for project fade in / out
REVERSE_BUDGET = 350e6           # bytes of decoded frames a reversed chunk may hold
_LAYOUTS = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}


# --------------------------------------------------------------------------- #
# Output format
# --------------------------------------------------------------------------- #
@dataclass
class Target:
    codec: str                   # "hevc" | "h264"
    width: int                   # display (upright) size of the output
    height: int
    pix_fmt: str
    rotation: int                # rotation flag of the output (matches copied clips)
    fps: Fraction | None         # fixed output rate, None = every clip keeps its own
    primaries: str
    transfer: str
    matrix: str
    color_range: str
    sdr: bool                    # tone-map HDR footage to normal colours
    crf: int
    speed_preset: str
    allow_copy: bool             # stream copy allowed for matching clips
    reference: Clip
    convert_ids: set[str]        # clips whose video never matches (always re-encoded)
    audio_copy: bool
    audio_rate: int
    audio_channels: int
    audio_bitrate: str
    ext: str
    cover: bool = False          # custom canvas: crop clips to fill it
    background: str = "black"    # what fills bars: black | blur
    interlaced: str = ""         # field order kept for copied interlaced footage ("tt"/"bb"), else ""
    notes: list[str] = field(default_factory=list)

    @property
    def ten_bit(self) -> bool:
        return "10" in self.pix_fmt or "12" in self.pix_fmt

    @property
    def hdr(self) -> bool:
        return self.transfer in HDR_TRANSFERS

    @property
    def part_ext(self) -> str:
        # HEVC parts in Matroska (keeps in-band headers), H.264 in MOV (keeps DTS)
        return ".mkv" if self.codec == "hevc" else ".mov"

    @property
    def alpha_fmt(self) -> str:
        return "yuva420p10le" if self.ten_bit else "yuva420p"

    @property
    def overlay_fmt(self) -> str:
        return "yuv420p10" if self.ten_bit else "yuv420"


def _even(x: float) -> int:
    n = int(round(x))
    return max(2, n - n % 2)


def canvas_size(ref_w: int, ref_h: int, canvas: str) -> tuple[int, int]:
    """Largest frame of the canvas shape that fits in the footage (no upscaling)."""
    aspect = CANVASES.get(canvas, CANVASES["original"])[1]
    if not aspect or abs(aspect - ref_w / ref_h) < 0.01:
        return ref_w, ref_h
    if ref_w / ref_h > aspect:              # footage wider -> keep height
        return _even(ref_h * aspect), _even(ref_h)
    return _even(ref_w), _even(ref_w / aspect)


def _videos(project: Project) -> list[Clip]:
    """Unique video clips of the main track (photos excluded)."""
    out = {}
    for s in project.segments:
        if not s.clip.error and not s.clip.is_image:
            out.setdefault(s.clip.id, s.clip)
    return list(out.values())


def make_target(project: Project) -> Target:
    unique = _videos(project)
    if not unique:                       # photos only
        unique = [s.clip for s in project.segments if not s.clip.error][:1]
    preset = PRESETS.get(project.preset, PRESETS["original"])
    # The reference format is the one most of the *copyable* footage uses:
    # clips that get re-encoded anyway (visual edits) shouldn't decide it.
    weights: dict[str, float] = {}
    for s in project.segments:
        if s.kind == "video" and not s.clip.error and not s.edit.has_visual:
            weights[s.clip.id] = weights.get(s.clip.id, 0.0) + s.length
    fit = compat.fit_plan(unique, weights=weights if weights else None)
    ref, rv = fit.reference, fit.reference.v
    w, h = canvas_size(*rv.display_size, project.canvas)
    if preset.max_height and min(w, h) > preset.max_height:
        k = preset.max_height / min(w, h)
        w, h = _even(w * k), _even(h * k)
    w, h = _even(w), _even(h)

    ten_bit_src = any("10" in c.v.pix_fmt or "12" in c.v.pix_fmt for c in unique)
    copy_codec = rv.codec in compat.FIT_CODECS and all(len(c.video) == 1 for c in unique)
    if preset.codec:
        codec = preset.codec
    else:
        codec = rv.codec if rv.codec in compat.FIT_CODECS else ("hevc" if ten_bit_src else "h264")
    sdr = preset.sdr
    if preset.key == "original" and copy_codec:
        pix = rv.pix_fmt or "yuv420p"
    elif codec == "hevc" and ten_bit_src and not sdr:
        pix = "yuv420p10le"
    else:
        pix = "yuv420p"
    allow_copy = (preset.key == "original" and copy_codec and (w, h) == tuple(rv.display_size))

    if sdr or rv.codec == "image":
        prim, trc, mat, rng = "bt709", "bt709", "bt709", "tv"
    else:
        prim, trc = _known(rv.color_primaries), _known(rv.color_transfer)
        mat, rng = _known(rv.color_space), _known(rv.color_range)

    fps = None
    if preset.max_fps:
        vids = [c for c in unique if not c.is_image]
        top = (compat.highest_fps(vids) if vids else 0) or 30
        fps = compat.fps_fraction(min(top, preset.max_fps))

    segs = project.segments
    sound_edits = (any(s.edit.has_audio_edit or s.is_still for s in segs) or project.audios
                   or any(o.has_audio for o in project.overlays)
                   or any(project.transition_after(i) > 0 for i in range(len(segs)))
                   or project.fade_in or project.fade_out)
    audio_keys = {compat.audio_key(c) for c in unique}
    # MPEG program streams (DVD .VOB / .MPG) don't cut cleanly at audio frame
    # boundaries when stream-copied: their sound is re-encoded instead
    ps_source = any(c.format_name in ("mpeg", "vob", "mpegvideo") for c in unique)
    audio_copy = (preset.key == "original" and not sound_edits and len(audio_keys) == 1 and not ps_source)
    with_audio = [c for c in unique if c.audio]
    rate, channels = 48000, 2
    if with_audio and preset.key == "original":
        rate = with_audio[0].audio[0].sample_rate or 48000
        channels = min(max(c.audio[0].channels for c in with_audio), 8) or 2
    notes = []
    if not audio_copy and any(len(c.audio) > 1 for c in with_audio):
        names = {compat.AUDIO_NAMES.get(a.codec, "") for c in with_audio for a in c.audio[1:]}
        extra = " (" + ", ".join(sorted(n for n in names if n)) + ")" if any(names) else ""
        notes.append("The sound was edited, so the soundtrack was mixed into one AAC track; "
                     f"extra audio tracks{extra} were left out.")
    if any(s.edit.stabilize for s in segs) and ten_bit_src:
        notes.append("Stabilized clips are processed in 8-bit (FFmpeg's stabilizer can't do 10-bit).")

    audio_codecs = {a.codec for c in unique for a in c.audio} if audio_copy else {"aac"}
    ext = pick_container(codec, audio_codecs, unique[0].path.suffix.lower() if preset.key == "original" else ".mp4")
    interlaced = ""
    if allow_copy and codec == "h264" and rv.field_order in ("tt", "bb", "tb", "bt"):
        interlaced = rv.field_order

    return Target(
        codec=codec, width=w, height=h, pix_fmt=pix,
        rotation=rv.rotation if allow_copy else 0, fps=fps,
        primaries=prim, transfer=trc, matrix=mat, color_range=rng, sdr=sdr,
        crf=preset.crf, speed_preset="fast" if (codec == "hevc" and preset.key == "original") else "medium",
        allow_copy=allow_copy, reference=ref,
        convert_ids=set(fit.convert) if allow_copy else {c.id for c in unique},
        audio_copy=audio_copy, audio_rate=rate if not preset.sdr else 48000,
        audio_channels=channels, audio_bitrate=preset.audio_bitrate, ext=ext,
        cover=project.canvas != "original", background=project.background, interlaced=interlaced, notes=notes)


def pick_container(video_codec: str, audio_codecs: set[str], prefer: str) -> str:
    """Output container that can hold the video codec and every copied audio
    codec: the source's own (MP4/MOV/MKV) if possible, else MP4, MOV, MKV."""
    order = ([prefer] if prefer in (".mp4", ".mov", ".mkv") else []) + [".mp4", ".mov", ".mkv"]
    for ext in order:
        rules = compat.CONTAINER_RULES.get(ext)
        if rules is None:                       # Matroska: anything but Apple spatial audio
            if not audio_codecs & {"apple_apac"}:
                return ext
            continue
        vset, aset = rules
        if video_codec in vset and audio_codecs <= aset:
            return ext
    return ".mkv"


# --------------------------------------------------------------------------- #
# Packet index (for keyframe-exact smart cuts)
# --------------------------------------------------------------------------- #
@dataclass
class PacketIndex:
    pts: list[int]               # decode order
    keys: list[bool]
    sizes: list[int]
    tb: Fraction
    start: float                 # container start time (FFmpeg -ss is relative to it)
    frames: list[float]          # sorted presentation times, seconds relative to start
    end: float                   # end of the last frame (relative)

    def rel(self, pts: int) -> float:
        return float(pts * self.tb) - self.start


def packet_index(path: Path) -> PacketIndex:
    proc = ff.run([ff.ffprobe_path(), "-v", "error", "-select_streams", "V:0",
                   "-show_entries", "packet=pts,duration,size,flags:stream=time_base:format=start_time",
                   "-of", "json", str(path)], timeout=600)
    if proc.returncode != 0:
        raise MergeError(ff.decode(proc.stderr).strip() or f"Could not read {path.name}")
    info = json.loads(ff.decode(proc.stdout))
    tb = Fraction(info["streams"][0]["time_base"])
    start = float(info.get("format", {}).get("start_time", 0) or 0)
    pts, keys, durs, sizes = [], [], [], []
    for p in info.get("packets", []):
        if "pts" not in p:
            continue
        pts.append(int(p["pts"]))
        keys.append("K" in p.get("flags", ""))
        durs.append(int(p.get("duration", 0) or 0))
        sizes.append(int(p.get("size", 0) or 0))
    if not pts:
        raise MergeError(f"No video frames found in {path.name}")
    idx = PacketIndex(pts, keys, sizes, tb, start, [], 0.0)
    idx.frames = sorted(idx.rel(x) for x in pts)
    last = max(range(len(pts)), key=lambda i: pts[i])
    step = durs[last] or (pts[last] - sorted(pts)[-2] if len(pts) > 1 else 0)
    idx.end = idx.rel(pts[last] + step)
    return idx


@dataclass
class Cut:
    """How to produce source range [a, b): encode head, copy middle, encode tail."""
    a: float
    b: float
    head: tuple[float, float, int] | None = None     # (start, end, frames)
    middle: tuple[float, float, int, list[int]] | None = None  # (start, end, packets, drop)
    tail: tuple[float, float, int] | None = None
    first_packet: int = 0


def _snap(frames: list[float], t: float, end: float) -> float:
    """First frame time >= t (with a quarter-frame tolerance); *end* if none."""
    tol = 0.25 * ((frames[1] - frames[0]) if len(frames) > 1 else 0.02)
    i = bisect_left(frames, t - tol)
    return frames[i] if i < len(frames) else end


def _count(frames: list[float], a: float, b: float) -> int:
    eps = 1e-6
    return bisect_left(frames, b - eps) - bisect_left(frames, a - eps)


def plan_cut(idx: PacketIndex, a: float, b: float, codec: str,
             min_head: float = 0.0, min_tail: float = 0.0) -> Cut:
    """Choose keyframes so the most frames of [a, b) can be stream-copied.

    A cut point is usable at keyframe K when no earlier packet (decode order) is
    shown after K. HEVC "open GOP" keyframes (CRA) may be followed by a few
    leading frames that depend on the previous group: those packets are dropped
    from the copy and the head encode covers them instead. H.264 needs fully
    clean keyframes. The end is cut just before a keyframe in decode order; the
    frames that would be missing there are covered by the tail encode."""
    a = _snap(idx.frames, a, idx.end)
    b = _snap(idx.frames, b, idx.end)
    cut = Cut(a, b)
    n = len(idx.pts)
    pts = idx.pts
    prefix_max = [0] * n
    m = None
    for i in range(n):
        prefix_max[i] = m
        m = pts[i] if m is None else max(m, pts[i])
    suffix_min = [0] * (n + 1)
    big = max(pts) + 1
    suffix_min[n] = big
    for i in range(n - 1, -1, -1):
        suffix_min[i] = min(suffix_min[i + 1], pts[i])

    def start_ok(i):
        if not idx.keys[i] or (prefix_max[i] is not None and prefix_max[i] >= pts[i]):
            return False
        return codec == "hevc" or suffix_min[i] == pts[i]

    starts = [i for i in range(n) if start_ok(i) and idx.rel(pts[i]) >= a + min_head - 1e-6]
    i1 = min(starts, key=lambda i: pts[i]) if starts else None
    if i1 is not None:
        k1 = idx.rel(pts[i1])
        limit = b - min_tail
        best = None
        mid_max = None
        drop: list[int] = []
        for j in range(i1, n + 1):
            if j > i1 and (j == n or idx.keys[j]):
                boundary = idx.end if j == n else idx.rel(suffix_min[j])
                if boundary > limit + 1e-6:
                    break                    # suffix minima only grow from here
                if mid_max is None or idx.rel(mid_max) < boundary - 1e-6:
                    best = (j, boundary, list(drop))
            if j == n:
                break
            if pts[j] < pts[i1]:
                drop.append(j - i1)          # leading frame of an open-GOP keyframe
            else:
                mid_max = pts[j] if mid_max is None else max(mid_max, pts[j])
        if best is not None:
            i2, k2, drop = best
            n_packets = i2 - i1
            if n_packets - len(drop) > 0 and k2 > k1:
                cut.middle = (k1, k2, n_packets, drop)
                cut.first_packet = i1
                if k1 > a + 1e-6:
                    cut.head = (a, k1, _count(idx.frames, a, k1))
                if b > k2 + 1e-6:
                    cut.tail = (k2, b, _count(idx.frames, k2, b))
                return cut
    cut.head = (a, b, _count(idx.frames, a, b))      # too short: encode it all
    return cut


# --------------------------------------------------------------------------- #
# Filter graph building blocks
# --------------------------------------------------------------------------- #
def _rate(fr: Fraction) -> str:
    return f"{fr.numerator}/{fr.denominator}"


def _ease(u: str) -> str:
    return f"(({u})*({u})*(3-2*({u})))"


@dataclass
class TextRender:
    """A caption drawn by the UI at export resolution (tight around the text)."""
    png: bytes
    x: int                       # top-left on the canvas
    y: int
    w: int
    h: int
    steps: list[bytes] = field(default_factory=list)   # typewriter frames (same size)
    step_dur: float = 0.05


@dataclass
class ChainOpts:
    t_offset: float = 0.0        # where this piece starts within the segment (output s)
    length: float = 0.0          # output length of this piece
    fade_in: float = 0.0         # picture fade from black over segment time [0, fade_in)
    fade_out: float = 0.0        # fade to black over the last fade_out s of the segment
    seg_len: float = 0.0
    fps: Fraction | None = None  # convert to this rate (cfr)
    finalize: bool = True        # inverse rotation + pixel format
    preview: bool = False


class _Graph:
    """Collects inputs and filter chains of one FFmpeg run."""

    def __init__(self):
        self.inputs: list[list[str]] = []
        self.chains: list[str] = []
        self._n = 0

    def input(self, args: list[str]) -> int:
        self.inputs.append(args)
        return len(self.inputs) - 1

    def label(self, p: str) -> str:
        self._n += 1
        return f"{p}{self._n}"

    def add(self, src: str, filters: list[str], out: str | None = None) -> str:
        out = out or self.label("x")
        self.chains.append(f"{src}{','.join(filters) if filters else 'null'}[{out}]")
        return out

    def args(self) -> list[str]:
        a = []
        for i in self.inputs:
            a += i
        return a + ["-filter_complex", ";".join(self.chains)]


@dataclass
class _Ctx:
    """Things a render needs besides the project: files made on the way."""
    project: Project
    target: Target
    work: Path                                  # folder for helper files (cwd of FFmpeg)
    texts: dict[str, TextRender] = field(default_factory=dict)
    text_files: dict[str, Path] = field(default_factory=dict)
    trf: dict[tuple, str] = field(default_factory=dict)        # stabilizer data, relative names
    vignettes: dict[float, Path] = field(default_factory=dict)

    def rate(self, clip: Clip | None = None) -> str:
        if self.target.fps:
            return _rate(self.target.fps)
        fps = (clip.v.nominal_fps if clip and clip.v and not clip.is_image else 0) or 30
        return _rate(compat.fps_fraction(fps))

    def vignette(self, strength: float) -> Path:
        key = round(strength, 2)
        if key not in self.vignettes:
            W, H = self.target.width, self.target.height
            p = self.work / f"vignette_{int(key * 100)}.png"
            a = (f"255*min(1,{key * 1.6:.3f}*pow(max(0,hypot((X-W/2)/(W/2),(Y-H/2)/(H/2))/1.4142-0.3)/0.7,1.6))")
            proc = ff.run([ff.ffmpeg_path(), "-v", "error", "-y", "-f", "lavfi", "-i",
                           f"color=black:s={W}x{H},format=rgba", "-vf",
                           f"geq=r=0:g=0:b=0:a='{a}'", "-frames:v", "1", str(p)], timeout=120)
            if proc.returncode != 0:
                raise MergeError("Could not prepare the vignette: " + ff.decode(proc.stderr)[-300:])
            self.vignettes[key] = p
        return self.vignettes[key]


INTERLACED = ("tt", "bb", "tb", "bt")


def _deinterlace(clip: Clip, target: Target) -> list[str]:
    """Camcorder 1080i / DVD footage: remove the comb lines when the result is
    progressive (bwdif keeps one frame per frame, so the timing is unchanged).
    Re-encoded bits of footage that stays interlaced are left as they are."""
    if clip.v and clip.v.field_order in INTERLACED and not target.interlaced:
        return ["bwdif=mode=send_frame:parity=auto:deint=all"]
    return []


def _user_rotation(seg: Segment) -> list[str]:
    f = {90: ["transpose=clock"], 180: ["hflip", "vflip"], 270: ["transpose=cclock"]}.get(seg.edit.rotate % 360, [])
    return f + (["hflip"] if seg.edit.flip else [])


def _display_after_rotation(seg: Segment) -> tuple[int, int]:
    w, h = seg.clip.v.display_size
    return (h, w) if seg.edit.rotate % 180 == 90 else (w, h)


def _color_filter(seg: Segment) -> str:
    """Brightness / contrast / saturation / warmth in YUV (any bit depth, no
    RGB round trip, so HDR stays HDR)."""
    if not seg.edit.has_color:
        return ""
    b, c, s, w = seg.edit.color_params()
    ymid = "((minval+maxval)/2)"
    y = f"clip(({ymid}+(val-{ymid})*{c:.4f})+{b:.4f}*(maxval-minval),minval,maxval)"
    cm = "((minval+maxval)/2)"
    u = f"clip({cm}+(val-{cm})*{s:.4f}-{w * 0.035:.4f}*(maxval-minval),minval,maxval)"
    v = f"clip({cm}+(val-{cm})*{s:.4f}+{w * 0.03:.4f}*(maxval-minval),minval,maxval)"
    return f"lutyuv=y='{y}':u='{u}':v='{v}'"


def _tonemap(clip: Clip) -> list[str]:
    v = clip.v
    prim = _known(v.color_primaries) or "bt2020"
    mat = _known(v.color_space) or "bt2020nc"
    if mat == "bt2020c":
        mat = "bt2020nc"
    return [f"zscale=tin={v.color_transfer}:pin={prim}:min={mat}:rin=tv:t=linear:npl=100",
            "format=gbrpf32le", "zscale=p=bt709", "tonemap=tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv", "format=yuv420p"]


def _rgb_to_target(target: Target, alpha: bool = False) -> list[str]:
    """RGB(A) pictures (photos, captions) into the output's colours. In HLG
    output, SDR white sits at 75 % signal (ITU-R BT.2408), so photos and text
    aren't glaring on HDR screens."""
    out = target.alpha_fmt if alpha else target.pix_fmt
    if target.transfer == "smpte2084":
        # HDR10 / PQ (Samsung, many Android phones): SDR white -> 203 nits (BT.2408)
        return ["format=gbrapf32le" if alpha else "format=gbrpf32le",
                "zscale=tin=iec61966-2-1:pin=bt709:min=gbr:rin=full:t=smpte2084:p=bt2020:m=bt2020nc:r=tv:npl=203",
                f"format={out}"]
    f = []
    if target.hdr:
        f.append("colorchannelmixer=rr=0.75:gg=0.75:bb=0.75")
    m = "bt2020" if target.matrix.startswith("bt2020") else "bt709"
    f.append(f"scale=out_color_matrix={m}:out_range=tv")
    f.append(f"format={out}")
    return f


def _match_video_color(clip: Clip, target: Target) -> list[str]:
    hdr_src = clip.v.color_transfer in HDR_TRANSFERS
    if target.sdr and hdr_src:
        return _tonemap(clip)
    if target.transfer == "smpte2084" and not hdr_src:
        return ["zscale=tin=bt709:pin=bt709:min=bt709:rin=tv:t=smpte2084:p=bt2020:m=bt2020nc:r=tv:npl=203",
                f"format={target.pix_fmt}"]
    if target.hdr and not hdr_src:
        return ["scale=in_color_matrix=bt709:in_range=tv,format=gbrp",
                "colorchannelmixer=rr=0.75:gg=0.75:bb=0.75",
                f"scale=out_color_matrix=bt2020:out_range=tv,format={target.pix_fmt}"]
    return []


def _fade_layer(g: _Graph, base: str, ctx: _Ctx, o: ChainOpts, rate: str) -> str:
    """Fade from / to black, exact at any part boundary (a black layer whose
    transparency follows the segment's own clock)."""
    t = ctx.target
    L = o.seg_len or (o.t_offset + o.length)
    windows = []
    if o.fade_in > 0 and o.t_offset < o.fade_in:
        windows.append(("out", 0.0, o.fade_in))          # black layer: opaque -> clear
    if o.fade_out > 0 and o.t_offset + o.length > L - o.fade_out:
        windows.append(("in", L - o.fade_out, o.fade_out))
    for kind, st, d in windows:
        src = g.label("blk")
        g.chains.append(f"color=black:s={t.width}x{t.height}:r={rate}:d={o.length + 1:.3f},"
                        f"format={t.alpha_fmt},setpts=PTS+{o.t_offset:.6f}/TB,"
                        f"fade=t={kind}:st={st:.6f}:d={d:.6f}:alpha=1,setpts=PTS-{o.t_offset:.6f}/TB[{src}]")
        lo, hi = st - o.t_offset, st + d - o.t_offset
        if kind == "in":
            hi = o.length + 1
        else:
            lo = min(lo, 0.0)
        base = g.add(f"[{base}][{src}]", [f"overlay=format={t.overlay_fmt}:eof_action=pass:"
                                           f"enable='between(t,{lo:.4f},{hi:.4f})'"])
    return base


def seg_chain(g: _Graph, seg: Segment, ctx: _Ctx, a_src: float, b_src: float, o: ChainOpts,
              stabilize: bool = True) -> str:
    """Add the inputs/filters producing segment *seg*'s picture for source range
    [a_src, b_src) (forward order; reversed clips play it backwards). Returns
    the output label (canvas size, segment clock starting at 0)."""
    t = ctx.target
    clip = seg.clip
    W, H = t.width, t.height
    rate = _rate(o.fps) if o.fps else ctx.rate(clip)
    span = max(1e-3, b_src - a_src)
    f: list[str] = []
    if seg.kind == "image":
        k = g.input(["-loop", "1", "-framerate", rate, "-t", f"{o.length + 0.5:.6f}",
                     "-i", str(clip.render_path or clip.path)])
        f += ["setpts=PTS-STARTPTS", f"trim=duration={o.length:.6f}"] + _rgb_to_target(t)
    elif seg.kind == "freeze":
        k = g.input(["-ss", f"{_seek(seg.still_at, clip):.6f}", "-i", str(clip.path)])
        f += _deinterlace(clip, t) + ["trim=end_frame=1", "loop=loop=-1:size=1", f"setpts=N/(({rate})*TB)",
                                      f"trim=duration={o.length:.6f}"]
    else:
        k = g.input(["-ss", f"{_seek(a_src, clip):.6f}", "-t", f"{span + 0.5:.6f}", "-i", str(clip.path)])
        f += ["setpts=PTS-STARTPTS", f"trim=duration={span:.6f}"]
        f += _deinterlace(clip, t)
        trf = ctx.trf.get(_trf_key(seg, a_src, b_src)) if stabilize else None
        if trf:
            f.append(f"vidstabtransform=input={trf}:smoothing=20:optzoom=1:interpol=bicubic")
        if seg.edit.reverse:
            f.append("reverse")
        sp = seg.speed
        f.append("setpts=(PTS-STARTPTS)/%.6f" % sp if abs(sp - 1) > 1e-6 else "setpts=PTS-STARTPTS")
    f += _user_rotation(seg)

    # ---- fit to the canvas -------------------------------------------------
    dw, dh = _display_after_rotation(seg)
    fit_label = None
    if (dw, dh) != (W, H):
        differs = abs(dw / dh - W / H) > 0.01
        if t.cover and differs:
            f.append(f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H}")
        elif t.background == "blur" and differs:
            src = g.add(f"[{k}:v]", f + ["split"], g.label("sp"))
            # the split produces two outputs: rename the chain end accordingly
            g.chains[-1] = g.chains[-1].replace(f"[{src}]", f"[{src}a][{src}b]")
            bw, bh = _even(W / 8), _even(H / 8)
            bg = g.add(f"[{src}b]", [f"scale={bw}:{bh}:force_original_aspect_ratio=increase:flags=bilinear",
                                     f"crop={bw}:{bh}", "gblur=sigma=4", f"scale={W}:{H}:flags=bicubic",
                                     "setsar=1"])
            fg = g.add(f"[{src}a]", [f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos",
                                     "setsar=1"])
            fit_label = g.add(f"[{bg}][{fg}]", [f"overlay=(W-w)/2:(H-h)/2:format={t.overlay_fmt}"])
            f = []
        else:
            f.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
    f.append("setsar=1")

    # ---- zoom / Ken Burns -----------------------------------------------------
    e = seg.edit
    if e.has_transform:
        L = o.seg_len or seg.length
        if e.tf_end is None:
            z, px, py = max(1.0, e.tf.zoom), e.tf.x, e.tf.y
            f.append(f"scale={_even(W * z)}:{_even(H * z)}:flags=lanczos")
            f.append(f"crop={W}:{H}:x='(iw-{W})*(0.5+0.5*({px:.4f}))':y='(ih-{H})*(0.5+0.5*({py:.4f}))'")
        else:
            u = _ease(f"clip((t+{o.t_offset:.6f})/{max(L, 1e-3):.6f},0,1)")
            z0, z1 = max(1.0, e.tf.zoom), max(1.0, e.tf_end.zoom)
            Z = f"({z0:.4f}+({z1 - z0:.4f})*{u})"
            PX = f"({e.tf.x:.4f}+({e.tf_end.x - e.tf.x:.4f})*{u})"
            PY = f"({e.tf.y:.4f}+({e.tf_end.y - e.tf.y:.4f})*{u})"
            f.append(f"scale=w='2*trunc({W}*{Z}/2)':h='2*trunc({H}*{Z}/2)':eval=frame:flags=bicubic")
            f.append(f"crop={W}:{H}:x='(iw-ow)*(0.5+0.5*{PX})':y='(ih-oh)*(0.5+0.5*{PY})'")
    if seg.kind != "image":
        f += _match_video_color(clip, t)
    col = _color_filter(seg)
    if col:
        f.append(col)
    if e.sharpen > 1e-3:
        f.append(f"cas=strength={min(1.0, e.sharpen):.3f}")
    if e.blur > 1e-3:
        f.append(f"gblur=sigma={e.blur * 18 * W / 1920:.2f}")
    src = f"[{fit_label}]" if fit_label else f"[{k}:v]"
    out = g.add(src, f + [f"format={t.pix_fmt}"] if not o.preview else f)
    if e.vignette > 1e-3:
        vk = g.input(["-loop", "1", "-i", str(ctx.vignette(e.vignette))])
        out = g.add(f"[{out}][{vk}:v]", [f"overlay=format={t.overlay_fmt}:shortest=1"])
    if (o.fade_in > 0 or o.fade_out > 0) and not o.preview:
        out = _fade_layer(g, out, ctx, o, rate)
    if o.fps:
        out = g.add(f"[{out}]", [f"fps={_rate(o.fps)}"])
    return out


def apply_layers(g: _Graph, base: str, ctx: _Ctx, t0: float, t1: float) -> str:
    """Composite picture-in-picture items and captions visible in timeline [t0, t1)."""
    t = ctx.target
    W, H = t.width, t.height
    p = ctx.project
    for it in p.overlays:
        d = it.start - t0
        v0, v1 = max(0.0, d), min(t1 - t0, it.end - t0)
        if v1 - v0 < 1e-3:
            continue
        span = v1 - v0
        clip = it.clip
        ow = _even(W * max(0.05, min(1.0, it.scale)))
        if it.kind == "image":
            k = g.input(["-loop", "1", "-framerate", ctx.rate(), "-t", f"{span + 0.2:.6f}",
                         "-i", str(clip.render_path or clip.path)])
            f = ["setpts=PTS-STARTPTS", f"scale={ow}:-2:flags=lanczos"] + _rgb_to_target(t, alpha=True)
        else:
            seek = it.src_in + max(0.0, -d)
            k = g.input(["-ss", f"{_seek(seek, clip):.6f}", "-t", f"{span + 0.5:.6f}", "-i", str(clip.path)])
            f = ["setpts=PTS-STARTPTS", f"trim=duration={span:.6f}", f"scale={ow}:-2:flags=lanczos"]
            f += _match_video_color(clip, t) + [f"format={t.alpha_fmt}"]
        if it.opacity < 0.999:
            f.append(f"lut=a=val*{max(0.0, it.opacity):.3f}")
        f.append(f"setpts=PTS+{v0:.6f}/TB")
        ov = g.add(f"[{k}:v]", f)
        base = g.add(f"[{base}][{ov}]",
                     [f"overlay=x='W*{it.x:.4f}-w/2':y='H*{it.y:.4f}-h/2':eof_action=pass:"
                      f"format={t.overlay_fmt}:enable='between(t,{v0:.4f},{v1:.4f})'"])
    for it in p.texts:
        tr = ctx.texts.get(it.id)
        if tr is None or not it.active:
            continue
        d = it.start - t0
        v0, v1 = max(0.0, d), min(t1 - t0, it.end - t0)
        if v1 - v0 < 1e-3:
            continue
        span = v1 - v0
        files = ctx.text_files[it.id]
        if it.anim_in == "typewriter" and tr.steps and d >= -1e-6:
            k = g.input(["-f", "concat", "-safe", "0", "-i", str(files)])
            f = ["fps=" + ctx.rate(), "setpts=PTS-STARTPTS"]
        else:
            k = g.input(["-loop", "1", "-framerate", ctx.rate(), "-t", f"{span + 0.2:.6f}", "-i", str(files)])
            f = ["setpts=PTS-STARTPTS"]
        f += _rgb_to_target(t, alpha=True) + [f"setpts=PTS+{v0:.6f}/TB"]
        ad = max(0.05, min(it.anim_dur, it.duration / 2))
        S, E = d, d + it.duration
        pin = f"clip((t-({S:.4f}))/{ad:.4f},0,1)"
        pout = f"clip((t-({E - ad:.4f}))/{ad:.4f},0,1)"
        if it.anim_in in ("fade", "slide_up", "slide_left", "pop") and S >= -1e-6:
            f.append(f"fade=t=in:st={max(0.0, S):.4f}:d={ad:.4f}:alpha=1")
        if it.anim_out in ("fade", "slide_down", "pop") and E - ad >= 0:
            f.append(f"fade=t=out:st={E - ad:.4f}:d={ad:.4f}:alpha=1")
        if it.anim_in == "pop" or it.anim_out == "pop":
            kin = f"(0.6+0.4*{_ease(pin)})" if it.anim_in == "pop" else "1"
            kout = f"(1-0.4*{_ease(pout)})" if it.anim_out == "pop" else "1"
            f.append(f"scale=w='max(2,2*trunc(iw*{kin}*{kout}/2))':h=-2:eval=frame")
        cx, cy = tr.x + tr.w / 2, tr.y + tr.h / 2
        xe, ye = f"{cx:.2f}-w/2", f"{cy:.2f}-h/2"
        if it.anim_in == "slide_up":
            ye += f"+{H * 0.05:.2f}*(1-{_ease(pin)})"
        if it.anim_in == "slide_left":
            xe += f"-{W * 0.08:.2f}*(1-{_ease(pin)})"
        if it.anim_out == "slide_down":
            ye += f"+{H * 0.05:.2f}*{_ease(pout)}"
        tx = g.add(f"[{k}:v]", f)
        base = g.add(f"[{base}][{tx}]", [f"overlay=x='{xe}':y='{ye}':eof_action=pass:format={t.overlay_fmt}:"
                                         f"enable='between(t,{v0:.4f},{v1:.4f})'"])
    return base


def _finalize(g: _Graph, base: str, target: Target) -> str:
    post = []
    inv = _inverse_rotation(target.rotation) if target.rotation else ""
    if inv:
        post.append(inv)
    post.append(f"format={target.pix_fmt}")
    if target.interlaced:
        post.append("setfield=tff" if target.interlaced in ("tt", "tb") else "setfield=bff")
    # Tag every frame with exactly the reference's colour description (also
    # "unknown"). Filters such as caption overlays otherwise leave e.g. a
    # BT.709 tag behind; the encoder writes it into the stream header, and a
    # header that differs only in such details makes FFmpeg's HEVC decoder
    # mis-decode the stream-copied part that follows.
    post.append("setparams=color_primaries={}:color_trc={}:colorspace={}:range={}".format(
        target.primaries or "unknown", target.transfer or "unknown", target.matrix or "unknown",
        "pc" if target.color_range in ("pc", "jpeg") else "tv"))
    return g.add(f"[{base}]", post)


def _encoder_args(target: Target) -> list[str]:
    args = []
    prim, trc, mat, rng = target.primaries, target.transfer, target.matrix, target.color_range
    if target.codec == "hevc":
        xp = ["log-level=error", "repeat-headers=1"]
        if prim:
            xp.append(f"colorprim={prim}")
        if trc:
            xp.append(f"transfer={trc}")
        if mat:
            xp.append(f"colormatrix={mat}")
        if rng:
            xp.append("range=" + ("full" if rng in ("pc", "jpeg") else "limited"))
        # no forced profile: x265/x264 pick it from the pixel format (4:2:0 / 4:2:2 / 4:4:4)
        args += ["-c:v", "libx265", "-preset", target.speed_preset, "-crf", str(target.crf),
                 "-x265-params", ":".join(xp)]
    else:
        args += ["-c:v", "libx264", "-preset", target.speed_preset, "-crf", str(target.crf)]
        if target.interlaced:
            # re-encoded bits of interlaced camcorder footage stay interlaced, so
            # they match the stream-copied rest (1080i AVCHD, DV-style files)
            args += ["-flags", "+ildct+ilme"]        # field order comes from setfield in _finalize
    for opt, val in (("-color_primaries", prim), ("-color_trc", trc),
                     ("-colorspace", mat), ("-color_range", rng)):
        if val:
            args += [opt, val]
    return args


def _part_mux(dst: Path) -> list[str]:
    if dst.suffix == ".mkv":
        return ["-map_metadata", "-1", "-f", "matroska", "-n", str(dst)]
    # one time base for every MOV part: FFmpeg's concat doesn't convert between them
    return ["-video_track_timescale", COMMON_TIMESCALE, "-map_metadata", "-1", "-f", "mov", "-n", str(dst)]


# --------------------------------------------------------------------------- #
# Parts
# --------------------------------------------------------------------------- #
@dataclass
class Part:
    kind: str                    # source | embed | copy | encode | xfade
    duration: float
    seg: Segment | None = None
    seg_b: Segment | None = None
    a: float = 0.0               # source range (forward)
    b: float = 0.0
    t0: float = 0.0              # timeline time where this part starts
    frames: int = 0              # exact frame count (smart-cut encodes)
    opts: ChainOpts | None = None
    opts_b: ChainOpts | None = None
    b_a: float = 0.0             # second clip's source range (xfade)
    b_b: float = 0.0
    xfade: str = "fade"
    packets: int = 0
    drop: list[int] = field(default_factory=list)
    first: int = 0               # decode-order index of the first copied packet
    expect: list[int] = field(default_factory=list)   # sizes of the copied non-key packets

    @property
    def weight(self) -> float:
        return self.duration * (COPY_WEIGHT if self.kind in ("source", "embed", "copy") else ENCODE_WEIGHT)


def _fps_for(target: Target, *segs: Segment) -> Fraction:
    if target.fps:
        return target.fps
    rates = [s.clip.v.nominal_fps for s in segs if s.kind != "image" and s.clip.v.nominal_fps]
    return compat.fps_fraction(max(rates) if rates else 30)


def _needs_encode(seg: Segment, target: Target) -> bool:
    return (seg.is_still or not target.allow_copy or seg.clip.id in target.convert_ids
            or seg.edit.has_visual)


def _layer_intervals(project: Project) -> list[tuple[float, float]]:
    iv = [(t.start, t.end) for t in project.texts if t.active]
    iv += [(o.start, o.end) for o in project.overlays if o.duration > 0]
    return iv


def _merge(iv: list[tuple[float, float]], gap: float) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _reverse_chunk(seg: Segment, target: Target) -> float:
    """Seconds of a reversed clip FFmpeg may buffer at once (memory budget)."""
    bpp = 3.0 if "10" in (seg.clip.v.pix_fmt or "") else 1.5
    frame = seg.clip.v.width * seg.clip.v.height * bpp
    frames = max(6, int(REVERSE_BUDGET / max(frame, 1)))
    fps = seg.clip.v.nominal_fps or 30
    return max(0.1, frames / fps * seg.speed)        # in output seconds


def plan_parts(project: Project, target: Target, index_of) -> list[Part]:
    """Split the timeline into parts. *index_of(clip)* returns a PacketIndex."""
    segs = project.segments
    n = len(segs)
    trans = [project.transition_after(i) for i in range(n)]
    starts = project.starts()
    layers = _layer_intervals(project)
    parts: list[Part] = []

    def enc(seg, i, la, lb, fade_in, fade_out, fps=None):
        """Encode part for segment-local window [la, lb)."""
        sp = seg.speed
        if seg.edit.reverse:
            a, b = seg.src_out - lb * sp, seg.src_out - la * sp
        else:
            a, b = seg.src_in + la * sp, seg.src_in + lb * sp
        return Part("encode", lb - la, seg, a=a, b=b, t0=starts[i] + la,
                    opts=ChainOpts(t_offset=la, length=lb - la, fade_in=fade_in, fade_out=fade_out,
                                   seg_len=seg.length, fps=fps))

    for i, seg in enumerate(segs):
        L = seg.length
        lead = trans[i - 1] if i > 0 else 0.0
        trail = trans[i]
        fade_in = max(seg.edit.fade_in, FADE if (project.fade_in and i == 0) else 0.0)
        fade_out = max(seg.edit.fade_out, FADE if (project.fade_out and i == n - 1) else 0.0)
        la0, lb0 = lead, L - trail
        if lb0 - la0 > 1e-3:
            if _needs_encode(seg, target):
                # a constant rate gives exact, non-overlapping timing whatever the source's
                # container claims (WMV, VOB, 3GP, variable-rate phone footage …)
                fps = target.fps or _fps_for(target, seg)
                step = _reverse_chunk(seg, target) if seg.edit.reverse else (lb0 - la0)
                la = la0
                while la < lb0 - 1e-6:
                    lb = min(lb0, la + step)
                    if lb0 - lb < step * 0.25:           # avoid a tiny last chunk
                        lb = lb0
                    parts.append(enc(seg, i, la, lb, fade_in, fade_out, fps))
                    la = lb
            else:
                T = starts[i]
                forced = []
                for s, e in layers:
                    ls, le = max(la0, s - T), min(lb0, e - T)
                    if le - ls > 1e-3:
                        forced.append((ls, le))
                forced = _merge(forced, 1.5)
                # snap forced ranges that almost touch the body edges
                forced = [(la0 if ls - la0 < 1.0 else ls, lb0 if lb0 - le < 1.0 else le) for ls, le in forced]
                whole = (not forced and not seg.trimmed and lead == 0 and trail == 0
                         and not fade_in and not fade_out)
                if whole:
                    parts.append(Part("source", seg.clip.duration, seg, a=seg.src_in, b=seg.src_out, t0=T))
                else:
                    pos = la0
                    pieces = []
                    for ls, le in forced:
                        if ls > pos + 1e-6:
                            pieces.append(("copy", pos, ls))
                        pieces.append(("enc", ls, le))
                        pos = le
                    if lb0 > pos + 1e-6:
                        pieces.append(("copy", pos, lb0))
                    idx = None
                    for kind, la, lb in pieces:
                        idx = idx or index_of(seg.clip)
                        if kind == "enc":
                            # stretch under a caption / overlay: the exact source frames, so it
                            # meets the stream-copied neighbours without gaps or overlaps
                            pe = enc(seg, i, la, lb, fade_in, fade_out)
                            pe.a = _snap(idx.frames, pe.a, idx.end)
                            pe.b = _snap(idx.frames, pe.b, idx.end)
                            pe.frames = _count(idx.frames, pe.a, pe.b)
                            pe.duration = pe.b - pe.a
                            pe.t0 = T + (pe.a - seg.src_in)
                            pe.opts.t_offset, pe.opts.length = pe.a - seg.src_in, pe.duration
                            parts.append(pe)
                            continue
                        a, b = seg.src_in + la, seg.src_in + lb
                        cut = plan_cut(idx, a, b, seg.clip.v.codec,
                                       min_head=fade_in if la <= 1e-6 else 0.0,
                                       min_tail=fade_out if lb >= L - 1e-6 else 0.0)
                        if cut.head:
                            s, e, k = cut.head
                            parts.append(Part("encode", e - s, seg, a=s, b=e, frames=k, t0=T + (s - seg.src_in),
                                              opts=ChainOpts(t_offset=s - seg.src_in, length=e - s,
                                                             fade_in=fade_in, fade_out=fade_out, seg_len=L)))
                        if cut.middle:
                            s, e, pk, drop = cut.middle
                            i1 = cut.first_packet
                            dropset = set(drop)
                            kept = [j for j in range(i1, i1 + pk) if (j - i1) not in dropset]
                            parts.append(Part("copy", e - s, seg, a=s, b=e, packets=pk, drop=drop, first=i1,
                                              t0=T + (s - seg.src_in),
                                              expect=[idx.sizes[j] for j in kept if not idx.keys[j]]))
                        if cut.tail:
                            s, e, k = cut.tail
                            parts.append(Part("encode", e - s, seg, a=s, b=e, frames=k, t0=T + (s - seg.src_in),
                                              opts=ChainOpts(t_offset=s - seg.src_in, length=e - s,
                                                             fade_in=fade_in, fade_out=fade_out, seg_len=L)))
        if trail > 0 and i < n - 1:
            nb = segs[i + 1]
            fr = _fps_for(target, seg, nb)
            pa = enc(seg, i, L - trail, L, 0.0, 0.0)
            pb = enc(nb, i + 1, 0.0, trail, 0.0, 0.0)
            parts.append(Part(
                "xfade", trail, seg, nb, a=pa.a, b=pa.b, b_a=pb.a, b_b=pb.b, xfade=seg.edit.transition,
                t0=starts[i] + L - trail,
                opts=ChainOpts(t_offset=L - trail, length=trail, fps=fr, finalize=False, seg_len=L),
                opts_b=ChainOpts(t_offset=0.0, length=trail, fps=fr, finalize=False, seg_len=nb.length)))
    return parts


def _seek(t: float, clip: Clip) -> float:
    fps = (clip.v.nominal_fps if clip.v else 0) or 30
    return max(0.0, t - 0.25 / fps)


def _trf_key(seg: Segment, a: float, b: float) -> tuple:
    return (seg.id, round(a, 4), round(b, 4))


def encode_args(part: Part, ctx: _Ctx, dst: Path) -> list[str]:
    g = _Graph()
    base = seg_chain(g, part.seg, ctx, part.a, part.b, part.opts)
    base = apply_layers(g, base, ctx, part.t0, part.t0 + part.duration)
    out = _finalize(g, base, ctx.target)
    args = g.args() + ["-map", f"[{out}]", "-an", "-sn", "-dn"]
    if part.frames:
        args += ["-frames:v", str(part.frames)]
    else:
        args += ["-t", f"{part.duration:.6f}"]
    args += ["-fps_mode", "cfr" if part.opts.fps else "passthrough"]
    return args + _encoder_args(ctx.target) + _part_mux(dst)


def xfade_args(part: Part, ctx: _Ctx, dst: Path) -> list[str]:
    t = ctx.target
    g = _Graph()
    d = part.duration
    xa = seg_chain(g, part.seg, ctx, part.a, part.b, part.opts)
    xb = seg_chain(g, part.seg_b, ctx, part.b_a, part.b_b, part.opts_b)
    tail = [f"trim=duration={d:.6f}", "settb=AVTB", f"format={t.pix_fmt}"]
    xa = g.add(f"[{xa}]", tail)
    xb = g.add(f"[{xb}]", tail)
    base = g.add(f"[{xa}][{xb}]", [f"xfade=transition={part.xfade}:duration={d:.6f}:offset=0"])
    base = apply_layers(g, base, ctx, part.t0, part.t0 + d)
    out = _finalize(g, base, t)
    return (g.args() + ["-map", f"[{out}]", "-an", "-sn", "-dn", "-t", f"{d:.6f}", "-fps_mode", "cfr"]
            + _encoder_args(t) + _part_mux(dst))


def copy_args(part: Part, dst: Path) -> list[str]:
    """Stream-copy packets [first, first + packets) (decode order) of the clip.

    Packets are selected by their index rather than by seeking: seeking can
    land on an earlier keyframe in some MOV files. Reading the video packets
    from the start costs little (nothing is decoded)."""
    clip = part.seg.clip
    bsf = []
    terms = ([f"lt(n\\,{part.first})"] if part.first else []) + \
            [f"eq(n\\,{part.first + k})" for k in part.drop]
    if terms:
        bsf.append("noise=drop=" + "+".join(terms))
    if clip.v.codec == "hevc":
        bsf.append("hevc_mp4toannexb")
    # -frames:v counts packets *after* the drop filter
    args = ["-i", str(clip.path), "-map", "0:V:0", "-c", "copy",
            "-frames:v", str(part.packets - len(part.drop))]
    if bsf:
        args += ["-bsf:v", ",".join(bsf)]
    return args + _part_mux(dst)


def stab_detect_args(seg: Segment, a: float, b: float, name: str) -> list[str]:
    span = b - a
    return ["-ss", f"{_seek(a, seg.clip):.6f}", "-t", f"{span + 0.5:.6f}", "-i", str(seg.clip.path),
            "-vf", f"setpts=PTS-STARTPTS,trim=duration={span:.6f},"
                   f"vidstabdetect=result={name}:shakiness=6:accuracy=15",
            "-an", "-f", "null", "-"]


def _frame_hashes(path: Path, start: float | None = None, n: int = 3) -> list[str]:
    a = [ff.ffmpeg_path(), "-v", "error", "-noautorotate"]
    if start is not None:
        a += ["-ss", f"{max(0.0, start):.6f}"]
    a += ["-i", str(path), "-map", "0:V:0", "-frames:v", str(n), "-fps_mode", "passthrough", "-f", "framemd5", "-"]
    proc = ff.run(a, timeout=300)
    return [ln.rsplit(",", 1)[1].strip() for ln in ff.decode(proc.stdout).splitlines()
            if ln and not ln.startswith("#")]


def _copy_ok(p: "Part", dst: Path) -> bool:
    """Did the stream copy take exactly the planned packets?

    MP4/MOV sources: every packet size must match. Other containers (MPEG-TS
    from camcorders, Matroska …) change packet sizes when rewrapped, so there
    the packet count must match and the first frames must decode identically
    to the source at that point."""
    clip = p.seg.clip
    if "mov" in clip.format_name or "mp4" in clip.format_name:
        return _copied_sizes(dst) == p.expect
    proc = ff.run([ff.ffprobe_path(), "-v", "error", "-select_streams", "V:0", "-count_packets",
                   "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(dst)], timeout=300)
    import re
    m = re.search(r"\d+", ff.decode(proc.stdout))
    if not m or int(m.group()) != p.packets - len(p.drop):
        return False
    fps = clip.v.nominal_fps or 30
    want = _frame_hashes(clip.path, p.a - 0.25 / fps, 3)
    return bool(want) and _frame_hashes(dst, None, 3) == want


def _copied_sizes(path: Path) -> list[int] | None:
    """Sizes of the non-key video packets of *path* (to verify a copy)."""
    proc = ff.run([ff.ffprobe_path(), "-v", "error", "-select_streams", "V:0",
                   "-show_entries", "packet=size,flags", "-of", "json", str(path)], timeout=300)
    try:
        pk = json.loads(ff.decode(proc.stdout)).get("packets", [])
    except json.JSONDecodeError:
        return None
    return [int(p["size"]) for p in pk if "K" not in p.get("flags", "")]


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #
def _atempo(speed: float) -> list[str]:
    out = []
    while speed > 2.0 + 1e-9:
        out.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5 - 1e-9:
        out.append("atempo=0.5")
        speed /= 0.5
    if abs(speed - 1) > 1e-6:
        out.append(f"atempo={speed:.6f}")
    return out


def _audio_track(clip: Clip) -> int | None:
    return next((j for j, a in enumerate(clip.audio) if a.codec not in UNDECODABLE_AUDIO), None)


def audio_mix_args(project: Project, target: Target, dst: Path, speech_only: bool = False) -> list[str]:
    """The whole soundtrack in one FFmpeg run (or, with *speech_only*, just the
    clips' own sound as 16 kHz mono WAV for speech recognition)."""
    segs = project.segments
    rate = 16000 if speech_only else target.audio_rate
    layout = "mono" if speech_only else _LAYOUTS.get(target.audio_channels, "stereo")
    fmt = f"aresample={rate},aformat=sample_fmts=fltp:sample_rates={rate}:channel_layouts={layout}"
    g = _Graph()
    total = project.duration
    seg_labels = []
    for i, s in enumerate(segs):
        L = s.length
        e = s.edit
        track = _audio_track(s.clip) if s.kind == "video" else None
        vol = 0.0 if e.muted else e.volume
        if track is not None and vol > 0:
            span = s.src_out - s.src_in
            if s.clip.format_name in ("mpeg", "vob", "mpegvideo"):
                # DVD / MPEG-PS: seeking lands mid audio frame and FFmpeg aborts,
                # so decode from the start and cut afterwards (cheap for sound)
                k = g.input(["-i", str(s.clip.path)])
                f = [fmt, "asetpts=PTS-STARTPTS", f"atrim=start={s.src_in:.6f}:duration={span:.6f}",
                     "asetpts=PTS-STARTPTS"]
            else:
                k = g.input(["-ss", f"{s.src_in:.6f}", "-t", f"{span + 0.5:.6f}", "-i", str(s.clip.path)])
                f = [fmt, "asetpts=PTS-STARTPTS", f"atrim=duration={span:.6f}"]
            if e.reverse:
                f.append("areverse")
            f += _atempo(s.speed)
            if e.denoise and not speech_only:
                f.append("afftdn=nf=-25:tn=1")
            src = f"[{k}:a:{track}]"
        else:
            f = [f"anullsrc=r={rate}:cl={layout}", fmt]
            src = ""
        if abs(vol - 1) > 1e-4 and vol > 0:
            f.append(f"volume={vol:.4f}")
        f += ["apad", f"atrim=end={L:.6f}", "asetpts=PTS-STARTPTS"]
        fi = max(e.audio_fade_in, e.fade_in)
        fo = max(e.audio_fade_out, e.fade_out)
        if fi > 0 and not speech_only:
            f.append(f"afade=t=in:st=0:d={min(fi, L):.4f}")
        if fo > 0 and not speech_only:
            f.append(f"afade=t=out:st={max(0.0, L - fo):.4f}:d={min(fo, L):.4f}")
        seg_labels.append(g.add(src, f))
    cur = seg_labels[0]
    for i in range(1, len(seg_labels)):
        d = project.transition_after(i - 1)
        if d > 0:
            cur = g.add(f"[{cur}][{seg_labels[i]}]", [f"acrossfade=d={d:.6f}:c1=tri:c2=tri"])
        else:
            cur = g.add(f"[{cur}][{seg_labels[i]}]", ["concat=n=2:v=0:a=1"])
    if speech_only:
        out = g.add(f"[{cur}]", [f"atrim=end={total:.6f}"])
        return g.args() + ["-map", f"[{out}]", "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1",
                           "-f", "wav", "-y", str(dst)]

    def placed(k_src: str, f: list[str], start: float) -> str:
        ms = int(round(start * 1000))
        return g.add(k_src, f + ([f"adelay=delays={ms}:all=1"] if ms > 0 else []))

    speech = [cur]
    music_duck, music_free = [], []
    for o in project.overlays:
        if not o.has_audio:
            continue
        tr = _audio_track(o.clip)
        span = min(o.duration, max(0.0, total - o.start))
        if tr is None or span <= 0:
            continue
        k = g.input(["-ss", f"{o.src_in:.6f}", "-t", f"{span + 0.3:.6f}", "-i", str(o.clip.path)])
        f = [fmt, "asetpts=PTS-STARTPTS", f"atrim=duration={span:.6f}"]
        if abs(o.volume - 1) > 1e-4:
            f.append(f"volume={o.volume:.4f}")
        speech.append(placed(f"[{k}:a:{tr}]", f, o.start))
    for a in project.audios:
        span = project.audio_length(a)
        if span <= 0.01 or a.volume <= 0:
            continue
        loop = ["-stream_loop", "-1"] if (a.kind == "music" and a.loop) else []
        k = g.input(loop + ["-ss", f"{a.src_in:.6f}", "-i", str(a.path)])
        f = [fmt, "asetpts=PTS-STARTPTS", f"atrim=duration={span:.6f}", f"volume={a.volume:.4f}"]
        if a.fade_in > 0:
            f.append(f"afade=t=in:st=0:d={min(a.fade_in, span):.4f}")
        if a.fade_out > 0:
            f.append(f"afade=t=out:st={max(0.0, span - a.fade_out):.4f}:d={min(a.fade_out, span):.4f}")
        lab = placed(f"[{k}:a:0]", f, a.start)
        if a.kind == "music":
            (music_duck if a.duck else music_free).append(lab)
        else:
            speech.append(lab)

    def mix(labels: list[str]) -> str:
        if len(labels) == 1:
            return labels[0]
        return g.add("".join(f"[{x}]" for x in labels),
                     [f"amix=inputs={len(labels)}:duration=first:normalize=0:dropout_transition=0"])

    # main sound first so duration=first keeps the timeline length
    voice = mix(speech)
    parts = []
    if music_duck:
        sc_src = g.add(f"[{voice}]", ["asplit=2"], g.label("vs"))
        g.chains[-1] = g.chains[-1].replace(f"[{sc_src}]", f"[{sc_src}a][{sc_src}b]")
        voice = f"{sc_src}a"
        md = mix([g.add(f"[{x}]", ["apad", f"atrim=end={total:.6f}"]) for x in music_duck]) \
            if len(music_duck) > 1 else g.add(f"[{music_duck[0]}]", ["apad", f"atrim=end={total:.6f}"])
        ducked = g.add(f"[{md}][{sc_src}b]",
                       ["sidechaincompress=threshold=0.02:ratio=8:attack=30:release=500:makeup=1"])
        parts.append(ducked)
    parts += music_free
    final = mix([voice] + parts) if parts else voice
    post = []
    if parts:
        post.append("alimiter=limit=0.98:level=0")
    if project.fade_in:
        post.append(f"afade=t=in:st=0:d={FADE}")
    if project.fade_out:
        post.append(f"afade=t=out:st={max(0.0, total - FADE):.6f}:d={FADE}")
    post.append(f"atrim=end={total:.6f}")
    out = g.add(f"[{final}]", post)
    return g.args() + ["-map", f"[{out}]", "-c:a", "aac", "-b:a", target.audio_bitrate,
                       "-ar", str(target.audio_rate), "-f", "mp4", "-n", str(dst)]


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _write_texts(ctx: _Ctx) -> None:
    for it in ctx.project.texts:
        tr = ctx.texts.get(it.id)
        if tr is None:
            continue
        if it.anim_in == "typewriter" and tr.steps:
            lines = ["ffconcat version 1.0"]
            for j, png in enumerate(tr.steps):
                p = ctx.work / f"text_{it.id}_{j:03d}.png"
                p.write_bytes(png)
                lines += [f"file '{p.name}'", f"duration {tr.step_dur:.4f}"]
            p = ctx.work / f"text_{it.id}.png"
            p.write_bytes(tr.png)
            lines += [f"file '{p.name}'", f"duration {max(0.1, it.duration):.4f}", f"file '{p.name}'"]
            lst = ctx.work / f"text_{it.id}.txt"
            lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
            ctx.text_files[it.id] = lst
        else:
            p = ctx.work / f"text_{it.id}.png"
            p.write_bytes(tr.png)
            ctx.text_files[it.id] = p


def render_project(project: Project, output: Path, on_progress: ProgressFn, cancel: threading.Event,
                   texts: dict[str, TextRender] | None = None, target: Target | None = None) -> MergeResult:
    """Render *project* to *output*. *texts*: text item id -> caption image
    (drawn by the UI at export resolution)."""
    output = Path(output)
    target = target or make_target(project)
    segs = project.segments
    if not segs:
        raise MergeError("Nothing to export.")
    total = project.duration

    indexes: dict[str, PacketIndex] = {}

    def index_of(clip: Clip) -> PacketIndex:
        if clip.id not in indexes:
            if cancel.is_set():
                raise Cancelled()
            indexes[clip.id] = packet_index(clip.path)
        return indexes[clip.id]

    on_progress(0.0, None, "Planning")
    parts = plan_parts(project, target, index_of)
    hevc = target.codec == "hevc"
    any_new = any(p.kind != "source" for p in parts)
    sources = [p.seg.clip for p in parts if p.kind == "source"]
    # whole clips that are copied are rewrapped when they'd be joined with new
    # parts or with clips of another time base (concat can't mix time bases)
    tb_mismatch = len({stream_timebase(c.path, "V:0") for c in sources}) > 1
    if any_new or tb_mismatch or (hevc and needs_header_embedding(sources)):
        for p in parts:
            if p.kind == "source":
                p.kind = "embed"
    # copied sound: every clip's audio is rewrapped into one container type
    # (same time base everywhere), trimmed clips cut to their range
    audio_ext = (".mov" if all(audio_container(s.clip) == ".mov" for s in segs if s.clip.audio) else ".mka")
    audio_parts = [s for s in segs if s.clip.audio] if target.audio_copy else []

    # stabilizer analysis for every rendered range of a stabilized clip
    stab_jobs: list[tuple[Segment, float, float]] = []
    for p in parts:
        for seg, a, b in ((p.seg, p.a, p.b), (p.seg_b, p.b_a, p.b_b)):
            if p.kind in ("encode", "xfade") and seg is not None and seg.kind == "video" and seg.edit.stabilize:
                stab_jobs.append((seg, a, b))

    weights = [p.weight for p in parts if p.kind != "source"]
    weights += [(b - a) * ENCODE_WEIGHT * 0.4 for _, a, b in stab_jobs]
    weights += [s.length * COPY_WEIGHT * 0.2 for s in audio_parts]
    has_audio = any(s.has_audio for s in segs) or bool(project.audios) or any(o.has_audio for o in project.overlays)
    if has_audio and not target.audio_copy:
        weights.append(total * 0.6)
    weights.append(total * COPY_WEIGHT)
    stages = _Stages(on_progress, weights)
    parts_dir = _parts_dir(output)
    ctx = _Ctx(project, target, parts_dir, texts or {})
    tmp_out = None
    n_enc = sum(1 for p in parts if p.kind in ("encode", "xfade"))
    k_enc = 0
    fallback_notes: list[str] = []
    try:
        _write_texts(ctx)
        for j, (seg, a, b) in enumerate(stab_jobs):
            name = f"stab{j:04d}.trf"
            stages.run(stab_detect_args(seg, a, b, name), b - a, f"Analysing shake ({j + 1} of {len(stab_jobs)})",
                       cancel, cwd=parts_dir)
            ctx.trf[_trf_key(seg, a, b)] = name

        video_files, video_durs = [], []
        for i, p in enumerate(parts, 1):
            if p.kind == "source":
                video_files.append(p.seg.clip.path)
            elif p.kind == "embed":
                if hevc:
                    dst = parts_dir / f"p{i:05d}.mkv"
                    args = _embed_args(p.seg.clip.path, dst)
                else:
                    dst = parts_dir / f"p{i:05d}{target.part_ext}"
                    args = rewrap_video_args(p.seg.clip.path, dst)
                stages.run(args, p.duration, f"Copying {p.seg.clip.name} (lossless)", cancel)
                video_files.append(dst)
            elif p.kind == "copy":
                dst = parts_dir / f"p{i:05d}{target.part_ext}"
                stages.run(copy_args(p, dst), p.duration, f"Copying {p.seg.clip.name} (lossless)", cancel)
                if not _copy_ok(p, dst):
                    # Unexpected stream layout: fall back to encoding this range.
                    _remove_quietly(dst)
                    fallback_notes.append(f"Part of {p.seg.clip.name} couldn't be cut losslessly "
                                          "and was re-encoded instead.")
                    p.kind, p.frames = "encode", 0
                    p.opts = ChainOpts(t_offset=p.a - p.seg.src_in, length=p.duration, seg_len=p.seg.length)
                    run_ffmpeg_simple(encode_args(p, ctx, dst), p.duration, cancel, parts_dir)
                video_files.append(dst)
            elif p.kind == "encode":
                k_enc += 1
                dst = parts_dir / f"p{i:05d}{target.part_ext}"
                stages.run(encode_args(p, ctx, dst), p.duration, f"Rendering part {k_enc} of {n_enc}",
                           cancel, cwd=parts_dir)
                video_files.append(dst)
            else:
                k_enc += 1
                dst = parts_dir / f"p{i:05d}{target.part_ext}"
                stages.run(xfade_args(p, ctx, dst), p.duration,
                           f"Rendering transition ({k_enc} of {n_enc})", cancel, cwd=parts_dir)
                video_files.append(dst)
            video_durs.append(p.duration)

        # ---- audio ---------------------------------------------------------
        audio_input: list[str] = []
        td = tempfile.mkdtemp(prefix="lvm-", dir=str(parts_dir))
        if has_audio and target.audio_copy:
            files, durs = [], []
            for j, s in enumerate(segs, 1):
                dst = parts_dir / f"a{j:05d}{audio_ext}"
                args = (rewrap_audio_args(s.clip.path, dst, s.src_in, s.src_out - s.src_in) if s.trimmed
                        else rewrap_audio_args(s.clip.path, dst))
                stages.run(args, s.length, "Copying audio (lossless)", cancel)
                files.append(dst)
                durs.append(s.length)
            alist = _concat_list(files, Path(td), "audio.txt", durs)
            audio_input = ["-f", "concat", "-safe", "0", "-i", str(alist)]
        elif has_audio:
            dst = parts_dir / "soundtrack.m4a"
            stages.run(audio_mix_args(project, target, dst), total, "Mixing audio", cancel)
            audio_input = ["-i", str(dst)]

        # ---- join -----------------------------------------------------------
        tmp_out = _temp_output(output)

        def join_args() -> list[str]:
            vlist = _concat_list(video_files, Path(td), "video.txt", video_durs)
            rot = ["-display_rotation:v:0", str(target.rotation)] if target.rotation else []
            args = [*rot, "-f", "concat", "-safe", "0", "-i", str(vlist), *audio_input, "-map", "0:V"]
            if audio_input:
                args += ["-map", "1:a?"]
            args += ["-c", "copy", "-avoid_negative_ts", "make_zero", "-max_muxing_queue_size", "4096"]
            ext = output.suffix.lower()
            args += ["-f", MUXERS.get(ext, "matroska")]
            if ext in (".mp4", ".m4v", ".mov"):
                if hevc:
                    args += ["-tag:v", "hvc1"]
                args += ["-movflags", "+faststart"] if project.preset != "original" else []
            first = next((s.clip for s in segs if s.kind == "video"), None)
            if first is not None and first.creation_time_tag:
                args += ["-metadata", f"creation_time={first.creation_time_tag}"]
            return args + ["-n", str(tmp_out)]

        stages.run(join_args(), total, "Joining", cancel)
        on_progress(1.0, None, "Verifying")

        # ---- verify: copied footage right after rendered footage must decode
        # exactly as in the original (guards against decoder quirks when the
        # stream switches between encoders); repair by re-rendering if not.
        bad = _bad_boundaries(tmp_out, parts, video_files, video_durs)
        if bad:
            for i in bad:
                p = parts[i]
                fallback_notes.append(f"Part of {p.seg.clip.name} didn't join cleanly after an edit and "
                                      "was re-encoded instead.")
                p.kind, p.frames = "encode", 0
                p.opts = ChainOpts(t_offset=p.a - p.seg.src_in, length=p.duration, seg_len=p.seg.length)
                dst = parts_dir / f"r{i:05d}{target.part_ext}"
                on_progress(1.0, None, f"Repairing part {i + 1}")
                run_ffmpeg_simple(encode_args(p, ctx, dst), p.duration, cancel, parts_dir)
                video_files[i] = dst
            _remove_quietly(tmp_out)
            run_ffmpeg_simple(join_args(), total, cancel)
            if _bad_boundaries(tmp_out, parts, video_files, video_durs):
                raise MergeError("The joined video failed verification (copied footage didn't decode "
                                 "identically). No file was written.")

        if has_audio and not probe(tmp_out).audio:
            raise MergeError("The sound could not be written into the video (the audio format didn't fit the "
                             "container). No file was written — please report this file type.")
        copied = sum(p.duration for p in parts if p.kind in ("source", "embed", "copy"))
        notes = list(target.notes) + fallback_notes
        if 0 < copied < total - 0.05:
            notes.append(f"{copied:.0f} of {total:.0f} seconds were copied without re-encoding; "
                         "only edited parts were rendered.")
        lossless = copied >= total - 0.05 and target.audio_copy
        return _finish(tmp_out, output, total, lossless, notes)
    except BaseException:
        if tmp_out:
            _remove_quietly(tmp_out)
        raise
    finally:
        _remove_quietly(parts_dir)


def _bad_boundaries(joined: Path, parts: list[Part], files: list[Path], durs: list[float]) -> list[int]:
    """Indexes of copied parts that follow a rendered part and don't decode
    bit-exact in the joined file (first frames compared)."""
    bad, offset = [], 0.0
    for i, p in enumerate(parts):
        if (i > 0 and p.kind in ("source", "embed", "copy")
                and parts[i - 1].kind in ("encode", "xfade")):
            want = _frame_hashes(files[i], None, 3)
            fps = p.seg.clip.v.nominal_fps or 30
            # decode a window around the boundary (timestamps may be shifted by
            # container start offsets) and look for the expected frames in it
            got = _frame_hashes(joined, offset - 0.6, int(fps * 1.2) + 6)
            ok = bool(want) and any(got[j:j + len(want)] == want for j in range(len(got)))
            if not ok:
                bad.append(i)
        offset += durs[i]
    return bad


def run_ffmpeg_simple(args: list[str], duration: float, cancel: threading.Event, cwd: Path | None = None) -> None:
    from .merger import run_ffmpeg
    run_ffmpeg(args, duration, lambda f, s: None, cancel, cwd)


# --------------------------------------------------------------------------- #
# Single-frame preview (exactly what the export's main track will look like)
# --------------------------------------------------------------------------- #
def preview_frame(seg: Segment, project: Project, t_local: float, width: int,
                  target: Target | None = None, timeout: float = 30) -> bytes | None:
    """PNG of segment *seg* at *t_local* seconds into it (without the overlay
    tracks; the preview draws those itself). Stabilization is not previewed."""
    target = target or make_target(project)
    with tempfile.TemporaryDirectory(prefix="lvm-pv-") as td:
        ctx = _Ctx(project, target, Path(td))
        g = _Graph()
        L = seg.length
        t_local = min(max(0.0, t_local), max(0.0, L - 1e-3))
        fps = seg.clip.v.nominal_fps if seg.kind == "video" else 30
        step = 1.0 / (fps or 30)
        if seg.edit.reverse:
            a = max(seg.src_in, seg.src_time(t_local) - step * seg.speed)
            b = a + step * seg.speed
        else:
            a = seg.src_time(t_local)
            b = a + step * seg.speed
        # fades are drawn live by the preview itself
        opts = ChainOpts(t_offset=t_local, length=step, seg_len=L, preview=False)
        out = seg_chain(g, seg, ctx, a, b, opts, stabilize=False)
        out = g.add(f"[{out}]", [f"scale={width}:-2:flags=bilinear", "format=rgb24"])
        args = [ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-nostdin", *g.args(),
                "-map", f"[{out}]", "-frames:v", "1", "-an", "-f", "image2pipe", "-c:v", "png", "pipe:1"]
        try:
            proc = ff.run(args, timeout=timeout, cwd=td)
        except Exception:
            return None
        return proc.stdout if proc.returncode == 0 and proc.stdout else None
