from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from ..config import TAXONOMY_DECISION_RULES, PipelineConfig
from ..core import normalize_text
from ..errors import PipelineError
from ..llm import ChatClient
from ..payloads import correction_payload, segment_payload, source_payload
from ..schemas import (
    IMMUTABLE_SEGMENT_FIELDS,
    canonicalize_row_labels,
    classification_response_schema,
    validate_classification_payload,
)


def _classification_prompt(config: PipelineConfig) -> str:
    labels = config.controlled_labels()
    return f"""
Categorize supplied segments without rewriting them. Return JSON only as {{"rows":[...]}}. For ordinary calls return
one row per supplied segment; correction calls follow requested_segment_ids and the correction rules below. For each result,
return only its segment ID, generalized_category, interaction_type, sediment_transport_phase, and a short
classification_rationale list. Python retains the original causal order, event, process, and evidence unchanged.

Exact controlled values:
- generalized_category: {labels['generalized_category']}
- interaction_type: {labels['interaction_type']}
- sediment_transport_phase: {labels['sediment_transport_phase']}

{TAXONOMY_DECISION_RULES}

Give a short evidence-based classification_rationale, identifying the relevant rule and supporting quote or mechanism.
Treat review_issues and correction_instruction as claims to reassess against the supplied evidence and these rules,
not authoritative replacement labels. Accept a proposed replacement only if supported; otherwise retain or select the supported label,
including an uncertainty label where appropriate, and briefly explain why. Match quoted evidence rather than chunk IDs
from another request. Neither a reviewer proposal nor a structure's protective purpose establishes connectivity direction.
Never paraphrase or otherwise change a source segment.
For structured review_issues, use previous_answer and the entire supplied chain to reassess consequences.
Return rows for all requested_segment_ids and any OTHER affected segments whose labels or rationale need correction.
Omit unchanged, unrequested classifications; Python retains them. Correction evidence IDs are permanent source IDs.
""".strip()


def validate_classification_against_segments(
    payload: Any,
    segments: dict[str, Any],
    config: PipelineConfig,
    expected_segment_ids: Iterable[int] | None = None,
) -> None:
    """Validate exact coverage, controlled labels, and immutable segment data."""
    validate_classification_payload(payload, config)
    source_by_id = {item["segment"]: item for item in segments.get("segments", [])}
    expected = set(source_by_id) if expected_segment_ids is None else set(expected_segment_ids)
    returned = [row.get("segment") for row in payload["rows"]]
    if any(not isinstance(value, int) for value in returned):
        raise ValueError("classification segment IDs must be integers")
    if len(returned) != len(set(returned)):
        raise ValueError("classification contains a segment more than once")
    if set(returned) != expected:
        missing = sorted(expected - set(returned))
        unknown = sorted(set(returned) - expected)
        raise ValueError(f"classification coverage mismatch; missing={missing}, unknown={unknown}")
    for row in payload["rows"]:
        source = source_by_id.get(row["segment"])
        if source is None:
            raise ValueError(f"classification returned unknown segment {row['segment']}")
        for field in IMMUTABLE_SEGMENT_FIELDS:
            if field in row and row[field] != source.get(field):
                raise ValueError(f"classification rewrote segment {row['segment']} field {field}")


def _normalize_classified_row(row: dict[str, Any], source: dict[str, Any], config: PipelineConfig) -> dict[str, Any]:
    canonicalize_row_labels(row, config)
    result = {field: deepcopy(source[field]) for field in IMMUTABLE_SEGMENT_FIELDS}
    result.update(
        {
            "generalized_category": row["generalized_category"],
            "interaction_type": row["interaction_type"],
            "sediment_transport_phase": row["sediment_transport_phase"],
            "classification_rationale": [normalize_text(item) for item in row["classification_rationale"]],
        }
    )
    return result


def classification_agent(
    client: ChatClient,
    segments: dict[str, Any],
    config: PipelineConfig,
    segment_ids: Iterable[int] | None = None,
    existing: dict[str, Any] | list[dict[str, Any]] | None = None,
    correction_instruction: str | None = None,
    *,
    review_issues: list[dict[str, Any]] | None = None,
    source: dict[str, Any] | None = None,
    previous_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_by_id = {item["segment"]: item for item in segments.get("segments", [])}
    if len(source_by_id) != len(segments.get("segments", [])) or not source_by_id:
        raise PipelineError("classification requires a non-empty segment chain with unique IDs")
    requested = list(source_by_id) if segment_ids is None else list(segment_ids)
    if len(requested) != len(set(requested)):
        raise PipelineError("affected classification segment IDs must be unique")
    unknown = set(requested) - set(source_by_id)
    if unknown:
        raise PipelineError(f"cannot classify unknown segments: {sorted(unknown)}")
    existing_rows = existing.get("rows", []) if isinstance(existing, dict) else (existing or [])
    if set(requested) != set(source_by_id) and not existing_rows:
        raise PipelineError("targeted categorization requires existing classifications for unaffected segments")

    requested_set = set(requested)
    requested_segments = [item for item in segments["segments"] if item["segment"] in requested_set]

    def validate(payload: Any) -> None:
        returned = {row.get("segment") for row in payload.get("rows", [])} if isinstance(payload, dict) else set()
        if review_issues and not requested_set <= returned:
            raise ValueError(f"classification omitted requested segments: {sorted(requested_set - returned)}")
        validate_classification_against_segments(payload, segments, config, returned if review_issues else requested_set)

    correction = correction_payload(previous_source or source or {"chunks": []}, {"rows": existing_rows}, review_issues) if review_issues else {}
    references = {citation["chunk_id"]: citation["chunk_id"] for item in segments["segments"] for citation in item["evidence"]}

    payload = client.complete_json(
        system_prompt=_classification_prompt(config),
        user_payload={
            "segments": segment_payload(segments["segments"] if review_issues else requested_segments,
                                        references=references if review_issues else None),
            **correction,
            **({"requested_segment_ids": requested,
                "source": source_payload(source["chunks"], permanent_ids=True)[0] if source else None} if review_issues else {}),
            "correction_instruction": normalize_text(correction_instruction or "") or None,
        },
        validate=validate,
        response_schema=classification_response_schema(config),
    )
    replacements = {
        row["segment"]: _normalize_classified_row(row, source_by_id[row["segment"]], config)
        for row in payload["rows"]
    }

    if existing_rows:
        unaffected = set(source_by_id) - set(replacements)
        existing_payload = {"rows": [deepcopy(row) for row in existing_rows if row.get("segment") in unaffected]}
        validate_classification_against_segments(existing_payload, segments, config, unaffected)
        replacements.update(
            {
                row["segment"]: _normalize_classified_row(row, source_by_id[row["segment"]], config)
                for row in existing_payload["rows"]
            }
        )
    if set(replacements) != set(source_by_id):
        raise PipelineError("classification did not produce one result for every segment")
    rows = [replacements[item["segment"]] for item in segments["segments"]]
    return {"doc_id": segments["doc_id"], "status": "pass", "rows": rows}
