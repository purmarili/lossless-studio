"""Drive the real main window: merge flows and the editor, with screenshots.

Usage: python tests/ui_smoke.py <samples-root> <screenshot-dir> [--offscreen]
Dialogs are auto-answered so the run is unattended.
"""

import os
import sys
import time
from pathlib import Path

if "--offscreen" in sys.argv:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from lossless_merger import compat, ffmpeg_tools as ff  # noqa: E402
from lossless_merger.media import probe  # noqa: E402
from lossless_merger.ui import main_window as mw, theme  # noqa: E402

samples, shots = Path(sys.argv[1]), Path(sys.argv[2])
shots.mkdir(parents=True, exist_ok=True)
answers: list[str] = []
asked: list[str] = []


def fake_ask(parent, title, text, buttons, issues=None, footer=None):
    asked.append(title)
    print(f"  [dialog] {title} -> {[b[0] for b in buttons]}")
    return answers.pop(0) if answers else buttons[-1][0]


mw.ask = fake_ask
mw.inform = lambda p, t, x: asked.append(t)

app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(theme.STYLESHEET)
win = mw.MainWindow()
win.resize(1440, 900)
win.show()


def pump(cond, timeout=120):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.01)
    assert cond(), "timed out"


def shot(name):
    pump(lambda: True, 0.4)
    win.grab().save(str(shots / f"{name}.png"))


def load(folder, n):
    win.dirty = False
    win.load_folder(samples / folder)
    pump(lambda: win.stack.currentIndex() == 2)
    pump(lambda: len(win.thumbs) >= n, 60)


def names():
    return [s.clip.name for s in win.project.segments]


shot("01_empty")

# --- Lossless merge of a 4K folder --------------------------------------------
load("4k60", 3)
print("order:", names())
assert names()[0] == "Видео запись.mp4"
assert win.merge_btn.text() == "Merge Videos"
shot("02_loaded_4k")
out = shots / "ui_merged_ვიდეო.mp4"
out.unlink(missing_ok=True)
win._run_merge(win.ordered_clips(), out, None)
pump(lambda: win.merge_thread is None, 120)
m = probe(out)
print(f"merged: {m.duration:.2f}s {m.v.width}x{m.v.height}")
assert abs(m.duration - 9) < 0.2 and asked[-1] == "Merge complete", asked

# Overwrite protection
asked.clear()
before = out.stat().st_mtime
answers[:] = ["back"]
calls = iter([(str(out), ""), ("", "")])
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: next(calls))
assert win._ask_output(win.ordered_clips(), True) is None and "File already exists" in asked
assert out.stat().st_mtime == before
out.unlink()

# --- Cancel a re-encode ---------------------------------------------------------
load("mixed", 3)
assert win.report.blockers
out2 = shots / "cancel_test.mp4"
win._run_merge(win.ordered_clips(), out2, compat.encode_target(win.ordered_clips()))
pump(lambda: win.progress_bar.value() > 0, 60)
win.cancel_merge()
pump(lambda: win.merge_thread is None, 60)
assert not out2.exists() and not [p for p in shots.iterdir() if p.name.startswith(".")]

# --- iPhone Auto-FPS folder: convert to 60 fps through the Merge button ----------
load("iphone", 4)
mw.FrameRateDialog.exec_choice = lambda self: "duplicate"
out3 = shots / "iphone_60fps.mov"
out3.unlink(missing_ok=True)
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(out3), ""))
asked.clear()
win.start_merge()
pump(lambda: win.merge_thread is None, 300)
m = probe(out3)
assert abs(float(m.v.frame_rate) - 60) < 0.1 and asked[-1] == "Merge complete", asked
win.settings.remove("fps_choice")
out3.unlink()

# --- Front-camera folder: "Re-encode only this 1" ----------------------------------
load("selfie", 3)
out4 = shots / "selfie_fit.mov"
out4.unlink(missing_ok=True)
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(out4.with_suffix("")), "QuickTime movie (*.mov)"))
asked.clear()
answers[:] = ["fit"]
win.start_merge()
pump(lambda: win.merge_thread is None, 300)
assert asked[0] == "1 clip doesn’t match the others", asked
assert len(probe(out4).audio) == 2 and asked[-1] == "Merge complete", asked
out4.unlink()

# =============================== EDITOR ==========================================
load("iphone", 4)
pump(lambda: all(c in win.strips for c in win.library), 30)
tl, pv, insp = win.timeline, win.preview, win.inspector
shot("10_editor")

# split clip 2 in the middle, select & speed up the first half, reverse the second
st = win.project.starts()
win._seek(st[1] + 1.0)
win.split_here()
assert len(win.project.segments) == 5, names()
first_half = win.project.segments[1]
win._select("segment", first_half.id)
next(b for b in insp.segment_page.speed_group.buttons() if b.property("speed") == 2.0).click()
assert first_half.edit.speed == 2.0
win._select("segment", win.project.segments[2].id)
insp.segment_page.reverse.setChecked(True)
assert win.project.segments[2].edit.reverse

# trim the start of clip 1 with I at 0.5 s, freeze frame at 1.5 s
win._seek(0.5)
win.set_in_here()
assert abs(win.project.segments[0].src_in - 0.5) < 0.05
win._seek(1.0)
win.freeze_here()
assert any(s.kind == "freeze" for s in win.project.segments)

# Ken Burns + look on the last clip
last = win.project.segments[-1]
win._select("segment", last.id)
insp.segment_page.tabs.setCurrentIndex(3)
insp.segment_page._kb_preset(*[__import__("lossless_merger.project", fromlist=["Transform"]).Transform(1.0),
                               __import__("lossless_merger.project", fromlist=["Transform"]).Transform(1.3)])
insp.segment_page._look_preset("warm")
assert last.edit.tf_end is not None and last.edit.look == "warm"

# transition between the first two clips
win._select("transition", win.project.segments[0].id)
next(b for b in insp.transition_page.group.buttons() if b.property("key") == "fade").click()
assert win.project.transition_after(0) > 0

# text with pop animation
win._seek(0.2)
win.add_text()
insp.text_page.edit.setPlainText("საქართველო · Тбилиси")
next(b for b in insp.text_page.style_group.buttons() if b.property("key") == "neon").click()
text = win.project.texts[0]
assert text.text.startswith("საქართველო") and text.style == "neon"

# photo from "Import" -> timeline (Ken Burns by default), and as picture-in-picture
photo = shots / "photo_test.jpg"
ff.run([ff.ffmpeg_path(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=900x1200", "-frames:v", "1",
        str(photo)])
music = shots / "music_test.m4a"
ff.run([ff.ffmpeg_path(), "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=3",
        "-ac", "2", str(music)])
QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([str(photo), str(music)], ""))
win.add_media("library")
pump(lambda: not win._batches, 30)
assert any(c.is_image for c in win.clips.values()) and win.project.audios
win.list.setCurrentRow(win.list.count() - 1)
win._seek(win.project.duration)
win.add_selected_to_timeline()
assert win.project.segments[-1].kind == "image"
win._seek(1.0)
win.list.setCurrentRow(0)
win.add_selected_as_overlay()
assert win.project.overlays
pump(lambda: True, 1.0)
win._select("", "")
win._seek(1.5)
pump(lambda: True, 2.0)
shot("11_editor_layers")
win._select("text", text.id)
shot("12_editor_text")
win._select("segment", last.id)
insp.segment_page.tabs.setCurrentIndex(1)
shot("13_editor_look")
win._select("", "")
shot("14_editor_project")

# undo / redo
n_texts = len(win.project.texts)
win.delete_selected()            # nothing selected -> no-op
win._select("text", text.id)
win.delete_selected()
assert len(win.project.texts) == n_texts - 1
win.undo()
assert len(win.project.texts) == n_texts, "undo restores the text"
win.redo()
assert len(win.project.texts) == n_texts - 1
win.undo()

# play a little
pv.play()
pump(lambda: pv.position() > 1.2, 20)
pv.pause()
print("played to", round(pv.position(), 2))

# save + reopen the project
proj = shots / "smoke.lsproj"
proj.unlink(missing_ok=True)
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(proj), ""))
win.project_path = None
assert win.save_project() and proj.exists()
dur_before, n_before = win.project.duration, len(win.project.segments)
win.dirty = False
win.load_project(proj)
assert abs(win.project.duration - dur_before) < 1e-6 and len(win.project.segments) == n_before
assert win.project.texts and win.project.texts[0].style == "neon"

# export
out5 = shots / "edited.mov"
out5.unlink(missing_ok=True)
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(out5), ""))
asked.clear()
win.start_merge()
pump(lambda: win.merge_thread is None, 900)
assert asked[0] == "Export edited video" and asked[-1] == "Export complete", asked
m = probe(out5)
print(f"edited export: {m.duration:.2f}s (project {win.project.duration:.2f}s) {m.v.codec} "
      f"{m.v.width}x{m.v.height} rot={m.v.rotation} audio={[a.codec for a in m.audio]}")
assert abs(m.duration - win.project.duration) < 0.2
win.preview.shutdown()                       # release the preview's open files
pump(lambda: True, 0.5)

# ========================== ADDING CLIPS ONE BY ONE ===============================
win.preview.set_project(win.project)
win.dirty = False
win.start_empty(add=False)
assert win.stack.currentIndex() == 2 and not win.project.segments
shot("20_empty_timeline")
iph = sorted((samples / "iphone").glob("*.MOV"))           # IMG_0101..0104, recorded in that order
# pick two in reverse order: with "recording order" on they are sorted by date
QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([str(iph[2]), str(iph[0])], ""))
win.add_media("end")
pump(lambda: not win._batches and len(win.project.segments) == 2, 30)
assert names() == ["IMG_0101.MOV", "IMG_0103.MOV"], names()
# one more at the end via Ctrl+I path
QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([str(iph[3])], ""))
win.add_media("end")
pump(lambda: len(win.project.segments) == 3, 30)
assert names()[-1] == "IMG_0104.MOV"
# drop a file from Explorer between the first two clips
win._on_media_dropped([str(iph[1])], "paths", ("index", 1))
pump(lambda: len(win.project.segments) == 4, 30)
assert names() == ["IMG_0101.MOV", "IMG_0102.MOV", "IMG_0103.MOV", "IMG_0104.MOV"], names()
# drag a tile from the media panel onto the overlay track at 2 s
win._on_media_dropped([win.clips[str(iph[0])].id], "ids", ("overlay", 2.0))
assert win.project.overlays and abs(win.project.overlays[-1].start - 2.0) < 1e-6
# insert at the playhead (in the first half of clip 3 -> before it)
win._seek(win.project.starts()[2] + 0.3)
win.list.clearSelection()
win._select_in_bin([str(iph[3])])
win.add_selected_to_timeline("playhead")
assert names()[2] == "IMG_0104.MOV", names()
pump(lambda: all(c in win.strips for c in win.library if not win.clips[c].is_image), 30)
shot("21_added_one_by_one")
# remove from the media panel (used -> only from panel)
answers[:] = ["keep"]
win.list.clearSelection()
win._select_in_bin([str(iph[3])])
win.remove_from_library()
assert str(iph[3]) not in win.library and "IMG_0104.MOV" in names()
# delete every clip -> empty timeline is fine, undo brings them back
while win.project.segments:
    win._select("segment", win.project.segments[0].id)
    win.delete_selected()
assert not win.project.segments and win.project.duration == 0
win.undo()
assert len(win.project.segments) == 1
win.dirty = False

# --- 360° video: dropped on the timeline -> choose a view -> converted & inserted ------
fmt_root = samples.parent / "formats"
insv = fmt_root / "insta360" / "VID_20251003_130500_00_002.insv"
if insv.exists():
    from lossless_merger.ui import reframe_dialog
    from lossless_merger import reframe as rf
    reframe_dialog.ReframeDialog.result_view = lambda self: (rf.View360(yaw=45), False)
    n0 = len(win.project.segments)
    win._on_media_dropped([str(insv)], "paths", ("index", 0))
    pump(lambda: len(win.project.segments) == n0 + 1, 120)
    first = win.project.segments[0].clip
    assert "_view" in first.name and first.name.endswith(".mp4") and not first.is_360, first.name
    print("360 converted:", first.name, first.v.width, "x", first.v.height)
    shot("22_360_converted")
    win.dirty = False
    win.preview.shutdown()
    pump(lambda: True, 0.5)
    try:
        first.path.unlink(missing_ok=True)
    except PermissionError:
        pass

for f in (out5, photo, music, proj):
    try:
        f.unlink(missing_ok=True)
    except PermissionError:
        pass
win.dirty = False
print("UI SMOKE TEST PASSED")
QTimer.singleShot(0, app.quit)
win.close()

