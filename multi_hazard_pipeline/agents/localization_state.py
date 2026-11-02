"""Input-bound localization checkpoints; no credentials or model reasoning are persisted."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def input_fingerprint(artifact_dir, package, segments_table=None, *, settings=None, figure_manifest=None):
    root, package = Path(artifact_dir), Path(package).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    pkg = json.loads(package.read_text(encoding="utf-8"))
    figure_files = []
    if figure_manifest:
        figure_path = Path(figure_manifest).resolve()
        figure_document = json.loads(figure_path.read_text(encoding="utf-8"))
        for entry in figure_document.get("outlines", []):
            candidate = (figure_path.parent / entry["candidate"]).resolve()
            figure_files.append(candidate)
            if candidate.is_file():
                metadata = json.loads(candidate.read_text(encoding="utf-8"))
                if metadata.get("image"):
                    figure_files.append((candidate.parent / metadata["image"]).resolve())
    content = {}
    for path in [package, *(root / n for n in ("translated.json", "final_rows.json", "segments.json")),
                 *([Path(segments_table)] if segments_table else []), *([Path(figure_manifest)] if figure_manifest else []), *figure_files]:
        if path.is_file():
            content[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
    paths = [package.parent / item["path"] for item in pkg.get("layers", []) + pkg.get("rasters", [])]
    paths += [Path(i["path"]) for i in manifest.get("inputs", []) if i.get("path")]
    # Include shapefile sidecars: a changed DBF changes the candidate identities.
    paths += [s for p in list(paths) if p.suffix.lower() == ".shp" for s in p.parent.glob(p.stem + ".*")]
    files = {str(p.resolve()): [p.stat().st_size, p.stat().st_mtime_ns] if p.exists() else None for p in paths}
    payload = dict(version=1, content=content, files=files, settings=settings,
                   inputs=manifest.get("inputs"), localization_input=manifest.get("localization_input"))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
