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
from .core import read_json, run_lock
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
    "running": "Processing",
    "revision_required": "Needs automatic revision",
    "awaiting_human_review": "Awaiting human review",
    "rejected": "Rejected",
    "failed": "Failed",
    "approved": "Approved",
    "split": "Collection separated",
}


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


def _list_runs(root: Path) -> list[dict[str, Any]]:
    runs = []
    for manifest_path in root.glob("*/manifest.json"):
        try:
            runs.append(read_json(manifest_path))
        except (OSError, ValueError):
            continue
    return sorted(runs, key=lambda item: item.get("created_at", ""), reverse=True)


def _prepare_and_execute(run_dir: Path, config: PipelineConfig) -> None:
    """Run PDF preparation and event extraction on the same single worker."""
    manifest = read_json(run_dir / "manifest.json")
    selected = [Path(item["path"]) for item in manifest["inputs"]]
    if len(selected) != 1 or selected[0].suffix.lower() != ".pdf":
        execute_run(run_dir, config)
        return
    manifest["current_stage"] = "preparation"
    manifest["stages"]["preparation"] = "running"
    manifest["progress"] = 5
    save_manifest(run_dir, manifest)
    split_paths = split_event_reports(
        selected[0], run_dir.parent / ".splits" / manifest["run_id"],
        ChatClient.from_config(config.llm), config,
    )
    if not split_paths:
        raise PipelineError("PDF preparation produced no event reports")
    manifest["stages"]["preparation"] = "completed"
    if split_paths == [selected[0].resolve()]:
        save_manifest(run_dir, manifest)
        execute_run(run_dir, config)
        return
    manifest["child_runs"] = []
    for path in split_paths:
        child = create_run(
            path.parent, run_dir.parent, config,
            input_paths=[path], doc_id=_uploaded_report_id([path]),
        )
        manifest["child_runs"].append({"run_id": child["run_id"], "filename": path.name})
        save_manifest(run_dir, manifest)
    manifest["status"] = "split"
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
    root = Path(artifact_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    app.config.update(ARTIFACT_ROOT=str(root), PIPELINE_CONFIG=config, MAX_CONTENT_LENGTH=100 * 1024 * 1024)
    owned_executor = executor is None
    app.extensions["pipeline_executor"] = executor or ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="multi-hazard"
    )
    if owned_executor:
        atexit.register(app.extensions["pipeline_executor"].shutdown, wait=False)

    @app.get("/")
    def index():
        return render_template("index.html", runs=_list_runs(root), status_labels=STATUS_LABELS)

    @app.post("/runs")
    def start_run():
        uploads = [item for item in request.files.getlist("files") if item.filename]
        input_dir = request.form.get("input_dir", "").strip()
        if bool(uploads) == bool(input_dir):
            return render_template(
                "index.html",
                runs=_list_runs(root),
                status_labels=STATUS_LABELS,
                error="Choose uploaded files or one existing input directory.",
            ), 400
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
                source_dir = Path(input_dir).resolve()
                selected = discover_inputs(source_dir)
            manifest = create_run(
                source_dir, root, config, input_paths=selected,
                doc_id=_uploaded_report_id(selected) if uploads else None,
            )
        except (OSError, PipelineError) as exc:
            return render_template(
                "index.html",
                runs=_list_runs(root),
                status_labels=STATUS_LABELS,
                error=str(exc),
            ), 400
        run_dir = Path(manifest["artifact_dir"])
        task = "preparation" if len(selected) == 1 and selected[0].suffix.lower() == ".pdf" else "pipeline"
        if task == "preparation":
            manifest["stages"][task] = "pending"
            save_manifest(run_dir, manifest)
        _submit(app, task, run_dir, _prepare_and_execute, run_dir, config)
        return redirect(url_for("run_detail", run_id=manifest["run_id"]), code=303)

    @app.get("/runs/<run_id>")
    def run_detail(run_id: str, error: str | None = None):
        run_dir = _run_dir(app, run_id)
        manifest = read_json(run_dir / "manifest.json")
        return render_template(
            "run.html",
            manifest=manifest,
            status_label=STATUS_LABELS.get(manifest.get("status"), manifest.get("status", "Unknown")),
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
        path = Path(inputs[index].get("path", ""))
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
