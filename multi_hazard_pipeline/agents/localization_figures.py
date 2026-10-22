"""Load reviewed pixel outlines through the verified figure workflow, never model coordinates."""
from __future__ import annotations

import json
from pathlib import Path


def load_outline_manifest(path, target_crs):
    from segment_mapping.figure_workflow import load_prepared_figure, transform_digitized_outline
    from shapely.geometry import shape

    manifest = Path(path).resolve()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    accepted, rejected = {}, []
    for entry in document.get("outlines", []):
        identifier = entry.get("id", "")
        if not identifier or identifier in accepted:
            raise ValueError("Outline IDs must be nonempty and unique")
        try:
            candidate_path = (manifest.parent / entry["candidate"]).resolve()
            candidate = load_prepared_figure(candidate_path)
            interpretation = entry["interpretation"]
            if interpretation["geometry_role"] != "event_extent" or not interpretation.get("target_event_verified") or not interpretation.get("outline_explicit"):
                raise ValueError("An event outline requires an explicit delineation and verified target event interpretation")
            if interpretation.get("event_date", "unknown").lower() == "unknown" or interpretation.get("event_confidence") not in ("high", "medium"):
                raise ValueError("Target event/date interpretation remains unresolved")
            if interpretation.get("boundary_confidence") not in ("high", "medium", "low"):
                raise ValueError("Outline interpretation must state boundary confidence")
            if not entry.get("segments"):
                raise ValueError("Outline must identify the original segment IDs it supports")
            feature = transform_digitized_outline(candidate_path, entry["pixel_geometry"], limits=entry["limits"],
                                                 interpretation=interpretation, target_crs=target_crs)
            geometry = shape(feature["geometry"])
            if geometry.geom_type not in ("Polygon", "MultiPolygon"):
                raise ValueError("An affected-area outline must be polygonal")
            accepted[identifier] = dict(id=identifier, geometry={"wkt": geometry.wkt},
                segments=[str(s) for s in entry["segments"]], source=candidate["source"],
                interpretation=interpretation, georeferencing_assessment=feature["georeferencing_assessment"],
                candidate_path=str(candidate_path), figure_id=candidate["id"])
        except (ValueError, KeyError, TypeError, OSError) as exc:
            rejected.append({"id": identifier, "reason": str(exc)})
    return accepted, rejected
