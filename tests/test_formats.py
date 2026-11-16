"""Footage from many cameras and apps: probe, lossless merge, trim, export.

Run:  python -m unittest tests.test_formats -v
Samples come from tests/make_formats.py (Android, GoPro, Insta360, AVCHD
camcorder, Sony XAVC, webcam AVI, VP9/Opus WebM, WhatsApp, 3GP/AMR, ProRes,
DVD VOB, AV1, WMV, 4:4:4 capture).
"""

import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lossless_merger import compat, ffmpeg_tools as ff, merger, render  # noqa: E402
from lossless_merger.media import probe, scan_folder, sort_chronologically  # noqa: E402
from lossless_merger.project import Project, Segment  # noqa: E402
from tests import make_formats  # noqa: E402
from tests.test_engine import decoded_hashes  # noqa: E402


def clean_decode(path: Path) -> str:
    """Decoder errors of a file ('' = clean). passthrough: mixed-rate videos are
    variable frame rate on purpose; don't let FFmpeg squeeze them onto a fixed
    clock (that reports false 'non monotonic' errors)."""
    r = ff.run([ff.ffmpeg_path(), "-v", "error", "-xerror", "-i", str(path), "-fps_mode", "passthrough",
                "-enc_time_base", "demux", "-f", "null", "-"])
    return ff.decode(r.stderr).strip()


class FormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="lvm-formats-"))
        make_formats.main(cls.root)
        cls.out = cls.root / "_out"
        cls.out.mkdir()
        cls.ev = threading.Event()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def all_inputs(self):
        return [f for f in scan_folder(self.root, recursive=True) if "_out" not in f.parts]

    def folder(self, name):
        clips = [probe(f) for f in scan_folder(self.root / name, recursive=True)]
        return sort_chronologically([c for c in clips if not c.error])

    def test_every_file_is_read(self):
        files = self.all_inputs()
        names = {f.name for f in files}
        self.assertNotIn("GL010042.LRV", names)            # GoPro proxy
        self.assertNotIn("GX010042.THM", names)            # GoPro thumbnail
        self.assertFalse(any(n.lower().endswith(".lrv") for n in names))   # Insta360 proxy
        self.assertNotIn("VID_20251003_131000_10_003.insv", names)        # 2nd lens of a pair
        for f in files:
            c = probe(f)
            if f.suffix == ".360":
                self.assertIn("GoPro Player", c.error or "")
                continue
            self.assertIsNone(c.error, f"{f.name}: {c.error}")
            self.assertTrue(c.v and c.duration > 2.5, f.name)

    def test_360_detection_and_conversion(self):
        from lossless_merger import reframe
        two = probe(self.root / "insta360" / "VID_20251003_130500_00_002.insv")
        pair = probe(self.root / "insta360" / "VID_20251003_131000_00_003.insv")
        flat = probe(self.root / "insta360" / "VID_20251003_130000_00_001.mp4")
        self.assertEqual(two.projection, "dfisheye2")
        self.assertEqual(pair.projection, "dfisheye2")
        self.assertEqual(pair.lens_pair.name, "VID_20251003_131000_10_003.insv")
        self.assertEqual(flat.projection, "")
        for clip in (two, pair):
            for mode in reframe.MODES:
                with self.subTest(clip=clip.name, mode=mode):
                    view = reframe.View360(mode=mode, yaw=40, pitch=-10, swap=mode == "wide")
                    self.assertTrue((reframe.preview_png(clip, view, 1.0, 320) or b"").startswith(b"\x89PNG"))
                    out = self.out / f"{clip.path.stem}_{mode}.mp4"
                    reframe.convert(clip, view, out, lambda *a: None, self.ev)
                    m = probe(out)
                    self.assertEqual((m.v.width, m.v.height), reframe.out_size(clip, view))
                    self.assertAlmostEqual(m.duration, clip.duration, delta=0.15)
                    self.assertEqual(m.projection, "")          # a normal video now
                    self.assertTrue(m.audio)

    def test_gopro_chapters_in_order_and_lossless(self):
        clips = self.folder("gopro")
        self.assertEqual([c.name for c in clips], ["GX010042.MP4", "GX020042.MP4", "GX010043.MP4"])
        self.assertTrue(compat.analyze(clips).lossless_possible)
        out = self.out / "gopro.mp4"
        res = merger.merge_lossless(clips, out, lambda *a: None, self.ev)
        self.assertAlmostEqual(res.duration, 9.0, delta=0.15)
        self.assertEqual(clean_decode(out), "")

    def test_camcorder_avchd_lossless(self):
        clips = self.folder("camcorder")
        self.assertEqual(len(clips), 2)
        self.assertEqual(clips[0].v.field_order, "tt")
        out = self.out / f"avchd{compat.suggested_extension(clips)}"
        res = merger.merge_lossless(clips, out, lambda *a: None, self.ev)
        self.assertAlmostEqual(res.duration, 6.0, delta=0.15)
        self.assertEqual(clean_decode(out), "")

    def test_sony_xavc_422_pcm(self):
        clips = self.folder("sony_xavc")
        self.assertEqual(compat.suggested_extension(clips), ".mov")    # PCM: QuickTime, not MKV
        out = self.out / "xavc.mov"
        merger.merge_lossless(clips, out, lambda *a: None, self.ev)
        m = probe(out)
        self.assertEqual((m.v.pix_fmt, m.a.codec), ("yuv422p10le", "pcm_s16be"))

    def test_time_base_mismatch_merge(self):
        """Same codec and size, different time bases (different devices):
        FFmpeg's concat would wreck the timing without the rewrap."""
        a, b = self.out / "tb600.mp4", self.out / "tb90k.mp4"
        for p, ts in ((a, "600"), (b, "90000")):
            subprocess.run([ff.ffmpeg_path(), "-v", "error", "-y", *make_formats.src(), *make_formats.tone(),
                            "-c:v", "libx264", "-preset", "veryfast", "-g", "30", "-c:a", "aac",
                            "-video_track_timescale", ts, str(p)], check=True)
        clips = [probe(a), probe(b)]
        out = self.out / "tb_merged.mp4"
        res = merger.merge_lossless(clips, out, lambda *x: None, self.ev)
        self.assertAlmostEqual(res.duration, 6.0, delta=0.1)
        self.assertEqual(decoded_hashes(out), decoded_hashes(a) + decoded_hashes(b))

    def test_trim_every_format(self):
        for f in self.all_inputs():
            c = probe(f)
            if c.error or c.is_360:
                continue
            with self.subTest(file=f.name):
                seg = Segment(c)
                seg.edit.start, seg.edit.end = 0.55, 2.45
                p = Project([seg])
                tgt = render.make_target(p)
                out = self.out / f"trim_{f.stem}{tgt.ext}"
                res = render.render_project(p, out, lambda *a: None, self.ev, {}, tgt)
                self.assertAlmostEqual(res.duration, 1.9, delta=0.12)
                self.assertTrue(probe(out).audio, "sound must be kept")
                self.assertEqual(clean_decode(out), "")

    def test_everything_in_one_video(self):
        clips = [probe(f) for f in self.all_inputs()]
        clips = [c for c in clips if not c.error and not c.is_360]   # 360 raw files are converted first
        for preset in ("original", "1080p"):
            with self.subTest(preset=preset):
                p = Project([Segment(c) for c in clips], preset=preset)
                tgt = render.make_target(p)
                out = self.out / f"all_{preset}{tgt.ext}"
                res = render.render_project(p, out, lambda *a: None, self.ev, {}, tgt)
                self.assertAlmostEqual(res.duration, p.duration, delta=0.2)
                err = clean_decode(out)
                self.assertEqual(err, "", err[:600])


if __name__ == "__main__":
    unittest.main()

