# Third-party software

The source code in this repository is MIT-licensed (see `LICENSE`). The
Windows download additionally bundles the following programs and libraries,
each under its own license:

| Component | License | Where in the download | Source code |
|---|---|---|---|
| **FFmpeg** 9.0 (BtbN "win64-gpl-shared" build) | GPL-3.0-or-later | `ffmpeg\` (license: `ffmpeg\LICENSE.txt`) | https://ffmpeg.org/download.html · build scripts: https://github.com/BtbN/FFmpeg-Builds |
| **whisper.cpp** (`whisper-cli`) | MIT | `whisper\` (license: `whisper\LICENSE.txt`) | https://github.com/ggml-org/whisper.cpp |
| **Qt 6 / PySide6** 6.12 | LGPL-3.0 | `_internal\PySide6\` | https://code.qt.io · https://download.qt.io/official_releases/QtForPython/ |
| **Python** 3 runtime | PSF License | `_internal\` | https://www.python.org |

FFmpeg and whisper.cpp run as separate programs; the app starts them and
reads their output. They are not linked into the app. Qt is used as
dynamically linked libraries and can be replaced by the user.

Speech models for auto captions (OpenAI Whisper, MIT license, converted by
the ggml authors) are not bundled. They are downloaded from Hugging Face
(huggingface.co/ggerganov/whisper.cpp) only after the user confirms.
