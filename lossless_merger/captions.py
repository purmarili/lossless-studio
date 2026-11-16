"""Automatic captions with whisper.cpp (runs offline on this PC).

The clips' sound is mixed to 16 kHz mono with FFmpeg, transcribed by the
bundled ``whisper-cli`` and turned into subtitle text items. Speech models are
downloaded once (with the user's consent) into the app's data folder.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import ffmpeg_tools as ff
from .appdirs import sub
from .merger import Cancelled, MergeError

MODELS = {   # size -> (file, approx MB, label)
    "base": ("ggml-base.bin", 148, "Fast (base, 148 MB)"),
    "small": ("ggml-small.bin", 488, "Accurate (small, 488 MB)"),
    "medium": ("ggml-medium.bin", 1530, "Best (medium, 1.5 GB, slow)"),
}
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{name}"
# Voice activity detection: only parts with speech are transcribed (Whisper
# otherwise "hears" words in wind, engine noise or silence).
VAD_NAME = "ggml-silero-v5.1.2.bin"
VAD_URL = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/" + VAD_NAME
LANGUAGES = {"auto": "Detect automatically", "ka": "Georgian", "ru": "Russian", "en": "English",
             "uk": "Ukrainian", "de": "German", "tr": "Turkish", "fr": "French", "es": "Spanish"}


@dataclass
class Cue:
    start: float
    end: float
    text: str


def available() -> bool:
    return ff.whisper_path() is not None


def models_dir() -> Path:
    return sub("models")


def model_path(size: str) -> Path:
    return models_dir() / MODELS[size][0]


def has_model(size: str) -> bool:
    p = model_path(size)
    v = models_dir() / VAD_NAME
    return p.exists() and p.stat().st_size > 10_000_000 and v.exists() and v.stat().st_size > 100_000


def download_model(size: str, progress: Callable[[float], None], cancel: threading.Event) -> Path:
    vad = models_dir() / VAD_NAME
    if not (vad.exists() and vad.stat().st_size > 100_000):
        _download(VAD_URL, vad, lambda f: None, cancel)
    p = model_path(size)
    if p.exists() and p.stat().st_size > 10_000_000:
        return p
    return _download(MODEL_URL.format(name=MODELS[size][0]), p, progress, cancel)


def _download(url: str, dst: Path, progress: Callable[[float], None], cancel: threading.Event) -> Path:
    tmp = dst.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "LosslessStudio"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancel.is_set():
                    raise Cancelled()
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    progress(done / total)
        if total and tmp.stat().st_size != total:
            raise MergeError("The download was incomplete. Please try again.")
        os.replace(tmp, dst)
        return dst
    except Cancelled:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise MergeError(f"Could not download the speech model: {exc}") from exc


def _ts(s: str) -> float:
    h, m, rest = s.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def transcribe(project, size: str, language: str, progress: Callable[[float, str], None],
               cancel: threading.Event) -> list[Cue]:
    from . import render
    from .merger import run_ffmpeg
    work = models_dir() / "work"
    work.mkdir(exist_ok=True)
    wav = work / "speech.wav"
    out_json = work / "out.json"
    for p in (wav, out_json):
        p.unlink(missing_ok=True)
    target = render.make_target(project)
    progress(0.0, "Preparing the sound")
    run_ffmpeg(render.audio_mix_args(project, target, wav, speech_only=True), project.duration,
               lambda f, s: progress(0.1 * f, "Preparing the sound"), cancel)
    exe = ff.whisper_path()
    if not exe:
        raise MergeError("Speech recognition (whisper.cpp) is not installed with this app.")
    threads = max(2, min(8, (os.cpu_count() or 4) - 1))
    # relative paths: whisper.cpp can't open non-ASCII paths on Windows
    cmd = [exe, "-m", MODELS[size][0], "-f", "work/speech.wav", "-l", language, "-oj", "-of", "work/out",
           "-ml", "42", "-sow", "-t", str(threads), "-pp",
           "-mc", "0",            # no text context: avoids repetition loops
           "-sns"]                # suppress non-speech tokens
    if (models_dir() / VAD_NAME).exists():
        cmd += ["--vad", "-vm", VAD_NAME, "-vp", "200"]
    proc = ff.popen(cmd, cwd=models_dir())
    errors: list[str] = []

    def read_err():
        for raw in proc.stderr:
            line = ff.decode(raw)
            m = re.search(r"progress\s*=\s*(\d+)%", line)
            if m:
                progress(0.1 + 0.9 * int(m.group(1)) / 100, "Recognising speech")
            elif line.strip():
                errors.append(line.strip())

    def drain_out():                       # the transcript is also printed; don't let the pipe fill up
        for _ in proc.stdout:
            pass

    threads_ = [threading.Thread(target=read_err, daemon=True), threading.Thread(target=drain_out, daemon=True)]
    for t in threads_:
        t.start()
    while proc.poll() is None:
        if cancel.wait(0.2):
            proc.kill()
            proc.wait()
            raise Cancelled()
    for t in threads_:
        t.join(timeout=2)
    if proc.returncode != 0 or not out_json.exists():
        raise MergeError("Speech recognition failed:\n" + "\n".join(errors[-8:]))
    data = json.loads(out_json.read_text(encoding="utf-8", errors="replace"))
    cues = []
    for seg in data.get("transcription", []):
        text = (seg.get("text") or "").strip()
        if not text or text.startswith("[") and text.endswith("]"):
            continue                               # [Music], [BLANK_AUDIO] ...
        off = seg.get("offsets") or {}
        if "from" in off:
            a, b = off["from"] / 1000, off["to"] / 1000
        else:
            ts = seg.get("timestamps") or {}
            a, b = _ts(ts["from"]), _ts(ts["to"])
        if b - a < 0.2:
            b = a + 0.2
        cues.append(Cue(a, min(b, project.duration), text))
    for p in (wav, out_json):
        p.unlink(missing_ok=True)
    return _clean(cues)


def _clean(cues: list[Cue]) -> list[Cue]:
    """Merge immediate repeats and drop hallucination loops (the same line
    over and over), a known Whisper failure on music or noise."""
    out: list[Cue] = []
    run = 0
    for c in cues:
        if out and c.text == out[-1].text and c.start - out[-1].end < 1.0:
            out[-1].end = max(out[-1].end, c.end)
            run += 1
            continue
        if run >= 3:                      # the previous line repeated 4+ times: noise
            out.pop()
        run = 0
        out.append(c)
    if run >= 3 and out:
        out.pop()

    def looping(text: str) -> bool:           # "talik, talik, talik, talik ..." inside one line
        words = [w.strip(",.!?").lower() for w in text.split()]
        return len(words) >= 6 and len(set(words)) / len(words) < 0.35

    return [c for c in out if c.end - c.start >= 0.2 and not looping(c.text)]
