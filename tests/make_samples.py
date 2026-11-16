"""Generate small synthetic test recordings with FFmpeg.

Usage:  python tests/make_samples.py <output-folder>

Creates 4K 60 fps H.264/AAC clips with Unicode names (Georgian, Russian,
apostrophes, spaces) and creation_time metadata that does NOT match the
alphabetical order, plus an "odd" folder with an incompatible 1080p30 clip.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lossless_merger import ffmpeg_tools as ff  # noqa: E402

# (file name, creation_time, test pattern, tone Hz)
CLIPS_4K = [  # same "camera" settings -> identical codec headers
    ("ვიდეო ჩანაწერი 1.mp4", "2025-06-01T10:00:05Z", "testsrc2", 440),   # 2nd
    ("Видео запись.mp4", "2025-06-01T09:58:00Z", "testsrc2", 660),       # 1st
    ("clip 'quoted' & spaced.mp4", "2025-06-01T10:03:30Z", "testsrc2", 880),  # 3rd
]


def make(out: Path, name, ctime, pattern, hz, size="3840x2160", rate="60", seconds=3,
         extra=()):
    cmd = [ff.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"{pattern}=size={size}:rate={rate}:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency={hz}:sample_rate=48000:duration={seconds}",
           # veryfast uses B-frames like real cameras (decode delay != 0)
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", str(int(float(rate)) or 30),
           "-c:a", "aac", "-b:a", "192k", "-ac", "2",
           *extra]
    if ctime:
        cmd += ["-metadata", f"creation_time={ctime}"]
    cmd += [str(out / name)]
    subprocess.run(cmd, check=True)


def main(root: Path):
    good = root / "4k60"
    odd = root / "mixed"
    good.mkdir(parents=True, exist_ok=True)
    odd.mkdir(parents=True, exist_ok=True)
    for name, ctime, pat, hz in CLIPS_4K:
        make(good, name, ctime, pat, hz)
    # Mixed folder: two compatible 1080p60 clips + one 1080p30 mono clip (incompatible)
    make(odd, "a_first.mp4", "2025-01-01T08:00:00Z", "testsrc2", 300, size="1920x1080")
    make(odd, "b_second.mp4", "2025-01-01T08:05:00Z", "smptebars", 500, size="1920x1080")
    make(odd, "c_phone_vertical.mp4", "2025-01-01T08:10:00Z", "testsrc", 700,
         size="1080x1920", rate="30", extra=["-ac", "1"])
    # Same format but different encoder headers (smptebars signals BT.601 colour)
    dev = root / "devices"
    dev.mkdir(exist_ok=True)
    make(dev, "cam_a.mp4", "2025-02-01T08:00:00Z", "testsrc2", 300, size="1920x1080")
    make(dev, "cam_b.mp4", "2025-02-01T08:01:00Z", "smptebars", 500, size="1920x1080")
    # File-name timestamp (OBS style), no metadata
    obs = root / "obs"
    obs.mkdir(exist_ok=True)
    make(obs, "2025-03-02 21-15-00.mkv", None, "testsrc2", 400, size="1280x720")
    make(obs, "2025-03-02 20-00-00.mkv", None, "testsrc2", 600, size="1280x720")
    # H.264 with B-frames at 60 / 30 / 60 fps, same resolution
    h264 = root / "h264fps"
    h264.mkdir(exist_ok=True)
    make(h264, "s1.mp4", "2025-04-01T10:00:00Z", "testsrc2", 300, size="1280x720", rate="60")
    make(h264, "s2.mp4", "2025-04-01T10:01:00Z", "testsrc2", 400, size="1280x720", rate="30")
    make(h264, "s3.mp4", "2025-04-01T10:02:00Z", "testsrc2", 500, size="1280x720", rate="60")
    make_iphone(root / "iphone")
    # Front-camera case: landscape clips stored upright + one landscape clip stored
    # sideways (2160x3840-style) with a rotation flag. Two audio tracks each.
    sf = root / "selfie"
    make_hevc(sf, "IMG_0201.MOV", "2025-09-01T10:00:00Z", 60, "1280x720")
    make_hevc(sf, "IMG_0202.MOV", "2025-09-01T10:01:00Z", 60, "1280x720", stored_sideways=True)
    make_hevc(sf, "IMG_0203.MOV", "2025-09-01T10:02:00Z", 60, "1280x720")
    # Reverse case: main clips carry the rotation flag, odd clip is stored upright.
    rm = root / "rotmix"
    make_hevc(rm, "R1.MOV", "2025-09-02T10:00:00Z", 30, "1280x720", rotation=90)
    make_hevc(rm, "R2.MOV", "2025-09-02T10:01:00Z", 30, "720x1280")
    make_hevc(rm, "R3.MOV", "2025-09-02T10:02:00Z", 30, "1280x720", rotation=90)
    print(f"Samples written to {root}")


def make_hevc(out: Path, name, ctime, fps, size, rotation=None, stored_sideways=False, seconds=2):
    """HEVC 10-bit HLG clip with AAC stereo + AAC 4.0 tracks (like iPhone AAC + spatial)."""
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / f"tmp_{name}"
    vf = ["-vf", "transpose=clock"] if stored_sideways else []
    subprocess.run([
        ff.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=48000:duration={seconds}",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", *vf,
        "-c:v", "libx265", "-preset", "fast", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
        "-x265-params", "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
        "-c:a", "aac", "-b:a", "128k", "-ac:a:0", "2", "-ac:a:1", "4",
        "-metadata", f"creation_time={ctime}", "-f", "mov", str(tmp)], check=True)
    rot = -90 if stored_sideways else rotation
    if rot:
        subprocess.run([ff.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                        "-display_rotation", str(rot), "-i", str(tmp), "-map", "0", "-c", "copy",
                        "-map_metadata", "0", str(out / name)], check=True)
        tmp.unlink()
    else:
        tmp.replace(out / name)


# iPhone-like: HEVC 10-bit HLG BT.2020 in .MOV, rotated (portrait), recorded with
# "Auto FPS" so some clips dropped to 30/24 fps -> different encoder headers.
IPHONE = [  # name, fps, creation_time, x265 params that change SPS/PPS
    ("IMG_0101.MOV", 60, "2025-08-10T18:00:00Z", ""),
    ("IMG_0102.MOV", 30, "2025-08-10T18:02:00Z", "wpp=0:signhide=0:cbqpoffs=4:crqpoffs=-3:tskip=1:ref=1"),
    ("IMG_0103.MOV", 24, "2025-08-10T18:04:00Z", "ctu=32:bframes=6:b-pyramid=1"),
    ("IMG_0104.MOV", 60, "2025-08-10T18:06:00Z", ""),
]


def make_iphone(out: Path, seconds=2):
    out.mkdir(parents=True, exist_ok=True)
    for name, fps, ctime, params in IPHONE:
        tmp = out / f"tmp_{name}"
        subprocess.run([
            ff.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={fps}:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency={300 + fps * 5}:sample_rate=48000:duration={seconds}",
            "-c:v", "libx265", "-preset", "fast", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
            "-x265-params", "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc"
                            + (":" + params if params else ""),
            "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
            "-c:a", "aac", "-b:a", "128k", "-metadata", f"creation_time={ctime}", "-f", "mov", str(tmp)],
            check=True)
        # add the portrait display-matrix flag without re-encoding
        subprocess.run([ff.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                        "-display_rotation", "90", "-i", str(tmp), "-map", "0", "-c", "copy",
                        "-map_metadata", "0", str(out / name)], check=True)
        tmp.unlink()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
