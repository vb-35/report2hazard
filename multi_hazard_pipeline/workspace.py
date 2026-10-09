"""Read-only projections of atomic pipeline artifacts for the review workspace."""
from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any

from .core import normalize_text, read_json
from .pipeline import WORKSPACE_ARTIFACTS, artifact_token


def quote_match(text: str, quote: str) -> dict[str, Any]:
    # Keep offsets into the displayed text while applying the pipeline's whitespace/case rules.
    normalized, offsets = [], []
    for index, char in enumerate(text):
        if char.isspace() or char == "\x00":
            if normalized and normalized[-1] != " ":
                normalized.append(" ")
                offsets.append(index)
        else:
            for folded in char.casefold():
                normalized.append(folded)
                offsets.append(index)
    haystack = "".join(normalized)
    needle = normalize_text(quote).casefold()
    matches = []
    start = 0
    while needle:
        found = haystack.find(needle, start)
        if found < 0:
            break
        matches.append([offsets[found], offsets[found + len(needle) - 1] + 1])
        start = found + 1
    state = "exact" if len(matches) == 1 else "repeated" if matches else "unmatched"
    return {"state": state, "ranges": matches if state == "exact" else []}


def documents(manifest: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    result = []
    for index, item in enumerate(manifest.get("inputs", [])):
        path = Path(item.get("path", ""))
        available = path.is_file() and path.suffix.lower() in {".pdf", ".docx", ".txt"}
        result.append({
            "id": str(index), "filename": item.get("filename", path.name),
            "source_type": item.get("source_type", path.suffix.lower()[1:]),
            "available": available, "url": f"/runs/{run_id}/source/{index}" if available else None,
        })
    return result


STAGE_ORDER = ("extraction", "translation", "segmentation", "categorization", "candidate_report", "self_evaluation")
# Attempt deadlines, socket/connect timeouts and gateway timeouts all surface only in the error text.
TIMEOUT_ERROR = re.compile(r"time limit|time budget|timed out|time-out|timeout|WinError 10060", re.IGNORECASE)


def _attempt(record: dict[str, Any]) -> dict[str, Any]:
    error = record.get("error")
    return {
        "task": record.get("task"), "attempt": record.get("attempt", 1), "outcome": record.get("outcome"),
        "elapsed_seconds": record.get("elapsed_seconds") or 0,
        "timed_out": bool(error and TIMEOUT_ERROR.search(error)),
        "error": error[:300] if error else None,
        "retry_delay_seconds": record.get("retry_delay_seconds"),
    }


def _counts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "requests": sum(item["attempt"] == 1 for item in attempts),
        "attempts": len(attempts),
        "retries": sum(item["attempt"] > 1 for item in attempts),
        "timeouts": sum(item["timed_out"] for item in attempts),
        "failed_attempts": sum(item["outcome"] != "pass" for item in attempts),
        "retry_wait_seconds": round(sum(item["retry_delay_seconds"] or 0 for item in attempts), 3),
    }


def timing_statistics(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Group timings.jsonl into stage executions with their model-call attempts."""
    records = []
    path = run_dir / "timings.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue  # A concurrent append can leave a partial final line.
    executions: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    started: dict[str, Any] | None = None

    def close(stage: str, outcome: str, elapsed: float | None, record: dict[str, Any]) -> None:
        executions.append({
            "stage": stage, "outcome": outcome, "elapsed_seconds": elapsed,
            "correction_round": record.get("correction_round"), "started_at": record.get("started_at"),
            "attempt_log": list(attempts), **_counts(attempts),
        })
        attempts.clear()

    for record in records:
        if record.get("type") == "llm_call":
            attempts.append(_attempt(record))
        elif record.get("type") == "stage_start":
            if attempts:  # Calls made outside any timed stage by older pipeline versions.
                close("other", "completed", None, {})
            started = record
        elif record.get("type") == "stage":
            # Older logs have no start records; calls before a stage end belong to that stage.
            close(record.get("stage"), record.get("outcome"), record.get("elapsed_seconds"),
                  {**record, "started_at": started.get("timestamp") if started else None})
            started = None
    if started or attempts:
        running = manifest.get("status") == "running"
        close(started.get("stage") if started else "other", "running" if running and started else "interrupted",
              None, {**(started or {}), "started_at": started.get("timestamp") if started else None})
    stages = []
    for name in (*STAGE_ORDER, "other"):
        runs = [item for item in executions if item["stage"] == name]
        if not runs:
            continue
        stage_attempts = [attempt for item in runs for attempt in item["attempt_log"]]
        active = next((item for item in runs if item["outcome"] == "running"), None)
        stages.append({
            "stage": name, "executions": len(runs),
            "elapsed_seconds": round(sum(item["elapsed_seconds"] or 0 for item in runs), 3),
            "running_since": active["started_at"] if active else None,
            **_counts(stage_attempts),
        })
    all_attempts = [attempt for item in executions for attempt in item["attempt_log"]]
    return {
        "stages": stages, "executions": executions,
        "totals": {"elapsed_seconds": round(sum(item["elapsed_seconds"] for item in stages), 3),
                   "running_since": next((item["running_since"] for item in stages if item["running_since"]), None),
                   **_counts(all_attempts)},
    }


def workspace_data(run_dir: Path, since: dict[str, str] | None = None) -> dict[str, Any]:
    since = since or {}
    # Workers use atomic file replacement. Retry if a generation changes during projection;
    # never take the worker lock, which is held during long model requests.
    for _ in range(3):
        manifest_token = artifact_token(run_dir / "manifest.json")
        manifest = read_json(run_dir / "manifest.json")
        tokens = {name: artifact_token(run_dir / name) for name in (*WORKSPACE_ARTIFACTS.values(), "human_review.json")}
        retained = manifest.get("retained_artifacts", {})
        valid = {name: bool(token and token != retained.get(name)) for name, token in tokens.items()}
        # Legacy in-flight runs have no retained identities. Wait for stage completion
        # instead of guessing whether their downstream files belong to the new revision.
        stages = list(WORKSPACE_ARTIFACTS)
        active = manifest.get("current_stage")
        if "retained_artifacts" not in manifest and manifest.get("status") == "running" and active in stages:
            for stage in stages[stages.index(active):]:
                valid[WORKSPACE_ARTIFACTS[stage]] = False
        chosen = next((name for name in ("candidate_report.json", "classified.json", "segments.json") if valid[name]), None)
        previous = chosen is None
        if previous:
            chosen = next((name for name in ("candidate_report.json", "classified.json", "segments.json") if tokens[name]), None)
        versions = {
            "reader": json.dumps([tokens["source.json"], valid["source.json"], tokens["translated.json"], valid["translated.json"], documents(manifest, manifest["run_id"])]),
            "results": json.dumps([chosen, tokens.get(chosen), previous, tokens["source.json"], tokens["self_evaluation.json"], valid["self_evaluation.json"], tokens["human_review.json"]]),
            "statistics": json.dumps([artifact_token(run_dir / "timings.jsonl"), manifest.get("status")]),
        }
        versions = {key: sha256(value.encode()).hexdigest() for key, value in versions.items()}
        payload: dict[str, Any] = {"versions": versions, "manifest": {
            key: manifest.get(key) for key in ("run_id", "status", "current_stage", "stages", "correction_rounds", "max_correction_rounds", "warnings", "errors", "child_runs")
        }}
        source = None
        if since.get("reader") != versions["reader"] or since.get("results") != versions["results"]:
            source = read_json(run_dir / "source.json") if tokens["source.json"] else {}
        if since.get("reader") != versions["reader"]:
            document_list = documents(manifest, manifest["run_id"])
            for chunk in (source or {}).get("chunks", []):
                filename = chunk.get("filename", chunk.get("file"))
                if filename and not any(doc["filename"] == filename for doc in document_list):
                    document_list.append({"id": f"saved-{len(document_list)}", "filename": filename, "source_type": chunk.get("source_type"), "available": False, "url": None})
            payload["reader"] = {
                "source": source, "translated": read_json(run_dir / "translated.json") if tokens["translated.json"] else {},
                "source_previous": bool(tokens["source.json"] and not valid["source.json"]),
                "translation_previous": bool(tokens["translated.json"] and not valid["translated.json"]),
                "documents": document_list,
            }
        if since.get("statistics") != versions["statistics"]:
            payload["statistics"] = timing_statistics(run_dir, manifest)
        if since.get("results") != versions["results"]:
            result = read_json(run_dir / chosen) if chosen else {}
            rows = result.get("rows", result.get("segments", []))
            chunks = {chunk["chunk_id"]: chunk for chunk in (source or {}).get("chunks", [])}
            for row in rows:
                for evidence in row.get("evidence", []):
                    chunk = chunks.get(evidence.get("chunk_id"), {})
                    evidence["provenance"] = {key: value for key, value in chunk.items() if key != "text"} | evidence.get("provenance", {})
                    evidence["highlight"] = quote_match(chunk.get("text", ""), evidence.get("quote", "")) if chunk else {"state": "missing", "ranges": []}
            evaluation = read_json(run_dir / "self_evaluation.json") if valid["self_evaluation.json"] and not previous and chosen == "candidate_report.json" else None
            if evaluation and evaluation.get("candidate_revision", result.get("candidate_revision")) != result.get("candidate_revision"):
                evaluation = None
            payload["results"] = {
                "rows": sorted(rows, key=lambda row: row.get("causal_order", row.get("segment", 0))),
                "previous": previous, "revision": result.get("candidate_revision"),
                "kind": chosen, "evaluation": evaluation,
                "human": read_json(run_dir / "human_review.json") if tokens["human_review.json"] else None,
            }
        if manifest_token == artifact_token(run_dir / "manifest.json") and all(token == artifact_token(run_dir / name) for name, token in tokens.items()):
            return payload
    raise RuntimeError("Artifacts changed during reading; retry the workspace update")
