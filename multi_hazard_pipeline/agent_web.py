"""Interface pages for free agent runs (Claude Code or Codex working through the segment_mapping toolkit)."""
from __future__ import annotations

import shutil
from pathlib import Path

from flask import abort, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from . import agent_runs
from .core import bounded_filename, read_json, resolve_path
from .errors import PipelineError
from .pipeline import new_run_id

PROJECT = Path(__file__).resolve().parents[1]


def _path(name: str) -> str | None:
    value = request.form.get(name, "").strip().strip('"')
    return value or None


def _state(run_dir: Path) -> dict:
    from .localization_web import _output_files, OUTPUTS

    manifest = read_json(run_dir / "manifest.json")
    files = _output_files(run_dir)
    return dict(
        status=manifest.get("status"), status_label=agent_runs.STATUS_LABELS.get(manifest.get("status"), manifest.get("status")),
        busy=manifest.get("status") in agent_runs.BUSY, activity=manifest.get("activity") or "",
        error=manifest.get("error"), log=manifest.get("log", [])[-60:],
        started_at=manifest.get("started_at"), finished_at=manifest.get("finished_at"),
        map_url=url_for("localization_output", run_id=run_dir.name, artifact="segments_map.html")
                if "segments_map.html" in files else None,
        downloads=[dict(label=OUTPUTS.get(name, "Complete review ZIP"), name=name,
                        url=url_for("localization_output", run_id=run_dir.name, artifact=name)) for name in files],
        logs=[dict(label=agent_runs.LOG_FILES[name], name=name,
                   url=url_for("agent_run_file", run_id=run_dir.name, name=name)) for name in agent_runs.log_files(run_dir)],
    )


def register_agent_runs(app, get_run_dir) -> None:
    root = Path(app.config["ARTIFACT_ROOT"])

    def form_page(error=None, status=200):
        source_id = request.values.get("from_run") or None
        source_manifest = None
        if source_id:
            try:
                source_manifest = read_json(get_run_dir(source_id) / "manifest.json")
            except Exception:  # noqa: BLE001 - an unknown run simply gives an empty form
                source_manifest = None
        source_report = ""
        if source_manifest:
            try:
                source_report = str(agent_runs.report_from_run(source_manifest))
            except PipelineError:
                source_report = ""
        results_in_project = resolve_path(root).is_relative_to(PROJECT)
        return render_template("agent_new.html", form=request.form, error=error, runners=agent_runs.RUNNERS,
                               available=agent_runs.runner_availability(), source=source_manifest,
                               source_report=source_report, results_in_project=results_in_project,
                               project=str(PROJECT)), status

    @app.get("/agent-runs/new")
    def new_agent_run():
        return form_page()

    @app.post("/agent-runs/new")
    def start_agent_run():
        upload_dir = None
        run_id = new_run_id(request.form.get("event", "") or "agent")
        try:
            source_id = request.form.get("from_run") or None
            source_dir = get_run_dir(source_id) if source_id else None
            # Report: a path on this computer, an uploaded PDF, or the PDF of the chosen pipeline run.
            upload = request.files.get("report_file")
            report = _path("report_path")
            if upload and upload.filename:
                if report:
                    raise PipelineError("Report: upload a PDF or enter a path, not both.")
                name = bounded_filename(secure_filename(upload.filename))
                if not name.lower().endswith(".pdf"):
                    raise PipelineError("Report: agent runs read a PDF report.")
                upload_dir = root / ".uploads" / run_id
                upload_dir.mkdir(parents=True)
                report = str(upload_dir / name)
                upload.save(report)
            elif not report and source_dir is not None:
                report = str(agent_runs.report_from_run(read_json(source_dir / "manifest.json")))
            if not report:
                raise PipelineError("Report: upload the PDF or enter its path.")
            # Segment table: the approved rows of the chosen run, an upload, or a path.
            table_upload = request.files.get("segments_file")
            table_path = _path("segments_path")
            if table_upload and table_upload.filename:
                if table_path:
                    raise PipelineError("Segment table: upload a file or enter a path, not both.")
                table_text = table_upload.read().decode("utf-8-sig")
            elif table_path:
                table_text = Path(table_path).read_text(encoding="utf-8-sig")
            elif source_dir is not None:
                table_text = agent_runs.table_from_run(source_dir)
            else:
                raise PipelineError("Segment table: upload a file or enter its path.")
            run_dir = agent_runs.create_agent_run(
                root, report=Path(report), table_text=table_text, event=request.form.get("event", ""),
                runner=request.form.get("runner", ""), model=request.form.get("model"),
                effort=request.form.get("effort"), full_access=request.form.get("full_access") == "on",
                dem=_path("dem"), dem_info=request.form.get("dem_info", "").strip(),
                catchment=_path("catchment"), catchment_info=request.form.get("catchment_info", "").strip(),
                source_run_id=source_id, run_id=run_id)
        except (PipelineError, ValueError, OSError, UnicodeDecodeError) as exc:
            if upload_dir is not None:
                shutil.rmtree(upload_dir, ignore_errors=True)
            return form_page(str(exc), 409)
        agent_runs.start_agent_run(run_dir)
        return redirect(url_for("agent_run_detail", run_id=run_dir.name), code=303)

    @app.get("/agent-runs/<run_id>")
    def agent_run_detail(run_id, error=None):
        run_dir = get_run_dir(run_id)
        manifest = read_json(run_dir / "manifest.json")
        if not agent_runs.is_launched(manifest):
            abort(404)
        from .localization_compare import comparable_groups

        agent = manifest.get("agent") or {}
        group = next((g for g in comparable_groups(root) if any(r["run_id"] == run_id for r in g["runs"])), None)
        compare_url = url_for("compare_localizations", report=group["signature"],
                              runs=[r["run_id"] for r in group["runs"]][:6]) if group else None
        return render_template("agent_run.html", manifest=manifest, agent=agent, state=_state(run_dir),
                               runner_label=agent_runs.RUNNERS.get(agent.get("runner"), {}).get("label", "Agent"),
                               compare_url=compare_url, error=error)

    @app.get("/agent-runs/<run_id>/status")
    def agent_run_status(run_id):
        run_dir = get_run_dir(run_id)
        try:
            response = jsonify(_state(run_dir))
        except (OSError, ValueError):
            return jsonify(error="The run is temporarily unavailable; retrying."), 503
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/agent-runs/<run_id>/stop")
    def stop_agent_run(run_id):
        run_dir = get_run_dir(run_id)
        try:
            agent_runs.stop_agent_run(run_dir)
        except PipelineError as exc:
            return agent_run_detail(run_id, error=str(exc)), 409
        return redirect(url_for("agent_run_detail", run_id=run_id), code=303)

    @app.get("/agent-runs/<run_id>/files/<name>")
    def agent_run_file(run_id, name):
        path = agent_runs.log_files(get_run_dir(run_id)).get(name)
        if path is None:
            abort(404)
        inline = name.endswith(".txt") or name.endswith(".json")
        response = send_file(path, as_attachment=not inline, download_name=name,
                             mimetype="text/plain; charset=utf-8" if inline else None)
        response.headers["Cache-Control"] = "no-store"
        return response
