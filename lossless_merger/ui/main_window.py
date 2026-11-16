"""Main application window: media bin, preview, edit panel, timeline, export."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QThreadPool, QTimer
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QListWidgetItem, QMainWindow, QMenu, QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QSplitter, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from .. import APP_NAME, captions, compat, merger, projectio, render
from ..ffmpeg_tools import FFmpegNotFound, ffmpeg_path, ffprobe_path
from ..media import (AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, Clip, codec_label, fmt_duration,
                     fmt_size, probe, probe_audio, scan_folder, sort_chronologically)
from ..project import (PRESETS, STILL_LENGTH, AudioItem, OverlayItem, Project, Segment, TextItem)
from . import theme
from .clip_list import ID_ROLE, BinDelegate, BinList
from .dialogs import FrameRateDialog, ask, inform
from .inspector import Inspector
from .mediaimport import load_photo
from .preview import Preview, fmt_time
from .recorder import VoiceRecorder
from .textimg import text_render
from .timeline import Timeline
from .workers import (Bus, FilmstripTask, ImportSignals, ImportTask, MergeThread, ProbeTask, ThumbTask,
                      WaveTask)

MEDIA_FILTER = ("Media (" + " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS))
                + ");;Videos (" + " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS)) + ");;Photos ("
                + " ".join(f"*{e}" for e in sorted(IMAGE_EXTENSIONS)) + ");;All files (*.*)")
AUDIO_FILTER = "Audio (" + " ".join(f"*{e}" for e in sorted(AUDIO_EXTENSIONS)) + " *.mp4 *.mov);;All files (*.*)"
PROJECT_FILTER = f"Lossless Video Merger project (*{projectio.EXTENSION})"


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(theme.app_icon())
        self.resize(1440, 900)
        self.setMinimumSize(1060, 700)
        self.setAcceptDrops(True)

        self.settings = QSettings("LosslessVideoMerger", "LosslessVideoMerger")
        self.bus = Bus()
        self.bus.probed.connect(self._on_probed)
        self.bus.thumbnail.connect(self._on_thumbnail)
        self.bus.filmstrip.connect(self._on_filmstrip)
        self.bus.waveform.connect(self._on_waveform)
        self.probe_pool = QThreadPool(self)
        self.probe_pool.setMaxThreadCount(max(2, min(6, os.cpu_count() or 4)))
        self.thumb_pool = QThreadPool(self)
        self.thumb_pool.setMaxThreadCount(3)

        self.folder: Path | None = None
        self.project_path: Path | None = None
        self.generation = 0
        self.clips: dict[str, Clip] = {}          # media library, by path
        self.library: list[str] = []              # bin order
        self.thumbs: dict[str, QPixmap] = {}
        self.strips: dict[str, QPixmap] = {}
        self.waves: dict[str, QPixmap] = {}
        self.requested: set[str] = set()
        self.project = Project()
        self.sel = ("", "")
        self.pending = 0
        self.total_found = 0
        self.report = compat.CompatReport([])
        self.merge_thread: MergeThread | None = None
        self.job_done = None                      # callback for non-export jobs (captions)
        self.merge_started = 0.0
        self.undo_stack: list = []
        self.redo_stack: list = []
        self._last_snap = None
        self._last_key = None
        self._last_time = 0.0
        self.dirty = False
        self.recorder: VoiceRecorder | None = None
        self._batches: dict[int, dict] = {}
        self._batch_id = 0
        self.import_sig = ImportSignals()
        self.import_sig.done.connect(self._on_imported)
        self.rec_start = 0.0

        self._build_ui()
        QTimer.singleShot(0, self._check_ffmpeg)

    # ================================================================== UI
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(10)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(1)
        t = QLabel(APP_NAME)
        t.setObjectName("title")
        st = QLabel("Join and edit your recordings — untouched footage keeps its original quality.")
        st.setObjectName("subtitle")
        titles.addWidget(t)
        titles.addWidget(st)
        head.addLayout(titles, 1)
        self.undo_btn = QPushButton("↶  Undo")
        self.redo_btn = QPushButton("↷  Redo")
        self.open_proj_btn = QPushButton("Open project…")
        self.save_btn = QPushButton("Save project")
        for b in (self.undo_btn, self.redo_btn, self.open_proj_btn, self.save_btn):
            b.setObjectName("ghost")
            head.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        self.undo_btn.setToolTip("Undo (Ctrl+Z)")
        self.redo_btn.setToolTip("Redo (Ctrl+Y)")
        self.save_btn.setToolTip("Save the edit to continue later (Ctrl+S)")
        self.undo_btn.clicked.connect(self.undo)
        self.redo_btn.clicked.connect(self.redo)
        self.open_proj_btn.clicked.connect(self.open_project)
        self.save_btn.clicked.connect(self.save_project)
        self.undo_btn.setEnabled(False)
        self.redo_btn.setEnabled(False)
        head.addSpacing(6)
        self.open_btn = QPushButton("Choose folder…")
        self.open_btn.clicked.connect(self.choose_folder)
        head.addWidget(self.open_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(head)

        frow = QHBoxLayout()
        self.folder_label = QLabel("")
        self.folder_label.setObjectName("folderLabel")
        self.folder_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        frow.addWidget(self.folder_label, 1)
        self.recursive_cb = QCheckBox("Include subfolders")
        self.recursive_cb.setChecked(self.settings.value("recursive", False, type=bool))
        self.recursive_cb.toggled.connect(self._on_recursive_toggled)
        frow.addWidget(self.recursive_cb)
        root.addLayout(frow)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._empty_page())
        self.stack.addWidget(self._loading_page())
        self.stack.addWidget(self._editor_page())

        self.bottom = QFrame()
        self.bottom.setObjectName("panel")
        bl = QHBoxLayout(self.bottom)
        bl.setContentsMargins(16, 10, 10, 10)
        bl.setSpacing(14)
        self.status_box = QWidget()
        sl = QVBoxLayout(self.status_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(2)
        self.compat_label = QLabel("")
        self.compat_label.setTextFormat(Qt.TextFormat.RichText)
        self.compat_label.linkActivated.connect(lambda _: self.show_compat_details())
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("summary")
        sl.addWidget(self.compat_label)
        sl.addWidget(self.summary_label)
        bl.addWidget(self.status_box, 1)
        self.progress_box = QWidget()
        pl = QVBoxLayout(self.progress_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        self.progress_label = QLabel("")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setFixedHeight(8)
        pl.addWidget(self.progress_label)
        pl.addWidget(self.progress_bar)
        self.progress_box.hide()
        bl.addWidget(self.progress_box, 1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.clicked.connect(self.cancel_merge)
        self.cancel_btn.hide()
        bl.addWidget(self.cancel_btn)
        self.merge_btn = QPushButton("Merge Videos")
        self.merge_btn.setObjectName("primary")
        self.merge_btn.setEnabled(False)
        self.merge_btn.clicked.connect(self.start_merge)
        bl.addWidget(self.merge_btn)
        root.addWidget(self.bottom)
        self.bottom.hide()

        sc = lambda key, fn: QShortcut(QKeySequence(key), self, fn)  # noqa: E731
        sc(QKeySequence.StandardKey.Undo, self.undo)
        sc("Ctrl+Y", self.redo)
        sc("Ctrl+Shift+Z", self.redo)
        sc("Ctrl+S", self.save_project)
        sc("Ctrl+O", self.choose_folder)
        sc("Ctrl+Shift+O", self.open_project)
        sc("Ctrl+I", lambda: self.add_media("end"))
        sc("Ctrl+Shift+I", lambda: self.add_media("playhead"))
        sc(Qt.Key.Key_Space, self._space)
        sc(Qt.Key.Key_S, self.split_here)
        sc(Qt.Key.Key_I, self.set_in_here)
        sc(Qt.Key.Key_O, self.set_out_here)
        sc(Qt.Key.Key_F, self.freeze_here)
        sc(Qt.Key.Key_T, self.add_text)
        sc(Qt.Key.Key_Delete, self.delete_selected)
        sc(Qt.Key.Key_Left, lambda: self._step(-1))
        sc(Qt.Key.Key_Right, lambda: self._step(1))
        sc(Qt.Key.Key_Home, lambda: self._seek(0))
        sc(Qt.Key.Key_End, lambda: self._seek(self.project.duration))
        sc("Ctrl+=", lambda: self.timeline.zoom(1.3))
        sc("Ctrl+-", lambda: self.timeline.zoom(1 / 1.3))

    def _empty_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)
        self.drop_zone = QFrame()
        self.drop_zone.setObjectName("dropZone")
        dz = QVBoxLayout(self.drop_zone)
        dz.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(QPixmap.fromImage(theme.app_icon_image(72)))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("Start a new video")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel("Pick a whole folder (clips are put in the order they were recorded), or add videos and\n"
                      "photos one by one. You can also drop files or a folder anywhere on this window.")
        text.setObjectName("emptyText")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton("Choose folder…")
        btn.setObjectName("primary")
        btn.clicked.connect(self.choose_folder)
        one = QPushButton("＋  Add videos one by one…")
        one.setObjectName("primary")
        one.clicked.connect(lambda: self.start_empty(add=True))
        pbtn = QPushButton("Open project…")
        pbtn.clicked.connect(self.open_project)
        empty = QPushButton("Empty timeline")
        empty.setToolTip("Start with nothing and add media later")
        empty.clicked.connect(lambda: self.start_empty(add=False))
        for b in (btn, one, pbtn, empty):
            row.addWidget(b)
        row.addStretch(1)
        self.empty_title, self.empty_text = title, text
        dz.addWidget(icon)
        dz.addSpacing(6)
        dz.addWidget(title)
        dz.addWidget(text)
        dz.addSpacing(10)
        dz.addLayout(row)
        lay.addWidget(self.drop_zone)
        return page

    def _loading_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label = QLabel("Reading video information…")
        self.loading_label.setObjectName("emptyText")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_bar = QProgressBar()
        self.loading_bar.setFixedWidth(320)
        self.loading_bar.setFixedHeight(6)
        lay.addWidget(self.loading_label)
        lay.addSpacing(8)
        lay.addWidget(self.loading_bar, 0, Qt.AlignmentFlag.AlignCenter)
        return page

    def _editor_page(self):
        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.setChildrenCollapsible(False)
        top = QSplitter(Qt.Orientation.Horizontal)
        top.setChildrenCollapsible(False)

        # --- media bin ---
        binw = QFrame()
        binw.setObjectName("editorPanel")
        bl = QVBoxLayout(binw)
        bl.setContentsMargins(10, 10, 10, 10)
        bl.setSpacing(6)
        brow = QHBoxLayout()
        lab = QLabel("Media")
        lab.setObjectName("sectionTitle")
        brow.addWidget(lab)
        brow.addStretch(1)
        add = QPushButton("+ Import")
        add.setObjectName("ghost")
        add.setToolTip("Add videos, photos or music to this panel (without putting them on the timeline)")
        add.clicked.connect(lambda: self.add_media("library"))
        brow.addWidget(add)
        bl.addLayout(brow)
        self.list = BinList()
        self.list.setItemDelegate(BinDelegate(self._lookup, self._is_used, self.list))
        self.list.itemDoubleClicked.connect(lambda _: self.add_selected_to_timeline("end"))
        self.list.customContextMenuRequested.connect(self._bin_menu)
        bl.addWidget(self.list, 1)
        arow = QHBoxLayout()
        self.add_tl_btn = QPushButton("Add to timeline")
        self.add_ov_btn = QPushButton("Add as overlay")
        self.add_tl_btn.setToolTip("Add the selected clips to the end of the timeline (or double-click, "
                                   "or drag them onto the timeline)")
        self.add_ov_btn.setToolTip("Picture-in-picture at the playhead")
        self.add_tl_btn.clicked.connect(lambda: self.add_selected_to_timeline("end"))
        self.add_ov_btn.clicked.connect(lambda: self.add_selected_as_overlay())
        arow.addWidget(self.add_tl_btn)
        arow.addWidget(self.add_ov_btn)
        bl.addLayout(arow)
        self.bin_info = QLabel("")
        self.bin_info.setObjectName("hint")
        self.bin_info.setWordWrap(True)
        self.bin_info.setTextFormat(Qt.TextFormat.RichText)
        self.bin_info.linkActivated.connect(lambda _: self.show_compat_details())
        bl.addWidget(self.bin_info)
        top.addWidget(binw)

        # --- preview ---
        pv = QFrame()
        pv.setObjectName("editorPanel")
        pl = QVBoxLayout(pv)
        pl.setContentsMargins(10, 10, 10, 8)
        self.preview = Preview()
        self.preview.time_changed.connect(lambda t: self.timeline.set_playhead(t, follow=True))
        pl.addWidget(self.preview)
        top.addWidget(pv)

        # --- inspector ---
        insp = QFrame()
        insp.setObjectName("editorPanel")
        il = QVBoxLayout(insp)
        il.setContentsMargins(12, 8, 6, 8)
        self.inspector = Inspector()
        self.inspector.changed.connect(self._on_inspector_changed)
        self.inspector.transition_to_all.connect(self.transition_to_all)
        self.inspector.text_style_to_all.connect(self.text_style_to_all)
        il.addWidget(self.inspector)
        top.addWidget(insp)
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setStretchFactor(2, 0)
        binw.setMinimumWidth(170)
        insp.setMinimumWidth(400)
        top.setSizes([300, 720, 430])
        vsplit.addWidget(top)

        # --- toolbar + timeline ---
        bottom = QFrame()
        bottom.setObjectName("editorPanel")
        tl = QVBoxLayout(bottom)
        tl.setContentsMargins(8, 6, 8, 6)
        tl.setSpacing(4)
        bar = QHBoxLayout()
        bar.setSpacing(2)

        def tool(text, tip, fn):
            b = QPushButton(text)
            b.setObjectName("ghost")
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
            return b

        self.add_btn = QToolButton()
        self.add_btn.setObjectName("addMedia")
        self.add_btn.setText("＋  Add media")
        self.add_btn.setToolTip("Add videos or photos to the end of the timeline (Ctrl+I)\n"
                                "Click the arrow for more options.")
        self.add_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.add_btn.clicked.connect(lambda: self.add_media("end"))
        menu = QMenu(self.add_btn)
        menu.addAction("Add to the end of the timeline…\tCtrl+I", lambda: self.add_media("end"))
        menu.addAction("Insert at the playhead…\tCtrl+Shift+I", lambda: self.add_media("playhead"))
        menu.addAction("Add a whole folder…", self.add_folder)
        menu.addAction("Import to the media panel only…", lambda: self.add_media("library"))
        menu.addSeparator()
        self.sort_action = menu.addAction("Put several new clips in recording order")
        self.sort_action.setCheckable(True)
        self.sort_action.setChecked(self.settings.value("sort_new", True, type=bool))
        self.sort_action.toggled.connect(lambda c: self.settings.setValue("sort_new", c))
        self.add_btn.setMenu(menu)
        bar.addWidget(self.add_btn)
        bar.addSpacing(10)
        tool("✂ Split", "Split at the playhead (S)", self.split_here)
        tool("🗑 Delete", "Delete the selected item (Del)", self.delete_selected)
        tool("⧉ Duplicate", "Duplicate the selected item", self.duplicate_selected)
        tool("❄ Freeze", "Insert a freeze frame at the playhead (F)", self.freeze_here)
        bar.addSpacing(14)
        tool("T  Text", "Add a text at the playhead (T)", self.add_text)
        tool("♪ Music", "Add music or a sound", self.add_music)
        self.rec_btn = tool("● Voice-over", "Record your voice while the video plays", self.toggle_voiceover)
        self.cc_btn = tool("CC Auto captions", "Turn speech into subtitles (offline)", self.auto_captions)
        bar.addStretch(1)
        tool("−", "Zoom out (Ctrl+wheel)", lambda: self.timeline.zoom(1 / 1.3))
        tool("+", "Zoom in (Ctrl+wheel)", lambda: self.timeline.zoom(1.3))
        tool("Fit", "Show the whole video", lambda: self.timeline.zoom_fit())
        tl.addLayout(bar)
        self.timeline = Timeline(lambda cid: self.thumbs.get(cid), lambda cid: self.strips.get(cid),
                                 lambda p: self.waves.get(p))
        self.timeline.playhead_changed.connect(self._on_scrub)
        self.timeline.selection_changed.connect(self._on_timeline_select)
        self.timeline.editing.connect(self._on_timeline_editing)
        self.timeline.edited.connect(self._on_timeline_edited)
        self.timeline.request_split.connect(self.split_here)
        self.timeline.request_delete.connect(self.delete_selected)
        self.timeline.request_duplicate.connect(self.duplicate_selected)
        self.timeline.media_dropped.connect(self._on_media_dropped)
        tscroll = QScrollArea()
        tscroll.setWidgetResizable(True)
        tscroll.setFrameShape(QFrame.Shape.NoFrame)
        tscroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tscroll.setWidget(self.timeline)
        tl.addWidget(tscroll, 1)
        vsplit.addWidget(bottom)
        vsplit.setStretchFactor(0, 3)
        vsplit.setStretchFactor(1, 2)
        vsplit.setSizes([520, 290])
        self.editor = vsplit
        return vsplit

    # ============================================================ helpers
    def _lookup(self, clip_id):
        c = self.clips.get(clip_id)
        if c is None:
            return None, None, None
        seg = Segment(c, kind="image") if c.is_image else Segment(c)
        return seg, self.thumbs.get(clip_id), self.report.clip_severity(clip_id)

    def ordered_segments(self) -> list[Segment]:
        return list(self.project.segments)

    def ordered_clips(self) -> list[Clip]:
        return [s.clip for s in self.project.segments]

    def _check_ffmpeg(self):
        try:
            ffmpeg_path()
            ffprobe_path()
        except FFmpegNotFound as exc:
            inform(self, "FFmpeg not found", str(exc))
            self.open_btn.setEnabled(False)

    def _busy(self) -> bool:
        return self.merge_thread is not None

    def _request_media_art(self, clip: Clip):
        if clip.error or clip.id in self.requested:
            return
        self.requested.add(clip.id)
        self.thumb_pool.start(ThumbTask(self.bus, self.generation, clip))
        if not clip.is_image:
            self.thumb_pool.start(FilmstripTask(self.bus, clip))

    def _request_wave(self, path: Path):
        key = str(path)
        if key not in self.requested:
            self.requested.add(key)
            self.thumb_pool.start(WaveTask(self.bus, path))

    # ====================================================== folder loading
    def choose_folder(self):
        if self._busy() or not self._confirm_discard():
            return
        start = str(self.folder) if self.folder else self.settings.value("last_folder", "")
        d = QFileDialog.getExistingDirectory(self, "Choose a folder with video recordings", start)
        if d:
            self.load_folder(Path(d))

    def _on_recursive_toggled(self, checked):
        self.settings.setValue("recursive", checked)
        if self.folder and not self._busy() and self.project_path is None and self._confirm_discard():
            self.load_folder(self.folder)

    def _reset_state(self):
        self.generation += 1
        self.probe_pool.clear()
        self.thumb_pool.clear()
        self.preview.set_project(None)
        self.clips.clear()
        self.library.clear()
        self.thumbs.clear()
        self.strips.clear()
        self.waves.clear()
        self.requested.clear()
        self.project = Project()
        self.sel = ("", "")
        self.list.clear()
        self.report = compat.CompatReport([])
        self.project_path = None
        self.dirty = False

    def load_folder(self, folder: Path):
        self._reset_state()
        self.folder = folder
        self.settings.setValue("last_folder", str(folder))
        self.folder_label.setText(f"📁  {folder}")
        self.folder_label.setToolTip(str(folder))
        try:
            files = scan_folder(folder, self.recursive_cb.isChecked())
        except OSError as exc:
            inform(self, "Cannot read folder", str(exc))
            return
        if not files:
            self.empty_title.setText("No videos found in this folder")
            self.empty_text.setText("Supported: MP4, MOV, MKV, MTS/M2TS, TS, AVI, WebM, 3GP, MPG, WMV …\n"
                                    "Try another folder or enable “Include subfolders”.")
            self.stack.setCurrentIndex(0)
            self.bottom.hide()
            return
        self.total_found = self.pending = len(files)
        self.loading_bar.setRange(0, len(files))
        self.loading_bar.setValue(0)
        self.loading_label.setText(f"Reading video information… 0 of {len(files)}")
        self.stack.setCurrentIndex(1)
        self.bottom.hide()
        for f in files:
            self.probe_pool.start(ProbeTask(self.bus, self.generation, f))

    def _on_probed(self, gen, clip: Clip):
        if gen != self.generation:
            return
        self.clips[clip.id] = clip
        self.pending -= 1
        done = self.total_found - self.pending
        self.loading_bar.setValue(done)
        self.loading_label.setText(f"Reading video information… {done} of {self.total_found}")
        if self.pending == 0:
            ordered = sort_chronologically(list(self.clips.values()))
            self.library = [c.id for c in ordered]
            self.project = Project([Segment(c) for c in ordered])
            self._show_editor()
            self._reset_history()

    def _show_editor(self):
        self._fill_bin()
        self.stack.setCurrentIndex(2)
        self.bottom.show()
        for cid in self.library:
            self._request_media_art(self.clips[cid])
        for a in self.project.audios:
            self._request_wave(a.path)
        for o in self.project.overlays:
            self._request_media_art(o.clip)
        self.timeline.set_project(self.project)
        self.preview.set_project(self.project)
        self._project_changed(reload_preview=False)
        QTimer.singleShot(50, self.timeline.zoom_fit)
        self._select("", "")

    def _fill_bin(self):
        self.list.clear()
        for cid in self.library:
            item = QListWidgetItem()
            item.setData(ID_ROLE, cid)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            item.setToolTip(str(self.clips[cid].path))
            self.list.addItem(item)

    def _on_thumbnail(self, gen, clip_id, image):
        if image is None:
            return
        self.thumbs[clip_id] = QPixmap.fromImage(image)
        self.list.viewport().update()
        self.timeline.update()

    def _on_filmstrip(self, clip_id, image):
        if image is not None:
            self.strips[clip_id] = QPixmap.fromImage(image)
            self.timeline.update()

    def _on_waveform(self, key, image):
        if image is not None:
            self.waves[key] = QPixmap.fromImage(image)
            self.timeline.update()

    # ======================================================= media import
    # Adding media works the same way from every entry point (buttons, menus,
    # drag & drop from Explorer or the media panel): files are read in the
    # background as one batch, then placed in one undoable step.
    def start_empty(self, add: bool = False):
        if self._busy() or not self._confirm_discard():
            return
        self._reset_state()
        self.folder = None
        self.folder_label.setText("🎬  New video")
        self.folder_label.setToolTip("")
        self._show_editor()
        self._reset_history()
        if add:
            self.add_media("end")

    def add_media(self, where: str = "end"):
        """Ask for files and add them: where = end | playhead | library."""
        if self._busy():
            return
        start = self.settings.value("import_folder", str(self.folder or ""))
        title = {"library": "Import media", "playhead": "Insert at the playhead"}.get(where, "Add videos and photos")
        files, _ = QFileDialog.getOpenFileNames(self, title, start, MEDIA_FILTER)
        if not files:
            return
        self.settings.setValue("import_folder", str(Path(files[0]).parent))
        self.add_paths([Path(f) for f in files], ("library",) if where == "library" else (where,))

    def add_folder(self):
        if self._busy():
            return
        start = self.settings.value("import_folder", str(self.folder or ""))
        d = QFileDialog.getExistingDirectory(self, "Add all videos of a folder", start)
        if not d:
            return
        self.settings.setValue("import_folder", d)
        files = scan_folder(Path(d), self.recursive_cb.isChecked())
        if not files:
            inform(self, "No videos found", "There are no videos in this folder.")
            return
        self.add_paths(files, ("end",), sort=True)

    def add_paths(self, paths: list[Path], dest: tuple, sort: bool | None = None):
        """Add files. dest: ("end",) | ("playhead",) | ("index", i) | ("overlay", t) |
        ("audio", t) | ("library",)."""
        if self.stack.currentIndex() != 2:
            self.start_empty(add=False)
        audio = [p for p in paths if p.suffix.lower() in AUDIO_EXTENSIONS]
        media = [p for p in paths if p.suffix.lower() in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS]
        skipped = [p.name for p in paths if p not in audio and p not in media and not p.is_dir()]
        for d in [p for p in paths if p.is_dir()]:
            media += scan_folder(d, self.recursive_cb.isChecked())
        if audio:                       # sound always goes to the audio track (the panel holds pictures)
            t = dest[1] if dest[0] in ("audio", "overlay") else (
                self.preview.position() if self.project.segments else 0.0)
            for k, p in enumerate(audio):
                a = self._add_audio(p, "music" if k == 0 and dest[0] != "audio" else "sound", start=t, select=False)
                if a is not None:
                    t += self.project.audio_length(a) if a.kind != "music" else 0
        if skipped:
            inform(self, "Some files were skipped", "These aren't videos, photos or sound files:\n\n"
                   + "\n".join(skipped[:10]))
        if not media:
            return
        self._batch_id += 1
        bid = self._batch_id
        self._batches[bid] = {"paths": media, "clips": [None] * len(media), "left": len(media), "dest": dest,
                              "sort": self.settings.value("sort_new", True, type=bool) if sort is None else sort}
        self.preview.message.setText(f"Adding {len(media)} file{'s' if len(media) != 1 else ''}…")
        for i, p in enumerate(media):
            self.probe_pool.start(ImportTask(self.import_sig, bid, i, p))

    def _on_imported(self, bid: int, index: int, clip: Clip):
        b = self._batches.get(bid)
        if b is None:
            return
        b["clips"][index] = clip
        b["left"] -= 1
        if b["left"] > 0:
            self.preview.message.setText(f"Adding files… {len(b['clips']) - b['left']} of {len(b['clips'])}")
            return
        del self._batches[bid]
        clips = [c for c in b["clips"] if c is not None]
        bad = [c for c in clips if c.error]
        good = [c for c in clips if not c.error]
        if bad:
            inform(self, "Some files can't be used", "\n".join(f"{c.name}: {c.error}" for c in bad[:8]))
        if b["sort"] and len(good) > 1:
            good = sort_chronologically(good)
        for c in good:
            self.clips[c.id] = c
            if c.id not in self.library:
                self.library.append(c.id)
            self._request_media_art(c)
        self._fill_bin()
        self.preview.message.setText(f"Added {len(good)} file{'s' if len(good) != 1 else ''}" if good else "")
        if good and b["dest"][0] != "library":
            self._place(good, b["dest"])
        elif good:
            self._select_in_bin([c.id for c in good])

    def _select_in_bin(self, ids: list[str]):
        self.list.clearSelection()
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(ID_ROLE) in ids:
                item.setSelected(True)
                self.list.scrollToItem(item)

    def _new_segment(self, clip: Clip) -> Segment:
        if clip.is_image:
            from ..project import Transform
            seg = Segment(clip, kind="image", still_len=STILL_LENGTH)
            seg.edit.tf, seg.edit.tf_end = Transform(1.0), Transform(1.12)      # gentle Ken Burns
            return seg
        return Segment(clip)

    def _place(self, clips: list[Clip], dest: tuple):
        """Put library clips on the timeline (one undo step)."""
        clips = [c for c in clips if c is not None and not c.error]
        if not clips:
            return
        kind = dest[0]
        if kind == "overlay" and self.project.segments:
            t = dest[1]
            first = None
            for c in clips:
                dur = STILL_LENGTH if c.is_image else min(c.duration, max(1.0, self.project.duration - t))
                it = OverlayItem(c, kind="image" if c.is_image else "video", start=t, duration=max(0.5, dur))
                self.project.overlays.append(it)
                first = first or it
                t += it.duration
            self._edited("add.overlay")
            self._select("overlay", first.id)
            return
        if kind == "index":
            idx = dest[1]
        elif kind == "playhead":
            idx = self._insert_index()
        else:                                  # end (also for drops on the audio track)
            idx = len(self.project.segments)
        idx = max(0, min(idx, len(self.project.segments)))
        segs = [self._new_segment(c) for c in clips]
        self.project.segments[idx:idx] = segs
        self._edited("add")
        self._select("segment", segs[0].id)
        st = self.project.starts()[idx]
        self._seek(st)
        if len(self.project.segments) == len(segs):
            QTimer.singleShot(30, self.timeline.zoom_fit)
        self.preview.message.setText(f"Added {len(segs)} clip{'s' if len(segs) != 1 else ''} to the timeline")

    def _on_media_dropped(self, items, kind: str, dest: tuple):
        if self._busy():
            return
        if kind == "ids":
            clips = [self.clips[i] for i in items if i in self.clips]
            if dest[0] == "audio":
                dest = ("end",)
            self._place(clips, dest)
        else:
            self.add_paths([Path(p) for p in items], dest)

    def _is_used(self, clip_id: str) -> bool:
        return any(s.clip.id == clip_id for s in self.project.segments) or \
            any(o.clip.id == clip_id for o in self.project.overlays)

    def _bin_menu(self, pos):
        ids = self.list.selected_ids()
        if not ids:
            item = self.list.itemAt(pos)
            if item is None:
                m = QMenu(self)
                m.addAction("Import media…", lambda: self.add_media("library"))
                m.exec(self.list.viewport().mapToGlobal(pos))
                return
            item.setSelected(True)
            ids = [item.data(ID_ROLE)]
        m = QMenu(self)
        m.addAction("Add to the end of the timeline", lambda: self.add_selected_to_timeline("end"))
        m.addAction("Insert at the playhead", lambda: self.add_selected_to_timeline("playhead"))
        m.addAction("Add as picture-in-picture", self.add_selected_as_overlay)
        m.addSeparator()
        m.addAction("Show in folder", lambda: subprocess.Popen(["explorer", "/select,", str(self.clips[ids[0]].path)]))
        m.addAction("Remove from media panel\tDel", self.remove_from_library)
        m.exec(self.list.viewport().mapToGlobal(pos))

    def remove_from_library(self):
        ids = self.list.selected_ids()
        if not ids or self._busy():
            return
        used = [i for i in ids if self._is_used(i)]
        if used:
            choice = ask(self, "Clip is on the timeline",
                         f"{len(used)} of the selected clips {'is' if len(used) == 1 else 'are'} used on the "
                         "timeline. Remove {} from the timeline too?".format("it" if len(used) == 1 else "them"),
                         [("cancel", "Cancel", "normal"), ("keep", "Only from the panel", "normal"),
                          ("both", "Remove everywhere", "danger")])
            if choice == "cancel":
                return
            if choice == "both":
                self.project.segments = [x for x in self.project.segments if x.clip.id not in ids]
                self.project.overlays = [x for x in self.project.overlays if x.clip.id not in ids]
                self._edited("delete")
                self._select("", "")
        self.library = [i for i in self.library if i not in ids]
        self._fill_bin()

    def _bin_clips(self) -> list[Clip]:
        return [self.clips[i] for i in self.list.selected_ids() if i in self.clips]

    def _insert_index(self) -> int:
        if not self.project.segments:
            return 0
        i, tl = self.project.segment_at(self.preview.position())
        seg = self.project.segments[i]
        return i if tl < seg.length / 2 else i + 1

    def add_selected_to_timeline(self, where: str = "end"):
        if self._busy():
            return
        self._place(self._bin_clips(), (where,))

    def add_selected_as_overlay(self):
        if self._busy() or not self.project.segments:
            return
        self._place(self._bin_clips(), ("overlay", self.preview.position()))

    def _add_audio(self, path: Path, kind: str, start: float | None = None, select: bool = True):
        dur, err = probe_audio(path)
        if err:
            inform(self, "Can't use this file", f"{path.name}\n\n{err}")
            return None
        if start is None:
            start = self.preview.position() if self.project.segments else 0.0
        a = AudioItem(path, dur, kind=kind, start=start if kind != "music" or start > 0.5 else 0.0)
        if kind == "voice":
            a.volume, a.fade_out, a.duck, a.loop = 1.0, 0.0, False, False
            a.duration = dur
        elif kind == "sound":
            a.volume, a.fade_out, a.duck, a.loop = 1.0, 0.0, False, False
            a.duration = dur
        self.project.audios.append(a)
        self._request_wave(path)
        if self.stack.currentIndex() == 2:
            self._edited("add.audio")
            if select:
                self._select("audio", a.id)
        return a

    def add_music(self):
        if self._busy() or not self.project.segments:
            return
        start = self.settings.value("music_folder", str(self.folder or ""))
        path, _ = QFileDialog.getOpenFileName(self, "Choose music or a sound", start, AUDIO_FILTER)
        if path:
            self.settings.setValue("music_folder", str(Path(path).parent))
            self._add_audio(Path(path), "music")

    def add_text(self):
        if self._busy() or not self.project.segments:
            return
        t = min(self.preview.position(), max(0.0, self.project.duration - 0.5))
        it = TextItem("Your text", start=t, duration=min(3.0, max(0.5, self.project.duration - t)),
                      anim_in="pop", anim_out="fade")
        self.project.texts.append(it)
        self._edited("add.text")
        self._select("text", it.id)
        self.inspector.text_page.edit.setFocus()
        self.inspector.text_page.edit.selectAll()

    # ============================================================ selection
    def _find(self, kind: str, id_: str):
        p = self.project
        coll = {"segment": p.segments, "transition": p.segments, "text": p.texts,
                "overlay": p.overlays, "audio": p.audios}.get(kind, [])
        return next((x for x in coll if x.id == id_), None)

    def _select(self, kind: str, id_: str):
        self.sel = (kind, id_)
        self.timeline.select(kind, id_)
        self.inspector.show_for(kind, self._find(kind, id_), self.project)

    def _on_timeline_select(self, kind, id_):
        self.sel = (kind, id_)
        self.inspector.show_for(kind, self._find(kind, id_), self.project)
        if kind == "segment":
            seg = self._find(kind, id_)
            if seg is not None:
                st = self.project.starts()[self.project.segments.index(seg)]
                t = self.preview.position()
                if not (st <= t < st + seg.length):
                    self._seek(st)

    def _on_scrub(self, t: float):
        self.preview.seek(t)

    def _seek(self, t: float):
        t = max(0.0, min(t, self.project.duration))
        self.preview.seek(t)
        self.timeline.set_playhead(t, follow=True)

    def _step(self, frames: int):
        i, _ = self.project.segment_at(self.preview.position())
        seg = self.project.segments[i] if self.project.segments else None
        fps = (seg.clip.v.nominal_fps if seg and seg.kind == "video" else 30) or 30
        self._seek(self.preview.position() + frames / fps)

    def _space(self):
        if self.project.segments and not self._busy():
            self.preview.toggle_play()

    # ============================================================== history
    def _reset_history(self):
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._last_snap = (self.project.snapshot(), self.sel)
        self._last_key = None
        self.dirty = False
        self._update_history_buttons()

    @staticmethod
    def _discrete(key: str) -> bool:
        """Actions that always get their own undo step (sliders and typing are merged)."""
        return (key.split(".")[0] in ("add", "delete", "split", "freeze", "duplicate", "order", "captions",
                                      "remove") or key.endswith((".trim", ".move", ".all")) or key == "trim.here")

    def _edited(self, key: str):
        now = time.monotonic()
        if self._last_snap is not None and (key != self._last_key or now - self._last_time > 1.2
                                            or self._discrete(key)):
            self.undo_stack.append(self._last_snap)
            del self.undo_stack[:-200]
            self.redo_stack.clear()
        self._last_key, self._last_time = key, now
        self._last_snap = (self.project.snapshot(), self.sel)
        self.dirty = True
        self._update_history_buttons()
        self._project_changed()

    def _restore(self, state):
        snap, sel = state
        self.project.restore(snap)
        self._last_snap = (self.project.snapshot(), sel)
        self._last_key = None
        self.timeline.set_project(self.project)
        self.preview.set_project(self.project)
        self._project_changed()
        self._select(*sel) if self._find(*sel) is not None else self._select("", "")
        self._update_history_buttons()

    def undo(self):
        if self._busy() or not self.undo_stack:
            return
        self.redo_stack.append((self.project.snapshot(), self.sel))
        self._restore(self.undo_stack.pop())

    def redo(self):
        if self._busy() or not self.redo_stack:
            return
        self.undo_stack.append((self.project.snapshot(), self.sel))
        self._restore(self.redo_stack.pop())

    def _update_history_buttons(self):
        self.undo_btn.setEnabled(bool(self.undo_stack) and not self._busy())
        self.redo_btn.setEnabled(bool(self.redo_stack) and not self._busy())

    # ================================================================ edits
    def _project_changed(self, reload_preview: bool = True):
        """Something in the project changed: refresh every view."""
        self.timeline.refresh()
        clips = [s.clip for s in self.project.segments if s.kind == "video"]
        unique = list({c.id: c for c in clips}.values())
        self.report = compat.analyze(unique) if unique else compat.CompatReport([])
        self._apply_canvas()
        if reload_preview:
            self.preview.refresh()
        self._update_summary()
        self._update_buttons()
        self.list.viewport().update()

    def _apply_canvas(self):
        if not self.project.segments:
            self.inspector.set_summary("Add videos or photos to see the output format.")
            return
        try:
            tgt = render.make_target(self.project)
        except Exception:
            return
        self.preview.set_canvas(tgt.width, tgt.height, tgt.cover, self.project.background)
        self.inspector.set_summary(self._target_summary(tgt))

    def _target_summary(self, tgt) -> str:
        bits = "10-bit" if tgt.ten_bit else "8-bit"
        hdr = " HDR" if tgt.transfer in render.HDR_TRANSFERS else ""
        fps = "original frame rates" if tgt.fps is None else f"{float(tgt.fps):.3g} fps"
        audio = ("sound copied unchanged" if tgt.audio_copy else
                 f"sound mixed to AAC {tgt.audio_bitrate.rstrip('k')} kb/s")
        return (f"Output: {tgt.width}×{tgt.height} · {codec_label(tgt.codec)} {bits}{hdr} · {fps} · "
                f"{audio} · {tgt.ext.lstrip('.').upper()}\nLength {fmt_duration(self.project.duration)}")

    def _on_inspector_changed(self, key: str):
        self._edited(key)
        if key.startswith("text.") or key.startswith("overlay."):
            self.timeline.update()

    def _on_timeline_editing(self):
        self.preview.refresh()
        self._update_summary()
        if self.sel[0] in ("segment", "text", "overlay", "audio"):
            obj = self._find(*self.sel)
            page = {"segment": self.inspector.segment_page, "text": self.inspector.text_page,
                    "overlay": self.inspector.overlay_page, "audio": self.inspector.audio_page}[self.sel[0]]
            if obj is not None and self.inspector.currentWidget() is page:
                page.load(obj, self.project)

    def _on_timeline_edited(self, key: str):
        self._edited(key)
        if self.sel[0]:
            self.inspector.show_for(self.sel[0], self._find(*self.sel), self.project)

    def _segment_at_playhead(self) -> tuple[int, float]:
        return self.project.segment_at(self.preview.position())

    def split_here(self):
        if self._busy() or not self.project.segments:
            return
        t = self.preview.position()
        kind, id_ = self.sel
        if kind in ("text", "overlay", "audio"):
            it = self._find(kind, id_)
            length = self.project.audio_length(it) if kind == "audio" else it.duration
            if it is not None and it.start + 0.1 < t < it.start + length - 0.1:
                import copy
                b = copy.deepcopy(it, {id(getattr(it, "clip", None)): getattr(it, "clip", None)})
                b.id = __import__("uuid").uuid4().hex
                cut = t - it.start
                it.duration = cut
                b.start = t
                b.duration = length - cut
                if hasattr(b, "src_in"):
                    b.src_in = it.src_in + cut
                {"text": self.project.texts, "overlay": self.project.overlays,
                 "audio": self.project.audios}[kind].append(b)
                self._edited("split")
                self._select(kind, b.id)
                return
        i, tl = self._segment_at_playhead()
        seg = self.project.segments[i]
        parts = seg.split(tl)
        if parts is None:
            self.preview.message.setText("Move the playhead inside a clip to split it.")
            return
        self.project.segments[i:i + 1] = list(parts)
        self._edited("split")
        self._select("segment", parts[1].id)

    def freeze_here(self):
        if self._busy() or not self.project.segments:
            return
        i, tl = self._segment_at_playhead()
        seg = self.project.segments[i]
        if seg.kind != "video":
            return
        fr = Segment(seg.clip, kind="freeze", still_at=seg.src_time(tl), still_len=2.0)
        parts = seg.split(tl)
        if parts:
            self.project.segments[i:i + 1] = [parts[0], fr, parts[1]]
        else:
            self.project.segments.insert(i + (1 if tl > seg.length / 2 else 0), fr)
        self._edited("freeze")
        self._select("segment", fr.id)

    def set_in_here(self):
        self._trim_here(True)

    def set_out_here(self):
        self._trim_here(False)

    def _trim_here(self, start: bool):
        if self._busy() or not self.project.segments:
            return
        i, tl = self._segment_at_playhead()
        seg = self.project.segments[i]
        if seg.is_still:
            return
        src = seg.src_time(tl)
        first = start != seg.edit.reverse         # which source end moves
        if first:
            if src < seg.src_out - 0.2:
                seg.edit.start = src
        else:
            if src > seg.src_in + 0.2:
                seg.edit.end = src
        self._edited("trim.here")
        st = self.project.starts()[i]
        self._seek(st if start else st + seg.length - 0.05)

    def delete_selected(self):
        if self._busy():
            return
        if self.list.hasFocus():
            self.remove_from_library()
            return
        kind, id_ = self.sel
        p = self.project
        if kind == "segment":
            p.segments = [s for s in p.segments if s.id != id_]
        elif kind == "transition":
            seg = self._find("segment", id_)
            if seg:
                seg.edit.transition = "none"
        elif kind == "text":
            p.texts = [x for x in p.texts if x.id != id_]
        elif kind == "overlay":
            p.overlays = [x for x in p.overlays if x.id != id_]
        elif kind == "audio":
            p.audios = [x for x in p.audios if x.id != id_]
        else:
            return
        self._edited("delete")
        self._select("", "")

    def duplicate_selected(self):
        if self._busy():
            return
        import copy
        import uuid
        kind, id_ = self.sel
        it = self._find(kind, id_)
        if it is None or kind == "transition":
            return
        if kind == "segment":
            i = self.project.segments.index(it)
            dup = it.clone()
            self.project.segments.insert(i + 1, dup)
        else:
            dup = copy.deepcopy(it, {id(getattr(it, "clip", None)): getattr(it, "clip", None)})
            dup.id = uuid.uuid4().hex
            dup.start = it.start + (self.project.audio_length(it) if kind == "audio" else it.duration)
            {"text": self.project.texts, "overlay": self.project.overlays,
             "audio": self.project.audios}[kind].append(dup)
        self._edited("duplicate")
        self._select(kind, dup.id)

    def transition_to_all(self):
        seg = self._find("segment", self.sel[1]) if self.sel[0] in ("transition", "segment") else None
        if seg is None:
            return
        for s in self.project.segments[:-1]:
            s.edit.transition, s.edit.transition_dur = seg.edit.transition, seg.edit.transition_dur
        self._edited("transition.all")

    def text_style_to_all(self):
        src = self._find("text", self.sel[1])
        if src is None:
            return
        for t in self.project.texts:
            if t is not src:
                for k in ("style", "position", "x", "y", "size", "color", "bold", "anim_in", "anim_out"):
                    setattr(t, k, getattr(src, k))
        self._edited("text.all")

    # ======================================================== voice-over
    def toggle_voiceover(self):
        if self._busy() or not self.project.segments:
            return
        if self.recorder and self.recorder.recording():
            self.recorder.stop()
            self.preview.pause()
            self.rec_btn.setText("● Voice-over")
            return
        if not VoiceRecorder.has_microphone():
            inform(self, "No microphone", "Windows reports no microphone. Connect one and try again.")
            return
        if self.recorder is None:
            self.recorder = VoiceRecorder(self)
            self.recorder.finished.connect(self._voice_done)
            self.recorder.failed.connect(lambda msg: inform(self, "Recording failed", msg))
        self.rec_start = self.preview.position()
        self.recorder.start()
        self.preview.play()
        self.rec_btn.setText("■ Stop recording")

    def _voice_done(self, path):
        self.rec_btn.setText("● Voice-over")
        if path is None:
            inform(self, "Recording failed", "No sound was recorded.")
            return
        dur, err = probe_audio(path)
        if err or dur < 0.2:
            inform(self, "Recording failed", err or "The recording is empty.")
            return
        a = AudioItem(path, dur, kind="voice", start=self.rec_start, duration=dur, volume=1.0,
                      fade_out=0.0, duck=False, loop=False)
        self.project.audios.append(a)
        self._request_wave(path)
        self._edited("add.voice")
        self._select("audio", a.id)

    # ======================================================= auto captions
    def auto_captions(self):
        if self._busy() or not self.project.segments:
            return
        if not captions.available():
            inform(self, "Auto captions not available", "The speech recogniser (whisper.cpp) is missing "
                   "from this installation.")
            return
        if not any(s.has_audio and not s.edit.muted for s in self.project.segments):
            inform(self, "No speech to caption", "The clips on the timeline have no sound.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Auto captions")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        t = QLabel("Turn speech into subtitles")
        t.setObjectName("dialogTitle")
        lay.addWidget(t)
        info = QLabel("Speech is recognised on this PC with OpenAI's Whisper model (whisper.cpp) — nothing "
                      "is uploaded. The model is downloaded once. Accuracy for Georgian is lower than for "
                      "English or Russian; you can correct every caption afterwards.")
        info.setWordWrap(True)
        info.setObjectName("muted")
        lay.addWidget(info)
        lang = QComboBox()
        for k, v in captions.LANGUAGES.items():
            lang.addItem(v, k)
        lang.setCurrentIndex(max(0, lang.findData(self.settings.value("cc_lang", "auto"))))
        model = QComboBox()
        for k, (_, mb, label) in captions.MODELS.items():
            model.addItem(label + ("  ✓ downloaded" if captions.has_model(k) else ""), k)
        model.setCurrentIndex(max(0, model.findData(self.settings.value("cc_model", "small"))))
        replace = QCheckBox("Replace existing subtitles")
        replace.setChecked(True)
        for label, w in (("Language", lang), ("Quality", model)):
            row = QHBoxLayout()
            lab = QLabel(label)
            lab.setMinimumWidth(80)
            row.addWidget(lab)
            row.addWidget(w, 1)
            lay.addLayout(row)
        lay.addWidget(replace)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel, go = QPushButton("Cancel"), QPushButton("Create captions")
        go.setObjectName("primary")
        cancel.clicked.connect(dlg.reject)
        go.clicked.connect(dlg.accept)
        row.addWidget(cancel)
        row.addWidget(go)
        lay.addLayout(row)
        dlg.setMinimumWidth(520)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        size, language = model.currentData(), lang.currentData()
        self.settings.setValue("cc_lang", language)
        self.settings.setValue("cc_model", size)
        if not captions.has_model(size):
            mb = captions.MODELS[size][1]
            if ask(self, "Download speech model?",
                   f"The “{size}” speech model ({mb} MB) will be downloaded once from Hugging Face "
                   "(huggingface.co/ggerganov/whisper.cpp) and stored on this PC.",
                   [("cancel", "Cancel", "normal"), ("go", "Download", "primary")]) != "go":
                return
        project = self.project.copy()
        repl = replace.isChecked()

        def job(cb, cancel_ev, size=size, language=language, project=project):
            if not captions.has_model(size):
                captions.download_model(size, lambda f: cb(f * 0.3, None, "Downloading speech model"), cancel_ev)
            return captions.transcribe(project, size, language,
                                       lambda f, s: cb(0.3 + 0.7 * f if not captions.has_model(size) else f, None, s),
                                       cancel_ev)

        def done(cues):
            if repl:
                self.project.texts = [x for x in self.project.texts if x.style != "subtitle"]
            for c in cues:
                self.project.texts.append(TextItem(c.text, start=c.start, duration=max(0.3, c.end - c.start),
                                                   style="subtitle", size=4.6, bold=False))
            self._edited("captions")
            inform(self, "Captions added", f"{len(cues)} subtitle{'s' if len(cues) != 1 else ''} were added "
                   "to the text track. Click one to correct the wording, style or timing.")

        self._run_job(job, done, "Creating captions")

    # =========================================================== projects
    def _confirm_discard(self) -> bool:
        if not self.dirty or not self.project.edited:
            return True
        choice = ask(self, "Save your edit?", "The current edit has unsaved changes.",
                     [("cancel", "Cancel", "normal"), ("discard", "Don't save", "danger"),
                      ("save", "Save project…", "primary")])
        if choice == "save":
            return self.save_project()
        return choice == "discard"

    def save_project(self) -> bool:
        if not self.project.segments or self._busy():
            return False
        path = self.project_path
        if path is None:
            start = str((self.folder or Path.home()) / f"{(self.folder or Path('Edit')).name}{projectio.EXTENSION}")
            p, _ = QFileDialog.getSaveFileName(self, "Save project", start, PROJECT_FILTER)
            if not p:
                return False
            path = Path(p)
            if path.suffix.lower() != projectio.EXTENSION:
                path = path.with_name(path.name + projectio.EXTENSION)
        try:
            projectio.save(self.project, path, self.folder)
        except OSError as exc:
            inform(self, "Could not save", str(exc))
            return False
        self.project_path = path
        self.dirty = False
        self.preview.message.setText(f"Saved {path.name}")
        return True

    def open_project(self):
        if self._busy() or not self._confirm_discard():
            return
        start = self.settings.value("project_folder", str(self.folder or ""))
        p, _ = QFileDialog.getOpenFileName(self, "Open project", start, PROJECT_FILTER)
        if not p:
            return
        self.settings.setValue("project_folder", str(Path(p).parent))
        self.load_project(Path(p))

    def load_project(self, path: Path):
        try:
            proj, folder, missing = projectio.load(path, load_photo)
        except (OSError, ValueError, KeyError) as exc:
            inform(self, "Could not open project", str(exc))
            return
        self._reset_state()
        self.project = proj
        self.project_path = path
        self.folder = folder
        self.folder_label.setText(f"🎬  {path.name}" + (f"   ·   📁 {folder}" if folder else ""))
        for c in [s.clip for s in proj.segments] + [o.clip for o in proj.overlays]:
            if c.id not in self.clips:
                self.clips[c.id] = c
                self.library.append(c.id)
        self._show_editor()
        self._reset_history()
        if missing:
            inform(self, "Some files are missing", "These files were moved or deleted:\n\n"
                   + "\n".join(missing[:12]) + ("\n…" if len(missing) > 12 else ""))

    # =============================================================== status
    def _update_summary(self):
        segs = self.project.segments
        clips = [s.clip for s in segs]
        ok = [c for c in clips if not c.error]
        size = sum(c.size for c in {c.id: c for c in ok}.values())
        n = len(segs)
        self.summary_label.setText(f"{n} clip{'s' if n != 1 else ''}   ·   {fmt_duration(self.project.duration)} "
                                   f"total   ·   {fmt_size(size)} of footage")
        broken = [c for c in clips if c.error]
        edited = self.project.edited
        link = f"&nbsp;<a style='color:{theme.ACCENT}; text-decoration:none' href='#'>Details</a>"
        if broken:
            msg = (f"<span style='color:{theme.ERROR}'>●</span>&nbsp; {len(broken)} file(s) can't be read — "
                   f"delete them from the timeline to continue")
        elif n < 1:
            msg = (f"<span style='color:{theme.MUTED}'>●</span>&nbsp; Add videos with “＋ Add media” (Ctrl+I) "
                   "or drag them onto the timeline")
        elif n < 2 and not edited:
            msg = f"<span style='color:{theme.MUTED}'>●</span>&nbsp; Add at least two clips to merge, or edit a clip"
        elif edited:
            preset = PRESETS[self.project.preset]
            if preset.key == "original":
                msg = (f"<span style='color:{theme.ACCENT}'>●</span>&nbsp; Edited — untouched footage is copied "
                       "losslessly, only edited parts are re-encoded")
            else:
                msg = f"<span style='color:{theme.ACCENT}'>●</span>&nbsp; Edited — exporting as {preset.label}"
        elif self.report.blockers:
            msg = f"<span style='color:{theme.ERROR}'>●</span>&nbsp; Clips differ — lossless merge not possible {link}"
        elif self.report.warnings:
            msg = f"<span style='color:{theme.WARN}'>●</span>&nbsp; Lossless merge possible, with notes {link}"
        else:
            msg = f"<span style='color:{theme.OK}'>●</span>&nbsp; Ready for lossless merge (stream copy)"
        self.compat_label.setText(msg)
        self.merge_btn.setText("Export Video" if edited else "Merge Videos")
        self.bin_info.setText("Drag a clip onto the timeline, or double-click to add it at the end. "
                              "✓ = already used." if self.library else
                              "Drop videos and photos here, or click “+ Import”.")
        self.setWindowTitle(f"{APP_NAME}" + (f" — {self.project_path.stem}" if self.project_path else "")
                            + (" •" if self.dirty and self.project.edited else ""))

    def _update_buttons(self):
        busy = self._busy()
        segs = self.project.segments
        enough = len(segs) >= 2 or (len(segs) == 1 and self.project.edited)
        self.merge_btn.setEnabled(not busy and enough and not any(s.clip.error for s in segs))
        self.open_btn.setEnabled(not busy)
        self.open_proj_btn.setEnabled(not busy)
        self.save_btn.setEnabled(not busy and bool(segs))
        self.recursive_cb.setEnabled(not busy)
        self.editor.setEnabled(not busy)
        self._update_history_buttons()

    def show_compat_details(self):
        r = self.report
        if r.blockers:
            ask(self, "Lossless merge not possible",
                "These clips can't be joined without re-encoding:", [("ok", "Close", "primary")], r.issues)
        elif r.warnings:
            ask(self, "Lossless merge possible",
                "All clips can be joined with stream copy (no quality loss). Please note:",
                [("ok", "Close", "primary")], r.issues)

    # ================================================================ merge
    @staticmethod
    def _format_label(c: Clip) -> str:
        v = c.v
        w, h = v.display_size
        bits = " 10-bit" if ("10" in v.pix_fmt or "12" in v.pix_fmt) else ""
        hdr = " HDR" if v.color_transfer in ("arib-std-b67", "smpte2084") else ""
        return f"{w}×{h} {codec_label(v.codec)}{bits}{hdr}"

    def start_merge(self):
        self.preview.pause()
        if self.project.edited:
            self.start_export()
            return
        clips = self.ordered_clips()
        if len(clips) < 2 or any(c.error for c in clips):
            return
        report = compat.analyze(clips)
        target = None
        convert_ids: set[str] = set()
        reference = None
        fps, method = None, merger.DUPLICATE

        if report.blockers:
            fit = compat.fit_plan(clips)
            choice = "all"
            if fit.feasible and fit.convert:
                by_id = {c.id: c for c in clips}
                lines = "\n".join(f"{by_id[i].name} — {', '.join(why)}" for i, why in fit.convert.items())
                n = len(fit.convert)
                these = "these" if n != 1 else "this"
                choice = ask(
                    self, f"{n} clip{'s don’t' if n != 1 else ' doesn’t'} match the others",
                    f"Most of your footage is {self._format_label(fit.reference)}. "
                    f"{'These clips are' if n != 1 else 'This clip is'} stored differently, so "
                    f"{'they' if n != 1 else 'it'} can't be joined as {'they are' if n != 1 else 'it is'}:",
                    [("cancel", "Cancel", "normal"), ("all", "Re-encode everything…", "normal"),
                     ("fit", f"Re-encode only {these} {n}", "primary")],
                    [compat.Issue(compat.WARNING, "Will be re-encoded to match", lines)],
                    footer=f"Recommended: only {these} {n} {'clips are' if n != 1 else 'clip is'} re-encoded "
                           "(very high quality, same codec, bit depth and HDR as the rest), turned upright "
                           "and fitted to the same frame. All other clips are copied bit-exact, and all "
                           "audio, including Apple spatial audio, is copied unchanged.")
                if choice == "fit":
                    convert_ids, reference = set(fit.convert), fit.reference
                elif choice != "all":
                    return
            else:
                choice = ask(
                    self, "These clips can't be merged losslessly",
                    "Joining files without re-encoding requires every clip to share the same codec, "
                    "resolution, pixel format and audio format. The following differences were found:",
                    [("cancel", "Cancel", "normal"), ("all", "Re-encode instead…", "primary")],
                    report.issues,
                    footer=(fit.problem + "\n" if fit.problem else "")
                    + "Tip: delete the clips marked in red from the timeline to merge the rest losslessly.")
                if choice != "all":
                    return
            if choice == "all":
                target = compat.encode_target(clips)
                if ask(self, "Re-encode all clips?",
                       "Re-encoding converts every clip to one common format. It uses very high quality "
                       "settings, but it is NOT lossless: the video is compressed again, it is much "
                       "slower than a lossless merge and the file size may change.\n\nOutput format:\n\n"
                       + target.describe(),
                       [("cancel", "Cancel", "normal"), ("go", "Re-encode and merge", "primary")]) != "go":
                    return

        if target is None and len(compat.frame_rate_groups(clips)) > 1:
            plan = compat.conform_plan(clips)
            others = [i for i in report.warnings if i.title != compat.FPS_ISSUE]
            dlg = FrameRateDialog(self, clips, plan, others,
                                  self.settings.value("fps_choice", FrameRateDialog.KEEP),
                                  plan.reference.v.codec in merger.CONFORM_CODECS)
            choice = dlg.exec_choice()
            if choice is None:
                return
            self.settings.setValue("fps_choice", choice)
            if choice != FrameRateDialog.KEEP:
                fps, method = plan.fps, choice
                fit = compat.fit_plan(clips, fps)
                convert_ids, reference = set(fit.convert), fit.reference
        elif target is None and not convert_ids and report.warnings:
            if ask(self, "Ready to merge losslessly",
                   "All clips will be joined with stream copy — no re-encoding, no quality loss. "
                   "A few things to be aware of:",
                   [("cancel", "Cancel", "normal"), ("go", "Merge losslessly", "primary")],
                   report.issues) != "go":
                return

        out = self._ask_output(clips, lossless=target is None)
        if out is None:
            return
        temp_factor = 1.02
        if target is not None or convert_ids or merger.needs_header_embedding(clips):
            temp_factor = 2.5
        if not self._check_space(clips, out, temp_factor):
            return
        smart = (convert_ids, fps, method, reference) if convert_ids else None
        self._run_merge(clips, out, target, smart)

    def start_export(self):
        segs = self.project.segments
        if not segs or any(s.clip.error for s in segs):
            return
        project = self.project.copy()
        try:
            tgt = render.make_target(project)
        except Exception as exc:
            inform(self, "Can't export", str(exc))
            return
        copyable = [s for s in project.segments if not render._needs_encode(s, tgt)]
        edited = [s for s in project.segments if render._needs_encode(s, tgt)]
        trans = sum(1 for i in range(len(segs)) if project.transition_after(i) > 0)
        lines = []
        if tgt.allow_copy:
            untouched = [s for s in copyable if not s.trimmed]
            trimmed = [s for s in copyable if s.trimmed]
            if untouched:
                lines.append(f"• {len(untouched)} clip{'s' if len(untouched) != 1 else ''} copied bit-exact")
            if trimmed:
                lines.append(f"• {len(trimmed)} trimmed clip{'s' if len(trimmed) != 1 else ''}: copied bit-exact "
                             "except about a second at each cut")
            if edited:
                secs = sum(s.length for s in edited)
                lines.append(f"• {len(edited)} edited clip{'s' if len(edited) != 1 else ''}, photo"
                             f"{'s' if len(edited) != 1 else ''} or freeze frame{'s' if len(edited) != 1 else ''} "
                             f"({fmt_duration(secs)}) rendered at very high quality")
            if project.texts or project.overlays:
                lines.append("• Seconds covered by text or picture-in-picture are rendered; the rest stays copied")
        else:
            lines.append(f"• Everything is rendered as {PRESETS[project.preset].label}")
        if trans:
            lines.append(f"• {trans} transition{'s' if trans != 1 else ''} rendered")
        lines.append("• Sound copied unchanged" if tgt.audio_copy else "• Sound mixed and encoded to AAC")
        text = (f"{self._target_summary(tgt)}\n\n" + "\n".join(lines)
                + ("\n\n" + "\n".join(tgt.notes) if tgt.notes else ""))
        if ask(self, "Export edited video", text,
               [("cancel", "Cancel", "normal"), ("go", "Choose file and export…", "primary")]) != "go":
            return
        clips = [s.clip for s in project.segments]
        out = self._ask_output(clips, lossless=tgt.audio_copy, default_ext=tgt.ext, export=True)
        if out is None:
            return
        if not self._check_space(list({c.id: c for c in clips}.values()), out, 1.6):
            return
        texts = {t.id: text_render(t, tgt.width, tgt.height) for t in project.texts if t.active}

        def job(cb, cancel, project=project, out=out, texts=texts, tgt=tgt):
            return render.render_project(project, out, cb, cancel, texts, tgt)

        self._run_merge(clips, out, None, None, job=job)

    def _ask_output(self, clips: list[Clip], lossless: bool, default_ext: str | None = None,
                    export: bool = False) -> Path | None:
        default_ext = default_ext or (compat.suggested_extension(clips) if lossless else ".mp4")
        formats = list(compat.OUTPUT_FORMATS)
        if default_ext not in [e for e, _ in formats]:
            formats.insert(0, (default_ext, f"{default_ext.upper().lstrip('.')} video"))
        if not lossless or export:
            formats = [f for f in formats if f[0] in (".mp4", ".mov", ".mkv")]
        formats.sort(key=lambda f: f[0] != default_ext)
        filters = [f"{label} (*{ext})" for ext, label in formats]
        first = next((c for c in clips if not c.is_image), clips[0])
        stamp = first.recorded.astimezone().strftime("%Y-%m-%d %H-%M") if first.recorded else "video"
        folder = self.folder or first.path.parent
        word = "edited" if export else "merged"
        suggestion = folder / f"{folder.name} – {word} {stamp}{default_ext}"
        while True:
            path, selected = QFileDialog.getSaveFileName(
                self, "Save video as", str(suggestion), ";;".join(filters), filters[0],
                QFileDialog.Option.DontConfirmOverwrite)
            if not path:
                return None
            out = Path(path)
            chosen_ext = formats[filters.index(selected)][0] if selected in filters else default_ext
            if out.suffix.lower() not in {e for e, _ in formats}:
                out = out.with_name(out.name + chosen_ext)
            suggestion = out
            if _norm(out) in {_norm(c.path) for c in clips}:
                inform(self, "Choose a different name",
                       "The video can't replace one of the files it is made from.")
                continue
            if lossless:
                problem = next((p for p in (compat.container_problem(c, out.suffix) for c in clips[:1]) if p), None)
                if problem:
                    alt = compat.compatible_container(clips[0])
                    name = alt.upper().lstrip(".")
                    choice = ask(self, "This format can't hold these clips losslessly",
                                 problem + f"\n\n{name} can store them without re-encoding.",
                                 [("back", "Choose again", "normal"), ("alt", f"Save as {name}", "primary")])
                    if choice == "alt":
                        out = out.with_suffix(alt)
                        suggestion = out
                    else:
                        continue
            if out.exists():
                choice = ask(self, "File already exists",
                             f"“{out.name}” already exists in this folder.\n\nDo you want to replace it? "
                             "The existing file is only replaced after the export finishes successfully.",
                             [("back", "Choose another name", "normal"), ("replace", "Replace", "danger")])
                if choice != "replace":
                    continue
            return out

    def _check_space(self, clips, out: Path, factor: float) -> bool:
        need = sum(c.size for c in clips) * factor
        try:
            free = shutil.disk_usage(out.parent).free
        except OSError:
            return True
        if free >= need:
            return True
        return ask(self, "Not enough free space?",
                   f"The video needs about {fmt_size(need)}, but only {fmt_size(free)} is free on "
                   f"this drive. Saving will fail if the disk fills up.",
                   [("cancel", "Cancel", "normal"), ("go", "Try anyway", "danger")]) == "go"

    # ---------------------------------------------------------- job runner
    def _run_job(self, job, done, title):
        self.job_done = done
        self._run_merge([], Path("."), None, None, job=job, status=title)

    def _run_merge(self, clips, out: Path, target, smart=None, job=None, status="Starting…"):
        self.preview.pause()
        self.merge_thread = MergeThread(clips, out, target, smart, job=job)
        self.merge_thread.progress.connect(self._on_progress)
        self.merge_thread.succeeded.connect(self._on_success)
        self.merge_thread.failed.connect(self._on_failed)
        self.merge_thread.cancelled.connect(self._on_cancelled)
        self.merge_thread.finished.connect(self._on_thread_finished)
        self.merge_started = time.monotonic()
        self.status_box.hide()
        self.progress_box.show()
        self.progress_bar.setValue(0)
        self.progress_label.setText(status)
        self.merge_btn.hide()
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")
        self.cancel_btn.show()
        self._update_buttons()
        self.merge_thread.start()

    def _on_progress(self, fraction, speed, status):
        self.progress_bar.setValue(int(fraction * 1000))
        parts = [f"{status}", f"{fraction * 100:.0f}%"]
        if speed:
            parts.append(f"{speed:.1f}× speed")
        elapsed = time.monotonic() - self.merge_started
        if 0.02 < fraction < 1 and elapsed > 2:
            parts.append(f"about {fmt_duration(elapsed / fraction - elapsed)} left")
        self.progress_label.setText("   ·   ".join(parts))

    def cancel_merge(self):
        if self.merge_thread:
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling…")
            self.merge_thread.cancel()

    def _reset_after_merge(self):
        self.progress_box.hide()
        self.cancel_btn.hide()
        self.status_box.show()
        self.merge_btn.show()

    def _on_thread_finished(self):
        self.merge_thread = None
        self._reset_after_merge()
        self._update_buttons()

    def _on_success(self, result):
        self._reset_after_merge()
        if self.job_done is not None:
            cb, self.job_done = self.job_done, None
            cb(result)
            return
        kind = "Lossless — streams copied without re-encoding." if result.lossless else ""
        notes = ("\n" + "\n".join(result.notes)) if result.notes else ""
        choice = ask(self, "Merge complete" if not self.project.edited else "Export complete",
                     f"{result.output.name}\n\n{fmt_duration(result.duration)}   ·   {fmt_size(result.size)}\n"
                     f"{kind}{notes}",
                     [("show", "Show in folder", "normal"), ("ok", "Done", "primary")])
        if choice == "show":
            subprocess.Popen(["explorer", "/select,", str(result.output)])

    def _on_failed(self, message):
        self._reset_after_merge()
        if self.job_done is not None:
            self.job_done = None
            inform(self, "That didn't work", message[-1500:])
            return
        ask(self, "Export failed" if self.project.edited else "Merge failed",
            "FFmpeg reported an error. No output file was written and your original clips are "
            "untouched.\n\n" + message[-1500:]
            + "\n\nIf the error mentions the container or codec, try saving as MKV.",
            [("ok", "Close", "primary")])

    def _on_cancelled(self):
        self._reset_after_merge()
        self.job_done = None
        self.compat_label.setText(f"<span style='color:{theme.MUTED}'>●</span>&nbsp; Cancelled — "
                                  "no file was written")

    # ======================================================= window events
    def dragEnterEvent(self, e):
        if not self._busy() and e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.drop_zone.setProperty("active", True)
            self.drop_zone.style().polish(self.drop_zone)

    def dragLeaveEvent(self, e):
        self.drop_zone.setProperty("active", False)
        self.drop_zone.style().polish(self.drop_zone)

    def dropEvent(self, e):
        self.dragLeaveEvent(e)
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        if paths[0].suffix.lower() == projectio.EXTENSION:
            if self._confirm_discard():
                self.load_project(paths[0])
            return
        in_editor = self.stack.currentIndex() == 2
        if not in_editor and len(paths) == 1 and paths[0].is_dir():
            if self._confirm_discard():
                self.load_folder(paths[0])          # a folder on the start page: the classic merge
            return
        on_bin = in_editor and self.list.rect().contains(self.list.mapFrom(self, e.position().toPoint()))
        self.add_paths(paths, ("library",) if on_bin else ("end",))

    def closeEvent(self, e):
        if self.merge_thread:
            choice = ask(self, "Export in progress",
                         "Stop and close? The unfinished file will be deleted.",
                         [("stay", "Keep going", "normal"), ("quit", "Stop and close", "danger")])
            if choice != "quit":
                e.ignore()
                return
            self.merge_thread.cancel()
            self.merge_thread.wait(15000)
        elif not self._confirm_discard():
            e.ignore()
            return
        if self.recorder and self.recorder.recording():
            self.recorder.stop()
        self.preview.shutdown()
        self.generation += 1
        self.probe_pool.clear()
        self.thumb_pool.clear()
        e.accept()

    # convenience for tests -------------------------------------------------
    def current_time_label(self) -> str:
        return fmt_time(self.preview.position())
