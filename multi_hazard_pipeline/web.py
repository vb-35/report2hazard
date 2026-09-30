from __future__ import annotations

import atexit
import hashlib
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
from .pipeline import create_run, execute_run, new_run_id, save_manifest, utc_now
from .agents.source_agent import discover_inputs
from .splitter import split_event_reports


SUPPORTED_SUFFIXES = {".docx", ".pdf", ".txt"}
DOWNLOADS = {"candidate_report.json", "final_rows.json", "final_rows.csv"}
STATUS_LABELS = {
    "running": "Processing",
    "revision_required": "Needs automatic revision",
    "awaiting_human_review": "Awaiting human review",
    "rejected": "Rejected",
    "failed": "Failed",
    "approved": "Approved",
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
    if not issue_type and not comment and not segment:
        return []
    if not issue_type:
        raise PipelineError("an issue type is required for a per-segment comment")
    return [{"segment": segment or None, "issue_type": issue_type, "comment": comment}]


def _record_worker_failure(run_dir: Path, task: str, exc: Exception) -> None:
    try:
        manifest = read_json(run_dir / "manifest.json")
        manifest["status"] = "failed"
        manifest["current_stage"] = task
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


def _mark_queued_work(run_dir: Path, stage: str) -> None:
    with run_lock(run_dir):
        manifest = read_json(run_dir / "manifest.json")
        if manifest.get("status") != "awaiting_human_review":
            raise PipelineError("human review work can be queued only while awaiting human review")
        manifest["status"] = "running"
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
            split_paths = (
                split_event_reports(
                    selected[0],
                    root / ".splits" / run_id,
                    ChatClient.from_config(config.llm),
                    config,
                )
                if len(selected) == 1 and selected[0].suffix.lower() == ".pdf"
                else None
            )
            manifests = []
            if split_paths and split_paths != [selected[0].resolve()]:
                for path in split_paths:
                    manifests.append(
                        create_run(
                            path.parent,
                            root,
                            config,
                            input_paths=[path],
                            doc_id=_uploaded_report_id([path]),
                        )
                    )
            else:
                manifests.append(
                    create_run(
                        source_dir,
                        root,
                        config,
                        input_paths=selected,
                        doc_id=_uploaded_report_id(selected) if uploads else None,
                    )
                )
        except (OSError, PipelineError) as exc:
            return render_template(
                "index.html",
                runs=_list_runs(root),
                status_labels=STATUS_LABELS,
                error=str(exc),
            ), 400
        for manifest in manifests:
            run_dir = Path(manifest["artifact_dir"])
            _submit(app, "pipeline", run_dir, execute_run, run_dir, config)
        return redirect(url_for("run_detail", run_id=manifests[0]["run_id"]), code=303)

    @app.get("/runs/<run_id>")
    def run_detail(run_id: str):
        run_dir = _run_dir(app, run_id)
        manifest = read_json(run_dir / "manifest.json")
        return render_template(
            "run.html",
            manifest=manifest,
            status_label=STATUS_LABELS.get(manifest.get("status"), manifest.get("status", "Unknown")),
            candidate=_optional_json(run_dir, "candidate_report.json"),
            evaluation=_optional_json(run_dir, "self_evaluation.json"),
            human=_optional_json(run_dir, "human_review.json"),
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
                )
            }
        )

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
                if requested_stage not in {"translation", "segmentation", "categorization"}:
                    raise PipelineError(
                        "requested_stage must be translation, segmentation, or categorization"
                    )
                if requested_stage == "categorization" and not segment_ids:
                    raise PipelineError("categorization correction requires affected segment IDs")
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
            return str(exc), 409
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
            return f"invalid edit value: {exc}", 400
        try:
            _mark_queued_work(run_dir, "self_evaluation")
            _submit(
                app,
                "human_edit_evaluation",
                run_dir,
                apply_candidate_edits,
                run_dir,
                [{"segment": segment, "field": field, "new_value": raw_value}],
                config=config,
            )
        except PipelineError as exc:
            return str(exc), 409
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
