from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import traceback
from typing import Any
import uuid

from .agents import review_agent
from .config import DEFAULT_CONFIG, PipelineConfig
from .core import normalize_text, read_json, run_lock, write_csv, write_json
from .errors import PipelineError
from .language import language_summary, recorded_language_config
from .llm import ChatClient
from .pipeline import (
    correct_until_terminal,
    create_human_review,
    save_manifest,
    invalidate_results,
    utc_now,
)


ISSUE_TYPES = {"missing", "duplicate", "unsupported", "misordered", "miscategorized"}
EDITABLE_FIELDS = {
    "event",
    "process",
    "causal_order",
    "predecessor_segment_ids",
    "generalized_category",
    "interaction_type",
    "sediment_transport_phase",
}


def load_run(artifact_dir: str | Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    run_dir = Path(artifact_dir).resolve()
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise PipelineError(f"run manifest not found: {manifest_path}")
    manifest = read_json(manifest_path)
    human_path = run_dir / "human_review.json"
    human = read_json(human_path) if human_path.is_file() else create_human_review(
        manifest, read_json(run_dir / "candidate_report.json")
    )
    return run_dir, manifest, human


def normalize_segment_comments(comments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized = []
    for item in comments or []:
        issue_type = normalize_text(str(item.get("issue_type", ""))).lower()
        if issue_type not in ISSUE_TYPES:
            raise PipelineError(f"invalid human issue type: {issue_type!r}")
        normalized.append(
            {
                "segment": int(item["segment"]) if item.get("segment") not in (None, "") else None,
                "issue_type": issue_type,
                "comment": normalize_text(str(item.get("comment", ""))),
            }
        )
    return normalized


def record_decision(
    human: dict[str, Any],
    decision: str,
    global_comment: str,
    segment_comments: list[dict[str, Any]],
    requested_stage: str | None = None,
) -> dict[str, Any]:
    entry = {
        "decision": decision,
        "global_comment": normalize_text(global_comment),
        "segment_comments": segment_comments,
        "requested_stage": requested_stage,
        "timestamp": utc_now(),
    }
    human["global_comment"] = entry["global_comment"]
    human["segment_comments"] = segment_comments
    human["decisions"].append(entry)
    human["latest_decision"] = entry
    human["updated_at"] = entry["timestamp"]
    return entry


def _approve_run_unlocked(
    artifact_dir: str | Path,
    *,
    global_comment: str = "",
    segment_comments: list[dict[str, Any]] | None = None,
    config: PipelineConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    run_dir, manifest, human = load_run(artifact_dir)
    history = read_json(run_dir / "self_evaluation.json")
    evaluation = history["latest_evaluation"]
    if manifest["status"] != "awaiting_human_review" or evaluation["status"] != "pass":
        raise PipelineError("only a self-evaluation-passed candidate awaiting human review may be approved")
    candidate = read_json(run_dir / "candidate_report.json")
    if history.get("candidate_revision", candidate["candidate_revision"]) != candidate["candidate_revision"]:
        raise PipelineError("approval requires evaluation of the current candidate revision")
    comments = normalize_segment_comments(segment_comments)
    validate_comment_segments(comments, candidate)
    decision = record_decision(human, "approve", global_comment, comments)
    final_payload = {
        "run_id": manifest["run_id"],
        "doc_id": manifest["doc_id"],
        "status": "approved",
        "approved_at": decision["timestamp"],
        "candidate_revision": candidate["candidate_revision"],
        "rows": candidate["rows"],
    }
    token = uuid.uuid4().hex
    pending_json = run_dir / f".final_rows.{token}.json"
    pending_csv = run_dir / f".final_rows.{token}.csv"
    original_human = read_json(run_dir / "human_review.json")
    original_manifest = read_json(run_dir / "manifest.json")
    human["status"] = "approved"
    manifest["status"] = "approved"
    manifest["current_stage"] = "final_export"
    manifest["progress"] = 100
    manifest["human_decision"] = decision
    manifest["stages"]["human_review"] = "completed"
    manifest["stages"]["final_export"] = "completed"
    try:
        write_json(pending_json, final_payload)
        write_csv(pending_csv, candidate["rows"], config.export_columns)
        write_json(run_dir / "human_review.json", human)
        save_manifest(run_dir, manifest)
        os.replace(pending_json, run_dir / "final_rows.json")
        os.replace(pending_csv, run_dir / "final_rows.csv")
        save_manifest(run_dir, manifest)
    except Exception as exc:
        for path in (pending_json, pending_csv, run_dir / "final_rows.json", run_dir / "final_rows.csv"):
            path.unlink(missing_ok=True)
        write_json(run_dir / "human_review.json", original_human)
        record_operation_failure(run_dir, original_manifest, "final_export", exc)
        raise
    return manifest


def approve_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
    run_dir = Path(args[0] if args else kwargs["artifact_dir"]).resolve()
    with run_lock(run_dir):
        return _approve_run_unlocked(*args, **kwargs)


def _reject_run_unlocked(
    artifact_dir: str | Path,
    *,
    global_comment: str = "",
    segment_comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    run_dir, manifest, human = load_run(artifact_dir)
    if manifest["status"] != "awaiting_human_review":
        raise PipelineError("only a candidate awaiting human review may be rejected")
    candidate = read_json(run_dir / "candidate_report.json")
    comments = normalize_segment_comments(segment_comments)
    validate_comment_segments(comments, candidate)
    decision = record_decision(human, "reject", global_comment, comments)
    human["status"] = "rejected"
    manifest["status"] = "rejected"
    manifest["current_stage"] = "human_review"
    manifest["human_decision"] = decision
    manifest["stages"]["human_review"] = "completed"
    write_json(run_dir / "human_review.json", human)
    save_manifest(run_dir, manifest)
    return manifest


def reject_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
    run_dir = Path(args[0] if args else kwargs["artifact_dir"]).resolve()
    with run_lock(run_dir):
        return _reject_run_unlocked(*args, **kwargs)


def validate_candidate(candidate: dict[str, Any], source: dict[str, Any], config: PipelineConfig) -> None:
    rows = candidate.get("rows")
    if not isinstance(rows, list) or not rows:
        raise PipelineError("candidate must contain rows")
    ids = [int(row["segment"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise PipelineError("candidate segment IDs must be unique")
    ordered = sorted(rows, key=lambda row: int(row["causal_order"]))
    orders = [int(row["causal_order"]) for row in ordered]
    if orders != list(range(1, len(rows) + 1)):
        raise PipelineError("candidate causal_order must be contiguous from 1")
    earlier: set[int] = set()
    chunks = {item["chunk_id"]: normalize_text(item["text"]).casefold() for item in source["chunks"]}
    for row in ordered:
        segment = int(row["segment"])
        if not normalize_text(str(row.get("event", ""))):
            raise PipelineError(f"segment {segment} has no event")
        if not normalize_text(str(row.get("process", ""))):
            raise PipelineError(f"segment {segment} has no process")
        if any(int(item) not in earlier for item in row.get("predecessor_segment_ids", [])):
            raise PipelineError(f"segment {segment} predecessor IDs must refer to earlier segments")
        earlier.add(segment)
        if row["generalized_category"] not in config.generalized_categories:
            raise PipelineError(f"segment {segment} has invalid generalized_category")
        if row["interaction_type"] not in config.interaction_types:
            raise PipelineError(f"segment {segment} has invalid interaction_type")
        if row["sediment_transport_phase"] not in config.sediment_transport_phases:
            raise PipelineError(f"segment {segment} has invalid sediment_transport_phase")
        if not row.get("evidence"):
            raise PipelineError(f"segment {segment} has no evidence")
        for evidence in row["evidence"]:
            chunk_id = evidence["chunk_id"]
            quote = normalize_text(evidence["quote"]).casefold()
            if chunk_id not in chunks:
                raise PipelineError(f"segment {segment} cites unknown chunk {chunk_id}")
            if not quote or quote not in chunks[chunk_id]:
                raise PipelineError(f"segment {segment} quote is absent from chunk {chunk_id}")


def validate_comment_segments(comments: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    valid = {int(row["segment"]) for row in candidate["rows"]}
    unknown = {item["segment"] for item in comments if item["segment"] is not None} - valid
    if unknown:
        raise PipelineError(f"human comments reference unknown segment IDs: {sorted(unknown)}")


def record_operation_failure(
    run_dir: Path, manifest: dict[str, Any], stage: str, exc: Exception
) -> None:
    if getattr(exc, "language_analysis", None):
        manifest["language_summary"] = language_summary(exc.language_analysis)
    manifest["status"] = "failed"
    manifest["current_stage"] = stage
    if stage in manifest["stages"]:
        manifest["stages"][stage] = "failed"
    manifest["errors"].append(
        {
            "stage": stage,
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
            "timestamp": utc_now(),
        }
    )
    save_manifest(run_dir, manifest)


def _apply_candidate_edits_unlocked(
    artifact_dir: str | Path,
    edits: list[dict[str, Any]],
    *,
    config: PipelineConfig = DEFAULT_CONFIG,
    client: ChatClient | None = None,
    validate_only: bool = False,
) -> dict[str, Any]:
    run_dir, manifest, human = load_run(artifact_dir)
    eligible = manifest["status"] in {"awaiting_human_review", "revision_required"} or (
        manifest["status"] == "running" and manifest["current_stage"] == "self_evaluation"
    )
    if not eligible:
        raise PipelineError("candidate edits require a reviewable candidate")
    candidate = read_json(run_dir / "candidate_report.json")
    source = read_json(run_dir / "source.json")
    translated_path = run_dir / "translated.json"
    bilingual_source = read_json(translated_path) if translated_path.is_file() else source
    segments = read_json(run_dir / "segments.json")
    classified = read_json(run_dir / "classified.json")
    candidate_map = {int(row["segment"]): row for row in candidate["rows"]}
    segment_map = {int(row["segment"]): row for row in segments["segments"]}
    classified_map = {int(row["segment"]): row for row in classified["rows"]}
    audit_entries = []
    for edit in edits:
        segment = int(edit["segment"])
        field = edit["field"]
        if field not in EDITABLE_FIELDS:
            raise PipelineError(f"candidate field is not editable: {field}")
        if segment not in candidate_map:
            raise PipelineError(f"unknown segment {segment}")
        value = edit.get("new_value")
        if field == "causal_order":
            value = int(value)
            if value < 1 or value > len(candidate_map):
                raise PipelineError(f"causal_order must be between 1 and {len(candidate_map)}")
            reordered = sorted(candidate_map.values(), key=lambda item: item["causal_order"])
            moved = candidate_map[segment]
            reordered.remove(moved)
            reordered.insert(value - 1, moved)
            timestamp = utc_now()
            for order, reordered_row in enumerate(reordered, start=1):
                reordered_segment = int(reordered_row["segment"])
                old_order = reordered_row["causal_order"]
                if old_order == order:
                    continue
                reordered_row["causal_order"] = order
                segment_map[reordered_segment]["causal_order"] = order
                classified_map[reordered_segment]["causal_order"] = order
                audit_entries.append(
                    {
                        "timestamp": timestamp,
                        "segment": reordered_segment,
                        "field": field,
                        "old_value": old_order,
                        "new_value": order,
                    }
                )
            continue
        elif field == "predecessor_segment_ids":
            value = [int(item) for item in value]
        else:
            value = normalize_text(str(value))
        old_value = candidate_map[segment][field]
        if old_value == value:
            continue
        candidate_map[segment][field] = value
        if field in segment_map[segment]:
            segment_map[segment][field] = value
        if field in classified_map[segment]:
            classified_map[segment][field] = value
        audit_entries.append(
            {
                "timestamp": utc_now(),
                "segment": segment,
                "field": field,
                "old_value": old_value,
                "new_value": value,
            }
        )
    if not audit_entries:
        raise PipelineError("no candidate values changed")
    candidate["rows"] = sorted(candidate_map.values(), key=lambda row: row["causal_order"])
    candidate["candidate_revision"] += 1
    validate_candidate(candidate, source, config)
    if validate_only:
        return manifest
    invalidate_results(run_dir, manifest, "segmentation")
    manifest["current_stage"] = "self_evaluation"
    manifest["status"] = "running"
    manifest["stages"]["self_evaluation"] = "running"
    save_manifest(run_dir, manifest)
    segments["segments"] = sorted(segment_map.values(), key=lambda row: row["causal_order"])
    classified["rows"] = sorted(classified_map.values(), key=lambda row: row["causal_order"])
    human["edits"].extend(audit_entries)
    human["candidate_revision"] = candidate["candidate_revision"]
    human["updated_at"] = utc_now()
    write_json(run_dir / "human_review.json", human)
    write_json(run_dir / "segments.json", segments)
    write_json(run_dir / "classified.json", classified)
    write_json(run_dir / "candidate_report.json", candidate)
    for name in ("segmentation", "categorization", "candidate_report"):
        manifest["stages"][name] = "completed"
    save_manifest(run_dir, manifest)
    try:
        llm_client = client or ChatClient.from_config(config.llm)
        if isinstance(llm_client, ChatClient):
            llm_client = replace(llm_client, timing_path=run_dir / "timings.jsonl")
        evaluation = review_agent(llm_client, candidate, bilingual_source, config)
    except Exception as exc:
        record_operation_failure(run_dir, manifest, "self_evaluation", exc)
        raise
    history = read_json(run_dir / "self_evaluation.json")
    history["latest_evaluation"] = evaluation
    history["candidate_revision"] = candidate["candidate_revision"]
    history["status"] = evaluation["status"]
    history["evaluations"].append(
        {"trigger": "human_edit", "evaluation": evaluation, "timestamp": utc_now()}
    )
    write_json(run_dir / "self_evaluation.json", history)
    human["self_evaluation_status"] = evaluation["status"]
    human["status"] = "awaiting_human_review" if evaluation["status"] == "pass" else "revision_required"
    manifest["status"] = human["status"]
    manifest["current_stage"] = "human_review" if evaluation["status"] == "pass" else "self_evaluation"
    manifest["stages"]["self_evaluation"] = "completed"
    manifest["stages"]["human_review"] = "awaiting" if evaluation["status"] == "pass" else "pending"
    manifest["progress"] = 90 if evaluation["status"] == "pass" else 80
    write_json(run_dir / "human_review.json", human)
    save_manifest(run_dir, manifest)
    return manifest


def apply_candidate_edits(*args: Any, **kwargs: Any) -> dict[str, Any]:
    run_dir = Path(args[0] if args else kwargs["artifact_dir"]).resolve()
    with run_lock(run_dir):
        return _apply_candidate_edits_unlocked(*args, **kwargs)


def _request_correction_unlocked(
    artifact_dir: str | Path,
    *,
    requested_stage: str,
    segment_ids: list[int] | None = None,
    global_comment: str = "",
    segment_comments: list[dict[str, Any]] | None = None,
    config: PipelineConfig = DEFAULT_CONFIG,
    client: ChatClient | None = None,
    validate_only: bool = False,
) -> dict[str, Any]:
    run_dir, manifest, human = load_run(artifact_dir)
    config = recorded_language_config(config, manifest)
    eligible = manifest["status"] in {"awaiting_human_review", "revision_required"} or (
        manifest["status"] == "running"
        and manifest["current_stage"] in {"translation", "segmentation", "categorization"}
    )
    if not eligible:
        raise PipelineError("this run is not eligible for correction")
    if requested_stage not in {"translation", "segmentation", "categorization"}:
        raise PipelineError("requested_stage must be translation, segmentation, or categorization")
    if manifest["status"] == "running" and manifest["current_stage"] != requested_stage:
        raise PipelineError("queued correction stage does not match the requested stage")
    ids = sorted({int(item) for item in segment_ids or []})
    if requested_stage == "categorization" and not ids:
        raise PipelineError("categorization correction requires affected segment IDs")
    if manifest["correction_rounds"] >= manifest.get(
        "max_correction_rounds", config.max_correction_rounds
    ):
        raise PipelineError("maximum correction rounds already reached")
    candidate = read_json(run_dir / "candidate_report.json")
    unknown = set(ids) - {int(row["segment"]) for row in candidate["rows"]}
    if unknown:
        raise PipelineError(f"correction references unknown segment IDs: {sorted(unknown)}")
    comments = normalize_segment_comments(segment_comments)
    validate_comment_segments(comments, candidate)
    if validate_only:
        return manifest
    decision = record_decision(human, "request_correction", global_comment, comments, requested_stage)
    source = read_json(run_dir / "source.json")
    translated_path = run_dir / "translated.json"
    translated = read_json(translated_path) if translated_path.is_file() else source
    segments = read_json(run_dir / "segments.json")
    classified = read_json(run_dir / "classified.json")
    history = read_json(run_dir / "self_evaluation.json")
    comment_text = " ".join(item["comment"] for item in comments if item["comment"])
    message = normalize_text(" ".join(filter(None, (global_comment, comment_text)))) or (
        f"Human requested {requested_stage} correction"
    )
    checks = dict(history["latest_evaluation"].get("checks", {}))
    checks[
        "categories_valid"
        if requested_stage == "categorization"
        else "evidence_verified"
        if requested_stage == "translation"
        else "causal_chain_coherent"
    ] = False
    synthetic = {
        "status": "revision_required",
        "summary": message,
        "issues": history["latest_evaluation"].get("issues", []) + [
            {
                "stage": requested_stage,
                "segment_ids": ids,
                "code": "human_requested_correction",
                "message": message,
                "suggested_action": message,
            }
        ],
        "checks": checks,
    }
    history["latest_evaluation"] = synthetic
    history["status"] = "revision_required"
    history["evaluations"].append(
        {"trigger": "human_request", "evaluation": synthetic, "timestamp": decision["timestamp"]}
    )
    manifest["status"] = "running"
    manifest["current_stage"] = requested_stage
    manifest["human_decision"] = decision
    write_json(run_dir / "human_review.json", human)
    write_json(run_dir / "self_evaluation.json", history)
    save_manifest(run_dir, manifest)
    try:
        llm_client = client or ChatClient.from_config(config.llm)
        if isinstance(llm_client, ChatClient):
            llm_client = replace(llm_client, timing_path=run_dir / "timings.jsonl")
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
    except Exception as exc:
        record_operation_failure(run_dir, manifest, manifest.get("current_stage", requested_stage), exc)
        raise
    human["candidate_revision"] = candidate["candidate_revision"]
    human["self_evaluation_status"] = history["status"]
    if history["status"] == "pass":
        human["status"] = "awaiting_human_review"
        manifest["status"] = "awaiting_human_review"
        manifest["current_stage"] = "human_review"
        manifest["stages"]["human_review"] = "awaiting"
        manifest["progress"] = 90
    else:
        human["status"] = "revision_required"
        manifest["status"] = "revision_required"
        manifest["current_stage"] = "self_evaluation"
        manifest["progress"] = 80
    manifest["stages"]["self_evaluation"] = "completed"
    write_json(run_dir / "human_review.json", human)
    save_manifest(run_dir, manifest)
    return manifest


def request_correction(*args: Any, **kwargs: Any) -> dict[str, Any]:
    run_dir = Path(args[0] if args else kwargs["artifact_dir"]).resolve()
    with run_lock(run_dir):
        return _request_correction_unlocked(*args, **kwargs)
