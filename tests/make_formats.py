"""Synthetic footage that mimics many cameras and apps (short, small).

Usage:  python tests/make_formats.py <output-folder>

Each sub-folder holds what one device typically records, with its file
naming, codecs, containers and quirks (rotation flags, interlacing, extra
data / timecode / subtitle tracks, sidecar proxy files, dual-lens 360 video).
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lossless_merger import ffmpeg_tools as ff  # noqa: E402

SECONDS = 3


def run(args):
    subprocess.run([ff.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def src(size="640x360", rate=30, seconds=SECONDS, pattern="testsrc2"):
    return ["-f", "lavfi", "-i", f"{pattern}=size={size}:rate={rate}:duration={seconds}"]


def tone(hz=440, rate=48000, seconds=SECONDS, layout="stereo"):
    return ["-f", "lavfi", "-i", f"sine=frequency={hz}:sample_rate={rate}:duration={seconds},"
                                 f"aformat=channel_layouts={layout}"]


def meta(ctime):
    return ["-metadata", f"creation_time={ctime}"]


def rotate(path: Path, degrees: int):
    tmp = path.with_name("tmp_" + path.name)
    path.replace(tmp)
    run(["-display_rotation", str(degrees), "-i", str(tmp), "-map", "0", "-c", "copy", "-map_metadata", "0",
         str(path)])
    tmp.unlink()


def android(root: Path):
    d = root / "android"
    d.mkdir(parents=True, exist_ok=True)
    # Samsung: HEVC 10-bit HDR10 (PQ), portrait, file name = date
    for name, hz, ct in (("20251001_101500.mp4", 300, "2025-10-01T07:15:00Z"),
                         ("20251001_101700.mp4", 400, "2025-10-01T07:17:00Z")):
        run([*src(), *tone(hz), "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
             "-x265-params", "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
                             "keyint=30", "-c:a", "aac", *meta(ct), str(d / name)])
        rotate(d / name, 90)
    # Pixel: H.264, upside-down landscape (rotation 180)
    run([*src(), *tone(500), "-c:v", "libx264", "-preset", "veryfast", "-g", "30", "-c:a", "aac",
         *meta("2025-10-01T07:20:00Z"), str(d / "PXL_20251001_102000123.mp4")])
    rotate(d / "PXL_20251001_102000123.mp4", 180)


def gopro(root: Path):
    d = root / "gopro"
    d.mkdir(parents=True, exist_ok=True)
    # One long recording split into chapters (same creation time!) + the next recording.
    for name, hz, ct in (("GX020042.MP4", 520, "2025-10-02T09:00:00Z"),
                         ("GX010042.MP4", 510, "2025-10-02T09:00:00Z"),
                         ("GX010043.MP4", 530, "2025-10-02T09:10:00Z")):
        # HEVC + AAC + timecode track + a subtitle track standing in for GPMF telemetry
        srt = d / "tmp.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nGPS\n", encoding="utf-8")
        run([*src(), *tone(hz), "-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s",
             "-c:v", "libx265", "-tag:v", "hvc1", "-x265-params", "log-level=error:keyint=30", "-c:a", "aac",
             "-c:s", "mov_text", "-timecode", "09:00:00:00", *meta(ct), str(d / name)])
        srt.unlink()
    # sidecar files a GoPro writes next to the video: must be ignored
    (d / "GL010042.LRV").write_bytes(b"\0" * 64)
    (d / "GX010042.THM").write_bytes(b"\0" * 64)


def insta360(root: Path):
    d = root / "insta360"
    d.mkdir(parents=True, exist_ok=True)
    # Ace Pro / GO: a normal flat video
    run([*src(), *tone(600), "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
         *meta("2025-10-03T10:00:00Z"), str(d / "VID_20251003_130000_00_001.mp4")])
    # X-series: raw dual-fisheye 360 — two lens streams in one .insv file
    run([*src("720x720", pattern="testsrc2"), *src("720x720", pattern="smptebars"), *tone(650),
         "-map", "0:v", "-map", "1:v", "-map", "2:a", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
         *meta("2025-10-03T10:05:00Z"), "-f", "mp4", str(d / "VID_20251003_130500_00_002.insv")])
    (d / "LRV_20251003_130500_01_002.lrv").write_bytes(b"\0" * 64)
    # ONE X style: one file per lens (_00_ front, _10_ back)
    for lens, pattern in (("00", "testsrc2"), ("10", "smptebars")):
        run([*src("640x640", pattern=pattern), *tone(660), "-c:v", "libx264", "-preset", "veryfast",
             "-c:a", "aac", *meta("2025-10-03T10:10:00Z"), "-f", "mp4",
             str(d / f"VID_20251003_131000_{lens}_003.insv")])
    # GoPro MAX: a .360 file (two streams) — has to be exported with GoPro Player
    g = root / "gopro"
    run([*src("512x256"), *src("512x256", pattern="smptebars"), *tone(540), "-map", "0:v", "-map", "1:v",
         "-map", "2:a", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-f", "mp4",
         str(g / "GS010050.360")])


def camcorder(root: Path):
    # AVCHD camcorder (Sony / Panasonic / Canon): 1080i H.264 + AC-3 in M2TS
    d = root / "camcorder" / "PRIVATE" / "AVCHD" / "BDMV" / "STREAM"
    d.mkdir(parents=True, exist_ok=True)
    for name, hz, ct in (("00001.MTS", 300, "2025-10-04T08:00:00Z"), ("00002.MTS", 350, "2025-10-04T08:05:00Z")):
        run([*src("1920x1080", 25), *tone(hz), "-c:v", "libx264", "-preset", "ultrafast", "-flags", "+ilme+ildct",
             "-x264-params", "tff=1", "-g", "25", "-c:a", "ac3", "-b:a", "192k", *meta(ct),
             "-f", "mpegts", "-mpegts_m2ts_mode", "1", str(d / name)])


def xavc(root: Path):
    # Sony mirrorless (XAVC S-I style): H.264 4:2:2 10-bit + LPCM audio
    d = root / "sony_xavc"
    d.mkdir(parents=True, exist_ok=True)
    for name, hz, ct in (("C0001.MP4", 300, "2025-10-05T08:00:00Z"), ("C0002.MP4", 320, "2025-10-05T08:01:00Z")):
        run([*src("1280x720", 25), *tone(hz), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv422p10le",
             "-g", "25", "-c:a", "pcm_s16be", *meta(ct), "-f", "mov", str(d / name)])


def misc(root: Path):
    d = root / "misc"
    d.mkdir(parents=True, exist_ok=True)
    # webcam: MJPEG + PCM in AVI
    run([*src("640x480", 30), *tone(700), "-c:v", "mjpeg", "-pix_fmt", "yuvj422p", "-q:v", "4",
         "-c:a", "pcm_s16le", str(d / "webcam.avi")])
    # screen recording: VP9 + Opus in WebM
    run([*src("1280x720", 30), *tone(710), "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
         "-b:v", "1M", "-c:a", "libopus", str(d / "screen.webm")])
    # WhatsApp: small H.264 baseline + mono 44.1 kHz AAC
    run([*src("480x848", 30), *tone(720, 44100, layout="mono"), "-c:v", "libx264", "-profile:v", "baseline",
         "-preset", "veryfast", "-c:a", "aac", "-b:a", "64k", str(d / "VID-20251006-WA0001.mp4")])
    # old phone: H.263 + AMR-NB in 3GP
    run([*src("176x144", 15), *tone(730, 8000, layout="mono"), "-c:v", "h263", "-c:a", "libopencore_amrnb",
         "-ar", "8000", "-ac", "1", "-b:a", "12.2k", str(d / "VID00001.3gp")])
    # editing camera / Mac export: ProRes 422 + 24-bit PCM
    run([*src("1280x720", 25), *tone(740), "-c:v", "prores_ks", "-profile:v", "2", "-c:a", "pcm_s24le",
         str(d / "A001_C001.mov")])
    # DVD: MPEG-2 PAL interlaced + MP2
    run([*src("720x576", 25), *tone(750), "-c:v", "mpeg2video", "-flags", "+ilme+ildct", "-b:v", "4M",
         "-c:a", "mp2", "-f", "dvd", str(d / "VTS_01_1.VOB")])
    # AV1 + Opus in MP4 (newer phones / downloads)
    run([*src("640x360", 30), *tone(760), "-c:v", "libsvtav1", "-preset", "12", "-c:a", "libopus",
         str(d / "clip_av1.mp4")])
    # Windows Media
    run([*src("640x360", 30), *tone(770), "-c:v", "wmv2", "-b:v", "1M", "-c:a", "wmav2", str(d / "old.wmv")])
    # odd size, full range, 4:4:4 (screen capture tools)
    run([*src("641x359", 30), *tone(780), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv444p",
         "-c:a", "aac", str(d / "capture_444.mkv")])


def main(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    for fn in (android, gopro, insta360, camcorder, xavc, misc):   # gopro before insta360 (adds .360)
        fn(root)
    print(f"Samples written to {root}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
