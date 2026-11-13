"""Free agent localization runs started from the interface (Claude Code or Codex).

An agent run is a results folder of its own (manifest kind "agent_localization", agent.launched = true). The agent
works through the segment_mapping toolkit in RUN/localization, so its outputs have the same layout as a pipeline
localization and Compare reads them the same way. The launcher's prompt, transcript and summary are kept in
RUN/localization/agent/.
"""
from __future__ import annotations

import argparse
import atexit
import csv
import io
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from .core import read_json, run_lock, worker_marker, write_json
from .errors import PipelineError
from .pipeline import new_run_id, save_manifest, utc_now

AGENT_KIND = "agent_localization"
RUNNERS: dict[str, dict[str, Any]] = {
    "claude": {"label": "Claude Code", "efforts": ("low", "medium", "high", "xhigh", "max"),
               "model_hint": "e.g. claude-opus-5-5 or claude-sonnet-5-5; empty = Claude Code default"},
    "codex": {"label": "Codex", "efforts": ("minimal", "low", "medium", "high", "xhigh"),
              "model_hint": "empty = Codex default model"},
}
BUSY = {"queued", "running", "stopping"}
STATUS_LABELS = {"queued": "Waiting to start", "running": "Agent working", "stopping": "Stopping",
                 "completed": "Map delivered", "failed": "Failed", "stopped": "Stopped"}
LOG_FILES = {"prompt.txt": "Prompt", "transcript.jsonl": "Transcript (JSON lines)",
             "final_message.txt": "Agent's final message", "agent_run.json": "Run summary"}
TABLE_HEADER = ["Segment", "Event", "Process", "Generalized Category", "Interaction Type", "Sediment Transport Phase"]

_PROCESSES: dict[str, Any] = {}  # run_id -> live agent process of this interface


def is_launched(manifest: dict) -> bool:
    return manifest.get("kind") == AGENT_KIND and bool((manifest.get("agent") or {}).get("launched"))


def runner_availability() -> dict[str, bool]:
    return {name: shutil.which(name) is not None for name in RUNNERS}


def model_label(runner: str, model: str | None, effort: str | None) -> str:
    label = f"{RUNNERS[runner]['label']} · {model or 'default model'}"
    return f"{label} · {effort} effort" if effort else label


def table_from_run(run_dir: Path) -> str:
    """The approved segments of a pipeline run as a toolkit segment table (TSV)."""
    path = run_dir / "final_rows.json"
    if not path.is_file():
        raise PipelineError("This report has no approved segments yet. Approve it before starting an agent run.")
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(TABLE_HEADER)
    for row in read_json(path)["rows"]:
        writer.writerow([row["segment"], row.get("event", ""), row["process"], row["generalized_category"],
                         row["interaction_type"], row["sediment_transport_phase"]])
    return buffer.getvalue()


def report_from_run(manifest: dict) -> Path:
    pdfs = [Path(item["path"]) for item in manifest.get("inputs", []) if str(item.get("path", "")).lower().endswith(".pdf")]
    if not pdfs:
        raise PipelineError("Agent runs read a PDF report; this report has no PDF input. Enter a PDF path instead.")
    if len(pdfs) > 1:
        raise PipelineError("This report has several PDF files; enter the one the agent should read.")
    return pdfs[0]


def create_agent_run(root: Path, *, report: Path, table_text: str, event: str = "", runner: str,
                     model: str | None = None, effort: str | None = None, full_access: bool = False,
                     dem: str | None = None, dem_info: str = "", catchment: str | None = None, catchment_info: str = "",
                     source_run_id: str | None = None, run_id: str | None = None) -> Path:
    """Create the run folder, segment table and prompt. Nothing runs until start_agent_run."""
    from segment_mapping.cli import build_prompt

    from .localization_compare import signature
    from .provided_localization import prepare_table

    if runner not in RUNNERS:
        raise PipelineError(f"Choose Claude Code or Codex (got {runner!r}).")
    if shutil.which(runner) is None:
        raise PipelineError(f"{RUNNERS[runner]['label']} (`{runner}`) is not installed or not on PATH for this interface.")
    model, effort = (model or "").strip() or None, (effort or "").strip().lower() or None
    if effort and effort not in RUNNERS[runner]["efforts"]:
        raise PipelineError(f"{RUNNERS[runner]['label']} effort must be one of {', '.join(RUNNERS[runner]['efforts'])}.")
    report = Path(report).resolve()
    if not report.is_file() or report.suffix.lower() != ".pdf":
        raise PipelineError(f"Report: {report} is not a PDF file on this computer.")
    for path, label in ((dem, "DEM"), (catchment, "Catchment")):
        if path and not Path(path).is_file():
            raise PipelineError(f"{label}: {path} was not found.")
    table_text, segments, event = prepare_table(table_text, event)
    run_id = run_id or new_run_id(f"{event or report.stem} {runner}")
    run_dir = root / run_id
    try:
        (run_dir / "inputs").mkdir(parents=True)
        table = run_dir / "inputs" / "segments.tsv"
        table.write_text(table_text, encoding="utf-8")
        args = argparse.Namespace(report=str(report), segments=str(table), run=str(run_dir / "localization"),
                                  dem=dem or None, dem_info=dem_info or None, catchment=catchment or None,
                                  catchment_info=catchment_info or None)
        try:
            prompt = build_prompt(args)
        except SystemExit as exc:  # the CLI helper reports bad inputs this way
            raise PipelineError(str(exc)) from exc
        (run_dir / "inputs" / "prompt.txt").write_text(prompt, encoding="utf-8")
        manifest = dict(
            run_id=run_id, kind=AGENT_KIND, event=event or report.stem, status="queued", created_at=utc_now(),
            activity="Waiting to start", log=[],
            inputs=[dict(filename=report.name, path=str(report), source_type="pdf")],
            agent=dict(launched=True, runner=runner, model=model_label(runner, model, effort), model_name=model,
                       effort=effort, full_access=bool(full_access), source_run_id=source_run_id,
                       signature=signature(segments), segments=len(segments),
                       dem=dem or None, catchment=catchment or None),
        )
        save_manifest(run_dir, manifest)
    except Exception:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return run_dir


def _update(run_dir: Path, **changes: Any) -> dict:
    with run_lock(run_dir):
        manifest = read_json(run_dir / "manifest.json")
        manifest.update(changes)
        save_manifest(run_dir, manifest)
        return manifest


def _log(run_dir: Path, message: str) -> None:
    with run_lock(run_dir):
        manifest = read_json(run_dir / "manifest.json")
        manifest["activity"] = message
        manifest["log"] = (manifest.get("log", []) + [dict(time=utc_now(), message=message)])[-200:]
        save_manifest(run_dir, manifest)


def _label_toolkit_run(run_dir: Path, agent: dict) -> None:
    """Best effort: name the agent in the toolkit run.json, so Compare and exported bundles show it."""
    from .localization_compare import signature

    config_path = run_dir / "localization" / "run.json"
    if not config_path.is_file():
        return
    config = read_json(config_path)
    config.update(model=agent.get("model"), agent_runner=agent.get("runner"))
    write_json(config_path, config)
    agent["signature"] = signature(config.get("segments") or []) or agent.get("signature")


def _finish(run_dir: Path, summary: dict | None, error: str | None) -> None:
    """Record the terminal status. Runs under the run lock, so a Stop request cannot interleave."""
    outputs = run_dir / "localization" / "outputs"
    with run_lock(run_dir):
        _PROCESSES.pop(run_dir.name, None)
        manifest = read_json(run_dir / "manifest.json")
        agent = manifest.get("agent") or {}
        try:
            _label_toolkit_run(run_dir, agent)
        except Exception as exc:  # noqa: BLE001 - an unreadable run.json must not keep the run "running"
            note = f"run.json could not be labelled: {type(exc).__name__}: {exc}"
            manifest.setdefault("warnings", []).append(note)
        delivered = (outputs / "segments_map.html").is_file() and (outputs / "segments.geojson").is_file()
        code = (summary or {}).get("exit_code")
        if code == 0 and delivered and not error:
            status, activity = "completed", "The agent delivered the map"
        elif manifest.get("status") == "stopping":
            status, activity = "stopped", "Stopped from the interface"
        elif error:
            status, activity = "failed", error
        elif code == 0:
            status, activity = "failed", "The agent finished without delivering a map (see the transcript)"
        else:
            status, activity = "failed", f"The agent stopped with exit code {code}"
        manifest.update(status=status, activity=activity, finished_at=utc_now(), agent=agent, exit_code=code,
                        error=activity if status == "failed" else None)
        save_manifest(run_dir, manifest)


def start_agent_run(run_dir: Path, log_hook: Callable[[str], None] | None = None) -> threading.Thread:
    """Run the agent in a background thread; extraction and localization keep their own worker."""
    from segment_mapping.agent_launch import launch

    manifest = read_json(run_dir / "manifest.json")
    agent = manifest["agent"]
    args = argparse.Namespace(runner=agent["runner"], run=str(run_dir / "localization"),
                              report=manifest["inputs"][0]["path"], model=agent.get("model_name"),
                              effort=agent.get("effort"), full_access=agent.get("full_access", False), dry_run=False)
    prompt = (run_dir / "inputs" / "prompt.txt").read_text(encoding="utf-8")

    def log(message: str) -> None:
        _log(run_dir, message)
        if log_hook:
            log_hook(message)

    def started(process) -> None:
        with run_lock(run_dir):
            _PROCESSES[run_dir.name] = process
            _update(run_dir, pid=process.pid)

    def work() -> None:
        summary, error = None, None
        try:
            _update(run_dir, status="running", activity="Starting the agent", started_at=utc_now(), worker=worker_marker())
            summary = launch(args, prompt, log=log, on_start=started)
        except SystemExit as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - every failure must be visible on the run page
            error = f"{type(exc).__name__}: {exc}"
        try:
            _finish(run_dir, summary, error)
        except Exception as exc:  # noqa: BLE001 - last resort: never leave the run looking busy
            try:
                _update(run_dir, status="failed", activity=f"Could not record the result: {exc}", finished_at=utc_now())
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=work, name=f"agent-{run_dir.name}", daemon=True)
    thread.start()
    return thread


def stop_agent_run(run_dir: Path) -> None:
    from segment_mapping.agent_launch import stop_process

    with run_lock(run_dir):
        manifest = read_json(run_dir / "manifest.json")
        if manifest.get("status") not in BUSY:
            raise PipelineError("This agent run is not running.")
        process = _PROCESSES.get(run_dir.name)
        if process is None:
            raise PipelineError("This agent run was started by another interface process; stop it there.")
        _update(run_dir, status="stopping", activity="Stopping the agent")
    # Outside the lock: the worker needs it to record "stopped" once the process exits.
    stop_process(process)


def _stop_all() -> None:
    """On orderly interface shutdown, stop running agents instead of leaving them unsupervised."""
    from segment_mapping.agent_launch import stop_process

    for process in list(_PROCESSES.values()):
        try:
            stop_process(process)
        except Exception:  # noqa: BLE001
            pass


atexit.register(_stop_all)


def log_files(run_dir: Path) -> dict[str, Path]:
    folder = run_dir / "localization" / "agent"
    return {name: folder / name for name in LOG_FILES if (folder / name).is_file()}
