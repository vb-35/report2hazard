from __future__ import annotations

import sys
import re
import traceback
import unicodedata
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from .agents import (
    classification_agent,
    discover_inputs,
    review_agent,
    segment_agent,
    source_agent,
    stabilize_segment_ids,
    translation_agent,
)
from .config import DEFAULT_CONFIG, PipelineConfig
from .core import normalize_text, read_json, resolve_path, write_json
from .errors import PipelineError
from .language import language_settings, language_summary, recorded_language_config
from .llm import ChatClient, append_timing
from .schemas import IMMUTABLE_SEGMENT_FIELDS, validate_segment_chain


REPORT_GROUPING_RULE = (
    "All files explicitly selected for one run must be companion parts or supplements of one report; "
    "unrelated reports must be submitted as separate runs."
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_run_id(report_name: str = "report", *, created_at: datetime | None = None) -> str:
    title = unicodedata.normalize("NFKD", report_name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:80].rstrip("-") or "report"
    timestamp = (created_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d_%H-%M-%S-%fZ")
    return f"{slug}__{timestamp}"


def artifact_paths(artifact_dir: Path) -> dict[str, str | None]:
    names = {
        "source": "source.json",
        "translated": "translated.json",
        "segments": "segments.json",
        "classified": "classified.json",
        "candidate_report": "candidate_report.json",
        "self_evaluation": "self_evaluation.json",
        "human_review": "human_review.json",
        "final_rows_json": "final_rows.json",
        "final_rows_csv": "final_rows.csv",
        "manifest": "manifest.json",
        "timings": "timings.jsonl",
    }
    return {
        key: str(artifact_dir / name)
        if (artifact_dir / name).exists() or not name.startswith("final_rows")
        else None
        for key, name in names.items()
    }


def build_manifest(
    *,
    run_id: str,
    doc_id: str,
    input_paths: list[Path],
    artifact_dir: Path,
    config: PipelineConfig,
) -> dict[str, Any]:
    stages = {
        name: "pending"
        for name in (
            "extraction",
            "translation",
            "segmentation",
            "categorization",
            "candidate_report",
            "self_evaluation",
            "human_review",
            "final_export",
        )
    }
    return {
        "run_id": run_id,
        "doc_id": doc_id,
        "status": "running",
        "current_stage": "queued",
        "progress": 0,
        "input_dir": str(input_paths[0].parent),
        "artifact_dir": str(artifact_dir),
        "inputs": [
            {"filename": path.name, "path": str(path), "source_type": path.suffix.lower().lstrip(".")}
            for path in input_paths
        ],
        "report_grouping_rule": REPORT_GROUPING_RULE,
        "stages": stages,
        "correction_rounds": 0,
        "max_correction_rounds": config.max_correction_rounds,
        "warnings": [],
        "errors": [],
        "model": config.llm.model,
        "configuration": {
            **language_settings(config),
            "batch_max_chars": config.batch_max_chars,
            "max_correction_rounds": config.max_correction_rounds,
            "temperature": config.llm.temperature,
            "timeout_seconds": config.llm.timeout_seconds,
            "max_request_chars": config.llm.max_request_chars,
        },
        "artifacts": artifact_paths(artifact_dir),
        "human_decision": None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def save_manifest(artifact_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    manifest["artifacts"] = artifact_paths(artifact_dir)
    write_json(artifact_dir / "manifest.json", {key: value for key, value in manifest.items() if not key.startswith("_")})


WORKSPACE_ARTIFACTS = {
    "extraction": "source.json", "translation": "translated.json",
    "segmentation": "segments.json", "categorization": "classified.json",
    "candidate_report": "candidate_report.json", "self_evaluation": "self_evaluation.json",
}


def artifact_token(path: Path) -> str | None:
    try:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except FileNotFoundError:
        return None


def invalidate_results(artifact_dir: Path, manifest: dict[str, Any], stage: str) -> None:
    """Record retained artifact identities before replacing a pipeline suffix."""
    stages = list(WORKSPACE_ARTIFACTS)
    if stage not in stages:
        return
    invalid = manifest.setdefault("retained_artifacts", {})
    for name in stages[stages.index(stage):]:
        filename = WORKSPACE_ARTIFACTS[name]
        token = artifact_token(artifact_dir / filename)
        if token:
            invalid[filename] = token
        manifest["stages"][name] = "pending"
    manifest["stages"]["human_review"] = "pending"
    manifest["stages"]["final_export"] = "pending"


def start_stage_timing(artifact_dir: Path, manifest: dict[str, Any], stage: str) -> None:
    # The start record lets the interface time a running stage and attribute its model calls.
    manifest["_stage_timer"] = (stage, perf_counter())
    append_timing(artifact_dir / "timings.jsonl", {
        "type": "stage_start", "stage": stage, "correction_round": manifest["correction_rounds"],
    })


def finish_stage_timing(artifact_dir: Path, manifest: dict[str, Any], outcome: str) -> None:
    timer = manifest.pop("_stage_timer", None)
    if timer:
        append_timing(artifact_dir / "timings.jsonl", {
            "type": "stage", "stage": timer[0], "outcome": outcome,
            "elapsed_seconds": round(perf_counter() - timer[1], 3),
            "correction_round": manifest["correction_rounds"],
        })


def set_stage(
    artifact_dir: Path,
    manifest: dict[str, Any],
    stage: str,
    state: str = "running",
    progress: int | None = None,
) -> None:
    previous_stage = manifest.get("current_stage")
    # A replaced artifact marks the previous running stage as complete.
    if previous_stage in WORKSPACE_ARTIFACTS and manifest["stages"].get(previous_stage) == "running":
        filename = WORKSPACE_ARTIFACTS[previous_stage]
        token = artifact_token(artifact_dir / filename)
        if token and token != manifest.get("retained_artifacts", {}).get(filename):
            manifest["stages"][previous_stage] = "completed"
    invalidate_results(artifact_dir, manifest, stage)
    finish_stage_timing(artifact_dir, manifest,
                        "skipped" if manifest["stages"].get(previous_stage) == "skipped" else "completed")
    start_stage_timing(artifact_dir, manifest, stage)
    manifest["current_stage"] = stage
    if stage in manifest["stages"]:
        manifest["stages"][stage] = state
    if progress is not None:
        manifest["progress"] = progress
    save_manifest(artifact_dir, manifest)


def create_run(
    input_dir: str | Path,
    output_dir: str | Path,
    config: PipelineConfig = DEFAULT_CONFIG,
    *,
    run_id: str | None = None,
    input_paths: list[Path] | None = None,
    doc_id: str | None = None,
) -> dict[str, Any]:
    input_path = resolve_path(input_dir)
    selected = [resolve_path(path) for path in input_paths] if input_paths is not None else discover_inputs(input_path)
    if not selected:
        raise PipelineError(f"no supported report files selected in {input_path}")
    output_path = resolve_path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    identifier = run_id or new_run_id(selected[0].stem)
    artifact_dir = output_path / identifier
    artifact_dir.mkdir(parents=False, exist_ok=False)
    report_id = doc_id or normalize_text(input_path.name).lower().replace(" ", "_") or "report"
    manifest = build_manifest(
        run_id=identifier,
        doc_id=report_id,
        input_paths=selected,
        artifact_dir=artifact_dir,
        config=config,
    )
    save_manifest(artifact_dir, manifest)
    return manifest


def build_candidate_report(
    run_id: str,
    classified: dict[str, Any],
    source: dict[str, Any],
    revision: int,
) -> dict[str, Any]:
    provenance_by_chunk = {
        chunk["chunk_id"]: {
            key: chunk[key]
            for key in ("document_id", "doc_id", "filename", "source_type", "page", "paragraph",
                        "table", "table_path", "row", "cell", "parent_chunk_id", "char_start", "char_end")
            if key in chunk
        }
        for chunk in source["chunks"]
    }
    rows: list[dict[str, Any]] = []
    for classified_row in classified["rows"]:
        row = dict(classified_row)
        row["evidence"] = [
            {
                "chunk_id": item["chunk_id"],
                "quote": item["quote"],
                "provenance": provenance_by_chunk[item["chunk_id"]],
            }
            for item in classified_row["evidence"]
        ]
        rows.append(row)
    return {
        "run_id": run_id,
        "doc_id": classified["doc_id"],
        "status": "candidate",
        "candidate_revision": revision,
        "rows": sorted(rows, key=lambda item: item["causal_order"]),
    }


def initial_evaluation_history(evaluation: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": evaluation["status"],
        "latest_evaluation": evaluation,
        "evaluations": [{"round": 0, "evaluation": evaluation, "timestamp": utc_now()}],
        "correction_rounds": [],
    }


def classification_correction_ids(
    previous: dict[str, Any], revised: dict[str, Any], issues: list[dict[str, Any]],
    previous_source: dict[str, Any], source: dict[str, Any],
) -> list[int]:
    """Invalidate changed steps and their event/causal neighbors, retaining independent work."""
    old = {row["segment"]: row for row in previous["rows"]}
    new = {row["segment"]: row for row in revised["segments"]}
    if any(not issue["segment_ids"] or not set(issue["segment_ids"]) <= set(old) for issue in issues):
        return sorted(new)
    prior_chunks = {chunk["chunk_id"]: chunk for chunk in previous_source["chunks"]}
    changed_chunks = {chunk["chunk_id"] for chunk in source["chunks"]
                      if any(chunk.get(field) != prior_chunks.get(chunk["chunk_id"], {}).get(field)
                             for field in ("text", "translated_text"))}
    affected = set(old) - set(new)
    affected.update(value for issue in issues if issue["stage"] != "categorization" for value in issue["segment_ids"])
    categorization_ids = {value for issue in issues if issue["stage"] == "categorization" for value in issue["segment_ids"]}
    affected.update(segment_id for segment_id, row in new.items()
                    if segment_id not in old or any(row[field] != old[segment_id].get(field) for field in IMMUTABLE_SEGMENT_FIELDS)
                    or any(item["chunk_id"] in changed_chunks for item in row["evidence"]))
    # ponytail: conservatively reassess an entire event; narrow scope only if redundant classification calls matter.
    rows = list(old.values()) + list(new.values())
    while True:
        events = {row["event"] for row in rows if row["segment"] in affected}
        expanded = affected | {row["segment"] for row in rows
                               if row["event"] in events or affected.intersection(row["predecessor_segment_ids"])}
        expanded.update(value for row in rows if row["segment"] in expanded for value in row["predecessor_segment_ids"])
        if expanded == affected:
            return sorted(set(new) & (affected | categorization_ids))
        affected = expanded


def record_translation_outcome(manifest: dict[str, Any], translated: dict[str, Any]) -> None:
    analysis = translated.get("language_analysis")
    if analysis:
        manifest["language_summary"] = language_summary(analysis)
    manifest["stages"]["translation"] = (
        "skipped" if analysis and not any(item["translation_applied"] for item in analysis["chunks"].values()) else "completed"
    )


def correct_until_terminal(
    *,
    client: ChatClient,
    source: dict[str, Any],
    segments: dict[str, Any],
    classified: dict[str, Any],
    candidate: dict[str, Any],
    history: dict[str, Any],
    config: PipelineConfig,
    artifact_dir: Path,
    manifest: dict[str, Any],
    translated: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    config = recorded_language_config(config, manifest)
    bilingual_source = translated or source
    evaluation = history["latest_evaluation"]
    correction_limit = int(manifest.get("max_correction_rounds", config.max_correction_rounds))
    while (
        evaluation["status"] == "revision_required"
        and manifest["correction_rounds"] < correction_limit
    ):
        issues = evaluation["issues"]
        translation_issues = [item for item in issues if item["stage"] == "translation"]
        segmentation_issues = [item for item in issues if item["stage"] == "segmentation"]
        before_ids = [item["segment"] for item in segments["segments"]]
        previous_source = bilingual_source
        previous_answer = classified
        if translation_issues:
            stages_rerun = ["translation", "segmentation", "categorization"]
            set_stage(artifact_dir, manifest, "translation", progress=35)
            bilingual_source = translation_agent(
                client,
                source,
                config=config,
                previous_analysis=bilingual_source.get("language_analysis"),
                previous=bilingual_source,
                previous_answer=previous_answer,
                review_issues=issues,
            )
            record_translation_outcome(manifest, bilingual_source)
            write_json(artifact_dir / "translated.json", bilingual_source)
        elif segmentation_issues:
            stages_rerun = ["segmentation", "categorization"]
        else:
            stages_rerun = ["categorization"]
        if translation_issues or segmentation_issues:
            set_stage(artifact_dir, manifest, "segmentation", progress=55)
            revised_segments = segment_agent(
                client, bilingual_source, config,
                previous_answer=previous_answer, review_issues=issues, previous_source=previous_source,
            )
            segments = stabilize_segment_ids(revised_segments, segments)
            validate_segment_chain(segments, source)
            write_json(artifact_dir / "segments.json", segments)
        affected = classification_correction_ids(previous_answer, segments, issues, previous_source, bilingual_source)
        set_stage(artifact_dir, manifest, "categorization", progress=65)
        classified = classification_agent(
            client, segments, config, segment_ids=affected, existing=classified,
            review_issues=issues, source=bilingual_source, previous_source=previous_source,
        )
        write_json(artifact_dir / "classified.json", classified)
        candidate = build_candidate_report(
            manifest["run_id"], classified, source, candidate["candidate_revision"] + 1
        )
        write_json(artifact_dir / "candidate_report.json", candidate)
        manifest["stages"]["candidate_report"] = "completed"
        set_stage(artifact_dir, manifest, "self_evaluation", progress=75)
        evaluation = review_agent(client, candidate, bilingual_source, config)
        manifest["correction_rounds"] += 1
        round_record = {
            "round": manifest["correction_rounds"],
            "identified_issues": issues,
            "stages_rerun": stages_rerun,
            "translation_outcome": manifest["stages"]["translation"],
            "before_segment_ids": before_ids,
            "after_segment_ids": [item["segment"] for item in segments["segments"]],
            "resulting_evaluation": evaluation,
            "timestamp": utc_now(),
        }
        history["correction_rounds"].append(round_record)
        history["evaluations"].append(
            {"round": manifest["correction_rounds"], "evaluation": evaluation, "timestamp": utc_now()}
        )
        history["latest_evaluation"] = evaluation
        history["candidate_revision"] = candidate["candidate_revision"]
        history["status"] = evaluation["status"]
        for rerun_stage in stages_rerun:
            if rerun_stage != "translation":
                manifest["stages"][rerun_stage] = "completed"
        manifest["stages"]["candidate_report"] = "completed"
        write_json(artifact_dir / "self_evaluation.json", history)
        save_manifest(artifact_dir, manifest)
    manifest["stages"]["self_evaluation"] = "completed"
    return segments, classified, candidate, history


def create_human_review(manifest: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    awaiting = manifest.get("status") == "awaiting_human_review"
    return {
        "run_id": manifest["run_id"],
        "doc_id": manifest["doc_id"],
        "status": "awaiting_human_review" if awaiting else manifest.get("status", "revision_required"),
        "candidate_revision": candidate["candidate_revision"],
        "self_evaluation_status": "pass" if awaiting else "revision_required",
        "global_comment": "",
        "segment_comments": [],
        "edits": [],
        "decisions": [],
        "latest_decision": None,
        "updated_at": utc_now(),
    }


def execute_run(
    artifact_dir: str | Path,
    config: PipelineConfig = DEFAULT_CONFIG,
    *,
    client: ChatClient | None = None,
) -> dict[str, Any]:
    run_dir = resolve_path(artifact_dir)
    manifest = read_json(run_dir / "manifest.json")
    config = recorded_language_config(config, manifest)
    manifest["status"] = "running"
    input_paths = [Path(item["path"]) for item in manifest["inputs"]]
    active_stage = "extraction"
    try:
        set_stage(run_dir, manifest, active_stage, progress=10)
        source = source_agent(
            input_paths, manifest["doc_id"], max_chunk_chars=min(2000, max(1, config.batch_max_chars // 4))
        )
        source["report_grouping_rule"] = REPORT_GROUPING_RULE
        write_json(run_dir / "source.json", source)
        manifest["stages"][active_stage] = "completed"

        llm_client = client or ChatClient.from_config(config.llm)
        if isinstance(llm_client, ChatClient):
            llm_client = replace(llm_client, timing_path=run_dir / "timings.jsonl")
        active_stage = "translation"
        set_stage(run_dir, manifest, active_stage, progress=20)
        translated = translation_agent(llm_client, source, config=config)
        write_json(run_dir / "translated.json", translated)
        record_translation_outcome(manifest, translated)

        active_stage = "segmentation"
        set_stage(run_dir, manifest, active_stage, progress=35)
        segments = segment_agent(llm_client, translated, config)
        write_json(run_dir / "segments.json", segments)
        manifest["stages"][active_stage] = "completed"

        active_stage = "categorization"
        set_stage(run_dir, manifest, active_stage, progress=50)
        classified = classification_agent(llm_client, segments, config)
        write_json(run_dir / "classified.json", classified)
        manifest["stages"][active_stage] = "completed"

        active_stage = "candidate_report"
        set_stage(run_dir, manifest, active_stage, progress=60)
        candidate = build_candidate_report(manifest["run_id"], classified, source, 1)
        write_json(run_dir / "candidate_report.json", candidate)
        manifest["stages"][active_stage] = "completed"

        active_stage = "self_evaluation"
        set_stage(run_dir, manifest, active_stage, progress=75)
        evaluation = review_agent(llm_client, candidate, translated, config)
        history = initial_evaluation_history(evaluation)
        history["candidate_revision"] = candidate["candidate_revision"]
        write_json(run_dir / "self_evaluation.json", history)
        segments, classified, candidate, history = correct_until_terminal(
            client=llm_client,
            source=source,
            segments=segments,
            classified=classified,
            candidate=candidate,
            history=history,
            config=config,
            artifact_dir=run_dir,
            manifest=manifest,
            translated=translated,
        )
        manifest["stages"][active_stage] = "completed"
        if history["status"] == "pass":
            active_stage = "human_review"
            manifest["status"] = "awaiting_human_review"
            human_review = create_human_review(manifest, candidate)
            write_json(run_dir / "human_review.json", human_review)
            manifest["current_stage"] = "human_review"
            manifest["progress"] = 90
            manifest["stages"]["human_review"] = "awaiting"
        else:
            manifest["status"] = "revision_required"
            manifest["current_stage"] = "self_evaluation"
            manifest["progress"] = 80
            write_json(run_dir / "human_review.json", create_human_review(manifest, candidate))
        save_manifest(run_dir, manifest)
    except Exception as exc:
        if getattr(exc, "language_analysis", None):
            manifest["language_summary"] = language_summary(exc.language_analysis)
        failure_stage = manifest.get("current_stage", active_stage)
        if failure_stage not in manifest["stages"]:
            failure_stage = active_stage
        manifest["status"] = "failed"
        manifest["current_stage"] = failure_stage
        manifest["stages"][failure_stage] = "failed"
        manifest["errors"].append(
            {
                "stage": failure_stage,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
                "timestamp": utc_now(),
            }
        )
        save_manifest(run_dir, manifest)
    finally:
        finish_stage_timing(run_dir, manifest, "failed" if manifest["status"] == "failed" else "completed")
    return read_json(run_dir / "manifest.json")


def run_pipeline(
    input_dir: str | Path,
    output_dir: str | Path,
    config: PipelineConfig = DEFAULT_CONFIG,
    *,
    client: ChatClient | None = None,
    run_id: str | None = None,
    input_paths: list[Path] | None = None,
) -> dict[str, Any]:
    try:
        manifest = create_run(
            input_dir,
            output_dir,
            config,
            run_id=run_id,
            input_paths=input_paths,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "current_stage": "input_validation",
            "errors": [{"type": type(exc).__name__, "message": str(exc)}],
        }
    return execute_run(manifest["artifact_dir"], config, client=client)


def run_pdf_collection(
    input_pdf: str | Path,
    output_dir: str | Path,
    split_output_dir: str | Path,
    config: PipelineConfig = DEFAULT_CONFIG,
    *,
    client: ChatClient | None = None,
) -> list[dict[str, Any]]:
    """Split a collection and run every event PDF as an independent ordinary run."""
    from .splitter import split_event_reports

    llm_client = client or ChatClient.from_config(config.llm)
    event_pdfs = split_event_reports(Path(input_pdf), Path(split_output_dir), llm_client, config)
    return [
        run_pipeline(
            event_pdf.parent,
            output_dir,
            config,
            client=llm_client,
            input_paths=[event_pdf],
        )
        for event_pdf in event_pdfs
    ]


def main(argv: list[str] | None = None) -> int:
    from .cli import main as cli_main

    return cli_main(argv if argv is not None else sys.argv[1:])
