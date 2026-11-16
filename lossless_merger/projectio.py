"""Saving and opening edit projects (.lvmproj, JSON).

Media files are referenced by their full path; nothing is copied. When a file
is missing on load, the project still opens and the missing files are listed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path
from typing import Callable

from .media import Clip, probe
from .project import AudioItem, ClipEdit, OverlayItem, Project, Segment, TextItem, Transform

VERSION = 1
EXTENSION = ".lvmproj"


def _edit_to_dict(e: ClipEdit) -> dict:
    d = asdict(e)
    return d


def _edit_from_dict(d: dict) -> ClipEdit:
    names = {f.name for f in fields(ClipEdit)}
    kw = {k: v for k, v in d.items() if k in names}
    if isinstance(kw.get("tf"), dict):
        kw["tf"] = Transform(**kw["tf"])
    if isinstance(kw.get("tf_end"), dict):
        kw["tf_end"] = Transform(**kw["tf_end"])
    return ClipEdit(**kw)


def _media(clip: Clip) -> dict:
    return {"path": str(clip.path), "image": clip.is_image}


def save(project: Project, path: Path, folder: Path | None = None) -> None:
    data = {
        "app": "LosslessVideoMerger", "version": VERSION,
        "folder": str(folder) if folder else None,
        "settings": {"canvas": project.canvas, "background": project.background, "preset": project.preset,
                     "fade_in": project.fade_in, "fade_out": project.fade_out},
        "segments": [{"media": _media(s.clip), "kind": s.kind, "still_at": s.still_at, "still_len": s.still_len,
                      "id": s.id, "edit": _edit_to_dict(s.edit)} for s in project.segments],
        "texts": [asdict(t) for t in project.texts],
        "overlays": [{**{k: v for k, v in asdict(o).items() if k != "clip"}, "media": _media(o.clip)}
                     for o in project.overlays],
        "audios": [{**asdict(a), "path": str(a.path)} for a in project.audios],
    }
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load(path: Path, load_photo: Callable[[Path], Clip],
         on_progress: Callable[[int, int], None] | None = None) -> tuple[Project, Path | None, list[str]]:
    """(project, folder, missing files). *load_photo* turns a photo path into a Clip."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("app") != "LosslessVideoMerger":
        raise ValueError("This is not a Lossless Video Merger project.")
    cache: dict[str, Clip] = {}
    missing: list[str] = []
    media = [s["media"] for s in data.get("segments", [])] + [o["media"] for o in data.get("overlays", [])]
    for n, m in enumerate(media):
        key = m["path"]
        if key in cache:
            continue
        p = Path(key)
        if not p.exists():
            missing.append(key)
        cache[key] = load_photo(p) if m.get("image") else probe(p)
        if on_progress:
            on_progress(n + 1, len(media))

    proj = Project()
    st = data.get("settings", {})
    proj.canvas = st.get("canvas", "original")
    proj.background = st.get("background", "black")
    proj.preset = st.get("preset", "original")
    proj.fade_in = bool(st.get("fade_in"))
    proj.fade_out = bool(st.get("fade_out"))
    for s in data.get("segments", []):
        clip = cache[s["media"]["path"]]
        proj.segments.append(Segment(clip, _edit_from_dict(s.get("edit", {})), s.get("id") or Segment(clip).id,
                                     s.get("kind", "video"), s.get("still_at", 0.0), s.get("still_len", 4.0)))
    tnames = {f.name for f in fields(TextItem)}
    for t in data.get("texts", []):
        proj.texts.append(TextItem(**{k: v for k, v in t.items() if k in tnames}))
    onames = {f.name for f in fields(OverlayItem)} - {"clip"}
    for o in data.get("overlays", []):
        proj.overlays.append(OverlayItem(cache[o["media"]["path"]], **{k: v for k, v in o.items() if k in onames}))
    anames = {f.name for f in fields(AudioItem)}
    for a in data.get("audios", []):
        kw = {k: v for k, v in a.items() if k in anames}
        kw["path"] = Path(kw["path"])
        if not kw["path"].exists():
            missing.append(str(kw["path"]))
        proj.audios.append(AudioItem(**kw))
    folder = Path(data["folder"]) if data.get("folder") else None
    return proj, folder, missing
