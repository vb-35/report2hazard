from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from ..config import PipelineConfig
from ..core import normalize_text
from ..errors import PipelineError
from ..llm import ChatClient
from ..schemas import (
    IMMUTABLE_SEGMENT_FIELDS,
    canonicalize_row_labels,
    classification_response_schema,
    validate_classification_payload,
)


def _classification_prompt(config: PipelineConfig) -> str:
    labels = config.controlled_labels()
    return f"""
Categorize each supplied segment without rewriting it. Return JSON only as {{"rows":[...]}}. For each supplied segment,
return only its segment ID, generalized_category, interaction_type, sediment_transport_phase, and a short
classification_rationale list. Python retains the original causal order, event, process, and evidence unchanged.

Exact controlled values:
- generalized_category: {labels['generalized_category']}
- interaction_type: {labels['interaction_type']}
- sediment_transport_phase: {labels['sediment_transport_phase']}

Classify the step's effect on sediment connectivity, not ordinary social benefit, damage, or desirability.
For any controlled field, use "unknown" when the field applies but the evidence is insufficient to select a label.
Use "not applicable" when the field does not apply to the evidenced step. Explain either choice in classification_rationale;
do not use these labels instead of a specific label supported by the evidence.
Precedence and disambiguation:
- Interaction: Feedback for backflow/backwater/upstream or reverse response; otherwise Process-structure when a structure
  controls or is controlled by the process; otherwise Process-process when one natural process supplies, triggers, or
  alters another; otherwise Process-topography when terrain controls the process.
- Infrastructure: Positive Impact means overtopping, failure, damage, or destruction increases propagation, transport,
  or dispersion. Negative Impact means retention, trapping, blockage, clogging, or interruption. Use unqualified Impact
  only when a structure is affected and connectivity direction is unclear.
- Triggering Event is the active initiator. Material Mobilization recruits sediment. Changes in geomorphology requires
  explicit channel/landform reshaping. Post-event redistribution is delayed or secondary. Natural dam failure is only a
  natural blockage. Sediment Surge is a sediment-heavy downstream surge. Favourable Topography is terrain/channel form
  amplifying movement.
- Transport phase: Dysconnectivity for retention/blockage/clogging/interruption; otherwise Erosion for removal/recruitment;
  otherwise Deposition for settling/accumulation; otherwise Transportation for evidenced sediment movement.
  Use "not applicable" for pre-event conditions or triggers with no sediment movement or retention/blockage;
  use "unknown" when a sediment phase applies but cannot be determined from the evidence.

Representative Schnannerbach examples:
- "Previous landslide deposits remobilized into the main channel" -> Unstable pre-event conditions | Process-process | Transportation.
- "Sediment retention basin filled and was overtopped by debris flow" -> Positive Impact on permanent or temporary infrastructure | Process-structure | Transportation.
- "Bridge near Rosanna River became blocked by debris" -> Negative Impact on permanent or temporary infrastructure | Process-structure | Dysconnectivity.
- "Backflow from Rosanna River caused upstream flooding" -> Alteration of channel dynamics | Feedback | Transportation.
Never paraphrase or otherwise change a source segment.
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
        validate_classification_against_segments(payload, segments, config, requested_set)

    payload = client.complete_json(
        system_prompt=_classification_prompt(config),
        user_payload={
            "doc_id": segments["doc_id"],
            "controlled_labels": config.controlled_labels(),
            "segments": requested_segments,
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
        unaffected = set(source_by_id) - requested_set
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
