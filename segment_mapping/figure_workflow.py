"""Controlled, offline figure candidates; no inferred CRS or event interpretation.

Public entry points: prepare_figure, list_prepared_figures, load_prepared_figure,
assess_georeferencing and transform_digitized_outline. All pixel coordinates refer
to the copied image, never an independently resized display of it. Candidate JSON
is data, not executable geometry expressions. Legacy Georef users are unaffected.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import shapely
from PIL import Image, ImageDraw
from pyproj import CRS, Transformer
from shapely.geometry import MultiPoint, box, mapping, shape

from .georef import Georef

CONFIDENCES = {"high", "medium", "low", "unknown"}
ROLES = {"event_extent", "location", "context", "search_area"}
GEOMETRIES = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Candidate image must stay inside the prepared figure directory")
    return path


def prepare_figure(image_path, output_dir, metadata: dict) -> Path:
    """Copy an inspectable image and provenance into a self-contained candidate.

    metadata requires id, source (report/page/figure), geometry_role, event_date,
    legend (observations, including conflicts/unknowns). Optional georef has the
    existing Georef cfg/controls/checks; crs_evidence is verified only with a
    nonempty source citation. Explicit 'unknown' strings are valid observations.
    No localization or documented-footprint claim is made by preparation.
    """
    required = ("id", "source", "geometry_role", "event_date", "legend")
    if any(not metadata.get(k) for k in required):
        raise ValueError(f"Figure metadata requires {required}")
    if metadata["geometry_role"] not in ROLES:
        raise ValueError("Invalid geometry_role")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    image_path = Path(image_path)
    with Image.open(image_path) as im:
        im.convert("RGB").save(out / "figure.png")
        size = list(im.size)
    data = copy.deepcopy(metadata)
    data.update(schema_version=1, image="figure.png", image_size=size,
                image_sha256=_hash(out / "figure.png"), original_image_sha256=_hash(image_path),
                pixel_convention="native image pixels, origin upper-left, x right, y down")
    data.setdefault("crs_evidence", {"status": "unknown", "source": ""})
    data.setdefault("outline_interpretation", {"status": "unresolved", "boundary_confidence": "unknown"})
    candidate = out / "figure_candidate.json"
    candidate.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return candidate


def load_prepared_figure(candidate_path) -> dict:
    """Load only a local manifest with an intact image; reject traversal/symlinks."""
    candidate = Path(candidate_path)
    data = json.loads(candidate.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported figure candidate schema")
    image_path = _inside(candidate.parent, data["image"])
    if _hash(image_path) != data["image_sha256"]:
        raise ValueError("Prepared figure image hash mismatch")
    with Image.open(image_path) as im:
        if list(im.size) != data["image_size"]:
            raise ValueError("Prepared figure image dimensions changed")
    return data


def list_prepared_figures(root) -> list[dict]:
    """Expose prepared artifacts only; malformed candidates remain inspectable errors.

    Returns manifest paths and small metadata suitable for a harness tool result;
    it neither scans arbitrary report images nor creates transforms automatically.
    """
    result = []
    root = Path(root).resolve()
    for path in sorted(root.rglob("figure_candidate.json")):
        if not path.resolve().is_relative_to(root):
            continue
        try:
            data = load_prepared_figure(path)
            result.append({"candidate_path": str(path), **{k: data.get(k) for k in
                           ("id", "source", "image_size", "geometry_role", "event_date", "crs_evidence")}})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result.append({"candidate_path": str(path), "error": str(exc)})
    return result


def assess_georeferencing(candidate: dict, limits: dict) -> dict:
    """Require evidence, withheld checks, spatial coverage and explicit accuracy limits.

    limits: max_check_error_m, min_control_coverage [0,1],
    min_checkpoint_span [0,1] (max pairwise pixel separation/image diagonal),
    min_checkpoints (>=2). No default accuracy is invented from fitted residuals.
    Grid mapping needs a verified printed CRS; landmark mapping may leave the
    printed CRS unknown, but requires cited independently matched landmarks.
    """
    required = ("max_check_error_m", "min_control_coverage", "min_checkpoint_span", "min_checkpoints")
    if any(k not in limits for k in required):
        raise ValueError(f"Explicit accuracy limits required: {required}")
    if any(not math.isfinite(float(limits[k])) for k in required):
        raise ValueError("Accuracy limits must be finite")
    if limits["max_check_error_m"] <= 0 or any(not 0 <= limits[k] <= 1 for k in required[1:3]):
        raise ValueError("Invalid accuracy or coverage limits")
    if limits["min_checkpoints"] < 2 or int(limits["min_checkpoints"]) != limits["min_checkpoints"]:
        raise ValueError("At least two independent checkpoints required")
    reasons = []
    cfg = candidate.get("georef") or {}
    evidence = candidate.get("crs_evidence") or {}
    basis = candidate.get("mapping_basis", "grid")
    if basis not in {"grid", "landmarks"}:
        reasons.append("Mapping basis must be grid or landmarks")
    if basis == "grid":
        if evidence.get("status") != "verified" or not evidence.get("source") or not evidence.get("crs"):
            reasons.append("Printed grid CRS is unknown or lacks a verified source")
        elif cfg.get("grid_crs") is None or CRS(cfg["grid_crs"]) != CRS(evidence["crs"]):
            reasons.append("Verified printed CRS does not match declared grid_crs")
        if not candidate.get("control_provenance"):
            reasons.append("Grid-to-target control conversion provenance is missing")
    elif not candidate.get("control_provenance"):
        reasons.append("Independent landmark matching provenance is missing")
    try:
        crs = CRS(cfg.get("target_crs"))
        if not crs.is_projected or any(abs(a.unit_conversion_factor - 1) > 1e-12 for a in crs.axis_info[:2]):
            reasons.append("Checkpoint errors require a projected target CRS in metres")
    except Exception:
        reasons.append("Valid target CRS is missing")
    if not cfg.get("gcps"):
        return {"accepted": False, "reasons": reasons + ["No control points supplied"],
                "georeferencing_confidence": "unknown", "limits": dict(limits),
                "crs_evidence": copy.deepcopy(evidence), "mapping_basis": basis}
    gr = Georef(candidate["id"], cfg, cfg["gcps"], cfg.get("checks", []))
    width, height = candidate["image_size"]
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions must be positive")
    image = box(0, 0, width, height)
    if not math.isfinite(gr.pixel_size) or gr.pixel_size <= 0 or not math.isfinite(gr.rms):
        reasons.append("Transform is degenerate or nonfinite")
    if gr.method == "projective":
        corners = np.array([[0, 0, 1], [width, 0, 1], [width, height, 1], [0, height, 1]])
        denominator = corners @ gr.H[2]
        if not np.isfinite(denominator).all() or denominator.min() <= 0 <= denominator.max():
            reasons.append("Projective horizon crosses the prepared image")
    points = np.array([g["pixel"] for g in gr.gcps])
    hull = MultiPoint(points).convex_hull
    coverage = hull.intersection(image).area / image.area
    if coverage < limits["min_control_coverage"]:
        reasons.append("Control coverage is below the declared limit")
    checks = gr.checks
    if len(checks) < limits["min_checkpoints"]:
        reasons.append("Insufficient independent checkpoints")
    check_px = np.array([c["pixel"] for c in checks], float).reshape(-1, 2)
    span = float(np.linalg.norm(check_px[:, None] - check_px[None, :], axis=2).max()) / math.hypot(width, height) if len(checks) else 0.0
    if span < limits["min_checkpoint_span"]:
        reasons.append("Independent checkpoints lack spatial coverage")
    if any(not c.get("source") for c in checks):
        reasons.append("Checkpoint source/identification evidence is missing")
    all_points = gr.gcps + checks
    if any(not image.covers(MultiPoint([c["pixel"]])) for c in all_points):
        reasons.append("Controls or checkpoints lie outside the image")
    for i, c in enumerate(checks):
        # Withheld points cannot be reused fitted points or duplicate checks.
        if any(np.allclose(c["pixel"], g["pixel"], rtol=0, atol=1e-6) or
               np.allclose(c["target"], g["target"], rtol=0, atol=1e-6)
               for g in gr.gcps + checks[:i]):
            reasons.append("Checkpoints overlap fitted controls or duplicate another checkpoint")
            break
    errors = np.array([np.linalg.norm(gr.apply([c["pixel"]])[0] - c["target"]) for c in checks])
    maximum = float(errors.max()) if len(errors) else None
    if not np.isfinite(errors).all():
        reasons.append("Independent checkpoint predictions are nonfinite")
    if maximum is not None and maximum > limits["max_check_error_m"]:
        reasons.append("Independent checkpoint error exceeds the declared accuracy limit")
    return {"accepted": not reasons, "reasons": reasons, "limits": dict(limits),
            "crs_evidence": copy.deepcopy(evidence), "mapping_basis": basis,
            "control_provenance": candidate.get("control_provenance"),
            "georeferencing_confidence": "medium" if not reasons else "unknown",
            "control_coverage": coverage, "checkpoint_span": span,
            "max_checkpoint_error_m": maximum,
            "checkpoint_rms_m": float(np.sqrt(np.mean(errors**2))) if len(errors) else None,
            "accuracy_note": "Checkpoint errors measure sampled map alignment, not event boundary interpretation or a statistical accuracy guarantee.",
            "georef": gr.info()}


def write_inspection(candidate_path, limits: dict) -> Path:
    """Write numbered controls/checks over the exact image and a separate QA ledger."""
    path = Path(candidate_path)
    data = load_prepared_figure(path)
    assessment = assess_georeferencing(data, limits)
    with Image.open(_inside(path.parent, data["image"])) as im:
        im = im.convert("RGB")
        draw = ImageDraw.Draw(im)
        for key, color in (("gcps", "blue"), ("checks", "green")):
            for i, item in enumerate(data.get("georef", {}).get(key, [])):
                x, y = item["pixel"]
                draw.ellipse((x-5, y-5, x+5, y+5), outline=color, width=2)
                draw.text((x+7, y), f"{key} {i+1}", fill=color)
        im.save(path.parent / "controls_inspection.png")
    output = path.parent / "georeferencing_assessment.json"
    output.write_text(json.dumps(assessment, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def transform_digitized_outline(candidate_path, pixel_geometry: dict, *, limits: dict,
                                interpretation: dict, target_crs=None) -> dict:
    """Transform native point/line/polygon geometry, holes and multiparts intact.

    Interpretation must explicitly provide geometry_role, event_date, legend,
    reason, event_confidence and boundary_confidence. Transform success never sets
    a 'documented' label. Reprojection uses always_xy and returns target_crs beside
    the geometry; no buffer, simplification, repair or area fitting is performed.
    """
    data = load_prepared_figure(candidate_path)
    assessment = assess_georeferencing(data, limits)
    if not assessment["accepted"]:
        raise ValueError("Unresolved georeferencing: " + "; ".join(assessment["reasons"]))
    for key in ("geometry_role", "event_date", "legend", "reason", "event_confidence", "boundary_confidence"):
        if not interpretation.get(key):
            raise ValueError(f"Explicit outline interpretation requires {key}")
    if interpretation["geometry_role"] not in ROLES or any(interpretation[k] not in CONFIDENCES for k in ("event_confidence", "boundary_confidence")):
        raise ValueError("Invalid role or interpretation confidence")
    geometry = shape(pixel_geometry)
    if geometry.geom_type not in GEOMETRIES or geometry.is_empty or not geometry.is_valid or geometry.has_z:
        raise ValueError("Outline must be valid, nonempty, two dimensional point/line/polygon geometry")
    gr_cfg = data["georef"]
    gr = Georef(data["id"], gr_cfg, gr_cfg["gcps"], gr_cfg.get("checks", []))
    if not box(0, 0, *data["image_size"]).covers(geometry):
        raise ValueError("Outline lies outside the prepared image")
    if not MultiPoint([c["pixel"] for c in gr.gcps]).convex_hull.covers(geometry):
        raise ValueError("Outline extrapolates beyond control coverage")
    result = gr.transform_geom(geometry)
    crs = CRS(gr_cfg["target_crs"])
    destination = CRS(target_crs) if target_crs else crs
    if destination != crs:
        converter = Transformer.from_crs(crs, destination, always_xy=True)
        result = shapely.transform(result, lambda xy: np.column_stack(converter.transform(xy[:, 0], xy[:, 1])))
    if result.is_empty or not result.is_valid or not np.isfinite(result.bounds).all():
        raise ValueError("Transformed outline is invalid or nonfinite")
    properties = copy.deepcopy(interpretation)
    properties.update(figure_id=data["id"], georeferencing_confidence=assessment["georeferencing_confidence"],
                      location_confidence=assessment["georeferencing_confidence"])
    return {"type": "Feature", "geometry": mapping(result), "properties": properties,
            "target_crs": destination.to_string(), "georeferencing_assessment": assessment}
