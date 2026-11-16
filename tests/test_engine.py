"""End-to-end tests of the merge engine against real FFmpeg.

Run:  python -m unittest tests.test_engine -v
Samples are generated automatically into a temporary folder.
"""

import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lossless_merger import ffmpeg_tools as ff  # noqa: E402
from lossless_merger.compat import analyze, container_problem, encode_target, suggested_extension  # noqa: E402
from lossless_merger.media import probe, scan_folder, sort_chronologically, thumbnail_png  # noqa: E402
from lossless_merger import compat  # noqa: E402
from lossless_merger.merger import (BLEND, DUPLICATE, Cancelled, merge_conform, merge_fit,  # noqa: E402
                                    merge_lossless, merge_reencode, needs_header_embedding)
from tests import make_samples  # noqa: E402


def packet_hashes(path: Path, selector: str) -> list[tuple[str, str]]:
    """(size, md5) of every compressed packet, read with stream copy (no decoding)."""
    out = ff.run([ff.ffmpeg_path(), "-v", "error", "-i", str(path), "-map", selector,
                  "-c", "copy", "-f", "framemd5", "-"]).stdout
    rows = []
    for line in ff.decode(out).splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        rows.append((parts[4], parts[5]))
    return rows


def decoded_hashes(path: Path) -> list[str]:
    """md5 of every decoded video frame; fails on any decoder error."""
    # passthrough: never let FFmpeg drop/duplicate frames to force a constant rate
    proc = ff.run([ff.ffmpeg_path(), "-v", "error", "-xerror", "-noautorotate", "-i", str(path),
                   "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "framemd5", "-"])
    assert proc.returncode == 0 and not proc.stderr.strip(), ff.decode(proc.stderr)
    return [line.rsplit(",", 1)[1].strip() for line in ff.decode(proc.stdout).splitlines()
            if line and not line.startswith("#")]


def psnr_frames(a: Path, na: int, b: Path, nb: int) -> float:
    """PSNR between displayed frame #na of *a* and frame #nb of *b* (exact frames)."""
    lavfi = (f"[0:v]select=eq(n\\,{na}),setpts=N[x];[1:v]select=eq(n\\,{nb}),setpts=N[y];"
             f"[x][y]psnr")
    proc = ff.run([ff.ffmpeg_path(), "-hide_banner", "-i", str(a), "-i", str(b),
                   "-lavfi", lavfi, "-frames:v", "1", "-f", "null", "-"])
    import re
    m = re.search(r"average:([\d.]+|inf)", ff.decode(proc.stderr))
    assert m, ff.decode(proc.stderr)[-500:]
    return float(m.group(1))


def frame_times(path: Path) -> list[float]:
    out = ff.run([ff.ffprobe_path(), "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path)]).stdout
    return [float(x.strip(",")) for x in ff.decode(out).split() if x.strip(",")]


def load(folder: Path):
    return sort_chronologically([probe(p) for p in scan_folder(folder)])


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="lvm-test-"))
        make_samples.main(cls.root)
        cls.out = cls.root / "out"
        cls.out.mkdir()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def progress(self):
        calls = []
        return calls, lambda f, s, status: calls.append((f, s, status))

    # ------------------------------------------------------------------ #
    def test_scan_probe_and_chronological_order(self):
        clips = load(self.root / "4k60")
        self.assertEqual([c.name for c in clips],
                         ["Видео запись.mp4", "ვიდეო ჩანაწერი 1.mp4", "clip 'quoted' & spaced.mp4"])
        for c in clips:
            self.assertIsNone(c.error)
            self.assertEqual(c.date_source, "metadata")
            self.assertEqual((c.v.width, c.v.height), (3840, 2160))
            self.assertAlmostEqual(c.v.fps, 60, places=2)
            self.assertAlmostEqual(c.duration, 3.0, delta=0.1)

    def test_filename_timestamp_fallback(self):
        clips = load(self.root / "obs")
        self.assertEqual([c.name for c in clips],
                         ["2025-03-02 20-00-00.mkv", "2025-03-02 21-15-00.mkv"])
        self.assertTrue(all(c.date_source == "file name" for c in clips))
        self.assertEqual(suggested_extension(clips), ".mkv")

    def test_thumbnail_unicode(self):
        clip = load(self.root / "4k60")[1]
        png = thumbnail_png(clip.path, clip.duration)
        self.assertTrue(png and png.startswith(b"\x89PNG"))

    def test_compatible_clips_are_lossless(self):
        report = analyze(load(self.root / "4k60"))
        self.assertTrue(report.lossless_possible)
        self.assertEqual(report.issues, [])

    def test_incompatible_clips_are_reported(self):
        clips = load(self.root / "mixed")
        report = analyze(clips)
        self.assertFalse(report.lossless_possible)
        titles = {i.title for i in report.blockers}
        self.assertIn("Different resolutions", titles)
        self.assertIn("Different audio channel layouts", titles)
        self.assertIn("Different frame rates", {i.title for i in report.warnings})
        self.assertEqual(report.clip_severity(clips[2].id), "blocker")
        self.assertIsNone(report.clip_severity(clips[0].id))

    def test_container_check(self):
        clip = load(self.root / "4k60")[0]
        self.assertIsNone(container_problem(clip, ".mp4"))
        self.assertIsNotNone(container_problem(clip, ".webm"))

    # ------------------------------------------------------------------ #
    def test_lossless_merge_is_bit_exact(self):
        clips = load(self.root / "4k60")
        out = self.out / "შედეგი — итог.mp4"
        calls, cb = self.progress()
        result = merge_lossless(clips, out, cb, threading.Event())

        self.assertTrue(out.exists())
        self.assertTrue(result.lossless)
        self.assertEqual(result.notes, [])
        self.assertTrue(calls and calls[-1][0] == 1.0)
        merged = probe(out)
        self.assertAlmostEqual(merged.duration, 9.0, delta=0.15)
        self.assertEqual((merged.v.codec, merged.v.width, merged.v.height), ("h264", 3840, 2160))
        self.assertAlmostEqual(merged.v.fps, 60, places=2)
        self.assertEqual((merged.a.codec, merged.a.sample_rate, merged.a.channels), ("aac", 48000, 2))
        self.assertEqual(merged.creation_time_tag, clips[0].creation_time_tag)

        self.assert_lossless(clips, out)
        # No temp files left behind
        self.assertEqual([p.name for p in self.out.iterdir() if p.name.startswith(".")], [])
        out.unlink()

    def test_lossless_merge_mkv(self):
        clips = load(self.root / "obs")
        out = self.out / "merged.mkv"
        merge_lossless(clips, out, lambda *a: None, threading.Event())
        merged = probe(out)
        self.assertAlmostEqual(merged.duration, 6.0, delta=0.15)
        self.assert_lossless(clips, out)
        out.unlink()

    def assert_lossless(self, clips, out):
        # Video: same number of compressed packets (nothing re-encoded or dropped)
        # and every decoded frame pixel-identical. Keyframe packets may carry the
        # SPS/PPS headers in-band (FFmpeg concat auto_convert), which is harmless.
        expected_pkts = sum(len(packet_hashes(c.path, "0:v:0")) for c in clips)
        self.assertEqual(len(packet_hashes(out, "0:v:0")), expected_pkts)
        expected = [h for c in clips for h in decoded_hashes(c.path)]
        self.assertEqual(decoded_hashes(out), expected, "decoded video differs")
        # Audio: every packet byte-identical, in order.
        expected_audio = [h for c in clips for h in packet_hashes(c.path, "0:a:0")]
        self.assertEqual(packet_hashes(out, "0:a:0"), expected_audio, "audio differs")
        self.assert_monotonic(out)

    def assert_monotonic(self, out):
        """Presentation times strictly increase (no overlapping/stacked frames at joins)."""
        ts = frame_times(out)
        bad = [(i, ts[i - 1], ts[i]) for i in range(1, len(ts)) if ts[i] <= ts[i - 1]]
        self.assertEqual(bad[:3], [], "frame timestamps go backwards")
        steps = [ts[i] - ts[i - 1] for i in range(1, len(ts))]
        self.assertLess(max(steps), 0.1, "unexpected gap between frames")

    def test_different_encoder_headers_decode_identically(self):
        clips = load(self.root / "devices")
        report = analyze(clips)
        self.assertTrue(report.lossless_possible)
        self.assertEqual([i.title for i in report.warnings], ["Different encoder settings"])
        out = self.out / "devices.mp4"
        merge_lossless(clips, out, lambda *a: None, threading.Event())
        # Decode everything: no decoder errors and every frame pixel-identical.
        expected = [h for c in clips for h in decoded_hashes(c.path)]
        got = decoded_hashes(out)
        self.assertEqual(len(got), len(expected))
        self.assertEqual(got, expected)
        out.unlink()

    # ---------------- iPhone "Auto FPS" footage: HEVC 10-bit HLG, 60/30/24 fps
    def test_iphone_mixed_fps_analysis(self):
        clips = load(self.root / "iphone")
        self.assertEqual([c.name for c in clips], ["IMG_0101.MOV", "IMG_0102.MOV", "IMG_0103.MOV", "IMG_0104.MOV"])
        self.assertEqual([c.v.nominal_fps for c in clips], [60, 30, 24, 60])
        report = analyze(clips)
        self.assertTrue(report.lossless_possible)
        self.assertIn(compat.FPS_ISSUE, [i.title for i in report.warnings])
        self.assertTrue(needs_header_embedding(clips))
        plan = compat.conform_plan(clips)
        self.assertEqual(plan.fps, 60)
        self.assertEqual([c.name for c in plan.convert], ["IMG_0102.MOV", "IMG_0103.MOV"])

    def test_iphone_lossless_keeps_every_frame(self):
        """Different HEVC headers: without header embedding ~25% of frames were lost."""
        clips = load(self.root / "iphone")
        out = self.out / "iphone_lossless.mp4"
        merge_lossless(clips, out, lambda *a: None, threading.Event())
        self.assert_lossless(clips, out)
        m = probe(out)
        self.assertEqual((m.v.codec, m.v.codec_tag, m.v.pix_fmt), ("hevc", "hvc1", "yuv420p10le"))
        self.assertEqual((m.v.color_transfer, m.v.color_primaries), ("arib-std-b67", "bt2020"))
        self.assertEqual(m.v.rotation, clips[0].v.rotation)
        self.assertNotEqual(m.v.rotation, 0)
        self.assertAlmostEqual(m.duration, 8.0, delta=0.2)
        self.assertEqual([p.name for p in self.out.iterdir() if p.name.startswith(".")], [])
        out.unlink()

    def _check_conform(self, method):
        clips = load(self.root / "iphone")
        out = self.out / f"iphone_60_{method}.mp4"
        result = merge_conform(clips, out, 60, method, lambda *a: None, threading.Event())
        self.assertFalse(result.lossless)
        m = probe(out)
        self.assertAlmostEqual(m.duration, 8.0, delta=0.2)
        self.assertAlmostEqual(float(m.v.frame_rate), 60, delta=0.1)
        self.assertEqual((m.v.codec, m.v.pix_fmt, m.v.color_transfer, m.v.color_primaries),
                         ("hevc", "yuv420p10le", "arib-std-b67", "bt2020"))
        self.assertEqual(m.v.rotation, clips[0].v.rotation)
        self.assert_monotonic(out)
        ts = frame_times(out)
        steps = [ts[i] - ts[i - 1] for i in range(1, len(ts))]
        # 60 fps everywhere (1 ms rounding; small gaps only at the 3 joins)
        self.assertGreater(sum(0.0155 < s < 0.0175 for s in steps), len(steps) - 6)
        frames = decoded_hashes(out)              # also asserts: no decode errors
        self.assertAlmostEqual(len(frames), 4 * 2 * 60, delta=4)   # 4 clips x 2 s x 60 fps
        # The 60 fps clips were copied untouched: their frames are bit-identical.
        first = decoded_hashes(clips[0].path)
        last = decoded_hashes(clips[3].path)
        self.assertEqual(frames[:len(first)], first)
        self.assertEqual(frames[-len(last):], last)
        # Audio is never re-encoded by the conversion.
        expected_audio = [h for c in clips for h in packet_hashes(c.path, "0:a:0")]
        self.assertEqual(packet_hashes(out, "0:a:0"), expected_audio)
        self.assertEqual([p.name for p in self.out.iterdir() if p.name.startswith(".")], [])
        out.unlink()

    def test_iphone_convert_to_60_duplicate(self):
        self._check_conform(DUPLICATE)

    def test_iphone_convert_to_60_blend(self):
        self._check_conform(BLEND)

    def test_h264_mixed_fps_lossless_and_conform(self):
        """H.264 with B-frames at 60/30/60 fps: lossless keeps every frame;
        conform copies the 60 fps clips untouched and converts the 30 fps one."""
        clips = load(self.root / "h264fps")
        self.assertFalse(needs_header_embedding(clips))
        out = self.out / "h264_mixed.mp4"
        merge_lossless(clips, out, lambda *a: None, threading.Event())
        self.assert_lossless(clips, out)
        out.unlink()

        self.assertEqual([c.name for c in compat.conform_plan(clips).convert], ["s2.mp4"])
        out = self.out / "h264_60.mp4"
        merge_conform(clips, out, 60, DUPLICATE, lambda *a: None, threading.Event())
        frames = decoded_hashes(out)
        self.assertAlmostEqual(len(frames), 3 * 3 * 60, delta=3)
        first, last = decoded_hashes(clips[0].path), decoded_hashes(clips[2].path)
        self.assertEqual(frames[:len(first)], first)
        self.assertEqual(frames[-len(last):], last)
        self.assertAlmostEqual(float(probe(out).v.frame_rate), 60, delta=0.1)
        self.assert_monotonic(out)
        out.unlink()

    # ---------------- Front camera / orientation: re-encode only the odd clip
    def _check_fit(self, folder, odd_name):
        clips = load(self.root / folder)
        report = analyze(clips)
        self.assertIn("Different orientation", [i.title for i in report.blockers])
        plan = compat.fit_plan(clips)
        self.assertTrue(plan.feasible, plan.problem)
        self.assertEqual([Path(i).name for i in plan.convert], [odd_name])
        self.assertNotEqual(plan.reference.name, odd_name)

        out = self.out / f"{folder}_fit.mov"
        result = merge_fit(clips, out, lambda *a: None, threading.Event())
        self.assertFalse(result.lossless)
        m = probe(out)
        self.assertEqual(m.v.rotation, plan.reference.v.rotation)
        self.assertEqual((m.v.width, m.v.height), (plan.reference.v.width, plan.reference.v.height))
        self.assertEqual((m.v.pix_fmt, m.v.color_transfer), ("yuv420p10le", "arib-std-b67"))
        self.assert_monotonic(out)
        got = decoded_hashes(out)                  # no decode errors
        pos = 0
        for c in clips:
            src = decoded_hashes(c.path)
            if c.name == odd_name:
                # Shown the same way as the original (upright), not sideways:
                mid = len(src) // 2
                self.assertGreater(psnr_frames(out, pos + mid, c.path, mid), 35,
                                   "re-encoded clip is not displayed like the original")
            else:
                self.assertEqual(got[pos:pos + len(src)], src, f"{c.name} not bit-exact")
            pos += len(src)
        self.assertEqual(len(got), pos)
        for track in ("0:a:0", "0:a:1"):           # both audio tracks byte-identical
            expected = [h for c in clips for h in packet_hashes(c.path, track)]
            self.assertEqual(packet_hashes(out, track), expected, track)
        self.assertEqual([p.name for p in self.out.iterdir() if p.name.startswith(".")], [])
        out.unlink()

    def test_fit_front_camera_clip(self):
        self._check_fit("selfie", "IMG_0202.MOV")

    def test_fit_upright_clip_into_rotated_footage(self):
        self._check_fit("rotmix", "R2.MOV")

    def test_container_rules_for_spatial_audio(self):
        clip = load(self.root / "4k60")[0]
        clip.audio.append(clip.audio[0].__class__("apple_apac", "", 48000, 4, "", ""))
        self.assertIsNone(container_problem(clip, ".mov"))
        self.assertIsNotNone(container_problem(clip, ".mp4"))
        self.assertIsNotNone(container_problem(clip, ".mkv"))
        self.assertEqual(compat.compatible_container(clip), ".mov")

    def test_cancel_removes_partial_output(self):
        clips = load(self.root / "mixed")
        out = self.out / "cancelled.mp4"
        cancel = threading.Event()

        def cb(f, s, status):
            if f > 0:
                cancel.set()

        with self.assertRaises(Cancelled):
            merge_reencode(clips, out, encode_target(clips), cb, cancel)
        self.assertFalse(out.exists())
        leftovers = [p.name for p in self.out.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_reencode_fallback(self):
        clips = load(self.root / "mixed")
        target = encode_target(clips)
        self.assertEqual((target.width, target.height, float(target.fps)), (1920, 1080, 60.0))
        out = self.out / "re-encoded.mp4"
        result = merge_reencode(clips, out, target, lambda *a: None, threading.Event())
        self.assertFalse(result.lossless)
        merged = probe(out)
        self.assertAlmostEqual(merged.duration, 9.0, delta=0.3)
        self.assertEqual((merged.v.width, merged.v.height), (1920, 1080))
        self.assertAlmostEqual(merged.v.fps, 60, places=1)
        self.assertEqual(merged.a.channels, 2)
        out.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
