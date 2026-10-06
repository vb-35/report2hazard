"""Read-only projections of atomic pipeline artifacts for the review workspace."""
from __future__ import annotations

import json
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
