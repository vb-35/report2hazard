"""Local interface for the post-approval localization pipeline and standalone localization tests."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from flask import abort, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from . import provided_localization as provided
from .config import PipelineConfig
from .core import bounded_filename, read_json, resolve_path, run_lock
from .errors import PipelineError
from .pipeline import new_run_id, save_manifest, utc_now


BUSY = {"queued", "running"}
REPORT_SUFFIXES = {".docx", ".pdf", ".txt"}
OUTPUTS = {
    "segments_map.html": "Interactive map",
    "data_report.md": "Spatial report",
    "segment_results.csv": "Segment results CSV",
    "segment_results.json": "Segment results JSON",
    "segments.geojson": "GeoJSON",
    "segments_evidence.csv": "Spatial evidence CSV",
    "checks.json": "Validation checks",
    "README.md": "Readme",
}
STATUS_LABELS = {
    "queued": "Queued", "running": "Running", "delivered": "Delivered",
    "delivered_with_unresolved": "Delivered, some segments unresolved", "partial": "Partial run",
    "incomplete": "Incomplete", "review_incomplete": "Self-review incomplete",
    "revision_required": "Self-review found issues", "build_errors": "Map build errors",
    "validation_errors": "Validation errors", "failed": "Failed",
}


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", (status or "Not started").replace("_", " ").capitalize())


def _output_files(run_dir: Path) -> dict[str, Path]:
    folder = resolve_path(run_dir / "localization" / "outputs")
    files = {name: folder / name for name in OUTPUTS}
    files.update({path.name: path for path in folder.glob("*_review.zip")})
    return {name: path for name, path in files.items()
            if path.resolve().parent == folder and path.is_file()}


def _saved_json(path: Path, default):
    return read_json(path) if path.is_file() else default


def _segment_total(run_dir: Path, manifest: dict) -> int:
    settings = (manifest.get("localization") or {}).get("settings") or {}
    if settings.get("only"):
        return len(settings["only"])
    if (manifest.get("segment_table") or {}).get("segments"):
        return manifest["segment_table"]["segments"]
    return len(_saved_json(run_dir / "final_rows.json", {}).get("rows") or [])


def _path_field(name: str, label: str, required: bool = False) -> str | None:
    value = request.form.get(name, "").strip().strip('"')
    if not value:
        if required:
            raise PipelineError(f"{label}: enter a file path on this computer.")
        return None
    return str(provided.resolve_existing(value, label))


def _options(package: str | None = None, segments_table: str | None = None) -> dict:
    """Localization settings shared by both forms."""
    tokens = int(request.form.get("max_output_tokens", "24000"))
    if not 1024 <= tokens <= 100000:
        raise PipelineError("Output token limit must be between 1024 and 100000.")
    only = [value.strip() for value in request.form.get("only", "").split(",") if value.strip()] or None
    return dict(
        package=package or _path_field("package", "Spatial package JSON", required=True),
        segments_table=segments_table or _path_field("segments_table", "Alternative segment table"),
        figure_manifest=_path_field("figure_manifest", "Figure outline manifest"), only=only,
        vision=request.form.get("mode", "vision") == "vision",
        resume=request.form.get("resume") == "on",
        review=request.form.get("review") == "on",
        browser_check=request.form.get("browser_check") == "on",
        allow_network=request.form.get("allow_network") == "on",
        model=request.form.get("model", "").strip() or None,
        api_base_url=request.form.get("api_base_url", "").strip() or None,
        max_output_tokens=tokens,
    )


def register_localization(app, config: PipelineConfig, get_run_dir) -> None:
    root = Path(app.config["ARTIFACT_ROOT"])

    def data(run_dir):
        manifest = read_json(run_dir / "manifest.json")
        info = manifest.get("localization") or {}
        checkpoint = _saved_json(run_dir / "localization/llm/checkpoint.json", {})
        files = _output_files(run_dir)
        return dict(
            info=info, busy=info.get("status") in BUSY, status_label=status_label(info.get("status")),
            processed=len(checkpoint.get("answers") or {}), total=_segment_total(run_dir, manifest),
            model_calls=info.get("model_calls", checkpoint.get("model_calls", 0)),
            provided=provided.summary(run_dir, manifest) if provided.is_provided(manifest) else None,
            results=_saved_json(run_dir / "localization/outputs/segment_results.json", []),
            downloads=[dict(name=name, label=OUTPUTS.get(name, "Complete review ZIP"),
                            url=url_for("localization_output", run_id=run_dir.name, artifact=name))
                       for name in files],
            map_url=url_for("localization_output", run_id=run_dir.name, artifact="segments_map.html")
                    if "segments_map.html" in files else None,
        )

    def queue(run_dir: Path, options: dict, *, translate: bool | None = None) -> None:
        """Record the request and run it on the single pipeline worker."""
        # Import the GIS pipeline lazily: browsing extraction needs no GIS initialization.
        from segment_mapping.package import load_package

        load_package(options["package"], verify=False)
        with run_lock(run_dir):
            manifest = read_json(run_dir / "manifest.json")
            standalone = provided.is_provided(manifest)
            if manifest.get("status") != "approved" or manifest.get("report_mode") == "multi" or manifest.get("child_runs"):
                raise PipelineError("Approve this individual report before starting localization.")
            if (manifest.get("localization") or {}).get("status") in BUSY:
                raise PipelineError("Localization is already queued or running for this report.")
            folder = run_dir / "localization"
            if folder.exists() and any(folder.iterdir()) and not options["resume"]:
                raise PipelineError("Localization already exists. Select Resume to reuse its checkpoints with matching settings.")
            settings = dict(options, **({"translate": translate} if standalone else {}))
            manifest["localization"] = dict(status="queued", activity="Waiting for the pipeline worker",
                                           settings=settings, package=options["package"], queued_at=utc_now(), log=[])
            save_manifest(run_dir, manifest)

        def update(**changes):
            with run_lock(run_dir):
                current = read_json(run_dir / "manifest.json")
                current.setdefault("localization", {}).update(changes, updated_at=utc_now())
                save_manifest(run_dir, current)

        def log(message):
            with run_lock(run_dir):
                current = read_json(run_dir / "manifest.json")
                info = current.setdefault("localization", {})
                info["activity"] = str(message).strip()
                info["log"] = (info.get("log", []) + [dict(time=utc_now(), message=str(message).strip())])[-100:]
                call = re.search(r"model call (\d+):", str(message))
                if call:
                    info["model_calls"] = int(call[1])
                info["updated_at"] = utc_now()
                save_manifest(run_dir, current)

        def work():
            from .agents.localization_agent import localize_run

            try:
                update(status="running", activity="Preparing localization")
                if standalone:
                    provided.prepare_provided_run(run_dir, config, translate=bool(translate), log=log)
                result = localize_run(run_dir, config=config, log=log, **options)
                update(**result, activity="Localization finished")
                provided.finish_stage(run_dir, "completed")
            except Exception as exc:
                # A spatial failure must never revoke the report's extraction approval.
                update(status="failed", error=f"{type(exc).__name__}: {exc}",
                       activity="Localization failed", finished_at=utc_now())
                provided.finish_stage(run_dir, "failed")

        try:
            app.extensions["pipeline_executor"].submit(work)
        except Exception as exc:
            update(status="failed", error=f"Could not queue localization: {exc}")
            raise PipelineError(f"Could not queue localization: {exc}") from exc

    @app.get("/runs/<run_id>/localization")
    def localization_detail(run_id, error=None):
        run_dir = get_run_dir(run_id)
        manifest = read_json(run_dir / "manifest.json")
        if manifest.get("report_mode") == "multi" or manifest.get("child_runs"):
            abort(404)
        state = data(run_dir)
        settings = state["info"].get("settings") or {"package": state["info"].get("package", "")}
        return render_template("localization.html", manifest=manifest, state=state, settings=settings,
                               test=state["provided"], show_table=True, default_model=config.llm.model, error=error,
                               existing=(run_dir / "localization").exists())

    @app.get("/runs/<run_id>/localization/status")
    def localization_status(run_id):
        try:
            response = jsonify(data(get_run_dir(run_id)))
            response.headers["Cache-Control"] = "no-store"
            return response
        except (OSError, ValueError):
            return jsonify(error="Localization results are temporarily unavailable; retrying."), 503

    @app.post("/runs/<run_id>/localization")
    def start_localization(run_id):
        run_dir = get_run_dir(run_id)
        try:
            standalone = provided.is_provided(read_json(run_dir / "manifest.json"))
            queue(run_dir, _options(), translate=request.form.get("translate") == "on" if standalone else None)
        except (PipelineError, ValueError, OSError, KeyError, TypeError) as exc:
            return localization_detail(run_id, error=str(exc)), 409
        return redirect(url_for("localization_detail", run_id=run_id), code=303)

    def new_form(error=None, status=200):
        form = request.form
        # Keep the user's choices when the form is shown again after an error.
        settings = dict(vision=form.get("mode", "vision") == "vision", review=form.get("review") == "on",
                        browser_check=form.get("browser_check") == "on", allow_network=form.get("allow_network") == "on",
                        model=form.get("model"), api_base_url=form.get("api_base_url"),
                        max_output_tokens=form.get("max_output_tokens"), figure_manifest=form.get("figure_manifest"),
                        only=[value.strip() for value in form.get("only", "").split(",") if value.strip()]) if form else {}
        return render_template("localization_new.html", form=form, settings=settings, show_table=False, error=error,
                               default_model=config.llm.model, file_layers=provided.FILE_LAYERS), status

    @app.get("/localization/new")
    def new_localization():
        return new_form()

    @app.post("/localization/new")
    def start_provided_localization():
        uploads = [item for item in request.files.getlist("report_files") if item.filename]
        report_path = request.form.get("report_path", "").strip().strip('"')
        table_upload = request.files.get("segments_file")
        table_path = request.form.get("segments_path", "").strip().strip('"')
        upload_dir = run_dir = None
        try:
            if bool(uploads) == bool(report_path):
                raise PipelineError("Report: upload the report files or enter one file or folder path, not both.")
            if bool(table_upload and table_upload.filename) == bool(table_path):
                raise PipelineError("Segment table: upload a file or enter one file path, not both.")
            # Validate the segment table before copying anything.
            if table_path:
                table_file = provided.resolve_existing(table_path, "Segment table")
                table_name, raw = table_file.name, table_file.read_bytes()
            else:
                table_name, raw = table_upload.filename, table_upload.read()
            if Path(table_name).suffix.lower() not in provided.TABLE_SUFFIXES:
                raise PipelineError("Segment table: use tab-separated (.tsv), comma-separated (.csv) or .txt text. "
                                    "In Excel, save the sheet as “Text (Tab delimited)”.")
            try:
                table_text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                table_text = raw.decode("cp1252")
            _, segments, event = provided.prepare_table(table_text, request.form.get("event", ""))
            run_id = new_run_id(event or Path(table_name).stem)
            spatial = request.form.get("spatial_source", "package")
            if spatial == "package":
                package = _path_field("package", "Spatial package JSON", required=True)
            elif spatial == "files":
                dem = Path(_path_field("dem", "Elevation model (DEM)", required=True))
                layers = {}
                for role, label, required in provided.FILE_LAYERS:
                    path = _path_field(role, label, required=required)
                    if path:
                        layers[role] = dict(path=Path(path), name_field=request.form.get(f"{role}_name_field", "").strip() or None)
            else:
                raise PipelineError("Choose how to provide the spatial data.")
            _options(package="pending", segments_table="pending")  # validate settings before creating the run
            if uploads:
                upload_dir = root / ".uploads" / run_id
                upload_dir.mkdir(parents=True)
                reports, seen = [], set()
                for upload in uploads:
                    filename = bounded_filename(secure_filename(upload.filename or ""))
                    if not filename or Path(filename).suffix.lower() not in REPORT_SUFFIXES:
                        raise PipelineError(f"Unsupported report file: {upload.filename}")
                    if filename.casefold() in seen:
                        raise PipelineError(f"Duplicate report filename: {filename}")
                    seen.add(filename.casefold())
                    upload.save(upload_dir / filename)
                    reports.append(upload_dir / filename)
                source_dir = upload_dir
            else:
                path = resolve_path(report_path)
                if path.is_dir():
                    from .agents.source_agent import discover_inputs

                    source_dir, reports = path, discover_inputs(path)
                elif path.is_file() and path.suffix.lower() in REPORT_SUFFIXES:
                    source_dir, reports = path.parent, [path]
                else:
                    raise PipelineError(f"Report: no PDF, DOCX or TXT report found at {report_path}")
            run_dir, manifest = provided.create_provided_run(
                root, config, reports=reports, source_dir=source_dir, doc_id=provided.safe_slug(event or run_id),
                table_text=table_text, table_name=table_name, event=request.form.get("event", ""), run_id=run_id)
            if spatial == "files":
                package = str(provided.build_package(run_dir / "inputs" / "spatial_package.json",
                                                     name=f"{manifest['event']} provided data", crs=request.form.get("crs", "").strip(),
                                                     dem=dem, layers=layers))
                manifest["provided_spatial"] = {"mode": "files", "files": [str(dem)] + [str(item["path"]) for item in layers.values()]}
                save_manifest(run_dir, manifest)
            queue(run_dir, _options(package=package, segments_table=manifest["segment_table"]["path"]),
                  translate=request.form.get("translate") == "on")
        except (PipelineError, ValueError, OSError, KeyError, TypeError) as exc:
            # A refused test leaves neither copies nor a half-created run behind.
            for folder in (upload_dir, run_dir):
                if folder is not None:
                    shutil.rmtree(folder, ignore_errors=True)
            return new_form(str(exc), 409 if run_dir else 400)
        return redirect(url_for("localization_detail", run_id=run_dir.name), code=303)

    @app.get("/runs/<run_id>/localization/output/<artifact>")
    def localization_output(run_id, artifact):
        path = _output_files(get_run_dir(run_id)).get(artifact)
        if path is None:
            abort(404)
        response = send_file(path, as_attachment=artifact != "segments_map.html", download_name=artifact)
        if artifact == "segments_map.html":
            response.headers["Content-Security-Policy"] = "sandbox allow-scripts allow-downloads"
        response.headers["Cache-Control"] = "no-store"
        return response
