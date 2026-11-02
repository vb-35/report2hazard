"""Standalone localization test runs from a user-supplied report, segment table and GIS data.

No extraction, segmentation or classification is run: the supplied segment table is
authoritative. The report is only read (and optionally translated) as localization evidence.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, Callable

from .config import PipelineConfig
from .core import display_path, read_json, resolve_path, write_json
from .errors import PipelineError
from .pipeline import create_run, save_manifest, utc_now


KIND = "provided_localization"
TABLE_SUFFIXES = {".tsv", ".csv", ".txt"}
# Layer roles offered as separate files; the first three are required by every spatial package.
FILE_LAYERS = (
    ("catchment", "Catchment outline", True),
    ("streams", "Streams", True),
    ("roads", "Roads", False),
    ("bridges", "Bridges", False),
    ("buildings", "Buildings", False),
    ("names", "Place names", False),
)
NAME_FIELDS = ("name", "gew_name_haupt", "namezgwlv", "nameezgwlv", "event", "bezeichnung", "gewaessername", "label")
STAGES = (("report_text", "Report text"), ("translation", "Translation"), ("localization", "Localization"))


def is_provided(manifest: dict[str, Any]) -> bool:
    return manifest.get("report_mode") == KIND


def _table_rows(text: str) -> tuple[list[list[str]], str]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise PipelineError("The segment table is empty.")
    sample = "\n".join(lines[:5])
    try:
        delimiter = "\t" if "\t" in sample else csv.Sniffer().sniff(sample, delimiters=",;|").delimiter
    except csv.Error as exc:
        raise PipelineError("The segment table must be tab, comma or semicolon separated text with a header row.") from exc
    return list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)), delimiter


def table_events(text: str) -> list[str]:
    rows, _ = _table_rows(text)
    column = next((i for i, name in enumerate(rows[0]) if "event" in name.strip().lower()), None)
    if column is None:
        return []
    return list(dict.fromkeys(row[column].strip() for row in rows[1:] if len(row) > column and row[column].strip()))


def prepare_table(text: str, event: str = "") -> tuple[str, list[dict], str]:
    """Validate the table, keep only one event's rows, and return (TSV text, segments, event)."""
    from segment_mapping.runctx import parse_segment_table

    events = table_events(text)
    event = event.strip()
    if event or len(events) > 1:
        if not event:
            raise PipelineError(f"The segment table contains several events ({', '.join(events)}). Enter the event to localize.")
        match = next((name for name in events if name.casefold() == event.casefold()), None)
        if match is None:
            raise PipelineError(f"Event “{event}” is not in the segment table (found: {', '.join(events) or 'no event column'}).")
        rows, _ = _table_rows(text)
        column = next(i for i, name in enumerate(rows[0]) if "event" in name.strip().lower())
        rows = [rows[0]] + [row for row in rows[1:] if len(row) > column and row[column].strip() == match]
        buffer = io.StringIO()
        csv.writer(buffer, delimiter="\t", lineterminator="\n").writerows(rows)
        text, event = buffer.getvalue(), match
    elif events:
        event = events[0]
    try:
        segments, _ = parse_segment_table(text)
    except ValueError as exc:
        raise PipelineError(f"Segment table: {exc}") from exc
    if not segments:
        raise PipelineError("The segment table has a header but no segment rows.")
    if any(not segment["seg_id"] for segment in segments):
        raise PipelineError("Every segment row needs a segment ID.")
    return text, segments, event


def guess_name_field(path: Path, layer: str | None = None) -> str | None:
    import pyogrio

    fields = [str(field) for field in pyogrio.read_info(path, layer=layer)["fields"]]
    lowered = {field.casefold(): field for field in fields}
    for preferred in NAME_FIELDS:
        if preferred in lowered:
            return lowered[preferred]
    return next((field for field in fields if "name" in field.casefold()), None)


def detect_crs(catchment: Path, dem: Path) -> str:
    """The first metre-based projected CRS of the catchment or DEM."""
    import pyogrio
    import rasterio
    from segment_mapping.package import PackageError, _metre_crs

    candidates = [pyogrio.read_info(catchment).get("crs")]
    with rasterio.open(dem) as dataset:
        candidates.append(dataset.crs.to_string() if dataset.crs else None)
    for value in filter(None, candidates):
        try:
            crs = _metre_crs(value)
        except PackageError:
            continue
        epsg = crs.to_epsg()
        return f"EPSG:{epsg}" if epsg else crs.to_wkt()
    raise PipelineError("Could not find a projected CRS in metres in the catchment or DEM. Enter the local CRS, for example EPSG:31254.")


def build_package(target: Path, *, name: str, crs: str, dem: Path, layers: dict[str, dict[str, Any]]) -> Path:
    """Write a package.json that references the supplied files in place (they are not copied)."""
    from segment_mapping.package import PackageError, _metre_crs, load_package

    if crs:
        try:
            _metre_crs(crs)
        except Exception as exc:  # pyproj raises its own CRSError for unknown codes
            raise PipelineError(f"CRS: {exc}") from exc
    else:
        crs = detect_crs(layers["catchment"]["path"], dem)
    entries = []
    for role, layer in layers.items():
        field = layer.get("name_field") or guess_name_field(layer["path"])
        entries.append({"name": role, "path": str(layer["path"]), "role": role, "name_field": field})
    package = {"name": name, "crs": crs, "layers": entries,
               "rasters": [{"name": "dem", "path": str(dem), "role": "dem"}],
               "sources": [{"name": entry["name"], "provider": "provided with the test", "use": entry["role"]}
                           for entry in entries] + [{"name": "dem", "provider": "provided with the test", "use": "dem"}]}
    write_json(target, package)
    try:
        load_package(target, verify=False)
    except (PackageError, OSError, ValueError) as exc:
        raise PipelineError(f"Spatial data: {exc}") from exc
    return target


def create_provided_run(root: Path, config: PipelineConfig, *, reports: list[Path], source_dir: Path, doc_id: str,
                        table_text: str, table_name: str, event: str, run_id: str) -> tuple[Path, dict]:
    text, segments, event = prepare_table(table_text, event)
    manifest = create_run(source_dir, root, config, run_id=run_id, input_paths=reports, doc_id=doc_id)
    run_dir = Path(manifest["artifact_dir"])
    inputs = run_dir / "inputs"
    inputs.mkdir()
    # A filtered table is rewritten as TSV; otherwise the supplied text is kept exactly.
    table = inputs / f"segments_input{'.tsv' if text != table_text else Path(table_name).suffix.lower()}"
    table.write_text(text, encoding="utf-8")
    rows = [dict(segment=segment["seg_id"], event=segment["event"] or event, process=segment["process"],
                 generalized_category=segment["category"], interaction_type=segment["interact"],
                 sediment_transport_phase=segment["phase"]) for segment in segments]
    write_json(run_dir / "final_rows.json", {"status": "approved", "rows": rows,
                                             "origin": "user-supplied segment table; no model extraction"})
    manifest.update(
        report_mode=KIND, event=event or doc_id, status="approved", current_stage="report_text", progress=0,
        stages={name: "pending" for name, _ in STAGES},
        segment_table={"path": str(table), "original_name": table_name, "segments": len(segments)},
        approval_basis="User-supplied segment table for a localization test; extraction and human review were not run.",
    )
    save_manifest(run_dir, manifest)
    return run_dir, manifest


def prepare_provided_run(run_dir: Path, config: PipelineConfig, *, translate: bool,
                         log: Callable[[str], None], client=None) -> None:
    """Read (and optionally translate) the report, once; later relaunches reuse the saved text."""
    from .agents.source_agent import source_agent
    from .agents.translation_agent import translation_agent
    from .llm import ChatClient
    from .pipeline import record_translation_outcome

    def stage(name, status):
        manifest = read_json(run_dir / "manifest.json")
        manifest["stages"][name] = status
        manifest["current_stage"] = name
        save_manifest(run_dir, manifest)

    manifest = read_json(run_dir / "manifest.json")
    if not (run_dir / "source.json").is_file():
        stage("report_text", "running")
        log("Reading the report text")
        source = source_agent([Path(item["path"]) for item in manifest["inputs"]], manifest["doc_id"],
                              max_chunk_chars=min(2000, max(1, config.batch_max_chars // 4)))
        write_json(run_dir / "source.json", source)
    stage("report_text", "completed")
    if translate and not (run_dir / "translated.json").is_file():
        stage("translation", "running")
        log("Translating the report text to English")
        llm = client or ChatClient.from_config(config.llm)
        translated = translation_agent(llm, read_json(run_dir / "source.json"), config=config)
        write_json(run_dir / "translated.json", translated)
        manifest = read_json(run_dir / "manifest.json")
        record_translation_outcome(manifest, translated)
        save_manifest(run_dir, manifest)
    stage("translation", "completed" if (run_dir / "translated.json").is_file() else "skipped")
    stage("localization", "running")


def finish_stage(run_dir: Path, status: str) -> None:
    manifest = read_json(run_dir / "manifest.json")
    if not is_provided(manifest):
        return
    stages = manifest.setdefault("stages", {})
    for name, value in stages.items():
        if value == "running":
            stages[name] = status
    manifest["updated_at"] = utc_now()
    save_manifest(run_dir, manifest)


def summary(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    settings = (manifest.get("localization") or {}).get("settings") or {}
    table = manifest.get("segment_table") or {}
    return {
        "event": manifest.get("event"),
        "reports": [item.get("filename") for item in manifest.get("inputs", [])],
        "table": table.get("original_name"), "segments": table.get("segments"),
        "package": display_path(settings.get("package") or ""),
        "spatial_files": [display_path(path) for path in (manifest.get("provided_spatial") or {}).get("files") or []],
        "stages": [{"name": name, "label": label, "status": manifest.get("stages", {}).get(name, "pending")}
                   for name, label in STAGES],
    }


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:60] or "localization"


def resolve_existing(value: str, label: str, *, directory: bool = False) -> Path:
    path = resolve_path(value.strip().strip('"'))
    if not (path.is_dir() if directory else path.is_file()):
        raise PipelineError(f"{label}: {'folder' if directory else 'file'} not found: {value.strip()}")
    return path
