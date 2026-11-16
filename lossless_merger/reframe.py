"""360° footage (Insta360 X / ONE, stitched 360 MP4s) -> a normal video.

Raw two-lens footage is stitched and reframed in one step with FFmpeg's
v360 filter: you choose where to look (left/right, up/down, tilt), how wide
(field of view), or a "tiny planet". The result is a normal video file that
the editor treats like any other clip.

The quick stitch uses a generic lens model; Insta360 Studio uses per-camera
calibration and stitches seams more precisely.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg_tools as ff
from .media import Clip
from .merger import MergeError, ProgressFn, run_ffmpeg

MODES = {"flat": "Normal view", "wide": "Wide (fisheye look)", "planet": "Tiny planet"}


@dataclass
class View360:
    mode: str = "flat"
    yaw: float = 0.0          # look left (-) / right (+), degrees
    pitch: float = 0.0        # look down (-) / up (+)
    roll: float = 0.0         # tilt
    fov: float = 100.0        # horizontal field of view (normal view)
    swap: bool = False        # swap the two lenses (front / back)
    lens_fov: float = 200.0   # angle one raw lens covers


def out_size(clip: Clip, view: View360) -> tuple[int, int]:
    """Output resolution: about the detail the lenses really have, at most 4K."""
    v = clip.v
    lens = v.height if clip.projection in ("dfisheye", "dfisheye2") else v.height / 2
    if view.mode == "planet":
        s = int(min(2160, max(720, lens)))
        return s - s % 2, s - s % 2
    h = int(min(2160, max(720, lens * 0.75)))
    h -= h % 2
    w = int(h * 16 / 9)
    return w - w % 2, h


def inputs(clip: Clip, start: float | None = None, length: float | None = None) -> list[str]:
    pre = (["-ss", f"{start:.3f}"] if start else []) + (["-t", f"{length:.3f}"] if length else [])
    args = pre + ["-i", str(clip.path)]
    if clip.lens_pair:
        args += pre + ["-i", str(clip.lens_pair)]
    return args


def graph(clip: Clip, view: View360, w: int, h: int, out: str = "v") -> str:
    """Filtergraph text producing [out] from the input(s)."""
    proj = clip.projection
    parts = []
    if proj == "dfisheye2":
        a, b = ("0:v:0", "1:v:0") if clip.lens_pair else ("0:v:0", "0:v:1")
        if view.swap:
            a, b = b, a
        parts.append(f"[{a}][{b}]hstack=inputs=2[sbs]")
        src, ifmt = "[sbs]", "dfisheye"
    elif proj == "dfisheye":
        if view.swap:
            parts.append("[0:v:0]split[l][r];[l]crop=iw/2:ih:0:0[l2];[r]crop=iw/2:ih:iw/2:0[r2];[r2][l2]hstack[sbs]")
            src = "[sbs]"
        else:
            src = "[0:v:0]"
        ifmt = "dfisheye"
    else:
        src, ifmt = "[0:v:0]", "e"
    lens = f":ih_fov={view.lens_fov:.1f}:iv_fov={view.lens_fov:.1f}" if ifmt == "dfisheye" else ""
    if view.mode == "planet":
        v360 = (f"v360=input={ifmt}:output=sg{lens}:pitch={-90 + view.pitch:.2f}:yaw={view.yaw:.2f}"
                f":roll={view.roll:.2f}:h_fov=300:v_fov=300:w={w}:h={h}:interp=lanczos")
    else:
        out_fmt = "fisheye" if view.mode == "wide" else "flat"
        hf = min(170.0, view.fov * (1.5 if view.mode == "wide" else 1.0))
        vf = math.degrees(2 * math.atan(math.tan(math.radians(min(hf, 179) / 2)) * h / w))
        if view.mode == "wide":
            vf = hf * h / w
        v360 = (f"v360=input={ifmt}:output={out_fmt}{lens}:h_fov={hf:.2f}:v_fov={vf:.2f}:yaw={view.yaw:.2f}"
                f":pitch={view.pitch:.2f}:roll={view.roll:.2f}:w={w}:h={h}:interp=lanczos")
    parts.append(f"{src}{v360},setsar=1[{out}]")
    return ";".join(parts)


def preview_png(clip: Clip, view: View360, t: float, width: int = 960, timeout: float = 60) -> bytes | None:
    w, h = out_size(clip, view)
    pw = min(width, w)
    ph = int(pw * h / w) // 2 * 2
    g = graph(clip, view, pw, ph, "pv")
    args = [ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-nostdin", *inputs(clip, max(0.0, t)),
            "-filter_complex", g, "-map", "[pv]", "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"]
    try:
        proc = ff.run(args, timeout=timeout)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def convert(clip: Clip, view: View360, dst: Path, on_progress: ProgressFn, cancel: threading.Event) -> Path:
    """Render the chosen view of *clip* into a normal MP4 (*dst*)."""
    w, h = out_size(clip, view)
    ten_bit = "10" in (clip.v.pix_fmt or "")
    g = graph(clip, view, w, h, "o")
    tmp = dst.with_name(f".{dst.stem}.part{dst.suffix}")
    tmp.unlink(missing_ok=True)
    args = [*inputs(clip), "-filter_complex", g, "-map", "[o]"]
    track = next((i for i, a in enumerate(clip.audio) if a.codec != "apple_apac"), None)
    if track is not None:
        args += ["-map", f"0:a:{track}"]
        args += ["-c:a", "copy"] if clip.audio[track].codec == "aac" else ["-c:a", "aac", "-b:a", "256k"]
    if ten_bit:
        args += ["-c:v", "libx265", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
                 "-x265-params", "log-level=error"]
    else:
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p"]
    if clip.creation_time_tag:
        args += ["-metadata", f"creation_time={clip.creation_time_tag}"]
    args += ["-movflags", "+faststart", "-f", "mp4", "-n", str(tmp)]
    try:
        run_ffmpeg(args, clip.duration, lambda f, s: on_progress(f, s, f"Converting 360° video {clip.name}"),
                   cancel)
        tmp.replace(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if not dst.exists():
        raise MergeError("The 360° conversion produced no file.")
    return dst


def output_path(clip: Clip, view: View360) -> Path:
    """Next to the original if that folder is writable, else in the app's data folder."""
    from .appdirs import sub
    tag = {"flat": "view", "wide": "wide", "planet": "planet"}[view.mode]
    name = f"{clip.path.stem}_{tag}.mp4"
    for folder in (clip.path.parent, sub("converted")):
        try:
            probe_file = folder / f".lvm_write_test_{threading.get_ident()}"
            probe_file.write_bytes(b"")
            probe_file.unlink()
        except OSError:
            continue
        p = folder / name
        k = 2
        while p.exists():
            p = folder / f"{clip.path.stem}_{tag}_{k}.mp4"
            k += 1
        return p
    return sub("converted") / name
