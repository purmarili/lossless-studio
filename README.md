# Lossless Studio

A Windows desktop app that joins a folder of video recordings into one file **without re-encoding**: it uses FFmpeg stream copy (`-c copy`), so the result keeps the original resolution, frame rate, codecs, bit depth and audio quality.

It is also a CapCut-style editor with:

- a timeline
- trimming and splitting
- text, picture-in-picture, transitions and effects
- music, voice-over and offline auto captions

Anything you don't touch is still copied bit-exact; only the frames you change are re-encoded.

## Download

**[⬇ Download for Windows (64-bit)](https://github.com/purmarili/lossless-studio/releases/latest/download/LosslessStudio-win64.zip)**. You can also see all versions on the [Releases page](https://github.com/purmarili/lossless-studio/releases).

1. Unzip the file anywhere, for example to `Documents\LosslessStudio`.
2. Run `LosslessStudio.exe`. Nothing needs to be installed, and FFmpeg is included.
3. The app isn't code-signed, so Windows SmartScreen may say *"Windows protected your PC"*. Click **More info › Run anyway**.

It needs Windows 10 or 11, 64-bit. Keep the whole folder together: the exe needs the `_internal`, `ffmpeg` and `whisper` folders next to it.

## Using it

1. Run `LosslessStudio.exe` (or `python main.py`).
2. Click **Choose folder…**, or drop a folder onto the window. Tick **Include subfolders** for camera layouts such as `DCIM\100GOPRO`.
3. The app finds all videos, sorts them by when they were recorded, and puts them on the timeline.
4. Optionally edit (see below).
5. Click **Merge Videos** (or **Export Video** once something is edited) and choose where to save the result. The bar shows progress, speed and time left, and you can **Cancel** at any time.

## Works with footage from (almost) any camera

| Source | What it records | Notes |
|---|---|---|
| **iPhone** | HEVC 10-bit HDR (HLG / Dolby Vision), spatial audio, Auto FPS | Rotation, spatial audio (MOV) and mixed frame rates are kept |
| **Android** (Samsung, Pixel, Xiaomi …) | H.264 / HEVC, HDR10 (PQ), portrait or upside-down | PQ HDR stays HDR; photos and text placed in HDR video get the correct brightness (SDR white at 203 nits) |
| **GoPro HERO** | HEVC / H.264 MP4 with telemetry and timecode tracks, split into chapters (GX01…, GX02…) | Chapters are put in order and merged losslessly; `.LRV` / `.THM` side files are ignored |
| **Insta360 X / ONE** (360°) | Raw two-lens `.insv` (one file, or a `_00_` + `_10_` pair) | A dialog asks where to look (left/right, up/down, tilt, zoom), or makes a *tiny planet*, and converts it into a normal video. This is a quick stitch; Insta360 Studio exports stitch seams more precisely, and those MP4s work here too |
| **Insta360 Ace / GO**, DJI, action cams | H.264 / HEVC MP4 | Same as phones |
| **GoPro MAX** `.360` | Proprietary 360° format | Export it with the free GoPro Player first; the app explains this when you add one |
| **Camcorders** (Sony, Panasonic, Canon AVCHD) | 1080i H.264 + AC-3 in `.MTS` / `.M2TS` | Merged losslessly; smart-cut trimming stays interlaced; deinterlaced (bwdif) only when re-encoded to a progressive format |
| **Sony / Canon mirrorless** (XAVC S) | H.264 4:2:2 10-bit + PCM audio | Kept at 4:2:2 10-bit; saved as MOV because MP4 can't hold PCM |
| Webcams, screen recorders (OBS), WhatsApp, Telegram | MJPEG/AVI, VP9/AV1 + Opus WebM/MKV, small H.264 MP4 | Read and converted as needed |
| Old phones, DVDs, Windows | H.263 + AMR `.3gp`, MPEG-2 `.VOB` / `.MPG`, `.WMV` | Read and converted |
| Editing exports | ProRes, DNxHD `.mov` | Read and converted |

When clips from different devices are merged, they are made compatible losslessly wherever possible. FFmpeg's join can't mix different *time bases* (devices store timestamps differently, and mixing them corrupts the timing), so mismatched clips are first rewrapped without re-encoding.

## Adding clips one by one

You don't need a folder. On the start screen click **＋ Add videos one by one…** or **Empty timeline**, then use any of these. Each addition is one undo step.

| How | Where the clips go |
|---|---|
| **＋ Add media** button or **Ctrl+I** | End of the timeline |
| Arrow next to the button › *Insert at the playhead* (**Ctrl+Shift+I**) | Before or after the clip under the playhead (whichever half the playhead is in) |
| Arrow › *Add a whole folder…* | End of the timeline, in recording order |
| Arrow › *Import to the media panel only…*, or **+ Import** | Media panel only |
| Drag files from Explorer onto the **timeline** | Exactly where you drop them: between clips on the video track, as picture-in-picture on the overlay track, or as sound on the audio track. A coloured marker shows the spot |
| Drag files onto the media panel | Media panel only |
| Drag files anywhere else on the window | End of the timeline |
| Drag tiles from the **media panel** onto the timeline | Like files from Explorer (several at once work) |
| Double-click a tile | End of the timeline |
| Right-click a tile | *Add to the end*, *Insert at the playhead*, *Add as picture-in-picture*, *Show in folder*, *Remove from media panel* |

- **Order:** when you add several files at once, they're put in recording order. Untick *Put several new clips in recording order* in the arrow menu to keep your own selection order.
- **Speed:** files are read in the background, so adding many doesn't freeze the app.
- **Mixed files:** videos, photos and sound files can be added together, and each goes to the right track.
- **✓ on a tile** means that clip is already on the timeline.
- **Lossless merge:** if you only add clips and edit nothing, the button stays **Merge Videos** and the result is a lossless merge.

## The editor

The window has four parts:

- **Media** (left): every video and photo you can use. **+ Import** adds videos, photos (EXIF orientation is applied) or music from anywhere. You can also drop files on the window. Double-click a tile, or press **Add to timeline**, to insert it after the clip at the playhead. **Add as overlay** puts it on top as picture-in-picture.
- **Preview** (centre): plays the whole edit, including captions, overlays, music and fades. Space plays and pauses; click the picture to toggle. When paused, the exact export colours are shown.
- **Edit panel** (right): controls for whatever is selected on the timeline. With nothing selected, it shows the export settings.
- **Timeline** (bottom), with four tracks:
  - **Text**: captions and titles.
  - **Overlay**: picture-in-picture.
  - **Video**: the main track.
  - **Audio**: music, voice-overs and sounds.

**Timeline controls**

| Action | How |
|---|---|
| Trim a clip | Drag its edges, or press **I** / **O** at the playhead |
| Reorder | Drag a clip |
| Move or trim text, overlays, sounds | Drag them; they snap to the playhead and to clip edges |
| Add a transition | Click a ◆ between two clips |
| Zoom | Ctrl + wheel. Double-click an empty spot to fit the whole video |
| Context menu | Right-click a clip or item |

**Keyboard shortcuts**

| Key | Action |
|---|---|
| S | Split at the playhead |
| F | Insert a freeze frame |
| T | Add text |
| Del | Delete the selection |
| ← / → | Step one frame |
| Home / End | Jump to start / end |
| Ctrl+Z / Ctrl+Y | Undo / redo (covers every edit) |
| Ctrl+S | Save the project |

**Save project** writes a small `.lsproj` file (older `.lvmproj` files open too), so you can continue an edit later. It only references your media, and your files are never modified.

### What you can edit

| Selection | Controls | Effect on quality |
|---|---|---|
| **Clip** › Clip | Speed 0.25× to 4× (pitch kept), play backwards, photo or freeze-frame length | That clip is re-encoded |
| **Clip** › Look | Filters (Vivid, Warm, Cool, Black & white, Faded, Dramatic, Golden hour), brightness, contrast, saturation, warmth, rotate, mirror. HDR stays HDR | That clip is re-encoded |
| **Clip** › Effects | Sharpen, vignette, blur, stabilize shaky footage, fade from/to black | That clip is re-encoded |
| **Clip** › Zoom | Zoom and framing, or a Ken Burns animation from one framing to another (presets: slow zoom in/out, pan). Photos get a gentle zoom by default | That clip is re-encoded |
| **Clip** › Audio | Volume, mute, reduce background noise, fade in/out | Picture stays lossless; the soundtrack is mixed to AAC |
| **Transition** (◆) | 13 transitions, 0.2–2 s; **Use for all clips** | Only the overlap is re-encoded |
| **Text** | Any language (ქართული, Русский, emoji); 6 styles (Classic, Box, Outline, Neon, Subtitle, Headline); colour, size, position; animations in (fade, pop, slide up, slide in, typewriter) and out | Only the seconds it covers are re-encoded |
| **Overlay** | Picture-in-picture size, position (corner presets), opacity, volume | Only the seconds it covers are re-encoded |
| **Audio item** | Volume, fades, loop to the end, **ducking** (music gets quieter while people talk) | Soundtrack mixed to AAC |
| Nothing selected | Export format, frame shape (9:16, 1:1, 4:5), black bars or a blurred-video background, fade in/out of the whole video | See below |

**Toolbar**

- **✂ Split**, **🗑 Delete**, **⧉ Duplicate**, **❄ Freeze** (holds the current frame for 2 s).
- **T Text**, **♪ Music**.
- **● Voice-over**: records your microphone while the video plays.
- **CC Auto captions**: turns speech into subtitles **offline** with whisper.cpp. Nothing is uploaded. A speech model (148 MB to 1.5 GB) is downloaded once, after you confirm. Voice-activity detection skips silence and noise. Accuracy for Georgian is clearly lower than for English or Russian: use the "Accurate" or "Best" model and set the language. Every caption can be corrected afterwards.

### Export formats

| Format | Use it for |
|---|---|
| **Original quality** (default) | Archiving. Untouched footage is copied bit-exact, trimmed clips are smart-cut, and edited parts are re-encoded in the same codec, bit depth and HDR as the rest (x265/x264 CRF 16). |
| **4K HEVC — smaller file** | Everything re-encoded to HEVC CRF 21 with HDR kept. Roughly half the size. |
| **1080p — plays everywhere** | H.264 with normal (SDR) colours. HDR footage is tone-mapped so it doesn't look washed out on non-HDR screens. Good for WhatsApp, Instagram, TVs and old PCs. |
| **720p — small file** | H.264 SDR at 30 fps, for sending by message. |

**Frame shape** can turn the video into 9:16 (Reels, TikTok, Shorts), 1:1 or 4:5. Clips are cropped to fill the frame without being enlarged, so 4K landscape becomes 1216×2160 vertical. Combine it with 1080p to get the standard 1080×1920.

### Smart cut: how trimming stays lossless

Video is stored in groups of frames that start with a keyframe (on iPhones about one per second). The app copies every group fully inside your trim packet for packet, and re-encodes only the few frames between your cut point and the next keyframe, matched to the original format. iPhone low-light (Auto FPS) clips use "open GOP" keyframes, whose first frames depend on the previous group; those few frames are re-encoded too. Every copied range is verified against the source packet sizes. If anything doesn't match, that range is re-encoded instead, and the result dialog says so.

The same smart cut applies when a caption or picture-in-picture covers part of an untouched clip: only the seconds under it, rounded out to the nearest keyframes, are re-encoded.

The sound of a trimmed clip is copied as well, including Apple spatial audio, unless the sound itself is edited (volume, speed, music, voice-overs, transitions, fades). In that case it is mixed into one AAC track and extra tracks are left out; the export dialog says this before you start.

**Join check.** After every export, the app decodes the joined file wherever copied footage follows re-encoded footage, and compares those frames with the original. During testing, FFmpeg's HEVC decoder sometimes mis-decoded copied frames after a re-encoded part (an encoder header detail). The app avoids that case. If such a join ever slips through anyway, that piece is re-rendered automatically and the result dialog says so.

The status line at the bottom tells you before you start whether the merge will be lossless:

| Status | Meaning |
|---|---|
| 🟢 Ready for lossless merge | All clips match, so they are joined bit-for-bit with stream copy. |
| 🟡 Lossless merge possible, with notes | The merge is still lossless. Something minor differs, such as frame rate, orientation or encoder headers. Click **Details** to see what. |
| 🔴 Clips differ | Stream copy can't join these clips (for example 4K + 1080p, or H.264 + HEVC). The app explains exactly which clips differ. You can remove the clips marked in red, or re-encode if you agree to it. |

## Mixed frame rates (iPhone "Auto FPS")

With **Settings › Camera › Record Video › Auto FPS** turned on, an iPhone set to 4K 60 records some clips at 30 or 24 fps. It does this automatically in dim light, because fewer frames per second give each frame more light. Turn the setting off to always get 60 fps.

When the clips' frame rates differ, **Merge Videos** asks how to handle them:

| Option | What happens |
|---|---|
| **Keep original frame rates** (lossless, default) | Every clip is copied untouched, and each one plays at its own rate. Modern players and phones handle this. |
| **Convert to 60 fps: repeat frames** | Only the slower clips are re-encoded (x265/x264, CRF 16), matched to your 60 fps clips: same codec, 10-bit, and HDR/HLG colour tags. Motion looks like the original. |
| **Convert to 60 fps: blend frames** | Same, but in-between frames are blended. Motion is smoother, with slight ghosting on fast movement. |

When converting, the 60 fps clips are still copied bit-exact and audio is never re-encoded. Converting 4K on the CPU is slow, usually slower than real time. The app remembers your last choice.

## Clips that don't match (front camera, other devices)

Sometimes a few clips are stored differently from the rest. For example, the iPhone 17 Pro front camera records landscape video while you hold the phone upright, and stores it turned on its side with a rotation flag. A merged file can carry only one rotation flag, so those clips would play sideways.

When only some clips differ, **Merge Videos** names them and says why, then offers **Re-encode only these N**. Those clips are:

- turned upright (or fitted with black bars if their shape really differs);
- re-encoded at very high quality in the same format as the rest: codec, 10-bit, HDR/HLG tags and stored orientation;
- joined with everything else.

All other clips stay bit-exact, and **all audio, including Apple spatial audio (APAC), is copied unchanged**. The full re-encode is still offered as a secondary option.

Apple spatial audio can only be stored in **MOV**. MP4 and MKV can't hold it, so the app saves as MOV by default. If you pick MP4, it offers MOV instead.

**Different codec headers.** Clips recorded in different modes can have different HEVC parameter sets. A plain stream-copy join then decodes later clips with the wrong parameters, which drops or corrupts frames. The app detects this and first re-wraps each HEVC clip, losslessly, with its headers inside the stream. This costs one extra copy pass and temporary disk space. Frame timestamps then have 1 ms precision, the same as any MKV file. FFmpeg handles H.264 automatically.

## Quality guarantees

- **No silent re-encoding.** Without edits, clips are re-encoded only when you choose 60 fps conversion, or when lossless merging is impossible and you confirm it in two separate dialogs. With edits, the export dialog lists exactly what is copied and what is re-encoded before anything starts. A full re-encode uses the highest frame rate among the clips, so frames are repeated rather than dropped. The second dialog states plainly that re-encoding is not lossless. Even then the quality is very high: x264 CRF 16 or x265 CRF 18 (x265 for 10-bit/HDR sources), with AAC audio at 320 kb/s.
- Joins are checked for: video codec, resolution, pixel format and bit depth, scan type, audio codec, sample rate, channel layout and track count. Frame rate, codec profile, orientation and encoder headers are checked too, and only produce warnings.
- **Verified.** After merging, the output is probed and its duration is checked against the sum of the clips. The test suite (below) proves that every decoded frame is pixel-identical and every audio packet is byte-identical.
- 4K/60 fps, 10-bit HEVC (iPhone/Android), rotation flags and the `hvc1` tag (needed by Apple players) are all preserved. The first clip's `creation_time` is copied to the output.
- **Large files.** FFmpeg streams the data from disk to disk, so RAM use stays small whatever the file size. Free disk space is checked before starting.
- **Unicode file names** in Georgian, Russian or any other script, plus apostrophes and spaces, are fully supported.

## Safety

- The output is written to a hidden temporary file (`.name.merging-xxxx.mp4`) in the target folder. It is renamed only after FFmpeg finishes and the result has been verified. A cancelled or failed merge deletes the temp file, and an existing file is never touched in that case.
- If the target file already exists, the app asks before replacing it, and the replacement happens only after a successful merge.
- The output can never overwrite one of the source clips.
- Source files are only read and never modified.

## How the recording date is found

1. Video metadata: QuickTime `com.apple.quicktime.creationdate` (local time on iPhones), `creation_time`, and `date`/`DATE_RECORDED`. Placeholder dates such as 1904/1970 are ignored.
2. A timestamp in the file name, e.g. OBS `2025-03-02 21-15-00.mkv`, `VID_20250302_211500.mp4`, `PXL_20250302_211500123.mp4`, DJI `DJI_20250302211500_0001.MP4`.
3. Otherwise, the file's creation or modification time, whichever is earlier. Copying files resets the creation time, but the modification time usually survives.

The card for each clip shows which of these sources was used.

## Supported files

Input: `.mp4 .m4v .mov .mkv .webm .avi .mts .m2ts .ts .mpg .mpeg .3gp .3g2 .wmv .flv .mxf .insv`.
Output: the source container by default (MP4/MOV/MKV/TS/…). If the codecs can't be stored in the chosen container, for example PCM audio in MP4, the app suggests MKV.

Only the video and audio tracks are merged. Telemetry or data tracks (GoPro GPS, timecode) and embedded subtitles are dropped, because they generally can't be concatenated.

## Running from source

Requires Python 3.10+ (tested with 3.14) on Windows 10/11.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
# FFmpeg: put ffmpeg.exe + ffprobe.exe (+ DLLs) in .\ffmpeg\, or have them on PATH.
# build.ps1 downloads them automatically.
.\.venv\Scripts\python main.py
```

FFmpeg is searched for in this order: `LVM_FFMPEG_DIR` env var → bundled `ffmpeg\` folder → `PATH`.

## Building the Windows executable

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1            # folder build + zip (recommended)
powershell -ExecutionPolicy Bypass -File .\build.ps1 -OneFile   # single .exe (slower start)
```

The script creates `.venv`, installs dependencies, downloads FFmpeg 9.0 (BtbN GPL shared build) if it is missing, runs the tests and builds with PyInstaller. Output:

- `dist\LosslessStudio\LosslessStudio.exe`: copy the whole folder anywhere. No installation is needed.
- `dist\LosslessStudio-win64.zip`: the same folder, zipped for sharing.

## Tests

```powershell
.\.venv\Scripts\python -m unittest tests.test_engine tests.test_render -v
.\.venv\Scripts\python tests\ui_smoke.py <samples-dir> <screenshot-dir>   # drives the real window
```

`tests/test_render.py` covers the editor:

- smart trims on open-GOP HEVC, closed-GOP HEVC and H.264 keep exact frame counts, the copied frames are bit-exact, and the re-encoded edges score above 38 dB PSNR;
- splitting and reordering;
- speed, look, rotation and Unicode text;
- transitions, including between 8-bit H.264 and 10-bit HEVC;
- music, and fades in and out;
- 1080p SDR export from HDR;
- the 9:16 canvas;
- HDR and rotation flags kept when copying.

`tests/make_samples.py` generates synthetic test footage: 4K 60 fps clips with Georgian, Russian and apostrophe file names whose metadata dates differ from their alphabetical order, a mixed 1080p60/vertical-30fps set, clips with differing encoder headers, and OBS-style MKVs. The tests check:

- iPhone-like footage (HEVC 10-bit HLG, rotated, Auto FPS 60/30/24 with different encoder headers): lossless merge keeps every frame, and conversion to 60 fps works by both repeating and blending frames;
- frame timestamps must strictly increase in every merged file;
- the chronological order and the date sources;
- lossless merging: same packet count, pixel-identical decoded frames, byte-identical audio packets;
- MKV output;
- incompatibility detection;
- cancellation, which must leave no partial files behind;
- the re-encode fallback.

## Project layout

```
main.py                       entry point
lossless_merger/
  ffmpeg_tools.py             locate & run ffmpeg/ffprobe (Unicode-safe, no console windows)
  media.py                    folder scan, ffprobe parsing, recording date, thumbnails
  compat.py                   lossless compatibility analysis, container rules, re-encode target
  merger.py                   lossless concat & re-encode, progress, cancel, verify, atomic rename
  project.py                  edit model: main track, text / overlay / audio tracks, export presets
  render.py                   edited export: smart cut, layers, effects, transitions, audio mix, verify
  captions.py                 offline auto captions (whisper.cpp + VAD)
  projectio.py                save / open .lsproj projects
  ui/                         PySide6 interface (timeline, preview player, edit panel, media bin,
                              caption drawing, voice recorder, dialogs, workers, main window)
whisper/                      whisper-cli.exe + DLLs (downloaded by build.ps1)
tests/                        sample generator, engine tests, UI smoke test
tools/make_icon.py            renders assets/icon.ico
build.ps1                     Windows build script
```

## License

The app's source code is released under the [MIT License](LICENSE).

The Windows download also bundles FFmpeg (GPL-3.0), whisper.cpp (MIT) and Qt/PySide6 (LGPL-3.0); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). FFmpeg and whisper.cpp run as separate programs and are not linked into the app.
