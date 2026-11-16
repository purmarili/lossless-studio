"""The actual merging, done by FFmpeg in a separate process.

FFmpeg streams the files from disk to disk, so memory use stays small no
matter how large the recordings are. The result is first written to a hidden
temporary file next to the destination and only renamed into place once FFmpeg
has finished successfully, so a cancelled or failed merge never leaves a
half-written file behind and never touches an existing file.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import ffmpeg_tools as ff
from .compat import EncodeTarget
from .media import Clip, probe

# progress callback: (fraction 0..1, speed multiplier or None, status text)
ProgressFn = Callable[[float, "float | None", str], None]

MUXERS = {
    ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska",
    ".webm": "webm", ".ts": "mpegts", ".mts": "mpegts", ".m2ts": "mpegts",
    ".avi": "avi",
}


class Cancelled(Exception):
    pass


class MergeError(Exception):
    pass


@dataclass
class MergeResult:
    output: Path
    duration: float
    size: int
    lossless: bool
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# FFmpeg process with progress + cancellation
# --------------------------------------------------------------------------- #
def run_ffmpeg(args: list[str], duration: float, on_progress: Callable[[float, float | None], None],
               cancel: threading.Event, cwd: Path | None = None) -> None:
    cmd = [ff.ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *args]
    proc = ff.popen(cmd, cwd=cwd)
    errors: deque[str] = deque(maxlen=30)

    def drain_stderr():
        for raw in proc.stderr:
            line = ff.decode(raw).rstrip()
            if line:
                errors.append(line)

    def watch_cancel():
        while proc.poll() is None:
            if cancel.wait(0.1):
                proc.kill()
                return

    threads = [threading.Thread(target=drain_stderr, daemon=True),
               threading.Thread(target=watch_cancel, daemon=True)]
    for t in threads:
        t.start()

    speed = None
    for raw in proc.stdout:
        key, _, value = ff.decode(raw).strip().partition("=")
        if key == "speed":
            try:
                speed = float(value.rstrip("x"))
            except ValueError:
                speed = None
        elif key == "out_time_us":
            try:
                sec = int(value) / 1_000_000
            except ValueError:
                continue
            if duration > 0:
                on_progress(min(max(sec / duration, 0.0), 1.0), speed)
    proc.wait()
    for t in threads:
        t.join(timeout=2)
    proc.stdout.close()
    proc.stderr.close()
    if cancel.is_set():
        raise Cancelled()
    if proc.returncode != 0:
        detail = "\n".join(errors) or f"FFmpeg exited with code {proc.returncode}"
        raise MergeError(detail)



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _concat_list(paths: list[Path], directory: Path, name: str = "concat.txt",
                 durations: list[float] | None = None) -> Path:
    """Write an ffconcat list (UTF-8, so any Unicode file name works).

    Explicit durations make two lists (video / audio) advance in lock-step."""
    lines = ["ffconcat version 1.0"]
    for i, p in enumerate(paths):
        escaped = str(Path(p).resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
        if durations and durations[i] > 0:
            lines.append(f"duration {durations[i]:.6f}")
    list_path = directory / name
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_path


def _temp_output(final: Path) -> Path:
    return final.with_name(f".{final.stem}.merging-{secrets.token_hex(4)}{final.suffix}")


def _remove_quietly(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def _container_args(video_clip: Clip, time_clip: Clip, out: Path) -> list[str]:
    ext = out.suffix.lower()
    args = ["-f", MUXERS.get(ext, "matroska")]
    v = video_clip.v
    if ext in (".mp4", ".m4v", ".mov") and v and v.codec == "hevc":
        # hvc1 is the tag Apple devices/QuickTime require; keep hev1 if the source used it.
        args += ["-tag:v", v.codec_tag if v.codec_tag in ("hvc1", "hev1") else "hvc1"]
    if time_clip.creation_time_tag:
        args += ["-metadata", f"creation_time={time_clip.creation_time_tag}"]
    return args


def _finish(tmp: Path, final: Path, expected: float, lossless: bool, notes: list[str]) -> MergeResult:
    info = probe(tmp)
    if info.error:
        _remove_quietly(tmp)
        raise MergeError(f"The merged file could not be verified: {info.error}")
    tolerance = max(2.0, expected * 0.005)
    if expected and abs(info.duration - expected) > tolerance:
        notes.append(f"Duration check: merged file is {info.duration:.1f}s, "
                     f"clips add up to {expected:.1f}s.")
    os.replace(tmp, final)   # final destination was confirmed by the user beforehand
    return MergeResult(final, info.duration, final.stat().st_size, lossless, notes)


class _Stages:
    """Maps the progress of several FFmpeg runs onto one 0..1 bar (by weight)."""

    def __init__(self, on_progress: ProgressFn, weights: list[float]):
        self.on_progress, self.total = on_progress, sum(weights) or 1.0
        self.weights, self.done, self.index = weights, 0.0, 0

    def run(self, args: list[str], duration: float, status: str, cancel: threading.Event,
            cwd: Path | None = None) -> None:
        w, base = self.weights[self.index], self.done
        run_ffmpeg(args, duration,
                   lambda f, s: self.on_progress(min(1.0, (base + f * w) / self.total), s, status),
                   cancel, cwd)
        self.done += w
        self.index += 1


def _parts_dir(output: Path) -> Path:
    d = output.with_name(f".{output.stem}.parts-{secrets.token_hex(4)}")
    d.mkdir()
    return d


def needs_header_embedding(clips: list[Clip]) -> bool:
    """HEVC clips with different codec headers (SPS/PPS/VPS).

    The concat demuxer keeps only the first clip's header, so later clips would
    decode with the wrong parameters (dropped/corrupt frames). FFmpeg fixes this
    automatically for H.264 only; for HEVC each clip's video is re-wrapped first
    with its own headers placed in the stream (lossless, packets are unchanged).
    """
    vids = [c.v for c in clips if c.v]
    return bool(vids) and vids[0].codec == "hevc" and len({v.extradata_hash for v in vids}) > 1


# --------------------------------------------------------------------------- #
# Per-clip preparation
# --------------------------------------------------------------------------- #
DUPLICATE = "duplicate"   # repeat frames (motion identical to the original)
BLEND = "blend"           # blend neighbouring frames (smoother, slight ghosting)
CONFORM_CODECS = {"hevc", "h264"}

COPY_WEIGHT = 0.15      # relative cost of a stream-copy pass per second of footage
ENCODE_WEIGHT = 10.0    # relative cost of re-encoding a second of (4K) footage


def _embed_args(src: Path, dst: Path) -> list[str]:
    # Video only, in Matroska: it keeps in-band parameter sets (MP4/MOV would
    # strip them). Audio is taken straight from the originals, because e.g.
    # Apple spatial audio can't be stored in Matroska.
    return ["-i", str(src), "-map", "0:V:0", "-c", "copy",
            "-bsf:v", "hevc_mp4toannexb", "-f", "matroska", "-n", str(dst)]


def _known(v: str) -> str:
    return "" if (not v or v in ("unknown", "unspecified", "reserved")) else v


def _inverse_rotation(rotation: int) -> str:
    """Filter that turns an upright picture into the reference's stored
    orientation, so the reference's rotation flag displays it upright again.
    FFmpeg's autorotate applies theta = -rotation; this undoes it."""
    theta = (-rotation) % 360
    return {90: "transpose=cclock", 270: "transpose=clock", 180: "hflip,vflip"}.get(theta, "")


def _convert_args(clip: Clip, ref: Clip, fps: float | None, method: str, part: Path) -> list[str]:
    """Re-encode *clip*'s video so it can be stream-copied together with *ref*."""
    from .compat import fps_fraction
    from .media import same_fps
    v, rv = clip.v, ref.v
    same_geometry = (v.width, v.height, v.rotation) == (rv.width, rv.height, rv.rotation)
    filters = []
    change_rate = bool(fps) and not same_fps(v.nominal_fps, fps)
    if change_rate:
        fr = fps_fraction(fps)
        rate = f"{fr.numerator}/{fr.denominator}"
        filters.append(f"framerate=fps={rate}" if method == BLEND else f"fps={rate}")
    if not same_geometry:
        # FFmpeg rotates the input upright (autorotate); fit it into the
        # reference's display size, then store it the way the reference is stored.
        dw, dh = rv.display_size
        if v.display_size != (dw, dh):
            filters.append(f"scale={dw}:{dh}:force_original_aspect_ratio=decrease:flags=lanczos,"
                           f"pad={dw}:{dh}:(ow-iw)/2:(oh-ih)/2:color=black")
        filters.append("setsar=1")
        inv = _inverse_rotation(rv.rotation)
        if inv:
            filters.append(inv)
    pix = rv.pix_fmt or "yuv420p"
    filters.append(f"format={pix}")

    args = ([] if not same_geometry else ["-noautorotate"]) + ["-i", str(clip.path),
            "-map", "0:V:0", "-an", "-sn", "-dn", "-vf", ",".join(filters),
            # never run past the original clip (frame-rate filters can emit one extra
            # frame), so the next clip's first frame isn't overlapped
            "-t", f"{clip.duration:.6f}",
            # keep the clip's own (possibly variable) timing unless converting the rate
            "-fps_mode", "cfr" if change_rate else "passthrough"]

    ten_bit = "10" in pix or "12" in pix
    # Same colour description (HDR HLG/PQ, BT.2020 ...) as the copied clips.
    prim = _known(v.color_primaries) or _known(rv.color_primaries)
    trc = _known(v.color_transfer) or _known(rv.color_transfer)
    space = _known(v.color_space) or _known(rv.color_space)
    rng = _known(v.color_range) or _known(rv.color_range)
    if rv.codec == "hevc":
        # x265 ignores FFmpeg's -color_* options, so pass them as x265 parameters.
        xp = ["log-level=error", "repeat-headers=1"]
        if prim:
            xp.append(f"colorprim={prim}")
        if trc:
            xp.append(f"transfer={trc}")
        if space:
            xp.append(f"colormatrix={space}")
        if rng:
            xp.append("range=" + ("full" if rng in ("pc", "jpeg") else "limited"))
        args += ["-c:v", "libx265", "-preset", "fast", "-crf", "16",
                 "-profile:v", "main10" if ten_bit else "main", "-x265-params", ":".join(xp)]
    else:
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "16",
                 "-profile:v", "high10" if ten_bit else "high"]
    for opt, val in (("-color_primaries", prim), ("-color_trc", trc),
                     ("-colorspace", space), ("-color_range", rng)):
        if val:
            args += [opt, val]
    # HEVC parts go in Matroska (keeps the in-band headers); H.264 parts in MOV,
    # which keeps exact decode timestamps (FFmpeg handles H.264 headers itself).
    fmt = "matroska" if part.suffix == ".mkv" else "mov"
    args += ["-map_metadata", "-1", "-f", fmt, "-n", str(part)]
    return args


# --------------------------------------------------------------------------- #
# The merge pipeline
# --------------------------------------------------------------------------- #
def merge_smart(clips: list[Clip], output: Path, convert_ids: set[str], fps: float | None,
                method: str, on_progress: ProgressFn, cancel: threading.Event,
                reference: Clip | None = None) -> MergeResult:
    """Join *clips* into *output*.

    - clips in *convert_ids* get their video re-encoded to match *reference*
      (and, with *fps*, converted to that frame rate);
    - all other clips are copied bit-exact;
    - audio of every clip is always copied unchanged from the original file.
    """
    from .compat import fit_plan
    output = Path(output)
    ref = reference or fit_plan(clips, fps).reference
    if convert_ids and ref.v.codec not in CONFORM_CODECS:
        raise MergeError(f"Only H.264 and HEVC clips can be converted, not {ref.v.codec}.")
    hevc = ref.v.codec == "hevc"
    embed = hevc and (bool(convert_ids) or needs_header_embedding(clips))
    actions = ["encode" if c.id in convert_ids else ("embed" if embed else "copy") for c in clips]
    separate = any(a != "copy" for a in actions)
    total = sum(c.duration for c in clips)

    weights = [c.duration * (ENCODE_WEIGHT if a == "encode" else COPY_WEIGHT)
               for c, a in zip(clips, actions) if a != "copy"] + [total * COPY_WEIGHT]
    stages = _Stages(on_progress, weights)
    parts_dir = _parts_dir(output) if separate else None
    tmp_out = None
    n_conv = actions.count("encode")
    try:
        video_sources, k = [], 0
        for i, (c, a) in enumerate(zip(clips, actions), 1):
            if a == "encode":
                k += 1
                dst = parts_dir / f"clip{i:05d}{'.mkv' if hevc else '.mov'}"
                what = f"to {fps:g} fps" if fps and c.v and not _same(c, fps) else "to match"
                stages.run(_convert_args(c, ref, fps, method, dst), c.duration,
                           f"Converting clip {k} of {n_conv} ({what})", cancel)
                video_sources.append(dst)
            elif a == "embed":
                dst = parts_dir / f"clip{i:05d}.mkv"
                stages.run(_embed_args(c.path, dst), c.duration,
                           f"Preparing clip {i} of {len(clips)} (lossless)", cancel)
                video_sources.append(dst)
            else:
                video_sources.append(c.path)

        tmp_out = _temp_output(output)
        with tempfile.TemporaryDirectory(prefix="lvm-") as td:
            rot = ["-display_rotation:v:0", str(ref.v.rotation)] if ref.v.rotation else []
            common = ["-c", "copy", "-avoid_negative_ts", "make_zero", "-max_muxing_queue_size", "4096",
                      *_container_args(ref, clips[0], output), "-n", str(tmp_out)]
            if not separate:
                lst = _concat_list(video_sources, Path(td))
                args = [*rot, "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-map", "0:V", "-map", "0:a?", *common]
            else:
                durs = [c.duration for c in clips]
                vlist = _concat_list(video_sources, Path(td), "video.txt", durs)
                alist = _concat_list([c.path for c in clips], Path(td), "audio.txt", durs)
                args = [*rot, "-f", "concat", "-safe", "0", "-i", str(vlist),
                        "-f", "concat", "-safe", "0", "-i", str(alist),
                        "-map", "0:V", "-map", "1:a?", *common]
            stages.run(args, total, "Joining (lossless)", cancel)
        on_progress(1.0, None, "Verifying")
        notes = []
        if n_conv:
            notes.append(f"{n_conv} clip(s) were re-encoded to match; the other "
                         f"{len(clips) - n_conv} were copied untouched. Audio was copied unchanged.")
        return _finish(tmp_out, output, total, n_conv == 0, notes)
    except BaseException:
        if tmp_out:
            _remove_quietly(tmp_out)
        raise
    finally:
        if parts_dir:
            _remove_quietly(parts_dir)


def _same(c: Clip, fps: float) -> bool:
    from .media import same_fps
    return same_fps(c.v.nominal_fps, fps)


def merge_lossless(clips: list[Clip], output: Path, on_progress: ProgressFn,
                   cancel: threading.Event) -> MergeResult:
    """Everything stream-copied (HEVC header embedding applied when needed)."""
    return merge_smart(clips, output, set(), None, DUPLICATE, on_progress, cancel, clips[0])


def merge_conform(clips: list[Clip], output: Path, fps: float, method: str,
                  on_progress: ProgressFn, cancel: threading.Event) -> MergeResult:
    """Convert clips at other frame rates (and mismatched clips) to *fps*."""
    from .compat import fit_plan
    plan = fit_plan(clips, fps)
    return merge_smart(clips, output, set(plan.convert), fps, method, on_progress, cancel,
                       plan.reference)


def merge_fit(clips: list[Clip], output: Path, on_progress: ProgressFn, cancel: threading.Event,
              fps: float | None = None, method: str = DUPLICATE) -> MergeResult:
    """Re-encode only the clips whose video doesn't match the others."""
    from .compat import fit_plan
    plan = fit_plan(clips, fps)
    if not plan.feasible:
        raise MergeError(plan.problem)
    return merge_smart(clips, output, set(plan.convert), fps, method, on_progress, cancel,
                       plan.reference)


# --------------------------------------------------------------------------- #
# Full re-encode (only after explicit user consent)
# --------------------------------------------------------------------------- #
_LAYOUTS = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}


def _encode_args(clip: Clip, t: EncodeTarget, part: Path) -> list[str]:
    args = ["-i", str(clip.path)]
    if t.audio and not clip.audio:
        layout = _LAYOUTS.get(t.channels, "stereo")
        args += ["-f", "lavfi", "-t", f"{clip.duration:.3f}",
                 "-i", f"anullsrc=r={t.sample_rate}:cl={layout}"]
    fps = f"{t.fps.numerator}/{t.fps.denominator}"
    pix = "yuv420p10le" if t.ten_bit else "yuv420p"
    vf = (f"scale={t.width}:{t.height}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={t.width}:{t.height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
          f"fps={fps},format={pix}")
    args += ["-map", "0:V:0"]
    if t.audio:
        args += ["-map", "0:a:0" if clip.audio else "1:a:0"]
    args += ["-vf", vf]
    if t.video_codec == "hevc":
        args += ["-c:v", "libx265", "-preset", "medium", "-crf", "18", "-tag:v", "hvc1",
                 "-x265-params", "log-level=error"]
    else:
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "16"]
    if t.audio:
        args += ["-c:a", "aac", "-b:a", "320k", "-ar", str(t.sample_rate), "-ac", str(t.channels)]
    args += ["-map_metadata", "-1", "-f", "mp4", "-n", str(part)]
    return args


def merge_reencode(clips: list[Clip], output: Path, target: EncodeTarget,
                   on_progress: ProgressFn, cancel: threading.Event) -> MergeResult:
    """Normalise every clip to one format, then join the parts losslessly.

    Encoding clip by clip keeps only one decoder/encoder pair in memory at a
    time, which matters for long 4K footage.
    """
    output = Path(output)
    total = sum(c.duration for c in clips) or 1.0
    parts_dir = _parts_dir(output)
    tmp_out = _temp_output(output)
    encode_share = 0.97
    try:
        parts, done = [], 0.0
        for i, clip in enumerate(clips, 1):
            part = parts_dir / f"part{i:05d}.mp4"
            status = f"Re-encoding clip {i} of {len(clips)}"
            base = done

            def prog(f, s, base=base, d=clip.duration, status=status):
                on_progress((base + f * d) / total * encode_share, s, status)

            run_ffmpeg(_encode_args(clip, target, part), clip.duration, prog, cancel)
            parts.append(part)
            done += clip.duration

        list_file = _concat_list(parts, parts_dir)
        first_part = probe(parts[0])
        args = ["-f", "concat", "-safe", "0", "-i", str(list_file),
                "-map", "0:V", "-map", "0:a?", "-c", "copy",
                *_container_args(first_part, clips[0], output),
                "-n", str(tmp_out)]
        run_ffmpeg(args, total,
                   lambda f, s: on_progress(encode_share + f * (1 - encode_share), s, "Joining"), cancel)
        on_progress(1.0, None, "Verifying")
        return _finish(tmp_out, output, total, False,
                       ["Clips were re-encoded to a common format at your request."])
    except BaseException:
        _remove_quietly(tmp_out)
        raise
    finally:
        _remove_quietly(parts_dir)
