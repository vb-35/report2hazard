from __future__ import annotations

import atexit
import hashlib
import json
import re
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from .config import DEFAULT_CONFIG, PipelineConfig
from .core import read_json, resolve_path, run_lock
from .errors import PipelineError
from .human_review import apply_candidate_edits, approve_run, reject_run, request_correction
from .llm import ChatClient
from .pipeline import create_run, execute_run, invalidate_results, new_run_id, save_manifest, utc_now
from .agents.source_agent import discover_inputs
from .splitter import split_event_reports
from .workspace import workspace_data


SUPPORTED_SUFFIXES = {".docx", ".pdf", ".txt"}
DOWNLOADS = {"candidate_report.json", "final_rows.json", "final_rows.csv"}
STATUS_LABELS = {
    "queued": "Queued",
    "running": "Running",
    "revision_required": "Needs automatic revision",
    "awaiting_human_review": "Awaiting human review",
    "rejected": "Rejected",
    "failed": "Failed",
    "approved": "Approved",
    "split": "Reports separated",
    "completed": "Completed",
}
REPORT_MODES = {"single", "multi"}
REVIEWABLE = {"awaiting_human_review", "revision_required"}


def _uploaded_report_id(paths: list[Path]) -> str:
    names = "\n".join(sorted(path.name.casefold() for path in paths))
    digest = hashlib.sha256(names.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", paths[0].stem.casefold()).strip("-") or "report"
    return f"{slug}-{digest}"


def _run_dir(app: Flask, run_id: str) -> Path:
    root = Path(app.config["ARTIFACT_ROOT"])
    candidate = (root / run_id).resolve()
    if not run_id or Path(run_id).name != run_id or candidate.parent != root:
        abort(404)
    if not (candidate / "manifest.json").is_file():
        abort(404)
    return candidate


def _optional_json(run_dir: Path, name: str) -> dict[str, Any] | None:
    path = run_dir / name
    return read_json(path) if path.is_file() else None


def _segment_comments() -> list[dict[str, Any]]:
    issue_type = request.form.get("issue_type", "").strip()
    comment = request.form.get("segment_comment", "").strip()
    segment = request.form.get("comment_segment", "").strip()
    if not issue_type and not comment:
        return []
    if not issue_type:
        raise PipelineError("an issue type is required for a per-segment comment")
    return [{"segment": segment or None, "issue_type": issue_type, "comment": comment}]


def _record_worker_failure(run_dir: Path, task: str, exc: Exception) -> None:
    try:
        manifest = read_json(run_dir / "manifest.json")
        manifest["status"] = "failed"
        failed_stage = task if task in manifest.get("stages", {}) else manifest.get("current_stage", task)
        if failed_stage not in manifest.get("stages", {}):
            failed_stage = task
        manifest["current_stage"] = failed_stage
        if failed_stage in manifest.get("stages", {}):
            manifest["stages"][failed_stage] = "failed"
        manifest.setdefault("errors", []).append(
            {
                "stage": task,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
                "timestamp": utc_now(),
            }
        )
        save_manifest(run_dir, manifest)
    except Exception:
        # The original exception remains available on the executor Future even
        # when a corrupt/missing manifest prevents structured diagnostics.
        pass


def _mark_queued_work(run_dir: Path, stage: str, *, editing: bool = False) -> None:
    with run_lock(run_dir):
        manifest = read_json(run_dir / "manifest.json")
        if manifest.get("status") not in {"awaiting_human_review", "revision_required"}:
            raise PipelineError("human review work requires a reviewable candidate")
        manifest["status"] = "running"
        invalidate_results(run_dir, manifest, "segmentation" if editing else stage)
        manifest["current_stage"] = stage
        if stage in manifest.get("stages", {}):
            manifest["stages"][stage] = "running"
        save_manifest(run_dir, manifest)


def _submit(app: Flask, task: str, run_dir: Path, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    def work() -> None:
        try:
            operation(*args, **kwargs)
        except Exception as exc:  # background failures must remain inspectable
            _record_worker_failure(run_dir, task, exc)

    try:
        app.extensions["pipeline_executor"].submit(work)
    except Exception as exc:
        _record_worker_failure(run_dir, task, exc)
        raise PipelineError(f"could not queue {task}: {exc}") from exc


def _display_status(manifest: dict[str, Any]) -> str:
    # Runs wait in the single-worker queue with status "running"; show them as queued.
    if manifest.get("status") == "running" and manifest.get("current_stage") == "queued":
        return "queued"
    return manifest.get("status") or "unknown"


def _is_collection(manifest: dict[str, Any]) -> bool:
    return manifest.get("report_mode") == "multi" or bool(manifest.get("child_runs"))


def _title(manifest: dict[str, Any]) -> str:
    return ", ".join(item["filename"] for item in manifest.get("inputs", [])) or manifest.get("run_id", "")


def _collection_data(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Summarize a multi-report collection from its live child run manifests."""
    reports = []
    for index, child in enumerate(manifest.get("child_runs") or [], 1):
        try:
            child_manifest = read_json(root / child["run_id"] / "manifest.json")
        except (OSError, ValueError):
            child_manifest = {}
        reports.append({
            "index": index,
            "run_id": child["run_id"],
            "filename": child["filename"],
            "status": _display_status(child_manifest),
            "current_stage": child_manifest.get("current_stage"),
            "progress": child_manifest.get("progress") or 0,
            "url": url_for("run_detail", run_id=child["run_id"]),
        })
    statuses = {report["status"] for report in reports}
    processing = statuses & {"running", "queued"}
    parent = _display_status(manifest)
    active = next((report for report in reports if report["status"] == "running"), None)
    if parent != "split" or not reports:
        status = parent
    elif "running" in statuses:
        status = "running"
    elif "queued" in statuses:
        status = "queued"
    elif statuses & REVIEWABLE:
        status = "awaiting_human_review"
    elif statuses == {"approved"}:
        status = "approved"
    else:
        status = "completed"
    preparation = manifest.get("stages", {}).get("preparation", "pending")
    if status == "queued" and not reports:
        activity = "Waiting for another task to finish"
    elif preparation == "running":
        activity = "Separating the PDF into individual reports"
    elif active:
        stage = (active["current_stage"] or "").replace("_", " ")
        activity = f"Extracting report {active['index']} of {len(reports)} · {stage}"
    elif processing:
        activity = "Waiting for the next report to start"
    elif reports:
        done = sum(report["status"] in {"approved", "rejected"} for report in reports)
        activity = f"{done} of {len(reports)} report(s) reviewed"
    else:
        activity = (manifest.get("current_stage") or "").replace("_", " ")
    if reports:
        extraction = "running" if processing else "failed" if statuses == {"failed"} else "completed"
        review = ("awaiting" if statuses & REVIEWABLE else "completed"
                  if not processing and statuses <= {"approved", "rejected", "failed"} else "pending")
    else:
        extraction = review = "pending"
    return {
        "run_id": manifest.get("run_id"),
        "title": _title(manifest),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "activity": activity,
        "progress": round(sum(report["progress"] for report in reports) / len(reports))
        if reports else manifest.get("progress") or 0,
        "stages": [
            {"name": "preparation", "label": "Report separation", "status": preparation},
            {"name": "extraction", "label": "Report extraction", "status": extraction},
            {"name": "human_review", "label": "Human review", "status": review},
        ],
        "reports": reports,
        "warnings": manifest.get("warnings", []),
        "errors": manifest.get("errors", []),
    }


def _list_runs(root: Path) -> list[dict[str, Any]]:
    manifests = []
    for manifest_path in root.glob("*/manifest.json"):
        try:
            manifests.append(read_json(manifest_path))
        except (OSError, ValueError):
            continue
    # Reports separated from a collection are listed inside their collection.
    children = {child["run_id"] for manifest in manifests for child in manifest.get("child_runs") or []}
    runs = []
    for manifest in manifests:
        if manifest.get("run_id") in children or manifest.get("parent_run_id"):
            continue
        if _is_collection(manifest):
            summary = _collection_data(root, manifest)
            runs.append({**summary, "collection": True, "created_at": manifest.get("created_at", ""),
                         "report_count": len(summary["reports"])})
        else:
            status = _display_status(manifest)
            runs.append({
                "run_id": manifest.get("run_id"), "title": _title(manifest), "collection": False,
                "status": status, "status_label": STATUS_LABELS.get(status, status),
                "activity": (manifest.get("current_stage") or "").replace("_", " ").capitalize(),
                "progress": manifest.get("progress") or 0, "created_at": manifest.get("created_at", ""),
            })
    return sorted(runs, key=lambda item: item["created_at"], reverse=True)


def _collection_position(root: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Locate a separated report within its collection for previous/next navigation."""
    parent_id = manifest.get("parent_run_id")
    candidates = [root / parent_id / "manifest.json"] if parent_id else root.glob("*/manifest.json")
    for path in candidates:
        try:
            parent = read_json(path)
        except (OSError, ValueError):
            continue
        children = [child["run_id"] for child in parent.get("child_runs") or []]
        if manifest["run_id"] not in children:
            continue
        index = children.index(manifest["run_id"])
        return {
            "run_id": parent["run_id"],
            "title": _title(parent),
            "index": index + 1,
            "count": len(children),
            "previous": children[index - 1] if index > 0 else None,
            "next": children[index + 1] if index + 1 < len(children) else None,
        }
    return None


def _prepare_collection(run_dir: Path, config: PipelineConfig) -> None:
    """Separate a multi-report PDF, then extract every report as its own run on the same worker."""
    manifest = read_json(run_dir / "manifest.json")
    source = Path(manifest["inputs"][0]["path"])
    manifest["current_stage"] = "preparation"
    manifest["stages"]["preparation"] = "running"
    manifest["progress"] = 5
    save_manifest(run_dir, manifest)
    split_paths = split_event_reports(
        source, run_dir.parent / ".splits" / manifest["run_id"],
        ChatClient.from_config(config.llm), config,
    )
    if not split_paths:
        raise PipelineError("PDF preparation produced no event reports")
    if split_paths == [resolve_path(source)]:
        manifest["warnings"].append({"stage": "preparation", "message": "Only one event report was detected in this PDF."})
    manifest["child_runs"] = []
    for path in split_paths:
        child = create_run(
            path.parent, run_dir.parent, config,
            input_paths=[path], doc_id=_uploaded_report_id([path]),
        )
        child["parent_run_id"] = manifest["run_id"]
        save_manifest(Path(child["artifact_dir"]), child)
        manifest["child_runs"].append({"run_id": child["run_id"], "filename": path.name})
        save_manifest(run_dir, manifest)
    # Every report is queued before preparation completes, so extraction shows as running at once.
    manifest["stages"]["preparation"] = "completed"
    manifest["status"] = "split"
    manifest["current_stage"] = "extraction"
    manifest["progress"] = 100
    save_manifest(run_dir, manifest)
    for child in manifest["child_runs"]:
        execute_run(run_dir.parent / child["run_id"], config)


def create_app(
    artifact_root: str | Path = "results",
    *,
    config: PipelineConfig = DEFAULT_CONFIG,
    executor: Any | None = None,
) -> Flask:
    app = Flask(__name__)
    root = resolve_path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    app.config.update(ARTIFACT_ROOT=str(root), PIPELINE_CONFIG=config, MAX_CONTENT_LENGTH=100 * 1024 * 1024)
    owned_executor = executor is None
    app.extensions["pipeline_executor"] = executor or ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="multi-hazard"
    )
    if owned_executor:
        atexit.register(app.extensions["pipeline_executor"].shutdown, wait=False)

    def index_error(message: str):
        return render_template("index.html", runs=_list_runs(root), error=message), 400

    @app.get("/")
    def index():
        return render_template("index.html", runs=_list_runs(root))

    @app.post("/runs")
    def start_run():
        uploads = [item for item in request.files.getlist("files") if item.filename]
        input_dir = request.form.get("input_dir", "").strip()
        report_mode = request.form.get("report_mode", "")
        if report_mode not in REPORT_MODES:
            return index_error("Choose whether this is a single report or a multi-report collection.")
        if bool(uploads) == bool(input_dir):
            return index_error("Choose uploaded files or one existing input directory.")
        try:
            run_id = new_run_id()
            if uploads:
                upload_dir = root / ".uploads" / run_id
                upload_dir.mkdir(parents=True)
                selected: list[Path] = []
                seen: set[str] = set()
                for upload in uploads:
                    filename = secure_filename(upload.filename or "")
                    if not filename or Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
                        raise PipelineError(f"unsupported report file: {upload.filename}")
                    if filename.casefold() in seen:
                        raise PipelineError(f"duplicate uploaded filename: {filename}")
                    seen.add(filename.casefold())
                    path = upload_dir / filename
                    upload.save(path)
                    selected.append(path)
                source_dir = upload_dir
            else:
                source_dir = resolve_path(input_dir)
                selected = discover_inputs(source_dir)
            if report_mode == "multi" and (len(selected) != 1 or selected[0].suffix.lower() != ".pdf"):
                raise PipelineError("A multi-report collection must be exactly one PDF file.")
            manifest = create_run(
                source_dir, root, config, input_paths=selected,
                doc_id=_uploaded_report_id(selected) if uploads else None,
            )
        except (OSError, PipelineError) as exc:
            return index_error(str(exc))
        run_dir = Path(manifest["artifact_dir"])
        manifest["report_mode"] = report_mode
        if report_mode == "multi":
            # A collection only separates reports; each report gets its own child run.
            manifest["stages"] = {"preparation": "pending"}
            save_manifest(run_dir, manifest)
            _submit(app, "preparation", run_dir, _prepare_collection, run_dir, config)
        else:
            save_manifest(run_dir, manifest)
            _submit(app, "pipeline", run_dir, execute_run, run_dir, config)
        return redirect(url_for("run_detail", run_id=manifest["run_id"]), code=303)

    @app.get("/runs/<run_id>")
    def run_detail(run_id: str, error: str | None = None):
        run_dir = _run_dir(app, run_id)
        manifest = read_json(run_dir / "manifest.json")
        if _is_collection(manifest):
            return render_template("collection.html", collection=_collection_data(root, manifest))
        status = _display_status(manifest)
        return render_template(
            "run.html",
            manifest=manifest,
            status_label=STATUS_LABELS.get(status, status),
            collection=_collection_position(root, manifest),
            candidate=_optional_json(run_dir, "candidate_report.json"),
            evaluation=_optional_json(run_dir, "self_evaluation.json"),
            human=_optional_json(run_dir, "human_review.json"),
            controlled_labels=config.controlled_labels(),
            workspace=workspace_data(run_dir),
            error=error,
        )

    @app.get("/runs/<run_id>/status")
    def run_status(run_id: str):
        manifest = read_json(_run_dir(app, run_id) / "manifest.json")
        return jsonify(
            {
                key: manifest.get(key)
                for key in (
                    "run_id",
                    "status",
                    "current_stage",
                    "progress",
                    "correction_rounds",
                    "max_correction_rounds",
                    "errors",
                    "warnings",
                    "updated_at",
                    "stages",
                )
            }
        )

    @app.get("/runs/<run_id>/collection")
    def collection_status(run_id: str):
        manifest = read_json(_run_dir(app, run_id) / "manifest.json")
        if not _is_collection(manifest):
            abort(404)
        response = jsonify(_collection_data(root, manifest))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/runs/<run_id>/workspace")
    def run_workspace(run_id: str):
        try:
            since = json.loads(request.args.get("since", "{}"))
        except ValueError:
            abort(400)
        if not isinstance(since, dict):
            abort(400)
        try:
            response = jsonify(workspace_data(_run_dir(app, run_id), since))
            response.headers["Cache-Control"] = "no-store"
            return response
        except (ValueError, OSError, RuntimeError):
            return jsonify(error="Saved results are temporarily unavailable; retrying."), 503

    @app.get("/runs/<run_id>/source/<document_id>")
    def source_file(run_id: str, document_id: str):
        manifest = read_json(_run_dir(app, run_id) / "manifest.json")
        # Identifiers are canonical indexes into this run's allowlist, never filesystem paths.
        if not re.fullmatch(r"0|[1-9][0-9]{0,9}", document_id):
            abort(404)
        inputs = manifest.get("inputs", [])
        index = int(document_id)
        if index >= len(inputs):
            abort(404)
        path = resolve_path(inputs[index].get("path", ""))
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            abort(404)
        return send_file(path, as_attachment=path.suffix.lower() != ".pdf", download_name=inputs[index].get("filename", path.name))

    @app.post("/runs/<run_id>/decision")
    def decide(run_id: str):
        run_dir = _run_dir(app, run_id)
        decision = request.form.get("decision", "")
        try:
            common = {
                "global_comment": request.form.get("global_comment", ""),
                "segment_comments": _segment_comments(),
            }
            if decision == "approve":
                approve_run(run_dir, config=config, **common)
            elif decision == "reject":
                reject_run(run_dir, **common)
            elif decision == "request_correction":
                requested_stage = request.form.get("requested_stage", "")
                ids_text = request.form.get("segment_ids", "")
                segment_ids = [int(item.strip()) for item in ids_text.split(",") if item.strip()]
                with run_lock(run_dir):
                    request_correction(
                        run_dir, requested_stage=requested_stage, segment_ids=segment_ids,
                        config=config, validate_only=True, **common,
                    )
                    _mark_queued_work(run_dir, requested_stage)
                _submit(
                    app,
                    f"human_{requested_stage}_correction",
                    run_dir,
                    request_correction,
                    run_dir,
                    requested_stage=requested_stage,
                    segment_ids=segment_ids,
                    config=config,
                    **common,
                )
            else:
                raise PipelineError("unknown human decision")
        except (PipelineError, ValueError) as exc:
            return run_detail(run_id, error=str(exc)), 409
        except OSError as exc:
            if read_json(run_dir / "manifest.json").get("status") != "failed":
                _record_worker_failure(run_dir, "human_decision", exc)
            return "human decision failed; diagnostics were recorded", 500
        return redirect(url_for("run_detail", run_id=run_id), code=303)

    @app.post("/runs/<run_id>/edit")
    def edit_candidate(run_id: str):
        run_dir = _run_dir(app, run_id)
        try:
            segment = int(request.form.get("segment", ""))
            field = request.form.get("field", "")
            raw_value: Any = request.form.get("new_value", "")
            if field == "predecessor_segment_ids":
                raw_value = [int(item.strip()) for item in raw_value.split(",") if item.strip()]
        except ValueError as exc:
            return run_detail(run_id, error=f"invalid edit value: {exc}"), 400
        try:
            edits = [{"segment": segment, "field": field, "new_value": raw_value}]
            with run_lock(run_dir):
                apply_candidate_edits(run_dir, edits, config=config, validate_only=True)
                _mark_queued_work(run_dir, "self_evaluation", editing=True)
            _submit(
                app,
                "human_edit_evaluation",
                run_dir,
                apply_candidate_edits,
                run_dir,
                edits,
                config=config,
            )
        except (PipelineError, ValueError) as exc:
            return run_detail(run_id, error=str(exc)), 409
        return redirect(url_for("run_detail", run_id=run_id), code=303)

    @app.get("/runs/<run_id>/download/<artifact>")
    def download(run_id: str, artifact: str):
        if artifact not in DOWNLOADS:
            abort(404)
        run_dir = _run_dir(app, run_id)
        manifest = read_json(run_dir / "manifest.json")
        if artifact.startswith("final_rows") and manifest.get("status") != "approved":
            abort(404)
        path = (run_dir / artifact).resolve()
        if path.parent != run_dir or not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=artifact)

    return app


def main() -> None:
    create_app().run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
