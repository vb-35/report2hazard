from __future__ import annotations

from typing import Any

from .segment_agent import EVENT_STAGE_RULES
from ..config import TAXONOMY_DECISION_RULES, PipelineConfig
from ..core import normalize_text
from ..language import language_prompt_context
from ..llm import ChatClient
from ..payloads import segment_payload, source_payload
from ..schemas import review_response_schema, validate_review_payload


CHECK_NAMES = (
    "causal_chain_coherent",
    "evidence_verified",
    "segments_complete",
    "segments_unique",
    "categories_valid",
)


def _issue(stage: str, segment_ids: list[int], code: str, message: str, action: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "segment_ids": segment_ids,
        "code": code,
        "message": message,
        "suggested_action": action,
    }


def deterministic_candidate_evaluation(
    candidate: dict[str, Any], source: dict[str, Any], config: PipelineConfig
) -> tuple[dict[str, bool], list[dict[str, Any]]]:
    """Return deterministic whole-candidate checks and machine-readable issues."""
    checks = {name: True for name in CHECK_NAMES}
    issues: list[dict[str, Any]] = []
    rows = candidate.get("rows")
    if not isinstance(rows, list) or not rows:
        checks.update({name: False for name in CHECK_NAMES})
        return checks, [_issue("segmentation", [], "empty_candidate", "Candidate has no segments.", "Regenerate segmentation.")]

    ids = [row.get("segment") for row in rows]
    integer_ids = [value for value in ids if isinstance(value, int)]
    if (
        len(integer_ids) != len(ids)
        or any(value < 1 for value in integer_ids)
        or len(integer_ids) != len(set(integer_ids))
    ):
        checks["segments_unique"] = False
        checks["segments_complete"] = False
        issues.append(_issue("segmentation", integer_ids, "duplicate_or_invalid_segment_id", "Segment IDs are invalid or duplicated.", "Regenerate a uniquely numbered segment chain."))

    orders = [row.get("causal_order") for row in rows]
    if orders != list(range(1, len(rows) + 1)):
        checks["causal_chain_coherent"] = False
        checks["segments_complete"] = False
        issues.append(_issue("segmentation", integer_ids, "invalid_causal_order", "Causal order must be contiguous and list-ordered.", "Reorder the causal chain without changing stable segment IDs."))

    order_by_id = {
        row["segment"]: row.get("causal_order")
        for row in rows
        if isinstance(row.get("segment"), int) and isinstance(row.get("causal_order"), int)
    }
    for row in rows:
        segment_id = row.get("segment")
        predecessors = row.get("predecessor_segment_ids")
        if not isinstance(predecessors, list) or any(
            not isinstance(value, int)
            or value not in order_by_id
            or order_by_id[value] >= row.get("causal_order", 0)
            for value in (predecessors or [])
        ):
            checks["causal_chain_coherent"] = False
            issues.append(_issue("segmentation", [segment_id] if isinstance(segment_id, int) else [], "invalid_predecessor", "A predecessor is missing, invalid, or not earlier.", "Repair predecessor references and causal order."))

    chunks = {normalize_text(chunk.get("chunk_id", "")): normalize_text(chunk.get("text", "")) for chunk in source.get("chunks", [])}
    for row in rows:
        segment_id = row.get("segment")
        citations = row.get("evidence")
        if not isinstance(citations, list) or not citations:
            checks["evidence_verified"] = False
            issues.append(_issue("segmentation", [segment_id] if isinstance(segment_id, int) else [], "missing_evidence", "Segment has no evidence.", "Regenerate the segment with source evidence."))
            continue
        for citation in citations:
            chunk_id = normalize_text(citation.get("chunk_id", "")) if isinstance(citation, dict) else ""
            quote = normalize_text(citation.get("quote", "")) if isinstance(citation, dict) else ""
            if chunk_id not in chunks:
                checks["evidence_verified"] = False
                issues.append(_issue("segmentation", [segment_id] if isinstance(segment_id, int) else [], "unknown_evidence_chunk", f"Evidence cites unknown chunk {chunk_id!r}.", "Replace it with a real source chunk citation."))
            elif not quote or quote.casefold() not in chunks[chunk_id].casefold():
                checks["evidence_verified"] = False
                issues.append(_issue("segmentation", [segment_id] if isinstance(segment_id, int) else [], "evidence_quote_not_found", f"Evidence quote is absent from chunk {chunk_id!r}.", "Use an exact or whitespace-normalized source quote."))

    controlled = {
        "generalized_category": config.generalized_categories,
        "interaction_type": config.interaction_types,
        "sediment_transport_phase": config.sediment_transport_phases,
    }
    for row in rows:
        invalid = [field for field, values in controlled.items() if row.get(field) not in values]
        if invalid:
            checks["categories_valid"] = False
            segment_id = row.get("segment")
            issues.append(_issue("categorization", [segment_id] if isinstance(segment_id, int) else [], "invalid_controlled_label", f"Invalid controlled field(s): {', '.join(invalid)}.", "Reclassify the affected segment using the controlled taxonomy."))
    return checks, issues


def validate_candidate_report(candidate: dict[str, Any], source: dict[str, Any], config: PipelineConfig) -> None:
    """Raise when deterministic candidate facts fail; safe for human-edit validation."""
    checks, issues = deterministic_candidate_evaluation(candidate, source, config)
    if issues:
        detail = "; ".join(f"{item['code']}: {item['message']}" for item in issues)
        raise ValueError(detail)
    if not all(checks.values()):
        raise ValueError("candidate failed deterministic validation")


def review_agent(client: ChatClient, candidate: dict[str, Any], source: dict[str, Any], config: PipelineConfig) -> dict[str, Any]:
    deterministic_checks, deterministic_issues = deterministic_candidate_evaluation(candidate, source, config)
    if deterministic_issues:
        return {
            "doc_id": candidate.get("doc_id", source.get("doc_id")),
            "status": "revision_required",
            "summary": "Deterministic candidate validation failed before semantic evaluation.",
            "issues": deterministic_issues,
            "checks": deterministic_checks,
            "evaluation_source": "deterministic",
        }

    system_prompt = """
Evaluate the complete candidate report as an ordered set of causal steps, allowing independent branches and roots. Inspect the full report and supplied source chunks,
not isolated rows. Return JSON only with status pass or revision_required, summary, issues, and all five checks.
The checks object must contain exactly these Boolean keys on every response:
{"causal_chain_coherent":true,"evidence_verified":true,"segments_complete":true,
"segments_unique":true,"categories_valid":true}. Set relevant values false when revision is required; never omit a key.
Every issue MUST contain all five fields, including segment_ids as an array of integer segment numbers:
{"stage":"categorization","segment_ids":[1],"code":"wrong_category",
"message":"Segment 1: generalized_category=Negative Impact on permanent or temporary infrastructure; c1 'Blocks partially destroyed the nets'; T2 requires evidenced decreased sediment passage, which this quote does not establish.",
"suggested_action":"Reassess connectivity direction under T2; use a replacement only if supported."}
For a whole-report issue with no affected segment, use "segment_ids":[], never omit it or use null.
The stage value must be exactly one of "translation", "segmentation", or "categorization".

Assess semantic facts that deterministic code cannot decide reliably:
- Where translation_applied is true, does translated_text faithfully preserve its original text, including terminology, names, quantities, negation,
  uncertainty, and causality? Use issue stage translation and fail evidence_verified for translation defects.
- Where translation_applied is false, translated_text intentionally retains the original language. Minority-language
  passthrough under skip_translation is not a translation defect; never request translation merely for it.
  Missing or incorrect interpretation of these passages in the English event/process fields is a segmentation issue.
  Legacy artifacts without flags retain the contract that translated_text is an English translation.
- When no separate translated_text is supplied, interpret text directly in its original language, including minority-language passages; identical translations are omitted.
- Chunk IDs and document IDs are request-local. Respect document boundaries, filenames, and locations when interpreting the chain and citations.
- Are every segment's event and process written in English?
- Is each predecessor link supported by source evidence of a direct causal relationship? Chronological order,
  adjacent descriptions, and shared locations alone do not prove causation. Independent branches and empty predecessors are valid.
- Are important major stages of the main event missing? Completeness does not require every observation or background fact.
- Would an expert merge rows because they describe the same major stage? Flag the affected IDs with a targeted merge instruction
  and fail segments_unique. Repeated measurements, locations, damaged objects, summaries, and captions do not justify separate rows.
- Are historical events, unrelated background, or emergency/evacuation accounts included without a relevant physical process?
  Flag these as segmentation scope issues. Do not restore intentionally excluded context merely for completeness.
- Do not fail a report solely because its segment count falls outside the suggested range.
- Is every claim supported by its evidence in meaning (citation IDs and quote occurrence are already code-checked)?
- Is every segment categorized exactly once, and do taxonomy precedence/disambiguation rules fit its role in sediment connectivity?
- Is the complete report internally consistent?

Issue stage must be translation, segmentation, or categorization. Use stable snake_case issue codes, affected segment IDs (or [] for a
whole-report omission), a concise message, and an actionable correction instruction. Segmentation issues include missing,
duplicate, fragmented, unsupported, or misordered causal steps. Categorization issues apply only when the segment chain is
sound but a controlled label is semantically wrong. Pass only with no issues and every check true. Do not act as a human reviewer.

Independently determine which labels the source supports using the shared rules below before comparing the candidate;
the classifier's rationale is a claim to verify, not evidence or authority. For every categorization disagreement, the
message must identify the segment, field/current label, source chunk ID and short quote, and rule ID with an explanation
of what the current label violates. If the problem is absence of evidence for a direction, say what the source establishes
and what is missing. Suggest a replacement only when supported; otherwise request evidence-based reassessment using T1/T2.
Do not flag a supported uncertainty label merely for being nonspecific. Keep summary/messages concise; omit deliberation.
""".strip() + "\n\n" + EVENT_STAGE_RULES + "\n\n" + TAXONOMY_DECISION_RULES
    projected, references = source_payload(source.get("chunks", []), language_prompt_context(source))

    def validate(payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("status") == "revision_required" and isinstance(payload.get("checks"), dict) and isinstance(payload.get("issues"), list):
            for issue in payload["issues"]:
                stage = issue.get("stage") if isinstance(issue, dict) else None
                check = {"translation": "evidence_verified", "segmentation": "causal_chain_coherent", "categorization": "categories_valid"}.get(stage)
                if check in payload["checks"]:
                    payload["checks"][check] = False
        validate_review_payload(payload)
        real_ids = {row["segment"] for row in candidate["rows"]}
        for issue in payload["issues"]:
            unknown = set(issue["segment_ids"]) - real_ids
            if unknown:
                raise ValueError(f"evaluation cites unknown segment IDs: {sorted(unknown)}")

    payload = client.complete_json(
        system_prompt=system_prompt,
        user_payload={
            "controlled_labels": config.controlled_labels(),
            "candidate_report": {"rows": segment_payload(
                candidate["rows"], review=True,
                references={permanent: local for local, permanent in references.items()},
            )},
            "documents": projected["documents"],
            "source_chunks": projected["chunks"],
        },
        validate=validate,
        response_schema=review_response_schema(config),
    )
    return {
        "doc_id": candidate.get("doc_id", source.get("doc_id")),
        "status": payload["status"],
        "summary": normalize_text(payload["summary"]),
        "issues": payload["issues"],
        "checks": payload["checks"],
        "evaluation_source": "llm",
    }
