from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from .config import TAXONOMY, PipelineConfig
from .core import canonicalize_controlled_value, normalize_text


CONTROLLED_FIELDS = ("generalized_category", "interaction_type", "sediment_transport_phase")
SUPPORTED_SOURCE_LANGUAGES = ("English", "German", "French", "Italian")
SOURCE_LANGUAGE_LABELS = SUPPORTED_SOURCE_LANGUAGES + ("Mixed", "Unknown")
IMMUTABLE_SEGMENT_FIELDS = (
    "segment",
    "causal_order",
    "predecessor_segment_ids",
    "event",
    "process",
    "evidence",
)


class CitationQuoteMismatch(ValueError):
    def __init__(self, segment: int, chunk_id: str, quote: str):
        self.segment = segment
        self.chunk_id = chunk_id
        self.quote = quote
        super().__init__(
            f"segment {segment} quote not found in chunk {chunk_id}: {quote!r}. "
            "Copy a short literal substring from that chunk's text, including any broken characters; "
            "do not quote translated_text or repair spelling."
        )


def _aliases(field: str, allowed: tuple[str, ...]) -> dict[str, str]:
    return {value.casefold(): value for value in allowed} | TAXONOMY[field]["aliases"]


def canonicalize_row_labels(row: dict[str, Any], config: PipelineConfig) -> dict[str, Any]:
    for field, allowed in (
        ("generalized_category", config.generalized_categories),
        ("interaction_type", config.interaction_types),
        ("sediment_transport_phase", config.sediment_transport_phases),
    ):
        row[field] = canonicalize_controlled_value(row.get(field), allowed, _aliases(field, allowed))
    return row


def load_row_schema(config: PipelineConfig) -> dict[str, Any]:
    schema = json.loads(config.schema_path.read_text(encoding="utf-8"))
    for field, values in config.controlled_labels().items():
        schema["properties"][field]["enum"] = values
    return schema


def load_validator(config: PipelineConfig) -> Draft202012Validator:
    return Draft202012Validator(load_row_schema(config))


def json_array_schema(item_schema: dict[str, Any], *, min_items: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": item_schema}
    if min_items is not None:
        schema["minItems"] = min_items
    return schema


def evidence_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"chunk_id": {"type": "string"}, "quote": {"type": "string"}},
        "required": ["chunk_id", "quote"],
    }


def segment_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "segment": {"type": "integer", "minimum": 1},
            "causal_order": {"type": "integer", "minimum": 1},
            "predecessor_segment_ids": json_array_schema({"type": "integer", "minimum": 1}),
            "event": {"type": "string"},
            "process": {"type": "string"},
            "evidence": json_array_schema(evidence_schema(), min_items=1),
        },
        "required": list(IMMUTABLE_SEGMENT_FIELDS),
    }


def segment_response_schema() -> dict[str, Any]:
    return {
        "name": "causal_segments",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"segments": json_array_schema(segment_item_schema())},
            "required": ["segments"],
        },
    }


def consolidation_response_schema() -> dict[str, Any]:
    fields = {
        "segment": {"type": "integer", "minimum": 1},
        "causal_order": {"type": "integer", "minimum": 1},
        "predecessor_segment_ids": json_array_schema({"type": "integer", "minimum": 1}),
        "event": {"type": "string"},
        "process": {"type": "string"},
        "source_segment_ids": json_array_schema({"type": "integer", "minimum": 1}, min_items=1),
    }
    return {
        "name": "consolidated_causal_segments",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"segments": json_array_schema({
                "type": "object", "additionalProperties": False,
                "properties": fields, "required": list(fields),
            })},
            "required": ["segments"],
        },
    }


def citation_verification_response_schema() -> dict[str, Any]:
    return {
        "name": "citation_semantic_verification",
        "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "supported": {"type": "boolean"},
                "fragment_ids": json_array_schema({"type": "string"}),
                "reason": {"type": "string"},
            },
            "required": ["supported", "fragment_ids", "reason"],
        },
    }


def translation_response_schema() -> dict[str, Any]:
    return {
        "name": "translated_chunks",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "translations": json_array_schema(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "chunk_id": {"type": "string"},
                            "source_language": {
                                "type": "string",
                                "enum": list(SOURCE_LANGUAGE_LABELS),
                            },
                            "translated_text": {"type": "string"},
                        },
                        "required": ["chunk_id", "source_language", "translated_text"],
                    }
                )
            },
            "required": ["translations"],
        },
    }


def event_separation_response_schema() -> dict[str, Any]:
    event = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "start_page": {"type": "integer", "minimum": 1},
            "heading_quote": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["title", "start_page", "heading_quote", "confidence"],
    }
    boundary = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "page": {"type": "integer", "minimum": 1},
            "heading_quote": {"type": "string"},
        },
        "required": ["page", "heading_quote"],
    }
    return {
        "name": "event_report_separation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["pass", "review_required"]},
                "events": json_array_schema(event),
                "collection_end": {"anyOf": [boundary, {"type": "null"}]},
                "warnings": json_array_schema({"type": "string"}),
            },
            "required": ["status", "events", "collection_end", "warnings"],
        },
    }


def classification_response_schema(config: PipelineConfig) -> dict[str, Any]:
    properties = {
            "segment": {"type": "integer", "minimum": 1},
            "generalized_category": {"type": "string", "enum": list(config.generalized_categories)},
            "interaction_type": {"type": "string", "enum": list(config.interaction_types)},
            "sediment_transport_phase": {"type": "string", "enum": list(config.sediment_transport_phases)},
            "classification_rationale": json_array_schema({"type": "string"}, min_items=1),
    }
    return {
        "name": "classified_segments",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "rows": json_array_schema(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": properties,
                        "required": ["segment"] + list(CONTROLLED_FIELDS) + ["classification_rationale"],
                    }
                )
            },
            "required": ["rows"],
        },
    }


def review_response_schema(config: PipelineConfig | None = None) -> dict[str, Any]:
    check_names = (
        "causal_chain_coherent",
        "evidence_verified",
        "segments_complete",
        "segments_unique",
        "categories_valid",
    )
    issue = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "stage": {
                "type": "string",
                "enum": ["translation", "segmentation", "categorization"],
            },
            "segment_ids": json_array_schema({"type": "integer", "minimum": 1}),
            "code": {"type": "string"},
            "message": {"type": "string"},
            "suggested_action": {"type": "string"},
        },
        "required": ["stage", "segment_ids", "code", "message", "suggested_action"],
    }
    return {
        "name": "whole_report_evaluation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["pass", "revision_required"]},
                "summary": {"type": "string"},
                "issues": json_array_schema(issue),
                "checks": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {name: {"type": "boolean"} for name in check_names},
                    "required": list(check_names),
                },
            },
            "required": ["status", "summary", "issues", "checks"],
        },
    }


def validate_segment_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise ValueError("segments payload must be an object with a segments list")
    for item in payload["segments"]:
        if not isinstance(item, dict):
            raise ValueError("segment item must be an object")
        if not isinstance(item.get("segment"), int) or item["segment"] < 1:
            raise ValueError("segment must be a positive integer")
        if not isinstance(item.get("causal_order"), int) or item["causal_order"] < 1:
            raise ValueError("causal_order must be a positive integer")
        predecessors = item.get("predecessor_segment_ids")
        if not isinstance(predecessors, list) or any(not isinstance(value, int) or value < 1 for value in predecessors):
            raise ValueError("predecessor_segment_ids must be a list of positive integers")
        if not normalize_text(item.get("event", "")):
            raise ValueError("segment event is required")
        if not normalize_text(item.get("process", "")):
            raise ValueError("segment process is required")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("segment evidence is required")
        for citation in evidence:
            if not isinstance(citation, dict) or not normalize_text(citation.get("chunk_id", "")):
                raise ValueError("evidence chunk_id is required")
            if not normalize_text(citation.get("quote", "")):
                raise ValueError("evidence quote is required")


def validate_segment_chain(payload: Any, source: dict[str, Any]) -> None:
    """Validate IDs, order, predecessors, evidence IDs, and normalized quotes."""
    validate_segment_payload(payload)
    segments = payload["segments"]
    ids = [item["segment"] for item in segments]
    orders = [item["causal_order"] for item in segments]
    if len(ids) != len(set(ids)):
        raise ValueError("segment IDs must be unique")
    if orders != list(range(1, len(segments) + 1)):
        raise ValueError("causal_order must be unique, contiguous, and list-ordered")
    chunks = {normalize_text(chunk["chunk_id"]): normalize_text(chunk["text"]) for chunk in source.get("chunks", [])}
    order_by_id = {item["segment"]: item["causal_order"] for item in segments}
    for item in segments:
        if len(item["predecessor_segment_ids"]) != len(set(item["predecessor_segment_ids"])):
            raise ValueError(f"segment {item['segment']} has duplicate predecessor IDs")
        for predecessor in item["predecessor_segment_ids"]:
            if predecessor not in order_by_id:
                raise ValueError(f"segment {item['segment']} references unknown predecessor {predecessor}")
            if order_by_id[predecessor] >= item["causal_order"]:
                raise ValueError(f"segment {item['segment']} predecessor {predecessor} is not earlier")
        for citation in item["evidence"]:
            chunk_id = normalize_text(citation["chunk_id"])
            quote = normalize_text(citation["quote"])
            if chunk_id not in chunks:
                raise ValueError(f"segment {item['segment']} cites unknown chunk {chunk_id}")
            if quote.casefold() not in chunks[chunk_id].casefold():
                raise CitationQuoteMismatch(item["segment"], chunk_id, quote)


def validate_classification_payload(payload: Any, config: PipelineConfig) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("classification payload must be an object with a rows list")
    for row in payload["rows"]:
        if not isinstance(row, dict):
            raise ValueError("classification row must be an object")
        canonicalize_row_labels(row, config)
        for field, allowed in (
            ("generalized_category", config.generalized_categories),
            ("interaction_type", config.interaction_types),
            ("sediment_transport_phase", config.sediment_transport_phases),
        ):
            if row.get(field) not in allowed:
                raise ValueError(f"invalid {field}: {row.get(field)!r}")
        rationale = row.get("classification_rationale")
        if not isinstance(rationale, list) or not rationale or any(not normalize_text(item) for item in rationale):
            raise ValueError("classification_rationale is required")


def validate_review_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("status") not in {"pass", "revision_required"}:
        raise ValueError("evaluation must include pass/revision_required status")
    if not normalize_text(payload.get("summary", "")):
        raise ValueError("evaluation summary is required")
    issues = payload.get("issues")
    checks = payload.get("checks")
    if not isinstance(issues, list) or not isinstance(checks, dict):
        raise ValueError("evaluation must include issues and checks")
    required_checks = {
        "causal_chain_coherent",
        "evidence_verified",
        "segments_complete",
        "segments_unique",
        "categories_valid",
    }
    if set(checks) != required_checks or any(not isinstance(value, bool) for value in checks.values()):
        raise ValueError("evaluation checks are incomplete")
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("stage") not in {
            "translation",
            "segmentation",
            "categorization",
        }:
            raise ValueError("evaluation issue stage is invalid")
        if not isinstance(issue.get("segment_ids"), list) or any(not isinstance(value, int) for value in issue["segment_ids"]):
            raise ValueError("evaluation issue segment_ids are invalid")
        if issue["stage"] == "categorization" and not issue["segment_ids"]:
            raise ValueError("categorization issues must identify affected segment IDs")
        for field in ("code", "message", "suggested_action"):
            if not normalize_text(issue.get(field, "")):
                raise ValueError(f"evaluation issue {field} is required")
    if payload["status"] == "pass" and (issues or not all(checks.values())):
        raise ValueError("passing evaluation cannot contain issues or failed checks")
    if payload["status"] == "revision_required" and not issues:
        raise ValueError("revision_required evaluation must contain an issue")
    if payload["status"] == "revision_required" and all(checks.values()):
        raise ValueError("revision_required evaluation must fail at least one check")
    if any(issue["stage"] == "segmentation" for issue in issues) and all(
        checks[name]
        for name in ("causal_chain_coherent", "evidence_verified", "segments_complete", "segments_unique")
    ):
        raise ValueError("segmentation issues must fail a segmentation check")
    if any(issue["stage"] == "categorization" for issue in issues) and checks["categories_valid"]:
        raise ValueError("categorization issues must fail categories_valid")
    if any(issue["stage"] == "translation" for issue in issues) and checks["evidence_verified"]:
        raise ValueError("translation issues must fail evidence_verified")
