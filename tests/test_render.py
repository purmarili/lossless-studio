"""Tests of the editing renderer (smart cut, splits, effects, transitions,
presets, music) against real FFmpeg.

Run:  python -m unittest tests.test_render -v
"""

import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lossless_merger import ffmpeg_tools as ff  # noqa: E402
from lossless_merger import render  # noqa: E402
from lossless_merger.media import image_clip, probe  # noqa: E402
from lossless_merger.project import AudioItem, OverlayItem, Project, Segment, TextItem, Transform  # noqa: E402
from tests.test_engine import decoded_hashes, frame_times, psnr_frames  # noqa: E402

FF = None


def make(out: Path, vcodec: list[str], size="640x360", rate=30, seconds=8, hz=440, pix="yuv420p",
         two_audio=False, extra=()):
    a2 = ["-f", "lavfi", "-i", f"sine=frequency={hz * 2}:sample_rate=48000:duration={seconds}"] if two_audio else []
    maps = ["-map", "0:v", "-map", "1:a"] + (["-map", "2:a"] if two_audio else [])
    subprocess.run([ff.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency={hz}:sample_rate=48000:duration={seconds}",
                    *a2, *maps, *vcodec, "-pix_fmt", pix, "-c:a", "aac", "-b:a", "128k", *extra,
                    "-metadata", "creation_time=2025-05-05T10:00:00Z", str(out)], check=True)


X265 = "log-level=error:keyint=30:min-keyint=30:bframes=4"


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="lvm-render-"))
        r = cls.root
        make(r / "open.mov", ["-c:v", "libx265", "-tag:v", "hvc1", "-x265-params", X265], pix="yuv420p10le",
             two_audio=True)
        make(r / "closed.mov", ["-c:v", "libx265", "-tag:v", "hvc1", "-x265-params", X265 + ":open-gop=0"],
             pix="yuv420p10le", two_audio=True, hz=660)
        make(r / "h264.mp4", ["-c:v", "libx264", "-preset", "veryfast", "-g", "30", "-bf", "3"])
        make(r / "h264b.mp4", ["-c:v", "libx264", "-preset", "veryfast", "-g", "30", "-bf", "3"], seconds=4, hz=880)
        # HDR HLG, rotated like an iPhone portrait clip
        make(r / "hlg_tmp.mov", ["-c:v", "libx265", "-tag:v", "hvc1", "-x265-params",
                                 X265 + ":colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc"],
             pix="yuv420p10le", seconds=4, size="1280x720")
        subprocess.run([ff.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90",
                        "-i", str(r / "hlg_tmp.mov"), "-map", "0", "-c", "copy", str(r / "hlg.mov")], check=True)
        # background music (stereo, 3 s, gets looped)
        subprocess.run([ff.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=330:sample_rate=44100:duration=3", "-ac", "2",
                        "-c:a", "aac", str(r / "music.m4a")], check=True)
        cls.clips = {p.stem: probe(p) for p in r.glob("*.*") if p.suffix in (".mov", ".mp4")}
        cls.out = r / "out"
        cls.out.mkdir()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def render(self, project, name):
        out = self.out / name
        if out.exists():
            out.unlink()
        calls = []
        res = render.render_project(project, out, lambda f, s, st: calls.append((f, st)), threading.Event())
        self.assertTrue(out.exists())
        self.assertFalse([p for p in self.out.iterdir() if p.name.startswith(".")], "temp files left")
        fr = [f for f, _ in calls]
        self.assertTrue(all(0 <= f <= 1.0001 for f in fr), [c for c in calls if not 0 <= c[0] <= 1.0001][:3])
        return res, probe(out)

    # ------------------------------------------------------------------ #
    def _check_trim(self, key, ext):
        clip = self.clips[key]
        seg = Segment(clip)
        seg.edit.start, seg.edit.end = 1.37, 6.42
        idx = render.packet_index(clip.path)
        cut = render.plan_cut(idx, 1.37, 6.42, clip.v.codec)
        self.assertIsNotNone(cut.middle, "nothing could be stream-copied")
        res, m = self.render(Project([seg]), f"trim_{key}{ext}")

        src = decoded_hashes(clip.path)
        out = decoded_hashes(self.out / f"trim_{key}{ext}")
        first = render._count(idx.frames, 0, cut.a)          # index of first kept frame
        n = render._count(idx.frames, cut.a, cut.b)
        self.assertEqual(len(out), n, "frame count")
        k1, k2 = cut.middle[0], cut.middle[1]
        mid_from = render._count(idx.frames, cut.a, k1)
        mid_n = render._count(idx.frames, k1, k2)
        self.assertGreater(mid_n, 60)
        self.assertEqual(out[mid_from:mid_from + mid_n], src[first + mid_from:first + mid_from + mid_n],
                         "stream-copied frames must be bit-exact")
        # re-encoded head/tail frames are visually identical
        if cut.head:
            self.assertGreater(psnr_frames(self.out / f"trim_{key}{ext}", 0, clip.path, first), 38)
        self.assertGreater(psnr_frames(self.out / f"trim_{key}{ext}", n - 1, clip.path, first + n - 1), 38)
        self.assertAlmostEqual(m.duration, cut.b - cut.a, delta=0.08)
        t = frame_times(self.out / f"trim_{key}{ext}")
        self.assertTrue(all(b > a for a, b in zip(t, t[1:])), "timestamps must increase")
        return m

    def test_smart_trim_hevc_open_gop(self):
        m = self._check_trim("open", ".mov")
        self.assertEqual(len(m.audio), 2, "both audio tracks copied")
        self.assertEqual(m.v.codec, "hevc")

    def test_smart_trim_hevc_closed_gop(self):
        self._check_trim("closed", ".mov")

    def test_smart_trim_h264(self):
        self._check_trim("h264", ".mp4")

    def test_split_reorder_and_join(self):
        a, b = self.clips["h264"], self.clips["h264b"]
        first, second = Segment(a).split(4.0)
        p = Project([second, Segment(b), first])
        tgt = render.make_target(p)
        self.assertTrue(tgt.audio_copy and tgt.allow_copy)
        res, m = self.render(p, "split.mp4")
        self.assertAlmostEqual(m.duration, 12.0, delta=0.1)
        out = decoded_hashes(self.out / "split.mp4")
        src_a, src_b = decoded_hashes(a.path), decoded_hashes(b.path)
        self.assertEqual(len(out), len(src_a) + len(src_b))
        # untouched clip b is in the middle, bit-exact
        self.assertEqual(out[120:120 + len(src_b)], src_b)

    def test_visual_edits_speed_text_color(self):
        clip = self.clips["closed"]
        seg = Segment(clip)
        seg.edit.speed = 2.0
        seg.edit.look = "warm"
        seg.edit.contrast = 1.2
        seg.edit.rotate = 90
        plain = Segment(self.clips["open"])
        txt = TextItem("ვიდეო — Привет", start=0.0, duration=2.0, anim_in="pop", anim_out="fade")
        p = Project([seg, plain], texts=[txt])
        out = self.out / "fx.mov"
        render.render_project(p, out, lambda *a: None, threading.Event(), texts={txt.id: _text(80, 30)})
        m = probe(out)
        self.assertAlmostEqual(m.duration, 4 + 8, delta=0.15)
        # 2x speed keeps 30 fps: 8 s of source -> 4 s -> 120 frames
        self.assertEqual(len(decoded_hashes(out)), 120 + 240)
        # a speed change re-encodes the sound: one mixed AAC track
        self.assertEqual([x.codec for x in m.audio], ["aac"])

    def test_transitions_and_music(self):
        a, b, c = (Segment(self.clips[k]) for k in ("h264", "h264b", "h264"))
        a.edit.transition, a.edit.transition_dur = "fade", 1.0
        b.edit.transition, b.edit.transition_dur = "slideleft", 0.5
        music = AudioItem(self.root / "music.m4a", 3.0, volume=0.5)
        p = Project([a, b, c], audios=[music], fade_in=True, fade_out=True)
        self.assertAlmostEqual(p.duration, 8 + 4 + 8 - 1.5, places=3)
        res, m = self.render(p, "trans.mp4")
        self.assertAlmostEqual(m.duration, p.duration, delta=0.1)
        decoded_hashes(self.out / "trans.mp4")
        self.assertAlmostEqual(_adur(self.out / "trans.mp4"), p.duration, delta=0.1)

    def test_caption_over_untouched_clip_keeps_rest_copied(self):
        clip = self.clips["closed"]
        txt = TextItem("Hi", start=3.0, duration=1.0, anim_in="slide_up", anim_out="slide_down")
        p = Project([Segment(clip)], texts=[txt])
        out = self.out / "caption.mov"
        res = render.render_project(p, out, lambda *a: None, threading.Event(), texts={txt.id: _text(60, 24)})
        src, o = decoded_hashes(clip.path), decoded_hashes(out)
        self.assertEqual(len(o), len(src))
        same = sum(1 for x, y in zip(src, o) if x == y)
        # only the GOPs around the caption (3-4 s) are re-encoded
        self.assertGreater(same, 240 - 75, f"{same} of 240 frames bit-exact")
        self.assertNotEqual(o[105], src[105], "caption must be burned in")
        self.assertEqual(len(probe(out).audio), 2, "audio still copied")

    def test_photos_freeze_reverse_kenburns_effects(self):
        photo = self.root / "photo.png"
        if not photo.exists():
            ff.run([ff.ffmpeg_path(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=800x1000", "-frames:v", "1",
                    str(photo)])
        pc = image_clip(photo, 800, 1000)
        ph = Segment(pc, kind="image", still_len=2.0)
        ph.edit.tf = Transform(1.0)
        ph.edit.tf_end = Transform(1.4, 0.3, -0.2)
        v = Segment(self.clips["h264"])
        v.edit.reverse = True
        v.edit.end = 3.0
        v.edit.vignette, v.edit.sharpen = 0.6, 0.5
        fr = Segment(self.clips["closed"], kind="freeze", still_at=2.0, still_len=1.5)
        fr.edit.blur = 0.4
        p = Project([ph, v, fr], canvas="9:16", background="blur")
        res, m = self.render(p, "mix_media.mp4")
        self.assertAlmostEqual(m.duration, 2 + 3 + 1.5, delta=0.12)
        self.assertEqual(m.v.display_size, (202, 360))
        decoded_hashes(self.out / "mix_media.mp4")
        # reversed: first frame of the reversed clip == last frame of the source range
        self.assertGreater(psnr_frames(self.out / "mix_media.mp4", 60, self.out / "mix_media.mp4", 60), 40)

    def test_pip_overlay_with_audio_and_ducking(self):
        base = Segment(self.clips["h264"])
        pip = OverlayItem(self.clips["h264b"], start=1.0, duration=3.0, src_in=0.5, scale=0.4, opacity=0.9)
        music = AudioItem(self.root / "music.m4a", 3.0, volume=0.7, duck=True)
        voice = AudioItem(self.clips["h264b"].path, 4.0, kind="voice", start=5.0, volume=1.0)
        p = Project([base], overlays=[pip], audios=[music, voice])
        res, m = self.render(p, "pip.mp4")
        self.assertAlmostEqual(m.duration, 8.0, delta=0.1)
        self.assertAlmostEqual(_adur(self.out / "pip.mp4"), 8.0, delta=0.1)
        o, src = decoded_hashes(self.out / "pip.mp4"), decoded_hashes(self.clips["h264"].path)
        self.assertNotEqual(o[60], src[60], "picture-in-picture visible at 2 s")

    def test_stabilize(self):
        seg = Segment(self.clips["h264b"])
        seg.edit.stabilize = True
        seg.edit.end = 2.0
        res, m = self.render(Project([seg]), "stab.mp4")
        self.assertAlmostEqual(m.duration, 2.0, delta=0.1)
        decoded_hashes(self.out / "stab.mp4")

    def test_export_1080p_sdr_from_hdr(self):
        p = Project([Segment(self.clips["hlg"]), Segment(self.clips["hlg"])], preset="1080p")
        res, m = self.render(p, "sdr.mp4")
        v = m.v
        self.assertEqual((v.codec, v.pix_fmt, v.color_transfer), ("h264", "yuv420p", "bt709"))
        self.assertEqual(v.display_size, (720, 1280))      # portrait, upright, not upscaled
        self.assertEqual(v.rotation, 0)
        self.assertFalse(res.lossless)

    def test_canvas_vertical(self):
        p = Project([Segment(self.clips["h264"])], canvas="9:16")
        tgt = render.make_target(p)
        self.assertEqual((tgt.width, tgt.height), (202, 360))
        self.assertFalse(tgt.allow_copy)
        res, m = self.render(p, "vertical.mp4")
        self.assertEqual(m.v.display_size, (202, 360))

    def test_hdr_keeps_rotation_when_copying(self):
        clip = self.clips["hlg"]
        seg = Segment(clip)
        seg.edit.start = 0.9
        res, m = self.render(Project([seg]), "hlg_trim.mov")
        self.assertEqual(m.v.rotation, clip.v.rotation)
        self.assertEqual(m.v.color_transfer, "arib-std-b67")
        n = len(decoded_hashes(self.out / "hlg_trim.mov"))
        self.assertEqual(n, 120 - 27)

    def test_mixed_formats_with_transition(self):
        a, b = Segment(self.clips["h264"]), Segment(self.clips["closed"])
        a.edit.end = 5.0
        a.edit.transition, a.edit.transition_dur = "dissolve", 0.8
        b.edit.start = 2.0
        p = Project([a, b])
        res, m = self.render(p, "mixed_trans.mov")
        self.assertAlmostEqual(m.duration, 5 + 6 - 0.8, delta=0.1)
        decoded_hashes(self.out / "mixed_trans.mov")
        t = frame_times(self.out / "mixed_trans.mov")
        self.assertTrue(all(y > x for x, y in zip(t, t[1:])))

    def test_preview_frame(self):
        seg = Segment(self.clips["hlg"])
        seg.edit.look = "bw"
        png = render.preview_frame(seg, Project([seg]), 1.0, 320)
        self.assertTrue(png and png.startswith(b"\x89PNG"))


def _png(w, h) -> bytes:
    """A caption-like RGBA PNG (made by FFmpeg, so the tests need no Qt)."""
    return ff.run([ff.ffmpeg_path(), "-v", "error", "-f", "lavfi", "-i", f"color=c=red@0.7:s={w}x{h}",
                   "-frames:v", "1", "-pix_fmt", "rgba", "-f", "image2pipe", "-c:v", "png", "pipe:1"]).stdout


def _text(w, h) -> "render.TextRender":
    return render.TextRender(_png(w, h), 20, 30, w, h)


def _adur(path) -> float:
    return float(ff.decode(ff.run([ff.ffprobe_path(), "-v", "error", "-select_streams", "a:0",
                                   "-show_entries", "stream=duration", "-of", "csv=p=0",
                                   str(path)]).stdout).strip())


if __name__ == "__main__":
    unittest.main()
